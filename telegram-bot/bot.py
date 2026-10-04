import os
import re
import json
import sqlite3
import asyncio
import tempfile
from pathlib import Path
from datetime import datetime, timedelta

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from google import genai


# ============================================================
# CONFIGURAÇÕES
# ============================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

MODELOS_GEMINI = [
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
]

BANCO = "thigas_ai.db"

MAX_PDF_MB = 50
MAX_TELEGRAM_MESSAGE = 4000


# ============================================================
# VERIFICAÇÃO
# ============================================================

if not TELEGRAM_TOKEN:
    raise RuntimeError(
        "TELEGRAM_TOKEN não foi configurado."
    )

if not GEMINI_API_KEY:
    raise RuntimeError(
        "GEMINI_API_KEY não foi configurada."
    )


# ============================================================
# CLIENTE GEMINI
# ============================================================

client = genai.Client(
    api_key=GEMINI_API_KEY
)


# ============================================================
# BANCO DE DADOS
# ============================================================

def conectar_banco():

    conexao = sqlite3.connect(
        BANCO,
        check_same_thread=False
    )

    conexao.row_factory = sqlite3.Row

    return conexao


db = conectar_banco()


def iniciar_banco():

    cursor = db.cursor()

    # Usuários
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS usuarios (
            user_id INTEGER PRIMARY KEY,
            nome TEXT,
            xp INTEGER DEFAULT 0,
            dias_estudo INTEGER DEFAULT 0,
            ultimo_estudo TEXT
        )
    """)

    # Exercícios
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS exercicios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            assunto TEXT,
            dificuldade TEXT,
            pergunta TEXT,
            resposta_correta TEXT,
            explicacao TEXT,
            resposta_usuario TEXT,
            acertou INTEGER DEFAULT 0,
            criado_em TEXT
        )
    """)

    # Flashcards
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS flashcards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            assunto TEXT,
            pergunta TEXT,
            resposta TEXT,
            intervalo INTEGER DEFAULT 1,
            facilidade REAL DEFAULT 2.5,
            revisado_em TEXT,
            proxima_revisao TEXT
        )
    """)

    # Metas
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS metas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            titulo TEXT,
            data_limite TEXT,
            concluida INTEGER DEFAULT 0,
            criada_em TEXT
        )
    """)

    # Sessões de estudo
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sessoes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            assunto TEXT,
            minutos INTEGER,
            data TEXT
        )
    """)

    # Materiais
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS materiais (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            nome TEXT,
            tipo TEXT,
            file_id TEXT,
            resumo TEXT,
            criado_em TEXT
        )
    """)

    # Simulados
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS simulados (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            assunto TEXT,
            questoes TEXT,
            respostas TEXT,
            inicio TEXT,
            duracao INTEGER,
            finalizado INTEGER DEFAULT 0,
            nota REAL DEFAULT 0
        )
    """)

    db.commit()


iniciar_banco()


# ============================================================
# USUÁRIO
# ============================================================

def registrar_usuario(
    user_id,
    nome
):

    cursor = db.cursor()

    cursor.execute(
        """
        INSERT OR IGNORE INTO usuarios
        (user_id, nome)
        VALUES (?, ?)
        """,
        (user_id, nome)
    )

    cursor.execute(
        """
        UPDATE usuarios
        SET nome = ?
        WHERE user_id = ?
        """,
        (nome, user_id)
    )

    db.commit()


def adicionar_xp(
    user_id,
    xp
):

    cursor = db.cursor()

    cursor.execute(
        """
        UPDATE usuarios
        SET xp = xp + ?
        WHERE user_id = ?
        """,
        (xp, user_id)
    )

    db.commit()


def registrar_estudo(
    user_id,
    assunto,
    minutos=0
):

    hoje = datetime.now().strftime(
        "%Y-%m-%d"
    )

    cursor = db.cursor()

    cursor.execute(
        """
        INSERT INTO sessoes
        (user_id, assunto, minutos, data)
        VALUES (?, ?, ?, ?)
        """,
        (
            user_id,
            assunto,
            minutos,
            hoje
        )
    )

    cursor.execute(
        """
        SELECT ultimo_estudo, dias_estudo
        FROM usuarios
        WHERE user_id = ?
        """,
        (user_id,)
    )

    usuario = cursor.fetchone()

    dias = usuario["dias_estudo"] if usuario else 0
    ultimo = usuario["ultimo_estudo"] if usuario else None

    if ultimo != hoje:

        dias += 1

        cursor.execute(
            """
            UPDATE usuarios
            SET dias_estudo = ?,
                ultimo_estudo = ?
            WHERE user_id = ?
            """,
            (
                dias,
                hoje,
                user_id
            )
        )

    db.commit()


# ============================================================
# PROMPT
# ============================================================

PROMPT_BASE = """
Você é o THIGAS AI, um tutor acadêmico pessoal.

Você deve ensinar como um excelente professor particular.

Seja:
- claro
- didático
- direto
- paciente
- objetivo
- rigoroso quando necessário

Responda em português do Brasil.

Não use:
- LaTeX
- Markdown pesado
- links
- blocos de código sem necessidade
- introduções genéricas
- frases como "Claro!"
- textos desnecessariamente longos

Matemática:

Use:

× para multiplicação
÷ para divisão
² para potência
³ para potência

Mostre cálculos passo a passo.

Quando houver erro do estudante:
1. diga o que está errado
2. explique por que está errado
3. mostre como fazer corretamente
4. dê uma dica para não repetir o erro

O objetivo não é apenas entregar a resposta.

