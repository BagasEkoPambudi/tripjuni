import os
import json
import logging
import re
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler, filters, ContextTypes
)
import google.generativeai as genai
import gspread
from google.oauth2.service_account import Credentials

# ── ENV CONFIG ───────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
SHEET_ID = os.getenv("SHEET_ID")

ANGGOTA_TRIP = ["Bagas", "A", "B", "C", "D"]  # ubah sesuai kebutuhan

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(format="%(asctime)s │ %(levelname)s │ %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Init ─────────────────────────────────────────────────────────────────────
genai.configure(api_key=GEMINI_API_KEY)
gemini = genai.GenerativeModel("gemini-2.5-flash")

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

def get_sheet():
    creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
    creds_dict = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return gspread.authorize(creds).open_by_key(SHEET_ID).sheet1

# ── Scan Struk ───────────────────────────────────────────────────────────────
def scan_struk_dengan_gemini(image_bytes: bytes) -> dict:
    import PIL.Image, io
    img = PIL.Image.open(io.BytesIO(image_bytes))

    prompt = """Kembalikan JSON struk:
{
  "nama_toko": "string",
  "tanggal": "DD/MM/YYYY",
  "items": [{"nama": "string", "qty": number, "harga_satuan": number, "subtotal": number}],
  "subtotal": number,
  "pajak_persen": number,
  "pajak_nominal": number,
  "diskon": number,
  "total": number
}"""

    response = gemini.generate_content([prompt, img])
    raw = re.sub(r"```(?:json)?", "", response.text.strip()).strip("`").strip()
    return json.loads(raw)

# ── Hitung Pembagian ─────────────────────────────────────────────────────────
def hitung_pembagian(data_struk, pemesan):
    items = data_struk["items"]
    pajak = data_struk["pajak_nominal"]

    item_map = {it["nama"].lower(): it["subtotal"] for it in items}

    item_count = {}
    for p in pemesan.values():
        for i in p:
            item_count[i.lower()] = item_count.get(i.lower(), 0) + 1

    hasil = {}
    subtotal_all = 0

    for nama, pesanan in pemesan.items():
        sub = 0
        for i in pesanan:
            sub += item_map[i.lower()] / item_count[i.lower()]
        hasil[nama] = {"subtotal": int(sub), "pesanan": pesanan}
        subtotal_all += sub

    for nama in hasil:
        sub = hasil[nama]["subtotal"]
        pajak_bagian = int(sub / subtotal_all * pajak) if subtotal_all else 0
        hasil[nama]["total"] = sub + pajak_bagian

    return hasil

# ── Telegram Handler ─────────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot aktif 🚀 kirim foto struk")

async def handle_foto(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ proses...")

    try:
        foto = update.message.photo[-1]
        file = await foto.get_file()
        img = await file.download_as_bytearray()

        data = scan_struk_dengan_gemini(bytes(img))

        teks = f"🏪 {data['nama_toko']}\n💰 Total: {data['total']}"
        await msg.edit_text(teks)

    except Exception as e:
        logger.error(e)
        await msg.edit_text("❌ gagal baca struk")

# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    if not TELEGRAM_TOKEN:
        raise Exception("BOT_TOKEN belum diset di Railway")

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_foto))

    logger.info("Bot jalan...")
    app.run_polling()

if __name__ == "__main__":
    main()
