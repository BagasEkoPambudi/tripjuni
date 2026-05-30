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
from config import TELEGRAM_TOKEN, GEMINI_API_KEY, SHEET_ID, ANGGOTA_TRIP

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(format="%(asctime)s │ %(levelname)s │ %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ── States ───────────────────────────────────────────────────────────────────
PILIH_PEMESAN, KONFIRMASI         = range(2)
M_TOKO, M_ITEM, M_PAJAK, M_LANJUT = range(10, 14)

# ── Init ─────────────────────────────────────────────────────────────────────
genai.configure(api_key=GEMINI_API_KEY)
gemini = genai.GenerativeModel("gemini-2.5-flash")
SCOPES = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

def get_sheet():
    creds = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
    return gspread.authorize(creds).open_by_key(SHEET_ID).sheet1


# ════════════════════════════════════════════════════════════════════════════
#  SCAN STRUK
# ════════════════════════════════════════════════════════════════════════════

def scan_struk_dengan_gemini(image_bytes: bytes) -> dict:
    import PIL.Image, io
    img = PIL.Image.open(io.BytesIO(image_bytes))
    prompt = """Kamu adalah scanner struk belanja. Analisis gambar struk ini dan kembalikan HANYA JSON valid (tanpa markdown/backtick).

Format JSON:
{
  "nama_toko": "string",
  "tanggal": "DD/MM/YYYY atau null",
  "items": [{"nama": "string", "qty": number, "harga_satuan": number, "subtotal": number}],
  "subtotal": number,
  "pajak_persen": number,
  "pajak_nominal": number,
  "diskon": number,
  "total": number,
  "mata_uang": "IDR"
}

Aturan:
- Semua harga angka bulat tanpa titik/koma
- Tidak ada pajak → pajak_persen=0, pajak_nominal=0
- Tidak ada diskon → diskon=0
- total = subtotal + pajak_nominal - diskon"""
    response = gemini.generate_content([prompt, img])
    raw = re.sub(r"```(?:json)?", "", response.text.strip()).strip("`").strip()
    return json.loads(raw)


# ════════════════════════════════════════════════════════════════════════════
#  HITUNG PEMBAGIAN — PAJAK PROPORSIONAL
# ════════════════════════════════════════════════════════════════════════════

def hitung_pembagian(data_struk: dict, pemesan: dict) -> dict:
    """
    Logika pembagian:
    - Item dibagi rata ke semua yang memilih item tersebut
      contoh: Cap Jay 3 porsi Rp 51.000 dipilih 3 orang → masing-masing Rp 17.000
    - Pajak PROPORSIONAL sesuai subtotal masing-masing
    - Yang tidak memesan tidak kena pajak
    """
    items         = data_struk["items"]
    pajak_nominal = data_struk["pajak_nominal"]
    diskon_total  = data_struk["diskon"]
    total_struk   = data_struk["total"]

    # Hitung berapa orang yang memilih tiap item
    item_count = {}
    for pesanan in pemesan.values():
        for p in pesanan:
            key = p.lower()
            item_count[key] = item_count.get(key, 0) + 1

    # Map nama item → subtotal
    item_subtotal_map = {it["nama"].lower(): it["subtotal"] for it in items}

    # Subtotal per orang: setiap item dibagi rata ke pemilihnya
    subtotal_per_orang = {}
    for nama, pesanan in pemesan.items():
        total = 0
        for p in pesanan:
            key   = p.lower()
            sub   = item_subtotal_map.get(key, 0)
            count = item_count.get(key, 1)
            total += round(sub / count)
        subtotal_per_orang[nama] = total

    grand_subtotal = sum(subtotal_per_orang.values())

    hasil = {}
    for nama, pesanan in pemesan.items():
        sub = subtotal_per_orang[nama]
        pajak_bagian  = round(sub / grand_subtotal * pajak_nominal) if grand_subtotal > 0 else 0
        diskon_bagian = round(sub / grand_subtotal * diskon_total)  if grand_subtotal > 0 else 0
        hasil[nama] = {
            "pesanan"      : pesanan,
            "subtotal"     : sub,
            "pajak_bagian" : pajak_bagian,
            "diskon_bagian": diskon_bagian,
            "total"        : max(0, sub + pajak_bagian - diskon_bagian),
        }

    # Koreksi selisih pembulatan → ke orang dengan subtotal terbesar
    total_dihitung = sum(v["total"] for v in hasil.values())
    selisih = total_struk - total_dihitung
    if selisih != 0 and hasil:
        nama_terbesar = max(hasil, key=lambda n: hasil[n]["subtotal"])
        hasil[nama_terbesar]["total"] += selisih

    return hasil



# ════════════════════════════════════════════════════════════════════════════
#  GOOGLE SHEETS
# ════════════════════════════════════════════════════════════════════════════

def inisialisasi_header(sheet):
    if not sheet.get_all_values() or sheet.cell(1, 1).value != "No":
        header = ["No", "Tanggal", "Toko", "Sumber", "Total Struk", "Pajak", "Diskon"] \
                 + [f"💰 {n}" for n in ANGGOTA_TRIP] + ["Item Detail"]
        sheet.insert_row(header, 1)

def simpan_ke_sheets(data_struk: dict, pembagian: dict, no_tx: int):
    sheet = get_sheet()
    inisialisasi_header(sheet)

    tanggal = data_struk.get("tanggal") or datetime.now().strftime("%d/%m/%Y")
    sumber  = "📸 Struk" if data_struk.get("sumber") == "foto" else "✏️ Manual"

    kolom_anggota = [
        f"Rp {pembagian[n]['total']:,}".replace(",", ".") if n in pembagian else "—"
        for n in ANGGOTA_TRIP
    ]
    item_detail = " | ".join(
        f"{it['nama']} x{it['qty']} @{it['harga_satuan']:,}".replace(",", ".")
        for it in data_struk["items"]
    )

    sheet.append_row([
        no_tx,
        tanggal,
        data_struk.get("nama_toko", "—"),
        sumber,
        f"Rp {data_struk['total']:,}".replace(",", "."),
        f"Rp {data_struk['pajak_nominal']:,}".replace(",", "."),
        f"Rp {data_struk['diskon']:,}".replace(",", "."),
        *kolom_anggota,
        item_detail,
    ], value_input_option="USER_ENTERED")

def ambil_no_tx() -> int:
    try:
        vals  = get_sheet().col_values(1)
        angka = [int(v) for v in vals[1:] if str(v).isdigit()]
        return max(angka) + 1 if angka else 1
    except Exception:
        return 1


# ════════════════════════════════════════════════════════════════════════════
#  HELPERS UI
# ════════════════════════════════════════════════════════════════════════════

def buat_keyboard_item(nama: str, items: list, dipilih: list) -> InlineKeyboardMarkup:
    keyboard = []
    for it in items:
        cek   = "✅" if it["nama"] in dipilih else "☐"
        label = f"{cek} {it['nama']} — Rp {it['harga_satuan']:,}".replace(",", ".")
        keyboard.append([InlineKeyboardButton(label, callback_data=f"item|{nama}|{it['nama']}")])
    keyboard.append([
        InlineKeyboardButton("⏭ Tidak menanggung", callback_data=f"skip|{nama}"),
        InlineKeyboardButton("✅ Selesai",          callback_data=f"done|{nama}"),
    ])
    return InlineKeyboardMarkup(keyboard)

def format_ringkasan(data_struk: dict) -> str:
    items_teks = "\n".join(
        f"  • {it['nama']} x{it['qty']} — Rp {it['subtotal']:,}".replace(",", ".")
        for it in data_struk["items"]
    )
    teks = (
        f"🏪 *{data_struk.get('nama_toko','—')}*\n"
        f"📅 {data_struk.get('tanggal','—')}\n\n"
        f"📋 *Item:*\n{items_teks}\n\n"
        f"💵 Subtotal : Rp {data_struk['subtotal']:,}\n".replace(",",".")
    )
    if data_struk["pajak_nominal"] > 0:
        teks += f"🧾 Pajak ({data_struk['pajak_persen']}%) : Rp {data_struk['pajak_nominal']:,}\n".replace(",",".")
    if data_struk["diskon"] > 0:
        teks += f"🎁 Diskon  : -Rp {data_struk['diskon']:,}\n".replace(",",".")
    teks += f"💰 *Total  : Rp {data_struk['total']:,}*".replace(",",".")
    return teks


# ════════════════════════════════════════════════════════════════════════════
#  COMMAND UMUM
# ════════════════════════════════════════════════════════════════════════════

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Halo! Bot pencatat pengeluaran trip.*\n\n"
        "📸 *Scan struk foto* → kirim foto langsung\n"
        "✏️ *Input manual*    → ketik /manual\n"
        "📊 *Rekap pengeluaran* → ketik /rekap\n"
        "❌ *Batalkan proses*  → ketik /batal\n\n"
        f"👥 Anggota: {', '.join(ANGGOTA_TRIP)}\n\n"
        "💡 _Pajak dihitung proporsional sesuai porsi belanja masing-masing._",
        parse_mode="Markdown"
    )

