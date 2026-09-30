import os
import asyncio
import hashlib
import hmac
import uuid
import html
import requests
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from supabase import create_client, Client

from telegram import (
    Bot,
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardRemove,
    ChatMember,
    ChatMemberUpdated,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ChatMemberHandler,
    MessageHandler,
    filters,
)
from telegram.error import TelegramError


# ============================================================
# CONFIGURAÇÕES
# ============================================================

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SECRET_KEY")

MP_TOKEN = os.getenv("MERCADOPAGO_ACCESS_TOKEN")
BOT_TOKEN = os.getenv("BOT_TOKEN")
MP_WEBHOOK_SECRET = os.getenv("MERCADOPAGO_WEBHOOK_SECRET")

MP_CLIENT_ID = os.getenv("MERCADOPAGO_CLIENT_ID")
MP_CLIENT_SECRET = os.getenv("MERCADOPAGO_CLIENT_SECRET")

MP_REDIRECT_URI = (
    "https://meu-bot-telegram-production-d9c3.up.railway.app"
    "/oauth/callback"
)

faltando = []

if not SUPABASE_URL:
    faltando.append("SUPABASE_URL")

if not SUPABASE_KEY:
    faltando.append("SUPABASE_SECRET_KEY")

if not MP_TOKEN:
    faltando.append("MERCADOPAGO_ACCESS_TOKEN")

if not BOT_TOKEN:
    faltando.append("BOT_TOKEN")

if faltando:
    raise RuntimeError(
        "Variáveis ausentes: " + ", ".join(faltando)
    )


supabase: Client = create_client(
    SUPABASE_URL,
    SUPABASE_KEY
)


telegram_app: Application = (
    Application.builder()
    .token(BOT_TOKEN)
    .updater(None)
    .build()
)


# ============================================================
# ESTADOS DO CONVERSATION HANDLER
# ============================================================

(
    AGUARDANDO_NOME_BOT,
    AGUARDANDO_TOKEN_BOT,
    AGUARDANDO_PRODUTO_INFO,
    AGUARDANDO_SELECAO_PLANO,
    AGUARDANDO_VALOR_PLANO,
    AGUARDANDO_DECISAO_MAIS_PLANOS,
    AGUARDANDO_MIDIA,
    AGUARDANDO_GRUPO_VIP,
    AGUARDANDO_REVISAO,
) = range(9)


# ============================================================
# FUNÇÕES AUXILIARES
# ============================================================

def obter_ou_criar_cliente(telegram_user_id: int):

    cliente = (
        supabase.table("clients")
        .select("id")
        .eq("telegram_user_id", telegram_user_id)
        .limit(1)
        .execute()
    )

    if cliente.data:
        return cliente.data[0]["id"]

    novo_cliente = (
        supabase.table("clients")
        .insert({
            "telegram_user_id": telegram_user_id,
            "status": "active",
            "created_at": datetime.now(
                timezone.utc
            ).isoformat()
        })
        .execute()
    )

    return novo_cliente.data[0]["id"]


def obter_bot_do_update(update: Update):
    """
    Retorna o bot que realmente recebeu o Update.
    Não usa telegram_app.bot, pois ele é o Botchê principal.
    """

    try:

        bot = update.get_bot()

        if bot:
            return bot

    except Exception:
        pass

    try:

        if update.effective_message:

            return update.effective_message.get_bot()

    except Exception:
        pass

    try:

        if update.callback_query:

            if update.callback_query.message:

                return (
                    update.callback_query.message.get_bot()
                )

    except Exception:
        pass

    raise RuntimeError(
        "Não foi possível identificar o bot "
        "que recebeu o Update."
    )


async def obter_token_bot_cliente(client_id: int, token_atual: str = None) -> str:
    query = (
        supabase.table("telegram_bots")
        .select("bot_token")
        .eq("client_id", client_id)
        .eq("status", "active")
    )

    # Se sabemos qual bot recebeu o Update,
    # buscamos exatamente esse bot.
    if token_atual:
        query = query.eq("bot_token", token_atual)

    bot_res = query.limit(1).execute()

    if bot_res.data and bot_res.data[0].get("bot_token"):
        token = bot_res.data[0]["bot_token"]

        if token == BOT_TOKEN:
            raise RuntimeError(
                "O bot encontrado é o Botchê principal."
            )

        return token

    raise RuntimeError(
        f"Bot ativo não encontrado para o cliente {client_id}."
    )


async def capturar_novo_canal(update: Update, context):

    result: ChatMemberUpdated = update.my_chat_member

    if not result:
        return

    chat = result.chat
    new_status = result.new_chat_member.status
    old_status = result.old_chat_member.status

    if (
        new_status in [
            ChatMember.ADMINISTRATOR,
            ChatMember.MEMBER
        ]
        and old_status not in [
            ChatMember.ADMINISTRATOR,
            ChatMember.MEMBER
        ]
    ):

        from_user_id = result.from_user.id

        client_id = obter_ou_criar_cliente(
            from_user_id
        )

        try:

            supabase.table("vip_groups").upsert({
                "client_id": client_id,
                "chat_id": str(chat.id),
                "title": chat.title or "Canal/Grupo VIP",
                "created_at": datetime.now(
                    timezone.utc
                ).isoformat()
            }).execute()

            print(
                f"✅ Canal '{chat.title}' "
                f"vinculado ao cliente {client_id}"
            )

        except Exception as e:

            print(
                f"❌ Erro ao registrar canal "
                f"no Supabase: {e}"
            )


# ============================================================
# CADASTRO MANUAL DESATIVADO
# ============================================================

async def registrar_bot(client_id: int):

    raise RuntimeError(
        "Cadastro manual de bot desativado. "
        "Use o fluxo /configurar para o cliente "
        "informar o token do próprio bot."
    )


# ============================================================
# REMARKETING
# ============================================================

async def verificar_remarketing():

    agora = datetime.now(timezone.utc)

    pagamentos = (
        supabase.table("payments")
        .select("*")
        .eq("status", "pending")
        .eq("remarketing_enviado", False)
        .execute()
    )

    if not pagamentos.data:
        return

    for pagamento in pagamentos.data:

        try:

            created_at_str = (
                pagamento["created_at"]
                .replace("Z", "+00:00")
            )

            criado_em = datetime.fromisoformat(
                created_at_str
            )

            if criado_em.tzinfo is None:

                criado_em = criado_em.replace(
                    tzinfo=timezone.utc
                )

            minutos_passados = (
                agora - criado_em
            ).total_seconds() / 60

            if minutos_passados < 5:
                continue

            telegram_user_id = int(
                pagamento["telegram_user_id"]
            )

            payment_url = pagamento.get(
                "payment_url"
            )

            client_id = pagamento.get(
                "client_id"
            )

            if not payment_url:
                continue

            custom_token = (
                await obter_token_bot_cliente(
                    client_id
                )
            )

            async with Bot(
                token=custom_token
            ) as client_bot:

                await client_bot.send_message(
                    chat_id=telegram_user_id,
                    text=(
                        "⌛ <b>Ainda dá tempo de "
                        "garantir sua vaga!</b>\n\n"
                        "Notei que você gerou o Pix "
                        "mas não concluiu o pagamento. "
                        "Seus dados e sua vaga no canal "
                        "VIP estão reservados por "
                        "tempo limitado.\n\n"
                        "Clique no botão abaixo para "
                        "concluir:"
                    ),
                    reply_markup=InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton(
                                "💳 Concluir Meu Acesso",
                                url=payment_url
                            )
                        ]
                    ]),
                    parse_mode="HTML"
                )

            (
                supabase.table("payments")
                .update({
                    "remarketing_enviado": True
                })
                .eq("id", pagamento["id"])
                .execute()
            )

        except Exception as erro:

            print(
                f"❌ ERRO REMARKETING: {erro}"
            )


# ============================================================
# REGISTRAR ACESSO
# ============================================================

