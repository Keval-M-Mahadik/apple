import telebot
from telebot import types
import sqlite3
import requests
import threading
import time
import qrcode
import zipfile
import os
import tempfile
from io import BytesIO
from contextlib import closing
from flask import Flask, jsonify

# ==========================================
# 1. CONFIGURATION
# ==========================================

BOT_TOKEN = '8769920582:AAEiy279d3MmTjrK756dVNdPapDuRNOUgkM'
SUPPORT_USERNAME = '@Tony_M_Unlock'
SUPPORT_DISPLAY = '<a href="https://t.me/Tony_M_Unlock">@Tony_M_Unlock</a>'

API_KEY = 'YOUR_IMEI_API_KEY_HERE'
API_URL = 'https://your-imei-provider.com/api/check'

ADMIN_BOT_TOKEN = '8826918363:AAGpUODrXRn3siNcyP_IymbrSHglzWiSjyc'
ADMIN_IDS = [8613123407]

TRON_WALLET_ADDRESS = 'TAotmbQ2qvXmaHgPDtuL29GzjErduT1y4k'
USDT_CONTRACT = 'TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t'
USDC_CONTRACT = 'TEkxiTehnzSmSe2XqrBj4w32RUN966rdz8'
TRONGRID_API_URL = 'https://api.trongrid.io'

TRONGRID_API_KEYS = [
    '8628b901-103e-49d6-b1b3-ba197dd41fc8',
    'b4cd9a16-0d7e-4381-852f-d4b2381ea932',
    'ecdb360a-83e4-4047-98b9-41b0e8b9b3f9',
]
TRONGRID_API_KEYS = [k for k in TRONGRID_API_KEYS if k and not k.startswith('YOUR_')]

WEBHOOK_HOST = '0.0.0.0'
WEBHOOK_PORT = 5000

bot = telebot.TeleBot(BOT_TOKEN, parse_mode='HTML', threaded=True, num_threads=8)
admin_bot = telebot.TeleBot(ADMIN_BOT_TOKEN, parse_mode='HTML', threaded=True, num_threads=4)

app = Flask(__name__)

# ==========================================
# PENDING SERVICE STATE MANAGEMENT
# ==========================================
_pending_lock = threading.Lock()
_pending_prompts = {}

def set_pending(user_id, service_key):
    with _pending_lock:
        _pending_prompts[user_id] = service_key

def get_pending(user_id):
    with _pending_lock:
        return _pending_prompts.get(user_id)

def clear_pending(user_id):
    with _pending_lock:
        _pending_prompts.pop(user_id, None)

def safe_register_step(chat_id, callback, *args, **kwargs):
    try:
        bot.clear_step_handler_by_chat_id(chat_id)
    except Exception as e:
        print(f"clear_step_handler error: {e}")
    bot.register_next_step_handler_by_chat_id(chat_id, callback, *args, **kwargs)

# ==========================================
# CALLBACK OWNER VERIFICATION
# ==========================================
def verify_callback_owner(call):
    try:
        if call.message is None:
            bot.answer_callback_query(call.id, "⛔ Not allowed.", show_alert=True)
            return False
        if call.message.chat.id != call.from_user.id:
            bot.answer_callback_query(call.id, "⛔ Not allowed.", show_alert=True)
            return False
        return True
    except Exception as e:
        print(f"verify_callback_owner error: {e}")
        return False

# ==========================================
# 2. DATABASE (Optimized)
# ==========================================
DB_PATH = 'apple_bot.db'
_user_cache = {}
_cache_lock = threading.Lock()
_local = threading.local()

def _get_conn():
    conn = getattr(_local, 'conn', None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
        conn.execute('PRAGMA journal_mode=WAL;')
        conn.execute('PRAGMA synchronous=NORMAL;')
        conn.execute('PRAGMA temp_store=MEMORY;')
        conn.execute('PRAGMA cache_size=-128000;')
        conn.execute('PRAGMA mmap_size=268435456;')
        conn.execute('PRAGMA busy_timeout=5000;')
        _local.conn = conn
    return conn

def init_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    cursor = conn.cursor()
    cursor.execute('PRAGMA journal_mode=WAL;')
    cursor.execute('PRAGMA synchronous=NORMAL;')
    cursor.execute('PRAGMA temp_store=MEMORY;')
    cursor.execute('PRAGMA cache_size=-128000;')
    cursor.execute('PRAGMA mmap_size=268435456;')
    cursor.execute('PRAGMA busy_timeout=5000;')

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            balance REAL DEFAULT 0.00,
            is_authorized INTEGER DEFAULT 1,
            language TEXT DEFAULT 'en'
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS deposit_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            currency TEXT NOT NULL,
            amount REAL NOT NULL,
            method TEXT,
            proof TEXT,
            file_id TEXT,
            status TEXT DEFAULT 'pending',
            created_at INTEGER NOT NULL,
            reviewed_by INTEGER,
            reviewed_at INTEGER
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS processed_txids (
            txid TEXT PRIMARY KEY,
            processed_at INTEGER NOT NULL
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS bot_settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    cursor.execute("INSERT OR IGNORE INTO bot_settings (key, value) VALUES ('maintenance', 'off')")

    try:
        cursor.execute('ALTER TABLE users ADD COLUMN language TEXT DEFAULT "en"')
    except sqlite3.OperationalError:
        pass

    cursor.execute('CREATE INDEX IF NOT EXISTS idx_users_balance ON users(balance DESC)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_deposit_user_status ON deposit_requests(user_id, status)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_deposit_status ON deposit_requests(status)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_deposit_created ON deposit_requests(created_at DESC)')

    conn.commit()
    conn.close()

init_db()

def db_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL;')
    conn.execute('PRAGMA synchronous=NORMAL;')
    conn.execute('PRAGMA temp_store=MEMORY;')
    conn.execute('PRAGMA cache_size=-128000;')
    conn.execute('PRAGMA mmap_size=268435456;')
    conn.execute('PRAGMA busy_timeout=5000;')
    return conn

def get_bot_setting(key):
    with closing(db_conn()) as conn:
        row = conn.execute('SELECT value FROM bot_settings WHERE key = ?', (key,)).fetchone()
        return row['value'] if row else None

def set_bot_setting(key, value):
    conn = _get_conn()
    conn.execute('INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)', (key, value))
    conn.commit()

def get_user(user_id):
    with _cache_lock:
        cached = _user_cache.get(user_id)
        if cached is not None:
            return cached
    conn = _get_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT balance, language FROM users WHERE user_id = ?', (user_id,))
    row = cursor.fetchone()
    if row is None:
        cursor.execute('INSERT INTO users (user_id, balance, language) VALUES (?, ?, ?)',
                       (user_id, 0.00, 'en'))
        conn.commit()
        balance, lang = 0.00, 'en'
    else:
        balance, lang = row[0], row[1]
    with _cache_lock:
        _user_cache[user_id] = (balance, lang)
    return balance, lang

def update_balance(user_id, amount):
    conn = _get_conn()
    conn.execute('UPDATE users SET balance = balance + ? WHERE user_id = ?', (amount, user_id))
    conn.commit()
    with _cache_lock:
        _user_cache.pop(user_id, None)

def try_deduct_balance(user_id, amount):
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute('''
        UPDATE users SET balance = balance - ?
        WHERE user_id = ? AND balance >= ?
    ''', (amount, user_id, amount))
    conn.commit()
    if cur.rowcount == 0:
        cur.execute('SELECT balance FROM users WHERE user_id = ?', (user_id,))
        row = cur.fetchone()
        with _cache_lock:
            _user_cache.pop(user_id, None)
        return False, (row[0] if row else 0.0)
    cur.execute('SELECT balance FROM users WHERE user_id = ?', (user_id,))
    new_bal = cur.fetchone()[0]
    with _cache_lock:
        _user_cache.pop(user_id, None)
    return True, new_bal

def set_balance(user_id, amount):
    conn = _get_conn()
    conn.execute('UPDATE users SET balance = ? WHERE user_id = ?', (amount, user_id))
    conn.commit()
    with _cache_lock:
        _user_cache.pop(user_id, None)

def update_user_lang(user_id, lang):
    conn = _get_conn()
    conn.execute('UPDATE users SET language = ? WHERE user_id = ?', (lang, user_id))
    conn.commit()
    with _cache_lock:
        _user_cache.pop(user_id, None)

def get_all_users():
    with closing(db_conn()) as conn:
        return [dict(r) for r in conn.execute('SELECT user_id, balance, language FROM users').fetchall()]

def get_total_users():
    with closing(db_conn()) as conn:
        return conn.execute('SELECT COUNT(*) FROM users').fetchone()[0]

def get_total_balance():
    with closing(db_conn()) as conn:
        return conn.execute('SELECT SUM(balance) FROM users').fetchone()[0] or 0

def get_user_info(user_id):
    with closing(db_conn()) as conn:
        row = conn.execute('SELECT user_id, balance, language, is_authorized FROM users WHERE user_id = ?',
                           (user_id,)).fetchone()
        return dict(row) if row else None

def delete_user(user_id):
    conn = _get_conn()
    conn.execute('DELETE FROM users WHERE user_id = ?', (user_id,))
    conn.commit()
    with _cache_lock:
        _user_cache.pop(user_id, None)

def get_top_users(limit=20):
    with closing(db_conn()) as conn:
        return [dict(r) for r in conn.execute(
            'SELECT user_id, balance FROM users ORDER BY balance DESC LIMIT ?', (limit,)).fetchall()]


def get_pending_deposits_count():
    with closing(db_conn()) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM deposit_requests WHERE status = 'pending'"
        ).fetchone()[0]

def get_deposit_stats():
    with closing(db_conn()) as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*), COALESCE(SUM(amount),0) FROM deposit_requests GROUP BY status"
        ).fetchall()
        return {r[0]: {'count': r[1], 'total': r[2]} for r in rows}

