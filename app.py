import os
import asyncio
from datetime import datetime, timedelta, timezone
import mercadopago
from supabase import create_client
import hashlib
import hmac

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

app = FastAPI()

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
Application,
CommandHandler,
CallbackQueryHandler,
)

mp_token = os.getenv("MERCADOPAGO_ACCESS_TOKEN")

if not mp_token:
    raise RuntimeError("MERCADOPAGO_ACCESS_TOKEN não configurado.")

mp = mercadopago.SDK(mp_token)

telegram_app = None
_initialized = False
_init_lock = asyncio.Lock()

def get_supabase():
    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_SECRET_KEY")

    if not supabase_url or not supabase_key:
        raise RuntimeError("Supabase não configurado no Vercel.")

    return create_client(
        supabase_url,
        supabase_key
    )

async def registrar_bot(client_id):
    supabase = get_supabase()

    telegram = await get_telegram_app()

    bot_info = await telegram.bot.get_me()

    print(
        f"🤖 BOT IDENTIFICADO: "
        f"{bot_info.id} | @{bot_info.username}"
    )

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
            "bot_token": os.getenv("BOT_TOKEN"),
            "status": "active"
        })
        .execute()
    )

    print("🤖 BOT REGISTRADO NO SUPABASE.")

    return resultado.data[0]["id"]

async def verificar_remarketing():
    supabase = get_supabase()
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
        
    telegram = await get_telegram_app()
    for pagamento in pagamentos.data:
        try:
            criado_em = datetime.fromisoformat(
                pagamento["created_at"].replace("Z", "+00:00")
            )
            minutos_passados = (
                agora - criado_em
            ).total_seconds() / 60

            if minutos_passados < 5:
                continue

            telegram_user_id = pagamento["telegram_user_id"]
            payment_url = pagamento.get("payment_url")

            if not payment_url:
                print(
                    f"⚠️ PAGAMENTO SEM LINK: "
                    f"{pagamento['id']}"
                )
                continue

            await telegram.send_message(
                chat_id=telegram_user_id,
                text=(
                    "⌛ Seu pagamento ainda não foi concluído.\n"
                    "Seu acesso VIP está esperando por você!\n"
                    "👇 Se ainda quiser entrar, "
                    "finalize o pagamento abaixo:"
                ),
                reply_markup=InlineKeyboardMarkup(
                    botoes_remarketing
                ),
            )

            supabase.table("payments").update({
                "remarketing_enviado": True
            }).eq(
                "id",
                pagamento["id"]
            ).execute()

            print(
                f"📲 REMARKETING ENVIADO: "
                f"{telegram_user_id}"
            )
        except Exception as erro:
            print(
                f"❌ ERRO NO REMARKETING: "
                f"{pagamento.get('id')}: {erro}"
            )

async def registrar_acesso(telegram_user_id, payment_id):

    supabase = get_supabase()

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