O objetivo é fazer o estudante aprender.
"""


# ============================================================
# LIMPEZA
# ============================================================

def limpar_resposta(texto):

    if not texto:
        return "Não consegui gerar uma resposta."

    texto = re.sub(
        r"```.*?\n?",
        "",
        texto,
        flags=re.DOTALL
    )

    texto = texto.replace(
        "$$",
        ""
    )

    texto = texto.replace(
        "\\[",
        ""
    )

    texto = texto.replace(
        "\\]",
        ""
    )

    texto = texto.replace(
        "\\(",
        ""
    )

    texto = texto.replace(
        "\\)",
        ""
    )

    texto = texto.replace(
        "**",
        ""
    )

    texto = texto.replace(
        "__",
        ""
    )

    texto = texto.replace(
        "`",
        ""
    )

    texto = re.sub(
        r"^\s*#{1,6}\s*",
        "",
        texto,
        flags=re.MULTILINE
    )

    texto = re.sub(
        r"\[([^\]]+)\]\([^)]+\)",
        r"\1",
        texto
    )

    texto = re.sub(
        r"https?://\S+",
        "",
        texto
    )

    linhas = []

    for linha in texto.splitlines():

        linha = linha.strip()

        if linha == "":
            if linhas and linhas[-1] == "":
                continue

        linhas.append(linha)

    texto = "\n".join(linhas)

    texto = re.sub(
        r"\n{3,}",
        "\n\n",
        texto
    )

    return texto.strip()


# ============================================================
# DIVIDIR MENSAGEM
# ============================================================

def dividir_mensagem(
    texto,
    limite=MAX_TELEGRAM_MESSAGE
):

    partes = []

    while len(texto) > limite:

        corte = texto.rfind(
            "\n",
            0,
            limite
        )

        if corte == -1:
            corte = limite

        partes.append(
            texto[:corte].strip()
        )

        texto = texto[corte:].strip()

    if texto:
        partes.append(texto)

    return partes


async def enviar_mensagem(
    update,
    texto
):

    partes = dividir_mensagem(texto)

    for parte in partes:

        await update.message.reply_text(
            parte
        )

        await asyncio.sleep(
            0.25
        )


# ============================================================
# GEMINI
# ============================================================

def erro_temporario(
    erro
):

    texto = str(erro).upper()

    return any(
        item in texto
        for item in [
            "429",
            "500",
            "502",
            "503",
            "504",
            "UNAVAILABLE",
            "RESOURCE_EXHAUSTED",
            "OVERLOADED",
            "TIMEOUT",
        ]
    )


async def chamar_gemini(
    prompt,
    estruturado=False,
    schema=None
):

    ultimo_erro = None

    for modelo in MODELOS_GEMINI:

        for tentativa in range(3):

            try:

                argumentos = {
                    "model": modelo,
                    "input": prompt,
                }

                if estruturado:

                    argumentos[
                        "response_format"
                    ] = {
                        "type": "text",
                        "mime_type": "application/json",
                        "schema": schema
                    }

                else:

                    argumentos[
                        "generation_config"
                    ] = {
                        "thinking_level": "medium"
                    }

                interaction = await asyncio.to_thread(
                    client.interactions.create,
                    **argumentos
                )

                texto = getattr(
                    interaction,
                    "output_text",
                    None
                )

                if not texto:

                    raise RuntimeError(
                        "Gemini não retornou texto."
                    )

                print(
                    f"✅ Gemini respondeu com {modelo}"
                )

                if estruturado:

                    return json.loads(
                        texto
                    )

                return limpar_resposta(
                    texto
                )

            except Exception as erro:

                ultimo_erro = erro

                print(
                    f"❌ {modelo}: {repr(erro)}"
                )

                if not erro_temporario(
                    erro
                ):

                    raise

                espera = 2 ** tentativa

                print(
                    f"⏳ Nova tentativa em "
                    f"{espera}s"
                )

                await asyncio.sleep(
                    espera
                )

        print(
            f"⚠️ Pulando modelo {modelo}"
        )

    raise ultimo_erro


# ============================================================
# /START
# ============================================================

async def start(
    update,
    context
):

    user = update.effective_user

    registrar_usuario(
        user.id,
        user.first_name
    )

    mensagem = """
🤖 THIGAS AI

BOT DE ESTUDOS

👨‍🏫 TUTOR
/tutor química
/tutor função quadrática

📝 EXERCÍCIOS
/questao química
/corrigir minha resposta
/erro

🧠 REVISÃO
/flashcards química
/revisar

📊 DESEMPENHO
/desempenho
/progresso
/ranking

📅 PLANEJAMENTO
/meta 10/10/2026 Prova de química
/cronograma
/calendario

📚 MATERIAIS
/materiais

Envie um PDF para estudar o conteúdo.

🎯 MODO PROVA
/simulado química
/respostas 1A 2C 3B
/finalizar

⏱️ CRONÔMETRO
/cronometro 30

💡 Também pode simplesmente mandar
qualquer pergunta.
"""

    await update.message.reply_text(
        mensagem.strip()
    )


# ============================================================
# /AJUDA
# ============================================================

async def ajuda(
    update,
    context
):

    await update.message.reply_text(
        """
📚 CENTRAL DO THIGAS AI

👨‍🏫 Tutor
/tutor assunto

📝 Questões
/questao assunto
/corrigir resposta
/erro

🧠 Revisão
/flashcards assunto
/revisar

📊 Desempenho
/desempenho
/progresso
/ranking

📅 Planejamento
/meta DATA descrição
/cronograma
/calendario

📚 Materiais
/materiais

🎯 Prova
/simulado assunto
/respostas 1A 2B 3C
/finalizar

⏱️ Cronômetro
/cronometro minutos

📄 PDFs:
Envie o PDF diretamente pelo Telegram.
"""
    )


# ============================================================
# 👨‍🏫 TUTOR
# ============================================================

async def tutor(
    update,
    context
):

    user = update.effective_user

    assunto = " ".join(
        context.args
    ).strip()

    if not assunto:

        await update.message.reply_text(
            "Use:\n\n/tutor assunto\n\n"
            "Exemplo:\n"
            "/tutor ligação iônica"
        )

        return

    await update.message.reply_text(
        "👨‍🏫 Preparando a explicação..."
    )

    prompt = f"""
{PROMPT_BASE}

MODO TUTOR.

Explique o seguinte assunto:

{assunto}

Estruture:

1. O que é
2. Como funciona
3. Exemplo
4. Passo a passo
5. Erros comuns
6. Resumo final

