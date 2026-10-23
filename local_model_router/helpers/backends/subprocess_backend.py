"""
Subprocess Backend — runs llama-server as local processes.

This is the legacy behavior: expects llama.cpp binary installed on the
host (or accessible via WSL). Works on Windows, Linux, macOS.

Suitable when:
  - Running llama.cpp directly on the host (not in a container)
  - llama.cpp is already compiled locally
  - Docker is not available
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shlex
import signal
import socket
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

import psutil

from .base import BackendType, InferenceBackend, SlotStatus


class _OwnedProcess:
    """Small psutil adapter for a process adopted from persisted ownership state."""

    def __init__(self, process: Any):
        self._process = process
        self.pid = process.pid

    def poll(self):
        try:
            return None if self._process.is_running() else 0
        except Exception:
            return 0

    def send_signal(self, sig):
        self._process.send_signal(sig)

    def terminate(self):
        self._process.terminate()

    def kill(self):
        self._process.kill()

    def wait(self, timeout=None):
        return self._process.wait(timeout=timeout)


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
        self._started_at: Dict[str, float] = {}
        self._process_identities: Dict[str, float] = {}
        self._process_executables: Dict[str, str] = {}
        self._process_state_path: Optional[Path] = None

    @property
    def backend_type(self) -> BackendType:
        return BackendType.SUBPROCESS

    # ── Public API ──────────────────────────────────────────────────

    async def start_slot(self, name: str, config: Dict[str, Any]) -> SlotStatus:
        restart_count = 0
        if name in self._processes:
            proc = self._processes[name]
            if proc.poll() is None:  # still running
                self.logger.info(f"Slot '{name}' already running (pid={proc.pid})")
                return await self.health_check(name)
            # Process exited — clean up
            restart_count = self._slots.get(name, SlotStatus(name=name)).restart_count + 1
            del self._processes[name]
            self._started_at.pop(name, None)
            self._process_identities.pop(name, None)

        port = int(config.get("port", 8080))
        status = SlotStatus(
            name=name,
            port=port,
            host="localhost",
            model_id=config.get("model_id", ""),
            restart_count=restart_count,
        )

        if not self._valid_slot_name(name):
            status.error = "Invalid slot name"
            status.extra["failure_code"] = "invalid_slot_name"
            self._slots[name] = status
            return status

        # Check model file
        model_path = config.get("model_path", "")
        if not model_path or not os.path.exists(model_path):
            status.error = "Model file not found"
            status.extra["failure_code"] = "model_missing"
            self.logger.error(status.error)
            self._slots[name] = status
            return status

        if self._port_open("127.0.0.1", port):
            status.error = "Port is already in use by another or unverifiable process"
            status.extra["failure_code"] = "port_in_use"
            self._slots[name] = status
            return status

        proc = None
        try:
            cmd = self._build_command(config)
            self.logger.info("Starting slot '%s' on port %s", name, port)

            env = os.environ.copy()
            cuda_devices = self.global_config.get("cuda_visible_devices")
            if cuda_devices and cuda_devices != "auto":
                env["CUDA_VISIBLE_DEVICES"] = str(cuda_devices)

            log_dir = self.global_config.get("log_dir", "logs/llama_cpp")
            os.makedirs(log_dir, exist_ok=True)
            log_file = open(os.path.join(log_dir, f"{name}.log"), "a", encoding="utf-8")

            use_wsl = self.global_config.get("use_wsl", False)

            try:
                if use_wsl and sys.platform == "win32":
                    full_cmd = ["wsl", "bash", "-lc", shlex.join(cmd)]
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
            finally:
                log_file.close()

            self._processes[name] = proc
            self._started_at[name] = time.monotonic()
            identity = self._process_create_time(proc.pid)
            executable = self._process_executable(proc.pid)
            if identity is not None:
                self._process_identities[name] = identity
            if executable:
                self._process_executables[name] = executable
            status.pid = proc.pid

            if identity is None or not executable:
                status.running = proc.poll() is None
                status.error = "Could not verify ownership of the started process"
                status.extra["failure_code"] = "process_ownership_unverified"
                self._slots[name] = status
                await self._stop_known_process(name, proc)
                return status
            self._process_identities[name] = identity
            self._process_executables[name] = executable
            try:
                # Persist ownership before the potentially long model load so a
                # restarted router can still adopt and stop this exact process.
                self._write_process_state(name, config, proc.pid, identity, executable)
            except OSError:
                await self._stop_known_process(name, proc)
                status.running = False
                status.healthy = False
                status.error = "Could not persist process ownership"
                status.extra["failure_code"] = "state_write_failed"
                return status

            timeout = self._get_startup_timeout()
            if await self._wait_healthy(port, timeout, proc):
                status.running = True
                status.healthy = True
                status.uptime_s = time.monotonic() - self._started_at[name]
                self.logger.info(f"Slot '{name}' ready on port {port} (pid={proc.pid})")
            else:
                exit_code = proc.poll()
                status.running = exit_code is None
                if exit_code is None:
                    status.error = "Process started but health check timed out"
                    status.extra["failure_code"] = "health_timeout"
                else:
                    status.error = f"Process exited during startup (exit code {exit_code})"
                    status.extra.update(failure_code="process_exited", exit_code=exit_code)
                    self._remove_process_state(proc.pid)
                self.logger.error(f"Slot '{name}' failed health check")

        except Exception as e:
            status.error = "Could not start local model process"
            status.extra["failure_code"] = "start_failed"
            status.extra["exception_type"] = type(e).__name__
            if proc is not None:
                exit_code = proc.poll()
                status.running = exit_code is None
                if exit_code is not None:
                    status.extra["exit_code"] = exit_code
            self.logger.error("Failed to start slot '%s' (%s)", name, type(e).__name__)

        self._slots[name] = status
        return status

    async def stop_slot(self, name: str) -> bool:
        proc = self._processes.get(name)
        if not proc:
            return False

        expected_identity = self._process_identities.get(name)
        expected_executable = self._process_executables.get(name)
        current_identity = self._process_create_time(proc.pid)
        current_executable = self._process_executable(proc.pid)
        identity_changed = (
            expected_identity is not None
            and current_identity is not None
            and abs(current_identity - expected_identity) > 0.001
        )
        ownership_unverified = (
            expected_identity is None
            or current_identity is None
            or not expected_executable
            or not current_executable
            or (
                not identity_changed
                and os.path.normcase(os.path.realpath(current_executable))
                != os.path.normcase(os.path.realpath(expected_executable))
            )
        )
        if identity_changed or ownership_unverified:
            status = self._slots.get(name)
            if status:
                if identity_changed:
                    status.error = "Process identity changed; refusing to stop"
                    status.extra["failure_code"] = "process_identity_changed"
                else:
                    status.error = "Process ownership could not be verified; refusing to stop"
                    status.extra["failure_code"] = "process_ownership_unverified"
            return False

        try:
            if proc.poll() is None:
                if sys.platform == "win32":
                    proc.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    proc.terminate()

                try:
                    await asyncio.to_thread(proc.wait, 10)
                except (subprocess.TimeoutExpired, psutil.TimeoutExpired, TimeoutError):
                    proc.kill()
                    await asyncio.to_thread(proc.wait, 10)

            self._processes.pop(name, None)
            self._slots.pop(name, None)
            self._started_at.pop(name, None)
            self._process_identities.pop(name, None)
            self._process_executables.pop(name, None)
            self._remove_process_state(proc.pid)
            self.logger.info(f"Stopped slot '{name}'")
            return True
        except Exception as e:
            self.logger.warning("Error stopping slot '%s' (%s)", name, type(e).__name__)
            return False

    async def health_check(self, name: str) -> SlotStatus:
        status = self._slots.get(name)
        if not status:
            return SlotStatus(name=name, error="Unknown slot")

        proc = self._processes.get(name)
        exit_code = proc.poll() if proc else None
        if not proc or exit_code is not None:
            status.running = False
            status.healthy = False
            status.error = "Process not running"
            status.extra["failure_code"] = "process_exited"
            if exit_code is not None:
                status.extra["exit_code"] = exit_code
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

        if status.healthy:
            status.error = None
            status.extra.pop("failure_code", None)
            status.extra.pop("exit_code", None)
            started_at = self._started_at.get(name)
            if started_at is not None:
                status.uptime_s = time.monotonic() - started_at
        else:
            status.error = "Health check failed"
            status.extra["failure_code"] = "health_probe_failed"

        return status

    async def list_slots(self) -> Dict[str, SlotStatus]:
        return dict(self._slots)

    async def cleanup(self) -> None:
        names = list(self._processes.keys())
        for name in names:
            await self.stop_slot(name)
        self.logger.info(f"Cleaned up {len(names)} process(es)")

    def configure_process_state(self, path: str | os.PathLike[str]) -> None:
        self._process_state_path = Path(path).resolve(strict=False)

    def owns_slot(self, name: str) -> bool:
        proc = self._processes.get(name)
        return bool(proc is not None and proc.poll() is None)

    def adopt_slot(self, name: str, config: Dict[str, Any]) -> bool:
        """Adopt only a process whose persisted PID, start time, executable and model all match."""
        path = self._process_state_path
        if path is None or not path.is_file() or name in self._processes:
            return False
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            pid = int(state.get("pid") or 0)
            port = int(config.get("port") or 8080)
            expected_created = float(state["process_created_at"])
            expected_executable = str(state["process_executable"])
            expected_model = str(state.get("model") or "")
            if (
                pid <= 0
                or str(state.get("slot_id") or "") != name
                or int(state.get("port") or 0) != port
            ):
                return False
            process = psutil.Process(pid)
            actual_executable = str(Path(process.exe()).resolve(strict=False))
            command = {
                str(Path(arg).resolve(strict=False)).casefold()
                for arg in process.cmdline()
                if arg
            }
            recorded_model = (
                str(Path(expected_model).resolve(strict=False)).casefold()
                if expected_model
                else ""
            )
            if (
                abs(process.create_time() - expected_created) >= 2.0
                or os.path.normcase(os.path.realpath(actual_executable))
                != os.path.normcase(os.path.realpath(expected_executable))
                or not recorded_model
                or recorded_model not in command
            ):
                return False
            configured_model = str(config.get("model_path") or "")
            if configured_model and (
                os.path.normcase(os.path.realpath(configured_model))
                != os.path.normcase(os.path.realpath(expected_model))
            ):
                return False
            self._processes[name] = _OwnedProcess(process)
            self._process_identities[name] = expected_created
            self._process_executables[name] = actual_executable
            self._started_at[name] = time.monotonic()
            self._slots[name] = SlotStatus(
                name=name,
                port=port,
                host="localhost",
                model_id=str(config.get("model_id") or ""),
                pid=pid,
                running=True,
            )
            self.logger.info("Adopted verified managed slot '%s' (pid=%s)", name, pid)
            return True
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError, psutil.Error):
            return False

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

    @staticmethod
    def _valid_slot_name(name: str) -> bool:
        return bool(name and name not in {".", ".."} and os.path.basename(name) == name and "/" not in name and "\\" not in name)

    @staticmethod
    def _process_create_time(pid: int) -> Optional[float]:
        try:
            return float(psutil.Process(pid).create_time())
        except (psutil.Error, OSError, ValueError):
            return None

    @staticmethod
    def _process_executable(pid: int) -> Optional[str]:
        try:
            return str(Path(psutil.Process(pid).exe()).resolve(strict=False))
        except (psutil.Error, OSError, ValueError):
            return None

    @staticmethod
    def _port_open(host: str, port: int) -> bool:
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return True
        except OSError:
            return False

    async def _stop_known_process(self, name: str, proc: Any) -> None:
        """Stop the exact Popen object created by this backend after an ownership-read failure."""
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    await asyncio.to_thread(proc.wait, 10)
                except (subprocess.TimeoutExpired, TimeoutError):
                    proc.kill()
                    await asyncio.to_thread(proc.wait, 10)
        except Exception:
            pass
        finally:
            self._processes.pop(name, None)
            self._slots.pop(name, None)
            self._started_at.pop(name, None)
            self._process_identities.pop(name, None)
            self._process_executables.pop(name, None)

    def _write_process_state(
        self,
        name: str,
        config: Dict[str, Any],
        pid: int,
        created_at: float,
        executable: str,
    ) -> None:
        path = self._process_state_path
        if path is None:
            return
        payload = {
            "schema_version": 2,
            "slot_id": name,
            "pid": pid,
            "binary": executable,
            "process_executable": executable,
            "process_created_at": created_at,
            "model": str(config.get("model_path") or ""),
            "port": int(config.get("port") or 8080),
            "visible_terminal": bool(config.get("visible_terminal")),
            "started_at": time.time(),
            "log": str(Path(str(self.global_config.get("log_dir") or "logs/llama_cpp")) / f"{name}.log"),
        }
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _remove_process_state(self, pid: int) -> None:
        path = self._process_state_path
        if path is None or not path.is_file():
            return
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            if int(state.get("pid") or 0) == pid:
                path.unlink(missing_ok=True)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return

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

        mmproj = str(config.get("mmproj_path") or "").strip()
        if mmproj:
            if use_wsl:
                mmproj = self._convert_wsl_path(mmproj)
            cmd.extend(["--mmproj", mmproj])

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