agora = datetime.now(timezone.utc)

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
      
        supabase.table("access_control").update({
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


    print(f"🆕 ACESSO CRIADO: {telegram_user_id}")

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


async def get_telegram_app():

    global telegram_app, _initialized

    if telegram_app is None:
        token = os.getenv("BOT_TOKEN")

        if not token:
            raise RuntimeError(
                "BOT_TOKEN não configurado no Vercel."
            )

    telegram_app = (
        Application.builder()
        .token(token)
        .updater(None)
        .build()
    )

    telegram_app.add_handler(
        CommandHandler("start", start)
    )

    telegram_app.add_handler(
        CallbackQueryHandler(botoes)
    )

if not _initialized:
    async with _init_lock:
        if not _initialized:
            await telegram_app.initialize()
            _initialized = True

return telegram_app

async def start(update: Update, context):
    botoes = [
        [
            InlineKeyboardButton(
                "🛒 Comprar",
                callback_data="comprar"
            )
    ],
    [
        InlineKeyboardButton(
            "📋 Ver produtos",
            callback_data="produtos"
        )
    ],
    [
        InlineKeyboardButton(
            "❓ Suporte",
            callback_data="suporte"
        )
    ],
]

await update.message.reply_text(
    "🤖 Olá! Bem-vindo!\n\nEscolha uma opção!👇",
    reply_markup=InlineKeyboardMarkup(botoes),
)

async def botoes(update: Update, context):
    query = update.callback_query
    await query.answer()

    if query.data in ["comprar", "renovar"]:
        try:
            supabase = get_supabase()

            bot_username = (
                await context.bot.get_me()
            ).username

            bot_data = (
                supabase.table("telegram_bots")
                .select("id, client_id")
                .eq("username", bot_username)
                .eq("status", "active")
                .limit(1)
                .execute()
            )

            if not bot_data.data:
                raise RuntimeError(
                    "Bot Telegram não cadastrado."
                )

            bot_config = bot_data.data[0]
            client_id = bot_config["client_id"]
           
            conexao = (
                supabase.table("payment_connections")
                .select("id")
                .eq("client_id", client_id)
                .eq("status", "active")
                .limit(1)
                .execute()
            )

            if not conexao.data:
                raise RuntimeError(
                    "Nenhuma conexão de pagamento ativa encontrada."
                )

            payment_connection_id = conexao.data[0]["id"]

            produto = (
                supabase.table("products")
                .select("*")
                .eq("client_id", client_id)
                .eq("status", "active")
                .limit(1)
                .execute()
            )
            
            if not produto.data:
                raise RuntimeError(
                    "Nenhum produto ativo encontrado."
                )

            produto = produto.data[0]

            produto_id = produto["id"]
            preco = float(produto["price"])
            dias_acesso = produto["duration_days"]
            vip_group_id = produto["vip_group_id"]

            import requests
            import uuid

            access_token = os.getenv(
                "MERCADOPAGO_ACCESS_TOKEN"
            )
            
            headers = {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "X-Idempotency-Key": str(uuid.uuid4()),
            }
            
            order_data = {
            "type": "online",
            "total_amount": f"{preco:.2f}",
            "external_reference": (
                f"vip_{query.from_user.id}"
            ),
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
                "email": "nilsondeabreu.lp@gmail.com"
            }
        }
            response = requests.post(
                "https://api.mercadopago.com/v1/orders",
                headers=headers,
                json=order_data,
                timeout=20,
            )

            print(
                "MERCADO PAGO:",
                response.status_code
            )

            print(response.text)

            response.raise_for_status()

            order = response.json()

            payment_url = (
                order["transactions"]["payments"][0]
                ["payment_method"]
                ["ticket_url"]
            )
            order_id = order["id"]

            supabase.table("payments").insert({
                "order_id": order_id,
                "telegram_user_id": query.from_user.id,
                "amount": preco,
                "status": "pending",
                "external_reference": (
                    order["external_reference"]
                ),
                "dias_acesso": dias_acesso,
                "data_expiracao": None,
                "payment_url": payment_url,
                "remarketing_enviado": False,
                "client_id": produto["client_id"],
                "product_id": produto_id,
                "vip_group_id": vip_group_id,
                "payment_connection_id": (
                    payment_connection_id
                ),
            }).execute()

                  
            await context.bot.send_message(
                chat_id=query.from_user.id,
                text=(
                    "💰 Pix gerado!\n\n"
                    "👇 Clique abaixo para realizar o pagamento:"
                ),
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "💳 Pagar com Pix",
                            url=payment_url
                        )
                    ]
                ])
            )

        except Exception as erro:
            print(
                f"❌ ERRO AO GERAR PAGAMENTO: {erro}"
            )

    return 

async def verificar_acessos():
    supabase = get_supabase()
    telegram = await get_telegram_app()

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
        telegram_user_id = acesso[
            "telegram_user_id"
        ]

        data_expiracao = datetime.fromisoformat(
            acesso["data_expiracao"]
            .replace("Z", "+00:00")
        )

        segundos_restantes = (
            data_expiracao - agora
        ).total_seconds()

        if segundos_restantes <= 0:

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
                vip_group_id = pagamento.data[0][
                    "vip_group_id"
                ]

                vip_group = (
                    supabase.table("vip_groups")
                    .select("chat_id")
                    .eq("id", vip_group_id)
                    .single()
                    .execute()
                )

                vip_chat_id = vip_group.data[
                    "chat_id"
                ]

                try:
                    await telegram.bot.ban_chat_member(
                        chat_id=vip_chat_id,
                        user_id=telegram_user_id,
                    )

                    await telegram.bot.unban_chat_member(
                        chat_id=vip_chat_id,
                        user_id=telegram_user_id,
                        only_if_banned=True,
                    )

                    print(
                        f"🚪 USUÁRIO REMOVIDO DO VIP: "
                        f"{telegram_user_id}"
                    )

                except Exception as erro_remocao:
                    print(
                        f"⚠️ ERRO AO REMOVER "
                        f"{telegram_user_id}: "
                        f"{erro_remocao}"
                    )

            supabase.table(
                "access_control"
            ).update({
                "status": "expirado",
                "atualizado_em": agora.isoformat()
            }).eq(
                "telegram_user_id",
                telegram_user_id
            ).eq(
                "client_id",
                acesso["client_id"]
            ).execute()

            print(
                f"⛔ ACESSO EXPIRADO: "
                f"{telegram_user_id}"
            )

    except Exception as erro:
        print(
            f"❌ ERRO AO VERIFICAR ACESSO: "
            f"{acesso.get('telegram_user_id')}: "
            f"{erro}"
        )