# ==========================================
# DB EXPORT HELPERS
# ==========================================
def export_db_zip():
    """Compressed zip of the SQLite DB using safe online backup."""
    ts = time.strftime('%Y%m%d_%H%M%S')
    zip_name = f'apple_bot_db_{ts}.zip'
    tmp_db = None
    try:
        fd, tmp_db = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        src = sqlite3.connect(DB_PATH, timeout=30)
        dst = sqlite3.connect(tmp_db)
        with dst:
            src.backup(dst)
        src.close()
        dst.close()
        buf = BytesIO()
        with zipfile.ZipFile(buf, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            zf.write(tmp_db, arcname=f'apple_bot_{ts}.db')
        buf.seek(0)
        return zip_name, buf
    finally:
        if tmp_db and os.path.exists(tmp_db):
            try:
                os.remove(tmp_db)
            except OSError:
                pass


def export_db_raw():
    """Safe online backup returned as a RAW .db file (self-contained)."""
    ts = time.strftime('%Y%m%d_%H%M%S')
    db_name = f'apple_bot_{ts}.db'
    tmp_db = None
    try:
        fd, tmp_db = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        src = sqlite3.connect(DB_PATH, timeout=30)
        dst = sqlite3.connect(tmp_db)
        with dst:
            src.backup(dst)          # checksums + checkpoints — WAL/SHM not needed
        src.close()
        dst.close()
        with open(tmp_db, 'rb') as f:
            buf = BytesIO(f.read())
        buf.seek(0)
        buf.name = db_name
        return db_name, buf
    finally:
        if tmp_db and os.path.exists(tmp_db):
            try:
                os.remove(tmp_db)
            except OSError:
                pass


def _read_sidecar(path):
    """⬅️ FIXED — read a live SQLite sidecar file (-wal / -shm)
    if it exists AND is non-empty. Telegram rejects 0-byte uploads."""
    if not os.path.exists(path):
        return None
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    if size <= 0:
        return None  # skip empty files — Telegram rejects them with "file must be non-empty"
    with open(path, 'rb') as f:
        buf = BytesIO(f.read())
    buf.seek(0)
    buf.name = os.path.basename(path)
    return buf


# ---- deposit helpers (atomic) ----
def create_deposit_request(user_id, currency, amount):
    conn = _get_conn()
    cur = conn.cursor()
    try:
        cur.execute('BEGIN IMMEDIATE')
        cur.execute('''
            SELECT id FROM deposit_requests
            WHERE user_id = ? AND status IN ('awaiting_proof','pending')
            LIMIT 1
        ''', (user_id,))
        if cur.fetchone():
            conn.commit()
            return None
        cur.execute('''
            INSERT INTO deposit_requests (user_id, currency, amount, status, created_at)
            VALUES (?, ?, ?, 'awaiting_proof', ?)
        ''', (user_id, currency.upper(), amount, int(time.time())))
        dep_id = cur.lastrowid
        conn.commit()
        return dep_id
    except Exception as e:
        conn.rollback()
        print(f"create_deposit_request error: {e}")
        return None

def set_deposit_proof(deposit_id, method, proof, file_id=None):
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute('''
        UPDATE deposit_requests
        SET method = ?, proof = ?, file_id = ?, status = 'pending'
        WHERE id = ? AND status = 'awaiting_proof'
    ''', (method, proof, file_id, deposit_id))
    conn.commit()
    return cur.rowcount > 0

def get_deposit(deposit_id):
    with closing(db_conn()) as conn:
        row = conn.execute('SELECT * FROM deposit_requests WHERE id = ?', (deposit_id,)).fetchone()
        return dict(row) if row else None

def cancel_awaiting_deposit(user_id):
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute('''
        UPDATE deposit_requests
        SET status = 'cancelled', reviewed_at = ?
        WHERE user_id = ? AND status = 'awaiting_proof'
    ''', (int(time.time()), user_id))
    conn.commit()
    return cur.rowcount > 0

def is_txid_processed(txid):
    with closing(db_conn()) as conn:
        row = conn.execute('SELECT 1 FROM processed_txids WHERE txid = ?', (txid,)).fetchone()
        return row is not None

def mark_txid_processed(txid):
    conn = _get_conn()
    try:
        conn.execute('INSERT OR IGNORE INTO processed_txids (txid, processed_at) VALUES (?, ?)',
                     (txid, int(time.time())))
        conn.commit()
    except Exception as e:
        print(f"mark_txid_processed error: {e}")

def get_active_deposit_info(user_id):
    with closing(db_conn()) as conn:
        row = conn.execute('''
            SELECT id, status FROM deposit_requests
            WHERE user_id = ? AND status IN ('awaiting_proof','pending')
            ORDER BY id DESC LIMIT 1
        ''', (user_id,)).fetchone()
        return (row['id'], row['status']) if row else None

# ==========================================
# 3. LOCALIZATION
# ==========================================
LANGUAGES = {
    'en': {
        'welcome': "👋 <b>Welcome, {name}!</b>\n\n"
                   "╭──────────────────╮\n"
                   "│  👤 <b>Profile</b>\n"
                   "│  🆔 ID: <code>{user_id}</code>\n"
                   "│  💰 Balance: <code>{balance:.2f}$</code>\n"
                   "│  ✅ Status: Authorized\n"
                   "╰──────────────────╯\n\n"
                   "🆘 <b>Need Help?</b>\n"
                   "💬 Contact us: <b>{support}</b>\n\n"
                   "👇 <b>Select a service below to continue:</b>",
        'my_account': "👤 <b>My Account</b>\n\n"
                      "╭──────────────────╮\n"
                      "│  🆔 ID: <code>{user_id}</code>\n"
                      "│  💰 Balance: <code>{balance:.2f}$</code>\n"
                      "╰──────────────────╯",
        'service_prompt': "🛒 <b>{service}</b>\n💲 Price: <b>{price:.2f}$</b>\n\n✅ <b>Enter your IMEI/SN:</b>",
        'invalid_imei': "❌ Invalid IMEI/SN. Please enter a valid 15-digit IMEI.",
        'insufficient_balance': "❌ <b>Insufficient balance!</b>\nYour balance: {balance:.2f}$\nRequired: {price:.2f}$\n\nPlease add credits using /start.",
        'no_balance_warning': "🚫 <b>No Balance Detected</b>\n\n❌ You don't have any funds in your account.\n\n💰 Current balance: <code>{balance:.2f}$</code>\n\n👇 <i>Please deposit funds via <b>👤 My Account → 💰 Deposit</b> to use our services.</i>",
        'processing': "⏳ <b>Processing</b> {service} for IMEI: <code>{imei}</code>...",
        'result': "✅ <b>Result for</b> <code>{imei}</code>\n\n📱 <b>Model:</b> {model}\n🔒 <b>FMI:</b> {fmi}\n☁️ <b>iCloud:</b> {icloud}\n📶 <b>Carrier:</b> {carrier}",
        'new_balance': "\n\n💰 <b>New Balance:</b> <code>{balance:.2f}$</code>",
        'lang_select': "🌐 <b>Please select your language:</b>",
        'lang_changed': "✅ Language changed successfully!",
        'lang_unavailable': "⚠️ This language is not available yet.",
        'btn_lang': "🌐 Language",
        'btn_account': "👤 My Account",
        'btn_support': "🆘 Support",
        'more_info': "MORE INFO",
        'less_info': "LESS INFO",
        'please_choose_service': "Please choose Service 👇",
        'btn_back_menu': "⬅️ Back to Menu",
        'deposit': "💰 Deposit",
        'select_currency': "💱 Select currency",
        'choose_amount': "✨ <b>Choose the top-up amount</b>",
        'other_amount': "❓ Other",
        'back': "⬅️ Back",
        'deposit_prompt': "Please enter the amount you want to deposit (e.g., 25):",
        'creating_invoice': "⏳ Preparing your deposit address, please wait...",
        'payment_info': "💳 <b>Payment Details</b>\n\n💰 <b>Amount:</b> <code>{amount} {currency}</code>\n🌐 <b>Network:</b> TRON (TRC-20)\n\n📥 <b>Recipient Address:</b>\n<blockquote><code>{pay_address}</code></blockquote>\n\n⚠️ <i>Send the exact amount within 30 minutes.</i>\n\n👇 <i>Click 'Paid' once the transaction is complete.</i>",
        'custom_prompt': "💸 <b>Enter the amount in USD</b>\n\nMinimum: <b>$1</b>\nExample: <code>5</code>",
        'custom_invalid': "❌ Invalid amount. Please enter a number (min $1).",
        'payment_received': "✅ <b>Payment received!</b>\n\n💰 Amount: <b>{amount}$</b>\n💳 New balance: <b>{new_balance:.2f}$</b>",
        'admin_panel_info': "👑 <b>Admin Panel</b>\n\nYou are an admin. Use the admin bot to manage users.",
        'btn_cancel_pay': "❌ Cancel",
        'btn_paid_pay': "✅ Paid",
        'proof_prompt': "📤 <b>Submit Your Payment Proof</b>\n\nSend one of the following:\n\n1️⃣ The <b>transaction hash (TXID)</b> from your wallet app\n2️⃣ A <b>screenshot</b> of the payment confirmation\n\n<i>An admin will verify it and credit your balance.</i>\n\n✋ <i>Send /cancel to abort.</i>",
        'proof_received_txid': "✅ <b>Proof received!</b>\n\n📄 TXID: <code>{txid}</code>\n💰 Amount claimed: <b>{amount}$</b> ({currency})\n\n⏳ <i>An admin will verify this shortly.</i>",
        'proof_received_screenshot': "✅ <b>Proof received!</b>\n\n📷 Screenshot saved.\n💰 Amount claimed: <b>{amount}$</b> ({currency})\n\n⏳ <i>An admin will verify this shortly.</i>",
        'proof_invalid': "❌ Invalid proof. Please send a valid TXID (64 hex characters) or a screenshot.\n\n✋ Send /cancel to abort.",
        'proof_cancelled': "🚫 <b>Proof submission cancelled.</b>\n\nYour deposit request remains pending. You can submit proof any time by tapping <b>✅ PAID</b> again.",
        'proof_skipped_nav': "ℹ️ <b>Proof submission skipped.</b>\n\nYour deposit is still pending. Tap <b>✅ PAID</b> on the deposit message to submit proof later.",
        'pending_exists': "⏳ <b>You already have a pending deposit.</b>\n\nPlease wait for admin verification before creating a new one.",
        'proof_already_used': "❌ This TXID has already been used.",
        'already_submitted': "⏳ <b>Proof already submitted.</b>\n\nPlease wait for admin review.",
        'deposit_approved': "🎉 <b>Deposit Approved!</b>\n\n💰 Credited: <b>{amount:.2f}$</b>\n💳 New balance: <b>{balance:.2f}$</b>\n\nℹ️ TXID: <code>{proof}</code>",
        'deposit_rejected': "❌ <b>Deposit Rejected</b>\n\nYour deposit of <b>{amount}$</b> ({currency}) was rejected by an admin.\n\nIf you believe this is a mistake, contact support: {support}",
        'cancel_nothing': "ℹ️ Nothing to cancel.",
        'cancel_prompt': "❌ Action cancelled. Returning to the main menu.",
        'existing_deposit_awaiting': "⏳ <b>You already have a pending deposit.</b>\n\n💰 Amount: <b>{amount:.2f}$</b> ({currency})\n🆔 Request ID: <b>#{dep_id}</b>\n\n👇 <i>Please complete it or cancel it to start a new one.</i>",
        'existing_deposit_reviewing': "⏳ <b>Your deposit is being reviewed by an admin.</b>\n\n💰 Amount: <b>{amount:.2f}$</b> ({currency})\n🆔 Request ID: <b>#{dep_id}</b>\n\nPlease wait for approval.",
        'deposit_cancelled_success': "✅ <b>Pending deposit cancelled.</b>\n\nYou can now create a new deposit.",
        'no_active_deposit': "ℹ️ You don't have any active deposit.",
        'maintenance': (
            "🛠️ <b>Bot Under Maintenance</b>\n\n"
            "╭────────────────────────╮\n"
            "│  ⚙️  We are updating our systems\n"
            "│  🚀  to give you a better experience\n"
            "╰────────────────────────╯\n\n"
            "⏰ <i>Please try again in a few minutes.</i>\n\n"
            "🆘 <b>Need help?</b>\n"
            "💬 Contact support: {support}"
        ),
        'maintenance_short': (
            "🛠️ <b>Bot Under Maintenance</b>\n\n"
            "We are updating our systems.\n"
            "Please try again shortly.\n\n"
            "💬 Support: {support}"
        ),
        'support_msg': (
            "🆘 <b>Support</b>\n\n"
            "╭──────────────────╮\n"
            "│  👤 We're here to help\n"
            "╰──────────────────╯\n\n"
            "💬 Contact us anytime:\n"
            "👉 {support}\n\n"
            "<i>Usually replies within a few minutes.</i>"
        ),
    },
    'es': {
        'welcome': "👋 <b>¡Bienvenido, {name}!</b>\n\n"
                   "╭──────────────────╮\n"
                   "│  👤 <b>Perfil</b>\n"
                   "│  🆔 ID: <code>{user_id}</code>\n"
                   "│  💰 Saldo: <code>{balance:.2f}$</code>\n"
                   "│  ✅ Estado: Autorizado\n"
                   "╰──────────────────╯\n\n"
                   "🆘 <b>¿Necesitas ayuda?</b>\n"
                   "💬 Contáctanos: <code>{support}</code>\n\n"
                   "👇 <b>Selecciona un servicio para continuar:</b>",
        'my_account': "👤 <b>Mi Cuenta</b>\n\n"
                      "╭──────────────────╮\n"
                      "│  🆔 ID: <code>{user_id}</code>\n"
                      "│  💰 Saldo: <code>{balance:.2f}$</code>\n"
                      "╰──────────────────╯",
        'service_prompt': "🛒 <b>{service}</b>\n💲 Precio: <b>{price:.2f}$</b>\n\n✅ <b>Ingresa tu IMEI/SN:</b>",
        'invalid_imei': "❌ IMEI/SN inválido. Ingresa un IMEI válido de 15 dígitos.",
        'insufficient_balance': "❌ <b>¡Saldo insuficiente!</b>\nTu saldo: {balance:.2f}$\nRequerido: {price:.2f}$\n\nAgrega créditos usando /start.",
        'processing': "⏳ <b>Procesando</b> {service} para IMEI: <code>{imei}</code>...",
        'result': "✅ <b>Resultado para</b> <code>{imei}</code>\n\n📱 <b>Modelo:</b> {model}\n🔒 <b>FMI:</b> {fmi}\n☁️ <b>iCloud:</b> {icloud}\n📶 <b>Operador:</b> {carrier}",
        'new_balance': "\n\n💰 <b>Nuevo saldo:</b> <code>{balance:.2f}$</code>",
        'lang_select': "🌐 <b>Por favor selecciona tu idioma:</b>",
        'lang_changed': "✅ ¡Idioma cambiado exitosamente!",
        'btn_lang': "🌐 Idioma",
        'btn_account': "👤 Mi Cuenta",
        'more_info': "MÁS INFO",
        'less_info': "MENOS INFO",
        'please_choose_service': "Por favor elige un Servicio 👇",
        'btn_back_menu': "⬅️ Volver al Menú",
        'cancel_prompt': "❌ Acción cancelada. Volviendo al menú principal."
    },
    'ru': {
        'welcome': "👋 <b>Добро пожаловать, {name}!</b>\n\n"
                   "╭──────────────────╮\n"
                   "│  👤 <b>Профиль</b>\n"
                   "│  🆔 ID: <code>{user_id}</code>\n"
                   "│  💰 Баланс: <code>{balance:.2f}$</code>\n"
                   "│  ✅ Статус: Авторизован\n"
                   "╰──────────────────╯\n\n"
                   "🆘 <b>Нужна помощь?</b>\n"
                   "💬 Свяжитесь с нами: <code>{support}</code>\n\n"
                   "👇 <b>Выберите услугу ниже:</b>",
        'my_account': "👤 <b>Мой Аккаунт</b>\n\n"
                      "╭──────────────────╮\n"
                      "│  🆔 ID: <code>{user_id}</code>\n"
                      "│  💰 Баланс: <code>{balance:.2f}$</code>\n"
                      "╰──────────────────╯",
        'service_prompt': "🛒 <b>{service}</b>\n💲 Цена: <b>{price:.2f}$</b>\n\n✅ <b>Введите ваш IMEI/SN:</b>",
        'invalid_imei': "❌ Неверный IMEI/SN. Введите корректный 15-значный IMEI.",
        'insufficient_balance': "❌ <b>Недостаточно средств!</b>\nВаш баланс: {balance:.2f}$\nТребуется: {price:.2f}$\n\nПополните баланс через /start.",
        'processing': "⏳ <b>Обработка</b> {service} для IMEI: <code>{imei}</code>...",
        'result': "✅ <b>Результат для</b> <code>{imei}</code>\n\n📱 <b>Модель:</b> {model}\n🔒 <b>FMI:</b> {fmi}\n☁️ <b>iCloud:</b> {icloud}\n📶 <b>Оператор:</b> {carrier}",
        'new_balance': "\n\n💰 <b>Новый баланс:</b> <code>{balance:.2f}$</code>",
        'lang_select': "🌐 <b>Пожалуйста, выберите язык:</b>",
        'lang_changed': "✅ Язык успешно изменен!",
        'btn_lang': "🌐 Язык",
        'btn_account': "👤 Мой Аккаунт",
        'more_info': "ПОДРОБНЕЕ",
        'less_info': "СКРЫТЬ",
        'please_choose_service': "Пожалуйста, выберите услугу 👇",
        'btn_back_menu': "⬅️ Назад в меню",
        'cancel_prompt': "❌ Действие отменено. Возврат в главное меню."
    },
    'hi': {
        'welcome': "👋 <b>स्वागत है, {name}!</b>\n\n"
                   "╭──────────────────╮\n"
                   "│  👤 <b>प्रोफ़ाइल</b>\n"
                   "│  🆔 आईडी: <code>{user_id}</code>\n"
                   "│  💰 शेष राशि: <code>{balance:.2f}$</code>\n"
                   "│  ✅ स्थिति: अधिकृत\n"
                   "╰──────────────────╯\n\n"
                   "🆘 <b>मदद चाहिए?</b>\n"
                   "💬 हमसे संपर्क करें: <code>{support}</code>\n\n"
                   "👇 <b>जारी रखने के लिए नीचे एक सेवा चुनें:</b>",
        'my_account': "👤 <b>मेरा खाता</b>\n\n"
                      "╭──────────────────╮\n"
                      "│  🆔 आईडी: <code>{user_id}</code>\n"
                      "│  💰 शेष राशि: <code>{balance:.2f}$</code>\n"
                      "╰──────────────────╯",
        'service_prompt': "🛒 <b>{service}</b>\n💲 मूल्य: <b>{price:.2f}$</b>\n\n✅ <b>अपना IMEI/SN दर्ज करें:</b>",
        'invalid_imei': "❌ अमान्य IMEI/SN। कृपया एक वैध 15-अंकीय IMEI दर्ज करें।",
        'insufficient_balance': "❌ <b>अपर्याप्त शेष राशि!</b>\nआपकी शेष राशि: {balance:.2f}$\nआवश्यक: {price:.2f}$\n\nकृपया /start का उपयोग करके क्रेडिट जोड़ें।",
        'processing': "⏳ <b>प्रसंस्करण</b> {service} IMEI के लिए: <code>{imei}</code>...",
        'result': "✅ <b>परिणाम</b> <code>{imei}</code>\n\n📱 <b>मॉडल:</b> {model}\n🔒 <b>FMI:</b> {fmi}\n☁️ <b>iCloud:</b> {icloud}\n📶 <b>कैरियर:</b> {carrier}",
        'new_balance': "\n\n💰 <b>नई शेष राशि:</b> <code>{balance:.2f}$</code>",
        'lang_select': "🌐 <b>कृपया अपनी भाषा चुनें:</b>",
        'lang_changed': "✅ भाषा सफलतापूर्वक बदली गई!",
        'btn_lang': "🌐 भाषा",
        'btn_account': "👤 मेरा खाता",
        'more_info': "अधिक जानकारी",
        'less_info': "कम जानकारी",
        'please_choose_service': "कृपया सेवा चुनें 👇",
        'btn_back_menu': "⬅️ मेनू पर वापस जाएं",
        'cancel_prompt': "❌ कार्रवाई रद्द की गई। मुख्य मेनू पर वापस जा रहे हैं।"
    },
    'ar': {
        'welcome': "👋 <b>مرحباً، {name}!</b>\n\n"
                   "╭──────────────────╮\n"
                   "│  👤 <b>الملف الشخصي</b>\n"
                   "│  🆔 المعرف: <code>{user_id}</code>\n"
                   "│  💰 الرصيد: <code>{balance:.2f}$</code>\n"
                   "│  ✅ الحالة: مصرح به\n"
                   "╰──────────────────╯\n\n"
                   "🆘 <b>تحتاج مساعدة؟</b>\n"
                   "💬 اتصل بنا: <code>{support}</code>\n\n"
                   "👇 <b>اختر خدمة أدناه للمتابعة:</b>",
        'my_account': "👤 <b>حسابي</b>\n\n"
                      "╭──────────────────╮\n"
                      "│  🆔 المعرف: <code>{user_id}</code>\n"
                      "│  💰 الرصيد: <code>{balance:.2f}$</code>\n"
                      "╰──────────────────╯",
        'service_prompt': "🛒 <b>{service}</b>\n💲 السعر: <b>{price:.2f}$</b>\n\n✅ <b>أدخل IMEI/SN الخاص بك:</b>",
        'invalid_imei': "❌ IMEI/SN غير صالح. يرجى إدخال IMEI صالح مكون من 15 رقماً.",
        'insufficient_balance': "❌ <b>رصيد غير كاف!</b>\nرصيدك: {balance:.2f}$\nالمطلوب: {price:.2f}$\n\nيرجى إضافة رصيد باستخدام /start.",
        'processing': "⏳ <b>جاري المعالجة</b> {service} لـ IMEI: <code>{imei}</code>...",
        'result': "✅ <b>نتيجة لـ</b> <code>{imei}</code>\n\n📱 <b>الطراز:</b> {model}\n🔒 <b>FMI:</b> {fmi}\n☁️ <b>iCloud:</b> {icloud}\n📶 <b>المشغل:</b> {carrier}",
        'new_balance': "\n\n💰 <b>الرصيد الجديد:</b> <code>{balance:.2f}$</code>",
        'lang_select': "🌐 <b>يرجى اختيار لغتك:</b>",
        'lang_changed': "✅ تم تغيير اللغة بنجاح!",
        'btn_lang': "🌐 اللغة",
        'btn_account': "👤 حسابي",
        'more_info': "مزيد من المعلومات",
        'less_info': "أقل معلومات",
        'please_choose_service': "الرجاء اختيار الخدمة 👇",
        'btn_back_menu': "⬅️ العودة إلى القائمة",
        'cancel_prompt': "❌ تم إلغاء الإجراء. العودة إلى القائمة الرئيسية."
    },
    'zh': {
        'welcome': "👋 <b>欢迎，{name}！</b>\n\n"
                   "╭──────────────────╮\n"
                   "│  👤 <b>个人资料</b>\n"
                   "│  🆔 ID: <code>{user_id}</code>\n"
                   "│  💰 余额: <code>{balance:.2f}$</code>\n"
                   "│  ✅ 状态: 已授权\n"
                   "╰──────────────────╯\n\n"
                   "🆘 <b>需要帮助吗？</b>\n"
                   "💬 联系我们：<code>{support}</code>\n\n"
                   "👇 <b>请选择下方的服务以继续：</b>",
        'my_account': "👤 <b>我的账户</b>\n\n"
                      "╭──────────────────╮\n"
                      "│  🆔 ID: <code>{user_id}</code>\n"
                      "│  💰 余额: <code>{balance:.2f}$</code>\n"
                      "╰──────────────────╯",
        'service_prompt': "🛒 <b>{service}</b>\n💲 价格: <b>{price:.2f}$</b>\n\n✅ <b>请输入您的 IMEI/SN：</b>",
        'invalid_imei': "❌ IMEI/SN 无效。请输入有效的15位IMEI。",
        'insufficient_balance': "❌ <b>余额不足！</b>\n您的余额: {balance:.2f}$\n需要: {price:.2f}$\n\n请使用 /start 添加余额。",
        'processing': "⏳ <b>正在处理</b> {service}，IMEI: <code>{imei}</code>...",
        'result': "✅ <b>结果</b> <code>{imei}</code>\n\n📱 <b>型号:</b> {model}\n🔒 <b>FMI:</b> {fmi}\n☁️ <b>iCloud:</b> {icloud}\n📶 <b>运营商:</b> {carrier}",
        'new_balance': "\n\n💰 <b>新余额:</b> <code>{balance:.2f}$</code>",
        'lang_select': "🌐 <b>请选择您的语言：</b>",
        'lang_changed': "✅ 语言更改成功！",
        'btn_lang': "🌐 语言",
        'btn_account': "👤 我的账户",
        'more_info': "更多信息",
        'less_info': "收起信息",
        'please_choose_service': "请选择服务 👇",
        'btn_back_menu': "⬅️ 返回主菜单",
        'cancel_prompt': "❌ 操作已取消。返回主菜单。"
    },
    'fr': {
        'welcome': "👋 <b>Bienvenue, {name} !</b>\n\n"
                   "╭──────────────────╮\n"
                   "│  👤 <b>Profil</b>\n"
                   "│  🆔 ID: <code>{user_id}</code>\n"
                   "│  💰 Solde: <code>{balance:.2f}$</code>\n"
                   "│  ✅ Statut: Autorisé\n"
                   "╰──────────────────╯\n\n"
                   "🆘 <b>Besoin d'aide ?</b>\n"
                   "💬 Contactez-nous : <code>{support}</code>\n\n"
                   "👇 <b>Sélectionnez un service ci-dessous :</b>",
        'my_account': "👤 <b>Mon Compte</b>\n\n"
                      "╭──────────────────╮\n"
                      "│  🆔 ID: <code>{user_id}</code>\n"
                      "│  💰 Solde: <code>{balance:.2f}$</code>\n"
                      "╰──────────────────╯",
        'service_prompt': "🛒 <b>{service}</b>\n💲 Prix: <b>{price:.2f}$</b>\n\n✅ <b>Entrez votre IMEI/SN :</b>",
        'invalid_imei': "❌ IMEI/SN invalide. Veuillez entrer un IMEI valide de 15 chiffres.",
        'insufficient_balance': "❌ <b>Solde insuffisant !</b>\nVotre solde: {balance:.2f}$\nRequis: {price:.2f}$\n\nVeuillez ajouter des crédits via /start.",
        'processing': "⏳ <b>Traitement</b> {service} pour IMEI: <code>{imei}</code>...",
        'result': "✅ <b>Résultat pour</b> <code>{imei}</code>\n\n📱 <b>Modèle:</b> {model}\n🔒 <b>FMI:</b> {fmi}\n☁️ <b>iCloud:</b> {icloud}\n📶 <b>Opérateur:</b> {carrier}",
        'new_balance': "\n\n💰 <b>Nouveau solde :</b> <code>{balance:.2f}$</code>",
        'lang_select': "🌐 <b>Veuillez sélectionner votre langue :</b>",
        'lang_changed': "✅ Langue changée avec succès !",
        'btn_lang': "🌐 Langue",
        'btn_account': "👤 Mon Compte",
        'more_info': "PLUS D'INFO",
        'less_info': "MOINS D'INFO",
        'please_choose_service': "Veuillez choisir un service 👇",
        'btn_back_menu': "⬅️ Retour au Menu",
        'cancel_prompt': "❌ Action annulée. Retour au menu principal."
    },
    'de': {
        'welcome': "👋 <b>Willkommen, {name}!</b>\n\n"
                   "╭──────────────────╮\n"
                   "│  👤 <b>Profil</b>\n"
                   "│  🆔 ID: <code>{user_id}</code>\n"
                   "│  💰 Guthaben: <code>{balance:.2f}$</code>\n"
                   "│  ✅ Status: Autorisiert\n"
                   "╰──────────────────╯\n\n"
                   "🆘 <b>Brauchen Sie Hilfe?</b>\n"
                   "💬 Kontaktieren Sie uns: <code>{support}</code>\n\n"
                   "👇 <b>Wählen Sie unten einen Service:</b>",
        'my_account': "👤 <b>Mein Konto</b>\n\n"
                      "╭──────────────────╮\n"
                      "│  🆔 ID: <code>{user_id}</code>\n"
                      "│  💰 Guthaben: <code>{balance:.2f}$</code>\n"
                      "╰──────────────────╯",
        'service_prompt': "🛒 <b>{service}</b>\n💲 Preis: <b>{price:.2f}$</b>\n\n✅ <b>Geben Sie Ihre IMEI/SN ein:</b>",
        'invalid_imei': "❌ Ungültige IMEI/SN. Bitte geben Sie eine gültige 15-stellige IMEI ein.",
        'insufficient_balance': "❌ <b>Unzureichendes Guthaben!</b>\nIhr Guthaben: {balance:.2f}$\nErforderlich: {price:.2f}$\n\nBitte laden Sie Guthaben über /start auf.",
        'processing': "⏳ <b>Verarbeitung</b> {service} für IMEI: <code>{imei}</code>...",
        'result': "✅ <b>Ergebnis für</b> <code>{imei}</code>\n\n📱 <b>Modell:</b> {model}\n🔒 <b>FMI:</b> {fmi}\n☁️ <b>iCloud:</b> {icloud}\n📶 <b>Anbieter:</b> {carrier}",
        'new_balance': "\n\n💰 <b>Neues Guthaben:</b> <code>{balance:.2f}$</code>",
        'lang_select': "🌐 <b>Bitte wählen Sie Ihre Sprache:</b>",
        'lang_changed': "✅ Sprache erfolgreich geändert!",
        'btn_lang': "🌐 Sprache",
        'btn_account': "👤 Mein Konto",
        'more_info': "MEHR INFO",
        'less_info': "WENIGER INFO",
        'please_choose_service': "Bitte wählen Sie einen Service 👇",
        'btn_back_menu': "⬅️ Zurück zum Menü",
        'cancel_prompt': "❌ Aktion abgebrochen. Zurück zum Hauptmenü."
    },
    'pt': {
        'welcome': "👋 <b>Bem-vindo, {name}!</b>\n\n"
                   "╭──────────────────╮\n"
                   "│  👤 <b>Perfil</b>\n"
                   "│  🆔 ID: <code>{user_id}</code>\n"
                   "│  💰 Saldo: <code>{balance:.2f}$</code>\n"
                   "│  ✅ Status: Autorizado\n"
                   "╰──────────────────╯\n\n"
                   "🆘 <b>Precisa de ajuda?</b>\n"
                   "💬 Fale conosco: <code>{support}</code>\n\n"
                   "👇 <b>Selecione um serviço abaixo:</b>",
        'my_account': "👤 <b>Minha Conta</b>\n\n"
                      "╭──────────────────╮\n"
                      "│  🆔 ID: <code>{user_id}</code>\n"
                      "│  💰 Saldo: <code>{balance:.2f}$</code>\n"
                      "╰──────────────────╯",
        'service_prompt': "🛒 <b>{service}</b>\n💲 Preço: <b>{price:.2f}$</b>\n\n✅ <b>Insira seu IMEI/SN:</b>",
        'invalid_imei': "❌ IMEI/SN inválido. Por favor, insira um IMEI válido de 15 dígitos.",
        'insufficient_balance': "❌ <b>Saldo insuficiente!</b>\nSeu saldo: {balance:.2f}$\nNecessário: {price:.2f}$\n\nAdicione créditos usando /start.",
        'processing': "⏳ <b>Processando</b> {service} para IMEI: <code>{imei}</code>...",
        'result': "✅ <b>Resultado para</b> <code>{imei}</code>\n\n📱 <b>Modelo:</b> {model}\n🔒 <b>FMI:</b> {fmi}\n☁️ <b>iCloud:</b> {icloud}\n📶 <b>Operadora:</b> {carrier}",
        'new_balance': "\n\n💰 <b>Novo Saldo:</b> <code>{balance:.2f}$</code>",
        'lang_select': "🌐 <b>Por favor, selecione seu idioma:</b>",
        'lang_changed': "✅ Idioma alterado com sucesso!",
        'btn_lang': "🌐 Idioma",
        'btn_account': "👤 Minha Conta",
        'more_info': "MAIS INFO",
        'less_info': "MENOS INFO",
        'please_choose_service': "Por favor, escolha um serviço 👇",
        'btn_back_menu': "⬅️ Voltar ao Menu",
        'cancel_prompt': "❌ Ação cancelada. Voltando ao menu principal."
    },
    'tr': {
        'welcome': "👋 <b>Hoş geldin, {name}!</b>\n\n"
                   "╭──────────────────╮\n"
                   "│  👤 <b>Profil</b>\n"
                   "│  🆔 ID: <code>{user_id}</code>\n"
                   "│  💰 Bakiye: <code>{balance:.2f}$</code>\n"
                   "│  ✅ Durum: Yetkili\n"
                   "╰──────────────────╯\n\n"
                   "🆘 <b>Yardıma mı ihtiyacınız var?</b>\n"
                   "💬 Bize ulaşın: <code>{support}</code>\n\n"
                   "👇 <b>Devam etmek için aşağıdan bir hizmet seçin:</b>",
        'my_account': "👤 <b>Hesabım</b>\n\n"
                      "╭──────────────────╮\n"
                      "│  🆔 ID: <code>{user_id}</code>\n"
                      "│  💰 Bakiye: <code>{balance:.2f}$</code>\n"
                      "╰──────────────────╯",
        'service_prompt': "🛒 <b>{service}</b>\n💲 Fiyat: <b>{price:.2f}$</b>\n\n✅ <b>IMEI/SN girin:</b>",
        'invalid_imei': "❌ Geçersiz IMEI/SN. Lütfen 15 haneli geçerli bir IMEI girin.",
        'insufficient_balance': "❌ <b>Yetersiz bakiye!</b>\nBakiyeniz: {balance:.2f}$\nGerekli: {price:.2f}$\n\nLütfen /start kullanarak kredi ekleyin.",
        'processing': "⏳ <b>İşleniyor</b> {service} IMEI: <code>{imei}</code>...",
        'result': "✅ <b>Sonuç</b> <code>{imei}</code>\n\n📱 <b>Model:</b> {model}\n🔒 <b>FMI:</b> {fmi}\n☁️ <b>iCloud:</b> {icloud}\n📶 <b>Operatör:</b> {carrier}",
        'new_balance': "\n\n💰 <b>Yeni Bakiye:</b> <code>{balance:.2f}$</code>",
        'lang_select': "🌐 <b>Lütfen dilinizi seçin:</b>",
        'lang_changed': "✅ Dil başarıyla değiştirildi!",
        'btn_lang': "🌐 Dil",
        'btn_account': "👤 Hesabım",
        'more_info': "DAHA FAZLA",
        'less_info': "DAHA AZ",
        'please_choose_service': "Lütfen Hizmet Seçin 👇",
        'btn_back_menu': "⬅️ Menüye Dön",
        'cancel_prompt': "❌ İşlem iptal edildi. Ana menüye dönülüyor."
    }
}

def get_text(lang, key, **kwargs):
    lang_data = LANGUAGES.get(lang, LANGUAGES['en'])
    text = lang_data.get(key, LANGUAGES['en'].get(key, key))
    return text.format(**kwargs) if kwargs else text

def send_maintenance(user_id, lang=None, short=False):
    if lang is None:
        _, lang = get_user(user_id)
    key = 'maintenance_short' if short else 'maintenance'
    msg = get_text(lang, key, support=SUPPORT_DISPLAY)
    bot.send_message(user_id, msg, parse_mode='HTML', disable_web_page_preview=True)

# ==========================================
# 4. SERVICES
# ==========================================
SERVICES = {
    "fmi": {"price": 0.01, "api_id": "fmi_on_off", "name": {"en": "FMI ON/OFF 🔍", "es": "FMI ON/OFF 🔍", "ru": "FMI ВКЛ/ВЫКЛ 🔍", "hi": "FMI ON/OFF 🔍", "ar": "FMI ON/OFF 🔍", "zh": "FMI 开/关 🔍", "fr": "FMI ON/OFF 🔍", "de": "FMI AN/AUS 🔍", "pt": "FMI ON/OFF 🔍", "tr": "FMI AÇIK/KAPALI 🔍"}},
    "icloud": {"price": 0.15, "api_id": "icloud_status", "name": {"en": "iCloud Clean/Lost 🔍", "es": "iCloud Limpio/Perdido 🔍", "ru": "iCloud Чистый/Потерянный 🔍", "hi": "iCloud क्लीन/लॉस्ट 🔍", "ar": "iCloud نظيف/مفقود 🔍", "zh": "iCloud 干净/丢失 🔍", "fr": "iCloud Propre/Perdu 🔍", "de": "iCloud Sauber/Verloren 🔍", "pt": "iCloud Limpo/Perdido 🔍", "tr": "iCloud Temiz/Kayıp 🔍"}},
    "basic": {"price": 0.05, "api_id": "basic_info", "name": {"en": "Apple BASIC Info 🔍", "es": "Info BÁSICA de Apple 🔍", "ru": "Базовая информация Apple 🔍", "hi": "Apple बेसिक जानकारी 🔍", "ar": "معلومات Apple الأساسية 🔍", "zh": "Apple 基本信息 🔍", "fr": "Infos de base Apple 🔍", "de": "Apple BASIS Info 🔍", "pt": "Info BÁSICA da Apple 🔍", "tr": "Apple TEMEL Bilgi 🔍"}},
    "carrier": {"price": 0.10, "api_id": "carrier_pro", "name": {"en": "Apple Carrier Pro 🔍", "es": "Apple Carrier Pro 🔍", "ru": "Apple Carrier Pro 🔍", "hi": "Apple कैरियर प्रो 🔍", "ar": "Apple Carrier Pro 🔍", "zh": "Apple 运营商 Pro 🔍", "fr": "Apple Carrier Pro 🔍", "de": "Apple Anbieter Pro 🔍", "pt": "Apple Operadora Pro 🔍", "tr": "Apple Operatör Pro 🔍"}},
    "carrier_plus": {"price": 0.20, "api_id": "carrier_pro_plus", "name": {"en": "Apple Carrier Pro+🌟", "es": "Apple Carrier Pro+🌟", "ru": "Apple Carrier Pro+🌟", "hi": "Apple कैरियर प्रो+🌟", "ar": "Apple Carrier Pro+🌟", "zh": "Apple 运营商 Pro+🌟", "fr": "Apple Carrier Pro+🌟", "de": "Apple Anbieter Pro+🌟", "pt": "Apple Operadora Pro+🌟", "tr": "Apple Operatör Pro+🌟"}},
    "max": {"price": 0.25, "api_id": "max_info", "name": {"en": "Apple Max Info 🌟", "es": "Apple Max Info 🌟", "ru": "Apple Max Info 🌟", "hi": "Apple मैक्स जानकारी 🌟", "ar": "Apple Max Info 🌟", "zh": "Apple Max 信息 🌟", "fr": "Apple Max Info 🌟", "de": "Apple Max Info 🌟", "pt": "Apple Max Info 🌟", "tr": "Apple Max Bilgi 🌟"}},
    "imei_check": {"price": 0.02, "api_id": "imei_check", "name": {"en": "📁 Apple IMEI Check", "es": "📁 Verificación IMEI Apple", "ru": "📁 Проверка IMEI Apple", "hi": "📁 Apple IMEI चेक", "ar": "📁 فحص IMEI Apple", "zh": "📁 Apple IMEI 查询", "fr": "📁 Vérification IMEI Apple", "de": "📁 Apple IMEI Check", "pt": "📁 Verificação IMEI Apple", "tr": "📁 Apple IMEI Sorgulama"}},
    "gsx": {"price": 0.30, "api_id": "gsx", "name": {"en": "📁 GSX Services", "es": "📁 Servicios GSX", "ru": "📁 Услуги GSX", "hi": "📁 GSX सेवाएं", "ar": "📁 خدمات GSX", "zh": "📁 GSX 服务", "fr": "📁 Services GSX", "de": "📁 GSX Services", "pt": "📁 Serviços GSX", "tr": "📁 GSX Hizmetleri"}},
    "other_imei": {"price": 0.05, "api_id": "other_imei", "name": {"en": "📁 Other IMEI Check", "es": "📁 Otra Verificación IMEI", "ru": "📁 Другая проверка IMEI", "hi": "📁 अन्य IMEI चेक", "ar": "📁 فحص IMEI آخر", "zh": "📁 其他 IMEI 查询", "fr": "📁 Autre Vérification IMEI", "de": "📁 Anderer IMEI Check", "pt": "📁 Outra Verificação IMEI", "tr": "📁 Diğer IMEI Sorgulama"}},
    "other": {"price": 0.05, "api_id": "other", "name": {"en": "📁 Other", "es": "📁 Otro", "ru": "📁 Другое", "hi": "📁 अन्य", "ar": "📁 آخر", "zh": "📁 其他", "fr": "📁 Autre", "de": "📁 Andere", "pt": "📁 Outro", "tr": "📁 Diğer"}}
}

IMEI_SUB_SERVICES = {
    "carrier_lite": {"price": 0.02, "api_id": "carrier_lite", "name": {"en": "Apple Carrier Lite 🔍", "es": "Apple Carrier Lite 🔍", "ru": "Apple Carrier Lite 🔍", "hi": "Apple कैरियर लाइट 🔍", "ar": "Apple Carrier Lite 🔍", "zh": "Apple 运营商 Lite 🔍", "fr": "Apple Carrier Lite 🔍", "de": "Apple Anbieter Lite 🔍", "pt": "Apple Operadora Lite 🔍", "tr": "Apple Operatör Lite 🔍"}},
    "warranty_info": {"price": 0.03, "api_id": "warranty_info", "name": {"en": "Apple Warranty Info 🔍", "es": "Info de Garantía Apple 🔍", "ru": "Информация о гарантии Apple 🔍", "hi": "Apple वारंटी जानकारी 🔍", "ar": "معلومات ضمان Apple 🔍", "zh": "Apple 保修信息 🔍", "fr": "Infos de garantie Apple 🔍", "de": "Apple Garantie Info 🔍", "pt": "Info de Garantia Apple 🔍", "tr": "Apple Garanti Bilgisi 🔍"}},
    "gsma_blacklist": {"price": 0.05, "api_id": "gsma_blacklist", "name": {"en": "GSMA Blacklist Status History 🔍", "es": "Historial de Lista Negra GSMA 🔍", "ru": "История черного списка GSMA 🔍", "hi": "GSMA ब्लैकलिस्ट इतिहास 🔍", "ar": "سجل القائمة السوداء GSMA 🔍", "zh": "GSMA 黑名单历史 🔍", "fr": "Historique Liste Noire GSMA 🔍", "de": "GSMA Blacklist Verlauf 🔍", "pt": "Histórico de Lista Negra GSMA 🔍", "tr": "GSMA Kara Liste Geçmişi 🔍"}},
    "icloud_sn": {"price": 0.10, "api_id": "icloud_sn", "name": {"en": "iCloud Clean/Lost [SN] 🔍", "es": "iCloud Limpio/Perdido [SN] 🔍", "ru": "iCloud Чистый/Потерянный [SN] 🔍", "hi": "iCloud क्लीन/लॉस्ट [SN] 🔍", "ar": "iCloud نظيف/مفقود [SN] 🔍", "zh": "iCloud 干净/丢失 [SN] 🔍", "fr": "iCloud Propre/Perdu [SN] 🔍", "de": "iCloud Sauber/Verloren [SN] 🔍", "pt": "iCloud Limpo/Perdido [SN] 🔍", "tr": "iCloud Temiz/Kayıp [SN] 🔍"}},
    "part_number": {"price": 0.02, "api_id": "part_number", "name": {"en": "Apple Part Number (MPN) 🔍", "es": "Número de Parte Apple (MPN) 🔍", "ru": "Номер детали Apple (MPN) 🔍", "hi": "Apple पार्ट नंबर (MPN) 🔍", "ar": "رقم قطعة Apple (MPN) 🔍", "zh": "Apple 零件号 (MPN) 🔍", "fr": "Numéro de pièce Apple (MPN) 🔍", "de": "Apple Teilenummer (MPN) 🔍", "pt": "Número de Peça Apple (MPN) 🔍", "tr": "Apple Parça Numarası (MPN) 🔍"}},
    "owner_id": {"price": 0.05, "api_id": "owner_id", "name": {"en": "Apple Owner ID Info 🔍", "es": "Info ID de Propietario Apple 🔍", "ru": "Информация о владельце Apple 🔍", "hi": "Apple मालिक आईडी जानकारी 🔍", "ar": "معلومات معرف مالك Apple 🔍", "zh": "Apple 所有者 ID 信息 🔍", "fr": "Infos ID Propriétaire Apple 🔍", "de": "Apple Besitzer ID Info 🔍", "pt": "Info ID do Proprietário Apple 🔍", "tr": "Apple Sahip Kimliği Bilgisi 🔍"}},
    "chimaera": {"price": 0.05, "api_id": "chimaera", "name": {"en": "Apple Chimaera Check 🔍", "es": "Verificación Apple Chimaera 🔍", "ru": "Проверка Apple Chimaera 🔍", "hi": "Apple Chimaera चेक 🔍", "ar": "فحص Apple Chimaera 🔍", "zh": "Apple Chimaera 查询 🔍", "fr": "Vérification Apple Chimaera 🔍", "de": "Apple Chimaera Check 🔍", "pt": "Verificação Apple Chimaera 🔍", "tr": "Apple Chimaera Sorgulama 🔍"}}
}

GSX_SUB_SERVICES = {
    "gsx_iccid": {"price": 0.15, "api_id": "gsx_iccid", "name": {"en": "GSX [ICCID/MAC] 🔍", "es": "GSX [ICCID/MAC] 🔍", "ru": "GSX [ICCID/MAC] 🔍", "hi": "GSX [ICCID/MAC] 🔍", "ar": "GSX [ICCID/MAC] 🔍", "zh": "GSX [ICCID/MAC] 🔍", "fr": "GSX [ICCID/MAC] 🔍", "de": "GSX [ICCID/MAC] 🔍", "pt": "GSX [ICCID/MAC] 🔍", "tr": "GSX [ICCID/MAC] 🔍"}},
    "sold_by": {"price": 0.10, "api_id": "sold_by", "name": {"en": "Sold By 🌟", "es": "Vendido Por 🌟", "ru": "Продано 🌟", "hi": "द्वारा बेचा गया 🌟", "ar": "بيعت بواسطة 🌟", "zh": "销售方 🌟", "fr": "Vendu Par 🌟", "de": "Verkauft von 🌟", "pt": "Vendido Por 🌟", "tr": "Satıcı 🌟"}},
    "case_history_repairs": {"price": 0.20, "api_id": "case_history_repairs", "name": {"en": "Case History, Repairs 🔍", "es": "Historial de Casos, Reparaciones 🔍", "ru": "История дел, ремонты 🔍", "hi": "केस इतिहास, मरम्मत 🔍", "ar": "سجل الحالات والإصلاحات 🔍", "zh": "案例历史，维修 🔍", "fr": "Historique des cas, réparations 🔍", "de": "Fallhistorie, Reparaturen 🔍", "pt": "Histórico de Casos, Reparos 🔍", "tr": "Servis Geçmişi, Onarımlar 🔍"}},
    "full_gsx": {"price": 0.30, "api_id": "full_gsx", "name": {"en": "FULL GSX 🌟", "es": "FULL GSX 🌟", "ru": "ПОЛНЫЙ GSX 🌟", "hi": "पूर्ण GSX 🌟", "ar": "GSX كامل 🌟", "zh": "完整 GSX 🌟", "fr": "FULL GSX 🌟", "de": "FULL GSX 🌟", "pt": "FULL GSX 🌟", "tr": "TAM GSX 🌟"}},
    "macos_on_off": {"price": 0.05, "api_id": "macos_on_off", "name": {"en": "MacOS ON/OFF 🔍", "es": "MacOS ON/OFF 🔍", "ru": "MacOS ВКЛ/ВЫКЛ 🔍", "hi": "MacOS ON/OFF 🔍", "ar": "MacOS ON/OFF 🔍", "zh": "MacOS 开/关 🔍", "fr": "MacOS ON/OFF 🔍", "de": "MacOS AN/AUS 🔍", "pt": "MacOS ON/OFF 🔍", "tr": "MacOS AÇIK/KAPALI 🔍"}},
    "macos_clean_lost": {"price": 0.05, "api_id": "macos_clean_lost", "name": {"en": "MacOS Clean/Lost 🔍", "es": "MacOS Limpio/Perdido 🔍", "ru": "MacOS Чистый/Потерянный 🔍", "hi": "MacOS क्लीन/लॉस्ट 🔍", "ar": "MacOS نظيف/مفقود 🔍", "zh": "MacOS 干净/丢失 🔍", "fr": "MacOS Propre/Perdu 🔍", "de": "MacOS Sauber/Verloren 🔍", "pt": "MacOS Limpo/Perdido 🔍", "tr": "MacOS Temiz/Kayıp 🔍"}},
    "gsx_carrier": {"price": 0.05, "api_id": "gsx_carrier", "name": {"en": "GSX Carrier 🔍", "es": "GSX Operador 🔍", "ru": "GSX Оператор 🔍", "hi": "GSX कैरियर 🔍", "ar": "GSX المشغل 🔍", "zh": "GSX 运营商 🔍", "fr": "GSX Opérateur 🔍", "de": "GSX Anbieter 🔍", "pt": "GSX Operadora 🔍", "tr": "GSX Operatör 🔍"}},
    "mdm_on_off": {"price": 0.05, "api_id": "mdm_on_off", "name": {"en": "MDM ON/OFF 🔍", "es": "MDM ON/OFF 🔍", "ru": "MDM ВКЛ/ВЫКЛ 🔍", "hi": "MDM ON/OFF 🔍", "ar": "MDM ON/OFF 🔍", "zh": "MDM 开/关 🔍", "fr": "MDM ON/OFF 🔍", "de": "MDM AN/AUS 🔍", "pt": "MDM ON/OFF 🔍", "tr": "MDM AÇIK/KAPALI 🔍"}},
    "repairs_replacement": {"price": 0.10, "api_id": "repairs_replacement", "name": {"en": "Repairs, Replacement 🔍", "es": "Reparaciones, Reemplazo 🔍", "ru": "Ремонт, замена 🔍", "hi": "मरम्मत, प्रतिस्थापन 🔍", "ar": "الإصلاحات والاستبدال 🔍", "zh": "维修，更换 🔍", "fr": "Réparations, Remplacement 🔍", "de": "Reparaturen, Ersatz 🔍", "pt": "Reparos, Substituição 🔍", "tr": "Onarımlar, Değişim 🔍"}},
    "case_history": {"price": 0.10, "api_id": "case_history", "name": {"en": "Case History 🔍", "es": "Historial de Casos 🔍", "ru": "История дел 🔍", "hi": "केस इतिहास 🔍", "ar": "سجل الحالات 🔍", "zh": "案例历史 🔍", "fr": "Historique des cas 🔍", "de": "Fallhistorie 🔍", "pt": "Histórico de Casos 🔍", "tr": "Servis Geçmişi 🔍"}}
}

OTHER_IMEI_SUB_SERVICES = {
    "samsung_info": {"price": 0.02, "api_id": "samsung_info", "name": {"en": "Samsung Info 🔍", "es": "Samsung Info 🔍", "ru": "Samsung Info 🔍", "hi": "Samsung जानकारी 🔍", "ar": "Samsung معلومات 🔍", "zh": "Samsung 信息 🔍", "fr": "Samsung Info 🔍", "de": "Samsung Info 🔍", "pt": "Samsung Info 🔍", "tr": "Samsung Bilgi 🔍"}},
    "samsung_onoff": {"price": 0.02, "api_id": "samsung_onoff", "name": {"en": "Samsung ON/OFF 🔍", "es": "Samsung ON/OFF 🔍", "ru": "Samsung ВКЛ/ВЫКЛ 🔍", "hi": "Samsung ON/OFF 🔍", "ar": "Samsung ON/OFF 🔍", "zh": "Samsung 开/关 🔍", "fr": "Samsung ON/OFF 🔍", "de": "Samsung AN/AUS 🔍", "pt": "Samsung ON/OFF 🔍", "tr": "Samsung AÇIK/KAPALI 🔍"}},
    "xiaomi_onoff": {"price": 0.02, "api_id": "xiaomi_onoff", "name": {"en": "Xiaomi ON/OFF 🔍", "es": "Xiaomi ON/OFF 🔍", "ru": "Xiaomi ВКЛ/ВЫКЛ 🔍", "hi": "Xiaomi ON/OFF 🔍", "ar": "Xiaomi ON/OFF 🔍", "zh": "Xiaomi 开/关 🔍", "fr": "Xiaomi ON/OFF 🔍", "de": "Xiaomi AN/AUS 🔍", "pt": "Xiaomi ON/OFF 🔍", "tr": "Xiaomi AÇIK/KAPALI 🔍"}},
    "xiaomi_clean_lost": {"price": 0.02, "api_id": "xiaomi_clean_lost", "name": {"en": "Xiaomi Clean/Lost 🔍", "es": "Xiaomi Limpio/Perdido 🔍", "ru": "Xiaomi Чистый/Потерянный 🔍", "hi": "Xiaomi क्लीन/लॉस्ट 🔍", "ar": "Xiaomi نظيف/مفقود 🔍", "zh": "Xiaomi 干净/丢失 🔍", "fr": "Xiaomi Propre/Perdu 🔍", "de": "Xiaomi Sauber/Verloren 🔍", "pt": "Xiaomi Limpo/Perdido 🔍", "tr": "Xiaomi Temiz/Kayıp 🔍"}},
    "motorola_info": {"price": 0.02, "api_id": "motorola_info", "name": {"en": "Motorola Info 🔍", "es": "Motorola Info 🔍", "ru": "Motorola Info 🔍", "hi": "Motorola जानकारी 🔍", "ar": "Motorola معلومات 🔍", "zh": "Motorola 信息 🔍", "fr": "Motorola Info 🔍", "de": "Motorola Info 🔍", "pt": "Motorola Info 🔍", "tr": "Motorola Bilgi 🔍"}},
    "lenovo_info": {"price": 0.02, "api_id": "lenovo_info", "name": {"en": "Lenovo Info 🔍", "es": "Lenovo Info 🔍", "ru": "Lenovo Info 🔍", "hi": "Lenovo जानकारी 🔍", "ar": "Lenovo معلومات 🔍", "zh": "Lenovo 信息 🔍", "fr": "Lenovo Info 🔍", "de": "Lenovo Info 🔍", "pt": "Lenovo Info 🔍", "tr": "Lenovo Bilgi 🔍"}},
    "pixel_info": {"price": 0.02, "api_id": "pixel_info", "name": {"en": "Google Pixel Info 🔍", "es": "Google Pixel Info 🔍", "ru": "Google Pixel Info 🔍", "hi": "Google Pixel जानकारी 🔍", "ar": "Google Pixel معلومات 🔍", "zh": "Google Pixel 信息 🔍", "fr": "Google Pixel Info 🔍", "de": "Google Pixel Info 🔍", "pt": "Google Pixel Info 🔍", "tr": "Google Pixel Bilgi 🔍"}},
    "huawei_info": {"price": 0.02, "api_id": "huawei_info", "name": {"en": "Huawei Info 🔍", "es": "Huawei Info 🔍", "ru": "Huawei Info 🔍", "hi": "Huawei जानकारी 🔍", "ar": "Huawei معلومات 🔍", "zh": "Huawei 信息 🔍", "fr": "Huawei Info 🔍", "de": "Huawei Info 🔍", "pt": "Huawei Info 🔍", "tr": "Huawei Bilgi 🔍"}},
    "honor_info": {"price": 0.02, "api_id": "honor_info", "name": {"en": "Honor Info 🔍", "es": "Honor Info 🔍", "ru": "Honor Info 🔍", "hi": "Honor जानकारी 🔍", "ar": "Honor معلومات 🔍", "zh": "Honor 信息 🔍", "fr": "Honor Info 🔍", "de": "Honor Info 🔍", "pt": "Honor Info 🔍", "tr": "Honor Bilgi 🔍"}}
}

OTHER_SUB_SERVICES = {
    "att_usa_status": {"price": 0.05, "api_id": "att_usa_status", "name": {"en": "AT&T USA Status 🔍", "es": "AT&T USA Status 🔍", "ru": "AT&T USA Status 🔍", "hi": "AT&T USA Status 🔍", "ar": "AT&T USA Status 🔍", "zh": "AT&T USA Status 🔍", "fr": "AT&T USA Status 🔍", "de": "AT&T USA Status 🔍", "pt": "AT&T USA Status 🔍", "tr": "AT&T USA Status 🔍"}},
    "tmobile_usa_status": {"price": 0.05, "api_id": "tmobile_usa_status", "name": {"en": "T-Mobile USA Status 🔍", "es": "T-Mobile USA Status 🔍", "ru": "T-Mobile USA Status 🔍", "hi": "T-Mobile USA Status 🔍", "ar": "T-Mobile USA Status 🔍", "zh": "T-Mobile USA Status 🔍", "fr": "T-Mobile USA Status 🔍", "de": "T-Mobile USA Status 🔍", "pt": "T-Mobile USA Status 🔍", "tr": "T-Mobile USA Status 🔍"}},
    "verizon_usa_status": {"price": 0.05, "api_id": "verizon_usa_status", "name": {"en": "Verizon USA Status 🔍", "es": "Verizon USA Status 🔍", "ru": "Verizon USA Status 🔍", "hi": "Verizon USA Status 🔍", "ar": "Verizon USA Status 🔍", "zh": "Verizon USA Status 🔍", "fr": "Verizon USA Status 🔍", "de": "Verizon USA Status 🔍", "pt": "Verizon USA Status 🔍", "tr": "Verizon USA Status 🔍"}},
    "hlr_lookup": {"price": 0.10, "api_id": "hlr_lookup", "name": {"en": "HLR Lookup 🔍", "es": "HLR Lookup 🔍", "ru": "HLR Lookup 🔍", "hi": "HLR Lookup 🔍", "ar": "HLR Lookup 🔍", "zh": "HLR Lookup 🔍", "fr": "HLR Lookup 🔍", "de": "HLR Lookup 🔍", "pt": "HLR Lookup 🔍", "tr": "HLR Lookup 🔍"}},
    "ping_sms": {"price": 0.05, "api_id": "ping_sms", "name": {"en": "Ping SMS 🔍", "es": "Ping SMS 🔍", "ru": "Ping SMS 🔍", "hi": "Ping SMS 🔍", "ar": "Ping SMS 🔍", "zh": "Ping SMS 🔍", "fr": "Ping SMS 🔍", "de": "Ping SMS 🔍", "pt": "Ping SMS 🔍", "tr": "Ping SMS 🔍"}},
    "ping_sms_pro": {"price": 0.10, "api_id": "ping_sms_pro", "name": {"en": "Ping SMS (Pro) 🔍", "es": "Ping SMS (Pro) 🔍", "ru": "Ping SMS (Pro) 🔍", "hi": "Ping SMS (Pro) 🔍", "ar": "Ping SMS (Pro) 🔍", "zh": "Ping SMS (Pro) 🔍", "fr": "Ping SMS (Pro) 🔍", "de": "Ping SMS (Pro) 🔍", "pt": "Ping SMS (Pro) 🔍", "tr": "Ping SMS (Pro) 🔍"}},
    "yandex_alice_info": {"price": 0.05, "api_id": "yandex_alice_info", "name": {"en": "Yandex Alice Info 🔍", "es": "Yandex Alice Info 🔍", "ru": "Yandex Alice Info 🔍", "hi": "Yandex Alice Info 🔍", "ar": "Yandex Alice Info 🔍", "zh": "Yandex Alice Info 🔍", "fr": "Yandex Alice Info 🔍", "de": "Yandex Alice Info 🔍", "pt": "Yandex Alice Info 🔍", "tr": "Yandex Alice Info 🔍"}},
    "japan_blacklist_status": {"price": 0.05, "api_id": "japan_blacklist_status", "name": {"en": "Japan Blacklist Status", "es": "Japan Blacklist Status", "ru": "Japan Blacklist Status", "hi": "Japan Blacklist Status", "ar": "Japan Blacklist Status", "zh": "Japan Blacklist Status", "fr": "Japan Blacklist Status", "de": "Japan Blacklist Status", "pt": "Japan Blacklist Status", "tr": "Japan Blacklist Status"}}
}

ALL_SERVICE_NAMES = {}
for s_key, s_data in SERVICES.items():
    for n in s_data["name"].values(): ALL_SERVICE_NAMES[n] = s_key
ALL_IMEI_SUB_NAMES = {}
for s_key, s_data in IMEI_SUB_SERVICES.items():
    for n in s_data["name"].values(): ALL_IMEI_SUB_NAMES[n] = s_key
ALL_GSX_SUB_NAMES = {}
for s_key, s_data in GSX_SUB_SERVICES.items():
    for n in s_data["name"].values(): ALL_GSX_SUB_NAMES[n] = s_key
ALL_OTHER_IMEI_SUB_NAMES = {}
for s_key, s_data in OTHER_IMEI_SUB_SERVICES.items():
    for n in s_data["name"].values(): ALL_OTHER_IMEI_SUB_NAMES[n] = s_key
ALL_OTHER_SUB_NAMES = {}
for s_key, s_data in OTHER_SUB_SERVICES.items():
    for n in s_data["name"].values(): ALL_OTHER_SUB_NAMES[n] = s_key

ALL_BUTTONS = {**ALL_SERVICE_NAMES, **ALL_IMEI_SUB_NAMES, **ALL_GSX_SUB_NAMES,
               **ALL_OTHER_IMEI_SUB_NAMES, **ALL_OTHER_SUB_NAMES}

SERVICE_DETAILS = {
    "fmi": "\n\n📝 <b>Example:</b>\nFind My iPhone: ON ⚠️\nFind My iPhone: OFF ✅\n\n🕒 <b>Delivery:</b> Instant",
    "icloud": "\n\n📝 <b>Example:</b>\n      Find My iPhone: ON ⚠️\n      iCloud Status: LOST ❌\n\n🕒 <b>Delivery:</b> 1 Minutes",
    "basic": "\n\n📝 <b>Example:</b>\nModel: iPhone 16 Pro Max\nRefurbished: No\nFind My iPhone: ON ⚠️\niCloud Status: LOST ❌\nBlacklist Status: Clean ✅\nSim-Lock: Unlocked ✅\n\n🕒 <b>Delivery:</b> Instant",
    "carrier": "\n\n📝 <b>Example:</b>\nModel: iPhone 17 Pro Max\nActivation Status: Activated\nFind My iPhone: ON ⚠️\niCloud Status: Clean ✅\nBlacklist Status: Blacklisted ❌\nSim-Lock: Locked ❌\n\n🕒 <b>Delivery:</b> Instant",
    "carrier_plus": "\n\n📝 <b>Example:</b>\nModel: iPhone 17 Pro Max\nPart Number: MFXH4LL/A\nBlacklist Status: Blacklisted ❌\nSim-Lock: Locked ❌\n\n🕒 <b>Delivery:</b> Instant",
    "max": "\n\n📝 <b>Example:</b>\nModel: iPhone 17 Pro Max\nMDM Status: OFF ✅\nBlacklisted By: VERIZON WIRELESS\nBlacklist Reason: Fraudulently Obtained (0026)\n\n🕒 <b>Delivery:</b> Instant",
    "imei_check": "\n\n📝 <b>Example:</b>\nModel: iPhone 15 Pro\nIMEI: 123456789012345\nStatus: Clean ✅\n\n🕒 <b>Delivery:</b> Instant",
    "gsx": "\n\n📝 <b>Example:</b>\nGSX Case ID: 123456789\nStatus: Completed\n\n🕒 <b>Delivery:</b> Instant",
    "other_imei": "\n\n📝 <b>Example:</b>\nIMEI: 123456789012345\nStatus: Unknown\n\n🕒 <b>Delivery:</b> Instant",
    "other": "\n\n📝 <b>Example:</b>\nCustom check requested.\n\n🕒 <b>Delivery:</b> Instant",
    "carrier_lite": "\n\n📝 <b>Example:</b>\nModel: IPHONE 8 64GB SPACE GRAY [A1905]\nModel Description: IPHONE 8 SPACE GRAY 64GB-ZDD\nRefurbished: No\nDemo Device: No\nFind My iPhone: ON ⚠️\nEstimated Purchase Date: May 14, 2018\nPurchased In: Austria\nNext Activation Policy ID: 324\nNext Activation Policy: Austria A1 Mobilkom\nSim-Lock: Locked ❌\n\n🕒 <b>Delivery:</b> Instant",
    "warranty_info": "\n\n📝 <b>Example:</b>\nModel: IPHONE 11 64GB BLACK [A2111]\nActivation: Activated\nTelephone Technical Support: Expired\nRepairs and Service Coverage: Active\nCoverage Status: Apple Limited Warranty (156 days remaining)\nCoverage End Date: February 06, 2021\nAppleCare Eligible: No\nReplaced Device: No\nEstimated Purchase Date: February 07, 2020\nRegistered: Y\nLoaner: N\n\n🕒 <b>Delivery:</b> 5-10 sec",
    "gsma_blacklist": "\n\n📝 <b>Example:</b>\nModel: APPLE IPHONE X (A1865)\nManufacturer: APPLE INC\nBlacklist Status: BLACKLISTED ❌\nBlacklisted By: SPRINT\nBlacklisted Country: US\nBlacklisted On: 14/04/2019 06:47:00\n\n🕒 <b>Delivery:</b> ~1 Minute",
    "icloud_sn": "\n\n📝 <b>Example:</b>\niCloud Status: Clean ✅\n\n🕒 <b>Delivery:</b> Instant",
    "part_number": "\n\n📝 <b>Example:</b>\nModel: iPhone 12\nPart Number: MGGU3CH/A\nPart Number Country: China mainland\nPart Type: Retail\n\n🕒 <b>Delivery:</b> 1-2 Minutes",
    "owner_id": "\n\n🌎 <b>All country supported</b>\n\n⚠️ This service has a LOW chance of receiving information. Payment charge per request.\n\n📝 <b>Example:</b>\n      Model: iPhone 12 Pro\n      IMEI: 356696XXXXXXXXX\n      IMEI2: 356696XXXXXXXXX\n      Serial Number: F17XXXXXXX94\n      Owner Details: Not Found\n\n\n      Model: iPhone 12 Pro\n      IMEI: 356696XXXXXXXXX\n      IMEI2: 356696XXXXXXXXX\n      Serial Number: F17XXXXXXX94\n      Owner Details:\n      Cathy DreXXXXs\n      caXXXXXXy@gmail.com\n      +1707XXXXX97\n      United States\n\n\n🕒 <b>Delivery:</b>\n      90% of orders are done within 1-5 minutes\n      10% of orders are done within 2-3 hours\n      5% of orders are done within 1-5 days",
    "chimaera": "\n\nA Chimaera device is a device that Apple has blocked at the activation-server level. Even if the hardware is fine, the device may fail activation and cannot be set up, which can make it effectively unusable.\n\n📝 <b>Example:</b>\n      Model: iPhone 12 Pro\n      IMEI/Serial: 353075115XXXXX\n      Chimaera Locked: Yes ⚠️\n\n🕒 <b>Delivery:</b> Instant",
    "gsx_iccid": "\n\n📝 <b>Example:</b>\nConfig Description: IPHONE 16 PROMAX,NAJP,256GB,DSRT TTNM\nModel Description: IPHONE 16 PRO MAX\nIMEI: 3510377900XXXXX\nIMEI2: 35103779XXXXXXX\nSerial: L37Q0XXXXX\nConfig Code: 0050CJ\nProduct Line: 301048\nProduct Version: 26.3.1\nLast Unbrick Os Build: 23D8133\nCSN/CSN2/EID: 89049032007408882XXXXXXXXX\nCarrier Name: Carrier\nFirst Activation Date: 2025-01-18T20:09:36Z\nPurchase Date: 2025-01-19T00:00:00Z\nRegistration Date: 2025-01-18T00:00:00Z\nLast Restore Date: 2026-03-14T13:20:51Z\nUnlock Date: 2025-04-18T20:16:59Z\nUnlocked: Yes\nApplied Activation Policy: 10 - Unlock.\nInitial Activation Policy: 10 - Unlock.\nNext Tether Policy: 10 - Unlock.\nPersonalized: No\nPart Covered: No\nLabor Covered: No\nOnsite Coverage: No\nLimited Warranty: No\nWarranty Status: Out Of Warranty (No Coverage)\nSold To Name: APPLE\nPurchase Country: Japan\nMDM Lock: OFF\niCloud Lock: OFF\n\nCases History:\n1028446XXXXX 2026-03-14T18:55:34.174Z Summary: Блокировка активации потребителем\n1027139XXXXX 2025-10-04T16:30:13.521Z Summary: Consumer Activation Lock\n1027139XXXXX 2025-10-04T16:14:59.673Z Summary: Аккаунт Apple отключен\nReplacement Details: Not Found\n\n🕒 <b>Delivery:</b> Instant",
    "sold_by": "\n\n📝 <b>Example:</b>\nConfig Description: IPHONE 17 PRO MAX,NAUS,256GB,BLU\nModel Description: IPHONE 17 PRO MAX DBLUE 256GB-USA\nConfig Code: 006FR9\nProduct Line: 301246\nProduct Version: 26.5.1\nLast Unbrick Os Build: 23F81\nCSN/CSN2/EID: 890490320200088856002XXXXXXXX0\nWireless Mac Address: 8C8283181E9F\nPurchase Date: 2026-08-22T00:00:00Z\nUnlocked: No\nInitial Activation Policy: 2387 - US Verizon Locked Policy\nNext Tether Policy: 2387 - US Verizon Locked Policy\nPersonalized: No\nPart Covered: Yes\nLabor Covered: Yes\nOnsite Coverage: No\nLimited Warranty: Yes\nCoverage Start Date: 2026-08-22T00:00:00Z\nCoverage End Date: 2027-08-21T23:59:59Z\nWarranty Status: Apple Limited Warranty\nWarranty Days Remaining: 374\nSold To Name: VERIZON WIRELESS - 1000040\nPurchase Country: United States\nMDM Lock: OFF\niCloud Lock: OFF\n\n🕒 <b>Delivery:</b> Instant",
    "case_history_repairs": "\n\n📝 <b>Example:</b>\n      Description: IPHONE XR WHITE 64GB-RUS\n      Warranty Status: Out Of Warranty\n      Estimated Purchase Date: 2020-11-15\n      Replaced Device: No\n\n      Case Details\n      Status: RESOL\n      Last Modified: 2022-05-12T08:20:15.675Z\n      Id: 10169899XXXX\n      Issue Code: ICL0013\n      Issue Desc: Login issue (iCloud Only)\n      Component Code: ICL001\n      Component Desc: iCloud Account\n      Title: log in to Apple ID issue\n      Support Topic: log in to Apple ID issue\n      Status Description: Closed\n      Status: RESOL\n      Last Modified: 2022-05-13T10:14:37.072Z\n      Id: 10169983XXXX\n      Issue Code: ID05-02\n      Issue Desc: How to - reset/change password\n      Component Code: ID05\n      Component Desc: Forgotten / Change Password\n      Title: forgoten id password\n      Support Topic: forgoten id password\n      Status Description: Closed\n      Status: RESOL\n      Last Modified: 2022-05-12T08:01:55.081Z\n      Id: 10169898XXXX\n      Issue Code: NTI0302\n      Issue Desc: Law Enforcement Referral\n      Component Code: NTI03\n      Component Desc: Law Enforcement Inquiry or Referral\n      Title: Referral\n      Support Topic: Referral\n      Status Description: Closed\n\n      No Repair Found!\n\n🕒 <b>Delivery:</b> Instant",
    "full_gsx": "\n\n📝 <b>Example:</b>\nModel Description: IPHONE 13 ROW 256GB MIDNIGHT\nProduct Description: IPHONE 13 MIDNIGHT 256GB-YPT\nConfig Code: 000DT6\nPart Number: MLQ63QL/A\nModel Number: A2633\nProduct Line: 300428\nProduct Version: 16.2\nLast Unbrick Os Build: 20C65\nWireless Mac Address: A4C6F08AAXXX\nCSN/CSN2/EID: 89049032007008882600124255XXXXXX\nPurchase Date: 2023-01-14T00:00:00Z\nFirst Activation Date: 2023-01-14T17:19:49Z\nLast Restore Date: 2023-01-22T03:32:04Z\nUnlock Date: 2023-01-14T17:19:49Z\nWarranty Status Description: Out Of Warranty (No Coverage)\nLoaner: False\nUnlocked: True\nPersonalized: False\nPart Covered: False\nLabor Covered: False\nOnsite Coverage: False\nLimited Warranty: False\nInitial Activation Policy: 10 - Unlock.\nApplied Activation Policy: 10 - Unlock.\nNext Tether Policy: 10 - Unlock.\nSold To Name: TD SYNNEX SPAIN S.L\nPurchase Country: Spain\nMDM Lock: OFF\niCloud Lock: ON\niCloud Status: LOST\nSIM Lock: Unlocked\nCases: Not Found\nReplacement: Not Found\n\n🕒 <b>Delivery:</b> 1-5 Minutes",
    "macos_on_off": "\n\n📝 <b>Example:</b>\nDescription: MACBOOK AIR (13-INCH, 2017)\nFind My Mac: ON\n\n🕒 <b>Delivery:</b> Instant",
    "macos_clean_lost": "\n\n📝 <b>Example:</b>\nModel: MACBOOK PRO (13-INCH, M1, 2020)\nFind My Mac: ON\niCloud Status: LOST\n\n🕒 <b>Delivery:</b> Instant",
    "gsx_carrier": "\n\n📝 <b>Example:</b>\nModel: iPhone 12 Pro (A2406)\nIMEI/SN: 35668711XXXXXXX\nFind My iPhone: ON\nNext Tether Policy: Canada Bell/MTS Locked Policy\nSim-Lock: Locked\n\n🕒 <b>Delivery:</b> 1-2 Minutes",
    "mdm_on_off": "\n\n📝 <b>Example:</b>\n      Model: IPHONE 11\n      MDM Enrollment Status: ON ⚠️\n\n🕒 <b>Delivery:</b> Instant",
    "repairs_replacement": "\n\n📝 <b>Example:</b>\nReplacement History (2)\nH0DH5XXXN710 35287111XXX0825 352871112XXX013 35287111XXX082 Active \nFK1CDXXXN710 35392510XXX1894 353925109XXX607 35392510XXX189 Original 2022-03-04T08:%i:1646383653Z \n\n🕒 <b>Delivery:</b> Instant",
    "case_history": "\n\n📝 <b>Example:</b>\n      Description: IPHONE XR WHITE 64GB-RUS\n      Warranty Status: Out Of Warranty\n      Estimated Purchase Date: 2020-11-15\n      Replaced Device: No\n\n      Case Details\n      Status: RESOL\n      Last Modified: 2022-05-12T08:20:15.675Z\n      Id: 10169899XXXX\n      Issue Code: ICL0013\n      Issue Desc: Login issue (iCloud Only)\n      Component Code: ICL001\n      Component Desc: iCloud Account\n      Title: log in to Apple ID issue\n      Support Topic: log in to Apple ID issue\n      Status Description: Closed\n      Status: RESOL\n      Last Modified: 2022-05-13T10:14:37.072Z\n      Id: 10169983XXXX\n      Issue Code: ID05-02\n      Issue Desc: How to - reset/change password\n      Component Code: ID05\n      Component Desc: Forgotten / Change Password\n      Title: forgoten id password\n      Support Topic: forgoten id password\n      Status Description: Closed\n      Status: RESOL\n      Last Modified: 2022-05-12T08:01:55.081Z\n      Id: 10169898XXXX\n      Issue Code: NTI0302\n      Issue Desc: Law Enforcement Referral\n      Component Code: NTI03\n      Component Desc: Law Enforcement Inquiry or Referral\n      Title: Referral\n      Support Topic: Referral\n      Status Description: Closed\n\n🕒 <b>Delivery:</b> Instant",
    "samsung_info": "\n\n📝 <b>Example:</b>\nModel: Samsung Galaxy S24 Ultra\nStatus: Clean ✅\n\n🕒 <b>Delivery:</b> Instant",
    "samsung_onoff": "\n\n📝 <b>Example:</b>\nSamsung Find My Mobile: ON ⚠️\n\n🕒 <b>Delivery:</b> Instant",
    "xiaomi_onoff": "\n\n📝 <b>Example:</b>\nXiaomi Find Device: ON ⚠️\n\n🕒 <b>Delivery:</b> Instant",
    "xiaomi_clean_lost": "\n\n📝 <b>Example:</b>\nXiaomi Status: Clean ✅\n\n🕒 <b>Delivery:</b> Instant",
    "motorola_info": "\n\n📝 <b>Example:</b>\nModel: Motorola Edge 50 Pro\nStatus: Clean ✅\n\n🕒 <b>Delivery:</b> Instant",
    "lenovo_info": "\n\n📝 <b>Example:</b>\nModel: Lenovo Legion Y70\nStatus: Clean ✅\n\n🕒 <b>Delivery:</b> Instant",
    "pixel_info": "\n\n📝 <b>Example:</b>\nModel: Google Pixel 8 Pro\nStatus: Clean ✅\n\n🕒 <b>Delivery:</b> Instant",
    "huawei_info": "\n\n📝 <b>Example:</b>\nModel: Huawei P60 Pro\nStatus: Clean ✅\n\n🕒 <b>Delivery:</b> Instant",
    "honor_info": "\n\n📝 <b>Example:</b>\nModel: Honor Magic 6 Pro\nStatus: Clean ✅\n\n🕒 <b>Delivery:</b> Instant",
    "att_usa_status": "\n\nChecks the Finance Status for devices from AT&T USA\n\n📝 <b>Example:</b>\n      IMEI Number: ###############\n      Status: Unpaid Bills\n\n🕒 <b>Delivery:</b> Instant",
    "tmobile_usa_status": "\n\n📝 <b>Example:</b>\n      Model: iPhone XR\n      Manufacturer: Apple\n      IMEI: 356432101550791\n      IMEI2: 356432101515182\n      eSIM CSN: 89049032004008882600017923888123\n      eSIM Supported: Yes\n      Status: Blocked\n      Status Description: Reported unrecoverable by insurance Unrecoverable - Insurance\n\n🕒 <b>Delivery:</b> Instant",
    "verizon_usa_status": "\n\n📝 <b>Example:</b>\n      Model: GALAXY S22 ULTRA 512 BLK\n      IMEI: XXXXXXXXXXXXXXX\n      Status: Clean\n      Part Number: SMS908UZKFV\n      Product ID: DEV17760008\n      Device SKU: SKU5030023\n\n🕒 <b>Delivery:</b> Instant",
    "hlr_lookup": "\n\n📝 <b>Example:</b>\n      Number: +447773398984\n      Original Network Name: Orange (Everything Everywhere Limited)\n      Original Country Name: United Kingdom\n      Original Country Prefix: +44\n      Ported: Yes ✅\n      Ported Network Name: 3 (Hutchison 3G UK Ltd)\n      Ported Country Name: United Kingdom\n      Ported Country Prefix: +44\n      Roaming: No ✅\n      Local Time: Europe/London: GMT+01:00\n      Status: CONNECTED ✅\n\n🕒 <b>Delivery:</b> Instant",
    "ping_sms": "\n\n📝 <b>Example:</b>\n      Number: +79XXXXXXX76\n      Network Name: МТС\n      Country: Россия\n      Region: г.Санкт-Петербург и Ленинградская область\n      Status: Online ✅\n      Status Description: Ping-SMS Delivered\n\n🕒 <b>Delivery:</b> Instant",
    "ping_sms_pro": "\n\n📝 <b>Example:</b>\n      Number: +79XXXXXXX76\n      Network Name: МТС\n      Country: Россия\n      Region: г.Санкт-Петербург и Ленинградская область\n      Status: Online ✅\n      Status Description: Ping-SMS Delivered\n\n🕒 <b>Delivery:</b> Instant",
    "yandex_alice_info": "\n\n📝 <b>Example:</b>\nSoon..\n\n🕒 <b>Delivery:</b> Instant",
    "japan_blacklist_status": "\n\n📝 <b>Example:</b>\n\n\n🕒 <b>Delivery:</b> Instant"
}

# ==========================================
# 5. USER BOT KEYBOARDS
# ==========================================
_menu_cache = {}
_menu_cache_lock = threading.Lock()

def main_menu(lang, is_admin_flag=False):
    key = (lang, is_admin_flag)
    with _menu_cache_lock:
        cached = _menu_cache.get(key)
        if cached is not None:
            return cached
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.row(
        types.KeyboardButton(SERVICES["fmi"]["name"].get(lang, SERVICES["fmi"]["name"]["en"])),
        types.KeyboardButton(SERVICES["icloud"]["name"].get(lang, SERVICES["icloud"]["name"]["en"]))
    )
    markup.row(
        types.KeyboardButton(SERVICES["basic"]["name"].get(lang, SERVICES["basic"]["name"]["en"])),
        types.KeyboardButton(SERVICES["carrier"]["name"].get(lang, SERVICES["carrier"]["name"]["en"]))
    )
    markup.row(
        types.KeyboardButton(SERVICES["carrier_plus"]["name"].get(lang, SERVICES["carrier_plus"]["name"]["en"])),
        types.KeyboardButton(SERVICES["max"]["name"].get(lang, SERVICES["max"]["name"]["en"]))
    )
    markup.row(
        types.KeyboardButton(SERVICES["imei_check"]["name"].get(lang, SERVICES["imei_check"]["name"]["en"])),
        types.KeyboardButton(SERVICES["gsx"]["name"].get(lang, SERVICES["gsx"]["name"]["en"]))
    )
    markup.row(
        types.KeyboardButton(SERVICES["other_imei"]["name"].get(lang, SERVICES["other_imei"]["name"]["en"])),
        types.KeyboardButton(SERVICES["other"]["name"].get(lang, SERVICES["other"]["name"]["en"]))
    )
    markup.row(
        types.KeyboardButton(get_text(lang, 'btn_account')),
        types.KeyboardButton(get_text(lang, 'btn_lang'))
    )
    markup.row(
        types.KeyboardButton(get_text(lang, 'btn_support'))
    )
    if is_admin_flag:
        markup.row(types.KeyboardButton("👑 Admin Panel"))
    with _menu_cache_lock:
        _menu_cache[key] = markup
    return markup

def imei_sub_menu(lang):
    key = ('imei', lang)
    with _menu_cache_lock:
        c = _menu_cache.get(key)
        if c is not None: return c
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    buttons = []
    for k, data in IMEI_SUB_SERVICES.items():
        buttons.append(types.KeyboardButton(data["name"].get(lang, data["name"]["en"])))
    buttons.append(types.KeyboardButton(get_text(lang, 'btn_back_menu')))
    markup.add(*buttons)
    with _menu_cache_lock:
        _menu_cache[key] = markup
    return markup

def gsx_sub_menu(lang):
    key = ('gsx', lang)
    with _menu_cache_lock:
        c = _menu_cache.get(key)
        if c is not None: return c
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    buttons = []
    for k, data in GSX_SUB_SERVICES.items():
        buttons.append(types.KeyboardButton(data["name"].get(lang, data["name"]["en"])))
    buttons.append(types.KeyboardButton(get_text(lang, 'btn_back_menu')))
    markup.add(*buttons)
    with _menu_cache_lock:
        _menu_cache[key] = markup
    return markup

def other_imei_sub_menu(lang):
    key = ('other_imei', lang)
    with _menu_cache_lock:
        c = _menu_cache.get(key)
        if c is not None: return c
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    buttons = []
    for k, data in OTHER_IMEI_SUB_SERVICES.items():
        buttons.append(types.KeyboardButton(data["name"].get(lang, data["name"]["en"])))
    buttons.append(types.KeyboardButton(get_text(lang, 'btn_back_menu')))
    markup.add(*buttons)
    with _menu_cache_lock:
        _menu_cache[key] = markup
    return markup

def other_sub_menu(lang):
    key = ('other', lang)
    with _menu_cache_lock:
        c = _menu_cache.get(key)
        if c is not None: return c
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    buttons = []
    for k, data in OTHER_SUB_SERVICES.items():
        buttons.append(types.KeyboardButton(data["name"].get(lang, data["name"]["en"])))
    buttons.append(types.KeyboardButton(get_text(lang, 'btn_back_menu')))
    markup.add(*buttons)
    with _menu_cache_lock:
        _menu_cache[key] = markup
    return markup

def service_info_keyboard(lang, service_key, expanded=False):
    markup = types.InlineKeyboardMarkup()
    btn_text = get_text(lang, 'less_info') if expanded else get_text(lang, 'more_info')
    btn = types.InlineKeyboardButton(btn_text, callback_data=f"toggle_{service_key}_{0 if expanded else 1}")
    markup.add(btn)
    return markup

def language_menu():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("🇬🇧 English", callback_data="set_lang_en"),
        types.InlineKeyboardButton("🇷🇺 Русский", callback_data="set_lang_ru"),
        types.InlineKeyboardButton("🇪🇸 Español", callback_data="set_lang_es"),
        types.InlineKeyboardButton("🇮🇳 हिन्दी", callback_data="set_lang_hi"),
        types.InlineKeyboardButton("🇸🇦 العربية", callback_data="set_lang_ar"),
        types.InlineKeyboardButton("🇨🇳 中文", callback_data="set_lang_zh"),
        types.InlineKeyboardButton("🇫🇷 Français", callback_data="set_lang_fr"),
        types.InlineKeyboardButton("🇩🇪 Deutsch", callback_data="set_lang_de"),
        types.InlineKeyboardButton("🇵🇹 Português", callback_data="set_lang_pt"),
        types.InlineKeyboardButton("🇹🇷 Türkçe", callback_data="set_lang_tr"),
    )
    return markup

