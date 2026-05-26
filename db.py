"""
db.py — KAASOD v7 Database Layer
Supabase/PostgreSQL with automatic JSON fallback
ถ้า DATABASE_URL ไม่ถูกตั้งค่า จะ fallback ไป JSON เดิมทันที
"""

import os
import json
import logging
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# ── ตรวจสอบว่ามี psycopg2 และ DATABASE_URL ไหม ──────────────────
try:
    import psycopg2
    import psycopg2.extras
    _PG_AVAILABLE = True
except ImportError:
    _PG_AVAILABLE = False
    logger.warning("psycopg2 not installed — running in JSON-only mode")

DATABASE_URL = os.environ.get('DATABASE_URL', '')
DB_ENABLED   = _PG_AVAILABLE and bool(DATABASE_URL)

# ── JSON paths (fallback) ────────────────────────────────────────
DATA_DIR  = Path('pos_data')
SHOPS_DIR = DATA_DIR / 'shops'
USERS_FILE = DATA_DIR / 'users.json'


# ════════════════════════════════════════════════════════════════
# CONNECTION
# ════════════════════════════════════════════════════════════════

@contextmanager
def get_conn():
    """Context manager คืน psycopg2 connection พร้อม autocommit=False"""
    conn = psycopg2.connect(DATABASE_URL, sslmode='require')
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ════════════════════════════════════════════════════════════════
# SCHEMA INIT  (เรียกครั้งเดียวตอน app start)
# ════════════════════════════════════════════════════════════════

SCHEMA_SQL = """
-- ตาราง users
CREATE TABLE IF NOT EXISTS users (
    username     TEXT PRIMARY KEY,
    shop_id      TEXT NOT NULL UNIQUE,
    shop_name    TEXT NOT NULL,
    phone        TEXT,
    password     TEXT NOT NULL,
    plan         TEXT NOT NULL DEFAULT 'starter',
    price        INTEGER NOT NULL DEFAULT 199,
    status       TEXT NOT NULL DEFAULT 'pending',
    created_at   TEXT
);

-- ตาราง shop_data  (key-value per shop, เช่น products/stock/members)
CREATE TABLE IF NOT EXISTS shop_data (
    shop_id  TEXT NOT NULL,
    key      TEXT NOT NULL,
    value    JSONB NOT NULL DEFAULT '{}',
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (shop_id, key)
);

-- ตาราง sales_log  (แยกทุก transaction)
CREATE TABLE IF NOT EXISTS sales_log (
    id         BIGSERIAL PRIMARY KEY,
    shop_id    TEXT NOT NULL,
    username   TEXT,
    sale_date  DATE NOT NULL,
    sale_ts    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    total      INTEGER NOT NULL DEFAULT 0,
    pay        TEXT,
    member     TEXT,
    items      JSONB,
    raw        JSONB
);
CREATE INDEX IF NOT EXISTS idx_sales_shop_date ON sales_log (shop_id, sale_date);

-- ตาราง chat_logs
CREATE TABLE IF NOT EXISTS chat_logs (
    id        BIGSERIAL PRIMARY KEY,
    shop_id   TEXT NOT NULL,
    username  TEXT,
    ts        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    user_msg  TEXT,
    ai_reply  TEXT
);
CREATE INDEX IF NOT EXISTS idx_chat_shop ON chat_logs (shop_id);

-- ตาราง ai_usage  (daily count per shop)
CREATE TABLE IF NOT EXISTS ai_usage (
    shop_id   TEXT NOT NULL,
    use_date  DATE NOT NULL,
    count     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (shop_id, use_date)
);
"""


def init_db():
    """สร้าง schema ถ้ายังไม่มี — เรียกใน app startup"""
    if not DB_ENABLED:
        logger.info("DB_ENABLED=False — skipping schema init")
        return
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(SCHEMA_SQL)
        logger.info("✅ Database schema ready")
    except Exception as e:
        logger.error(f"❌ init_db failed: {e}")
        raise


# ════════════════════════════════════════════════════════════════
# USERS
# ════════════════════════════════════════════════════════════════

def load_users() -> dict:
    """คืน dict {username: user_obj} — DB ก่อน, fallback JSON"""
    if DB_ENABLED:
        try:
            with get_conn() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute("SELECT * FROM users")
                    rows = cur.fetchall()
            return {r['username']: dict(r) for r in rows}
        except Exception as e:
            logger.warning(f"load_users DB failed, fallback JSON: {e}")

    # JSON fallback
    if USERS_FILE.exists():
        return json.loads(USERS_FILE.read_text(encoding='utf-8'))
    return {}