@app.get("/")
async def home():
    return PlainTextResponse(
        "Bot online!"
    )

@app.get("/registrar-bot")
async def registrar_bot_endpoint(
    request: Request,
    client_id: int
):
    cron_secret = os.getenv("CRON_SECRET")
token = request.query_params.get("token")

if not cron_secret or token != cron_secret:
    return PlainTextResponse(
        "Não autorizado.",
        status_code=401,
    )

try:
    bot_id = await registrar_bot(
        client_id
    )

    return PlainTextResponse(
        f"Bot registrado. ID: {bot_id}"
    )

except Exception as e:
    print(
        f"ERRO AO REGISTRAR BOT: "
        f"{type(e).__name__}: {e}"
    )

    return PlainTextResponse(
        "Erro ao registrar bot.",
        status_code=500,
    )

@app.get("/verificar-acessos")
async def verificar_acessos_endpoint(
    request: Request
):
    cron_secret = os.getenv("CRON_SECRET")
    token = request.query_params.get("token")

    if not cron_secret or token != cron_secret:
        return PlainTextResponse(
            "Não autorizado.",
            status_code=401,
        )

try:
    await verificar_remarketing()
    await verificar_acessos()

    return PlainTextResponse(
        "Verificação executada."
    )

except Exception as e:
    print(
        f"ERRO AO VERIFICAR ACESSOS: "
        f"{type(e).__name__}: {e}"
    )

return PlainTextResponse(
    "Erro na verificação.",
    status_code=500,
)

