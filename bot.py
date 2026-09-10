from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes

TOKEN = "8696224741:AAGICbqfieY16ni72rdkanSmhMspwzIsHFg"


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


app = Application.builder().token(TOKEN).build()

app.add_handler(CommandHandler("start", start))

print("Bot iniciado!")

app.run_polling()