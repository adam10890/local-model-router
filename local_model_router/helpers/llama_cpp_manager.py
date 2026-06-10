"""
llama.cpp Server Manager for Agent Zero

Manages multiple llama.cpp server instances for local LLM inference.
Supports Mixture of Agents (MoA) architectures with specialized models.
"""

import os
import sys
import yaml
import asyncio
import subprocess
import aiohttp
import logging
from pathlib import Path
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from enum import Enum
import signal
import time

# NOTE: we intentionally do NOT `from helpers import files` at module scope.
# A0 core's `helpers.files` transitively imports `simpleeval`, which is
# listed in /a0/requirements.txt but has been observed missing from the
# `agent0ai/agent-zero:latest` image's venv. That import failure would
# cascade and make every slot operation break silently (`slots=[]` in the
# dashboard). We keep the plugin self-contained by resolving the default
# config path relative to THIS file instead.


def _default_config_path() -> str:
    """Default A0-root conf path, resolved without `helpers.files`.

    Layout (inside the A0 container):
        /a0/conf/llama_cpp_servers.yaml            <- what we return
        /a0/usr/plugins/a0_lmm_router/helpers/     <- this file's parents[3]/helpers
    """
    return str(Path(__file__).resolve().parents[4] / "conf" / "llama_cpp_servers.yaml")


class ServerRole(Enum):
    CHAT = "chat"
    UTILITY = "utility"
    EMBEDDING = "embedding"
    ROUTER = "router"
    SUBAGENT = "subagent"