@app.post("/telegram")
async def telegram_webhook(
    request: Request
):
    try:
        telegram = await get_telegram_app()

        data = await request.json()

        update = Update.de_json(
            data,
            bot=telegram.bot,
        )

        await telegram.process_update(
            update
        )

        return PlainTextResponse(
            "OK"
        )

    except Exception as e:
        print(
            f"ERRO NO WEBHOOK: "
            f"{type(e).__name__}: {e}"
        )

    return PlainTextResponse(
    f"Erro: {type(e).__name__}: {e}",
    status_code=500,
)

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

        print(
            "WEBHOOK MERCADO PAGO:"
        )

        print(data)

        if data.get("type") != "order":
            return PlainTextResponse(
                "OK"
            )
        return PlainTextResponse(
            "OK"
        )

        
        order_data = data.get(
            "data",
            {}
        )

        order_id = order_data.get(
            "id"
        )

        
        data_id = request.query_params.get(
            "data.id"
        )

        ts = None
        v1 = None

        if x_signature:
            for part in x_signature.split(","):
                key, value = part.split(
                    "=",
                    1
                )
            if key == "ts":
                ts = value

            elif key == "v1":
                v1 = value
                           
            
            if key == "ts":
                ts = value

            elif key == "v1":
                v1 = value

                secret = os.getenv(
            "MERCADOPAGO_WEBHOOK_SECRET"
        )

        manifest = (
            f"id:{data_id};"
            f"request-id:{x_request_id};"
            f"ts:{ts};"
        )
        
        if not v1 or not secret:
         return PlainTextResponse(
          "Invalid signature",
            status_code=401
        )
         signature = hmac.new(
            secret.encode(),
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
            
            if not order_id:
                return PlainTextResponse(
              "OK"
        )
                        
                print(
            f"ORDER RECEBIDA: {order_id}"
                )

                access_token = os.getenv(
            "MERCADOPAGO_ACCESS_TOKEN"
        )

        headers = {
            "Authorization": (
                f"Bearer {access_token}"
            )
        }
              
        import requests

        response = requests.get(
            f"https://api.mercadopago.com/v1/orders/{order_id}",
            headers=headers,
            timeout=20,
        )

        print(
            "CONSULTA ORDER:",
            response.status_code
        )

        print(response.text)

        response.raise_for_status()

        order = response.json()

        status = order.get(
            "status"
        )
        
        external_reference = order.get(
        "external_reference"
    )
        
        print(
        f"STATUS: {status}"
    )
        print(
        f"REFERÊNCIA: "
        f"{external_reference}"
    )
        
        if status == "processed":
            
            print(
            "✅ PAGAMENTO APROVADO!"
        )

        if (
            not external_reference
            or not external_reference.startswith("vip_")
        ):
            print(
                "⚠️ REFERÊNCIA INVÁLIDA."
            )

            return PlainTextResponse(
                "OK"
            )

        telegram_user_id = int(
            external_reference.replace(
                "vip_",
                ""
            )
        )

        supabase = get_supabase()

        pagamento = (
            supabase.table("payments")
            .select(
                "id, status, invite_enviado, "
                "client_id, vip_group_id, product_id"
            )
            .eq(
                "order_id",
                order_id
            )
            .limit(1)
            .execute()
        )

        if not pagamento.data:
            print(
                "⚠️ PAGAMENTO NÃO ENCONTRADO "
                "NO SUPABASE."
            )

            return PlainTextResponse(
                "OK"
            )

        pagamento_atual = (
            pagamento.data[0]
        )

        if pagamento_atual.get(
            "invite_enviado"
        ):
            print(
                "⚠️ PAGAMENTO E CONVITE "
                "JÁ PROCESSADOS."
            )

            return PlainTextResponse(
                "OK"
            )

        print(
            "🔄 PROCESSANDO PAGAMENTO APROVADO."
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

        if not produto.data:
            print(
                "⚠️ PRODUTO NÃO ENCONTRADO."
            )

            return PlainTextResponse(
                "OK"
            )

        dias_acesso = produto.data[
            "duration_days"
        ]

        data_expiracao = (
            datetime.now(timezone.utc)
            + timedelta(
                days=dias_acesso
            )
        )

        supabase.table(
            "payments"
        ).update({
            "status": "approved",
            "data_expiracao": (
                data_expiracao.isoformat()
            )
        }).eq(
            "order_id",
            order_id
        ).execute()

        await registrar_acesso(
            telegram_user_id,
            pagamento_atual["id"]
        )

        print(
            f"🎟️ ACESSO REGISTRADO: "
            f"{telegram_user_id}"
        )

        supabase.table(
            "subscriptions"
        ).insert({
            "client_id": (
                pagamento_atual["client_id"]
            ),
            "vip_group_id": (
                pagamento_atual["vip_group_id"]
            ),
            "product_id": (
                pagamento_atual["product_id"]
            ),
            "telegram_user_id": (
                telegram_user_id
            ),
            "payment_id": (
                pagamento_atual["id"]
            ),
            "status": "active",
            "started_at": (
                datetime.now(
                    timezone.utc
                ).isoformat()
            ),
            "expires_at": (
                data_expiracao.isoformat()
            )
        }).execute()

        print(
            f"👤 USUÁRIO TELEGRAM: "
            f"{telegram_user_id}"
        )

        telegram = await get_telegram_app()

        vip_group_id = (
            pagamento_atual[
                "vip_group_id"
            ]
        )

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

        if not vip_group.data:
            print(
                "⚠️ GRUPO VIP NÃO ENCONTRADO."
            )

            return PlainTextResponse(
                "OK"
            )

        vip_chat_id = (
            vip_group.data["chat_id"]
        )

        invite = (
            await telegram.bot.create_chat_invite_link(
                chat_id=vip_chat_id,
                member_limit=1,
            )
        )

        invite_link = (
            invite.invite_link
        )

        print(
            f"🔐 CONVITE GERADO: "
            f"{invite_link}"
        )

        await telegram.bot.send_message(
            chat_id=telegram_user_id,
            text=(
                "✅ Pagamento aprovado!\n\n"
                "🎉 Seu acesso VIP está liberado!\n\n"
                "👇 Clique abaixo para entrar no grupo:\n"
                f"{invite_link}"
            ),
        )

        supabase.table(
            "payments"
        ).update({
            "invite_enviado": True
        }).eq(
            "order_id",
            order_id
        ).execute()

        print(
            "🚀 ACESSO VIP ENVIADO!"
        )

    
        elif status == "failed":

            print(
                "❌ PAGAMENTO FALHOU"
            )

            supabase.table(
                "payments"
            ).update({
                "status": "failed"
            }).eq(
                "order_id",
                order_id
            ).execute()

        elif status == "refunded":

            print(
                "↩️ PAGAMENTO ESTORNADO"
            )

            supabase.table(
                "payments"
            ).update({
                "status": "refunded"
            }).eq(
                "order_id",
                order_id
            ).execute()

        elif status == "expired":

            print(
                "⏰ PAGAMENTO EXPIRADO"
            )

            supabase.table(
                "payments"
            ).update({
                "status": "expired"
            }).eq(
                "order_id",
                order_id
            ).execute()
        return PlainTextResponse(
            "OK"
        )
   
    except Exception as e:

        print(
            "ERRO WEBHOOK MERCADO PAGO: "
            f"{type(e).__name__}: {e}"
        )

        return PlainTextResponse(
            "Erro",
            status_code=500,
        )