async def rekap(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        data   = get_sheet().get_all_values()
        if len(data) <= 1:
            await update.message.reply_text("📭 Belum ada transaksi tercatat.")
            return
        header, baris = data[0], data[1:]

        total_per_orang = {n: 0 for n in ANGGOTA_TRIP}
        for row in baris:
            for nama in ANGGOTA_TRIP:
                kolom = f"💰 {nama}"
                if kolom in header:
                    idx = header.index(kolom)
                    if idx < len(row):
                        val = row[idx].replace("Rp ", "").replace(".", "").strip()
                        if val.isdigit():
                            total_per_orang[nama] += int(val)

        teks  = "📊 *REKAP PENGELUARAN TRIP*\n" + "─" * 30 + "\n"
        teks += "\n".join(
            f"  {'💰' if t > 0 else '➖'} {n}: *Rp {t:,}*".replace(",",".")
            for n, t in total_per_orang.items()
        )
        grand = sum(total_per_orang.values())
        teks += "\n" + "─" * 30
        teks += f"\n💵 *TOTAL: Rp {grand:,}*".replace(",",".")
        teks += f"\n📋 Transaksi tercatat: {len(baris)}"

        await update.message.reply_text(teks, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error rekap: {e}")
        await update.message.reply_text("❌ Gagal ambil data. Cek koneksi Sheets.")

async def batal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text("❌ Dibatalkan. Kirim foto atau /manual untuk mulai lagi.")
    return ConversationHandler.END


# ════════════════════════════════════════════════════════════════════════════
#  FLOW — SCAN FOTO
# ════════════════════════════════════════════════════════════════════════════

async def terima_foto(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ Membaca struk... mohon tunggu.")
    try:
        foto  = update.message.photo[-1]
        file  = await foto.get_file()
        img_b = await file.download_as_bytearray()

        data_struk = scan_struk_dengan_gemini(bytes(img_b))
        data_struk["sumber"] = "foto"
        ctx.user_data.update({"data_struk": data_struk, "pemesan": {}, "giliran_idx": 0})

        await msg.edit_text("✅ *Struk berhasil dibaca!*\n\n" + format_ringkasan(data_struk), parse_mode="Markdown")
        await tanya_pemesan(update, ctx)
        return PILIH_PEMESAN

    except json.JSONDecodeError:
        await msg.edit_text("⚠️ Gagal baca format struk. Pastikan foto jelas lalu kirim ulang.")
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Error scan: {e}")
        await msg.edit_text("❌ Terjadi kesalahan saat memproses. Coba lagi.")
        return ConversationHandler.END


# ════════════════════════════════════════════════════════════════════════════
#  FLOW — INPUT MANUAL
# ════════════════════════════════════════════════════════════════════════════

async def manual_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    ctx.user_data["manual_items"] = []
    await update.message.reply_text(
        "✏️ *Input Manual Pengeluaran*\n\n"
        "Langkah 1️⃣ — Ketik *nama toko / tempat:*\n"
        "_(contoh: Warung Bu Sari, Parkir, Bensin SPBU)_",
        parse_mode="Markdown"
    )
    return M_TOKO

async def manual_toko(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["manual_toko"] = update.message.text.strip()
    await update.message.reply_text(
        f"🏪 Toko: *{ctx.user_data['manual_toko']}*\n\n"
        "Langkah 2️⃣ — Ketik item dengan format:\n"
        "`nama item, jumlah, harga satuan`\n\n"
        "Contoh:\n"
        "`Nasi Goreng, 2, 25000`\n"
        "`Es Teh, 3, 5000`\n"
        "`Tiket masuk, 1, 15000`\n\n"
        "_Untuk item qty=1 boleh tulis: `Parkir, 1, 10000`_",
        parse_mode="Markdown"
    )
    return M_ITEM

async def manual_item(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    teks = update.message.text.strip()
    try:
        bagian = [b.strip() for b in teks.split(",")]
        if len(bagian) < 2:
            raise ValueError
        nama_item = bagian[0]
        if len(bagian) == 2:
            # format: nama, harga (qty=1)
            qty   = 1
            harga = int(bagian[1].replace(".", "").replace(",", ""))
        else:
            # format: nama, qty, harga
            qty   = int(bagian[1])
            harga = int(bagian[2].replace(".", "").replace(",", ""))
        if harga <= 0 or qty <= 0:
            raise ValueError
    except (ValueError, IndexError):
        await update.message.reply_text(
            "⚠️ Format salah. Gunakan:\n`nama item, qty, harga satuan`\n\n"
            "Contoh: `Nasi Goreng, 2, 25000`\n"
            "Atau: `Parkir, 1, 10000`",
            parse_mode="Markdown"
        )
        return M_ITEM

    ctx.user_data["manual_items"].append({
        "nama"        : nama_item,
        "qty"         : qty,
        "harga_satuan": harga,
        "subtotal"    : qty * harga,
    })

    items    = ctx.user_data["manual_items"]
    subtotal = sum(it["subtotal"] for it in items)
    daftar   = "\n".join(
        f"  {i+1}. {it['nama']} x{it['qty']} = Rp {it['subtotal']:,}".replace(",",".")
        for i, it in enumerate(items)
    )

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("➕ Tambah item", callback_data="m_tambah"),
        InlineKeyboardButton("✅ Lanjut",      callback_data="m_lanjut"),
    ]])
    await update.message.reply_text(
        f"✅ Item ditambahkan!\n\n📋 *Daftar item:*\n{daftar}\n\n"
        f"💵 Subtotal: *Rp {subtotal:,}*\n\n".replace(",",".") +
        "Tambah item lagi atau lanjut ke pajak?",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    return M_LANJUT

async def manual_lanjut_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "m_tambah":
        await query.edit_message_text(
            "Ketik item berikutnya:\n`nama item, qty, harga satuan`\n\n"
            "Contoh: `Es Jeruk, 2, 8000`",
            parse_mode="Markdown"
        )
        return M_ITEM

    # m_lanjut → tanya pajak
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("Tidak ada",    callback_data="pjk_0"),
        InlineKeyboardButton("PPN 11%",      callback_data="pjk_11"),
    ],[
        InlineKeyboardButton("Service 10%",  callback_data="pjk_10"),
        InlineKeyboardButton("Input sendiri %", callback_data="pjk_custom"),
    ]])
    await query.edit_message_text(
        "Langkah 3️⃣ — Ada pajak atau service charge?",
        reply_markup=keyboard
    )
    return M_PAJAK

