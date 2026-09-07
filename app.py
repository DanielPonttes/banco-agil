"""Run with: uv run streamlit run app.py"""

import csv
import logging
import uuid

import streamlit as st

from banco_agil.application import ConversationEngine
from banco_agil.domain import DomainError
from banco_agil.providers import AwesomeAPIProvider, GeminiProvider
from banco_agil.repository import BankRepository
from banco_agil.settings import Settings

st.set_page_config(page_title="Banco Ágil | Atendimento", page_icon="🏦", layout="centered")
settings = Settings.from_env()
logging.basicConfig(
    level=getattr(logging, settings.log_level, logging.INFO),
    format="%(levelname)s %(name)s %(message)s",
)


def make_engine():
    repo = BankRepository(settings.data_dir)
    repo.initialize(settings.examples_dir)
    return ConversationEngine(
        repo,
        GeminiProvider(settings.gemini_api_key, settings.gemini_model),
        AwesomeAPIProvider.from_settings(settings),
    )


st.title("🏦 Banco Ágil")
st.caption("Seu atendimento de crédito e câmbio, em uma só conversa.")

try:
    if "engine" not in st.session_state:
        st.session_state.engine = make_engine()
        st.session_state.messages = [
            {
                "role": "assistant",
                "content": "Olá! Bem-vindo ao Banco Ágil. Para começar, informe seu CPF.",
            }
        ]
        st.session_state.failed_turn = None
except (DomainError, OSError):
    st.error("Não foi possível abrir a base de dados. Verifique os arquivos em DATA_DIR.")
    st.stop()

engine = st.session_state.engine

with st.sidebar:
    st.header("Seu atendimento")
    st.caption("Demonstração com dados fictícios.")
    if st.button("Novo atendimento", use_container_width=True):
        for key in ("engine", "messages", "failed_turn"):
            st.session_state.pop(key, None)
        st.rerun()
    if st.button("Encerrar atendimento", disabled=engine.state.closed, use_container_width=True):
        st.session_state.messages.append({"role": "assistant", "content": engine.end_session()})
        st.session_state.failed_turn = None
        st.rerun()
    with st.expander("Clientes fictícios para testar"):
        try:
            with (settings.examples_dir / "clientes.csv").open(
                encoding="utf-8-sig", newline=""
            ) as f:
                for row in csv.DictReader(f):
                    st.markdown(f"**{row['nome']}**")
                    st.text(f"CPF: {row['cpf_cliente']}\nNascimento: {row['data_nascimento']}")
        except (OSError, KeyError):
            st.caption("Consulte data/examples/clientes.csv.")
    st.caption("Não informe dados pessoais reais nesta demonstração.")

if not settings.gemini_api_key:
    st.warning(
        "Configure GEMINI_API_KEY no arquivo .env para conversar com a IA. "
        "Depois, clique em Novo atendimento."
    )

for item in st.session_state.messages:
    with st.chat_message(item["role"]):
        st.write(item["content"])

failed = st.session_state.failed_turn
if failed:
    st.info("A etapa não foi concluída. Você pode tentar novamente sem duplicar o pedido.")
    if st.button("Tentar novamente"):
        with st.spinner("Retomando sua solicitação..."):
            result = engine.process_message(failed["text"], failed["id"])
        st.session_state.messages.append({"role": "assistant", "content": result.message})
        st.session_state.failed_turn = failed if result.retryable else None
        st.rerun()

if engine.state.closed:
    st.success("Atendimento encerrado. Use Novo atendimento para começar outra conversa.")

text = st.chat_input(
    "Digite sua mensagem", max_chars=4000, disabled=engine.state.closed or bool(failed)
)
if text:
    mid = str(uuid.uuid4())
    st.session_state.messages.append({"role": "user", "content": text})
    with st.spinner("Analisando sua solicitação..."):
        result = engine.process_message(text, mid)
    st.session_state.messages.append({"role": "assistant", "content": result.message})
    if result.retryable:
        st.session_state.failed_turn = {"text": text, "id": mid}
    st.rerun()
