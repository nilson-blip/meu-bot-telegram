import os
import asyncio
import hashlib
import hmac
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from supabase import create_client, Client

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardRemove,
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
# CONFIGURAÇÕES E CLIENTES GLOBAIS
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
if not SUPABASE_URL: faltando.append("SUPABASE_URL")
if not SUPABASE_KEY: faltando.append("SUPABASE_SECRET_KEY")
if not MP_TOKEN: faltando.append("MERCADOPAGO_ACCESS_TOKEN")
if not BOT_TOKEN: faltando.append("BOT_TOKEN")

if faltando:
    raise RuntimeError("Variáveis ausentes: " + ", ".join(faltando))

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

telegram_app: Application = (
    Application.builder()
    .token(BOT_TOKEN)
    .updater(None)
    .build()
)


# ============================================================
# ESTADOS DO CONVERSATION HANDLER (MÚLTIPLOS PLANOS)
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
# FUNÇÕES AUXILIARES DE BANCO
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
            "created_at": datetime.now(timezone.utc).isoformat()
        })
        .execute()
    )
    return novo_cliente.data[0]["id"]


async def registrar_bot(client_id: int):
    bot_info = await telegram_app.bot.get_me()

    existente = (
        supabase.table("telegram_bots")
        .select("id")
        .eq("bot_id", bot_info.id)
        .limit(1)
        .execute()
    )

    if existente.data:
        return existente.data[0]["id"]

    resultado = (
        supabase.table("telegram_bots")
        .insert({
            "client_id": client_id,
            "bot_id": bot_info.id,
            "username": bot_info.username,
            "bot_name": bot_info.first_name,
            "bot_token": BOT_TOKEN,
            "status": "active",
        })
        .execute()
    )
    return resultado.data[0]["id"]


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
            created_at_str = pagamento["created_at"].replace("Z", "+00:00")
            criado_em = datetime.fromisoformat(created_at_str)
            if criado_em.tzinfo is None:
                criado_em = criado_em.replace(tzinfo=timezone.utc)

            minutos_passados = (agora - criado_em).total_seconds() / 60
            if minutos_passados < 5:
                continue

            telegram_user_id = int(pagamento["telegram_user_id"])
            payment_url = pagamento.get("payment_url")
            if not payment_url:
                continue

            await telegram_app.bot.send_message(
                chat_id=telegram_user_id,
                text=(
                    "⌛ Seu pagamento ainda não foi concluído.\n\n"
                    "Seu acesso VIP está esperando por você!\n\n"
                    "👇 Se ainda quiser entrar, finalize o pagamento:"
                ),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💳 Finalizar pagamento", url=payment_url)]
                ]),
            )

            supabase.table("payments").update({"remarketing_enviado": True}).eq("id", pagamento["id"]).execute()
        except Exception as erro:
            print(f"❌ ERRO REMARKETING: {erro}")


async def registrar_acesso(telegram_user_id: int, payment_id: str):
    pagamento = (
        supabase.table("payments")
        .select("dias_acesso, client_id")
        .eq("id", payment_id)
        .single()
        .execute()
    )

    if not pagamento.data:
        raise RuntimeError("Pagamento não encontrado.")

    dias_acesso = pagamento.data["dias_acesso"]
    client_id = pagamento.data["client_id"]
    agora = datetime.now(timezone.utc)

    existente = (
        supabase.table("access_control")
        .select("*")
        .eq("telegram_user_id", telegram_user_id)
        .eq("client_id", client_id)
        .execute()
    ).data

    if existente:
        acesso = existente[0]
        expiracao_atual = datetime.fromisoformat(acesso["data_expiracao"].replace("Z", "+00:00"))
        nova_expiracao = (expiracao_atual if expiracao_atual > agora else agora) + timedelta(days=dias_acesso)

        supabase.table("access_control").update({
            "payment_id": payment_id,
            "data_inicio": agora.isoformat(),
            "data_expiracao": nova_expiracao.isoformat(),
            "status": "ativo",
            "aviso_expiracao_enviado": False,
            "atualizado_em": agora.isoformat(),
        }).eq("telegram_user_id", telegram_user_id).eq("client_id", client_id).execute()
    else:
        nova_expiracao = agora + timedelta(days=dias_acesso)
        supabase.table("access_control").insert({
            "telegram_user_id": telegram_user_id,
            "client_id": client_id,
            "payment_id": payment_id,
            "data_inicio": agora.isoformat(),
            "data_expiracao": nova_expiracao.isoformat(),
            "status": "ativo",
            "criado_em": agora.isoformat(),
            "atualizado_em": agora.isoformat(),
        }).execute()


