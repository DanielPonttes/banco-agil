from pathlib import Path

from streamlit.testing.v1 import AppTest

APP = Path(__file__).parents[1] / "app.py"


def prepare(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "ui-data"))
    monkeypatch.setenv("GEMINI_API_KEY", "")
    return AppTest.from_file(str(APP), default_timeout=20).run()


def test_ui_authentication_close_and_new_session(monkeypatch, tmp_path):
    app = prepare(monkeypatch, tmp_path)
    assert not app.exception
    assert app.title[0].value == "🏦 Banco Ágil"
    assert app.warning
    app.chat_input[0].set_value("01234567890").run()
    app.chat_input[0].set_value("15/01/1990").run()
    assert app.session_state["engine"].state.authenticated
    previous = app.session_state["engine"].state.session_id
    next(b for b in app.button if b.label == "Encerrar atendimento").click().run()
    assert app.chat_input[0].disabled
    next(b for b in app.button if b.label == "Novo atendimento").click().run()
    assert not app.session_state["engine"].state.authenticated
    assert app.session_state["engine"].state.session_id != previous
    assert not app.exception


def test_ui_missing_key_retry_is_clear(monkeypatch, tmp_path):
    app = prepare(monkeypatch, tmp_path)
    app.chat_input[0].set_value("01234567890").run()
    app.chat_input[0].set_value("15/01/1990").run()
    app.chat_input[0].set_value("Qual meu limite?").run()
    assert app.session_state["failed_turn"]
    assert app.chat_input[0].disabled
    assert any(b.label == "Tentar novamente" for b in app.button)
    assert not app.exception
