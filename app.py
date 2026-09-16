import os
import asyncio
import mercadopago

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


async def get_telegram_app():
    global telegram_app, _initialized

    if telegram_app is None:
        token = os.getenv("BOT_TOKEN")

        if not token:
            raise RuntimeError("BOT_TOKEN não configurado no Vercel.")

        telegram_app = (
            Application.builder()
            .token(token)
            .updater(None)
            .build()
        )

        telegram_app.add_handler(CommandHandler("start", start))
        telegram_app.add_handler(CallbackQueryHandler(botoes))

    if not _initialized:
        async with _init_lock:
            if not _initialized:
                await telegram_app.initialize()
                _initialized = True

    return telegram_app


async def start(update: Update, context):
    print("CHAT ID:", update.effective_chat.id)

    await update.message.reply_text(
        f"🆔 ID deste chat: {update.effective_chat.id}"
    )

    botoes = [
        [InlineKeyboardButton("🛒 Comprar", callback_data="comprar")],
        [InlineKeyboardButton("📋 Ver produtos", callback_data="produtos")],
        [InlineKeyboardButton("❓ Suporte", callback_data="suporte")],
    ]

    await update.message.reply_text(
        "🤖 Olá! Bem-vindo!\n\nEscolha uma opção:",
        reply_markup=InlineKeyboardMarkup(botoes),
    )


async def botoes(update: Update, context):
    query = update.callback_query
    await query.answer()

    if query.data == "comprar":
        try:
            import requests
            import uuid

            access_token = os.getenv("MERCADOPAGO_ACCESS_TOKEN")

            headers = {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "X-Idempotency-Key": str(uuid.uuid4()),
            }

            order_data = {
    "type": "online",
    "total_amount": "1.00",
    "external_reference": f"vip_{query.from_user.id}",
    "processing_mode": "automatic",
    "transactions": {
        "payments": [
            {
                "amount": "1.00",
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

            print("MERCADO PAGO:", response.status_code)
            print(response.text)

            response.raise_for_status()

            order = response.json()
            payment_url = order["transactions"]["payments"][0]["payment_method"]["ticket_url"]

            await query.message.reply_text(
                "🛒 VIP Teste\n\n"
                "💰 Valor: R$ 1,00\n\n"
                "👇 Clique abaixo para pagar:\n"
                f"{payment_url}"
            )

        except Exception as e:
            print(f"ERRO MERCADO PAGO: {type(e).__name__}: {e}")

            await query.message.reply_text(
                "❌ Não consegui gerar o pagamento agora."
            )

        return

    respostas = {
        "produtos": "📋 Produtos disponíveis\n\nVIP Teste — R$ 1,00",
        "suporte": "❓ Suporte\n\nEm breve você poderá falar com o suporte.",
    }

    await query.message.reply_text(
        respostas.get(query.data, "Opção inválida.")
    )


@app.get("/")
async def home():
    return PlainTextResponse("Bot online!")


@app.post("/telegram")
async def telegram_webhook(request: Request):
    try:
        telegram = await get_telegram_app()

        data = await request.json()

        update = Update.de_json(
            data,
            bot=telegram.bot,
        )

        await telegram.process_update(update)

        return PlainTextResponse("OK")

    except Exception as e:
        print(f"ERRO NO WEBHOOK: {type(e).__name__}: {e}")

        return PlainTextResponse(
            f"Erro: {type(e).__name__}: {e}",
            status_code=500,)
            
@app.post("/mercadopago")
async def mercadopago_webhook(request: Request):
    try:
        data = await request.json()
        print("UPDATE TELEGRAM:")
        print(data)

        print("WEBHOOK MERCADO PAGO:")
        print(data)

        if data.get("type") != "order":
            return PlainTextResponse("OK")

        order_data = data.get("data", {})
        order_id = order_data.get("id")

        if not order_id:
            return PlainTextResponse("OK")

        print(f"ORDER RECEBIDA: {order_id}")

        # Busca os dados completos da Order no Mercado Pago
        access_token = os.getenv("MERCADOPAGO_ACCESS_TOKEN")

        headers = {
            "Authorization": f"Bearer {access_token}"
        }

        import requests

        response = requests.get(
            f"https://api.mercadopago.com/v1/orders/{order_id}",
            headers=headers,
            timeout=20,
        )

        print("CONSULTA ORDER:", response.status_code)
        print(response.text)

        response.raise_for_status()

        order = response.json()

        status = order.get("status")
        external_reference = order.get("external_reference")

        print(f"STATUS: {status}")
        print(f"REFERÊNCIA: {external_reference}")
        if status == "processed":
            print("✅ PAGAMENTO APROVADO!")

            # Recupera o ID do usuário do Telegram
            if external_reference and external_reference.startswith("vip_"):
                telegram_user_id = int(
                    external_reference.replace("vip_", "")
                )

                print(f"👤 USUÁRIO TELEGRAM: {telegram_user_id}")

                telegram = await get_telegram_app()

                # ID do grupo VIP de teste
                vip_chat_id = -1004400475106

                # Cria convite de uso único
                invite = await telegram.bot.create_chat_invite_link(
                    chat_id=vip_chat_id,
                    member_limit=1,
                )

                invite_link = invite.invite_link

                print(f"🔐 CONVITE GERADO: {invite_link}")

                # Envia o convite para quem pagou
                await telegram.bot.send_message(
                    chat_id=telegram_user_id,
                    text=(
                        "✅ Pagamento aprovado!\n\n"
                        "🎉 Seu acesso VIP está liberado!\n\n"
                        "👇 Clique abaixo para entrar no grupo:\n"
                        f"{invite_link}"
                    ),
                )

                print("🚀 ACESSO VIP ENVIADO!")

            else:
                print("⚠️ REFERÊNCIA NÃO IDENTIFICADA")

        elif status == "failed":
            print("❌ PAGAMENTO FALHOU")

        elif status == "refunded":
            print("↩️ PAGAMENTO ESTORNADO")

        return PlainTextResponse("OK")

    except Exception as e:
        print(f"ERRO WEBHOOK MERCADO PAGO: {type(e).__name__}: {e}")

        return PlainTextResponse(
            "Erro",
            status_code=500,
        )