async def verificar_acessos():
    agora = datetime.now(timezone.utc)
    acessos = supabase.table("access_control").select("*").eq("status", "ativo").execute()

    if not acessos.data:
        return

    for acesso in acessos.data:
        try:
            telegram_user_id = acesso["telegram_user_id"]
            data_expiracao = datetime.fromisoformat(acesso["data_expiracao"].replace("Z", "+00:00"))

            if (data_expiracao - agora).total_seconds() <= 0:
                pagamento = (
                    supabase.table("payments")
                    .select("vip_group_id")
                    .eq("id", acesso["payment_id"])
                    .limit(1)
                    .execute()
                )

                if pagamento.data:
                    vip_group_id = pagamento.data[0]["vip_group_id"]
                    vip_group = (
                        supabase.table("vip_groups")
                        .select("chat_id")
                        .eq("id", vip_group_id)
                        .single()
                        .execute()
                    )

                    if vip_group.data:
                        vip_chat_id = vip_group.data["chat_id"]
                        try:
                            await telegram_app.bot.ban_chat_member(chat_id=vip_chat_id, user_id=telegram_user_id)
                            await telegram_app.bot.unban_chat_member(chat_id=vip_chat_id, user_id=telegram_user_id, only_if_banned=True)
                        except Exception as erro_remocao:
                            print(f"⚠️ ERRO REMOÇÃO {telegram_user_id}: {erro_remocao}")

                supabase.table("access_control").update({
                    "status": "expirado",
                    "atualizado_em": agora.isoformat(),
                }).eq("telegram_user_id", telegram_user_id).eq("client_id", acesso["client_id"]).execute()

        except Exception as erro:
            print(f"❌ ERRO VERIFICAR ACESSO: {erro}")


# ============================================================
# ONBOARDING DO ADMINISTRADOR (SUPORTE A MÚLTIPLOS PLANOS)
# ============================================================

async def cancelar_onboarding(update: Update, context):
    context.user_data.clear()
    mensagem = "❌ Configuração cancelada. Envie `/configurar` para reiniciar."
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.reply_text(mensagem, parse_mode="Markdown")
    else:
        await update.message.reply_text(mensagem, parse_mode="Markdown")
    return ConversationHandler.END


async def iniciar_configuracao(update: Update, context):
    context.user_data["planos"] = []  # Inicializa lista de planos
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Começar Configuração", callback_data="iniciar_onboarding")],
        [InlineKeyboardButton("❌ Cancelar", callback_data="cancelar_onboarding")]
    ])

    await update.message.reply_text(
        "👋 **Seja bem-vindo ao assistente de configuração!**\n\n"
        "Vamos configurar o seu bot e cadastrar seus planos de assinatura.\n"
        "Clique no botão abaixo para iniciar.",
        reply_markup=keyboard,
        parse_mode="Markdown",
    )
    return AGUARDANDO_NOME_BOT