def save_user(username: str, data: dict):
    """Upsert user คนเดียว — dual-write (DB + JSON)"""
    # ── DB write ──
    if DB_ENABLED:
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO users
                            (username, shop_id, shop_name, phone, password, plan, price, status, created_at)
                        VALUES
                            (%(username)s, %(shop_id)s, %(shop_name)s, %(phone)s, %(password)s,
                             %(plan)s, %(price)s, %(status)s, %(created_at)s)
                        ON CONFLICT (username) DO UPDATE SET
                            shop_name  = EXCLUDED.shop_name,
                            phone      = EXCLUDED.phone,
                            password   = EXCLUDED.password,
                            plan       = EXCLUDED.plan,
                            price      = EXCLUDED.price,
                            status     = EXCLUDED.status,
                            created_at = EXCLUDED.created_at
                    """, {**data, 'username': username})
        except Exception as e:
            logger.error(f"save_user DB failed: {e}")

    # ── JSON write (dual-write / fallback) ──
    _json_save_user(username, data)


def save_users(users: dict):
    """Save หลาย users พร้อมกัน (ใช้ใน legacy code) — dual-write"""
    for username, data in users.items():
        save_user(username, data)


def delete_user(username: str):
    """ลบ user — dual-write"""
    if DB_ENABLED:
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM users WHERE username = %s", (username,))
        except Exception as e:
            logger.error(f"delete_user DB failed: {e}")

    users = _json_load_users()
    if username in users:
        del users[username]
        _json_write_users(users)


# ════════════════════════════════════════════════════════════════
# SHOP DATA  (products / stock / members / settings / etc.)
# ════════════════════════════════════════════════════════════════

def read_shop_data(shop_id: str, key: str, default=None):
    """อ่าน key ของร้าน — DB ก่อน, fallback JSON file"""
    if DB_ENABLED:
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT value FROM shop_data WHERE shop_id=%s AND key=%s",
                        (shop_id, key)
                    )
                    row = cur.fetchone()
            if row is not None:
                return row[0]  # psycopg2 คืน Python obj จาก JSONB อัตโนมัติ
        except Exception as e:
            logger.warning(f"read_shop_data DB failed ({key}), fallback: {e}")

    # JSON fallback
    return _json_read_shop(shop_id, key, default)


def write_shop_data(shop_id: str, key: str, data):
    """Upsert shop data — dual-write"""
    if DB_ENABLED:
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO shop_data (shop_id, key, value, updated_at)
                        VALUES (%s, %s, %s, NOW())
                        ON CONFLICT (shop_id, key) DO UPDATE SET
                            value      = EXCLUDED.value,
                            updated_at = NOW()
                    """, (shop_id, key, json.dumps(data, ensure_ascii=False)))
        except Exception as e:
            logger.error(f"write_shop_data DB failed ({key}): {e}")

    # JSON dual-write
    _json_write_shop(shop_id, key, data)


# ════════════════════════════════════════════════════════════════
# SALES LOG
# ════════════════════════════════════════════════════════════════

def record_sale(shop_id: str, username: str, sale: dict):
    """บันทึก 1 transaction — dual-write"""
    today  = datetime.now().date()
    ts_now = datetime.now()

    if DB_ENABLED:
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO sales_log
                            (shop_id, username, sale_date, sale_ts, total, pay, member, items, raw)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """, (
                        shop_id,
                        username,
                        today,
                        ts_now,
                        sale.get('total', 0),
                        sale.get('pay', ''),
                        sale.get('member', ''),
                        json.dumps(sale.get('items', []), ensure_ascii=False),
                        json.dumps(sale, ensure_ascii=False),
                    ))
        except Exception as e:
            logger.error(f"record_sale DB failed: {e}")

    # JSON dual-write
    _json_append_sale(shop_id, today.strftime('%Y-%m-%d'), sale)


def get_sales(shop_id: str, dates: list) -> list:
    """ดึง sales หลายวัน — DB ก่อน, fallback JSON files"""
    if DB_ENABLED:
        try:
            with get_conn() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute("""
                        SELECT raw FROM sales_log
                        WHERE shop_id = %s AND sale_date = ANY(%s)
                        ORDER BY sale_ts
                    """, (shop_id, dates))
                    rows = cur.fetchall()
            return [r['raw'] for r in rows]
        except Exception as e:
            logger.warning(f"get_sales DB failed, fallback JSON: {e}")

    # JSON fallback
    result = []
    for d in dates:
        date_str = d.strftime('%Y-%m-%d') if hasattr(d, 'strftime') else str(d)
        result.extend(_json_read_sales(shop_id, date_str))
    return result


# ════════════════════════════════════════════════════════════════
# CHAT LOGS
# ════════════════════════════════════════════════════════════════

def append_chat_log(shop_id: str, username: str, user_msg: str, ai_reply: str):
    """บันทึก chat log — dual-write"""
    if DB_ENABLED:
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO chat_logs (shop_id, username, user_msg, ai_reply)
                        VALUES (%s, %s, %s, %s)
                    """, (shop_id, username, user_msg[:300], ai_reply[:500]))
        except Exception as e:
            logger.error(f"append_chat_log DB failed: {e}")

    # JSON dual-write
    logs = _json_read_shop(shop_id, 'chat_logs', [])
    logs.append({
        'ts':       datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'username': username,
        'user_msg': user_msg[:300],
        'ai_reply': ai_reply[:500],
    })
    logs = logs[-200:]
    _json_write_shop(shop_id, 'chat_logs', logs)