Não complique desnecessariamente.
"""

    try:

        resposta = await chamar_gemini(
            prompt
        )

        registrar_estudo(
            user.id,
            assunto,
            10
        )

        adicionar_xp(
            user.id,
            10
        )

        await enviar_mensagem(
            update,
            resposta
        )

    except Exception as erro:

        print(
            "ERRO TUTOR:",
            repr(erro)
        )

        await update.message.reply_text(
            "❌ Gemini temporariamente indisponível."
        )


# ============================================================
# 📝 GERAR QUESTÃO
# ============================================================

QUESTAO_SCHEMA = {
    "type": "object",
    "properties": {
        "assunto": {
            "type": "string"
        },
        "dificuldade": {
            "type": "string"
        },
        "pergunta": {
            "type": "string"
        },
        "resposta": {
            "type": "string"
        },
        "explicacao": {
            "type": "string"
        }
    },
    "required": [
        "assunto",
        "dificuldade",
        "pergunta",
        "resposta",
        "explicacao"
    ]
}


async def questao(
    update,
    context
):

    user = update.effective_user

    assunto = " ".join(
        context.args
    ).strip()

    if not assunto:

        assunto = "conhecimentos gerais"

    await update.message.reply_text(
        "📝 Gerando questão..."
    )

    prompt = f"""
{PROMPT_BASE}

Gere UMA questão para estudante.

Assunto:
{assunto}

Dificuldade:
média

Retorne apenas JSON com:

assunto
dificuldade
pergunta
resposta
explicacao

A pergunta deve exigir raciocínio.
"""

    try:

        dados = await chamar_gemini(
            prompt,
            estruturado=True,
            schema=QUESTAO_SCHEMA
        )

        cursor = db.cursor()

        cursor.execute(
            """
            INSERT INTO exercicios
            (
                user_id,
                assunto,
                dificuldade,
                pergunta,
                resposta_correta,
                explicacao,
                criado_em
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user.id,
                dados["assunto"],
                dados["dificuldade"],
                dados["pergunta"],
                dados["resposta"],
                dados["explicacao"],
                datetime.now().isoformat()
            )
        )

        db.commit()

        exercicio_id = cursor.lastrowid

        context.user_data[
            "ultimo_exercicio"
        ] = exercicio_id

        mensagem = f"""
📝 EXERCÍCIO

Assunto:
{dados["assunto"]}

Dificuldade:
{dados["dificuldade"]}

{dados["pergunta"]}

Envie sua resposta usando:

/corrigir sua resposta
"""

        await update.message.reply_text(
            mensagem.strip()
        )

    except Exception as erro:

        print(
            "ERRO QUESTÃO:",
            repr(erro)
        )

        await update.message.reply_text(
            "❌ Não consegui gerar a questão."
        )


# ============================================================
# 📝 CORRIGIR
# ============================================================

CORRECAO_SCHEMA = {
    "type": "object",
    "properties": {
        "acertou": {
            "type": "boolean"
        },
        "nota": {
            "type": "integer"
        },
        "feedback": {
            "type": "string"
        },
        "explicacao": {
            "type": "string"
        }
    },
    "required": [
        "acertou",
        "nota",
        "feedback",
        "explicacao"
    ]
}


async def corrigir(
    update,
    context
):

    user = update.effective_user

    resposta_usuario = " ".join(
        context.args
    ).strip()

    if not resposta_usuario:

        await update.message.reply_text(
            "Use:\n\n"
            "/corrigir sua resposta"
        )

        return

    exercicio_id = context.user_data.get(
        "ultimo_exercicio"
    )

    if not exercicio_id:

        await update.message.reply_text(
            "❌ Você ainda não tem uma questão pendente.\n\n"
            "Use /questao assunto"
        )

        return

    cursor = db.cursor()

    cursor.execute(
        """
        SELECT *
        FROM exercicios
        WHERE id = ?
        AND user_id = ?
        """,
        (
            exercicio_id,
            user.id
        )
    )

    exercicio = cursor.fetchone()

    if not exercicio:

        await update.message.reply_text(
            "❌ Questão não encontrada."
        )

        return

    prompt = f"""
{PROMPT_BASE}

CORRIJA A RESPOSTA DO ESTUDANTE.

QUESTÃO:
{exercicio["pergunta"]}

RESPOSTA CORRETA:
{exercicio["resposta_correta"]}

RESPOSTA DO ESTUDANTE:
{resposta_usuario}

Retorne JSON:

acertou: true ou false
nota: número de 0 a 100
feedback: feedback curto
explicacao: explique o erro ou confirme o raciocínio
"""

    try:

        resultado = await chamar_gemini(
            prompt,
            estruturado=True,
            schema=CORRECAO_SCHEMA
        )

        acertou = 1 if resultado[
            "acertou"
        ] else 0

        cursor.execute(
            """
            UPDATE exercicios
            SET resposta_usuario = ?,
                acertou = ?
            WHERE id = ?
            """,
            (
                resposta_usuario,
                acertou,
                exercicio_id
            )
        )

        db.commit()

        registrar_estudo(
            user.id,
            exercicio["assunto"],
            5
        )

        if acertou:

            adicionar_xp(
                user.id,
                20
            )

            emoji = "✅"

        else:

            adicionar_xp(
                user.id,
                5
            )

            emoji = "❌"

        mensagem = f"""
{emoji} CORREÇÃO

Nota: {resultado["nota"]}/100

{resultado["feedback"]}

📚 Explicação:

{resultado["explicacao"]}
"""

        await update.message.reply_text(
            mensagem.strip()
        )

        context.user_data[
            "ultimo_erro"
        ] = resultado["explicacao"]

    except Exception as erro:

        print(
            "ERRO CORREÇÃO:",
            repr(erro)
        )

        await update.message.reply_text(
            "❌ Não consegui corrigir."
        )


# ============================================================
# ❌ EXPLICAR ERRO
# ============================================================

async def erro(
    update,
    context
):

    ultimo = context.user_data.get(
        "ultimo_erro"
    )

    if not ultimo:

        await update.message.reply_text(
            "Ainda não tenho um erro recente para analisar."
        )

        return

    prompt = f"""
{PROMPT_BASE}

O estudante cometeu este erro:

{ultimo}

Explique de maneira simples:

1. Onde está o erro
2. Por que aconteceu
3. Como evitar
4. Um mini exemplo
"""

    try:

        resposta = await chamar_gemini(
            prompt
        )

        await enviar_mensagem(
            update,
            resposta
        )

    except Exception as erro_externo:

        print(
            repr(erro_externo)
        )

        await update.message.reply_text(
            "❌ Não consegui analisar o erro."
        )


# ============================================================
# 🧠 FLASHCARDS
# ============================================================