async def registrar_acesso(
    telegram_user_id: int,
    payment_id: str
):

    pagamento = (
        supabase.table("payments")
        .select("dias_acesso, client_id")
        .eq("id", payment_id)
        .single()
        .execute()
    )

    if not pagamento.data:

        raise RuntimeError(
            "Pagamento não encontrado."
        )

    dias_acesso = pagamento.data[
        "dias_acesso"
    ]

    client_id = pagamento.data[
        "client_id"
    ]

    agora = datetime.now(timezone.utc)

    existente = (
        supabase.table("access_control")
        .select("*")
        .eq(
            "telegram_user_id",
            telegram_user_id
        )
        .eq(
            "client_id",
            client_id
        )
        .execute()
    ).data

    if existente:

        acesso = existente[0]

        expiracao_atual = datetime.fromisoformat(
            acesso["data_expiracao"]
            .replace("Z", "+00:00")
        )

        nova_expiracao = (
            expiracao_atual
            if expiracao_atual > agora
            else agora
        ) + timedelta(
            days=dias_acesso
        )

        (
            supabase.table("access_control")
            .update({
                "payment_id": payment_id,
                "data_inicio": agora.isoformat(),
                "data_expiracao": nova_expiracao.isoformat(),
                "status": "ativo",
                "aviso_expiracao_enviado": False,
                "atualizado_em": agora.isoformat(),
            })
            .eq(
                "telegram_user_id",
                telegram_user_id
            )
            .eq(
                "client_id",
                client_id
            )
            .execute()
        )

    else:

        nova_expiracao = (
            agora +
            timedelta(days=dias_acesso)
        )

        (
            supabase.table("access_control")
            .insert({
                "telegram_user_id": telegram_user_id,
                "client_id": client_id,
                "payment_id": payment_id,
                "data_inicio": agora.isoformat(),
                "data_expiracao": nova_expiracao.isoformat(),
                "status": "ativo",
                "criado_em": agora.isoformat(),
                "atualizado_em": agora.isoformat(),
            })
            .execute()
        )


# ============================================================
# VERIFICAR ACESSOS
# ============================================================

async def verificar_acessos():

    agora = datetime.now(timezone.utc)

    acessos = (
        supabase.table("access_control")
        .select("*")
        .eq("status", "ativo")
        .execute()
    )

    if not acessos.data:
        return

    for acesso in acessos.data:

        try:

            telegram_user_id = acesso[
                "telegram_user_id"
            ]

            client_id = acesso[
                "client_id"
            ]

            data_expiracao = datetime.fromisoformat(
                acesso["data_expiracao"]
                .replace("Z", "+00:00")
            )

            if (
                data_expiracao - agora
            ).total_seconds() <= 0:

                pagamento = (
                    supabase.table("payments")
                    .select("vip_group_id")
                    .eq(
                        "id",
                        acesso["payment_id"]
                    )
                    .limit(1)
                    .execute()
                )

                if pagamento.data:

                    vip_group_id = pagamento.data[
                        0
                    ]["vip_group_id"]

                    vip_group = (
                        supabase.table("vip_groups")
                        .select("chat_id")
                        .eq(
                            "id",
                            vip_group_id
                        )
                        .single()
                        .execute()
                    )

                    if vip_group.data:

                        vip_chat_id = vip_group.data[
                            "chat_id"
                        ]

                        try:

                            custom_token = (
                                await obter_token_bot_cliente(
                                    client_id
                                )
                            )

                            async with Bot(
                                token=custom_token
                            ) as client_bot:

                                await client_bot.ban_chat_member(
                                    chat_id=vip_chat_id,
                                    user_id=telegram_user_id
                                )

                                await client_bot.unban_chat_member(
                                    chat_id=vip_chat_id,
                                    user_id=telegram_user_id,
                                    only_if_banned=True
                                )

                        except Exception as erro_remocao:

                            print(
                                f"⚠️ ERRO REMOÇÃO "
                                f"{telegram_user_id}: "
                                f"{erro_remocao}"
                            )

                (
                    supabase.table("access_control")
                    .update({
                        "status": "expirado",
                        "atualizado_em": agora.isoformat(),
                    })
                    .eq(
                        "telegram_user_id",
                        telegram_user_id
                    )
                    .eq(
                        "client_id",
                        client_id
                    )
                    .execute()
                )

        except Exception as erro:

            print(
                f"❌ ERRO VERIFICAR ACESSO: "
                f"{erro}"
            )


# ============================================================
# ONBOARDING
# ============================================================

async def cancelar_onboarding(
    update: Update,
    context
):

    context.user_data.clear()

    mensagem = (
        "Configuração interrompida. "
        "Quando quiser reiniciar, basta "
        "digitar `/configurar`."
    )

    if update.callback_query:

        await update.callback_query.answer()

        await update.callback_query.message.reply_text(
            mensagem
        )

    else:

        await update.message.reply_text(
            mensagem
        )

    return ConversationHandler.END


async def iniciar_configuracao(
    update: Update,
    context
):

    context.user_data["planos"] = []

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🚀 Iniciar Configuração",
                callback_data="iniciar_onboarding"
            )
        ],
        [
            InlineKeyboardButton(
                "❌ Cancelar",
                callback_data="cancelar_onboarding"
            )
        ]
    ])

    await update.message.reply_text(
        "👋 <b>Seja muito bem-vindo ao "
        "assistente de vendas!</b>\n\n"
        "Vou te ajudar a configurar seu bot "
        "em poucos passos para você "
        "automatizar as vendas do seu canal VIP.\n\n"
        "ℹ️ <b>Como funcionam as taxas:</b>\n"
        "• <b>Taxa da Plataforma:</b> "
        "5,00% por venda realizada.\n"
        "• <b>Taxa do Mercado Pago:</b> "
        "conforme as taxas da sua conta.\n\n"
        "Vamos começar? Clique no botão abaixo:",
        reply_markup=keyboard,
        parse_mode="HTML",
    )

    return AGUARDANDO_NOME_BOT


async def passo1_nome_bot(
    update: Update,
    context
):

    query = update.callback_query

    await query.answer()

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "❌ Cancelar",
                callback_data="cancelar_onboarding"
            )
        ]
    ])

    await query.message.reply_text(
        "🏷 <b>Passo 1 de 6: "
        "Nome do seu Bot</b>\n\n"
        "Como você gostaria de chamar "
        "o seu bot de vendas?\n"
        "<i>(Exemplo: VIP Premium Bot, "
        "Canal de Sinais Bot)</i>",
        reply_markup=keyboard,
        parse_mode="HTML"
    )

    return AGUARDANDO_NOME_BOT


async def receber_nome_bot(
    update: Update,
    context
):

    context.user_data[
        "bot_name"
    ] = update.message.text.strip()

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "❌ Cancelar",
                callback_data="cancelar_onboarding"
            )
        ]
    ])

    await update.message.reply_text(
        "🤖 <b>Passo 2 de 6: "
        "Token do Telegram</b>\n\n"
        "Agora, por favor, envie o "
        "<b>Token do Bot</b> que você "
        "gerou no @BotFather:",
        reply_markup=keyboard,
        parse_mode="HTML"
    )

    return AGUARDANDO_TOKEN_BOT


async def receber_token_bot(
    update: Update,
    context
):

    token_inserido = update.message.text.strip()
    print(f"🔎 TOKEN DIGITADO: {token_inserido[:12]}...")
    print(f"🔎 BOT PRINCIPAL: {BOT_TOKEN[:12]}...")

    try:

        temp_bot = Bot(
            token=token_inserido
        )

        await temp_bot.initialize()

        bot_info = await temp_bot.get_me()

        await temp_bot.shutdown()

        if token_inserido == BOT_TOKEN:

            await update.message.reply_text(
                "❌ Esse é o token do Botchê principal. "
                "Envie o token de OUTRO bot criado "
                "no @BotFather."
            )

            return AGUARDANDO_TOKEN_BOT

        bot_existente = (
            supabase.table("telegram_bots")
            .select("id, client_id")
            .eq(
                "bot_id",
                bot_info.id
            )
            .limit(1)
            .execute()
        )

        if bot_existente.data:

            cliente_existente = (
                bot_existente.data[0]["client_id"]
            )

            cliente_atual = (
                obter_ou_criar_cliente(
                    update.effective_user.id
                )
            )

            if cliente_existente != cliente_atual:

                await update.message.reply_text(
                    "❌ Esse bot já está vinculado "
                    "a outro cliente.\n\n"
                    "Envie o token de outro bot "
                    "criado no @BotFather."
                )

                return AGUARDANDO_TOKEN_BOT

        context.user_data["bot_id"] = bot_info.id

        context.user_data[
            "bot_username"
        ] = bot_info.username

        context.user_data[
            "bot_token"
        ] = token_inserido

    except Exception as e:

        print(
            f"❌ Erro ao validar token: {e}"
        )

        await update.message.reply_text(
            "❌ <b>Token inválido!</b> "
            "Por favor, verifique o token "
            "gerado no @BotFather e envie "
            "novamente:",
            parse_mode="HTML"
        )

        return AGUARDANDO_TOKEN_BOT

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "❌ Cancelar",
                callback_data="cancelar_onboarding"
            )
        ]
    ])

    username_limpo = html.escape(
        bot_info.username or ""
    )

    await update.message.reply_text(
        f"✅ Bot <b>@{username_limpo}</b> "
        "validado com sucesso!\n\n"
        "📝 <b>Passo 3 de 6: "
        "Apresentação do Produto</b>\n\n"
        "Digite o <b>Nome do seu Produto</b> "
        "e uma <b>Mensagem de Boas-Vindas</b> "
        "para o seu cliente.\n\n"
        "💡 <i>Você pode separar usando "
        "hífen (-), por exemplo:</i>\n"
        "<code>Comunidade VIP - "
        "Seja muito bem-vindo! "
        "Escolha um dos planos abaixo "
        "para liberar seu acesso imediato.</code>",
        reply_markup=keyboard,
        parse_mode="HTML"
    )

    return AGUARDANDO_PRODUTO_INFO


