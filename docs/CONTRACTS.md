# Contratos de implementação

Fronteira entre domínio e aplicação. Valores monetários internos usam Decimal. Nenhuma ferramenta financeira aceita CPF fornecido pelo modelo: aplicação injeta identidade autenticada. Erros esperados usam DomainError com mensagem segura.

## Domínio (src/banco_agil/domain.py e repository.py)

- DomainError(Exception).
- Customer dataclass: cpf_cliente: str, nome: str, data_nascimento: str (ISO), limite_atual: Decimal, score: int.
- Interview dataclass: renda_mensal: Decimal, tipo_emprego: str ('formal','autonomo','desempregado'), despesas_mensais: Decimal, num_dependentes: int, tem_dividas: bool.
- CreditResult dataclass: status_pedido: str, limite_atual: Decimal (anterior), novo_limite_solicitado: Decimal, limite_maximo: Decimal, score: int.
- normalize_cpf(value: str) -> str: somente 11 dígitos, DomainError para inválido.
- normalize_birth_date(value: str) -> str: DD/MM/AAAA ou ISO, DomainError para inválido/futuro.
- parse_money(value: str | int | float | Decimal) -> Decimal: BRL pt-BR (R$ 1.000,50), decimal canônico, rejeita não finitos/negativos, duas casas; strings ambíguas devem falhar.
- calculate_score(interview: Interview) -> int: fórmula do PDF, HALF_UP e clamp 0..1000; valida valores e enums.
- BankRepository(data_dir: Path): não sobrescreve bases existentes. initialize(seed_dir: Path) -> None copia exemplos somente para runtime ausente, valida tudo e recupera diário pendente; não mascara CSV corrompido.
- authenticate(cpf: str, birth_date: str) -> Customer | None: normaliza e compara, None para par não encontrado.
- get_customer(cpf: str) -> Customer.
- request_increase(cpf: str, requested: Decimal, operation_id: str) -> CreditResult: idempotência durável por operation_id; status pendente antes de final; aprova <= máximo; atualiza cliente, preserva histórico; lock global e diário de recuperação para ambos CSVs.
- update_score(cpf: str, interview: Interview, operation_id: str) -> int: idempotência durável, apenas cliente autenticado. Persistir score calculado.

## Application (src/banco_agil/application.py, providers.py, app.py)

Agente aplicação define próprios modelos de estado e classificação Pydantic, injetando repo e provider para testes. Não alterar contratos acima sem coordenar.

Quatro agentes reais via LangGraph, SDK Gemini com saída estruturada. Um ciclo por mensagem; END do turno não encerra sessão; ferramenta encerrar_atendimento marca estado terminal. Regex de encerramento só comandos inequívocos, não substring que encerraria 'não quero encerrar'. Gemini extrai intenção/slots e pergunta; decisões bancárias são ferramentas Python. Guard de autenticação em todas as ferramentas. Não enviar CPF/data de nascimento ao provedor. Agentes distintos com prompts e ações permitidas; roteamento invisível.

App: app.py na raiz, Streamlit. BankRepository(Path(settings.data_dir)).initialize(Path('data/examples')). Sessões sem globals mutáveis; mensagens com UUID para reruns. Mostra dados fictícios de login em ajuda. Provider ausente bloqueia caminho LLM com erro claro, sem modo fake silencioso. Mocks apenas em testes.

Autenticação coleta CPF e nascimento antes de especialista (inclusive câmbio), três mismatches totais, formato inválido não consome tentativa. Entrevista coleta 5 campos, aceita correção, confirma resumo, atualiza score, retorna crédito e pede aceite para NOVA solicitação do valor rejeitado. Recusa oferece outros assuntos. Pedido aumento 'em' vs 'para': interpretar com contexto ou esclarecer. Fechamento em todo estado, troca de assunto preserva entrevista.

Câmbio: httpx AwesomeAPI /json/last/{CODE}-BRL, timeout 5s + uma tentativa para falhas transitórias, valida par em catálogo ou lista USD/EUR/GBP inicialmente e catálogo para outros. Retorna bid, ask, timestamp e fonte; avisa cache possível sem chave, nunca inventa cotação. Sem HTTP arbitrário escolhido pelo usuário.

## Dados (agente domínio cria exemplos)

clientes.csv: cpf_cliente,nome,data_nascimento,limite_atual,score
score_limite.csv: score_minimo,limite_maximo
solicitacoes_aumento_limite.csv: cpf_cliente,data_hora_solicitacao,limite_atual,novo_limite_solicitado,status_pedido

Faixas: 0=500;300=1500;500=3000;700=5000;850=10000.
CPF fictício com zero inicial em pelo menos um caso.
Cenário entrevista: score300 limite1000 pedido3000; renda5000 despesas2000 formal 0 dependentes sem dívidas => score575 e aprova nova solicitação.
UTF-8, ISO UTC timestamps, precisão monetária 2 casas. Diário auxiliar JSON ignorado via data/runtime/.


## Fórmula exata e obrigatória

score_bruto = renda_mensal / (despesas_mensais + 1) * 30 + peso_emprego + peso_dependentes + peso_dividas.
Emprego: formal=300, autonomo=200, desempregado=0.
Dependentes: 0=100, 1=80, 2=60, >=3=30.
Dividas: True=-100, False=100.
Arredondar HALF_UP, limitar 0..1000. Não adaptar a fórmula para coincidir apenas com o exemplo575.
