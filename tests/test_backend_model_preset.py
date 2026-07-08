from pathlib import Path

from local_model_router.helpers.llama_cpp_manager import resolve_preset_alias


def test_resolve_preset_alias_returns_model_under_models_dir(tmp_path):
    preset = tmp_path / "models.ini"
    preset.write_text(
        "[chat]\nalias = primary\nmodel = models/chat.gguf\n",
        encoding="utf-8",
    )

    assert resolve_preset_alias(str(preset), "primary", "D:/llm") == str(
        Path("D:/llm") / "models/chat.gguf"
    )


def test_resolve_preset_alias_returns_empty_for_missing_alias(tmp_path):
    preset = tmp_path / "models.ini"
    preset.write_text("[chat]\nmodel = chat.gguf\n", encoding="utf-8")

    assert resolve_preset_alias(str(preset), "missing", str(tmp_path)) == ""