async def manual_pajak_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "pjk_custom":
        await query.edit_message_text(
            "Ketik besaran pajak/service dalam persen:\n_(contoh: `15` untuk 15%)_",
            parse_mode="Markdown"
        )
        return M_PAJAK

    persen_map = {"pjk_0": 0, "pjk_10": 10, "pjk_11": 11}
    persen = persen_map.get(query.data, 0)
    await _finalisasi_manual(query.message, ctx, persen, edit_msg=query)
    return PILIH_PEMESAN

async def manual_pajak_teks(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    teks = update.message.text.strip().replace("%", "")
    try:
        persen = float(teks)
        if not (0 <= persen <= 100):
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "⚠️ Format salah. Ketik angka saja, contoh: `15`",
            parse_mode="Markdown"
        )
        return M_PAJAK
    await _finalisasi_manual(update.message, ctx, persen, edit_msg=None)
    return PILIH_PEMESAN

async def _finalisasi_manual(message, ctx, persen: float, edit_msg=None):
    """Susun data_struk dari input manual dan mulai sesi pilih pemesan."""
    items    = ctx.user_data["manual_items"]
    subtotal = sum(it["subtotal"] for it in items)
    pajak    = round(subtotal * persen / 100)
    total    = subtotal + pajak

    data_struk = {
        "sumber"       : "manual",
        "nama_toko"    : ctx.user_data.get("manual_toko", "Manual"),
        "tanggal"      : datetime.now().strftime("%d/%m/%Y"),
        "items"        : items,
        "subtotal"     : subtotal,
        "pajak_persen" : persen,
        "pajak_nominal": pajak,
        "diskon"       : 0,
        "total"        : total,
        "mata_uang"    : "IDR",
    }
    ctx.user_data.update({"data_struk": data_struk, "pemesan": {}, "giliran_idx": 0})

    ringkasan = "✅ *Input manual siap!*\n\n" + format_ringkasan(data_struk)
    if edit_msg:
        await edit_msg.edit_message_text(ringkasan, parse_mode="Markdown")
        await message.reply_text("👇 Sekarang tentukan siapa yang menanggung item apa:")
    else:
        await message.reply_text(ringkasan, parse_mode="Markdown")

    # Kirim pertanyaan pemesan pertama
    idx  = 0
    nama = ANGGOTA_TRIP[idx]
    kb   = buat_keyboard_item(nama, items, [])
    await message.reply_text(
        f"🙋 *{nama}* menanggung item apa saja?\n_(Tap item, lalu ✅ Selesai)_",
        reply_markup=kb,
        parse_mode="Markdown"
    )