async def passo1_nome_bot(update: Update, context):
    query = update.callback_query
    await query.answer()

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancelar", callback_data="cancelar_onboarding")]
    ])

    await query.message.reply_text(
        "🏷️ **Passo 1 de 6: Nome do Bot**\n\n"
        "Digite o **nome público** do seu Bot (Ex: *VIP Sinais Bot*):",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    return AGUARDANDO_NOME_BOT


async def receber_nome_bot(update: Update, context):
    context.user_data["bot_name"] = update.message.text.strip()

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancelar", callback_data="cancelar_onboarding")]
    ])

    await update.message.reply_text(
        "🤖 **Passo 2 de 6: Token do Bot**\n\n"
        "Cole aqui o **Bot Token** gerado pelo @BotFather:",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    return AGUARDANDO_TOKEN_BOT


async def receber_token_bot(update: Update, context):
    context.user_data["bot_token"] = update.message.text.strip()

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancelar", callback_data="cancelar_onboarding")]
    ])

    await update.message.reply_text(
        "📦 **Passo 3 de 6: Produto/Apresentação**\n\n"
        "Digite o **Nome do Grupo/Produto** e a **Mensagem de Boas-Vindas/Saudação**:\n\n"
        "Exemplo:\n"
        "`Sala VIP de Sinais - Bem-vindo! Escolha abaixo o melhor plano para você ter acesso aos sinais.`",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    return AGUARDANDO_PRODUTO_INFO


async def receber_produto_info(update: Update, context):
    texto = update.message.text.strip()
    partes = texto.split(" - ", 1)

    if len(partes) == 2:
        context.user_data["produto_nome"] = partes[0].strip()
        context.user_data["saudacao"] = partes[1].strip()
    else:
        context.user_data["produto_nome"] = texto
        context.user_data["saudacao"] = "Seja bem-vindo ao nosso espaço VIP!"

    return await exibir_menu_planos(update, context)


async def exibir_menu_planos(update: Update, context):
    planos = context.user_data.get("planos", [])

    resumo = ""
    if planos:
        resumo = "📋 **Planos cadastrados até agora:**\n"
        for idx, p in enumerate(planos, 1):
            resumo += f"• **Plano {p['tempo'].capitalize()}**: R$ {p['valor']:.2f}\n"
        resumo += "\n"

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🗓 Semanal (7 dias)", callback_data="add_semanal"),
            InlineKeyboardButton("🗓 Quinzenal (15 dias)", callback_data="add_quinzenal"),
        ],
        [
            InlineKeyboardButton("🗓 Mensal (30 dias)", callback_data="add_mensal"),
            InlineKeyboardButton("♾️ Vitalício", callback_data="add_vitalicio"),
        ],
        [InlineKeyboardButton("❌ Cancelar", callback_data="cancelar_onboarding")]
    ])

    msg_texto = (
        f"{resumo}"
        "💰 **Configuração de Planos**\n\n"
        "Selecione o tipo do plano que deseja adicionar à oferta:"
    )

    if update.callback_query:
        await update.callback_query.message.reply_text(msg_texto, reply_markup=keyboard, parse_mode="Markdown")
    else:
        await update.message.reply_text(msg_texto, reply_markup=keyboard, parse_mode="Markdown")

    return AGUARDANDO_SELECAO_PLANO


async def receber_selecao_plano(update: Update, context):
    query = update.callback_query
    await query.answer()

    tempo_selecionado = query.data.replace("add_", "")
    context.user_data["plano_em_edicao"] = tempo_selecionado

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancelar", callback_data="cancelar_onboarding")]
    ])

    await query.message.reply_text(
        f"💲 Digite o valor para o **Plano {tempo_selecionado.capitalize()}** (Exemplo: `29.90`):",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    return AGUARDANDO_VALOR_PLANO


async def receber_valor_plano(update: Update, context):
    try:
        valor = float(update.message.text.replace(",", "."))
    except ValueError:
        await update.message.reply_text("❌ Valor inválido. Digite um número ex: `29.90`:")
        return AGUARDANDO_VALOR_PLANO

    tempo = context.user_data.pop("plano_em_edicao")
    
    dias_map = {"semanal": 7, "quinzenal": 15, "mensal": 30, "vitalicio": 36500}
    dias = dias_map.get(tempo, 30)

    # Adiciona o plano na lista
    context.user_data["planos"].append({
        "tempo": tempo,
        "valor": valor,
        "dias": dias
    })

    planos = context.user_data["planos"]
    resumo = "✅ **Plano Adicionado!**\n\n📋 **Planos Atuais:**\n"
    for p in planos:
        resumo += f"• **{p['tempo'].capitalize()}**: R$ {p['valor']:.2f}\n"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Adicionar outro plano", callback_data="add_mais_planos")],
        [InlineKeyboardButton("➡️ Avançar para Mídia", callback_data="avancar_midia")],
        [InlineKeyboardButton("❌ Cancelar", callback_data="cancelar_onboarding")]
    ])

    await update.message.reply_text(
        f"{resumo}\nDeseja cadastrar mais algum plano ou prosseguir?",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    return AGUARDANDO_DECISAO_MAIS_PLANOS


async def decisao_mais_planos(update: Update, context):
    query = update.callback_query
    await query.answer()

    if query.data == "add_mais_planos":
        return await exibir_menu_planos(update, context)

    # Avançar para Mídia
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⏩ Pular Mídia", callback_data="pular_midia")],
        [InlineKeyboardButton("❌ Cancelar", callback_data="cancelar_onboarding")]
    ])

    await query.message.reply_text(
        "🖼️ **Mídia Promocional (Opcional)**\n\n"
        "Envie uma foto ou vídeo para a capa do `/start` ou clique em pular:",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    return AGUARDANDO_MIDIA


async def receber_midia(update: Update, context):
    media_file_id = None
    media_type = None

    if update.message:
        if update.message.photo:
            media_file_id = update.message.photo[-1].file_id
            media_type = "photo"
        elif update.message.video:
            media_file_id = update.message.video.file_id
            media_type = "video"
    elif update.callback_query:
        await update.callback_query.answer()

    context.user_data["media_file_id"] = media_file_id
    context.user_data["media_type"] = media_type

    telegram_user_id = update.effective_user.id
    client_id = obter_ou_criar_cliente(telegram_user_id)

    grupos = (
        supabase.table("vip_groups")
        .select("id, title, chat_id")
        .eq("client_id", client_id)
        .execute()
    )

    if grupos.data:
        botoes_grupos = []
        for g in grupos.data:
            botoes_grupos.append([
                InlineKeyboardButton(f"📢 {g['title']}", callback_data=f"selecionar_grupo_{g['id']}")
            ])
        botoes_grupos.append([InlineKeyboardButton("❌ Cancelar", callback_data="cancelar_onboarding")])
        keyboard = InlineKeyboardMarkup(botoes_grupos)
        msg = "📢 **Passo 4 de 6: Vincular Grupo VIP**\n\nSelecione o canal de destino:"
    else:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Cancelar", callback_data="cancelar_onboarding")]
        ])
        msg = (
            "📢 **Passo 4 de 6: Vincular Grupo VIP**\n\n"
            "Envie o **ID do Chat/Canal VIP** (Ex: `-100123456789`):"
        )

    await (update.message or update.callback_query.message).reply_text(
        msg, reply_markup=keyboard, parse_mode="Markdown"
    )
    return AGUARDANDO_GRUPO_VIP


