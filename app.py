import os
import asyncio
import hashlib
import hmac
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from supabase import create_client, Client

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
)

# ============================================================
# CONFIGURAÇÕES E CLIENTES GLOBAIS
# ============================================================

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SECRET_KEY")
MP_TOKEN = os.getenv("MERCADOPAGO_ACCESS_TOKEN")
BOT_TOKEN = os.getenv("BOT_TOKEN")
MP_WEBHOOK_SECRET = os.getenv("MERCADOPAGO_WEBHOOK_SECRET")

if not all([SUPABASE_URL, SUPABASE_KEY, MP_TOKEN, BOT_TOKEN]):
    raise RuntimeError("Variáveis de ambiente obrigatórias não configuradas.")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
telegram_app: Application = Application.builder().token(BOT_TOKEN).updater(None).build()


# ============================================================
# LIFESPAN DA APLICAÇÃO (INICIALIZAÇÃO DO BOT)
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Setup handlers do Telegram
    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CallbackQueryHandler(botoes))

    # Inicializa a aplicação do Telegram
    await telegram_app.initialize()
    await telegram_app.start()
    print("🤖 Telegram Bot inicializado com sucesso.")
    
    yield
    
    # Finalização graciosa
    await telegram_app.stop()
    await telegram_app.shutdown()

app = FastAPI(lifespan=lifespan)


# ============================================================
# FUNÇÕES AUXILIARES DE NEGÓCIO
# ============================================================

async def registrar_bot(client_id: int):
    bot_info = await telegram_app.bot.get_me()

    print(f"🤖 BOT IDENTIFICADO: {bot_info.id} | @{bot_info.username}")

    existente = (
        supabase.table("telegram_bots")
        .select("id")
        .eq("bot_id", bot_info.id)
        .limit(1)
        .execute()
    )

    if existente.data:
        print("ℹ️ BOT JÁ ESTÁ REGISTRADO.")
        return existente.data[0]["id"]

    resultado = (
        supabase.table("telegram_bots")
        .insert({
            "client_id": client_id,
            "bot_id": bot_info.id,
            "username": bot_info.username,
            "bot_name": bot_info.first_name,
            "bot_token": BOT_TOKEN,
            "status": "active"
        })
        .execute()
    )

    print("🤖 BOT REGISTRADO NO SUPABASE.")
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
        print("🔎 NENHUM PAGAMENTO PENDENTE PARA REMARKETING.")
        return

    for pagamento in pagamentos.data:
        try:
            criado_em = datetime.fromisoformat(
                pagamento["created_at"].replace("Z", "+00:00")
            )

            minutos_passados = (agora - criado_em).total_seconds() / 60

            if minutos_passados < 5:
                continue

            telegram_user_id = pagamento["telegram_user_id"]
            payment_url = pagamento.get("payment_url")

            if not payment_url:
                print(f"⚠️ PAGAMENTO SEM LINK: {pagamento['id']}")
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
                ])
            )

            supabase.table("payments").update({
                "remarketing_enviado": True
            }).eq("id", pagamento["id"]).execute()

            print(f"📲 REMARKETING ENVIADO: {telegram_user_id}")

        except Exception as erro:
            print(f"❌ ERRO NO REMARKETING: {pagamento.get('id')}: {erro}")


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
        expiracao_atual = datetime.fromisoformat(
            acesso["data_expiracao"].replace("Z", "+00:00")
        )

        if expiracao_atual > agora:
            nova_expiracao = expiracao_atual + timedelta(days=dias_acesso)
        else:
            nova_expiracao = agora + timedelta(days=dias_acesso)

        supabase.table("access_control").update({
            "payment_id": payment_id,
            "data_inicio": agora.isoformat(),
            "data_expiracao": nova_expiracao.isoformat(),
            "status": "ativo",
            "aviso_10_enviado": False,
            "aviso_5_enviado": False,
            "aviso_3_enviado": False,
            "aviso_2_enviado": False,
            "aviso_1_enviado": False,
            "aviso_expiracao_enviado": False,
            "atualizado_em": agora.isoformat()
        }).eq("telegram_user_id", telegram_user_id).eq("client_id", client_id).execute()

        print(f"🔄 ACESSO RENOVADO: {telegram_user_id}")
    else:
        nova_expiracao = agora + timedelta(days=dias_acesso)

        supabase.table("access_control").insert({
            "telegram_user_id": telegram_user_id,
            "client_id": client_id,
            "payment_id": payment_id,
            "data_inicio": agora.isoformat(),
            "data_expiracao": nova_expiracao.isoformat(),
            "status": "ativo",
            "aviso_10_enviado": False,
            "aviso_5_enviado": False,
            "aviso_3_enviado": False,
            "aviso_2_enviado": False,
            "aviso_1_enviado": False,
            "aviso_expiracao_enviado": False,
            "criado_em": agora.isoformat(),
            "atualizado_em": agora.isoformat()
        }).execute()

        print(f"🆕 ACESSO CRIADO: {telegram_user_id}")