# ════════════════════════════════════════════════════════════════════════════
#  FLOW — PILIH PEMESAN & KONFIRMASI (shared foto + manual)
# ════════════════════════════════════════════════════════════════════════════

async def tanya_pemesan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    idx   = ctx.user_data["giliran_idx"]
    nama  = ANGGOTA_TRIP[idx]
    items = ctx.user_data["data_struk"]["items"]
    dipilih = ctx.user_data.get(f"pilih_{nama}", [])
    kb    = buat_keyboard_item(nama, items, dipilih)
    teks  = f"🙋 *{nama}* pesan apa saja?\n_(Tap item yang dipesan, lalu ✅ Selesai)_"

    if update.callback_query:
        await update.callback_query.edit_message_text(teks, reply_markup=kb, parse_mode="Markdown")
    else:
        await update.message.reply_text(teks, reply_markup=kb, parse_mode="Markdown")

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    await query.answer()
    bagian = query.data.split("|")
    aksi   = bagian[0]
    nama   = bagian[1] if len(bagian) > 1 else ""

    if aksi == "item":
        item_nama = bagian[2]
        dipilih   = ctx.user_data.setdefault(f"pilih_{nama}", [])
        if item_nama in dipilih:
            dipilih.remove(item_nama)
        else:
            dipilih.append(item_nama)
        await tanya_pemesan(update, ctx)
        return PILIH_PEMESAN

    elif aksi in ("done", "skip"):
        dipilih = ctx.user_data.get(f"pilih_{nama}", [])
        if dipilih:
            ctx.user_data["pemesan"][nama] = dipilih

        idx = ctx.user_data["giliran_idx"] + 1
        ctx.user_data["giliran_idx"] = idx

        if idx < len(ANGGOTA_TRIP):
            ctx.user_data.pop(f"pilih_{ANGGOTA_TRIP[idx]}", None)
            await tanya_pemesan(update, ctx)
            return PILIH_PEMESAN
        else:
            return await tampilkan_konfirmasi(update, ctx)

    elif aksi == "konfirmasi_ya":
        return await simpan_transaksi(update, ctx)

    elif aksi == "konfirmasi_ulang":
        ctx.user_data["pemesan"]     = {}
        ctx.user_data["giliran_idx"] = 0
        await query.edit_message_text("🔄 Mengulang pemilihan...")
        await tanya_pemesan(update, ctx)
        return PILIH_PEMESAN

    return PILIH_PEMESAN

