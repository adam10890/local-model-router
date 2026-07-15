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


def test_missing_first_run_config_under_imperium_home_is_resolved(tmp_path, monkeypatch):
    from local_model_router.helpers.conf_resolver import resolve_conf_path

    home = tmp_path / "Imperium"
    cfg = home / "conf" / "llama_cpp_servers.yaml"
    monkeypatch.setenv("IMPERIUM_HOME", str(home))
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


def test_standard_candidates_prefer_repository_then_managed_config(tmp_path, monkeypatch):
    from local_model_router.helpers.conf_resolver import plugin_root, standard_conf_candidates

    home = tmp_path / "Imperium"
    monkeypatch.setenv("IMPERIUM_HOME", str(home))

    assert standard_conf_candidates(__file__) == [
        (plugin_root(__file__) / "conf" / "llama_cpp_servers.yaml").resolve(),
        (home / "conf" / "llama_cpp_servers.yaml").resolve(),
    ]


def test_existing_repository_config_wins_over_managed_config(tmp_path, monkeypatch):
    from local_model_router.helpers import conf_resolver

    repo = tmp_path / "repo"
    repo_config = repo / "conf" / "llama_cpp_servers.yaml"
    repo_config.parent.mkdir(parents=True)
    repo_config.write_text("active_slots: []\n", encoding="utf-8")
    home = tmp_path / "Imperium"
    managed_config = home / "conf" / "llama_cpp_servers.yaml"
    managed_config.parent.mkdir(parents=True)
    managed_config.write_text("active_slots: []\n", encoding="utf-8")
    monkeypatch.setattr(conf_resolver, "plugin_root", lambda _caller=None: repo)
    monkeypatch.setenv("IMPERIUM_HOME", str(home))
    monkeypatch.delenv("A0_LMM_ROUTER_CONFIG", raising=False)

    assert conf_resolver.resolve_conf_path(__file__) == str(repo_config.resolve())


def test_fresh_profile_resolves_to_managed_config(tmp_path, monkeypatch):
    from local_model_router.helpers import conf_resolver

    repo = tmp_path / "repo"
    home = tmp_path / "Imperium"
    monkeypatch.setattr(conf_resolver, "plugin_root", lambda _caller=None: repo)
    monkeypatch.setenv("IMPERIUM_HOME", str(home))
    monkeypatch.delenv("A0_LMM_ROUTER_CONFIG", raising=False)

    expected = home / "conf" / "llama_cpp_servers.yaml"
    assert conf_resolver.resolve_conf_path(__file__) == str(expected.resolve())