async def receber_produto_info(
    update: Update,
    context
):

    texto = update.message.text.strip()

    partes = texto.split(
        " - ",
        1
    )

    if len(partes) == 2:

        context.user_data[
            "produto_nome"
        ] = partes[0].strip()

        context.user_data[
            "saudacao"
        ] = partes[1].strip()

    else:

        context.user_data[
            "produto_nome"
        ] = texto

        context.user_data[
            "saudacao"
        ] = (
            "Seja muito bem-vindo! "
            "Escolha o plano ideal para você:"
        )

    return await exibir_menu_planos(
        update,
        context
    )


async def exibir_menu_planos(
    update: Update,
    context
):

    planos = context.user_data.get(
        "planos",
        []
    )

    resumo = ""

    if planos:

        resumo = (
            "📋 <b>Planos cadastrados "
            "até o momento:</b>\n"
        )

        for p in planos:

            resumo += (
                f"• <b>Plano "
                f"{html.escape(p['tempo'].capitalize())}"
                f"</b>: R$ {p['valor']:.2f}\n"
            )

        resumo += "\n"

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🗓 Semanal (7 dias)",
                callback_data="add_semanal"
            ),
            InlineKeyboardButton(
                "🗓 Quinzenal (15 dias)",
                callback_data="add_quinzenal"
            ),
        ],
        [
            InlineKeyboardButton(
                "🗓 Mensal (30 dias)",
                callback_data="add_mensal"
            ),
            InlineKeyboardButton(
                "♾ Vitalício",
                callback_data="add_vitalicio"
            ),
        ],
        [
            InlineKeyboardButton(
                "❌ Cancelar",
                callback_data="cancelar_onboarding"
            )
        ]
    ])

    msg_texto = (
        f"{resumo}"
        "💰 <b>Passo 4 de 6: "
        "Planos de Assinatura</b>\n\n"
        "Selecione o período do plano "
        "que você deseja adicionar:"
    )

    if update.callback_query:

        await update.callback_query.message.reply_text(
            msg_texto,
            reply_markup=keyboard,
            parse_mode="HTML"
        )

    else:

        await update.message.reply_text(
            msg_texto,
            reply_markup=keyboard,
            parse_mode="HTML"
        )

    return AGUARDANDO_SELECAO_PLANO


async def receber_selecao_plano(
    update: Update,
    context
):

    query = update.callback_query

    await query.answer()

    tempo_selecionado = query.data.replace(
        "add_",
        ""
    )

    context.user_data[
        "plano_em_edicao"
    ] = tempo_selecionado

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "❌ Cancelar",
                callback_data="cancelar_onboarding"
            )
        ]
    ])

    await query.message.reply_text(
        f"💲 Qual o valor para o "
        f"<b>Plano "
        f"{html.escape(tempo_selecionado.capitalize())}"
        f"</b>?\n\n"
        "<i>(Exemplo: <code>29.90</code>)</i>",
        reply_markup=keyboard,
        parse_mode="HTML"
    )

    return AGUARDANDO_VALOR_PLANO


async def receber_valor_plano(
    update: Update,
    context
):

    try:

        valor = float(
            update.message.text.replace(
                ",",
                "."
            )
        )

    except ValueError:

        await update.message.reply_text(
            "Por favor, informe um valor "
            "numérico válido "
            "(ex: <code>29.90</code>):",
            parse_mode="HTML"
        )

        return AGUARDANDO_VALOR_PLANO

    tempo = context.user_data.pop(
        "plano_em_edicao",
        "mensal"
    )

    dias_map = {
        "semanal": 7,
        "quinzenal": 15,
        "mensal": 30,
        "vitalicio": 36500
    }

    dias = dias_map.get(
        tempo,
        30
    )

    context.user_data[
        "planos"
    ].append({
        "tempo": tempo,
        "valor": valor,
        "dias": dias
    })

    planos = context.user_data[
        "planos"
    ]

    resumo = (
        "✅ <b>Plano Adicionado com sucesso!</b>\n\n"
        "📋 <b>Planos Configurados:</b>\n"
    )

    for p in planos:

        resumo += (
            f"• <b>"
            f"{html.escape(p['tempo'].capitalize())}"
            f"</b>: R$ {p['valor']:.2f}\n"
        )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "➕ Adicionar Outro Plano",
                callback_data="add_mais_planos"
            )
        ],
        [
            InlineKeyboardButton(
                "➡️ Avançar para Mídia",
                callback_data="avancar_midia"
            )
        ],
        [
            InlineKeyboardButton(
                "❌ Cancelar",
                callback_data="cancelar_onboarding"
            )
        ]
    ])

    await update.message.reply_text(
        f"{resumo}\n"
        "Deseja cadastrar mais algum "
        "plano ou avançar?",
        reply_markup=keyboard,
        parse_mode="HTML"
    )

    return AGUARDANDO_DECISAO_MAIS_PLANOS


async def decisao_mais_planos(
    update: Update,
    context
):

    query = update.callback_query

    await query.answer()

    if query.data == "add_mais_planos":

        return await exibir_menu_planos(
            update,
            context
        )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "⏩ Pular Mídia",
                callback_data="pular_midia"
            )
        ],
        [
            InlineKeyboardButton(
                "❌ Cancelar",
                callback_data="cancelar_onboarding"
            )
        ]
    ])

    await query.message.reply_text(
        "🖼️ <b>Passo 5 de 6: "
        "Mídia Promocional (Opcional)</b>\n\n"
        "Envie uma foto ou vídeo para "
        "ser exibido junto com a oferta "
        "do seu bot.\n"
        "Se preferir não colocar mídia agora, "
        "clique em <b>Pular Mídia</b>.",
        reply_markup=keyboard,
        parse_mode="HTML"
    )

    return AGUARDANDO_MIDIA


async def receber_midia(
    update: Update,
    context
):

    if update.message:

        if update.message.photo:

            context.user_data[
                "media_file_id"
            ] = update.message.photo[-1].file_id

            context.user_data[
                "media_type"
            ] = "photo"

        elif update.message.video:

            context.user_data[
                "media_file_id"
            ] = update.message.video.file_id

            context.user_data[
                "media_type"
            ] = "video"

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "➡️ Avançar para Grupo VIP",
                    callback_data="avancar_grupo_vip"
                )
            ],
            [
                InlineKeyboardButton(
                    "❌ Cancelar",
                    callback_data="cancelar_onboarding"
                )
            ]
        ])

        await update.message.reply_text(
            "✅ <b>Mídia recebida com sucesso!</b>\n\n"
            "Clique no botão abaixo para prosseguir:",
            reply_markup=keyboard,
            parse_mode="HTML"
        )

        return AGUARDANDO_MIDIA

    elif update.callback_query:

        query = update.callback_query

        await query.answer()

        if query.data == "pular_midia":

            context.user_data[
                "media_file_id"
            ] = None

            context.user_data[
                "media_type"
            ] = None

        return await exibir_instrucoes_grupo_vip(
            update,
            context
        )


async def exibir_instrucoes_grupo_vip(
    update: Update,
    context
):

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🔍 Já adicionei o meu bot como Admin",
                callback_data="verificar_bot_cliente"
            )
        ],
        [
            InlineKeyboardButton(
                "❌ Cancelar",
                callback_data="cancelar_onboarding"
            )
        ]
    ])

    msg = (
        "📢 <b>Passo 6 de 6: "
        "Vincular Canal/Grupo VIP</b>\n\n"
        "Agora precisamos de conectar "
        "o <b>seu bot</b> ao seu Canal "
        "ou Grupo VIP:\n\n"
        "1. Abra o seu <b>Canal/Grupo VIP</b> "
        "no Telegram.\n"
        "2. Adicione o <b>seu bot</b> como "
        "<b>Administrador</b>.\n"
        "3. Garanta que ele tem permissão "
        "para <i>Convidar Usuários via Link</i>.\n"
        "4. Envie uma mensagem qualquer "
        "no canal para ativar a conexão.\n\n"
        "Quando concluir, clique no botão abaixo:"
    )

    if update.callback_query:

        await update.callback_query.message.reply_text(
            msg,
            reply_markup=keyboard,
            parse_mode="HTML"
        )

    else:

        await update.message.reply_text(
            msg,
            reply_markup=keyboard,
            parse_mode="HTML"
        )

    return AGUARDANDO_GRUPO_VIP


