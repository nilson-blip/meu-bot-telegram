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

app = FastAPI()

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
    botoes = [
        [InlineKeyboardButton("🛒 Comprar", callback_data="comprar")],
        [InlineKeyboardButton("📋 Ver produtos", callback_data="produtos")],
        [InlineKeyboardButton("❓ Suporte", callback_data="suporte")],
    ]

    await update.message.reply_text(
        "🤖 Olá! Bem-vindo!\n\nEscolha uma opção:",
        reply_markup=InlineKeyboardMarkup(botoes),
    )


async async def botoes(update: Update, context):
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
                "external_reference": "vip_teste",
                "processing_mode": "manual",
                "capture_mode": "automatic_async",
                "items": [
                    {
                        "external_code": "VIP-TESTE",
                        "title": "VIP Teste",
                        "description": "Produto de teste",
                        "quantity": 1,
                        "unit_price": "1.00",
                    }
                ],
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
            payment_url = order["checkout_url"]

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
            status_code=500,
        )