async def tampilkan_konfirmasi(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data_struk = ctx.user_data["data_struk"]
    pemesan    = ctx.user_data["pemesan"]

    if not pemesan:
        await update.callback_query.edit_message_text(
            "⚠️ Tidak ada yang memilih item. Transaksi dibatalkan."
        )
        return ConversationHandler.END

    pembagian = hitung_pembagian(data_struk, pemesan)
    ctx.user_data["pembagian"] = pembagian

    teks = "💳 *RINGKASAN PEMBAGIAN BIAYA*\n" + "─" * 30 + "\n"
    for nama in ANGGOTA_TRIP:
        if nama in pembagian:
            p = pembagian[nama]
            baris_item = ", ".join(p["pesanan"])
            teks += f"👤 *{nama}*\n"
            teks += f"   🍽 {baris_item}\n"
            teks += f"   💵 Item    : Rp {p['subtotal']:,}\n".replace(",",".")
            if p["pajak_bagian"] > 0:
                teks += f"   🧾 Pajak   : Rp {p['pajak_bagian']:,}\n".replace(",",".")
            if p["diskon_bagian"] > 0:
                teks += f"   🎁 Diskon  : -Rp {p['diskon_bagian']:,}\n".replace(",",".")
            teks += f"   💰 *Total  : Rp {p['total']:,}*\n\n".replace(",",".")
        else:
            teks += f"👤 *{nama}* — tidak memesan\n\n"

    teks += "─" * 30
    teks += f"\n💵 Total struk: *Rp {data_struk['total']:,}*".replace(",",".")
    if data_struk["pajak_nominal"] > 0:
        teks += f"\n🧾 Pajak proporsional per porsi belanja"

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Simpan ke Sheets", callback_data="konfirmasi_ya"),
        InlineKeyboardButton("🔄 Ulangi",           callback_data="konfirmasi_ulang"),
    ]])
    await update.callback_query.edit_message_text(teks, reply_markup=kb, parse_mode="Markdown")
    return KONFIRMASI