FLASHCARD_SCHEMA = {
    "type": "object",
    "properties": {
        "cards": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "pergunta": {
                        "type": "string"
                    },
                    "resposta": {
                        "type": "string"
                    }
                },
                "required": [
                    "pergunta",
                    "resposta"
                ]
            }
        }
    },
    "required": [
        "cards"
    ]
}


async def flashcards(
    update,
    context
):

    user = update.effective_user

    assunto = " ".join(
        context.args
    ).strip()

    if not assunto:

        await update.message.reply_text(
            "Use:\n\n"
            "/flashcards assunto"
        )

        return

    await update.message.reply_text(
        "🧠 Criando flashcards..."
    )

    prompt = f"""
{PROMPT_BASE}

Crie 10 flashcards sobre:

{assunto}

Cada card deve ter:
pergunta
resposta

Perguntas curtas.
Respostas objetivas.
Priorize conceitos importantes para prova.
"""

    try:

        dados = await chamar_gemini(
            prompt,
            estruturado=True,
            schema=FLASHCARD_SCHEMA
        )

        cursor = db.cursor()

        agora = datetime.now()

        proxima = agora + timedelta(
            days=1
        )

        for card in dados["cards"][:10]:

            cursor.execute(
                """
                INSERT INTO flashcards
                (
                    user_id,
                    assunto,
                    pergunta,
                    resposta,
                    intervalo,
                    facilidade,
                    revisado_em,
                    proxima_revisao
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user.id,
                    assunto,
                    card["pergunta"],
                    card["resposta"],
                    1,
                    2.5,
                    None,
                    proxima.isoformat()
                )
            )

        db.commit()

        mensagem = "🧠 FLASHCARDS\n\n"

        for i, card in enumerate(
            dados["cards"][:10],
            1
        ):

            mensagem += (
                f"{i}. {card['pergunta']}\n"
                f"→ {card['resposta']}\n\n"
            )

        mensagem += (
            "Use /revisar para começar "
            "a revisão espaçada."
        )

        await enviar_mensagem(
            update,
            mensagem
        )

    except Exception as erro:

        print(
            "ERRO FLASHCARDS:",
            repr(erro)
        )

        await update.message.reply_text(
            "❌ Não consegui criar os flashcards."
        )


# ============================================================
# 🔄 REVISÃO ESPAÇADA
# ============================================================

async def revisar(
    update,
    context
):

    user = update.effective_user

    agora = datetime.now().isoformat()

    cursor = db.cursor()

    cursor.execute(
        """
        SELECT *
        FROM flashcards
        WHERE user_id = ?
        AND (
            proxima_revisao IS NULL
            OR proxima_revisao <= ?
        )
        ORDER BY proxima_revisao ASC
        LIMIT 1
        """,
        (
            user.id,
            agora
        )
    )

    card = cursor.fetchone()

    if not card:

        await update.message.reply_text(
            "🎉 Você não possui flashcards vencidos para revisão."
        )

        return

    context.user_data[
        "card_revisao"
    ] = card["id"]

    mensagem = f"""
🧠 REVISÃO

Assunto:
{card["assunto"]}

❓ {card["pergunta"]}

Pense antes de olhar a resposta.

Quando terminar:

/sabia
ou
/errei
"""

    await update.message.reply_text(
        mensagem.strip()
    )


# ============================================================
# /SABIA
# ============================================================

async def sabia(
    update,
    context
):

    card_id = context.user_data.get(
        "card_revisao"
    )

    if not card_id:

        await update.message.reply_text(
            "Nenhum flashcard em revisão."
        )

        return

    cursor = db.cursor()

    cursor.execute(
        """
        SELECT *
        FROM flashcards
        WHERE id = ?
        """,
        (card_id,)
    )

    card = cursor.fetchone()

    if not card:
        return

    intervalo = min(
        card["intervalo"] * 2,
        30
    )

    proxima = datetime.now() + timedelta(
        days=intervalo
    )

    cursor.execute(
        """
        UPDATE flashcards
        SET intervalo = ?,
            revisado_em = ?,
            proxima_revisao = ?
        WHERE id = ?
        """,
        (
            intervalo,
            datetime.now().isoformat(),
            proxima.isoformat(),
            card_id
        )
    )

    db.commit()

    adicionar_xp(
        update.effective_user.id,
        10
    )

    context.user_data.pop(
        "card_revisao",
        None
    )

    await update.message.reply_text(
        f"✅ Boa!\n\n"
        f"Próxima revisão em aproximadamente "
        f"{intervalo} dia(s)."
    )


# ============================================================
# /ERREI
# ============================================================

async def errei(
    update,
    context
):

    card_id = context.user_data.get(
        "card_revisao"
    )

    if not card_id:

        await update.message.reply_text(
            "Nenhum flashcard em revisão."
        )

        return

    cursor = db.cursor()

    cursor.execute(
        """
        SELECT *
        FROM flashcards
        WHERE id = ?
        """,
        (card_id,)
    )

    card = cursor.fetchone()

    if not card:
        return

    proxima = datetime.now() + timedelta(
        days=1
    )

    cursor.execute(
        """
        UPDATE flashcards
        SET intervalo = 1,
            facilidade = MAX(facilidade - 0.2, 1.3),
            revisado_em = ?,
            proxima_revisao = ?
        WHERE id = ?
        """,
        (
            datetime.now().isoformat(),
            proxima.isoformat(),
            card_id
        )
    )

    db.commit()

    context.user_data.pop(
        "card_revisao",
        None
    )

    await update.message.reply_text(
        f"""
❌ Tudo bem.

A resposta era:

{card["resposta"]}

Esse assunto será revisado novamente amanhã.
"""
    )


# ============================================================
# 📊 DESEMPENHO
# ============================================================

async def desempenho(
    update,
    context
):

    user = update.effective_user

    cursor = db.cursor()

    cursor.execute(
        """
        SELECT
            COUNT(*) total,
            SUM(acertou) acertos
        FROM exercicios
        WHERE user_id = ?
        """,
        (user.id,)
    )

    dados = cursor.fetchone()

    total = dados["total"] or 0
    acertos = dados["acertos"] or 0

    porcentagem = (
        acertos / total * 100
        if total
        else 0
    )

    cursor.execute(
        """
        SELECT
            assunto,
            COUNT(*) total,
            SUM(acertou) acertos
        FROM exercicios
        WHERE user_id = ?
        GROUP BY assunto
        ORDER BY
            (SUM(acertou) * 1.0 / COUNT(*)) ASC
        LIMIT 5
        """,
        (user.id,)
    )

    fracos = cursor.fetchall()

    usuario = cursor.execute(
        """
        SELECT xp, dias_estudo
        FROM usuarios
        WHERE user_id = ?
        """,
        (user.id,)
    ).fetchone()

    mensagem = f"""
📊 SEU DESEMPENHO

📝 Questões:
{total}

✅ Acertos:
{acertos}

🎯 Aproveitamento:
{porcentagem:.1f}%

⭐ XP:
{usuario["xp"] if usuario else 0}

🔥 Dias estudando:
{usuario["dias_estudo"] if usuario else 0}

📉 ASSUNTOS QUE MERECEM ATENÇÃO:
"""

    if fracos:

        for item in fracos:

            taxa = (
                item["acertos"]
                / item["total"]
                * 100
            )

            mensagem += (
                f"\n• {item['assunto']}: "
                f"{taxa:.1f}%"
            )

    else:

        mensagem += "\nAinda não há dados suficientes."

    await update.message.reply_text(
        mensagem
    )


# ============================================================
# 📈 PROGRESSO
# ============================================================

async def progresso(
    update,
    context
):

    user = update.effective_user

    cursor = db.cursor()

    cursor.execute(
        """
        SELECT
            COALESCE(SUM(minutos), 0) minutos
        FROM sessoes
        WHERE user_id = ?
        """,
        (user.id,)
    )

    minutos = cursor.fetchone()["minutos"]

    cursor.execute(
        """
        SELECT
            COUNT(*) quantidade
        FROM flashcards
        WHERE user_id = ?
        """,
        (user.id,)
    )

    cards = cursor.fetchone()["quantidade"]

    cursor.execute(
        """
        SELECT
            COUNT(*) quantidade
        FROM materiais
        WHERE user_id = ?
        """,
        (user.id,)
    )

    materiais = cursor.fetchone()["quantidade"]

    await update.message.reply_text(
        f"""
📈 PROGRESSO

⏱️ Tempo estudado:
{minutos} minutos

🧠 Flashcards:
{cards}

📚 Materiais:
{materiais}

Continue construindo consistência.
"""
    )


# ============================================================
# 🏆 RANKING
# ============================================================

async def ranking(
    update,
    context
):

    cursor = db.cursor()

    cursor.execute(
        """
        SELECT nome, xp
        FROM usuarios
        ORDER BY xp DESC
        LIMIT 10
        """
    )

    usuarios = cursor.fetchall()

    mensagem = "🏆 RANKING\n\n"

    for i, usuario in enumerate(
        usuarios,
        1
    ):

        mensagem += (
            f"{i}. {usuario['nome']} — "
            f"{usuario['xp']} XP\n"
        )

    if not usuarios:

        mensagem += (
            "Ainda não há estudantes registrados."
        )

    await update.message.reply_text(
        mensagem
    )


# ============================================================
# 📅 META
# ============================================================

async def meta(
    update,
    context
):

    texto = " ".join(
        context.args
    ).strip()

    if not texto:

        await update.message.reply_text(
            """
Use:

/meta DATA descrição

Exemplo:

/meta 10/10/2026 Estudar ligações químicas
"""
        )

        return

    partes = texto.split(
        " ",
        1
    )

    if len(partes) < 2:

        await update.message.reply_text(
            "Informe a data e a descrição."
        )

        return

    data = partes[0]
    titulo = partes[1]

    try:

        datetime.strptime(
            data,
            "%d/%m/%Y"
        )

    except ValueError:

        await update.message.reply_text(
            "Data inválida. Use DD/MM/AAAA."
        )

        return

    cursor = db.cursor()

    cursor.execute(
        """
        INSERT INTO metas
        (
            user_id,
            titulo,
            data_limite,
            criada_em
        )
        VALUES (?, ?, ?, ?)
        """,
        (
            update.effective_user.id,
            titulo,
            data,
            datetime.now().isoformat()
        )
    )

    db.commit()

    await update.message.reply_text(
        f"""
🎯 META CRIADA

📅 {data}

🎯 {titulo}
"""
    )


# ============================================================
# 🗓️ CALENDÁRIO
# ============================================================

async def calendario(
    update,
    context
):

    user = update.effective_user

    cursor = db.cursor()

    cursor.execute(
        """
        SELECT *
        FROM metas
        WHERE user_id = ?
        AND concluida = 0
        ORDER BY data_limite
        """,
        (user.id,)
    )

    metas = cursor.fetchall()

    if not metas:

        await update.message.reply_text(
            "📅 Você não possui metas pendentes."
        )

        return

    mensagem = "📅 CALENDÁRIO\n\n"

    for item in metas:

        mensagem += (
            f"📌 {item['data_limite']}\n"
            f"{item['titulo']}\n\n"
        )

    await enviar_mensagem(
        update,
        mensagem
    )


# ============================================================
# 🗓️ CRONOGRAMA
# ============================================================

async def cronograma(
    update,
    context
):

    user = update.effective_user

    assunto = " ".join(
        context.args
    ).strip()

    if not assunto:

        assunto = (
            "Engenharia de Alimentos, "
            "Química, Matemática e revisão"
        )

    await update.message.reply_text(
        "📅 Montando seu cronograma..."
    )

    prompt = f"""
{PROMPT_BASE}

Crie um cronograma de estudos de 7 dias.

Contexto:
{assunto}

Considere:
- teoria
- exercícios
- revisão
- flashcards
- simulados
- descanso

Faça um plano realista.
Não coloque horas absurdas.
"""

    try:

        resposta = await chamar_gemini(
            prompt
        )

        registrar_estudo(
            user.id,
            "Planejamento",
            5
        )

        await enviar_mensagem(
            update,
            "📅 CRONOGRAMA DE 7 DIAS\n\n"
            + resposta
        )

    except Exception as erro:

        print(
            repr(erro)
        )

        await update.message.reply_text(
            "❌ Não consegui criar o cronograma."
        )


# ============================================================
# 📚 MATERIAIS
# ============================================================

async def materiais(
    update,
    context
):

    user = update.effective_user

    cursor = db.cursor()

    cursor.execute(
        """
        SELECT *
        FROM materiais
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT 20
        """,
        (user.id,)
    )

    lista = cursor.fetchall()

    if not lista:

        await update.message.reply_text(
            """
📚 MATERIAIS

Nenhum material cadastrado.

Envie um PDF diretamente aqui.
"""
        )

        return

    mensagem = "📚 SEUS MATERIAIS\n\n"

    for item in lista:

        mensagem += (
            f"📄 {item['nome']}\n"
            f"Tipo: {item['tipo']}\n\n"
        )

    await enviar_mensagem(
        update,
        mensagem
    )


# ============================================================
# 📄 PDF
# ============================================================

async def esperar_pdf(
    arquivo,
    tentativas=30
):

    for tentativa in range(
        tentativas
    ):

        atual = await asyncio.to_thread(
            client.files.get,
            name=arquivo.name
        )

        estado = str(
            getattr(
                atual,
                "state",
                ""
            )
        ).upper()

        print(
            f"📄 PDF: {estado}"
        )

        if (
            "ACTIVE" in estado
            or "READY" in estado
        ):

            return atual

        if "FAILED" in estado:

            raise RuntimeError(
                "Gemini falhou ao processar PDF."
            )

        await asyncio.sleep(2)

    raise TimeoutError(
        "PDF demorou demais."
    )


async def receber_pdf(
    update,
    context
):

    documento = update.message.document

    nome = (
        documento.file_name
        or "documento.pdf"
    )

    tamanho = (
        (documento.file_size or 0)
        / 1024
        / 1024
    )

    if not nome.lower().endswith(
        ".pdf"
    ):

        await update.message.reply_text(
            "❌ Envie um PDF."
        )

        return

    if tamanho > MAX_PDF_MB:

        await update.message.reply_text(
            f"❌ PDF muito grande.\n"
            f"Limite: {MAX_PDF_MB} MB."
        )

        return

    instrucao = (
        update.message.caption
        or "Faça um resumo detalhado para estudo."
    )

    status = await update.message.reply_text(
        "📄 PDF recebido.\n\n"
        "⏳ Processando..."
    )

    caminho = None
    arquivo_gemini = None

    try:

        telegram_file = await context.bot.get_file(
            documento.file_id
        )

        with tempfile.NamedTemporaryFile(
            suffix=".pdf",
            delete=False
        ) as temp:

            caminho = temp.name

        await telegram_file.download_to_drive(
            caminho
        )

        print(
            "☁️ Enviando PDF para Gemini..."
        )

        arquivo_gemini = await asyncio.to_thread(
            client.files.upload,
            file=caminho,
            config={
                "mime_type": "application/pdf"
            }
        )

        arquivo_pronto = await esperar_pdf(
            arquivo_gemini
        )

        await status.edit_text(
            "🧠 PDF pronto.\n\n"
            "Analisando conteúdo..."
        )

        prompt = f"""
{PROMPT_BASE}

Você recebeu um PDF acadêmico.

INSTRUÇÃO DO ESTUDANTE:

{instrucao}

Use o PDF como fonte principal.

Se for resumo:
- assunto principal
- conceitos importantes
- definições
- fórmulas
- exemplos
- pontos para prova
- revisão rápida

Se for questão:
crie questões baseadas no PDF.

Se for flashcards:
crie flashcards baseados no PDF.

Não invente informações.
"""

        resposta = None

        for modelo in MODELOS_GEMINI:

            try:

                interaction = await asyncio.to_thread(
                    client.interactions.create,
                    model=modelo,
                    input=[
                        {
                            "type": "document",
                            "uri": arquivo_pronto.uri,
                            "mime_type": arquivo_pronto.mime_type,
                        },
                        {
                            "type": "text",
                            "text": prompt,
                        }
                    ]
                )

                resposta = getattr(
                    interaction,
                    "output_text",
                    None
                )

                if resposta:
                    break

            except Exception as erro:

                print(
                    f"❌ PDF com {modelo}: "
                    f"{repr(erro)}"
                )

                if not erro_temporario(
                    erro
                ):

                    raise

                await asyncio.sleep(2)

        if not resposta:

            raise RuntimeError(
                "Nenhum modelo conseguiu analisar o PDF."
            )

        resposta = limpar_resposta(
            resposta
        )

        cursor = db.cursor()

        tipo = "PDF"

        legenda_lower = instrucao.lower()

        if "apostila" in legenda_lower:
            tipo = "Apostila"

        elif "anota" in legenda_lower:
            tipo = "Anotação"

        elif "resumo" in legenda_lower:
            tipo = "Resumo"

        cursor.execute(
            """
            INSERT INTO materiais
            (
                user_id,
                nome,
                tipo,
                file_id,
                resumo,
                criado_em
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                update.effective_user.id,
                nome,
                tipo,
                documento.file_id,
                resposta[:10000],
                datetime.now().isoformat()
            )
        )

        db.commit()

        registrar_estudo(
            update.effective_user.id,
            nome,
            15
        )

        adicionar_xp(
            update.effective_user.id,
            25
        )

        try:

            await status.delete()

        except Exception:
            pass

        await enviar_mensagem(
            update,
            resposta
        )

    except Exception as erro:

        print(
            "================================"
        )

        print(
            "❌ ERRO PDF:"
        )

        print(
            repr(erro)
        )

        print(
            "================================"
        )

        try:

            await status.delete()

        except Exception:
            pass

        await update.message.reply_text(
            "❌ Não consegui processar esse PDF.\n\n"
            "Veja o erro no CMD."
        )

    finally:

        if caminho:

            try:

                Path(
                    caminho
                ).unlink(
                    missing_ok=True
                )

            except Exception:
                pass

        if arquivo_gemini:

            try:

                await asyncio.to_thread(
                    client.files.delete,
                    name=arquivo_gemini.name
                )

            except Exception:
                pass