async def receber_grupo_vip(update: Update, context):
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        vip_group_id = query.data.replace("selecionar_grupo_", "")
        context.user_data["vip_group_id"] = vip_group_id
    else:
        chat_id_input = update.message.text.strip()
        telegram_user_id = update.effective_user.id
        client_id = obter_ou_criar_cliente(telegram_user_id)

        res = supabase.table("vip_groups").upsert({
            "client_id": client_id,
            "chat_id": chat_id_input,
            "title": "Canal VIP",
            "created_at": datetime.now(timezone.utc).isoformat()
        }).execute()
        context.user_data["vip_group_id"] = res.data[0]["id"]

    dados = context.user_data
    planos_txt = ""
    for p in dados.get("planos", []):
        planos_txt += f"• **Plano {p['tempo'].capitalize()}**: R$ {p['valor']:.2f}\n"

    texto_revisao = (
        "📋 **Passo 5 de 6: Revisão Final**\n\n"
        f"🤖 **Nome do Bot:** {dados.get('bot_name')}\n"
        f"📦 **Produto:** {dados.get('produto_nome')}\n"
        f"💬 **Saudação:** {dados.get('saudacao')}\n"
        f"📢 **Grupo VIP ID:** {dados.get('vip_group_id')}\n\n"
        f"💳 **Planos Configurados:**\n{planos_txt}\n"
        "Confirme antes de concluir."
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 CONCLUIR CONFIGURAÇÃO", callback_data="concluir_onboarding")],
        [InlineKeyboardButton("❌ Cancelar", callback_data="cancelar_onboarding")]
    ])

    await (update.message or update.callback_query.message).reply_text(
        texto_revisao, reply_markup=keyboard, parse_mode="Markdown"
    )
    return AGUARDANDO_REVISAO