async def verificar_acessos():
    agora = datetime.now(timezone.utc)

    acessos = (
        supabase.table("access_control")
        .select("*")
        .eq("status", "ativo")
        .execute()
    )

    if not acessos.data:
        print("🔎 NENHUM ACESSO ATIVO.")
        return

    for acesso in acessos.data:
        try:
            telegram_user_id = acesso["telegram_user_id"]
            data_expiracao = datetime.fromisoformat(
                acesso["data_expiracao"].replace("Z", "+00:00")
            )

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
                            await telegram_app.bot.ban_chat_member(
                                chat_id=vip_chat_id,
                                user_id=telegram_user_id,
                            )
                            await telegram_app.bot.unban_chat_member(
                                chat_id=vip_chat_id,
                                user_id=telegram_user_id,
                                only_if_banned=True,
                            )
                            print(f"🚪 USUÁRIO REMOVIDO DO VIP: {telegram_user_id}")

                        except Exception as erro_remocao:
                            print(f"⚠️ ERRO AO REMOVER {telegram_user_id}: {erro_remocao}")

                supabase.table("access_control").update({
                    "status": "expirado",
                    "atualizado_em": agora.isoformat()
                }).eq("telegram_user_id", telegram_user_id).eq("client_id", acesso["client_id"]).execute()

                print(f"⛔ ACESSO EXPIRADO: {telegram_user_id}")

        except Exception as erro:
            print(f"❌ ERRO AO VERIFICAR ACESSO: {acesso.get('telegram_user_id')}: {erro}")


# ============================================================
# HANDLERS DO TELEGRAM
# ============================================================

async def start(update: Update, context):
    botoes_menu = [
        [InlineKeyboardButton("🛒 Comprar", callback_data="comprar")],
        [InlineKeyboardButton("📋 Ver produtos", callback_data="produtos")],
        [InlineKeyboardButton("❓ Suporte", callback_data="suporte")],
    ]

    await update.message.reply_text(
        "🤖 Olá! Bem-vindo!\n\nEscolha uma opção!👇",
        reply_markup=InlineKeyboardMarkup(botoes_menu),
    )