class ServerStatus(Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    ERROR = "error"
    STOPPING = "stopping"


@dataclass
class ServerConfig:
    """Configuration for a single llama.cpp server instance."""
    name: str
    enabled: bool = True
    port: int = 8080
    role: ServerRole = ServerRole.CHAT
    specialty: Optional[str] = None
    model_path: str = ""
    model_id: str = ""  # logical model name (e.g., magistral_small_12), shown in UI
    gpu_layers: int = -1
    context_size: int = 4096
    batch_size: int = 512
    threads: int = 4
    parallel_slots: int = 1
    # Flash attention: bool or str "on"/"off"/"auto"
    flash_attention: Any = True
    embedding_mode: bool = False
    extra_args: List[str] = field(default_factory=list)
    
    # A.2 New fields for llama.cpp b8047+ upgrade
    # Auto-fit VRAM (Phase C)
    fit: bool = True
    fit_target_mib: int = 1024
    fit_ctx_min: int = 4096
    
    # Jinja templating (Phase B)
    jinja: Optional[bool] = None  # None=default, True/False=explicit
    
    # Reasoning models (Phase E)
    reasoning_format: str = ""  # "deepseek", "deepseek-legacy", "none", "auto"
    reasoning_budget: int = -1   # -1=unlimited, 0=disabled, >0=tokens
    
    # Context checkpoints (advanced)
    ctx_checkpoints: int = 0
    
    # KV cache unified
    kv_unified: bool = False
    
    # RAM cache
    cache_ram_mib: int = 0
    
    # Multimodal / Vision (Phase D)
    mmproj_path: str = ""  # Vision projector path
    
    # Speculative decoding (Phase F)
    # draft-mtp: uses MTP heads baked into the model (no separate draft model).
    # draft-simple / draft-eagle3: require a separate draft model via draft_model_path.
    # ngram-*: no model needed, works on any model.
    spec_type: str = ""          # --spec-type: "draft-mtp" | "draft-simple" | "ngram-simple" | etc.
    draft_model_path: str = ""
    draft_max: int = 0           # --spec-draft-n-max (0 = use server default of 3)
    draft_min: int = 0           # --spec-draft-n-min
    draft_p_min: float = 0.0     # --spec-draft-p-min (server default is 0.0)
    
    # TTS (Phase H)
    vocoder_path: str = ""
    tts_use_guide_tokens: bool = False
    
    # LoRA hot-swap (Phase H)
    lora_path: str = ""
    lora_init_without_apply: bool = False
    
    # Reranking (Phase H)
    rerank: bool = False
    
    # Prometheus metrics (Phase H)
    metrics: bool = False
    
    # SSL/TLS (Phase H)
    ssl_key_file: str = ""
    ssl_cert_file: str = ""

    # Native Router Mode (llama.cpp --models-dir / --models-autoload)
    # When router_mode=True the server registers ALL models in models_dir and
    # hot-swaps between them on demand. --model is NOT passed in this mode.
    router_mode: bool = False
    router_models_dir: str = ""       # container/host path to GGUF directory
    router_models_autoload: bool = True  # pass --models-autoload at startup
    router_models_preset: str = ""    # path to .ini preset file
    router_models_max: int = 1        # 1 = hot-swap; >1 = keep N models in VRAM
    # Default model alias (from preset.ini) to pre-load on startup via --model.
    # Set from the dashboard; persisted in conf/router_state.json.
    router_default_model: str = ""


@dataclass
class ServerInstance:
    """Runtime state for a server instance."""
    config: ServerConfig
    status: ServerStatus = ServerStatus.STOPPED
    process: Optional[subprocess.Popen] = None
    pid: Optional[int] = None
    start_time: Optional[float] = None
    error_message: Optional[str] = None
    restart_count: int = 0


class LlamaCppManager:
    """
    Manages llama.cpp server instances for Agent Zero.
    
    Features:
    - Start/stop individual servers or all servers
    - Health monitoring with auto-restart
    - VRAM budget management
    - Dynamic server configuration
    """
    
    _instance: Optional['LlamaCppManager'] = None
    
    def __init__(self, config_path: Optional[str] = None):
        self.config_path = config_path or _default_config_path()
        self.servers: Dict[str, ServerInstance] = {}
        self.global_config: Dict[str, Any] = {}
        self.logger = logging.getLogger("llama_cpp_manager")
        self._health_check_task: Optional[asyncio.Task] = None
        self._load_config()
    
    @classmethod
    def get_instance(cls, config_path: Optional[str] = None) -> 'LlamaCppManager':
        """Get singleton instance of the manager."""
        if cls._instance is None:
            cls._instance = cls(config_path)
        return cls._instance
    
    def _load_config(self) -> None:
        """Load server configurations from YAML file."""
        if not os.path.exists(self.config_path):
            self.logger.warning(f"Config file not found: {self.config_path}")
            return
            
        with open(self.config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f) or {}
        
        self.global_config = config.get('global', {})
        
        # Expand environment variables in global config
        self._expand_env_vars(self.global_config)
        
        # Load model cards for resolving model_id -> model_path
        model_cards = self._load_model_cards()
        
        # Load from active_slots (current format: list of slot dicts)
        active_slots = config.get('active_slots', [])
        if active_slots:
            slot_defaults = config.get('slot_defaults', {})
            for slot in active_slots:
                if slot is None:
                    continue
                
                name = slot.get('id', f"slot_{slot.get('port', 'unknown')}")
                
                # Resolve model_id to model_path via model_cards
                model_path = slot.get('model_path', '')
                model_id = slot.get('model_id', '')
                if not model_path and model_id and model_cards:
                    model_path = self._resolve_model_path(model_id, model_cards)
                
                # Parse role
                role_str = slot.get('role', 'chat')
                try:
                    role = ServerRole(role_str)
                except ValueError:
                    role = ServerRole.CHAT
                
                # Check if this is an embedding slot (by role or extra_args)
                extra_args = slot.get('extra_args', slot_defaults.get('extra_args', []))
                embedding_mode = role == ServerRole.EMBEDDING or '--embedding' in extra_args
                
                server_config = ServerConfig(
                    name=name,
                    enabled=slot.get('enabled', True),
                    port=slot.get('port', 8080),
                    role=role,
                    specialty=slot.get('specialty'),
                    model_path=model_path,
                    model_id=model_id,
                    gpu_layers=slot.get('gpu_layers', slot_defaults.get('gpu_layers', -1)),
                    context_size=slot.get('context_size', slot_defaults.get('context_size', 4096)),
                    batch_size=slot.get('batch_size', slot_defaults.get('batch_size', 512)),
                    threads=slot.get('threads', slot_defaults.get('threads', 4)),
                    parallel_slots=slot.get('parallel_slots', slot_defaults.get('parallel_slots', 1)),
                    flash_attention=slot.get('flash_attention', slot_defaults.get('flash_attention', True)),
                    embedding_mode=embedding_mode,
                    extra_args=extra_args,
                    # A.4 New fields from llama.cpp b8047+ upgrade
                    fit=slot.get('fit', slot_defaults.get('fit', True)),
                    fit_target_mib=slot.get('fit_target_mib', slot_defaults.get('fit_target_mib', 1024)),
                    fit_ctx_min=slot.get('fit_ctx_min', slot_defaults.get('fit_ctx_min', 4096)),
                    jinja=slot.get('jinja', slot_defaults.get('jinja')),
                    reasoning_format=slot.get('reasoning_format', slot_defaults.get('reasoning_format', '')),
                    reasoning_budget=slot.get('reasoning_budget', slot_defaults.get('reasoning_budget', -1)),
                    ctx_checkpoints=slot.get('ctx_checkpoints', slot_defaults.get('ctx_checkpoints', 0)),
                    kv_unified=slot.get('kv_unified', slot_defaults.get('kv_unified', False)),
                    cache_ram_mib=slot.get('cache_ram_mib', slot_defaults.get('cache_ram_mib', 0)),
                    mmproj_path=slot.get('mmproj_path', slot_defaults.get('mmproj_path', '')),
                    spec_type=slot.get('spec_type', slot_defaults.get('spec_type', '')),
                    draft_model_path=slot.get('draft_model_path', slot_defaults.get('draft_model_path', '')),
                    draft_max=slot.get('draft_max', slot_defaults.get('draft_max', 0)),
                    draft_min=slot.get('draft_min', slot_defaults.get('draft_min', 0)),
                    draft_p_min=slot.get('draft_p_min', slot_defaults.get('draft_p_min', 0.0)),
                    vocoder_path=slot.get('vocoder_path', slot_defaults.get('vocoder_path', '')),
                    tts_use_guide_tokens=slot.get('tts_use_guide_tokens', slot_defaults.get('tts_use_guide_tokens', False)),
                    lora_path=slot.get('lora_path', slot_defaults.get('lora_path', '')),
                    lora_init_without_apply=slot.get('lora_init_without_apply', slot_defaults.get('lora_init_without_apply', False)),
                    rerank=slot.get('rerank', slot_defaults.get('rerank', False)),
                    metrics=slot.get('metrics', slot_defaults.get('metrics', False)),
                    ssl_key_file=slot.get('ssl_key_file', slot_defaults.get('ssl_key_file', '')),
                    ssl_cert_file=slot.get('ssl_cert_file', slot_defaults.get('ssl_cert_file', '')),
                    # Router Mode fields
                    router_mode=slot.get('router_mode', slot_defaults.get('router_mode', False)),
                    router_models_dir=slot.get('router_models_dir', slot_defaults.get('router_models_dir', '')),
                    router_models_autoload=slot.get('router_models_autoload', slot_defaults.get('router_models_autoload', True)),
                    router_models_preset=slot.get('router_models_preset', slot_defaults.get('router_models_preset', '')),
                    router_models_max=slot.get('router_models_max', slot_defaults.get('router_models_max', 1)),
                )

                self.servers[name] = ServerInstance(config=server_config)
        
        # Also load legacy 'servers' dict format for backward compatibility
        servers_config = config.get('servers', {})
        for name, server_conf in servers_config.items():
            if server_conf is None:
                continue
            
            # Expand environment variables
            self._expand_env_vars(server_conf)
            
            # Resolve model_id to model_path if needed
            model_path = server_conf.get('model_path', '')
            model_id = server_conf.get('model_id', '')
            if not model_path and model_id and model_cards:
                model_path = self._resolve_model_path(model_id, model_cards)
            
            # Parse role
            role_str = server_conf.get('role', 'chat')
            try:
                role = ServerRole(role_str)
            except ValueError:
                role = ServerRole.CHAT
            
            server_config = ServerConfig(
                name=name,
                enabled=server_conf.get('enabled', True),
                port=server_conf.get('port', 8080),
                role=role,
                specialty=server_conf.get('specialty'),
                model_path=model_path,
                model_id=model_id,
                gpu_layers=server_conf.get('gpu_layers', -1),
                context_size=server_conf.get('context_size', 4096),
                batch_size=server_conf.get('batch_size', 512),
                threads=server_conf.get('threads', 4),
                parallel_slots=server_conf.get('parallel_slots', 1),
                flash_attention=server_conf.get('flash_attention', True),
                embedding_mode=server_conf.get('embedding_mode', False),
                extra_args=server_conf.get('extra_args', []),
                # A.4 New fields from llama.cpp b8047+ upgrade
                fit=server_conf.get('fit', True),
                fit_target_mib=server_conf.get('fit_target_mib', 1024),
                fit_ctx_min=server_conf.get('fit_ctx_min', 4096),
                jinja=server_conf.get('jinja'),
                reasoning_format=server_conf.get('reasoning_format', ''),
                reasoning_budget=server_conf.get('reasoning_budget', -1),
                ctx_checkpoints=server_conf.get('ctx_checkpoints', 0),
                kv_unified=server_conf.get('kv_unified', False),
                cache_ram_mib=server_conf.get('cache_ram_mib', 0),
                mmproj_path=server_conf.get('mmproj_path', ''),
                spec_type=server_conf.get('spec_type', ''),
                draft_model_path=server_conf.get('draft_model_path', ''),
                draft_max=server_conf.get('draft_max', 0),
                draft_min=server_conf.get('draft_min', 0),
                draft_p_min=server_conf.get('draft_p_min', 0.0),
                vocoder_path=server_conf.get('vocoder_path', ''),
                tts_use_guide_tokens=server_conf.get('tts_use_guide_tokens', False),
                lora_path=server_conf.get('lora_path', ''),
                lora_init_without_apply=server_conf.get('lora_init_without_apply', False),
                rerank=server_conf.get('rerank', False),
                metrics=server_conf.get('metrics', False),
                ssl_key_file=server_conf.get('ssl_key_file', ''),
                ssl_cert_file=server_conf.get('ssl_cert_file', ''),
            )
            
            self.servers[name] = ServerInstance(config=server_config)
    
    def _load_model_cards(self) -> Dict[str, Any]:
        """Load model definitions for resolving model_id to model_path.
        
        Merges data from both model_cards.yaml (detailed specs) and
        installed_models.yaml (logical IDs used by active_slots).
        """
        merged: Dict[str, Any] = {}
        conf_dir = os.path.dirname(self.config_path)
        
        # Load model_cards.yaml (detailed model specs)
        try:
            cards_path = os.path.join(conf_dir, 'model_cards.yaml')
            if os.path.exists(cards_path):
                with open(cards_path, 'r', encoding='utf-8') as f:
                    data = yaml.safe_load(f) or {}
                merged.update(data.get('models', {}))
        except Exception as e:
            self.logger.warning(f"Could not load model_cards.yaml: {e}")
        
        # Load installed_models.yaml (logical IDs like micro_router_phi)
        try:
            installed_path = os.path.join(conf_dir, 'installed_models.yaml')
            if os.path.exists(installed_path):
                with open(installed_path, 'r', encoding='utf-8') as f:
                    data = yaml.safe_load(f) or {}
                installed_models = data.get('models', {})
                installed_models_dir = data.get('models_path', '')
                for mid, mconf in installed_models.items():
                    if mid not in merged and mconf:
                        # Convert installed_models format to model_cards-like format
                        rel_path = mconf.get('path', '')
                        filename = mconf.get('file', '')
                        if rel_path and filename:
                            full_rel = os.path.join(rel_path, filename)
                        elif filename:
                            full_rel = filename
                        else:
                            full_rel = ''
                        merged[mid] = {
                            'file': {
                                'path': full_rel,
                            },
                            '_installed_models_dir': installed_models_dir,
                        }
        except Exception as e:
            self.logger.warning(f"Could not load installed_models.yaml: {e}")
        
        return merged
    
    def _resolve_model_path(self, model_id: str, model_cards: Dict[str, Any]) -> str:
        """Resolve a model_id to a full model file path using model_cards."""
        card = model_cards.get(model_id)
        if not card:
            self.logger.warning(f"Model card not found for model_id: {model_id}")
            return ''
        
        file_info = card.get('file', {})
        relative_path = file_info.get('path', '')
        if not relative_path:
            self.logger.warning(f"No file path in model card for: {model_id}")
            return ''
        
        # Build full path using models_dir from global config
        models_dir = self.global_config.get('models_dir', '')
        if models_dir:
            return os.path.join(models_dir, relative_path)
        
        # Fallback: use models_path from installed_models.yaml
        installed_dir = card.get('_installed_models_dir', '')
        if installed_dir:
            return os.path.join(installed_dir, relative_path)
        
        return relative_path
    
    def _expand_env_vars(self, config: Dict[str, Any]) -> None:
        """Expand environment variables in config values."""
        for key, value in config.items():
            if isinstance(value, str):
                # Replace ${VAR} patterns
                while '${' in value:
                    start = value.find('${')
                    end = value.find('}', start)
                    if end == -1:
                        break
                    var_name = value[start+2:end]
                    var_value = os.environ.get(var_name, '')
                    value = value[:start] + var_value + value[end+1:]
                config[key] = value
            elif isinstance(value, dict):
                self._expand_env_vars(value)
    
    def _get_server_binary(self) -> str:
        """Get path to llama.cpp server binary."""
        # Check if using WSL
        if self.global_config.get('use_wsl', False):
            wsl_path = self.global_config.get('llama_cpp_path_wsl', '~/llama.cpp/build/bin')
            binary_name = self.global_config.get('server_binary', 'llama-server')
            return f"{wsl_path}/{binary_name}"
        
        llama_path = self.global_config.get('llama_cpp_path', '')
        binary_name = self.global_config.get('server_binary', 'llama-server')
        
        if llama_path:
            binary_path = os.path.join(llama_path, binary_name)
            if os.path.exists(binary_path):
                return binary_path
            # Try with .exe on Windows
            if sys.platform == 'win32':
                binary_path_exe = binary_path + '.exe'
                if os.path.exists(binary_path_exe):
                    return binary_path_exe
        
        # Try to find in PATH
        return binary_name
    
    def _convert_path_to_wsl(self, windows_path: str) -> str:
        """Convert Windows path to WSL path."""
        if not windows_path:
            return windows_path
        # C:/Users/... -> /mnt/c/Users/...
        path = windows_path.replace('\\', '/')
        if len(path) >= 2 and path[1] == ':':
            drive = path[0].lower()
            path = f"/mnt/{drive}{path[2:]}"
        return path
    
    @staticmethod
    def _resolve_preset_alias(preset_path: str, alias: str, models_dir: str) -> str:
        """Return the full model path for an alias in a .ini preset file."""
        import configparser  # noqa: PLC0415
        if not preset_path or not os.path.exists(preset_path):
            return ""
        cp = configparser.ConfigParser()
        cp.read(preset_path, encoding="utf-8")
        for section in cp.sections():
            a = cp.get(section, "alias", fallback=section)
            if a == alias:
                rel = cp.get(section, "model", fallback="")
                return os.path.join(models_dir, rel) if (models_dir and rel) else rel
        return ""

    def _build_server_command(self, config: ServerConfig) -> List[str]:
        """Build command line arguments for llama.cpp server (b8047+ compatible)."""
        use_wsl = self.global_config.get('use_wsl', False)
        binary = self._get_server_binary()

        cmd = [
            binary,
            '-c', str(config.context_size),
            '-b', str(config.batch_size),
            '-t', str(config.threads),
            '-np', str(config.parallel_slots),
            '--port', str(config.port),
            '--host', '0.0.0.0',
        ]

        if config.router_mode:
            # Router Mode: prefer curated presets; use models-dir only as fallback.
            models_dir = config.router_models_dir
            if use_wsl:
                models_dir = self._convert_path_to_wsl(models_dir)
            if models_dir and not config.router_models_preset:
                cmd.extend(['--models-dir', models_dir])
            if config.router_models_autoload:
                cmd.append('--models-autoload')
            if config.router_models_preset:
                preset = config.router_models_preset
                if use_wsl:
                    preset = self._convert_path_to_wsl(preset)
                cmd.extend(['--models-preset', preset])
            if config.router_models_max > 0:
                cmd.extend(['--models-max', str(config.router_models_max)])
            # Pre-load the default model so first request has zero swap latency
            if config.router_default_model and config.router_models_preset:
                default_path = self._resolve_preset_alias(
                    config.router_models_preset, config.router_default_model, models_dir
                )
                if default_path:
                    if use_wsl:
                        default_path = self._convert_path_to_wsl(default_path)
                    cmd.extend(['--model', default_path])
        else:
            # ── Single-model mode (default) ────────────────────────────
            model_path = config.model_path
            if use_wsl:
                model_path = self._convert_path_to_wsl(model_path)
            cmd.extend(['-m', model_path])
        
        # GPU layers: int or str "auto"/"all"
        if config.gpu_layers != 0:
            cmd.extend(['-ngl', str(config.gpu_layers)])
        
        # Flash attention: b8047+ uses --flash-attn on/off/auto (not bare -fa)
        if config.flash_attention is True:
            cmd.extend(['--flash-attn', 'on'])
        elif config.flash_attention is False:
            cmd.extend(['--flash-attn', 'off'])
        # elif "auto" or other string, omit flag for default behavior
        
        # Embedding mode
        if config.embedding_mode:
            cmd.append('--embedding')
        
        # A.3 New flags for llama.cpp b8047+ upgrade
        
        # Auto-fit VRAM (Phase C)
        if config.fit:
            cmd.extend(['--fit', 'on'])
            if config.fit_target_mib > 0:
                cmd.extend(['--fit-target', str(config.fit_target_mib)])
            if config.fit_ctx_min > 0:
                cmd.extend(['--fit-ctx', str(config.fit_ctx_min)])
        
        # Jinja templating (Phase B)
        if config.jinja is False:
            cmd.append('--no-jinja')
        # jinja=True is default in b8047, no flag needed
        
        # Reasoning models (Phase E)
        if config.reasoning_format:
            cmd.extend(['--reasoning-format', config.reasoning_format])
            if config.reasoning_budget >= 0:
                cmd.extend(['--reasoning-budget', str(config.reasoning_budget)])
        
        # Context checkpoints
        if config.ctx_checkpoints > 0:
            cmd.extend(['--ctx-checkpoints', str(config.ctx_checkpoints)])
        
        # KV unified cache
        if config.kv_unified:
            cmd.append('--kv-unified')
        
        # RAM cache
        if config.cache_ram_mib > 0:
            cmd.extend(['--cache-ram', str(config.cache_ram_mib)])
        
        # Multimodal / Vision (Phase D)
        if config.mmproj_path:
            mmproj_path = self._convert_path_to_wsl(config.mmproj_path) if use_wsl else config.mmproj_path
            cmd.extend(['--mmproj', mmproj_path])
        
        # Speculative decoding (Phase F)
        if config.draft_model_path:
            draft_path = self._convert_path_to_wsl(config.draft_model_path) if use_wsl else config.draft_model_path
            cmd.extend(['--model-draft', draft_path])
            if config.draft_max > 0:
                cmd.extend(['--draft-max', str(config.draft_max)])
            if config.draft_min > 0:
                cmd.extend(['--draft-min', str(config.draft_min)])
            if config.draft_p_min > 0:
                cmd.extend(['--draft-p-min', str(config.draft_p_min)])
        
        # TTS (Phase H)
        if config.vocoder_path:
            vocoder_path = self._convert_path_to_wsl(config.vocoder_path) if use_wsl else config.vocoder_path
            cmd.extend(['--model-vocoder', vocoder_path])
        if config.tts_use_guide_tokens:
            cmd.append('--tts-use-guide-tokens')
        
        # LoRA hot-swap (Phase H)
        if config.lora_init_without_apply:
            cmd.append('--lora-init-without-apply')
        
        # Reranking (Phase H)
        if config.rerank:
            cmd.append('--rerank')
        
        # Prometheus metrics (Phase H)
        if config.metrics:
            cmd.append('--metrics')
        
        # SSL/TLS (Phase H)
        if config.ssl_key_file:
            ssl_key = self._convert_path_to_wsl(config.ssl_key_file) if use_wsl else config.ssl_key_file
            cmd.extend(['--ssl-key-file', ssl_key])
        if config.ssl_cert_file:
            ssl_cert = self._convert_path_to_wsl(config.ssl_cert_file) if use_wsl else config.ssl_cert_file
            cmd.extend(['--ssl-cert-file', ssl_cert])
        
        # Extra arguments (passed last to allow overriding)
        cmd.extend(config.extra_args)
        
        return cmd
    
    async def start_server(self, name: str) -> bool:
        """Start a specific server instance."""
        if name not in self.servers:
            self.logger.error(f"Server '{name}' not found in configuration")
            return False
        
        instance = self.servers[name]
        instance.status = ServerStatus.ERROR
        instance.error_message = "Legacy local llama.cpp launch is disabled; use BackendManager remote slots"
        self.logger.error(instance.error_message)
        return False
    
    async def _wait_for_health(self, instance: ServerInstance, timeout: int) -> bool:
        """Wait for server to become healthy."""
        url = f"http://localhost:{instance.config.port}/health"
        start_time = time.time()
        
        async with aiohttp.ClientSession() as session:
            while time.time() - start_time < timeout:
                try:
                    async with session.get(url, timeout=5) as response:
                        if response.status == 200:
                            data = await response.json()
                            if data.get('status') == 'ok':
                                return True
                except Exception:
                    pass
                
                # Check if process is still running
                if instance.process and instance.process.poll() is not None:
                    return False
                
                await asyncio.sleep(2)
        
        return False
    
    async def stop_server(self, name: str) -> bool:
        """Stop a specific server instance."""
        if name not in self.servers:
            return False
        
        instance = self.servers[name]
        
        if instance.status == ServerStatus.STOPPED:
            return True
        
        instance.status = ServerStatus.STOPPING
        use_wsl = self.global_config.get('use_wsl', False)
        
        try:
            if use_wsl and sys.platform == 'win32':
                # Kill llama-server process in WSL by port
                port = instance.config.port
                kill_cmd = f"kill $(lsof -t -i:{port}) 2>/dev/null || true"
                subprocess.run(['wsl', 'bash', '-c', kill_cmd], timeout=10)
            elif instance.process:
                if sys.platform == 'win32':
                    instance.process.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    instance.process.terminate()
                
                # Wait for graceful shutdown
                try:
                    instance.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    instance.process.kill()
                    instance.process.wait()
            
            instance.status = ServerStatus.STOPPED
            instance.process = None
            instance.pid = None
            self.logger.info(f"Server '{name}' stopped")
            return True
            
        except Exception as e:
            self.logger.error(f"Error stopping server '{name}': {e}")
            instance.status = ServerStatus.ERROR
            instance.error_message = str(e)
            return False
    
    async def start_all(self, profile: Optional[str] = None) -> Dict[str, bool]:
        """Start all enabled servers or servers in a specific profile."""
        results = {}
        
        for name, instance in self.servers.items():
            if instance.config.enabled:
                results[name] = await self.start_server(name)
        
        # Start health monitoring
        if any(results.values()):
            self._start_health_monitoring()
        
        return results
    
    async def stop_all(self) -> Dict[str, bool]:
        """Stop all running servers."""
        self._stop_health_monitoring()
        
        results = {}
        for name in self.servers:
            results[name] = await self.stop_server(name)
        
        return results
    
    def _start_health_monitoring(self) -> None:
        """Start background health monitoring."""
        if self._health_check_task is None or self._health_check_task.done():
            self._health_check_task = asyncio.create_task(self._health_monitor())
    
    def _stop_health_monitoring(self) -> None:
        """Stop health monitoring."""
        if self._health_check_task and not self._health_check_task.done():
            self._health_check_task.cancel()
    
    async def _health_monitor(self) -> None:
        """Background task for health monitoring."""
        interval = self.global_config.get('health_check_interval', 30)
        auto_restart = self.global_config.get('auto_restart', True)
        max_restarts = self.global_config.get('max_restart_attempts', 3)
        
        while True:
            try:
                await asyncio.sleep(interval)
                
                for name, instance in self.servers.items():
                    if instance.status != ServerStatus.RUNNING:
                        continue
                    
                    healthy = await self._check_health(instance)
                    
                    if not healthy:
                        self.logger.warning(f"Server '{name}' is unhealthy")
                        
                        if auto_restart and instance.restart_count < max_restarts:
                            self.logger.info(f"Attempting to restart server '{name}'")
                            await self.stop_server(name)
                            instance.restart_count += 1
                            await self.start_server(name)
                        else:
                            instance.status = ServerStatus.ERROR
                            instance.error_message = "Server unhealthy and max restarts exceeded"
                            
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Health monitor error: {e}")
    
    async def _check_health(self, instance: ServerInstance) -> bool:
        """Check if a server is healthy."""
        url = f"http://localhost:{instance.config.port}/health"
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=10) as response:
                    if response.status == 200:
                        data = await response.json()
                        return data.get('status') == 'ok'
        except Exception:
            pass
        
        return False
    
    def get_status(self) -> Dict[str, Dict[str, Any]]:
        """Get status of all servers."""
        status = {}
        
        for name, instance in self.servers.items():
            status[name] = {
                'status': instance.status.value,
                'port': instance.config.port,
                'role': instance.config.role.value,
                'model': os.path.basename(instance.config.model_path),
                'pid': instance.pid,
                'uptime': time.time() - instance.start_time if instance.start_time else 0,
                'restart_count': instance.restart_count,
                'error': instance.error_message,
            }
        
        return status
    
    def get_endpoint(self, role: ServerRole, specialty: Optional[str] = None) -> Optional[str]:
        """Get API endpoint URL for a server by role."""
        for instance in self.servers.values():
            if instance.config.role == role and instance.status == ServerStatus.RUNNING:
                if specialty is None or instance.config.specialty == specialty:
                    return f"http://localhost:{instance.config.port}/v1"
        
        return None
    
    def get_chat_endpoint(self) -> Optional[str]:
        """Get endpoint for chat model."""
        return self.get_endpoint(ServerRole.CHAT)
    
    def get_utility_endpoint(self) -> Optional[str]:
        """Get endpoint for utility model."""
        return self.get_endpoint(ServerRole.UTILITY)
    
    def get_embedding_endpoint(self) -> Optional[str]:
        """Get endpoint for embedding model."""
        return self.get_endpoint(ServerRole.EMBEDDING)
    
    def get_router_endpoint(self) -> Optional[str]:
        """Get endpoint for router model."""
        return self.get_endpoint(ServerRole.ROUTER)