async def verificar_grupo_vip_cliente(
    update: Update,
    context
):

    query = update.callback_query

    await query.answer(
        "Verificando o seu bot no canal..."
    )

    bot_token_cliente = context.user_data.get(
        "bot_token"
    )

    if not bot_token_cliente:

        await query.message.reply_text(
            "❌ Erro: Token do seu bot não "
            "foi encontrado. Por favor, "
            "digite /configurar para reiniciar."
        )

        return AGUARDANDO_GRUPO_VIP

    try:

        url = (
            "https://api.telegram.org/bot"
            f"{bot_token_cliente}/getUpdates"
        )

        response = requests.get(
            url,
            timeout=10
        ).json()

        chat_id = None
        chat_title = None

        if response.get("ok"):

            for result in reversed(
                response.get("result", [])
            ):

                for key in [
                    "my_chat_member",
                    "channel_post",
                    "message"
                ]:

                    if key in result:

                        chat_info = result[
                            key
                        ]["chat"]

                        chat_id = str(
                            chat_info["id"]
                        )

                        chat_title = chat_info.get(
                            "title",
                            "Canal VIP"
                        )

                        break

                if chat_id:
                    break

        if chat_id:

            telegram_user_id = (
                update.effective_user.id
            )

            client_id = (
                obter_ou_criar_cliente(
                    telegram_user_id
                )
            )

            res = (
                supabase.table("vip_groups")
                .upsert({
                    "client_id": client_id,
                    "chat_id": chat_id,
                    "title": chat_title,
                    "created_at": datetime.now(
                        timezone.utc
                    ).isoformat()
                })
                .execute()
            )

            if res.data:

                context.user_data[
                    "vip_group_id"
                ] = res.data[0]["id"]

            await query.message.reply_text(
                f"✅ <b>Canal detetado com sucesso!</b>\n\n"
                f"📢 <b>Canal:</b> "
                f"{html.escape(chat_title)}",
                parse_mode="HTML"
            )

            return await exibir_revisao_final(
                query.message,
                context
            )

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🔄 Tentar Novamente",
                    callback_data="verificar_bot_cliente"
                )
            ],
            [
                InlineKeyboardButton(
                    "❌ Cancelar",
                    callback_data="cancelar_onboarding"
                )
            ]
        ])

        await query.message.reply_text(
            "⚠️ <b>Ainda não conseguimos "
            "detetar o seu bot no canal.</b>\n\n"
            "Certifique-se de que adicionou "
            "o bot como Administrador e "
            "enviou uma mensagem.\n"
            "Depois clique em "
            "<b>Tentar Novamente</b>.",
            reply_markup=keyboard,
            parse_mode="HTML"
        )

        return AGUARDANDO_GRUPO_VIP

    except Exception as e:

        print(
            f"Erro ao verificar bot do cliente: {e}"
        )

        await query.message.reply_text(
            "❌ Ocorreu um erro ao verificar "
            "o bot. Tente novamente."
        )

        return AGUARDANDO_GRUPO_VIP


async def exibir_revisao_final(
    message,
    context
):

    dados = context.user_data

    planos_txt = ""

    for p in dados.get(
        "planos",
        []
    ):

        planos_txt += (
            f"• <b>Plano "
            f"{html.escape(str(p['tempo']).capitalize())}"
            f"</b>: R$ {p['valor']:.2f}\n"
        )

    bot_name_safe = html.escape(
        str(dados.get("bot_name", ""))
    )

    prod_nome_safe = html.escape(
        str(dados.get("produto_nome", ""))
    )

    saudacao_safe = html.escape(
        str(dados.get("saudacao", ""))
    )

    texto_revisao = (
        "📋 <b>Revisão das Configurações</b>\n\n"
        f"🤖 <b>Nome do Bot:</b> "
        f"{bot_name_safe}\n"
        f"📦 <b>Produto:</b> "
        f"{prod_nome_safe}\n"
        f"💬 <b>Mensagem:</b> "
        f"{saudacao_safe}\n"
        f"🖼️ <b>Mídia Anexada:</b> "
        f"{'Sim' if dados.get('media_file_id') else 'Não'}\n\n"
        f"💳 <b>Planos Cadastrados:</b>\n"
        f"{planos_txt}\n"
        "Tudo correto? Clique no botão abaixo "
        "para salvar:"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ Confirmar e Salvar",
                callback_data="concluir_onboarding"
            )
        ],
        [
            InlineKeyboardButton(
                "❌ Cancelar",
                callback_data="cancelar_onboarding"
            )
        ]
    ])

    await message.reply_text(
        texto_revisao,
        reply_markup=keyboard,
        parse_mode="HTML"
    )

    return AGUARDANDO_REVISAO


# ============================================================
# CONCLUIR CONFIGURAÇÃO
# ============================================================

async def concluir_configuracao(
    update: Update,
    context
):

    query = update.callback_query

    await query.answer()

    dados = context.user_data

    telegram_user_id = (
        update.effective_user.id
    )

    client_id = obter_ou_criar_cliente(
        telegram_user_id
    )

    try:

        (
            supabase.table("products")
            .update({
                "status": "inactive"
            })
            .eq(
                "client_id",
                client_id
            )
            .execute()
        )

        vip_group_id = dados.get(
            "vip_group_id"
        )

        for p in dados.get(
            "planos",
            []
        ):

            prod_payload = {
                "client_id": client_id,
                "title": dados.get(
                    "produto_nome",
                    "Produto VIP"
                ),
                "greeting_message": dados.get(
                    "saudacao",
                    "Seja bem-vindo!"
                ),
                "price": float(
                    p["valor"]
                ),
                "duration_type": str(
                    p["tempo"]
                ),
                "duration_days": int(
                    p["dias"]
                ),
                "media_file_id": dados.get(
                    "media_file_id"
                ),
                "media_type": dados.get(
                    "media_type"
                ),
                "status": "active",
            }

            if vip_group_id:

                prod_payload[
                    "vip_group_id"
                ] = vip_group_id

            (
                supabase.table("products")
                .insert(prod_payload)
                .execute()
            )

        custom_bot_token = dados.get("bot_token")
        custom_bot_id = dados.get("bot_id")
        custom_bot_username = dados.get("bot_username")
        custom_bot_name = dados.get("bot_name")

        if not custom_bot_token or not custom_bot_id:

            raise RuntimeError(
                "Dados do bot do cliente não encontrados."
            )

        if custom_bot_token == BOT_TOKEN:

            raise RuntimeError(
                "O bot do cliente não pode usar o "
                "mesmo token do Botchê principal."
            )

        bot_existente = (
            supabase.table("telegram_bots")
            .select(
                "id, client_id, bot_token"
            )
            .eq(
                "bot_id",
                custom_bot_id
            )
            .limit(1)
            .execute()
        )

        if bot_existente.data:

            bot_atual = bot_existente.data[0]

            if bot_atual["client_id"] != client_id:

                raise RuntimeError(
                    "Esse bot do Telegram já está "
                    "vinculado a outro cliente."
                )

            (
                supabase.table("telegram_bots")
                .update({
                    "username": custom_bot_username,
                    "bot_name": custom_bot_name,
                    "bot_token": custom_bot_token,
                    "status": "active"
                })
                .eq(
                    "id",
                    bot_atual["id"]
                )
                .execute()
            )

        else:

            (
                supabase.table("telegram_bots")
                .insert({
                    "client_id": client_id,
                    "bot_id": custom_bot_id,
                    "username": custom_bot_username,
                    "bot_name": custom_bot_name,
                    "bot_token": custom_bot_token,
                    "status": "active"
                })
                .execute()
            )

        if custom_bot_token:

            webhook_url = (
                "https://meu-bot-telegram-production-d9c3.up.railway.app/"
                f"telegram/{custom_bot_token}"
            )

            async with Bot(
                token=custom_bot_token
            ) as custom_bot:

                await custom_bot.set_webhook(
                    url=webhook_url
                )

        params = {
            "client_id": MP_CLIENT_ID,
            "response_type": "code",
            "platform_id": "mp",
            "redirect_uri": MP_REDIRECT_URI,
            "state": str(telegram_user_id),
        }

        link_mp = (
            "https://auth.mercadopago.com.br/"
            "authorization?"
            + urlencode(params)
        )

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🔗 Conectar Mercado Pago",
                    url=link_mp
                )
            ],
            [
                InlineKeyboardButton(
                    "✨ Finalizar e Concluir",
                    callback_data="finalizar_tudo"
                )
            ]
        ])

        await query.message.reply_text(
            "🎉 <b>Configuração Salva "
            "com Sucesso!</b>\n\n"
            "💳 <b>Conectar Mercado Pago "
            "(Última Etapa)</b>\n\n"
            "Clique o botão abaixo para "
            "conectar seu Mercado Pago "
            "com segurança.",
            reply_markup=keyboard,
            parse_mode="HTML"
        )

        return ConversationHandler.END

    except Exception as e:

        print(
            f"❌ ERRO AO CONCLUIR "
            f"CONFIGURAÇÃO: {e}"
        )

        await query.message.reply_text(
            "❌ Ocorreu um erro ao salvar "
            "as configurações. "
            "Tente /configurar."
        )

        return ConversationHandler.END