async def botoes(update: Update, context):
    query = update.callback_query
    await query.answer()

    if query.data in ["comprar", "renovar"]:
        try:
            bot_username = (await context.bot.get_me()).username

            bot_data = (
                supabase.table("telegram_bots")
                .select("id, client_id")
                .eq("username", bot_username)
                .eq("status", "active")
                .limit(1)
                .execute()
            )

            if not bot_data.data:
                raise RuntimeError("Bot Telegram não cadastrado.")

            client_id = bot_data.data[0]["client_id"]

            conexao = (
                supabase.table("payment_connections")
                .select("id")
                .eq("client_id", client_id)
                .eq("status", "active")
                .limit(1)
                .execute()
            )

            if not conexao.data:
                raise RuntimeError("Nenhuma conexão de pagamento ativa encontrada.")

            payment_connection_id = conexao.data[0]["id"]

            produto_resultado = (
                supabase.table("products")
                .select("*")
                .eq("client_id", client_id)
                .eq("status", "active")
                .limit(1)
                .execute()
            )

            if not produto_resultado.data:
                raise RuntimeError("Nenhum produto ativo encontrado.")

            produto = produto_resultado.data[0]
            produto_id = produto["id"]
            preco = float(produto["price"])
            dias_acesso = produto["duration_days"]
            vip_group_id = produto["vip_group_id"]

            headers = {
                "Authorization": f"Bearer {MP_TOKEN}",
                "Content-Type": "application/json",
                "X-Idempotency-Key": str(uuid.uuid4()),
            }

            order_data = {
                "type": "online",
                "total_amount": f"{preco:.2f}",
                "external_reference": f"vip_{query.from_user.id}",
                "processing_mode": "automatic",
                "transactions": {
                    "payments": [
                        {
                            "amount": f"{preco:.2f}",
                            "payment_method": {
                                "id": "pix",
                                "type": "bank_transfer"
                            }
                        }
                    ]
                },
                "payer": {
                    "email": "cliente@email.com"
                }
            }

            async with httpx.AsyncClient() as client:
                response = await client.post(
                    "https://api.mercadopago.com/v1/orders",
                    headers=headers,
                    json=order_data,
                    timeout=20.0,
                )

            print("MERCADO PAGO:", response.status_code)
            response.raise_for_status()

            order = response.json()
            payment_url = order["transactions"]["payments"][0]["payment_method"]["ticket_url"]
            order_id = order["id"]

            supabase.table("payments").insert({
                "order_id": order_id,
                "telegram_user_id": query.from_user.id,
                "amount": preco,
                "status": "pending",
                "external_reference": order["external_reference"],
                "dias_acesso": dias_acesso,
                "data_expiracao": None,
                "payment_url": payment_url,
                "remarketing_enviado": False,
                "client_id": produto["client_id"],
                "product_id": produto_id,
                "vip_group_id": vip_group_id,
                "payment_connection_id": payment_connection_id,
            }).execute()

            await context.bot.send_message(
                chat_id=query.from_user.id,
                text=(
                    "💰 Pix gerado!\n\n"
                    "👇 Clique abaixo para realizar o pagamento:"
                ),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💳 Pagar com Pix", url=payment_url)]
                ])
            )

        except Exception as erro:
            print(f"❌ ERRO AO GERAR PAGAMENTO: {erro}")

    elif query.data == "produtos":
        await query.message.reply_text("📋 Produtos disponíveis.")

    elif query.data == "suporte":
        await query.message.reply_text("❓ Entre em contato com o suporte.")


# ============================================================
# ENDPOINTS FASTAPI
# ============================================================

@app.get("/")
async def home():
    return PlainTextResponse("Bot online!")


@app.get("/registrar-bot")
async def registrar_bot_endpoint(request: Request, client_id: int):
    cron_secret = os.getenv("CRON_SECRET")
    token = request.query_params.get("token")

    if not cron_secret or token != cron_secret:
        return PlainTextResponse("Não autorizado.", status_code=401)

    try:
        bot_id = await registrar_bot(client_id)
        return PlainTextResponse(f"Bot registrado. ID: {bot_id}")
    except Exception as e:
        print(f"ERRO AO REGISTRAR BOT: {type(e).__name__}: {e}")
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
    except Exception as e:
        print(f"ERRO AO VERIFICAR ACESSOS: {type(e).__name__}: {e}")
        return PlainTextResponse("Erro na verificação.", status_code=500)


@app.post("/telegram")
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
        update = Update.de_json(data, bot=telegram_app.bot)
        await telegram_app.process_update(update)
        return PlainTextResponse("OK")
    except Exception as e:
        print(f"ERRO NO WEBHOOK TELEGRAM: {type(e).__name__}: {e}")
        return PlainTextResponse(f"Erro: {type(e).__name__}: {e}", status_code=500)


