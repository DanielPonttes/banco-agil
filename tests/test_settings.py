from banco_agil import settings


def test_local_configuration_reloads_without_mutating_environment(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ROOT", tmp_path)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    path = tmp_path / ".env"
    path.write_text("GEMINI_API_KEY=first\n", encoding="utf-8")
    assert settings.Settings.from_env().gemini_api_key == "first"
    path.write_text("GEMINI_API_KEY=second\n", encoding="utf-8")
    assert settings.Settings.from_env().gemini_api_key == "second"
    assert "GEMINI_API_KEY" not in settings.os.environ


def test_explicit_environment_takes_precedence(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ROOT", tmp_path)
    (tmp_path / ".env").write_text("GEMINI_API_KEY=local\n", encoding="utf-8")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    assert settings.Settings.from_env().gemini_api_key == ""