def paid_button_kb(lang, deposit_id):
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("❌ Cancel", callback_data=f"dep_cancel_{deposit_id}"),
        types.InlineKeyboardButton("✅ I HAVE PAID", callback_data=f"paid_{deposit_id}")
    )
    return markup

# ==========================================
# 6. ADMIN BOT KEYBOARDS
# ==========================================
def admin_main_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.row(
        types.KeyboardButton("📊 Stats"),
        types.KeyboardButton("🏆 Top Users")
    )
    markup.row(
        types.KeyboardButton("👥 Users List"),
        types.KeyboardButton("🔍 User Info")
    )
    markup.row(
        types.KeyboardButton("💵 Add Balance"),
        types.KeyboardButton("💸 Remove Balance")
    )
    markup.row(
        types.KeyboardButton("✏️ Set Balance"),
        types.KeyboardButton("🗑 Delete User")
    )
    markup.row(
        types.KeyboardButton("📢 Broadcast"),
        types.KeyboardButton("🛠️ Maintenance")
    )
    markup.row(
        types.KeyboardButton("📦 Download DB"),
        types.KeyboardButton("📦 Download DB (Raw .db)"),
    )
    markup.row(
        types.KeyboardButton("🔑 TronGrid Status")
    )
    return markup

def admin_cancel_kb():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(types.KeyboardButton("❌ Cancel"))
    return markup