@app.post("/mercadopago")
async def mercadopago_webhook(request: Request):
    try:
        x_signature = request.headers.get("x-signature")
        x_request_id = request.headers.get("x-request-id")
        data = await request.json()

        print("WEBHOOK MERCADO PAGO:", data)

        if data.get("type") != "order":
            return PlainTextResponse("OK")

        order_data = data.get("data", {})
        order_id = order_data.get("id")
        data_id = request.query_params.get("data.id") or order_id

        # Validar Assinatura Webhook
        ts = None
        v1 = None

        if x_signature:
            for part in x_signature.split(","):
                part = part.strip()
                if "=" not in part:
                    continue
                key, value = part.split("=", 1)
                if key.strip() == "ts":
                    ts = value.strip()
                elif key.strip() == "v1":
                    v1 = value.strip()

        manifest = f"id:{data_id};request-id:{x_request_id};ts:{ts};"

        if not v1 or not MP_WEBHOOK_SECRET:
            print("⚠️ ASSINATURA OU SECRET AUSENTE.")
            return PlainTextResponse("Invalid signature", status_code=401)

        signature = hmac.new(
            MP_WEBHOOK_SECRET.encode(),
            manifest.encode(),
            hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(signature, v1):
            print("⚠️ ASSINATURA INVÁLIDA.")
            return PlainTextResponse("Invalid signature", status_code=401)

        if not order_id:
            return PlainTextResponse("OK")

        print(f"ORDER RECEBIDA: {order_id}")

        # Consultar Pedido com HTTP Asynchronous Client
        headers = {"Authorization": f"Bearer {MP_TOKEN}"}
        
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"https://api.mercadopago.com/v1/orders/{order_id}",
                headers=headers,
                timeout=20.0,
            )

        print("CONSULTA ORDER:", response.status_code)
        response.raise_for_status()

        order = response.json()
        status = order.get("status")
        external_reference = order.get("external_reference")

        print(f"STATUS: {status} | REFERÊNCIA: {external_reference}")

        if status == "processed":
            if not external_reference or not external_reference.startswith("vip_"):
                print("⚠️ REFERÊNCIA INVÁLIDA.")
                return PlainTextResponse("OK")

            telegram_user_id = int(external_reference.replace("vip_", ""))

            pagamento = (
                supabase.table("payments")
                .select("id, status, invite_enviado, client_id, vip_group_id, product_id")
                .eq("order_id", order_id)
                .limit(1)
                .execute()
            )

            if not pagamento.data:
                print("⚠️ PAGAMENTO NÃO ENCONTRADO NO SUPABASE.")
                return PlainTextResponse("OK")

            pagamento_atual = pagamento.data[0]

            if pagamento_atual.get("invite_enviado"):
                print("⚠️ PAGAMENTO E CONVITE JÁ PROCESSADOS.")
                return PlainTextResponse("OK")

            produto = (
                supabase.table("products")
                .select("duration_days")
                .eq("id", pagamento_atual["product_id"])
                .single()
                .execute()
            )

            if not produto.data:
                print("⚠️ PRODUTO NÃO ENCONTRADO.")
                return PlainTextResponse("OK")

            dias_acesso = produto.data["duration_days"]
            data_expiracao = datetime.now(timezone.utc) + timedelta(days=dias_acesso)

            supabase.table("payments").update({
                "status": "approved",
                "data_expiracao": data_expiracao.isoformat()
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
                "expires_at": data_expiracao.isoformat()
            }).execute()

            vip_group = (
                supabase.table("vip_groups")
                .select("chat_id")
                .eq("id", pagamento_atual["vip_group_id"])
                .single()
                .execute()
            )

            if not vip_group.data:
                print("⚠️ GRUPO VIP NÃO ENCONTRADO.")
                return PlainTextResponse("OK")

            invite = await telegram_app.bot.create_chat_invite_link(
                chat_id=vip_group.data["chat_id"],
                member_limit=1,
            )

            await telegram_app.bot.send_message(
                chat_id=telegram_user_id,
                text=(
                    "✅ Pagamento aprovado!\n\n"
                    "🎉 Seu acesso VIP está liberado!\n\n"
                    f"👇 Clique abaixo para entrar no grupo:\n{invite.invite_link}"
                ),
            )

            supabase.table("payments").update({
                "invite_enviado": True
            }).eq("order_id", order_id).execute()

            print("🚀 ACESSO VIP ENVIADO!")

        elif status in ["failed", "refunded", "expired"]:
            print(f"STATUS DE PAGAMENTO ATUALIZADO: {status}")
            supabase.table("payments").update({
                "status": status
            }).eq("order_id", order_id).execute()

        return PlainTextResponse("OK")

    except Exception as e:
        print(f"ERRO WEBHOOK MERCADO PAGO: {type(e).__name__}: {e}")
        return PlainTextResponse("Erro", status_code=500)