# ============================================================
# /START
# ============================================================

async def start(
    update: Update,
    context
):

    await update.message.reply_text(
        "⏳ Carregando planos...",
        reply_markup=ReplyKeyboardRemove()
    )

    bot_atual = obter_bot_do_update(update)
    token_atual = bot_atual.token

    bot_data = (
        supabase.table("telegram_bots")
        .select("client_id")
        .eq(
            "bot_token",
            token_atual
        )
        .eq(
            "status",
            "active"
        )
        .limit(1)
        .execute()
    )

    if not bot_data.data:

        await update.message.reply_text(
            "👋 Este bot ainda não está configurado."
        )

        return

    client_id = bot_data.data[0][
        "client_id"
    ]

    produtos = (
        supabase.table("products")
        .select("*")
        .eq(
            "client_id",
            client_id
        )
        .eq(
            "status",
            "active"
        )
        .execute()
    )

    if not produtos.data:

        await update.message.reply_text(
            "📋 Nenhuma oferta disponível "
            "no momento."
        )

        return

    primeiro_prod = produtos.data[0]

    saudacao = html.escape(
        primeiro_prod.get(
            "greeting_message"
        ) or "Seja bem-vindo!"
    )

    titulo = html.escape(
        primeiro_prod.get(
            "title"
        ) or "Acesso VIP Exclusivo"
    )

    media_id = primeiro_prod.get(
        "media_file_id"
    )

    media_type = primeiro_prod.get(
        "media_type"
    )

    texto_oferta = (
        f"{saudacao}\n\n"
        f"🌟 <b>{titulo}</b>\n\n"
        "👇 Escolha abaixo o plano ideal para você:"
    )

    botoes_planos = []

    for prod in produtos.data:

        tempo = str(
            prod.get(
                "duration_type",
                "mensal"
            )
        ).capitalize()

        preco = float(
            prod.get(
                "price",
                0.0
            )
        )

        botoes_planos.append([
            InlineKeyboardButton(
                f"⚡ Plano {tempo} - R$ {preco:.2f}",
                callback_data=f"comprar_{prod['id']}"
            )
        ])

    keyboard = InlineKeyboardMarkup(
        botoes_planos
    )

    if (
        media_id
        and media_type == "photo"
    ):

        await update.message.reply_photo(
            photo=media_id,
            caption=texto_oferta,
            reply_markup=keyboard,
            parse_mode="HTML"
        )

    elif (
        media_id
        and media_type == "video"
    ):

        await update.message.reply_video(
            video=media_id,
            caption=texto_oferta,
            reply_markup=keyboard,
            parse_mode="HTML"
        )

    else:

        await update.message.reply_text(
            text=texto_oferta,
            reply_markup=keyboard,
            parse_mode="HTML"
        )


# ============================================================
# CRIAR CHECKOUT PRO - CARTÃO
# ============================================================

async def criar_checkout_cartao(
    product_id,
    produto,
    client_id,
    query
):

    conexao = (
        supabase.table(
            "payment_connections"
        )
        .select(
            "id, access_token, fee_percentage"
        )
        .eq(
            "client_id",
            client_id
        )
        .eq(
            "status",
            "active"
        )
        .limit(1)
        .execute()
    )

    if not conexao.data:

        raise RuntimeError(
            "Nenhuma conexão de pagamento "
            "ativa encontrada para este cliente."
        )

    payment_connection_id = (
        conexao.data[0]["id"]
    )

    client_access_token = (
        conexao.data[0].get(
            "access_token"
        )
    )

    if not client_access_token:

        raise RuntimeError(
            "A conexão Mercado Pago deste cliente "
            "não possui access_token."
        )

    fee_percentage = float(
        conexao.data[0].get(
            "fee_percentage"
        ) or 5.0
    )

    preco = float(
        produto["price"]
    )

    dias_acesso = int(
        produto["duration_days"]
    )

    vip_group_id = produto.get(
        "vip_group_id"
    )

    marketplace_fee = round(
        preco *
        (fee_percentage / 100.0),
        2
    )

    external_reference = (
        f"vip_{client_id}_"
        f"{query.from_user.id}_"
        f"{uuid.uuid4().hex}"
    )

    preference_data = {
        "items": [
            {
                "id": str(product_id),
                "title": (
                    f"Acesso VIP - "
                    f"{produto.get('title', 'Produto')}"
                ),
                "quantity": 1,
                "currency_id": "BRL",
                "unit_price": round(
                    preco,
                    2
                ),
            }
        ],
        "external_reference": external_reference,
        "marketplace_fee": marketplace_fee,
        "notification_url": (
            "https://meu-bot-telegram-production-d9c3.up.railway.app"
            "/mercadopago"
        ),
        "back_urls": {
            "success": (
                "https://meu-bot-telegram-production-d9c3.up.railway.app/"
            ),
            "failure": (
                "https://meu-bot-telegram-production-d9c3.up.railway.app/"
            ),
            "pending": (
                "https://meu-bot-telegram-production-d9c3.up.railway.app/"
            ),
        },
        "auto_return": "approved",
    }

    async with httpx.AsyncClient(
        follow_redirects=True
    ) as client:

        response = await client.post(
            "https://api.mercadopago.com/"
            "checkout/preferences",
            headers={
                "Authorization":
                    f"Bearer {client_access_token}",
                "Content-Type":
                    "application/json",
            },
            json=preference_data,
            timeout=30.0,
        )

    if response.status_code != 201:

        raise RuntimeError(
            "Mercado Pago recusou o checkout: "
            f"{response.text}"
        )

    preference = response.json()

    preference_id = str(
        preference.get("id")
    )

    payment_url = (
        preference.get("init_point")
        or preference.get(
            "sandbox_init_point"
        )
    )

    if not payment_url:

        raise RuntimeError(
            "Mercado Pago criou a preferência, "
            "mas não retornou init_point."
        )

    supabase.table("payments").insert({
        "order_id": preference_id,
        "telegram_user_id": query.from_user.id,
        "amount": preco,
        "status": "pending",
        "external_reference": external_reference,
        "dias_acesso": dias_acesso,
        "payment_url": payment_url,
        "remarketing_enviado": False,
        "client_id": client_id,
        "product_id": product_id,
        "vip_group_id": vip_group_id,
        "payment_connection_id": payment_connection_id,
    }).execute()

    return payment_url


# ============================================================
# CRIAR PIX DIRETO - SPLIT (SEM EXIGIR E-MAIL)
# ============================================================

async def criar_checkout_marketplace(
    product_id,
    produto,
    client_id,
    query
):

    conexao = (
        supabase.table("payment_connections")
        .select("id, access_token, fee_percentage")
        .eq("client_id", client_id)
        .eq("status", "active")
        .limit(1)
        .execute()
    )

    if not conexao.data:
        raise RuntimeError(
            "Nenhuma conexão Mercado Pago ativa encontrada."
        )

    payment_connection_id = conexao.data[0]["id"]

    client_access_token = conexao.data[0].get("access_token")

    if not client_access_token:
        raise RuntimeError(
            "A conexão Mercado Pago não possui access_token."
        )

    fee_percentage = float(
        conexao.data[0].get("fee_percentage") or 5.0
    )

    preco = round(float(produto["price"]), 2)

    dias_acesso = int(produto["duration_days"])

    vip_group_id = produto.get("vip_group_id")

    marketplace_fee = round(
        preco * (fee_percentage / 100.0),
        2
    )

    external_reference = (
        f"vip_{client_id}_"
        f"{query.from_user.id}_"
        f"{uuid.uuid4().hex}"
    )

    payload = {
        "items": [
            {
                "id": str(product_id),
                "title": str(
                    produto.get(
                        "title",
                        "Acesso VIP"
                    )
                ),
                "currency_id": "BRL",
                "quantity": 1,
                "unit_price": preco
            }
        ],

        "marketplace_fee": marketplace_fee,

        "external_reference": external_reference,

        "notification_url": (
            "https://meu-bot-telegram-production-d9c3.up.railway.app"
            "/mercadopago"
        ),

        "back_urls": {
            "success": (
                "https://meu-bot-telegram-production-d9c3.up.railway.app/"
            ),
            "failure": (
                "https://meu-bot-telegram-production-d9c3.up.railway.app/"
            ),
            "pending": (
                "https://meu-bot-telegram-production-d9c3.up.railway.app/"
            )
        },

        "auto_return": "approved"
    }

    async with httpx.AsyncClient(
        follow_redirects=True
    ) as client:

        response = await client.post(
            "https://api.mercadopago.com/checkout/preferences",

            headers={
                "Authorization":
                    f"Bearer {client_access_token}",
                "Content-Type":
                    "application/json",
                "X-Idempotency-Key":
                    str(uuid.uuid4())
            },

            json=payload,

            timeout=30.0
        )

    if response.status_code not in [200, 201]:

        raise RuntimeError(
            "Mercado Pago recusou o Checkout: "
            f"{response.text}"
        )

    preference = response.json()

    preference_id = preference.get("id")

    init_point = preference.get("init_point")

    if not preference_id or not init_point:

        raise RuntimeError(
            "Mercado Pago não retornou o link do Checkout."
        )

    pagamento = (
        supabase.table("payments")
        .insert({
            "order_id": str(preference_id),
            "telegram_user_id": query.from_user.id,
            "amount": preco,
            "status": "pending",
            "external_reference": external_reference,
            "dias_acesso": dias_acesso,
            "payment_url": init_point,
            "remarketing_enviado": False,
            "client_id": client_id,
            "product_id": product_id,
            "vip_group_id": vip_group_id,
            "payment_connection_id": payment_connection_id
        })
        .execute()
    )

    payment_row_id = (
        pagamento.data[0]["id"]
        if pagamento.data
        else None
    )

    return {
        "preference_id": str(preference_id),
        "payment_row_id": payment_row_id,
        "payment_url": init_point,
        "external_reference": external_reference
    }