async def simpan_transaksi(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.edit_message_text("💾 Menyimpan ke Google Sheets...")
    try:
        no_tx     = ambil_no_tx()
        simpan_ke_sheets(ctx.user_data["data_struk"], ctx.user_data["pembagian"], no_tx)
        await update.callback_query.edit_message_text(
            f"✅ *Tersimpan!* (No. {no_tx})\n\n"
            f"📊 /rekap → lihat total semua orang\n"
            f"📸 Kirim foto atau /manual → transaksi baru",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Error simpan: {e}")
        await update.callback_query.edit_message_text(
            "❌ Gagal menyimpan. Cek koneksi dan credentials.json."
        )
    ctx.user_data.clear()
    return ConversationHandler.END


# ════════════════════════════════════════════════════════════════════════════
#  MAIN
# ════════════════════════════════════════════════════════════════════════════

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Handler scan foto
    conv_foto = ConversationHandler(
        entry_points=[MessageHandler(filters.PHOTO, terima_foto)],
        states={
            PILIH_PEMESAN: [CallbackQueryHandler(handle_callback)],
            KONFIRMASI:    [CallbackQueryHandler(handle_callback)],
        },
        fallbacks=[CommandHandler("batal", batal)],
        per_user=True,
    )

    # Handler input manual
    conv_manual = ConversationHandler(
        entry_points=[CommandHandler("manual", manual_start)],
        states={
            M_TOKO:  [MessageHandler(filters.TEXT & ~filters.COMMAND, manual_toko)],
            M_ITEM:  [MessageHandler(filters.TEXT & ~filters.COMMAND, manual_item)],
            M_LANJUT:[CallbackQueryHandler(manual_lanjut_callback, pattern="^m_")],
            M_PAJAK: [
                CallbackQueryHandler(manual_pajak_callback, pattern="^pjk_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, manual_pajak_teks),
            ],
            PILIH_PEMESAN: [CallbackQueryHandler(handle_callback)],
            KONFIRMASI:    [CallbackQueryHandler(handle_callback)],
        },
        fallbacks=[CommandHandler("batal", batal)],
        per_user=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("rekap", rekap))
    app.add_handler(conv_foto)
    app.add_handler(conv_manual)

    logger.info("Bot berjalan... tekan Ctrl+C untuk berhenti.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