# ============================================================
# 🎯 SIMULADO
# ============================================================

SIMULADO_SCHEMA = {
    "type": "object",
    "properties": {
        "questoes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "numero": {
                        "type": "integer"
                    },
                    "pergunta": {
                        "type": "string"
                    },
                    "alternativas": {
                        "type": "object",
                        "properties": {
                            "A": {"type": "string"},
                            "B": {"type": "string"},
                            "C": {"type": "string"},
                            "D": {"type": "string"},
                            "E": {"type": "string"}
                        },
                        "required": [
                            "A",
                            "B",
                            "C",
                            "D",
                            "E"
                        ]
                    },
                    "resposta": {
                        "type": "string"
                    },
                    "explicacao": {
                        "type": "string"
                    }
                },
                "required": [
                    "numero",
                    "pergunta",
                    "alternativas",
                    "resposta",
                    "explicacao"
                ]
            }
        }
    },
    "required": [
        "questoes"
    ]
}


async def simulado(
    update,
    context
):

    user = update.effective_user

    assunto = " ".join(
        context.args
    ).strip()

    if not assunto:

        assunto = "conteúdos gerais"

    await update.message.reply_text(
        "🎯 Montando seu simulado..."
    )

    prompt = f"""
{PROMPT_BASE}

Crie um simulado com 5 questões de múltipla escolha.

Assunto:
{assunto}

Cada questão deve ter:
A
B
C
D
E

Somente uma alternativa correta.

Retorne JSON.
"""

    try:

        dados = await chamar_gemini(
            prompt,
            estruturado=True,
            schema=SIMULADO_SCHEMA
        )

        questoes = dados[
            "questoes"
        ][:5]

        cursor = db.cursor()

        cursor.execute(
            """
            INSERT INTO simulados
            (
                user_id,
                assunto,
                questoes,
                respostas,
                inicio,
                duracao
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                user.id,
                assunto,
                json.dumps(
                    questoes,
                    ensure_ascii=False
                ),
                "{}",
                datetime.now().isoformat(),
                30
            )
        )

        db.commit()

        simulado_id = cursor.lastrowid

        context.user_data[
            "simulado_id"
        ] = simulado_id

        mensagem = (
            "🎯 MODO PROVA\n\n"
            f"Assunto: {assunto}\n"
            "Questões: 5\n"
            "Tempo: 30 minutos\n\n"
        )

        for q in questoes:

            mensagem += (
                f"{q['numero']}. "
                f"{q['pergunta']}\n\n"
                f"A) {q['alternativas']['A']}\n"
                f"B) {q['alternativas']['B']}\n"
                f"C) {q['alternativas']['C']}\n"
                f"D) {q['alternativas']['D']}\n"
                f"E) {q['alternativas']['E']}\n\n"
            )

        mensagem += (
            "Quando terminar:\n\n"
            "/respostas 1A 2B 3C 4D 5E\n\n"
            "ou use /finalizar."
        )

        await enviar_mensagem(
            update,
            mensagem
        )

        # Cronômetro automático
        if context.job_queue:

            context.job_queue.run_once(
                finalizar_tempo_prova,
                30 * 60,
                data={
                    "chat_id": update.effective_chat.id,
                    "simulado_id": simulado_id
                },
                name=f"simulado_{simulado_id}",
                chat_id=update.effective_chat.id
            )

    except Exception as erro:

        print(
            "ERRO SIMULADO:",
            repr(erro)
        )

        await update.message.reply_text(
            "❌ Não consegui gerar o simulado."
        )


# ============================================================
# RESPOSTAS DO SIMULADO
# ============================================================

def extrair_respostas(
    texto
):

    respostas = {}

    padrao = re.findall(
        r"(\d+)\s*[-:=]?\s*([ABCDE])",
        texto.upper()
    )

    for numero, letra in padrao:

        respostas[
            str(numero)
        ] = letra

    return respostas


async def respostas(
    update,
    context
):

    user = update.effective_user

    texto = " ".join(
        context.args
    ).strip()

    if not texto:

        await update.message.reply_text(
            "Use:\n\n"
            "/respostas 1A 2B 3C 4D 5E"
        )

        return

    simulado_id = context.user_data.get(
        "simulado_id"
    )

    if not simulado_id:

        await update.message.reply_text(
            "❌ Nenhum simulado ativo."
        )

        return

    respostas_usuario = extrair_respostas(
        texto
    )

    cursor = db.cursor()

    cursor.execute(
        """
        SELECT *
        FROM simulados
        WHERE id = ?
        AND user_id = ?
        """,
        (
            simulado_id,
            user.id
        )
    )

    simulado_data = cursor.fetchone()

    if not simulado_data:
        return

    questoes = json.loads(
        simulado_data["questoes"]
    )

    acertos = 0

    relatorio = "📋 CORREÇÃO\n\n"

    for q in questoes:

        numero = str(
            q["numero"]
        )

        resposta_usuario = respostas_usuario.get(
            numero,
            "-"
        )

        correta = q[
            "resposta"
        ].upper()

        if resposta_usuario == correta:

            acertos += 1

            relatorio += (
                f"✅ Questão {numero}: correta\n"
            )

        else:

            relatorio += (
                f"❌ Questão {numero}: "
                f"você marcou {resposta_usuario}, "
                f"correta: {correta}\n"
            )

    nota = (
        acertos
        / len(questoes)
        * 10
    )

    cursor.execute(
        """
        UPDATE simulados
        SET respostas = ?,
            finalizado = 1,
            nota = ?
        WHERE id = ?
        """,
        (
            json.dumps(
                respostas_usuario
            ),
            nota,
            simulado_id
        )
    )

    db.commit()

    adicionar_xp(
        user.id,
        acertos * 20
    )

    registrar_estudo(
        user.id,
        simulado_data["assunto"],
        30
    )

    relatorio += (
        f"\n🎯 NOTA: {nota:.1f}/10\n"
        f"📊 Acertos: {acertos}/{len(questoes)}"
    )

    await enviar_mensagem(
        update,
        relatorio
    )

    context.user_data.pop(
        "simulado_id",
        None
    )


# ============================================================
# /FINALIZAR
# ============================================================

async def finalizar(
    update,
    context
):

    simulado_id = context.user_data.get(
        "simulado_id"
    )

    if not simulado_id:

        await update.message.reply_text(
            "Nenhum simulado ativo."
        )

        return

    await update.message.reply_text(
        "📋 Envie suas respostas primeiro:\n\n"
        "/respostas 1A 2B 3C 4D 5E"
    )


# ============================================================
# ⏱️ CRONÔMETRO
# ============================================================

async def cronometro(
    update,
    context
):

    if not context.args:

        await update.message.reply_text(
            "Use:\n\n"
            "/cronometro 30"
        )

        return

    try:

        minutos = int(
            context.args[0]
        )

    except ValueError:

        await update.message.reply_text(
            "Digite os minutos."
        )

        return

    if minutos <= 0:

        await update.message.reply_text(
            "O tempo precisa ser maior que zero."
        )

        return

    if not context.job_queue:

        await update.message.reply_text(
            "❌ JobQueue não está disponível."
        )

        return

    context.job_queue.run_once(
        cronometro_final,
        minutos * 60,
        data={
            "chat_id":
                update.effective_chat.id
        },
        name=f"timer_{update.effective_user.id}"
    )

    await update.message.reply_text(
        f"⏱️ Cronômetro iniciado.\n\n"
        f"Tempo: {minutos} minutos."
    )


async def cronometro_final(
    context
):

    data = context.job.data

    await context.bot.send_message(
        chat_id=data["chat_id"],
        text=(
            "⏰ TEMPO ESGOTADO!\n\n"
            "Pare o que está fazendo e faça "
            "uma pausa ou confira suas respostas."
        )
    )


async def finalizar_tempo_prova(
    context
):

    data = context.job.data

    await context.bot.send_message(
        chat_id=data["chat_id"],
        text=(
            "⏰ TEMPO DO SIMULADO ESGOTADO!\n\n"
            "Envie suas respostas agora:\n"
            "/respostas 1A 2B 3C 4D 5E"
        )
    )


# ============================================================
# TEXTO NORMAL
# ============================================================

async def receber_texto(
    update,
    context
):

    user = update.effective_user

    registrar_usuario(
        user.id,
        user.first_name
    )

    pergunta = update.message.text.strip()

    if not pergunta:
        return

    await update.message.reply_text(
        "⏳ Pensando..."
    )

    prompt = f"""
{PROMPT_BASE}

PERGUNTA DO ESTUDANTE:

{pergunta}

Responda como tutor particular.
"""

    try:

        resposta = await chamar_gemini(
            prompt
        )

        registrar_estudo(
            user.id,
            "Tutor",
            5
        )

        adicionar_xp(
            user.id,
            5
        )

        await enviar_mensagem(
            update,
            resposta
        )

    except Exception as erro:

        print(
            "ERRO TEXTO:",
            repr(erro)
        )

        await update.message.reply_text(
            "❌ O Gemini está temporariamente "
            "indisponível."
        )


# ============================================================
# ERROS
# ============================================================

async def erro_handler(
    update,
    context
):

    print(
        "❌ ERRO:",
        repr(context.error)
    )


# ============================================================
# MAIN
# ============================================================

def main():

    print("")
    print(
        "========================================"
    )
    print(
        "🤖 THIGAS AI — BOT DE ESTUDOS"
    )
    print(
        "========================================"
    )
    print(
        "👨‍🏫 Tutor"
    )
    print(
        "📝 Exercícios"
    )
    print(
        "🧠 Flashcards"
    )
    print(
        "🔄 Revisão espaçada"
    )
    print(
        "📊 Desempenho"
    )
    print(
        "📅 Planejamento"
    )
    print(
        "📚 Materiais"
    )
    print(
        "🎯 Modo prova"
    )
    print(
        "⏱️ Cronômetro"
    )
    print(
        "📄 PDF"
    )
    print(
        "💾 SQLite"
    )
    print(
        "========================================"
    )

    print("")
    print(
        "Modelos:"
    )

    for modelo in MODELOS_GEMINI:

        print(
            f"  • {modelo}"
        )

    print("")

    application = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .build()
    )

    # ========================================================
    # COMANDOS
    # ========================================================

    application.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    application.add_handler(
        CommandHandler(
            "ajuda",
            ajuda
        )
    )

    application.add_handler(
        CommandHandler(
            "tutor",
            tutor
        )
    )

    application.add_handler(
        CommandHandler(
            "questao",
            questao
        )
    )

    application.add_handler(
        CommandHandler(
            "corrigir",
            corrigir
        )
    )

    application.add_handler(
        CommandHandler(
            "erro",
            erro
        )
    )

    application.add_handler(
        CommandHandler(
            "flashcards",
            flashcards
        )
    )

    application.add_handler(
        CommandHandler(
            "revisar",
            revisar
        )
    )

    application.add_handler(
        CommandHandler(
            "sabia",
            sabia
        )
    )

    application.add_handler(
        CommandHandler(
            "errei",
            errei
        )
    )

    application.add_handler(
        CommandHandler(
            "desempenho",
            desempenho
        )
    )

    application.add_handler(
        CommandHandler(
            "progresso",
            progresso
        )
    )

    application.add_handler(
        CommandHandler(
            "ranking",
            ranking
        )
    )

    application.add_handler(
        CommandHandler(
            "meta",
            meta
        )
    )

    application.add_handler(
        CommandHandler(
            "cronograma",
            cronograma
        )
    )

    application.add_handler(
        CommandHandler(
            "calendario",
            calendario
        )
    )

    application.add_handler(
        CommandHandler(
            "materiais",
            materiais
        )
    )

    application.add_handler(
        CommandHandler(
            "simulado",
            simulado
        )
    )

    application.add_handler(
        CommandHandler(
            "respostas",
            respostas
        )
    )

    application.add_handler(
        CommandHandler(
            "finalizar",
            finalizar
        )
    )

    application.add_handler(
        CommandHandler(
            "cronometro",
            cronometro
        )
    )

    # ========================================================
    # PDF
    # ========================================================

    application.add_handler(
        MessageHandler(
            filters.Document.PDF,
            receber_pdf
        )
    )

    # ========================================================
    # TEXTO
    # ========================================================

    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            receber_texto
        )
    )

    application.add_error_handler(
        erro_handler
    )

    print("")
    print(
        "🤖 BOT INICIADO!"
    )
    print(
        "📡 Aguardando mensagens..."
    )
    print("")

    application.run_polling()


# ============================================================
# EXECUÇÃO
# ============================================================

if __name__ == "__main__":

    main()