import os
from telegram import Bot
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

class NotificadorTelegram:
    def __init__(self):
        self.bot = Bot(token=TELEGRAM_TOKEN) if TELEGRAM_TOKEN else None

    async def enviar_alerta_discrepancia(self, partida: str, catastro_count: int, suministros_count: int, direccion: str):
        """
        Envía una alerta si hay discrepancia entre Catastro y Suministros.
        """
        mensaje = (
            f"⚠️ *Alerta de Discrepancia*\n\n"
            f"📍 *Dirección:* {direccion}\n"
            f"🆔 *Partida:* {partida}\n"
            f"🏠 *Catastro:* {catastro_count} unidad(es)\n"
            f"🔌 *Suministros:* {suministros_count} medidor(es)\n\n"
            f"¿Desea validar estas sub-unidades manualmente?"
        )
        
        print(f"ALERTA LOCAL: {mensaje}") # Fallback por si no hay token

        if self.bot and CHAT_ID:
            try:
                await self.bot.send_message(chat_id=CHAT_ID, text=mensaje, parse_mode="Markdown")
            except Exception as e:
                print(f"Error enviando Telegram: {e}")