def is_admin(user_id):
    return user_id in ADMIN_IDS

def deposit_review_kb(deposit_id):
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("✅ APPROVE", callback_data=f"dep_approve_{deposit_id}"),
        types.InlineKeyboardButton("❌ REJECT", callback_data=f"dep_reject_{deposit_id}")
    )
    return markup

# ==========================================
# 7. QR CODE
# ==========================================
def generate_qr(data: str):
    qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_L, box_size=10, border=4)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    bio = BytesIO()
    bio.name = 'qr.png'
    img.save(bio, 'PNG')
    bio.seek(0)
    return bio

# ==========================================
# 8. TRONGRID HELPERS
# ==========================================
_http = requests.Session()
_trongrid_lock = threading.Lock()
_trongrid_key_index = 0
_trongrid_key_state = {i: [0, 0.0] for i in range(len(TRONGRID_API_KEYS))}
_TRONGRID_MAX_FAILURES = 3
_TRONGRID_COOLDOWN = 300

def _get_next_trongrid_key():
    global _trongrid_key_index
    with _trongrid_lock:
        total = len(TRONGRID_API_KEYS)
        if total == 0: return None, None
        now = time.time()
        for _ in range(total):
            idx = _trongrid_key_index % total
            _trongrid_key_index = (_trongrid_key_index + 1) % total
            cnt, ts = _trongrid_key_state.get(idx, [0, 0.0])
            if cnt >= _TRONGRID_MAX_FAILURES and (now - ts) < _TRONGRID_COOLDOWN:
                continue
            return idx, TRONGRID_API_KEYS[idx]
        idx = _trongrid_key_index % total
        _trongrid_key_index = (_trongrid_key_index + 1) % total
        return idx, TRONGRID_API_KEYS[idx]

