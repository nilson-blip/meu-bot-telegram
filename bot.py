import os
import asyncio
from http import HTTPStatus

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)


TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PORT = int(os.getenv("PORT", "8000"))


if not TOKEN:
    raise RuntimeError("A variável TOKEN não foi configurada.")

if not WEBHOOK_URL:
    raise RuntimeError("A variável WEBHOOK_URL não foi configurada.")


# =========================
# COMANDO /START
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    botoes = [
        [InlineKeyboardButton("🛒 Comprar", callback_data="comprar")],
        [InlineKeyboardButton("📋 Ver produtos", callback_data="produtos")],
        [InlineKeyboardButton("❓ Suporte", callback_data="suporte")]
    ]

    teclado = InlineKeyboardMarkup(botoes)

    await update.message.reply_text(
        "🤖 Olá! Bem-vindo!\n\n"
        "Escolha uma opção:",
        reply_markup=teclado
    )


# =========================
# BOTÕES
# =========================

async def botoes(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query

    await query.answer()

    if query.data == "comprar":
        await query.message.reply_text(
            "🛒 Área de compra\n\n"
            "Em breve você poderá comprar aqui."
        )

    elif query.data == "produtos":
        await query.message.reply_text(
            "📋 Produtos disponíveis\n\n"
            "Em breve vamos cadastrar os produtos."
        )

    elif query.data == "suporte":
        await query.message.reply_text(
            "❓ Suporte\n\n"
            "Em breve você poderá falar com o suporte."
        )


# =========================
# CONFIGURAÇÃO DO TELEGRAM
# =========================

application = (
    Application.builder()
    .token(TOKEN)
    .updater(None)
    .build()
)

application.add_handler(
    CommandHandler("start", start)
)

application.add_handler(
    CallbackQueryHandler(botoes)
)


# =========================
# RECEBE WEBHOOK DO TELEGRAM
# =========================

async def telegram_webhook(request: Request):

    dados = await request.json()

    update = Update.de_json(
        dados,
        bot=application.bot
    )

    await application.update_queue.put(update)

    return Response(
        status_code=HTTPStatus.OK
    )


# =========================
# TESTE DO SERVIDOR
# =========================

async def healthcheck(request: Request):

    return PlainTextResponse(
        "Bot online!"
    )


# =========================
# SERVIDOR WEB
# =========================

app = Starlette(
    routes=[
        Route(
            "/telegram",
            telegram_webhook,
            methods=["POST"]
        ),

        Route(
            "/health",
            healthcheck,
            methods=["GET"]
        ),
    ]
)


# =========================
# INICIALIZAÇÃO
# =========================

async def main():

    await application.initialize()

    await application.start()

    await application.bot.set_webhook(
        url=f"{WEBHOOK_URL}/telegram",
        allowed_updates=Update.ALL_TYPES
    )

    print("🤖 Bot iniciado com webhook!")
    print(f"Webhook: {WEBHOOK_URL}/telegram")

    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=PORT
    )

    server = uvicorn.Server(config)

    try:
        await server.serve()

    finally:
        await application.stop()
        await application.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