async def concluir_configuracao(update: Update, context):
    query = update.callback_query
    await query.answer()

    dados = context.user_data
    telegram_user_id = update.effective_user.id
    client_id = obter_ou_criar_cliente(telegram_user_id)

    # Inativa produtos antigos deste cliente para dar lugar à nova esteira de planos
    supabase.table("products").update({"status": "inactive"}).eq("client_id", client_id).execute()

    # Salva todos os planos informados na tabela de produtos
    for p in dados.get("planos", []):
        supabase.table("products").insert({
            "client_id": client_id,
            "title": dados.get("produto_nome"),
            "greeting_message": dados.get("saudacao"),
            "price": p["valor"],
            "duration_type": p["tempo"],
            "duration_days": p["dias"],
            "media_file_id": dados.get("media_file_id"),
            "media_type": dados.get("media_type"),
            "vip_group_id": dados.get("vip_group_id"),
            "status": "active",
        }).execute()

    bot_info = await context.bot.get_me()
    supabase.table("telegram_bots").upsert({
        "client_id": client_id,
        "bot_id": bot_info.id,
        "username": bot_info.username,
        "bot_name": dados.get("bot_name"),
        "bot_token": dados.get("bot_token"),
        "status": "active"
    }).execute()

    params = {
        "client_id": MP_CLIENT_ID,
        "response_type": "code",
        "platform_id": "mp",
        "redirect_uri": MP_REDIRECT_URI,
        "state": str(telegram_user_id),
    }

    link_mp = f"https://auth.mercadopago.com.br/authorization?{urlencode(params)}"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Conectar Mercado Pago", url=link_mp)],
        [InlineKeyboardButton("✅ Concluir Tudo", callback_data="finalizar_tudo")]
    ])

    await query.message.reply_text(
        "🎉 **Configuração Salva com Sucesso!**\n\n"
        "💳 **Passo 6 de 6: Vinculação do Mercado Pago**\n\n"
        "Conecte sua conta do Mercado Pago para liberar o recebimento Pix automático:",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    return ConversationHandler.END


# ============================================================
# FLUXO DO CLIENTE FINAL (/start COM MULTIPLOS BOTÕES)
# ============================================================

async def start(update: Update, context):
    await update.message.reply_text("⏳ Carregando planos...", reply_markup=ReplyKeyboardRemove())

    bot_username = (await context.bot.get_me()).username

    bot_data = (
        supabase.table("telegram_bots")
        .select("client_id")
        .eq("username", bot_username)
        .limit(1)
        .execute()
    )

    if not bot_data.data:
        await update.message.reply_text("🤖 Bot ainda não configurado pelo administrador.")
        return

    client_id = bot_data.data[0]["client_id"]

    # Busca TODOS os planos ativos deste client_id
    produtos = (
        supabase.table("products")
        .select("*")
        .eq("client_id", client_id)
        .eq("status", "active")
        .execute()
    )

    if not produtos.data:
        await update.message.reply_text("📋 Nenhuma oferta disponível no momento.")
        return

    primeiro_prod = produtos.data[0]
    saudacao = primeiro_prod.get("greeting_message") or "Seja bem-vindo!"
    titulo = primeiro_prod.get("title") or "Acesso VIP Exclusivo"

    media_id = primeiro_prod.get("media_file_id")
    media_type = primeiro_prod.get("media_type")

    texto_oferta = (
        f"{saudacao}\n\n"
        f"🌟 **{titulo}**\n\n"
        "👇 Escolha o seu plano abaixo:"
    )

    # Monta um botão inline para cada plano cadastrado no banco
    botoes_planos = []
    for prod in produtos.data:
        tempo = str(prod.get("duration_type", "mensal")).capitalize()
        preco = float(prod.get("price", 0.0))
        botoes_planos.append([
            InlineKeyboardButton(
                f"⚡ Plano {tempo} - R$ {preco:.2f}",
                callback_data=f"comprar_{prod['id']}"
            )
        ])

    keyboard = InlineKeyboardMarkup(botoes_planos)

    if media_id and media_type == "photo":
        await update.message.reply_photo(photo=media_id, caption=texto_oferta, reply_markup=keyboard, parse_mode="Markdown")
    elif media_id and media_type == "video":
        await update.message.reply_video(video=media_id, caption=texto_oferta, reply_markup=keyboard, parse_mode="Markdown")
    else:
        await update.message.reply_text(text=texto_oferta, reply_markup=keyboard, parse_mode="Markdown")


async def botoes(update: Update, context):
    query = update.callback_query
    await query.answer()

    if query.data == "finalizar_tudo":
        await query.message.reply_text("✨ Sistema pronto para operar.")
        return

    if query.data.startswith("comprar_"):
        try:
            product_id = query.data.replace("comprar_", "")

            # Busca exatamente o plano clicado pelo cliente
            produto_res = (
                supabase.table("products")
                .select("*")
                .eq("id", product_id)
                .single()
                .execute()
            )

            if not produto_res.data:
                raise RuntimeError("Plano não encontrado.")

            produto = produto_res.data
            client_id = produto["client_id"]

            conexao = (
                supabase.table("payment_connections")
                .select("id, access_token, fee_percentage")
                .eq("client_id", client_id)
                .eq("status", "active")
                .limit(1)
                .execute()
            )

            if not conexao.data:
                raise RuntimeError("Nenhuma conexão de pagamento ativa encontrada.")

            payment_connection_id = conexao.data[0]["id"]
            client_access_token = conexao.data[0].get("access_token") or MP_TOKEN
            fee_percentage = float(conexao.data[0].get("fee_percentage") or 5.0)

            preco = float(produto["price"])
            dias_acesso = produto["duration_days"]
            vip_group_id = produto.get("vip_group_id")

            marketplace_fee = round(preco * (fee_percentage / 100.0), 2)

            headers = {
                "Authorization": f"Bearer {client_access_token}",
                "Content-Type": "application/json",
                "X-Idempotency-Key": str(uuid.uuid4()),
            }

            order_data = {
                "type": "online",
                "total_amount": f"{preco:.2f}",
                "external_reference": f"vip_{query.from_user.id}",
                "processing_mode": "manual",
                "capture_mode": "automatic",
                "marketplace_fee": f"{marketplace_fee:.2f}",
                "transactions": {
                    "payments": [
                        {
                            "amount": f"{preco:.2f}",
                            "payment_method": {
                                "id": "pix",
                                "type": "bank_transfer",
                            },
                        }
                    ]
                },
                "payer": {"email": "cliente@email.com"},
            }

            async with httpx.AsyncClient(follow_redirects=True) as client:
                response = await client.post(
                    "https://api.mercadopago.com/v1/orders",
                    headers=headers,
                    json=order_data,
                    timeout=20.0,
                )

            response.raise_for_status()
            order = response.json()

            payments = order.get("transactions", {}).get("payments", [])
            if not payments:
                raise RuntimeError("Mercado Pago não retornou o pagamento.")

            payment_data = payments[0]
            payment_method = payment_data.get("payment_method", {})
            payment_url = payment_method.get("ticket_url") or payment_method.get("external_resource_url")
            order_id = order.get("id")

            supabase.table("payments").insert({
                "order_id": order_id,
                "telegram_user_id": query.from_user.id,
                "amount": preco,
                "status": "pending",
                "external_reference": order.get("external_reference", f"vip_{query.from_user.id}"),
                "dias_acesso": dias_acesso,
                "payment_url": payment_url,
                "remarketing_enviado": False,
                "client_id": client_id,
                "product_id": product_id,
                "vip_group_id": vip_group_id,
                "payment_connection_id": payment_connection_id,
            }).execute()

            await context.bot.send_message(
                chat_id=query.from_user.id,
                text=(
                    f"💰 Pix gerado para o **Plano {str(produto['duration_type']).capitalize()}**!\n\n"
                    "👇 Clique no botão abaixo para efetuar o pagamento:"
                ),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💳 Pagar via Pix", url=payment_url)]
                ]),
                parse_mode="Markdown"
            )

        except Exception as erro:
            print(f"❌ ERRO AO GERAR PAGAMENTO: {erro}")
            try:
                await context.bot.send_message(
                    chat_id=query.from_user.id,
                    text="❌ Não foi possível gerar o Pix agora. Tente novamente em instantes.",
                )
            except Exception:
                pass