def _mark_trongrid_key(idx, ok):
    if idx is None: return
    with _trongrid_lock:
        if ok:
            _trongrid_key_state[idx] = [0, 0.0]
        else:
            cnt, _ = _trongrid_key_state.get(idx, [0, 0.0])
            _trongrid_key_state[idx] = [cnt + 1, time.time()]

def _trongrid_headers(api_key=None):
    h = {'Accept': 'application/json'}
    if api_key: h['TRON-PRO-API-KEY'] = api_key
    return h

def _trongrid_request(path, params=None, max_attempts=None):
    if not TRONGRID_API_KEYS: return None
    if max_attempts is None: max_attempts = max(len(TRONGRID_API_KEYS) * 2, 2)
    url = f'{TRONGRID_API_URL}{path}'
    last_err = None
    for _ in range(max_attempts):
        idx, key = _get_next_trongrid_key()
        if idx is None: break
        try:
            r = _http.get(url, headers=_trongrid_headers(key), params=params, timeout=15)
            if r.status_code == 200:
                _mark_trongrid_key(idx, True)
                return r.json()
            if r.status_code in (403, 429, 500, 502, 503, 504):
                _mark_trongrid_key(idx, False)
                last_err = f"HTTP {r.status_code}"
                print(f"[TronGrid] Key #{idx+1} failed ({last_err}), rotating...")
                continue
            _mark_trongrid_key(idx, True)
            return None
        except requests.exceptions.RequestException as e:
            _mark_trongrid_key(idx, False)
            last_err = str(e)
            print(f"[TronGrid] Key #{idx+1} network error: {e}, rotating...")
            continue
    print(f"[TronGrid] All keys failed. Last error: {last_err}")
    return None

