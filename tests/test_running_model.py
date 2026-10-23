"""Running-model identity comes from llama.cpp argv, never Hermes pin names."""
from __future__ import annotations

from local_model_router.service.readiness import (
    build_ui_status,
    choose_running_model,
    inspect_running_llama_models,
    is_generic_model_id,
    model_from_llama_cmdline,
)


def test_cmdline_reads_gguf_not_alias():
    port, model = model_from_llama_cmdline(
        [
            "llama-server.exe",
            "--port",
            "18080",
            "-m",
            r"C:\Models\chat\Qwen3.8-27B-OBLITERATED-Q4_K_M.gguf",
            "--alias",
            "local",
        ]
    )
    assert port == 18080
    assert model == "Qwen3.8-27B-OBLITERATED-Q4_K_M"


def test_generic_ids_skip_hermes_local_and_aliases():
    assert is_generic_model_id("local")
    assert is_generic_model_id("auto")
    assert is_generic_model_id("chat")
    assert not is_generic_model_id("Qwen3.8-27B-OBLITERATED-Q4_K_M")
    assert not is_generic_model_id(r"C:\Models\chat.gguf")


def test_inspect_maps_port_from_llama_server_process():
    found = inspect_running_llama_models(
        [
            {
                "name": "llama-server.exe",
                "cmdline": [
                    "llama-server.exe",
                    "--port",
                    "18080",
                    "--model",
                    r"C:\Models\qwen38_27b.gguf",
                    "--alias",
                    "local",
                ],
            },
            {"name": "python.exe", "cmdline": ["python", "-m", "local_model_router"]},
        ]
    )
    assert found == {18080: "qwen38_27b"}


def test_choose_running_model_prefers_process_over_local_pin():
    name, source = choose_running_model(
        {"model_id": "local", "router_default_model": "local"},
        process_model="qwen38_27b",
        live_ids=["local", "chat"],
    )
    assert name == "qwen38_27b"
    assert source == "process"


def test_choose_running_model_uses_live_slot_when_process_missing():
    name, source = choose_running_model(
        {"model_id": "local"},
        live_ids=["local", "Qwen3.8-27B-OBLITERATED-Q4_K_M"],
    )
    assert name == "Qwen3.8-27B-OBLITERATED-Q4_K_M"
    assert source == "live_slot"


def test_ui_status_does_not_publish_hermes_local_pin():
    payload = build_ui_status(
        setup_state={
            "discovery": {
                "runtime_installed": True,
                "gguf_models": ["qwen.gguf"],
                "config_exists": True,
                "enabled_slots": 1,
            },
            "platform_support": {"status": "supported"},
        },
        slots_health=[
            {
                "id": "local_default",
                "enabled": True,
                "health": "healthy",
                "model_id": "local",
                "port": 18080,
                "backend_type": "subprocess",
            }
        ],
        compute={},
        base_url="http://127.0.0.1:9000",
        process_models={18080: "qwen38_27b"},
    )
    assert payload["active"]["model"] == "qwen38_27b"
    assert payload["active"]["source"] == "process"
    assert payload["active"]["model"] != "local"