# ============================================================
# LIFESPAN
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI):

    onboarding_handler = ConversationHandler(
        entry_points=[CommandHandler("configurar", iniciar_configuracao)],
        states={
            AGUARDANDO_NOME_BOT: [
                CallbackQueryHandler(passo1_nome_bot, pattern="^iniciar_onboarding$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receber_nome_bot),
            ],
            AGUARDANDO_TOKEN_BOT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receber_token_bot),
            ],
            AGUARDANDO_PRODUTO_INFO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receber_produto_info),
            ],
            AGUARDANDO_SELECAO_PLANO: [
                CallbackQueryHandler(receber_selecao_plano, pattern="^add_"),
            ],
            AGUARDANDO_VALOR_PLANO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receber_valor_plano),
            ],
            AGUARDANDO_DECISAO_MAIS_PLANOS: [
                CallbackQueryHandler(decisao_mais_planos, pattern="^(add_mais_planos|avancar_midia)$"),
            ],
            AGUARDANDO_MIDIA: [
                CallbackQueryHandler(receber_midia, pattern="^pular_midia$"),
                MessageHandler(filters.PHOTO | filters.VIDEO, receber_midia),
            ],
            AGUARDANDO_GRUPO_VIP: [
                CallbackQueryHandler(receber_grupo_vip, pattern="^selecionar_grupo_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receber_grupo_vip),
            ],
            AGUARDANDO_REVISAO: [
                CallbackQueryHandler(concluir_configuracao, pattern="^concluir_onboarding$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancelar", cancelar_onboarding),
            CommandHandler("configurar", iniciar_configuracao),
            CallbackQueryHandler(cancelar_onboarding, pattern="^cancelar_onboarding$"),
        ],
    )

    telegram_app.add_handler(onboarding_handler)
    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CallbackQueryHandler(botoes))
    telegram_app.add_handler(
    ChatMemberHandler(
        callback=capturar_novo_canal,
        chat_member_types=ChatMemberHandler.MY_CHAT_MEMBER
    )
)

    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.bot.initialize()

    yield

    await telegram_app.stop()
    await telegram_app.shutdown()