def verify_txid_on_chain(txid, expected_currency=None):
    try:
        return _trongrid_request(f'/v1/transactions/{txid}')
    except Exception as e:
        print(f"verify_txid error: {e}")
        return None

def check_trc20_transfers(address, min_timestamp=None, limit=50, contract_address=None):
    params = {'only_to': 'true', 'limit': limit}
    if contract_address: params['contract_address'] = contract_address
    if min_timestamp: params['min_timestamp'] = min_timestamp
    return _trongrid_request(f'/v1/accounts/{address}/transactions/trc20', params=params)

def get_trongrid_key_status():
    with _trongrid_lock:
        return [
            {
                'index': i + 1,
                'suffix': ('...' + k[-6:]) if k else 'EMPTY',
                'failures': _trongrid_key_state.get(i, [0, 0.0])[0],
                'last_fail_ago': (time.time() - _trongrid_key_state.get(i, [0, 0.0])[1])
            }
            for i, k in enumerate(TRONGRID_API_KEYS)
        ]

# ==========================================
# GLOBAL COMMANDS
# ==========================================
@bot.message_handler(commands=['cancel'])
def cancel_command(message):
    user_id = message.from_user.id
    _, lang = get_user(user_id)
    try:
        bot.clear_step_handler_by_chat_id(message.chat.id)
    except Exception:
        pass
    clear_pending(user_id)
    bot.send_message(user_id,
                     get_text(lang, 'cancel_prompt'),
                     parse_mode='HTML',
                     reply_markup=main_menu(lang, user_id in ADMIN_IDS))

@bot.message_handler(commands=['mydeposit'])
def my_deposit_command(message):
    user_id = message.from_user.id
    _, lang = get_user(user_id)
    active = get_active_deposit_info(user_id)
    if not active:
        bot.send_message(user_id, get_text(lang, 'no_active_deposit'),
                         reply_markup=main_menu(lang, user_id in ADMIN_IDS))
        return
    dep_id, dep_status = active
    dep = get_deposit(dep_id)
    if dep_status == 'pending':
        text = get_text(lang, 'existing_deposit_reviewing', amount=dep['amount'], currency=dep['currency'], dep_id=dep_id)
        markup = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("⬅️ Back", callback_data="back_to_acc"))
    else:
        text = get_text(lang, 'existing_deposit_awaiting', amount=dep['amount'], currency=dep['currency'], dep_id=dep_id)
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(
            types.InlineKeyboardButton("✅ I PAID — Submit Proof", callback_data=f"paid_{dep_id}"),
            types.InlineKeyboardButton("❌ Cancel Pending Deposit", callback_data=f"dep_cancel_{dep_id}"),
            types.InlineKeyboardButton("⬅️ Back", callback_data="back_to_acc")
        )
    bot.send_message(user_id, text, parse_mode='HTML', reply_markup=markup)

# ==========================================
#          USER BOT HANDLERS
# ==========================================
@bot.message_handler(commands=['start'])
def send_welcome(message):
    user_id = message.from_user.id
    if user_id not in ADMIN_IDS:
        maint = get_bot_setting('maintenance')
        if maint == 'on':
            send_maintenance(user_id)
            return
    clear_pending(user_id)
    try:
        bot.clear_step_handler_by_chat_id(message.chat.id)
    except Exception:
        pass
    first_name = message.from_user.first_name or "User"
    balance, lang = get_user(user_id)
    is_adm = user_id in ADMIN_IDS
    try:
        bot.send_message(user_id, get_text(lang, 'welcome', name=first_name, balance=balance, user_id=user_id, support=SUPPORT_USERNAME), reply_markup=main_menu(lang, is_adm))
    except Exception as e:
        print(f"Error in /start: {e}")

@bot.message_handler(func=lambda m: m.text in [get_text(l, 'btn_account') for l in LANGUAGES.keys()] + ["👤 My Account"])
def my_account(message):
    user_id = message.from_user.id
    if user_id not in ADMIN_IDS:
        maint = get_bot_setting('maintenance')
        if maint == 'on':
            send_maintenance(user_id, short=True)
            return
    clear_pending(user_id)
    balance, lang = get_user(user_id)
    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton(get_text(lang, 'deposit'), callback_data="deposit_start"),
        types.InlineKeyboardButton("⬅️ Back", callback_data="back_to_menu_inline")
    )
    bot.send_message(user_id, get_text(lang, 'my_account', user_id=user_id, balance=balance), reply_markup=markup)

@bot.message_handler(func=lambda m: m.text == "👑 Admin Panel")
def admin_panel_from_user(message):
    user_id = message.from_user.id
    clear_pending(user_id)
    _, lang = get_user(user_id)
    if user_id not in ADMIN_IDS:
        bot.send_message(user_id, "⛔ Access denied.", reply_markup=main_menu(lang, False))
        return
    bot.send_message(user_id, get_text(lang, 'admin_panel_info'), reply_markup=main_menu(lang, True))

@bot.message_handler(func=lambda m: m.text in [get_text(l, 'btn_support') for l in LANGUAGES.keys()] + ["🆘 Support"])
def support_handler(message):
    user_id = message.from_user.id
    if user_id not in ADMIN_IDS:
        maint = get_bot_setting('maintenance')
        if maint == 'on':
            send_maintenance(user_id, short=True)
            return
    clear_pending(user_id)
    _, lang = get_user(user_id)
    is_adm = user_id in ADMIN_IDS
    bot.send_message(
        user_id,
        get_text(lang, 'support_msg', support=SUPPORT_DISPLAY),
        reply_markup=main_menu(lang, is_adm),
        parse_mode='HTML',
        disable_web_page_preview=True
    )

@bot.message_handler(func=lambda m: m.text in [get_text(l, 'btn_lang') for l in LANGUAGES.keys()] + ["🌐 Language"])
def change_language(message):
    user_id = message.from_user.id
    if user_id not in ADMIN_IDS:
        maint = get_bot_setting('maintenance')
        if maint == 'on':
            send_maintenance(user_id, short=True)
            return
    clear_pending(user_id)
    _, lang = get_user(user_id)
    bot.send_message(user_id, get_text(lang, 'lang_select'), reply_markup=language_menu())

@bot.callback_query_handler(func=lambda c: c.data.startswith('set_lang_'))
def callback_set_lang(call):
    if not verify_callback_owner(call): return
    user_id = call.from_user.id
    lang_code = call.data.replace('set_lang_', '')
    if lang_code in LANGUAGES:
        update_user_lang(user_id, lang_code)
        balance, _ = get_user(user_id)
        is_adm = user_id in ADMIN_IDS
        bot.answer_callback_query(call.id, get_text(lang_code, 'lang_changed'))
        bot.send_message(user_id, get_text(lang_code, 'welcome', name=call.from_user.first_name, balance=balance, user_id=user_id, support=SUPPORT_USERNAME), reply_markup=main_menu(lang_code, is_adm))
    else:
        bot.answer_callback_query(call.id, get_text('en', 'lang_unavailable'), show_alert=True)

@bot.callback_query_handler(func=lambda c: c.data == 'back_to_menu_inline')
def back_to_menu_inline(call):
    if not verify_callback_owner(call): return
    bot.answer_callback_query(call.id)
    user_id = call.from_user.id
    clear_pending(user_id)
    _, lang = get_user(user_id)
    is_adm = user_id in ADMIN_IDS
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    bot.send_message(user_id, "Main Menu 👇", reply_markup=main_menu(lang, is_adm))

@bot.callback_query_handler(func=lambda c: c.data == 'deposit_start')
def deposit_start(call):
    if not verify_callback_owner(call): return
    bot.answer_callback_query(call.id)
    user_id = call.from_user.id
    _, lang = get_user(user_id)
    active = get_active_deposit_info(user_id)
    if active:
        dep_id, dep_status = active
        dep = get_deposit(dep_id)
        if dep_status == 'pending':
            text = get_text(lang, 'existing_deposit_reviewing', amount=dep['amount'], currency=dep['currency'], dep_id=dep_id)
            markup = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("⬅️ Back", callback_data="back_to_acc"))
        else:
            text = get_text(lang, 'existing_deposit_awaiting', amount=dep['amount'], currency=dep['currency'], dep_id=dep_id)
            markup = types.InlineKeyboardMarkup(row_width=1)
            markup.add(
                types.InlineKeyboardButton("✅ I PAID — Submit Proof", callback_data=f"paid_{dep_id}"),
                types.InlineKeyboardButton("❌ Cancel Pending Deposit", callback_data=f"dep_cancel_{dep_id}"),
                types.InlineKeyboardButton("⬅️ Back", callback_data="back_to_acc")
            )
        try:
            bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text=text, parse_mode='HTML', reply_markup=markup)
        except Exception as e:
            if "message is not modified" not in str(e): print(f"edit error: {e}")
        return
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("💎 USDT (TRC20)", callback_data="dep_curr_USDT"),
        types.InlineKeyboardButton("💎 USDC (TRC20)", callback_data="dep_curr_USDC"),
        types.InlineKeyboardButton(get_text(lang, 'back'), callback_data="back_to_acc")
    )
    try:
        bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text=get_text(lang, 'select_currency'), reply_markup=markup)
    except Exception as e:
        if "message is not modified" not in str(e): print(f"edit error: {e}")

@bot.callback_query_handler(func=lambda c: c.data.startswith('dep_cancel_'))
def cancel_pending_deposit_handler(call):
    if not verify_callback_owner(call): return
    user_id = call.from_user.id
    _, lang = get_user(user_id)
    dep_id = int(call.data.replace('dep_cancel_', ''))
    dep = get_deposit(dep_id)
    if not dep or dep['user_id'] != user_id:
        bot.answer_callback_query(call.id, "❌ Deposit not found.", show_alert=True)
        return
    if dep['status'] != 'awaiting_proof':
        bot.answer_callback_query(call.id, "⏳ Cannot cancel — already submitted or processed.", show_alert=True)
        return
    cancel_awaiting_deposit(user_id)
    bot.answer_callback_query(call.id, "✔️ Payment cancelled successfully", show_alert=False)
    try:
        bot.edit_message_reply_markup(chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=None)
    except Exception as e:
        print(f"Error removing inline keyboard: {e}")
    is_adm = user_id in ADMIN_IDS
    bot.send_message(user_id, "🏠 <b>Main Menu</b>", parse_mode='HTML', reply_markup=main_menu(lang, is_adm))

@bot.callback_query_handler(func=lambda c: c.data.startswith('dep_curr_'))
def deposit_currency(call):
    if not verify_callback_owner(call): return
    bot.answer_callback_query(call.id)
    user_id = call.from_user.id
    _, lang = get_user(user_id)
    currency = call.data.replace('dep_curr_', '')
    markup = types.InlineKeyboardMarkup(row_width=3)
    amounts = ['5', '10', '15', '20', '30', '50', '100', '150', '200']
    buttons = [types.InlineKeyboardButton(f"${amt}", callback_data=f"dep_amt_{currency}_{amt}") for amt in amounts]
    markup.add(*buttons)
    markup.add(
        types.InlineKeyboardButton(get_text(lang, 'other_amount'), callback_data=f"dep_other_{currency}"),
        types.InlineKeyboardButton(get_text(lang, 'back'), callback_data="deposit_start")
    )
    try:
        bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text=get_text(lang, 'choose_amount'), reply_markup=markup)
    except Exception as e:
        if "message is not modified" not in str(e): print(f"edit error: {e}")

@bot.callback_query_handler(func=lambda c: c.data.startswith('dep_amt_'))
def deposit_amount(call):
    if not verify_callback_owner(call): return
    user_id = call.from_user.id
    _, lang = get_user(user_id)
    parts = call.data.split('_')
    currency = parts[2]; amount = float(parts[3])
    bot.answer_callback_query(call.id)
    try:
        bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text=get_text(lang, 'creating_invoice'))
    except Exception as e:
        if "message is not modified" not in str(e): print(f"edit error: {e}")
    threading.Thread(target=_send_payment_qr, args=(user_id, lang, currency, amount, call.message.message_id), daemon=True).start()

@bot.callback_query_handler(func=lambda c: c.data.startswith('dep_other_'))
def deposit_other(call):
    if not verify_callback_owner(call): return
    bot.answer_callback_query(call.id)
    user_id = call.from_user.id
    _, lang = get_user(user_id)
    currency = call.data.replace('dep_other_', '')
    bot.send_message(user_id, get_text(lang, 'custom_prompt'), parse_mode='HTML')
    safe_register_step(call.message.chat.id, process_custom_amount, currency)

def process_custom_amount(message, currency):
    user_id = message.from_user.id
    _, lang = get_user(user_id)
    text = (message.text or '').strip()
    if _is_nav_button(text, lang):
        clear_pending(user_id)
        handle_services(message)
        return
    if text.startswith('/') or text.lower() in ('cancel',):
        bot.send_message(user_id, get_text(lang, 'proof_cancelled'), parse_mode='HTML', reply_markup=main_menu(lang, user_id in ADMIN_IDS))
        return
    try:
        amt = float(text)
        if amt < 1: raise ValueError
    except ValueError:
        bot.send_message(user_id, get_text(lang, 'custom_invalid'), parse_mode='HTML')
        safe_register_step(message.chat.id, process_custom_amount, currency)
        return
    threading.Thread(target=_send_payment_qr, args=(user_id, lang, currency, amt), daemon=True).start()

def _send_payment_qr(user_id, lang, currency, amount, message_id=None):
    is_adm = user_id in ADMIN_IDS
    if not TRON_WALLET_ADDRESS or not TRON_WALLET_ADDRESS.startswith('T'):
        bot.send_message(user_id, "❌ Deposit is not configured. Contact support.", reply_markup=main_menu(lang, is_adm))
        return
    deposit_id = create_deposit_request(user_id, currency, amount)
    if deposit_id is None:
        bot.send_message(user_id, get_text(lang, 'pending_exists'), reply_markup=main_menu(lang, is_adm))
        return
    text = get_text(lang, 'payment_info', amount=f"{amount:.2f}", currency=currency.upper(), pay_address=TRON_WALLET_ADDRESS)
    markup = paid_button_kb(lang, deposit_id)
    try: bot.send_chat_action(user_id, 'typing')
    except: pass
    if message_id:
        try:
            bot.edit_message_text(chat_id=user_id, message_id=message_id, text=text, parse_mode='HTML', reply_markup=markup)
        except Exception as e:
            print(f"Payment message edit error: {e}")
            bot.send_message(user_id, text, parse_mode='HTML', reply_markup=markup)
    else:
        bot.send_message(user_id, text, parse_mode='HTML', reply_markup=markup)