# ============================================================
# COMPRA - PIX / CARTÃO (COM TOKEN DO CLIENTE ISOLADO)
# ============================================================

async def botoes(
    update: Update,
    context
):

    query = update.callback_query

    await query.answer()

    # ========================================================
    # IMPORTANTE:
    # identifica EXATAMENTE qual bot recebeu este Update.
    #
    # Antes o código buscava somente pelo client_id.
    # Como o cliente pode ter vários bots ativos, isso podia
    # retornar o Botchê principal.
    # ========================================================

    bot_atual = obter_bot_do_update(update)
    token_atual = bot_atual.token

    print(
        "🔎 CALLBACK RECEBIDO | "
        f"bot_token={token_atual[:12]}..."
    )

    if query.data == "finalizar_tudo":

        await query.message.reply_text(
            "✨ Sistema 100% pronto e operacional!"
        )

        return

    # ========================================================
    # CHECKOUT MERCADO PAGO
    # ========================================================

 if query.data.startswith("plano_"):

        product_id = query.data.replace(
    "plano_",
    "",
    1
        )


        client_id = None

        try:

            produto_res = (
                supabase.table("products")
                .select("*")
                .eq("id", product_id)
                .eq("status", "active")
                .single()
                .execute()
            )

            if not produto_res.data:

                raise RuntimeError(
                    "Plano não encontrado."
                )

            produto = produto_res.data

            client_id = produto["client_id"]

            custom_token = (
                await obter_token_bot_cliente(
                    client_id,
                    token_atual
                )
            )

            print(
                "✅ BOT CLIENTE IDENTIFICADO | "
                f"client_id={client_id}"
            )

            await query.message.reply_text(
                "⏳ Gerando seu checkout..."
            )

            resultado = (
                await criar_checkout_marketplace(
                    product_id,
                    produto,
                    client_id,
                    query
                )
            )

            payment_url = resultado["payment_url"]

            preco = float(
                produto["price"]
            )

        except Exception as erro:

            print(
                f"❌ ERRO AO GERAR CHECKOUT: {erro}"
            )

            try:

                if client_id:

                    custom_token = (
                        await obter_token_bot_cliente(
                            client_id,
                            token_atual
                        )
                    )

                else:

                    custom_token = token_atual

                async with Bot(
                    token=custom_token
                ) as client_bot:

                    await client_bot.send_message(
                        chat_id=query.from_user.id,
                        text=(
                            "❌ Não foi possível "
                            "gerar o pagamento. "
                            "Tente novamente."
                        )
                    )

            except Exception as erro_fallback:

                print(
                    "❌ ERRO FALLBACK CHECKOUT: "
                    f"{erro_fallback}"
                )

        return

    # ========================================================
    # ESCOLHA DO PLANO
    # ========================================================

    if not query.data.startswith("comprar_"):

        return

    client_id = None

    try:

        product_id = query.data.replace(
            "comprar_",
            "",
            1
        )

        produto_res = (
            supabase.table("products")
            .select("*")
            .eq(
                "id",
                product_id
            )
            .eq(
                "status",
                "active"
            )
            .single()
            .execute()
        )

        if not produto_res.data:

            raise RuntimeError(
                "Plano não encontrado."
            )

        produto = produto_res.data

        client_id = produto["client_id"]

        # USA O MESMO BOT QUE RECEBEU O CALLBACK
        custom_token = (
            await obter_token_bot_cliente(
                client_id,
                token_atual
            )
        )

        print(
            "✅ BOT DO CLIENTE IDENTIFICADO | "
            f"client_id={client_id} | "
            f"token={custom_token[:12]}..."
        )


        preco = float(
            produto["price"]
        )

        async with Bot(
            token=custom_token
        ) as client_bot:

            await client_bot.send_message(
                chat_id=query.from_user.id,
                text=(
                    "💰 <b>Escolha a forma de pagamento</b>\n\n"
                    f"📦 Plano: "
                    f"{html.escape(str(produto.get('duration_type', 'Plano')).capitalize())}\n"
                    f"💵 Valor: <b>R$ {preco:.2f}</b>\n\n"
                    "Escolha uma das opções abaixo:"
                ),
                reply_markup=keyboard,
                parse_mode="HTML"
            )

    except Exception as erro:

        print(
            f"❌ ERRO AO PREPARAR PAGAMENTO: "
            f"{erro}"
        )

        try:

            if client_id:

                custom_token = (
                    await obter_token_bot_cliente(
                        client_id,
                        token_atual
                    )
                )

            else:

                custom_token = token_atual

            async with Bot(
                token=custom_token
            ) as client_bot:

                await client_bot.send_message(
                    chat_id=query.from_user.id,
                    text=(
                        "❌ Ocorreu um erro ao preparar "
                        "o pagamento. Por favor, "
                        "tente novamente."
                    )
                )

        except Exception as erro_fallback:

            print(
                "❌ ERRO AO ENVIAR MENSAGEM "
                f"DE ERRO: {erro_fallback}"
            )


# ============================================================
# LIFESPAN
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI):

    onboarding_handler = ConversationHandler(

        entry_points=[
            CommandHandler(
                "configurar",
                iniciar_configuracao
            )
        ],

        states={

            AGUARDANDO_NOME_BOT: [
                CallbackQueryHandler(
                    passo1_nome_bot,
                    pattern="^iniciar_onboarding$"
                ),
                MessageHandler(
                    filters.TEXT &
                    ~filters.COMMAND,
                    receber_nome_bot
                ),
            ],

            AGUARDANDO_TOKEN_BOT: [
                MessageHandler(
                    filters.TEXT &
                    ~filters.COMMAND,
                    receber_token_bot
                ),
            ],

            AGUARDANDO_PRODUTO_INFO: [
                MessageHandler(
                    filters.TEXT &
                    ~filters.COMMAND,
                    receber_produto_info
                ),
            ],

            AGUARDANDO_SELECAO_PLANO: [
                CallbackQueryHandler(
                    receber_selecao_plano,
                    pattern="^add_"
                ),
            ],

            AGUARDANDO_VALOR_PLANO: [
                MessageHandler(
                    filters.TEXT &
                    ~filters.COMMAND,
                    receber_valor_plano
                ),
            ],

            AGUARDANDO_DECISAO_MAIS_PLANOS: [
                CallbackQueryHandler(
                    decisao_mais_planos,
                    pattern=(
                        "^(add_mais_planos|"
                        "avancar_midia)$"
                    ),
                ),
            ],

            AGUARDANDO_MIDIA: [
                CallbackQueryHandler(
                    receber_midia,
                    pattern=(
                        "^(pular_midia|"
                        "avancar_grupo_vip)$"
                    ),
                ),
                MessageHandler(
                    filters.PHOTO |
                    filters.VIDEO,
                    receber_midia
                ),
            ],

            AGUARDANDO_GRUPO_VIP: [
                CallbackQueryHandler(
                    verificar_grupo_vip_cliente,
                    pattern="^verificar_bot_cliente$"
                ),
            ],

            AGUARDANDO_REVISAO: [
                CallbackQueryHandler(
                    concluir_configuracao,
                    pattern="^concluir_onboarding$"
                ),
            ],
        },

        fallbacks=[
            CommandHandler(
                "cancelar",
                cancelar_onboarding
            ),
            CommandHandler(
                "configurar",
                iniciar_configuracao
            ),
            CallbackQueryHandler(
                cancelar_onboarding,
                pattern="^cancelar_onboarding$"
            ),
        ],
    )

    telegram_app.add_handler(
        onboarding_handler
    )

    telegram_app.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    telegram_app.add_handler(
        CallbackQueryHandler(
            botoes
        )
    )

    telegram_app.add_handler(
        ChatMemberHandler(
            capturar_novo_canal,
            ChatMemberHandler.MY_CHAT_MEMBER
        )
    )

    await telegram_app.initialize()
    await telegram_app.start()

    await telegram_app.bot.initialize()

    yield

    await telegram_app.stop()
    await telegram_app.shutdown()


