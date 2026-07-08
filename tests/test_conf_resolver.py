from __future__ import annotations

from pathlib import Path


def test_env_config_must_be_named_llama_cpp_servers_yaml(tmp_path):
    from local_model_router.helpers.conf_resolver import is_safe_conf_path

    unsafe = tmp_path / "custom.yaml"
    unsafe.write_text("active_slots: []\n", encoding="utf-8")

    assert is_safe_conf_path(unsafe) is False


def test_env_config_in_temp_is_allowed_for_local_dev(tmp_path, monkeypatch):
    from local_model_router.helpers.conf_resolver import resolve_conf_path

    cfg = tmp_path / "llama_cpp_servers.yaml"
    cfg.write_text("active_slots: []\n", encoding="utf-8")
    monkeypatch.setenv("A0_LMM_ROUTER_CONFIG", str(cfg))

    assert resolve_conf_path(__file__) == str(cfg.resolve())


def test_unsafe_env_config_is_ignored(tmp_path, monkeypatch):
    from local_model_router.helpers.conf_resolver import resolve_conf_path

    unsafe = tmp_path / "not_router_config.yaml"
    unsafe.write_text("active_slots: []\n", encoding="utf-8")
    monkeypatch.setenv("A0_LMM_ROUTER_CONFIG", str(unsafe))

    resolved = Path(resolve_conf_path(__file__))

    assert resolved.name == "llama_cpp_servers.yaml"
    assert resolved != unsafe.resolve()


def test_standard_candidates_only_use_repository_config():
    from local_model_router.helpers.conf_resolver import plugin_root, standard_conf_candidates

    assert standard_conf_candidates(__file__) == [
        (plugin_root(__file__) / "conf" / "llama_cpp_servers.yaml").resolve()
    ]