@bot.callback_query_handler(func=lambda c: c.data.startswith('paid_'))
def on_paid_button(call):
    if not verify_callback_owner(call): return
    deposit_id = int(call.data.replace('paid_', ''))
    user_id = call.from_user.id
    _, lang = get_user(user_id)
    dep = get_deposit(deposit_id)
    if not dep or dep['user_id'] != user_id:
        bot.answer_callback_query(call.id, "❌ Deposit not found.")
        return
    if dep['status'] != 'awaiting_proof':
        bot.answer_callback_query(call.id, get_text(lang, 'already_submitted').replace('<b>','').replace('</b>',''), show_alert=True)
        return
    bot.answer_callback_query(call.id)
    bot.send_message(user_id, get_text(lang, 'proof_prompt'), parse_mode='HTML')
    safe_register_step(call.message.chat.id, handle_proof, deposit_id)

def _is_nav_button(text, lang):
    NAV = set()
    for l in LANGUAGES.keys():
        NAV.add(get_text(l, 'btn_support'))
        NAV.add(get_text(l, 'btn_account'))
        NAV.add(get_text(l, 'btn_lang'))
        NAV.add(get_text(l, 'btn_back_menu'))
    NAV.update({"🆘 Support", "👤 My Account", "🌐 Language", "⬅️ Back to Menu", "👑 Admin Panel"})
    NAV.update(ALL_BUTTONS.keys())
    return text in NAV

def handle_proof(message, deposit_id):
    user_id = message.from_user.id
    _, lang = get_user(user_id)
    is_adm = user_id in ADMIN_IDS
    text = (message.text or '').strip()
    if text.startswith('/'): return
    if _is_nav_button(text, lang):
        bot.send_message(user_id, get_text(lang, 'proof_skipped_nav'), parse_mode='HTML')
        handle_services(message)
        return
    if text.lower() in ('cancel',):
        bot.send_message(user_id, get_text(lang, 'proof_cancelled'), parse_mode='HTML', reply_markup=main_menu(lang, is_adm))
        return
    dep = get_deposit(deposit_id)
    if not dep:
        bot.send_message(user_id, "❌ Deposit not found.", reply_markup=main_menu(lang, is_adm))
        return
    if dep['user_id'] != user_id:
        bot.send_message(user_id, "⛔ Not allowed.", reply_markup=main_menu(lang, is_adm))
        return
    if dep['status'] != 'awaiting_proof':
        bot.send_message(user_id, get_text(lang, 'already_submitted'), reply_markup=main_menu(lang, is_adm))
        return
    if message.photo:
        file_id = message.photo[-1].file_id
        ok = set_deposit_proof(deposit_id, 'screenshot', message.caption or 'Screenshot', file_id)
        if not ok:
            bot.send_message(user_id, get_text(lang, 'already_submitted'), reply_markup=main_menu(lang, is_adm))
            return
        bot.send_message(user_id, get_text(lang, 'proof_received_screenshot', amount=dep['amount'], currency=dep['currency']), reply_markup=main_menu(lang, is_adm))
        _notify_admins_new_deposit(deposit_id)
        return
    if len(text) == 64 and all(c in '0123456789abcdefABCDEF' for c in text):
        if is_txid_processed(text):
            bot.send_message(user_id, get_text(lang, 'proof_already_used'))
            bot.send_message(user_id, get_text(lang, 'proof_prompt'), parse_mode='HTML')
            safe_register_step(message.chat.id, handle_proof, deposit_id)
            return
        onchain = verify_txid_on_chain(text)
        ok = set_deposit_proof(deposit_id, 'txid', text)
        if not ok:
            bot.send_message(user_id, get_text(lang, 'already_submitted'), reply_markup=main_menu(lang, is_adm))
            return
        bot.send_message(user_id, get_text(lang, 'proof_received_txid', txid=text, amount=dep['amount'], currency=dep['currency']), reply_markup=main_menu(lang, is_adm))
        _notify_admins_new_deposit(deposit_id, onchain_info=onchain)
        return
    bot.send_message(user_id, get_text(lang, 'proof_invalid'))
    bot.send_message(user_id, get_text(lang, 'proof_prompt'), parse_mode='HTML')
    safe_register_step(message.chat.id, handle_proof, deposit_id)

def _notify_admins_new_deposit(deposit_id, onchain_info=None):
    dep = get_deposit(deposit_id)
    if not dep: return
    username = f"<code>{dep['user_id']}</code>"
    text = (
        f"🔔 <b>New Deposit Request</b>\n\n"
        f"🆔 ID: <b>#{dep['id']}</b>\n"
        f"👤 User: {username}\n"
        f"💰 Amount: <b>{dep['amount']:.2f}$</b> ({dep['currency']})\n"
        f"🔖 Method: <b>{dep['method']}</b>\n"
        f"📄 Proof: <code>{dep['proof']}</code>\n"
        f"🕒 {time.strftime('%Y-%m-%d %H:%M', time.localtime(dep['created_at']))}\n"
    )
    if onchain_info:
        try:
            confirmed = onchain_info.get('ret', [{}])[0].get('contractRet')
            text += f"⛓️ On-chain: <b>{confirmed or 'unknown'}</b>\n"
        except Exception: pass
    for admin_id in ADMIN_IDS:
        try:
            if dep['file_id']:
                admin_bot.send_photo(admin_id, dep['file_id'], caption=text, reply_markup=deposit_review_kb(dep['id']), parse_mode='HTML')
            else:
                admin_bot.send_message(admin_id, text, reply_markup=deposit_review_kb(dep['id']), parse_mode='HTML')
        except Exception as e:
            print(f"admin notify error: {e}")

@admin_bot.callback_query_handler(func=lambda c: c.data.startswith('dep_approve_') or c.data.startswith('dep_reject_'))
def admin_review_deposit(call):
    if call.from_user.id not in ADMIN_IDS:
        admin_bot.answer_callback_query(call.id, "⛔ Not authorized")
        return
    action = 'approve' if call.data.startswith('dep_approve_') else 'reject'
    deposit_id = int(call.data.split('_')[-1])
    dep = get_deposit(deposit_id)
    if not dep:
        admin_bot.answer_callback_query(call.id, "Deposit not found")
        return
    if dep['status'] != 'pending':
        admin_bot.answer_callback_query(call.id, f"Already {dep['status']}")
        return
    user_id = dep['user_id']
    amount = float(dep['amount'])
    new_status = 'approved' if action == 'approve' else 'rejected'
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute('''
        UPDATE deposit_requests
        SET status = ?, reviewed_by = ?, reviewed_at = ?
        WHERE id = ? AND status = 'pending'
    ''', (new_status, call.from_user.id, int(time.time()), deposit_id))
    conn.commit()
    if cur.rowcount == 0:
        admin_bot.answer_callback_query(call.id, "Already processed by another admin")
        return

    _, user_lang = get_user(user_id)

    if action == 'approve':
        update_balance(user_id, amount)
        proof = dep.get('proof') or ''
        if len(proof) == 64 and all(c in '0123456789abcdefABCDEF' for c in proof):
            mark_txid_processed(proof)
        new_bal, _ = get_user(user_id)
        try:
            bot.send_message(user_id,
                get_text(user_lang, 'deposit_approved',
                         amount=amount, balance=new_bal, proof=proof),
                parse_mode='HTML')
        except Exception as e:
            print(f"user notify approve error: {e}")
        admin_bot.answer_callback_query(call.id, "✅ Approved")
    else:
        try:
            bot.send_message(user_id,
                get_text(user_lang, 'deposit_rejected',
                         amount=amount, currency=dep['currency'],
                         support=SUPPORT_USERNAME),
                parse_mode='HTML')
        except Exception as e:
            print(f"user notify reject error: {e}")
        admin_bot.answer_callback_query(call.id, "❌ Rejected")
    try:
        admin_bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception:
        pass

@bot.callback_query_handler(func=lambda c: c.data == 'back_to_acc')
def back_to_acc(call):
    if not verify_callback_owner(call): return
    bot.answer_callback_query(call.id)
    user_id = call.from_user.id
    balance, lang = get_user(user_id)
    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton(get_text(lang, 'deposit'), callback_data="deposit_start"),
        types.InlineKeyboardButton("⬅️ Back", callback_data="back_to_menu_inline")
    )
    try:
        bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text=get_text(lang, 'my_account', user_id=user_id, balance=balance), reply_markup=markup)
    except Exception as e:
        if "message is not modified" not in str(e): print(f"edit error: {e}")

@bot.callback_query_handler(func=lambda c: c.data.startswith('toggle_'))
def callback_toggle_info(call):
    if not verify_callback_owner(call): return
    bot.answer_callback_query(call.id)
    parts = call.data.rsplit('_', 1)
    if len(parts) != 2: return
    full_key, state = parts
    service_key = full_key.replace('toggle_', '')
    expanded = (state == '1')
    user_id = call.from_user.id
    _, lang = get_user(user_id)
    if service_key in SERVICES: sd = SERVICES[service_key]
    elif service_key in IMEI_SUB_SERVICES: sd = IMEI_SUB_SERVICES[service_key]
    elif service_key in GSX_SUB_SERVICES: sd = GSX_SUB_SERVICES[service_key]
    elif service_key in OTHER_IMEI_SUB_SERVICES: sd = OTHER_IMEI_SUB_SERVICES[service_key]
    elif service_key in OTHER_SUB_SERVICES: sd = OTHER_SUB_SERVICES[service_key]
    else: return
    price = sd["price"]
    service_name = sd["name"].get(lang, sd["name"]["en"])
    base = get_text(lang, 'service_prompt', service=service_name, price=price)
    full = base + SERVICE_DETAILS.get(service_key, "") if expanded else base
    try:
        bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text=full, reply_markup=service_info_keyboard(lang, service_key, expanded))
    except Exception as e:
        if "message is not modified" not in str(e): print(f"edit err: {e}")

# ==========================================
# MAIN HANDLER (catch-all)
# ==========================================
@bot.message_handler(func=lambda m: True)
def handle_services(message):
    user_id = message.from_user.id
    if message.text and message.text.startswith('/'): return
    if user_id not in ADMIN_IDS:
        maint = get_bot_setting('maintenance')
        if maint == 'on':
            send_maintenance(user_id)
            return
    balance, lang = get_user(user_id)
    is_adm = user_id in ADMIN_IDS
    if message.text in [get_text(l, 'btn_support') for l in LANGUAGES.keys()] + ["🆘 Support"]:
        clear_pending(user_id); support_handler(message); return
    if message.text == "👑 Admin Panel":
        clear_pending(user_id); admin_panel_from_user(message); return
    if message.text in [get_text(l, 'btn_account') for l in LANGUAGES.keys()] + ["👤 My Account"]:
        clear_pending(user_id); my_account(message); return
    if message.text in [get_text(l, 'btn_lang') for l in LANGUAGES.keys()] + ["🌐 Language"]:
        clear_pending(user_id); change_language(message); return
    if message.text in [get_text(l, 'btn_back_menu') for l in LANGUAGES.keys()] + ["⬅️ Back to Menu"]:
        clear_pending(user_id)
        bot.send_message(user_id, "Main Menu:", reply_markup=main_menu(lang, is_adm)); return
    for name_map, sd_dict in [
        (ALL_OTHER_SUB_NAMES, OTHER_SUB_SERVICES),
        (ALL_OTHER_IMEI_SUB_NAMES, OTHER_IMEI_SUB_SERVICES),
        (ALL_GSX_SUB_NAMES, GSX_SUB_SERVICES),
        (ALL_IMEI_SUB_NAMES, IMEI_SUB_SERVICES),
    ]:
        key = name_map.get(message.text)
        if key:
            sd = sd_dict[key]
            prompt = get_text(lang, 'service_prompt', service=sd["name"].get(lang, sd["name"]["en"]), price=sd["price"])
            clear_pending(user_id)
            bot.send_message(message.chat.id, prompt, reply_markup=service_info_keyboard(lang, key, False))
            set_pending(user_id, key)
            safe_register_step(message.chat.id, process_imei)
            return
    service_key = ALL_SERVICE_NAMES.get(message.text)
    if service_key:
        if service_key == "imei_check":
            clear_pending(user_id)
            bot.send_message(message.chat.id, get_text(lang, 'please_choose_service'), reply_markup=imei_sub_menu(lang)); return
        if service_key == "gsx":
            clear_pending(user_id)
            bot.send_message(message.chat.id, get_text(lang, 'please_choose_service'), reply_markup=gsx_sub_menu(lang)); return
        if service_key == "other_imei":
            clear_pending(user_id)
            bot.send_message(message.chat.id, get_text(lang, 'please_choose_service'), reply_markup=other_imei_sub_menu(lang)); return
        if service_key == "other":
            clear_pending(user_id)
            bot.send_message(message.chat.id, get_text(lang, 'please_choose_service'), reply_markup=other_sub_menu(lang)); return
        sd = SERVICES[service_key]
        prompt = get_text(lang, 'service_prompt', service=sd["name"].get(lang, sd["name"]["en"]), price=sd["price"])
        clear_pending(user_id)
        bot.send_message(message.chat.id, prompt, reply_markup=service_info_keyboard(lang, service_key, False))
        set_pending(user_id, service_key)
        safe_register_step(message.chat.id, process_imei)
    else:
        clear_pending(user_id)
        bot.send_message(user_id, "Please select a service from the menu below.", reply_markup=main_menu(lang, is_adm))

def process_imei(message):
    text = (message.text or "").strip()
    user_id = message.from_user.id
    balance, lang = get_user(user_id)
    is_adm = user_id in ADMIN_IDS
    service_key = get_pending(user_id)
    if service_key is None: return
    if (text in ALL_BUTTONS
            or text in [get_text(l, 'btn_lang') for l in LANGUAGES.keys()] + ["🌐 Language"]
            or text in [get_text(l, 'btn_account') for l in LANGUAGES.keys()] + ["👤 My Account"]
            or text in [get_text(l, 'btn_support') for l in LANGUAGES.keys()] + ["🆘 Support"]
            or text in [get_text(l, 'btn_back_menu') for l in LANGUAGES.keys()] + ["⬅️ Back to Menu"]
            or text == "👑 Admin Panel"):
        clear_pending(user_id)
        handle_services(message)
        return
    if text.startswith('/'):
        clear_pending(user_id)
        return

    if service_key in SERVICES:
        sd = SERVICES[service_key]
    elif service_key in IMEI_SUB_SERVICES:
        sd = IMEI_SUB_SERVICES[service_key]
    elif service_key in GSX_SUB_SERVICES:
        sd = GSX_SUB_SERVICES[service_key]
    elif service_key in OTHER_IMEI_SUB_SERVICES:
        sd = OTHER_IMEI_SUB_SERVICES[service_key]
    elif service_key in OTHER_SUB_SERVICES:
        sd = OTHER_SUB_SERVICES[service_key]
    else:
        clear_pending(user_id)
        bot.send_message(user_id, "Invalid service.", reply_markup=main_menu(lang, is_adm))
        return

    price = sd["price"]; api_id = sd["api_id"]
    service_name = sd["name"].get(lang, sd["name"]["en"])
    if not text.isdigit() or len(text) < 15:
        bot.send_message(user_id, get_text(lang, 'invalid_imei'), reply_markup=main_menu(lang, is_adm))
        return
    imei = text
    if not is_adm and balance <= 0:
        clear_pending(user_id)
        bot.send_message(user_id, get_text(lang, 'no_balance_warning', balance=balance), reply_markup=main_menu(lang, is_adm))
        return
    if balance < price:
        clear_pending(user_id)
        bot.send_message(user_id, get_text(lang, 'insufficient_balance', balance=balance, price=price), reply_markup=main_menu(lang, is_adm))
        return
    clear_pending(user_id)
    bot.send_message(user_id, get_text(lang, 'processing', service=service_name, imei=imei))
    threading.Thread(target=_process_imei_bg, args=(user_id, lang, imei, api_id, price, service_name, is_adm), daemon=True).start()

def _process_imei_bg(user_id, lang, imei, api_id, price, service_name, is_adm):
    success, result = call_real_api(imei, api_id, lang)
    if success:
        ok, new_bal = try_deduct_balance(user_id, price)
        if not ok:
            bot.send_message(user_id, get_text(lang, 'insufficient_balance', balance=new_bal, price=price), reply_markup=main_menu(lang, is_adm))
            return
        bot.send_message(user_id, f"{result}{get_text(lang, 'new_balance', balance=new_bal)}", reply_markup=main_menu(lang, is_adm))
    else:
        bot.send_message(user_id, f"{result}\n\n💰 <b>Your balance has not been charged.</b>", reply_markup=main_menu(lang, is_adm))

def call_real_api(imei, api_id, lang):
    if not API_KEY or not API_URL:
        return False, "❌ <b>API Error:</b> IMEI API is not configured. Please contact support."
    payload = {'api_key': API_KEY, 'imei': imei, 'service': api_id}
    try:
        r = _http.get(API_URL, params=payload, timeout=20)
        r.raise_for_status()
        data = r.json()
        if data.get('status') == 'success':
            msg = get_text(lang, 'result', imei=imei, model=data.get('model', 'N/A'), fmi=data.get('fmi', 'N/A'), icloud=data.get('icloud', 'N/A'), carrier=data.get('carrier', 'N/A'))
            return True, msg
        return False, f"❌ <b>API Error:</b> {data.get('message', 'Unknown')}"
    except Exception as e:
        return False, f"❌ <b>Network/System Error:</b> {e}"

# ==========================================
#          ADMIN BOT HANDLERS
# ==========================================
@admin_bot.message_handler(commands=['start'])
def admin_start(message):
    if not is_admin(message.from_user.id):
        admin_bot.send_message(message.chat.id, "⛔ You are not authorized.")
        return
    admin_bot.send_message(
        message.chat.id,
        "👑 <b>Admin Control Panel</b>\n\n"
        "╭────────────────────────╮\n"
        "│  📊 Stats & Top Users\n"
        "│  👥 Manage balances\n"
        "│  📢 Broadcast messages\n"
        "│  📦 Download DB (zip or raw .db)\n"
        "│  🛠️ Maintenance mode\n"
        "╰────────────────────────╯\n\n"
        "Select an option below 👇",
        reply_markup=admin_main_menu(),
        parse_mode='HTML'
    )

@admin_bot.message_handler(commands=['trongrid'])
def admin_trongrid_status(message):
    if not is_admin(message.from_user.id): return
    statuses = get_trongrid_key_status()
    text = "🔑 <b>TronGrid API Keys Status</b>\n\n"
    for s in statuses:
        text += (f"Key #{s['index']} <code>{s['suffix']}</code>\n"
                 f"  Failures: <b>{s['failures']}</b> | "
                 f"Last fail: <b>{int(s['last_fail_ago'])}s ago</b>\n\n")
    admin_bot.send_message(message.chat.id, text, reply_markup=admin_main_menu())