app = FastAPI(
    lifespan=lifespan
)


# ============================================================
# ENDPOINT RAIZ
# ============================================================

@app.get("/")
async def home():

    return PlainTextResponse(
        "Bot Mestre SaaS Online!"
    )


# ============================================================
# REGISTRAR BOT
# ============================================================

@app.get("/registrar-bot")
async def registrar_bot_endpoint(
    request: Request,
    client_id: int
):

    cron_secret = os.getenv(
        "CRON_SECRET"
    )

    token = request.query_params.get(
        "token"
    )

    if (
        not cron_secret
        or token != cron_secret
    ):

        return PlainTextResponse(
            "Não autorizado.",
            status_code=401
        )

    try:

        bot_id = await registrar_bot(
            client_id
        )

        return PlainTextResponse(
            f"Bot registrado. ID: {bot_id}"
        )

    except Exception as erro:

        print(
            f"❌ Cadastro manual bloqueado: {erro}"
        )

        return PlainTextResponse(
            "Cadastro manual de bot desativado. "
            "Use /configurar.",
            status_code=403
        )


# ============================================================
# VERIFICAR ACESSOS
# ============================================================

@app.get("/verificar-acessos")
async def verificar_acessos_endpoint(
    request: Request
):

    cron_secret = os.getenv(
        "CRON_SECRET"
    )

    token = request.query_params.get(
        "token"
    )

    if (
        not cron_secret
        or token != cron_secret
    ):

        return PlainTextResponse(
            "Não autorizado.",
            status_code=401
        )

    try:

        await verificar_remarketing()
        await verificar_acessos()

        return PlainTextResponse(
            "Verificação executada."
        )

    except Exception:

        return PlainTextResponse(
            "Erro na verificação.",
            status_code=500
        )


# ============================================================
# CONECTAR MERCADO PAGO
# ============================================================

@app.get("/conectar-mercadopago")
async def conectar_mercadopago(
    client_id: int
):

    if not MP_CLIENT_ID:

        return PlainTextResponse(
            "MERCADOPAGO_CLIENT_ID não configurado.",
            status_code=500
        )

    params = {
        "client_id": MP_CLIENT_ID,
        "response_type": "code",
        "platform_id": "mp",
        "redirect_uri": MP_REDIRECT_URI,
        "state": str(client_id),
    }

    return RedirectResponse(
        url=(
            "https://auth.mercadopago.com.br/"
            "authorization?"
            + urlencode(params)
        )
    )


# ============================================================
# OAUTH CALLBACK
# ============================================================

@app.get("/oauth/callback")
async def oauth_callback(
    request: Request
):

    code = request.query_params.get(
        "code"
    )

    state = request.query_params.get(
        "state"
    )

    error = request.query_params.get(
        "error"
    )

    if (
        error
        or not code
        or not state
    ):

        return PlainTextResponse(
            "Erro ou parâmetro ausente "
            "na autenticação OAuth.",
            status_code=400
        )

    try:

        telegram_user_id = int(
            state
        )

        client_id = (
            obter_ou_criar_cliente(
                telegram_user_id
            )
        )

    except Exception:

        return PlainTextResponse(
            "Erro ao identificar cliente.",
            status_code=500
        )

    try:

        oauth_data = {
            "client_id":
                MP_CLIENT_ID,
            "client_secret":
                MP_CLIENT_SECRET,
            "grant_type":
                "authorization_code",
            "code":
                code,
            "redirect_uri":
                MP_REDIRECT_URI,
        }

        async with httpx.AsyncClient(
            follow_redirects=True,
            verify=True
        ) as client:

            response = await client.post(
                "https://api.mercadopago.com/oauth/token",
                data=oauth_data,
                headers={
                    "Accept":
                        "application/json",
                    "Content-Type":
                        "application/x-www-form-urlencoded",
                    "User-Agent":
                        "Botche-OAuth-Client/1.0",
                },
                timeout=30.0,
            )

        if response.status_code != 200:

            print(
                "❌ ERRO OAUTH MP: "
                f"{response.text}"
            )

            return PlainTextResponse(
                "Erro na autorização "
                "do Mercado Pago.",
                status_code=400
            )

        oauth = response.json()

        access_token = oauth.get(
            "access_token"
        )

        if not access_token:

            raise RuntimeError(
                "OAuth não retornou access_token."
            )

        print(
            "✅ OAuth Mercado Pago concluído | "
            f"mp_user_id={oauth.get('user_id')}"
        )

        conexao_existente = (
            supabase.table(
                "payment_connections"
            )
            .select("id")
            .eq(
                "client_id",
                client_id
            )
            .limit(1)
            .execute()
        )

        dados_conexao = {
            "client_id":
                client_id,
            "provider":
                "mercadopago",
            "access_token":
                access_token,
            "refresh_token":
                oauth.get(
                    "refresh_token"
                ),
            "mp_user_id":
                str(
                    oauth.get(
                        "user_id"
                    )
                ),
            "public_key":
                oauth.get(
                    "public_key"
                ),
            "fee_percentage":
                5,
            "status":
                "active",
        }

        if conexao_existente.data:

            (
                supabase.table(
                    "payment_connections"
                )
                .update(
                    dados_conexao
                )
                .eq(
                    "id",
                    conexao_existente.data[0]["id"]
                )
                .execute()
            )

        else:

            (
                supabase.table(
                    "payment_connections"
                )
                .insert(
                    dados_conexao
                )
                .execute()
            )

        return PlainTextResponse(
            "✅ Mercado Pago conectado "
            "com sucesso! Pode fechar esta "
            "aba e retornar ao Telegram."
        )

    except Exception as erro:

        print(
            f"❌ ERRO CONEXÃO OAUTH: {erro}"
        )

        return PlainTextResponse(
            f"Erro na conexão OAuth: {erro}",
            status_code=500
        )


# ============================================================
# WEBHOOK TELEGRAM PRINCIPAL
# ============================================================

@app.post("/telegram")
async def telegram_webhook(
    request: Request
):

    try:

        data = await request.json()

        update = Update.de_json(
            data,
            bot=telegram_app.bot
        )

        await telegram_app.process_update(
            update
        )

        return PlainTextResponse(
            "OK"
        )

    except Exception as e:

        print(
            f"❌ Erro Webhook Principal: {e}"
        )

        return PlainTextResponse(
            f"Erro: {e}",
            status_code=500
        )


# ============================================================
# WEBHOOK TELEGRAM CLIENTE
# ============================================================

@app.post("/telegram/{custom_bot_token}")
async def custom_telegram_webhook(
    custom_bot_token: str,
    request: Request
):

    try:

        bot_res = (
            supabase.table("telegram_bots")
            .select("client_id, bot_id, username")
            .eq(
                "bot_token",
                custom_bot_token
            )
            .eq(
                "status",
                "active"
            )
            .limit(1)
            .execute()
        )

        if not bot_res.data:

            print(
                "❌ Webhook recebido com token "
                "de bot cliente não cadastrado."
            )

            return PlainTextResponse(
                "Bot não autorizado.",
                status_code=401
            )

        data = await request.json()

        async with Bot(
            token=custom_bot_token
        ) as custom_bot:

            update = Update.de_json(
                data,
                bot=custom_bot
            )

            print(
                "📨 UPDATE BOT CLIENTE | "
                f"client_id={bot_res.data[0]['client_id']} | "
                f"bot=@{bot_res.data[0].get('username')}"
            )

            await telegram_app.process_update(
                update
            )

        return PlainTextResponse(
            "OK"
        )

    except Exception as e:

        print(
            f"❌ Erro Webhook Customizado: {e}"
        )

        return PlainTextResponse(
            f"Erro: {e}",
            status_code=500
        )


# ============================================================
# WEBHOOK MERCADO PAGO (COM TRATAMENTO ROBUSTO)
# ============================================================

