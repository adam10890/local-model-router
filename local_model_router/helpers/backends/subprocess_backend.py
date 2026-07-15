"""
Subprocess Backend — runs llama-server as local processes.

This is the legacy behavior: expects llama.cpp binary installed on the
host (or accessible via WSL). Works on Windows, Linux, macOS.

Suitable when:
  - Running Agent Zero directly on host (not in container)
  - llama.cpp is already compiled locally
  - Docker is not available
"""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

from .base import BackendType, InferenceBackend, SlotStatus


class SubprocessBackend(InferenceBackend):
    """
    Manages llama-server instances as local subprocesses.

    Preserves all the existing behavior from the original LlamaCppManager:
    WSL support, Windows path conversion, process groups, etc.
    """

    def __init__(self, global_config: Dict[str, Any]):
        super().__init__(global_config)
        self._processes: Dict[str, subprocess.Popen] = {}
        self._slots: Dict[str, SlotStatus] = {}

    @property
    def backend_type(self) -> BackendType:
        return BackendType.SUBPROCESS

    # ── Public API ──────────────────────────────────────────────────

    async def start_slot(self, name: str, config: Dict[str, Any]) -> SlotStatus:
        if name in self._processes:
            proc = self._processes[name]
            if proc.poll() is None:  # still running
                self.logger.info(f"Slot '{name}' already running (pid={proc.pid})")
                return self._slots[name]
            # Process exited — clean up
            del self._processes[name]

        port = int(config.get("port", 8080))
        status = SlotStatus(
            name=name,
            port=port,
            host="localhost",
            model_id=config.get("model_id", ""),
        )

        # Check model file
        model_path = config.get("model_path", "")
        if not model_path or not os.path.exists(model_path):
            status.error = f"Model file not found: {model_path}"
            self.logger.error(status.error)
            self._slots[name] = status
            return status

        try:
            cmd = self._build_command(config)
            self.logger.info(f"Starting slot '{name}': {' '.join(cmd[:6])}...")

            env = os.environ.copy()
            cuda_devices = self.global_config.get("cuda_visible_devices")
            if cuda_devices and cuda_devices != "auto":
                env["CUDA_VISIBLE_DEVICES"] = str(cuda_devices)

            log_dir = self.global_config.get("log_dir", "logs/llama_cpp")
            os.makedirs(log_dir, exist_ok=True)
            log_file = open(os.path.join(log_dir, f"{name}.log"), "a")

            use_wsl = self.global_config.get("use_wsl", False)

            if use_wsl and sys.platform == "win32":
                wsl_cmd = " ".join(cmd)
                full_cmd = ["wsl", "bash", "-c", f"nohup {wsl_cmd} > /dev/null 2>&1 &"]
                proc = subprocess.Popen(
                    full_cmd,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    env=env,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                )
            else:
                visible = bool(config.get("visible_terminal"))
                creation = (
                    subprocess.CREATE_NEW_CONSOLE
                    if visible and sys.platform == "win32"
                    else subprocess.CREATE_NEW_PROCESS_GROUP
                    if sys.platform == "win32"
                    else 0
                )
                proc = subprocess.Popen(
                    cmd,
                    stdout=None if visible else log_file,
                    stderr=None if visible else subprocess.STDOUT,
                    env=env,
                    creationflags=creation,
                )

            self._processes[name] = proc
            status.pid = proc.pid

            timeout = self._get_startup_timeout()
            if await self._wait_healthy(port, timeout, proc):
                status.running = True
                status.healthy = True
                self.logger.info(f"Slot '{name}' ready on port {port} (pid={proc.pid})")
            else:
                status.error = "Process started but health check failed"
                self.logger.error(f"Slot '{name}' failed health check")

        except Exception as e:
            status.error = str(e)
            self.logger.error(f"Failed to start slot '{name}': {e}")

        self._slots[name] = status
        return status

    async def stop_slot(self, name: str) -> bool:
        # Capture port before popping so WSL kill path still works
        slot_status = self._slots.get(name)
        wsl_port = slot_status.port if slot_status else None

        proc = self._processes.pop(name, None)
        self._slots.pop(name, None)

        if not proc:
            return False

        use_wsl = self.global_config.get("use_wsl", False)

        try:
            if use_wsl and sys.platform == "win32":
                # Find and kill process by port in WSL
                if wsl_port:
                    kill_cmd = f"kill $(lsof -t -i:{wsl_port}) 2>/dev/null || true"
                    subprocess.run(["wsl", "bash", "-c", kill_cmd], timeout=10)
            elif proc.poll() is None:
                if sys.platform == "win32":
                    proc.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    proc.terminate()

                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()

            self.logger.info(f"Stopped slot '{name}'")
            return True
        except Exception as e:
            self.logger.warning(f"Error stopping slot '{name}': {e}")
            return False

    async def health_check(self, name: str) -> SlotStatus:
        status = self._slots.get(name)
        if not status:
            return SlotStatus(name=name, error="Unknown slot")

        proc = self._processes.get(name)
        if not proc or proc.poll() is not None:
            status.running = False
            status.healthy = False
            status.error = "Process not running"
            return status

        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                url = f"http://localhost:{status.port}/health"
                async with session.get(url, timeout=5) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        status.healthy = data.get("status") == "ok"
                        status.running = True
                    else:
                        status.healthy = False
        except Exception:
            status.healthy = False

        return status

    async def list_slots(self) -> Dict[str, SlotStatus]:
        return dict(self._slots)

    async def cleanup(self) -> None:
        names = list(self._processes.keys())
        for name in names:
            await self.stop_slot(name)
        self.logger.info(f"Cleaned up {len(names)} process(es)")

    # ── Private helpers ─────────────────────────────────────────────

    def _get_binary(self) -> str:
        use_wsl = self.global_config.get("use_wsl", False)
        if use_wsl:
            wsl_path = self.global_config.get("llama_cpp_path_wsl", "~/llama.cpp/build/bin")
            binary = self.global_config.get("server_binary", "llama-server")
            return f"{wsl_path}/{binary}"

        llama_path = self.global_config.get("llama_cpp_path", "")
        binary = self.global_config.get("server_binary", "llama-server")

        if llama_path:
            full = os.path.join(llama_path, binary)
            if os.path.exists(full):
                return full
            if sys.platform == "win32" and os.path.exists(full + ".exe"):
                return full + ".exe"

        return binary  # fallback to PATH

    def _convert_wsl_path(self, win_path: str) -> str:
        if not win_path:
            return win_path
        path = win_path.replace("\\", "/")
        if len(path) >= 2 and path[1] == ":":
            drive = path[0].lower()
            path = f"/mnt/{drive}{path[2:]}"
        return path

    def _build_command(self, config: Dict[str, Any]) -> List[str]:
        use_wsl = self.global_config.get("use_wsl", False)
        binary = self._get_binary()

        port = int(config.get("port", 8080))
        ctx = int(config.get("context_size", 8192))
        batch = int(config.get("batch_size", 512))
        threads = int(config.get("threads", 4))
        parallel = int(config.get("parallel_slots", 1))
        gpu_layers = config.get("gpu_layers", -1)

        cmd = [binary]

        if config.get("router_mode"):
            # ── Router Mode: directory-based hot-swap ─────────────────
            rdir = config.get("router_models_dir", "")
            if use_wsl and rdir:
                rdir = self._convert_wsl_path(rdir)
            preset = config.get("router_models_preset", "")
            if use_wsl and preset:
                preset = self._convert_wsl_path(preset)
            if rdir and not preset:
                cmd.extend(["--models-dir", rdir])
            if config.get("router_models_autoload", True):
                cmd.append("--models-autoload")
            if preset:
                cmd.extend(["--models-preset", preset])
            rmax = int(config.get("router_models_max", 1))
            if rmax > 0:
                cmd.extend(["--models-max", str(rmax)])
            # Pre-load default model (set via dashboard)
            default_alias = config.get("router_default_model", "")
            if default_alias and preset:
                from local_model_router.helpers.llama_cpp_manager import resolve_preset_alias  # noqa: PLC0415
                path = resolve_preset_alias(
                    preset if not use_wsl else self._convert_wsl_path(preset),
                    default_alias,
                    rdir,
                )
                if path:
                    cmd.extend(["--model", path])
        else:
            # ── Single-model mode (default) ────────────────────────────
            model_path = config.get("model_path", "")
            if use_wsl:
                model_path = self._convert_wsl_path(model_path)
            cmd.extend(["-m", model_path])

        cmd += [
            "-c", str(ctx),
            "-b", str(batch),
            "-t", str(threads),
            "-np", str(parallel),
            "--port", str(port),
            "--host", str(config.get("host") or "127.0.0.1"),
        ]

        alias = str(config.get("model_id") or "").strip()
        if alias:
            cmd.extend(["--alias", alias])

        if gpu_layers != 0:
            cmd.extend(["-ngl", str(gpu_layers)])

        fa = config.get("flash_attention")
        if fa is True:
            cmd.extend(["--flash-attn", "on"])
        elif fa is False:
            cmd.extend(["--flash-attn", "off"])

        if config.get("fit", True):
            cmd.extend(["--fit", "on"])
            fit_target = config.get("fit_target_mib", 1024)
            if fit_target > 0:
                cmd.extend(["--fit-target", str(fit_target)])

        if config.get("embedding_mode", False):
            cmd.append("--embedding")

        rf = config.get("reasoning_format", "")
        if rf:
            cmd.extend(["--reasoning-format", rf])

        if config.get("jinja") is True:
            cmd.append("--jinja")
        elif config.get("jinja") is False:
            cmd.append("--no-jinja")

        # Extra args passthrough
        extra = config.get("extra_args", [])
        if extra:
            cmd.extend(extra)

        return cmd

    async def _wait_healthy(
        self, port: int, timeout: int, proc: subprocess.Popen
    ) -> bool:
        import aiohttp

        start = time.time()
        while time.time() - start < timeout:
            if proc.poll() is not None:
                return False  # process exited
            try:
                async with aiohttp.ClientSession() as session:
                    url = f"http://localhost:{port}/health"
                    async with session.get(url, timeout=5) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if data.get("status") == "ok":
                                return True
            except Exception:
                pass
            await asyncio.sleep(2)
        return False