def get_chat_logs(shop_id: str, limit: int = 50) -> list:
    """ดึง chat logs — DB ก่อน, fallback JSON"""
    if DB_ENABLED:
        try:
            with get_conn() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute("""
                        SELECT ts, username, user_msg, ai_reply FROM chat_logs
                        WHERE shop_id = %s
                        ORDER BY ts DESC LIMIT %s
                    """, (shop_id, limit))
                    rows = cur.fetchall()
            return [dict(r) for r in reversed(rows)]
        except Exception as e:
            logger.warning(f"get_chat_logs DB failed, fallback: {e}")

    logs = _json_read_shop(shop_id, 'chat_logs', [])
    return logs[-limit:]


# ════════════════════════════════════════════════════════════════
# AI USAGE
# ════════════════════════════════════════════════════════════════

def get_today_usage(shop_id: str) -> int:
    """ดึง AI usage วันนี้"""
    today = datetime.now().strftime('%Y-%m-%d')

    if DB_ENABLED:
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT count FROM ai_usage WHERE shop_id=%s AND use_date=%s",
                        (shop_id, today)
                    )
                    row = cur.fetchone()
            return row[0] if row else 0
        except Exception as e:
            logger.warning(f"get_today_usage DB failed, fallback: {e}")

    # JSON fallback
    f = _shop_dir(shop_id) / 'ai_usage.json'
    if not f.exists():
        return 0
    data = json.loads(f.read_text(encoding='utf-8'))
    return data.get(today, 0)


def increment_usage(shop_id: str) -> int:
    """เพิ่ม AI usage +1 วันนี้ คืนค่าหลังเพิ่ม — dual-write"""
    today = datetime.now().strftime('%Y-%m-%d')
    new_count = 0

    if DB_ENABLED:
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO ai_usage (shop_id, use_date, count)
                        VALUES (%s, %s, 1)
                        ON CONFLICT (shop_id, use_date) DO UPDATE
                            SET count = ai_usage.count + 1
                        RETURNING count
                    """, (shop_id, today))
                    new_count = cur.fetchone()[0]
        except Exception as e:
            logger.error(f"increment_usage DB failed: {e}")

    # JSON dual-write
    f = _shop_dir(shop_id) / 'ai_usage.json'
    data = json.loads(f.read_text(encoding='utf-8')) if f.exists() else {}
    data[today] = data.get(today, 0) + 1
    keys = sorted(data.keys(), reverse=True)[:7]
    data = {k: data[k] for k in keys}
    f.write_text(json.dumps(data), encoding='utf-8')

    return new_count if new_count else data[today]


# ════════════════════════════════════════════════════════════════
# JSON HELPERS  (private — ใช้ภายใน db.py เท่านั้น)
# ════════════════════════════════════════════════════════════════

def _shop_dir(shop_id: str) -> Path:
    d = SHOPS_DIR / shop_id
    d.mkdir(parents=True, exist_ok=True)
    return d

def _json_load_users() -> dict:
    if USERS_FILE.exists():
        return json.loads(USERS_FILE.read_text(encoding='utf-8'))
    return {}

def _json_write_users(users: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    USERS_FILE.write_text(
        json.dumps(users, ensure_ascii=False, indent=2), encoding='utf-8'
    )

def _json_save_user(username: str, data: dict):
    users = _json_load_users()
    users[username] = data
    _json_write_users(users)

def _json_read_shop(shop_id: str, key: str, default=None):
    f = _shop_dir(shop_id) / f'{key}.json'
    if f.exists():
        return json.loads(f.read_text(encoding='utf-8'))
    return default if default is not None else {}

def _json_write_shop(shop_id: str, key: str, data):
    f = _shop_dir(shop_id) / f'{key}.json'
    f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')

def _json_append_sale(shop_id: str, date_str: str, sale: dict):
    f = _shop_dir(shop_id) / f'sales_{date_str}.json'
    existing = json.loads(f.read_text(encoding='utf-8')) if f.exists() else []
    existing.append(sale)
    f.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding='utf-8')

def _json_read_sales(shop_id: str, date_str: str) -> list:
    f = _shop_dir(shop_id) / f'sales_{date_str}.json'
    if f.exists():
        return json.loads(f.read_text(encoding='utf-8'))
    return []


# ════════════════════════════════════════════════════════════════
# STATUS HELPER
# ════════════════════════════════════════════════════════════════

def db_status() -> dict:
    """คืนสถานะ DB สำหรับ /api/health"""
    return {
        'db_enabled':   DB_ENABLED,
        'db_mode':      'postgresql' if DB_ENABLED else 'json_only',
        'pg_available': _PG_AVAILABLE,
        'db_url_set':   bool(DATABASE_URL),
    }