@app.post("/mercadopago")
async def mercadopago_webhook(
    request: Request
):

    try:

        x_signature = request.headers.get(
            "x-signature"
        )

        x_request_id = request.headers.get(
            "x-request-id"
        )

        data = await request.json()

        topic = (
            data.get("type")
            or data.get("action")
        )

        payment_id_hook = None

        if topic == "payment":

            payment_id_hook = (
                data.get(
                    "data",
                    {}
                ).get("id")
            )

        elif (
            topic == "order"
            or str(
                data.get(
                    "resource",
                    ""
                )
            ).startswith(
                "/v1/orders"
            )
        ):

            order_data = data.get(
                "data",
                {}
            )

            payment_id_hook = (
                order_data.get(
                    "id"
                )
            )

        data_id = (
            request.query_params.get(
                "data.id"
            )
            or request.query_params.get(
                "id"
            )
            or payment_id_hook
        )

        ts = None
        v1 = None

        if x_signature:

            for part in x_signature.split(","):

                part = part.strip()

                if "=" in part:

                    key, value = part.split(
                        "=",
                        1
                    )

                    if key.strip() == "ts":

                        ts = value.strip()

                    elif key.strip() == "v1":

                        v1 = value.strip()

        if (
            v1
            and MP_WEBHOOK_SECRET
            and data_id
            and x_request_id
            and ts
        ):

            manifest = (
                f"id:{data_id};"
                f"request-id:{x_request_id};"
                f"ts:{ts};"
            )

            signature = hmac.new(
                MP_WEBHOOK_SECRET.encode(),
                manifest.encode(),
                hashlib.sha256
            ).hexdigest()

            if not hmac.compare_digest(
                signature,
                v1
            ):

                return PlainTextResponse(
                    "Invalid signature",
                    status_code=401
                )

        if not data_id:

            return PlainTextResponse(
                "OK"
            )

        pagamento = (
            supabase.table("payments")
            .select(
                "id, status, invite_enviado, "
                "client_id, vip_group_id, "
                "product_id, payment_connection_id, "
                "external_reference, telegram_user_id"
            )
            .eq(
                "order_id",
                str(data_id)
            )
            .limit(1)
            .execute()
        )

        if not pagamento.data:

            try:

                conexoes = (
                    supabase.table(
                        "payment_connections"
                    )
                    .select(
                        "id, access_token, client_id"
                    )
                    .eq(
                        "status",
                        "active"
                    )
                    .execute()
                )

                payment_info_temp = None
                connection_temp = None

                for conexao_temp in (
                    conexoes.data or []
                ):

                    token_temp = (
                        conexao_temp.get(
                            "access_token"
                        )
                    )

                    if not token_temp:
                        continue

                    try:

                        async with httpx.AsyncClient(
                            follow_redirects=True
                        ) as client:

                            teste = await client.get(
                                f"https://api.mercadopago.com/"
                                f"v1/payments/{data_id}",
                                headers={
                                    "Authorization":
                                        f"Bearer {token_temp}"
                                },
                                timeout=15.0
                            )

                        if teste.status_code == 200:

                            payment_info_temp = (
                                teste.json()
                            )

                            connection_temp = (
                                conexao_temp
                            )

                            break

                    except Exception:
                        continue

                if (
                    payment_info_temp
                    and connection_temp
                ):

                    ext_temp = (
                        payment_info_temp.get(
                            "external_reference"
                        )
                    )

                    if ext_temp:

                        busca_ref = (
                            supabase.table(
                                "payments"
                            )
                            .select(
                                "id, status, "
                                "invite_enviado, "
                                "client_id, "
                                "vip_group_id, "
                                "product_id, "
                                "payment_connection_id, "
                                "external_reference, "
                                "telegram_user_id"
                            )
                            .eq(
                                "external_reference",
                                ext_temp
                            )
                            .eq(
                                "status",
                                "pending"
                            )
                            .limit(1)
                            .execute()
                        )

                        if busca_ref.data:

                            pagamento = busca_ref

                            (
                                supabase.table(
                                    "payments"
                                )
                                .update({
                                    "order_id":
                                        str(data_id)
                                })
                                .eq(
                                    "id",
                                    pagamento.data[0]["id"]
                                )
                                .execute()
                            )

            except Exception as busca_erro:

                print(
                    "⚠️ Erro ao localizar "
                    f"pagamento: {busca_erro}"
                )

        if not pagamento.data:

            return PlainTextResponse(
                "OK"
            )

        pagamento_atual = (
            pagamento.data[0]
        )

        payment_connection_id = (
            pagamento_atual.get(
                "payment_connection_id"
            )
        )

        if not payment_connection_id:

            return PlainTextResponse(
                "OK"
            )

        conexao = (
            supabase.table(
                "payment_connections"
            )
            .select("access_token, client_id")
            .eq(
                "id",
                payment_connection_id
            )
            .eq(
                "status",
                "active"
            )
            .limit(1)
            .execute()
        )

        if not conexao.data:

            return PlainTextResponse(
                "OK"
            )

        order_access_token = (
            conexao.data[0].get(
                "access_token"
            )
        )

        if not order_access_token:

            return PlainTextResponse(
                "OK"
            )

        headers = {
            "Authorization":
                f"Bearer {order_access_token}"
        }

        async with httpx.AsyncClient(
            follow_redirects=True
        ) as client:

            response = await client.get(
                f"https://api.mercadopago.com/"
                f"v1/payments/{data_id}",
                headers=headers,
                timeout=20.0
            )

        if response.status_code != 200:

            return PlainTextResponse(
                "OK"
            )

        payment_info = response.json()

        status = payment_info.get(
            "status"
        )

        external_reference = (
            payment_info.get(
                "external_reference"
            )
        )

        if (
            not external_reference
            or not external_reference.startswith(
                "vip_"
            )
        ):

            return PlainTextResponse(
                "OK"
            )

        if status == "approved":

            if pagamento_atual.get(
                "invite_enviado"
            ):

                return PlainTextResponse(
                    "OK"
                )

            telegram_user_id = (
                pagamento_atual.get(
                    "telegram_user_id"
                )
            )

            if not telegram_user_id:

                return PlainTextResponse(
                    "OK"
                )

            produto = (
                supabase.table("products")
                .select("duration_days")
                .eq(
                    "id",
                    pagamento_atual["product_id"]
                )
                .single()
                .execute()
            )

            dias_acesso = (
                produto.data["duration_days"]
                if produto.data
                else 30
            )

            data_expiracao = (
                datetime.now(timezone.utc)
                + timedelta(
                    days=dias_acesso
                )
            )

            (
                supabase.table("payments")
                .update({
                    "status":
                        "approved",
                    "data_expiracao":
                        data_expiracao.isoformat(),
                    "order_id":
                        str(data_id)
                })
                .eq(
                    "id",
                    pagamento_atual["id"]
                )
                .execute()
            )

            await registrar_acesso(
                telegram_user_id,
                pagamento_atual["id"]
            )

            (
                supabase.table("subscriptions")
                .insert({
                    "client_id":
                        pagamento_atual["client_id"],
                    "vip_group_id":
                        pagamento_atual["vip_group_id"],
                    "product_id":
                        pagamento_atual["product_id"],
                    "telegram_user_id":
                        telegram_user_id,
                    "payment_id":
                        pagamento_atual["id"],
                    "status":
                        "active",
                    "started_at":
                        datetime.now(
                            timezone.utc
                        ).isoformat(),
                    "expires_at":
                        data_expiracao.isoformat(),
                })
                .execute()
            )

            vip_group = (
                supabase.table("vip_groups")
                .select("chat_id")
                .eq(
                    "id",
                    pagamento_atual[
                        "vip_group_id"
                    ]
                )
                .single()
                .execute()
            )

            if vip_group.data:

                custom_token = (
                    await obter_token_bot_cliente(
                        pagamento_atual[
                            "client_id"
                        ]
                    )
                )

                async with Bot(
                    token=custom_token
                ) as client_bot:

                    invite = (
                        await client_bot
                        .create_chat_invite_link(
                            chat_id=vip_group.data[
                                "chat_id"
                            ],
                            member_limit=1,
                        )
                    )

                    await client_bot.send_message(
                        chat_id=telegram_user_id,
                        text=(
                            "✅ <b>Pagamento Aprovado!</b>\n\n"
                            "🎉 Seu acesso VIP foi "
                            "liberado com sucesso!\n\n"
                            "Clique no botão abaixo "
                            "para entrar no canal:"
                        ),
                        reply_markup=InlineKeyboardMarkup([
                            [
                                InlineKeyboardButton(
                                    "🚀 Entrar no Canal VIP",
                                    url=invite.invite_link
                                )
                            ]
                        ]),
                        parse_mode="HTML"
                    )

            (
                supabase.table("payments")
                .update({
                    "invite_enviado": True
                })
                .eq(
                    "id",
                    pagamento_atual["id"]
                )
                .execute()
            )

        elif status in [
            "cancelled",
            "refunded",
            "charged_back",
            "expired"
        ]:

            (
                supabase.table("payments")
                .update({
                    "status": status
                })
                .eq(
                    "id",
                    pagamento_atual["id"]
                )
                .execute()
            )

        return PlainTextResponse(
            "OK"
        )

    except Exception as e:

        print(
            f"❌ ERRO WEBHOOK MERCADO Pago: {e}"
        )

        return PlainTextResponse(
            "Erro",
            status_code=500
        )