app = FastAPI(lifespan=lifespan)


# ============================================================
# ENDPOINTS HTTP (HOME, VERIFICAÇÃO, OAUTH & WEBHOOKS)
# ============================================================

@app.get("/")
async def home():
    return PlainTextResponse("Bot Mestre SaaS Online!")


@app.get("/registrar-bot")
async def registrar_bot_endpoint(request: Request, client_id: int):
    cron_secret = os.getenv("CRON_SECRET")
    token = request.query_params.get("token")

    if not cron_secret or token != cron_secret:
        return PlainTextResponse("Não autorizado.", status_code=401)

    try:
        bot_id = await registrar_bot(client_id)
        return PlainTextResponse(f"Bot registrado. ID: {bot_id}")
    except Exception:
        return PlainTextResponse("Erro ao registrar bot.", status_code=500)


@app.get("/verificar-acessos")
async def verificar_acessos_endpoint(request: Request):
    cron_secret = os.getenv("CRON_SECRET")
    token = request.query_params.get("token")

    if not cron_secret or token != cron_secret:
        return PlainTextResponse("Não autorizado.", status_code=401)

    try:
        await verificar_remarketing()
        await verificar_acessos()
        return PlainTextResponse("Verificação executada.")
    except Exception:
        return PlainTextResponse("Erro na verificação.", status_code=500)


@app.get("/conectar-mercadopago")
async def conectar_mercadopago(client_id: int):
    if not MP_CLIENT_ID:
        return PlainTextResponse("MERCADOPAGO_CLIENT_ID não configurado.", status_code=500)

    params = {
        "client_id": MP_CLIENT_ID,
        "response_type": "code",
        "platform_id": "mp",
        "redirect_uri": MP_REDIRECT_URI,
        "state": str(client_id),
    }

    return RedirectResponse(url="https://auth.mercadopago.com.br/authorization?" + urlencode(params))


@app.get("/oauth/callback")
async def oauth_callback(request: Request):
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    error = request.query_params.get("error")

    if error or not code or not state:
        return PlainTextResponse("Erro ou parâmetro ausente na autenticação OAuth.", status_code=400)

    try:
        telegram_user_id = int(state)
        client_id = obter_ou_criar_cliente(telegram_user_id)
    except Exception:
        return PlainTextResponse("Erro ao identificar cliente.", status_code=500)

    try:
        oauth_data = {
            "client_id": MP_CLIENT_ID,
            "client_secret": MP_CLIENT_SECRET,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": MP_REDIRECT_URI,
        }

        async with httpx.AsyncClient(follow_redirects=True, verify=True) as client:
            response = await client.post(
                "https://api.mercadopago.com/oauth/token",
                data=oauth_data,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": "Botche-OAuth-Client/1.0",
                },
                timeout=30.0,
            )

        if response.status_code != 200:
            return PlainTextResponse("Erro na autorização do Mercado Pago.", status_code=400)

        oauth = response.json()
        access_token = oauth.get("access_token")

        conexao_existente = (
            supabase.table("payment_connections")
            .select("id")
            .eq("client_id", client_id)
            .limit(1)
            .execute()
        )

        dados_conexao = {
            "client_id": client_id,
            "provider": "mercadopago",
            "access_token": access_token,
            "refresh_token": oauth.get("refresh_token"),
            "mp_user_id": str(oauth.get("user_id")),
            "public_key": oauth.get("public_key"),
            "fee_percentage": 5,
            "status": "active",
        }

        if conexao_existente.data:
            supabase.table("payment_connections").update(dados_conexao).eq("id", conexao_existente.data[0]["id"]).execute()
        else:
            supabase.table("payment_connections").insert(dados_conexao).execute()

        return PlainTextResponse("✅ Mercado Pago conectado com sucesso! Pode fechar esta aba e retornar ao Telegram.")

    except Exception as erro:
        return PlainTextResponse(f"Erro na conexão OAuth: {erro}", status_code=500)


@app.post("/telegram")
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
        update = Update.de_json(data, bot=telegram_app.bot)
        await telegram_app.process_update(update)
        return PlainTextResponse("OK")
    except Exception as e:
        return PlainTextResponse(f"Erro: {e}", status_code=500)