@admin_bot.message_handler(commands=['db'])
def admin_db_command(message):
    if not is_admin(message.from_user.id): return
    admin_download_db_raw(message)

@admin_bot.message_handler(func=lambda m: m.text == "🛠️ Maintenance")
def admin_maintenance_menu(message):
    if not is_admin(message.from_user.id): return
    current = get_bot_setting('maintenance') or 'off'
    status_emoji = "🔴 ON" if current == 'on' else "🟢 OFF"
    text = (
        "🛠️ <b>Maintenance Mode</b>\n\n"
        "╭────────────────────────╮\n"
        f"│  Status: <b>{status_emoji}</b>\n"
        "╰────────────────────────╯\n\n"
        "• <b>ON</b>  → Blocks all non-admin users\n"
        "• <b>OFF</b> → Bot works normally\n\n"
        "<i>Users see a polished maintenance screen with support contact.</i>"
    )
    markup = types.InlineKeyboardMarkup(row_width=2)
    if current == 'on':
        markup.add(types.InlineKeyboardButton("🟢 Turn OFF", callback_data="maint_off"))
    else:
        markup.add(types.InlineKeyboardButton("🔴 Turn ON", callback_data="maint_on"))
    admin_bot.send_message(message.chat.id, text, reply_markup=markup, parse_mode='HTML')

@admin_bot.callback_query_handler(func=lambda c: c.data.startswith('maint_'))
def admin_maintenance_toggle(call):
    if call.from_user.id not in ADMIN_IDS:
        admin_bot.answer_callback_query(call.id, "⛔ Not authorized")
        return
    state = 'on' if call.data == 'maint_on' else 'off'
    set_bot_setting('maintenance', state)
    status_emoji = "🔴 ON" if state == 'on' else "🟢 OFF"
    admin_bot.answer_callback_query(call.id, f"✅ Maintenance turned {state.upper()}")
    text = (
        "🛠️ <b>Maintenance Mode</b>\n\n"
        "╭────────────────────────╮\n"
        f"│  Status: <b>{status_emoji}</b>\n"
        "╰────────────────────────╯\n\n"
        "• <b>ON</b>  → Blocks all non-admin users\n"
        "• <b>OFF</b> → Bot works normally\n\n"
        "<i>Users see a polished maintenance screen with support contact.</i>"
    )
    markup = types.InlineKeyboardMarkup(row_width=2)
    if state == 'on':
        markup.add(types.InlineKeyboardButton("🟢 Turn OFF", callback_data="maint_off"))
    else:
        markup.add(types.InlineKeyboardButton("🔴 Turn ON", callback_data="maint_on"))
    try:
        admin_bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=text,
            reply_markup=markup,
            parse_mode='HTML'
        )
    except Exception:
        pass

@admin_bot.message_handler(func=lambda m: m.text == "📊 Stats")
def admin_stats(message):
    if not is_admin(message.from_user.id): return
    users = get_total_users()
    bal = get_total_balance()
    avg = bal / users if users else 0
    pending = get_pending_deposits_count()
    dep_stats = get_deposit_stats()
    approved = dep_stats.get('approved', {'count': 0, 'total': 0})
    rejected = dep_stats.get('rejected', {'count': 0, 'total': 0})
    text = (
        "📊 <b>Bot Statistics</b>\n\n"
        "╭────────────────────────╮\n"
        f"│  👥 Users: <b>{users}</b>\n"
        f"│  💰 Total Balance: <b>{bal:.2f}$</b>\n"
        f"│  📈 Average: <b>{avg:.2f}$</b>\n"
        "├────────────────────────┤\n"
        f"│  ⏳ Pending deposits: <b>{pending}</b>\n"
        f"│  ✅ Approved: <b>{approved['count']}</b> ({approved['total']:.2f}$)\n"
        f"│  ❌ Rejected: <b>{rejected['count']}</b> ({rejected['total']:.2f}$)\n"
        "╰────────────────────────╯"
    )
    admin_bot.send_message(message.chat.id, text, reply_markup=admin_main_menu())

# ==========================================
# 📦 DOWNLOAD DB — zip version
# ==========================================
@admin_bot.message_handler(func=lambda m: m.text == "📦 Download DB")
def admin_download_db_zip(message):
    if not is_admin(message.from_user.id): return
    admin_bot.send_message(
        message.chat.id,
        "📦 <b>Preparing compressed database (.zip)...</b>\n<i>Safe online backup — bot stays fast.</i>",
        parse_mode='HTML'
    )
    try:
        zip_name, buf = export_db_zip()
        size_kb = len(buf.getvalue()) / 1024
        admin_bot.send_document(
            message.chat.id,
            buf,
            visible_file_name=zip_name,
            caption=(
                f"✅ <b>Database export ready</b>\n\n"
                f"📁 File: <code>{zip_name}</code>\n"
                f"📦 Size: <b>{size_kb:.1f} KB</b> (zip)\n"
                f"🕒 {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"<i>Unzip to get apple_bot_*.db — open with any SQLite tool.</i>"
            ),
            parse_mode='HTML'
        )
    except Exception as e:
        admin_bot.send_message(
            message.chat.id,
            f"❌ Export failed: <code>{e}</code>",
            parse_mode='HTML',
            reply_markup=admin_main_menu()
        )
        return
    admin_bot.send_message(message.chat.id, "Done.", reply_markup=admin_main_menu())


# ==========================================
# 📦 DOWNLOAD DB (RAW .db + WAL + SHM) — FIXED
# ==========================================
@admin_bot.message_handler(func=lambda m: m.text == "📦 Download DB (Raw .db)")
def admin_download_db_raw(message):
    if not is_admin(message.from_user.id): return
    admin_bot.send_message(
        message.chat.id,
        "📦 <b>Preparing raw database files...</b>\n"
        "<i>This sends the .db file plus WAL/SHM sidecar files (if present and non-empty).</i>",
        parse_mode='HTML'
    )
    try:
        # 1) Main .db — self-contained online backup
        db_name, buf = export_db_raw()
        size_kb = len(buf.getvalue()) / 1024
        admin_bot.send_document(
            message.chat.id,
            buf,
            visible_file_name=db_name,
            caption=(
                f"✅ <b>Main DB file</b>\n\n"
                f"📁 <code>{db_name}</code>\n"
                f"📦 Size: <b>{size_kb:.1f} KB</b>\n"
                f"🕒 {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"<i>This .db from online backup is fully self-contained — WAL/SHM below are just extra safety copies.</i>"
            ),
            parse_mode='HTML'
        )

        # 2) Live WAL + SHM sidecar files (only if non-empty — Telegram rejects 0-byte uploads)
        sidecars_sent = 0
        sidecars_skipped = 0
        for ext in ('-wal', '-shm'):
            sc = _read_sidecar(DB_PATH + ext)
            if sc is None:
                sidecars_skipped += 1
                continue
            size_kb = len(sc.getvalue()) / 1024
            try:
                admin_bot.send_document(
                    message.chat.id,
                    sc,
                    visible_file_name=os.path.basename(DB_PATH + ext),
                    caption=(
                        f"📎 <b>Sidecar file</b>\n\n"
                        f"📁 <code>{os.path.basename(DB_PATH + ext)}</code>\n"
                        f"📦 Size: <b>{size_kb:.1f} KB</b>\n"
                        f"<i>Only needed if you want the exact live state — usually not required.</i>"
                    ),
                    parse_mode='HTML'
                )
                sidecars_sent += 1
            except Exception as e:
                sidecars_skipped += 1
                print(f"sidecar send error ({ext}): {e}")

        summary = (
            f"✅ <b>Raw export complete.</b>\n\n"
            f"📁 Main .db sent\n"
            f"📎 Sidecars sent: <b>{sidecars_sent}</b>\n"
            f"⏭️ Sidecars skipped (empty/missing): <b>{sidecars_skipped}</b>\n\n"
            f"<i>To migrate: rename the main file to </i><code>apple_bot.db</code><i> on the new host and place it next to the bot script. Sidecars are optional.</i>"
        )
        admin_bot.send_message(
            message.chat.id,
            summary,
            parse_mode='HTML',
            reply_markup=admin_main_menu()
        )
    except Exception as e:
        admin_bot.send_message(
            message.chat.id,
            f"❌ Raw export failed: <code>{e}</code>",
            parse_mode='HTML',
            reply_markup=admin_main_menu()
        )


@admin_bot.message_handler(func=lambda m: m.text == "🔑 TronGrid Status")
def admin_trongrid_btn(message):
    if not is_admin(message.from_user.id): return
    try:
        statuses = get_trongrid_key_status()
    except Exception as e:
        admin_bot.send_message(message.chat.id, f"❌ Error: {e}", reply_markup=admin_main_menu())
        return
    text = "🔑 <b>TronGrid API Keys Status</b>\n\n"
    for s in statuses:
        text += (
            f"Key #{s['index']} <code>{s['suffix']}</code>\n"
            f"  Failures: <b>{s['failures']}</b> | "
            f"Last fail: <b>{int(s['last_fail_ago'])}s ago</b>\n\n"
        )
    admin_bot.send_message(message.chat.id, text, reply_markup=admin_main_menu())

@admin_bot.message_handler(func=lambda m: m.text == "👥 Users List")
def admin_users_list(message):
    if not is_admin(message.from_user.id): return
    users = get_all_users()
    if not users:
        admin_bot.send_message(message.chat.id, "No users.", reply_markup=admin_main_menu()); return
    chunks = []; current = "👥 <b>Users (first 50)</b>\n\n"
    for i, u in enumerate(users[:50], 1):
        line = f"{i}. <code>{u['user_id']}</code> — <b>{u['balance']:.2f}$</b>\n"
        if len(current) + len(line) > 4000:
            chunks.append(current); current = line
        else:
            current += line
    chunks.append(current)
    for c in chunks:
        admin_bot.send_message(message.chat.id, c)
    admin_bot.send_message(message.chat.id, "Done.", reply_markup=admin_main_menu())

@admin_bot.message_handler(func=lambda m: m.text == "🏆 Top Users")
def admin_top_users(message):
    if not is_admin(message.from_user.id): return
    top = get_top_users(20)
    if not top:
        admin_bot.send_message(message.chat.id, "No users.", reply_markup=admin_main_menu()); return
    text = "🏆 <b>Top 20 Users</b>\n\n"
    for i, u in enumerate(top, 1):
        text += f"{i}. <code>{u['user_id']}</code> — <b>{u['balance']:.2f}$</b>\n"
    admin_bot.send_message(message.chat.id, text, reply_markup=admin_main_menu())

@admin_bot.message_handler(func=lambda m: m.text == "💵 Add Balance")
def admin_add_prompt(message):
    if not is_admin(message.from_user.id): return
    msg = admin_bot.send_message(message.chat.id, "💵 <b>Add Balance</b>\n\nSend: <code>user_id amount</code>", reply_markup=admin_cancel_kb())
    admin_bot.register_next_step_handler(msg, admin_do_add)

def admin_do_add(message):
    if not is_admin(message.from_user.id): return
    if message.text == "❌ Cancel":
        admin_bot.send_message(message.chat.id, "Cancelled.", reply_markup=admin_main_menu()); return
    try:
        parts = message.text.split(); uid = int(parts[0]); amt = float(parts[1])
        if not get_user_info(uid):
            admin_bot.send_message(message.chat.id, "❌ User not found.", reply_markup=admin_main_menu()); return
        update_balance(uid, amt)
        info = get_user_info(uid)
        admin_bot.send_message(message.chat.id, f"✅ Added <b>{amt}$</b> to <code>{uid}</code>\nNew balance: <b>{info['balance']:.2f}$</b>", reply_markup=admin_main_menu())
        try: bot.send_message(uid, f"💰 Your balance was credited with <b>{amt}$</b>.")
        except Exception: pass
    except (ValueError, IndexError):
        admin_bot.send_message(message.chat.id, "❌ Invalid format.", reply_markup=admin_main_menu())

@admin_bot.message_handler(func=lambda m: m.text == "💸 Remove Balance")
def admin_remove_prompt(message):
    if not is_admin(message.from_user.id): return
    msg = admin_bot.send_message(message.chat.id, "💸 <b>Remove Balance</b>\n\nSend: <code>user_id amount</code>", reply_markup=admin_cancel_kb())
    admin_bot.register_next_step_handler(msg, admin_do_remove)

def admin_do_remove(message):
    if not is_admin(message.from_user.id): return
    if message.text == "❌ Cancel":
        admin_bot.send_message(message.chat.id, "Cancelled.", reply_markup=admin_main_menu()); return
    try:
        parts = message.text.split(); uid = int(parts[0]); amt = float(parts[1])
        info = get_user_info(uid)
        if not info:
            admin_bot.send_message(message.chat.id, "❌ User not found.", reply_markup=admin_main_menu()); return
        update_balance(uid, -amt)
        info = get_user_info(uid)
        admin_bot.send_message(message.chat.id, f"✅ Removed <b>{amt}$</b> from <code>{uid}</code>\nNew balance: <b>{info['balance']:.2f}$</b>", reply_markup=admin_main_menu())
    except (ValueError, IndexError):
        admin_bot.send_message(message.chat.id, "❌ Invalid format.", reply_markup=admin_main_menu())

@admin_bot.message_handler(func=lambda m: m.text == "✏️ Set Balance")
def admin_set_prompt(message):
    if not is_admin(message.from_user.id): return
    msg = admin_bot.send_message(message.chat.id, "✏️ <b>Set Balance</b>\n\nSend: <code>user_id new_balance</code>", reply_markup=admin_cancel_kb())
    admin_bot.register_next_step_handler(msg, admin_do_set)

def admin_do_set(message):
    if not is_admin(message.from_user.id): return
    if message.text == "❌ Cancel":
        admin_bot.send_message(message.chat.id, "Cancelled.", reply_markup=admin_main_menu()); return
    try:
        parts = message.text.split(); uid = int(parts[0]); amt = float(parts[1])
        if not get_user_info(uid):
            admin_bot.send_message(message.chat.id, "❌ User not found.", reply_markup=admin_main_menu()); return
        set_balance(uid, amt)
        admin_bot.send_message(message.chat.id, f"✅ Set balance of <code>{uid}</code> to <b>{amt:.2f}$</b>", reply_markup=admin_main_menu())
    except (ValueError, IndexError):
        admin_bot.send_message(message.chat.id, "❌ Invalid format.", reply_markup=admin_main_menu())

@admin_bot.message_handler(func=lambda m: m.text == "📢 Broadcast")
def admin_broadcast_prompt(message):
    if not is_admin(message.from_user.id): return
    msg = admin_bot.send_message(message.chat.id, "📢 <b>Broadcast</b>\n\nSend the message to broadcast (HTML supported).", reply_markup=admin_cancel_kb())
    admin_bot.register_next_step_handler(msg, admin_do_broadcast)

def admin_do_broadcast(message):
    admin_id = message.from_user.id
    if not is_admin(admin_id): return
    if message.text == "❌ Cancel":
        admin_bot.send_message(admin_id, "Cancelled.", reply_markup=admin_main_menu()); return
    text = message.text
    users = get_all_users()
    admin_bot.send_message(admin_id, f"📢 Broadcasting to <b>{len(users)}</b> users...")
    sent, failed = 0, 0
    for u in users:
        try:
            bot.send_message(u['user_id'], text, parse_mode='HTML'); sent += 1
            time.sleep(0.05)
        except Exception:
            failed += 1
    admin_bot.send_message(admin_id, f"✅ <b>Done</b>\n\n✅ Sent: <b>{sent}</b>\n❌ Failed: <b>{failed}</b>", reply_markup=admin_main_menu())

@admin_bot.message_handler(func=lambda m: m.text == "🔍 User Info")
def admin_userinfo_prompt(message):
    if not is_admin(message.from_user.id): return
    msg = admin_bot.send_message(message.chat.id, "🔍 <b>User Info</b>\n\nSend the user ID:", reply_markup=admin_cancel_kb())
    admin_bot.register_next_step_handler(msg, admin_do_userinfo)

def admin_do_userinfo(message):
    if not is_admin(message.from_user.id): return
    if message.text == "❌ Cancel":
        admin_bot.send_message(message.chat.id, "Cancelled.", reply_markup=admin_main_menu()); return
    try:
        uid = int(message.text.strip()); info = get_user_info(uid)
        if not info:
            admin_bot.send_message(message.chat.id, "❌ User not found.", reply_markup=admin_main_menu()); return
        admin_bot.send_message(message.chat.id, f"👤 <b>User Info</b>\n\n🆔 ID: <code>{info['user_id']}</code>\n💰 Balance: <b>{info['balance']:.2f}$</b>\n🌐 Language: <b>{info['language']}</b>\n✅ Authorized: <b>{'Yes' if info['is_authorized'] else 'No'}</b>", reply_markup=admin_main_menu())
    except ValueError:
        admin_bot.send_message(message.chat.id, "❌ Invalid ID.", reply_markup=admin_main_menu())

@admin_bot.message_handler(func=lambda m: m.text == "🗑 Delete User")
def admin_delete_prompt(message):
    if not is_admin(message.from_user.id): return
    msg = admin_bot.send_message(message.chat.id, "🗑 <b>Delete User</b>\n\nSend the user ID to delete:", reply_markup=admin_cancel_kb())
    admin_bot.register_next_step_handler(msg, admin_do_delete)

def admin_do_delete(message):
    if not is_admin(message.from_user.id): return
    if message.text == "❌ Cancel":
        admin_bot.send_message(message.chat.id, "Cancelled.", reply_markup=admin_main_menu()); return
    try:
        uid = int(message.text.strip())
        if not get_user_info(uid):
            admin_bot.send_message(message.chat.id, "❌ User not found.", reply_markup=admin_main_menu()); return
        delete_user(uid)
        admin_bot.send_message(message.chat.id, f"✅ Deleted <code>{uid}</code>.", reply_markup=admin_main_menu())
    except ValueError:
        admin_bot.send_message(message.chat.id, "❌ Invalid ID.", reply_markup=admin_main_menu())

# ==========================================
# 9. HEALTH CHECK
# ==========================================
@app.route('/')
def health():
    return jsonify({'status': 'ok', 'wallet': TRON_WALLET_ADDRESS})

@app.route('/trongrid/status')
def trongrid_status():
    return jsonify({'keys': get_trongrid_key_status()})

# ==========================================
# 10. START EVERYTHING
# ==========================================
def run_user_bot():
    print("🤖 User bot is running...")
    bot.infinity_polling(timeout=20, long_polling_timeout=10, skip_pending=True)

def run_admin_bot():
    print("👑 Admin bot is running...")
    admin_bot.infinity_polling(timeout=20, long_polling_timeout=10, skip_pending=True)

if __name__ == '__main__':
    threading.Thread(target=run_user_bot, daemon=True).start()
    threading.Thread(target=run_admin_bot, daemon=True).start()
    print(f"🌐 Web server running on {WEBHOOK_HOST}:{WEBHOOK_PORT}")
    app.run(host=WEBHOOK_HOST, port=WEBHOOK_PORT, threaded=True)