# ══════════════════════════════════════════════════════════════════════════════
# New Hybrid Backend API (v2)
# ══════════════════════════════════════════════════════════════════════════════
# This wraps the new backends (Docker / Subprocess) via auto-detection.
# Existing LlamaCppManager is preserved for backward compat; new code should
# prefer `get_backend_manager()`.

class BackendManager:
    """
    High-level manager that uses the new backend abstraction.
    
    Supports:
      - Auto-detection of Docker vs subprocess
      - Parallel slot execution (each slot = independent container/process)
      - Unified status API
    """
    
    _instance: Optional['BackendManager'] = None
    
    def __init__(self, config_path: Optional[str] = None):
        self.config_path = config_path or _default_config_path()
        self._backend = None
        self._slot_configs: Dict[str, Dict[str, Any]] = {}
        self.global_config: Dict[str, Any] = {}
        self.logger = logging.getLogger("lmm.backend_manager")
        self._load()
        self._init_failover()  # Initialize failover chains and cooldown probes
    
    @classmethod
    def get_instance(cls, config_path: Optional[str] = None) -> 'BackendManager':
        if cls._instance is None:
            cls._instance = cls(config_path)
        return cls._instance
    
    def _load(self) -> None:
        """Load config and create backend."""
        if not os.path.exists(self.config_path):
            self.logger.warning(f"Config not found: {self.config_path}")
            return
        
        with open(self.config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f) or {}
        
        global_config = config.get('global', {})
        self.global_config = global_config
        
        # Expand env vars
        for key, value in global_config.items():
            if isinstance(value, str) and '${' in value:
                start = value.find('${')
                end = value.find('}', start)
                if end != -1:
                    var = value[start+2:end]
                    global_config[key] = value[:start] + os.environ.get(var, '') + value[end+1:]
        
        # Create backend
        from local_model_router.helpers.backends.factory import create_backend
        self._backend = create_backend(global_config)
        self.logger.info(f"Backend: {self._backend.backend_type.value}")
        
        # Load slot configs
        backend_type = self._backend.backend_type.value if self._backend else str(global_config.get('backend', 'auto')).lower()
        model_cards = {} if backend_type == 'remote' else self._load_model_cards(config)
        models_dir = '' if backend_type == 'remote' else global_config.get('models_dir', '')
        
        for slot in config.get('active_slots', []):
            if not slot or not slot.get('enabled', True):
                continue
            
            name = slot.get('id', f"slot_{slot.get('port', 'unknown')}")
            
            # Resolve model_id → model_path only for backends that load local files.
            model_path = '' if backend_type == 'remote' else slot.get('model_path', '')
            if backend_type != 'remote' and not model_path and slot.get('model_id'):
                model_path = self._resolve_model(slot['model_id'], model_cards, models_dir)
            
            slot_config = dict(slot)
            slot_config['model_path'] = model_path
            self._slot_configs[name] = slot_config

        # Overlay persistent router state (set via dashboard)
        self._apply_router_state()

    def _apply_router_state(self) -> None:
        """Overlay conf/router_state.json onto _slot_configs (non-destructive)."""
        import json  # noqa: PLC0415
        for candidate in [
            "/a0/conf/router_state.json",
            os.path.join(os.path.dirname(self.config_path), "router_state.json"),
        ]:
            if os.path.exists(candidate):
                try:
                    with open(candidate, encoding="utf-8") as f:
                        state = json.load(f)
                    for slot_id, overrides in state.items():
                        if slot_id in self._slot_configs:
                            self._slot_configs[slot_id].update(overrides)
                    self.logger.info(f"Router state loaded from {candidate}")
                except Exception as exc:
                    self.logger.warning(f"Could not load router_state.json: {exc}")
                break

    def _load_model_cards(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """Load model cards for model_id → path resolution."""
        cards = {}
        conf_dir = os.path.dirname(self.config_path)
        
        for fname in ('model_cards.yaml', 'installed_models.yaml'):
            path = os.path.join(conf_dir, fname)
            if os.path.exists(path):
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        data = yaml.safe_load(f) or {}
                    cards.update(data.get('models', {}))
                except Exception:
                    pass
        return cards
    
    def _resolve_model(self, model_id: str, cards: Dict[str, Any], models_dir: str) -> str:
        """Resolve model_id to file path.
        
        Handles two formats:
          - model_cards.yaml:    file: {path: "subdir/model.gguf"}
          - installed_models.yaml: file: "model.gguf", path: "subdir/"
        """
        card = cards.get(model_id, {})
        if not card:
            return ''
        
        file_info = card.get('file', '')
        if isinstance(file_info, dict):
            # model_cards.yaml nested format
            rel_path = file_info.get('path', '')
        elif isinstance(file_info, str) and file_info:
            # installed_models.yaml flat format: file + path siblings
            dir_path = card.get('path', '')
            rel_path = os.path.join(dir_path, file_info) if dir_path else file_info
        else:
            rel_path = ''
        
        if rel_path and models_dir:
            return os.path.join(models_dir, rel_path)
        return rel_path
    
    @property
    def backend_type(self) -> str:
        return self._backend.backend_type.value if self._backend else "none"
    
    async def start_slot(self, name: str) -> Dict[str, Any]:
        """Start a single slot."""
        if not self._backend:
            return {"error": "No backend initialized"}
        config = self._slot_configs.get(name)
        if not config:
            return {"error": f"Slot '{name}' not found in config"}
        
        status = await self._backend.start_slot(name, config)
        return {
            "name": status.name,
            "running": status.running,
            "healthy": status.healthy,
            "port": status.port,
            "host": status.host,
            "container_id": status.container_id,
            "pid": status.pid,
            "error": status.error,
        }
    
    async def start_all(self) -> Dict[str, Dict[str, Any]]:
        """Start all configured slots in parallel."""
        if not self._backend:
            return {}

        # Lazy-start cooldown probes now that we have a running event loop.
        if self._cooldown_config.enabled:
            self._start_cooldown_probes()
        
        tasks = []
        names = []
        for name, config in self._slot_configs.items():
            if config.get('auto_load', True):
                tasks.append(self._backend.start_slot(name, config))
                names.append(name)
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        output = {}
        for name, result in zip(names, results):
            if isinstance(result, Exception):
                output[name] = {"error": str(result)}
            else:
                output[name] = {
                    "running": result.running,
                    "healthy": result.healthy,
                    "port": result.port,
                    "error": result.error,
                }
        return output
    
    async def stop_slot(self, name: str) -> bool:
        if not self._backend:
            return False
        return await self._backend.stop_slot(name)
    
    async def stop_all(self) -> None:
        if self._backend:
            await self._backend.cleanup()
    
    async def status(self) -> Dict[str, Dict[str, Any]]:
        """Get status of all slots."""
        if not self._backend:
            return {}
        slots = await self._backend.list_slots()
        result = {}
        for name, s in slots.items():
            cfg = self._slot_configs.get(name, {})
            result[name] = {
                "running": s.running,
                "healthy": s.healthy,
                "port": s.port,
                "host": s.host,
                "model_id": s.model_id,
                "container_id": s.container_id,
                "pid": s.pid,
                "error": s.error,
                "role": s.extra.get("role", ""),
                # Router Mode fields (from slot config)
                "router_mode": cfg.get("router_mode", False),
                "router_models_dir": cfg.get("router_models_dir", ""),
                "router_models_preset": cfg.get("router_models_preset", ""),
                "router_models_max": cfg.get("router_models_max", 1),
                "router_models_autoload": cfg.get("router_models_autoload", True),
            }
        return result
    
    def get_endpoint(self, role: str) -> Optional[str]:
        """Get the base URL for a slot by role name."""
        if self._backend and hasattr(self._backend, "get_endpoint_by_role"):
            endpoint = self._backend.get_endpoint_by_role(role)
            if endpoint:
                return endpoint
        if self.backend_type == "remote":
            return None
        for name, config in self._slot_configs.items():
            if config.get('role') == role:
                port = config.get('port', 8080)
                return f"http://localhost:{port}/v1"
        return None

    # ═════════════════════════════════════════════════════════════════════════
    # Failover Chain Support (adapted from tiny_router)
    # ═════════════════════════════════════════════════════════════════════════

    def _init_failover(self) -> None:
        """Initialize failover chains and cooldown tracking."""
        from local_model_router.helpers.smart_router.failover import (
            CooldownProbe, CooldownTracker, DEFAULT_CHAINS
        )

        # Load custom chains from config if present
        self._failover_chains = self.global_config.get('failover_chains', DEFAULT_CHAINS)
        self._cooldown_config = CooldownProbe(
            enabled=self.global_config.get('cooldown_probes_enabled', True),
            interval_seconds=self.global_config.get('cooldown_probe_interval', 30),
            max_attempts=self.global_config.get('cooldown_max_attempts', 10),
            probe_timeout=self.global_config.get('cooldown_probe_timeout', 5),
        )
        self._cooldown_tracker = CooldownTracker()
        self._failover_states: Dict[str, Any] = {}  # slot_id -> SlotFailoverState
        self._cooldown_task: Optional[asyncio.Task] = None

        from local_model_router.helpers.smart_router.health import SlotHealthChecker
        self._health_checker = SlotHealthChecker(
            timeout=self.global_config.get('health_check_timeout', 2),
        )

        # Cooldown probes are started lazily in start_all() to avoid
        # calling asyncio.create_task() during synchronous __init__.
        # During agent_init there may be no running event loop yet.

    def _start_cooldown_probes(self) -> None:
        """Start background cooldown probe task (requires running event loop)."""
        if self._cooldown_task is not None and not self._cooldown_task.done():
            return
        try:
            self._cooldown_task = asyncio.create_task(self._cooldown_probe_loop())
            self.logger.info("Cooldown probe loop started")
        except RuntimeError:
            # No running event loop — will be retried later from start_all().
            self.logger.debug("Cooldown probes deferred (no event loop)")

    def _stop_cooldown_probes(self) -> None:
        """Stop cooldown probe task."""
        if self._cooldown_task and not self._cooldown_task.done():
            self._cooldown_task.cancel()
            self.logger.info("Cooldown probe loop stopped")

    async def _cooldown_probe_loop(self) -> None:
        """Background loop to probe ERROR slots for recovery."""
        while True:
            try:
                await asyncio.sleep(self._cooldown_config.interval_seconds)

                error_slots = self._cooldown_tracker.get_error_slots()
                for slot_id in error_slots:
                    if not self._cooldown_tracker.should_probe(slot_id, self._cooldown_config):
                        continue

                    self._cooldown_tracker.record_probe(slot_id)
                    config = self._slot_configs.get(slot_id)
                    if not config:
                        continue

                    # Try health check
                    port = config.get('port', 8080)
                    host = config.get('host', 'localhost')
                    url = f"http://{host}:{port}/health"

                    try:
                        async with aiohttp.ClientSession() as session:
                            async with session.get(url, timeout=self._cooldown_config.probe_timeout) as resp:
                                if resp.status == 200:
                                    data = await resp.json()
                                    if data.get('status') == 'ok':
                                        # Slot recovered!
                                        self._cooldown_tracker.mark_recovered(slot_id)
                                        self.logger.info(f"Slot '{slot_id}' recovered via cooldown probe")
                                        # Record in stats
                                        from local_model_router.helpers.stats_tracker import record_failover
                                        record_failover(slot_id, slot_id, "recovery")
                    except Exception:
                        pass  # Still unhealthy, continue probing

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Cooldown probe error: {e}")

    def select_slot_with_failover(self, role: str, preferred_slot: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Select a slot for a given role, following failover chain if needed.

        Returns dict with:
            - slot_id: str
            - url: str (full endpoint URL)
            - is_failover: bool
            - failover_reason: str (if is_failover)
        """
        from local_model_router.helpers.smart_router.failover import (
            get_chain_for_role, get_next_in_chain, SlotFailoverState, create_decision
        )

        chain = get_chain_for_role(role, self._failover_chains)

        # If preferred_slot specified, start from there; otherwise use first in chain
        start_slot = preferred_slot or (chain[0] if chain else None)
        if not start_slot:
            return None

        # Check if start_slot is healthy
        slot_status = self._get_slot_health(start_slot)

        if slot_status == 'healthy':
            config = self._slot_configs.get(start_slot)
            if config:
                url = self._get_slot_url(start_slot, config)
                return create_decision(
                    slot_id=start_slot,
                    url=url,
                    role=role,
                    reason=f"primary slot for role '{role}'",
                    chain=chain,
                ).__dict__

        # Start_slot unhealthy — walk the failover chain
        current = start_slot
        reason = f"primary slot '{start_slot}' unhealthy" if slot_status == 'unhealthy' else f"primary slot '{start_slot}' not found"

        while current:
            next_slot = get_next_in_chain(current, chain)
            if not next_slot:
                break

            slot_status = self._get_slot_health(next_slot)
            if slot_status == 'healthy':
                config = self._slot_configs.get(next_slot)
                if config:
                    url = self._get_slot_url(next_slot, config)
                    # Record failover in stats
                    from local_model_router.helpers.stats_tracker import record_failover
                    record_failover(start_slot, next_slot, reason)

                    return create_decision(
                        slot_id=next_slot,
                        url=url,
                        role=role,
                        reason=f"failover from '{start_slot}' to '{next_slot}'",
                        chain=chain,
                    ).__dict__

            current = next_slot
            reason = f"slot '{current}' unhealthy, continuing chain"

        # Chain exhausted — no healthy slot found
        self.logger.warning(f"Failover chain exhausted for role '{role}', no healthy slots")
        return None

    def _get_slot_health(self, slot_id: str) -> str:
        """Check health of a slot: 'healthy', 'unhealthy', or 'unknown'.

        Cooldown check runs in-memory before any network call.
        Network probe is delegated to self._health_checker so it can be
        replaced without touching routing logic (e.g. for async probing
        in Phase 3 or stub injection in tests).
        """
        if slot_id in self._cooldown_tracker.get_error_slots():
            return 'unhealthy'

        if not self._backend:
            return 'unknown'

        config = self._slot_configs.get(slot_id)
        if not config:
            return 'unknown'

        return self._health_checker.check(config)

    async def _get_slot_health_async(self, slot_id: str) -> str:
        """Async version of _get_slot_health. Does not block the event loop."""
        if slot_id in self._cooldown_tracker.get_error_slots():
            return 'unhealthy'

        if not self._backend:
            return 'unknown'

        config = self._slot_configs.get(slot_id)
        if not config:
            return 'unknown'

        return await self._health_checker.check_async(config)

    async def select_slot_with_failover_async(
        self, role: str, preferred_slot: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Async version of select_slot_with_failover. Does not block the event loop.

        Preserves identical chain-walking, cooldown, and preferred_slot logic.
        The sync select_slot_with_failover() is unchanged and still works.
        """
        from local_model_router.helpers.smart_router.failover import (
            get_chain_for_role, get_next_in_chain, create_decision,
        )
        from local_model_router.helpers.stats_tracker import record_failover

        chain = get_chain_for_role(role, self._failover_chains)
        start_slot = preferred_slot or (chain[0] if chain else None)
        if not start_slot:
            return None

        slot_status = await self._get_slot_health_async(start_slot)

        if slot_status == 'healthy':
            config = self._slot_configs.get(start_slot)
            if config:
                url = self._get_slot_url(start_slot, config)
                return create_decision(
                    slot_id=start_slot,
                    url=url,
                    role=role,
                    reason=f"primary slot for role '{role}'",
                    chain=chain,
                ).__dict__

        current = start_slot
        reason = (
            f"primary slot '{start_slot}' unhealthy"
            if slot_status == 'unhealthy'
            else f"primary slot '{start_slot}' not found"
        )

        while current:
            next_slot = get_next_in_chain(current, chain)
            if not next_slot:
                break

            slot_status = await self._get_slot_health_async(next_slot)
            if slot_status == 'healthy':
                config = self._slot_configs.get(next_slot)
                if config:
                    url = self._get_slot_url(next_slot, config)
                    record_failover(start_slot, next_slot, reason)
                    return create_decision(
                        slot_id=next_slot,
                        url=url,
                        role=role,
                        reason=f"failover from '{start_slot}' to '{next_slot}'",
                        chain=chain,
                    ).__dict__

            current = next_slot
            reason = f"slot '{current}' unhealthy, continuing chain"

        self.logger.warning(
            "Async failover chain exhausted for role '%s', no healthy slots", role
        )
        return None

    def _get_slot_url(self, slot_id: str, config: Dict[str, Any]) -> str:
        """Get the full API URL for a slot."""
        port = config.get('port', 8080)
        host = config.get('host', 'localhost')
        return f"http://{host}:{port}/v1"

    def mark_slot_error(self, slot_id: str, error_message: str = "") -> None:
        """Mark a slot as in ERROR state for cooldown probing."""
        self._cooldown_tracker.mark_error(slot_id, error_message)
        self.logger.warning(f"Slot '{slot_id}' marked error: {error_message}")

    def get_failover_status(self) -> Dict[str, Any]:
        """Get current failover and cooldown status for dashboard."""
        from local_model_router.helpers.stats_tracker import get_stats_summary

        stats = get_stats_summary(window="24h")
        return {
            "failover_chains": self._failover_chains,
            "cooldown_enabled": self._cooldown_config.enabled,
            "cooldown_interval": self._cooldown_config.interval_seconds,
            "error_slots_being_probed": self._cooldown_tracker.get_error_slots(),
            "failover_stats": stats.get("failovers", {}),
            "slot_stats": stats.get("slots", []),
        }


# ══════════════════════════════════════════════════════════════════════════════
# Convenience functions (backward compat + new API)
# ══════════════════════════════════════════════════════════════════════════════

def get_manager() -> BackendManager:
    """Get the global manager instance."""
    return BackendManager.get_instance()


def get_backend_manager() -> BackendManager:
    """Get the hybrid BackendManager (new API — supports Docker + subprocess)."""
    return BackendManager.get_instance()


async def start_llama_servers() -> Dict[str, Dict[str, Any]]:
    """Start all configured slots."""
    return await get_backend_manager().start_all()


async def stop_llama_servers() -> None:
    """Stop tracking all configured slots."""
    return await get_backend_manager().stop_all()


def get_llama_status() -> Dict[str, Dict[str, Any]]:
    """Get configured slot status without spawning local processes."""
    manager = get_backend_manager()
    return {
        name: {
            "configured": True,
            "running": False,
            "healthy": False,
            "backend": manager.backend_type,
            "port": config.get("port"),
            "role": config.get("role", ""),
            "model_id": config.get("model_id", ""),
            "model_path": "",
        }
        for name, config in getattr(manager, "_slot_configs", {}).items()
    }