@app.post("/mercadopago")
async def mercadopago_webhook(request: Request):
    try:
        x_signature = request.headers.get("x-signature")
        x_request_id = request.headers.get("x-request-id")
        data = await request.json()

        if data.get("type") != "order":
            return PlainTextResponse("OK")

        order_data = data.get("data", {})
        order_id = order_data.get("id")
        data_id = request.query_params.get("data.id") or order_id

        ts = None
        v1 = None

        if x_signature:
            for part in x_signature.split(","):
                part = part.strip()
                if "=" in part:
                    key, value = part.split("=", 1)
                    if key.strip() == "ts":
                        ts = value.strip()
                    elif key.strip() == "v1":
                        v1 = value.strip()

        manifest = f"id:{data_id};request-id:{x_request_id};ts:{ts};"

        if not v1 or not MP_WEBHOOK_SECRET:
            return PlainTextResponse("Invalid signature", status_code=401)

        signature = hmac.new(MP_WEBHOOK_SECRET.encode(), manifest.encode(), hashlib.sha256).hexdigest()

        if not hmac.compare_digest(signature, v1):
            return PlainTextResponse("Invalid signature", status_code=401)

        if not order_id:
            return PlainTextResponse("OK")

        pagamento = (
            supabase.table("payments")
            .select("id, status, invite_enviado, client_id, vip_group_id, product_id, payment_connection_id")
            .eq("order_id", order_id)
            .limit(1)
            .execute()
        )

        if not pagamento.data:
            return PlainTextResponse("OK")

        pagamento_atual = pagamento.data[0]
        payment_connection_id = pagamento_atual.get("payment_connection_id")
        order_access_token = None

        if payment_connection_id:
            conexao = (
                supabase.table("payment_connections")
                .select("access_token")
                .eq("id", payment_connection_id)
                .limit(1)
                .execute()
            )
            if conexao.data:
                order_access_token = conexao.data[0].get("access_token")

        order_access_token = order_access_token or MP_TOKEN

        headers = {"Authorization": f"Bearer {order_access_token}"}

        async with httpx.AsyncClient(follow_redirects=True) as client:
            response = await client.get(f"https://api.mercadopago.com/v1/orders/{order_id}", headers=headers, timeout=20.0)

        response.raise_for_status()
        order = response.json()
        status = order.get("status")
        external_reference = order.get("external_reference")

        if status == "processed":
            if not external_reference or not external_reference.startswith("vip_"):
                return PlainTextResponse("OK")

            telegram_user_id = int(external_reference.replace("vip_", ""))

            if pagamento_atual.get("invite_enviado"):
                return PlainTextResponse("OK")

            produto = (
                supabase.table("products")
                .select("duration_days")
                .eq("id", pagamento_atual["product_id"])
                .single()
                .execute()
            )

            dias_acesso = produto.data["duration_days"] if produto.data else 30
            data_expiracao = datetime.now(timezone.utc) + timedelta(days=dias_acesso)

            supabase.table("payments").update({
                "status": "approved",
                "data_expiracao": data_expiracao.isoformat(),
            }).eq("order_id", order_id).execute()

            await registrar_acesso(telegram_user_id, pagamento_atual["id"])

            supabase.table("subscriptions").insert({
                "client_id": pagamento_atual["client_id"],
                "vip_group_id": pagamento_atual["vip_group_id"],
                "product_id": pagamento_atual["product_id"],
                "telegram_user_id": telegram_user_id,
                "payment_id": pagamento_atual["id"],
                "status": "active",
                "started_at": datetime.now(timezone.utc).isoformat(),
                "expires_at": data_expiracao.isoformat(),
            }).execute()

            vip_group = (
                supabase.table("vip_groups")
                .select("chat_id")
                .eq("id", pagamento_atual["vip_group_id"])
                .single()
                .execute()
            )

            if vip_group.data:
                invite = await telegram_app.bot.create_chat_invite_link(
                    chat_id=vip_group.data["chat_id"],
                    member_limit=1,
                )

                await telegram_app.bot.send_message(
                    chat_id=telegram_user_id,
                    text=(
                        "✅ Pagamento aprovado!\n\n"
                        "🎉 Seu acesso VIP está liberado!\n\n"
                        "👇 Clique abaixo para entrar no grupo:\n"
                        f"{invite.invite_link}"
                    ),
                )

            supabase.table("payments").update({"invite_enviado": True}).eq("order_id", order_id).execute()

        elif status in ["failed", "refunded", "expired"]:
            supabase.table("payments").update({"status": status}).eq("order_id", order_id).execute()

        return PlainTextResponse("OK")

    except Exception as e:
        print(f"ERRO WEBHOOK MERCADO PAGO: {e}")
        return PlainTextResponse("Erro", status_code=500)
