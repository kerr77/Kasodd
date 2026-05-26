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

-- ตาราง branches  (Multi-Branch: สาขาย่อยของ owner)
-- owner_shop_id = shop_id ของ owner หลัก (เจ้าของ)
-- branch_shop_id = shop_id ของสาขา (แต่ละสาขาก็ยัง login เป็น user ของตัวเอง)
CREATE TABLE IF NOT EXISTS branches (
    id             BIGSERIAL PRIMARY KEY,
    owner_shop_id  TEXT NOT NULL,
    branch_shop_id TEXT NOT NULL,
    branch_name    TEXT NOT NULL,
    created_at     TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (owner_shop_id, branch_shop_id)
);
CREATE INDEX IF NOT EXISTS idx_branches_owner ON branches (owner_shop_id);

-- ตาราง bill_deletions  (tracking ทุกครั้งที่ร้านลบบิล)
CREATE TABLE IF NOT EXISTS bill_deletions (
    id          BIGSERIAL PRIMARY KEY,
    shop_id     TEXT NOT NULL,
    username    TEXT,
    deleted_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    bill_time   TEXT,                      -- เวลาของบิลที่ถูกลบ
    bill_total  INTEGER DEFAULT 0,
    bill_items  JSONB,                     -- รายการสินค้าในบิลนั้น
    bill_pay    TEXT,                      -- cash / transfer / free
    reason      TEXT                       -- เหตุผล (optional)
);
CREATE INDEX IF NOT EXISTS idx_deletions_shop    ON bill_deletions (shop_id, deleted_at);
CREATE INDEX IF NOT EXISTS idx_deletions_date    ON bill_deletions (deleted_at);

-- ตาราง menu_additions  (tracking เมนูใหม่ที่ร้านเพิ่มเข้ามา)
CREATE TABLE IF NOT EXISTS menu_additions (
    id          BIGSERIAL PRIMARY KEY,
    shop_id     TEXT NOT NULL,
    username    TEXT,
    added_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    item_name   TEXT NOT NULL,
    item_price  INTEGER DEFAULT 0,
    item_key    TEXT                       -- product key ใน POS
);
CREATE INDEX IF NOT EXISTS idx_menu_shop   ON menu_additions (shop_id, added_at);
CREATE INDEX IF NOT EXISTS idx_menu_name   ON menu_additions (item_name, added_at);

-- ตาราง restock_alerts  (cache การคำนวณ restock prediction)
CREATE TABLE IF NOT EXISTS restock_alerts (
    id          BIGSERIAL PRIMARY KEY,
    shop_id     TEXT NOT NULL,
    product_name TEXT NOT NULL,
    current_stock INTEGER DEFAULT 0,
    avg_daily_sales NUMERIC(10,2) DEFAULT 0,
    days_left   NUMERIC(10,1) DEFAULT 0,
    urgency     TEXT NOT NULL DEFAULT 'ok',   -- 'critical' / 'warning' / 'ok'
    calc_date   DATE NOT NULL DEFAULT CURRENT_DATE,
    UNIQUE (shop_id, product_name, calc_date)
);
CREATE INDEX IF NOT EXISTS idx_restock_shop ON restock_alerts (shop_id, calc_date);
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
# MULTI-BRANCH
# ════════════════════════════════════════════════════════════════

def add_branch(owner_shop_id: str, branch_shop_id: str, branch_name: str) -> bool:
    """เพิ่มสาขาให้ owner"""
    if not DB_ENABLED:
        return False
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO branches (owner_shop_id, branch_shop_id, branch_name)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (owner_shop_id, branch_shop_id) DO UPDATE
                        SET branch_name = EXCLUDED.branch_name
                """, (owner_shop_id, branch_shop_id, branch_name))
        return True
    except Exception as e:
        logger.error(f"add_branch failed: {e}")
        return False


def remove_branch(owner_shop_id: str, branch_shop_id: str) -> bool:
    """ลบสาขาออกจาก group"""
    if not DB_ENABLED:
        return False
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    DELETE FROM branches
                    WHERE owner_shop_id=%s AND branch_shop_id=%s
                """, (owner_shop_id, branch_shop_id))
        return True
    except Exception as e:
        logger.error(f"remove_branch failed: {e}")
        return False


def get_branches(owner_shop_id: str) -> list:
    """ดึงรายการสาขาทั้งหมดของ owner"""
    if not DB_ENABLED:
        return []
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT branch_shop_id, branch_name, created_at
                    FROM branches
                    WHERE owner_shop_id = %s
                    ORDER BY created_at
                """, (owner_shop_id,))
                return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        logger.warning(f"get_branches failed: {e}")
        return []


def get_owner_of_branch(branch_shop_id: str) -> str | None:
    """ค้นหาว่าสาขานี้อยู่ใน group ของ owner ใด"""
    if not DB_ENABLED:
        return None
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT owner_shop_id FROM branches
                    WHERE branch_shop_id = %s LIMIT 1
                """, (branch_shop_id,))
                row = cur.fetchone()
        return row[0] if row else None
    except Exception as e:
        logger.warning(f"get_owner_of_branch failed: {e}")
        return None


def get_branch_summary(shop_ids: list, date_range: list) -> list:
    """ดึง sales summary ของหลายสาขาพร้อมกัน — ใช้ใน multi-branch dashboard"""
    if not DB_ENABLED or not shop_ids or not date_range:
        return []
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT
                        shop_id,
                        sale_date,
                        COUNT(*)                                   AS bills,
                        SUM(total)                                 AS revenue,
                        SUM(CASE WHEN pay='cash'     THEN total ELSE 0 END) AS cash,
                        SUM(CASE WHEN pay='transfer' THEN total ELSE 0 END) AS transfer
                    FROM sales_log
                    WHERE shop_id = ANY(%s) AND sale_date = ANY(%s)
                    GROUP BY shop_id, sale_date
                    ORDER BY shop_id, sale_date
                """, (shop_ids, date_range))
                return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        logger.warning(f"get_branch_summary failed: {e}")
        return []


# ════════════════════════════════════════════════════════════════
# AI RESTOCK PREDICTION
# ════════════════════════════════════════════════════════════════

def calc_restock_alerts(shop_id: str, lookback_days: int = 14) -> list:
    """
    คำนวณ restock prediction จาก sales_log + stock ปัจจุบัน
    คืน list ของ {product_name, current_stock, avg_daily_sales, days_left, urgency}
    urgency: 'critical' (<=3 วัน), 'warning' (<=7 วัน), 'ok'
    """
    from datetime import timedelta, date as date_type

    today = datetime.now().date()
    start = today - timedelta(days=lookback_days)

    # ── ดึง sales ย้อนหลัง lookback_days ─────────────────────────
    daily_sales: dict[str, float] = {}  # product_name → total qty sold
    if DB_ENABLED:
        try:
            with get_conn() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute("""
                        SELECT items FROM sales_log
                        WHERE shop_id = %s
                          AND sale_date >= %s
                          AND sale_date <= %s
                    """, (shop_id, start, today))
                    rows = cur.fetchall()
            for row in rows:
                items = row['items'] or []
                for it in items:
                    name = it.get('name', '').strip()
                    qty  = it.get('qty', 1)
                    if name:
                        daily_sales[name] = daily_sales.get(name, 0) + qty
        except Exception as e:
            logger.warning(f"calc_restock_alerts sales query failed: {e}")
            return []
    else:
        # JSON fallback — อ่านไฟล์ sales ย้อนหลัง
        for i in range(lookback_days):
            d = today - timedelta(days=i)
            sales = _json_read_sales(shop_id, d.strftime('%Y-%m-%d'))
            for s in sales:
                for it in s.get('items', []):
                    name = it.get('name', '').strip()
                    qty  = it.get('qty', 1)
                    if name:
                        daily_sales[name] = daily_sales.get(name, 0) + qty

    if not daily_sales:
        return []

    avg_daily: dict[str, float] = {k: v / lookback_days for k, v in daily_sales.items()}

    # ── ดึง stock ปัจจุบัน ────────────────────────────────────────
    stock_data: dict = read_shop_data(shop_id, 'stock', {})
    products_data: list = read_shop_data(shop_id, 'products', [])

    # build stock map: name → qty
    stock_map: dict[str, int] = {}
    if isinstance(stock_data, dict):
        stock_map = {k: int(v) for k, v in stock_data.items() if str(v).isdigit() or isinstance(v, (int, float))}
    elif isinstance(products_data, list):
        # บางร้านเก็บ stock ใน products array
        for p in products_data:
            name = p.get('name', '').strip()
            qty  = p.get('stock', p.get('qty', None))
            if name and qty is not None:
                stock_map[name] = int(qty)

    # ── คำนวณ days_left และ urgency ──────────────────────────────
    alerts = []
    for name, avg in avg_daily.items():
        if avg <= 0:
            continue
        stock = stock_map.get(name, None)
        if stock is None:
            continue  # ไม่มีข้อมูล stock — ข้าม

        days_left = stock / avg if avg > 0 else 9999

        if days_left <= 3:
            urgency = 'critical'
        elif days_left <= 7:
            urgency = 'warning'
        else:
            urgency = 'ok'

        alerts.append({
            'product_name':    name,
            'current_stock':   stock,
            'avg_daily_sales': round(avg, 2),
            'days_left':       round(days_left, 1),
            'urgency':         urgency,
        })

    # ── เรียงตาม urgency และ days_left ───────────────────────────
    order = {'critical': 0, 'warning': 1, 'ok': 2}
    alerts.sort(key=lambda x: (order[x['urgency']], x['days_left']))

    # ── cache ผลลัพธ์ใน DB (best-effort) ─────────────────────────
    if DB_ENABLED and alerts:
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    for a in alerts:
                        cur.execute("""
                            INSERT INTO restock_alerts
                                (shop_id, product_name, current_stock,
                                 avg_daily_sales, days_left, urgency, calc_date)
                            VALUES (%s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (shop_id, product_name, calc_date) DO UPDATE SET
                                current_stock   = EXCLUDED.current_stock,
                                avg_daily_sales = EXCLUDED.avg_daily_sales,
                                days_left       = EXCLUDED.days_left,
                                urgency         = EXCLUDED.urgency
                        """, (
                            shop_id,
                            a['product_name'],
                            a['current_stock'],
                            a['avg_daily_sales'],
                            a['days_left'],
                            a['urgency'],
                            today,
                        ))
        except Exception as e:
            logger.warning(f"cache restock_alerts failed (non-fatal): {e}")

    return alerts


def get_restock_summary_text(shop_id: str) -> str:
    """
    สร้างข้อความ restock summary สำหรับ inject เข้า AI system prompt
    เพื่อให้ AI รู้ว่าสินค้าไหนกำลังจะหมด
    """
    alerts = calc_restock_alerts(shop_id)
    critical = [a for a in alerts if a['urgency'] == 'critical']
    warning  = [a for a in alerts if a['urgency'] == 'warning']

    if not critical and not warning:
        return ""

    lines = ["⚠️ **ข้อมูลสต็อกที่ต้องสั่งเพิ่ม (จากการวิเคราะห์ยอดขาย 14 วันล่าสุด):**"]

    if critical:
        lines.append("🔴 วิกฤต (เหลือ ≤3 วัน):")
        for a in critical:
            lines.append(
                f"  - {a['product_name']}: คงเหลือ {a['current_stock']} ชิ้น "
                f"(ขายเฉลี่ย {a['avg_daily_sales']:.1f}/วัน → หมดใน ~{a['days_left']:.0f} วัน)"
            )

    if warning:
        lines.append("🟡 ควรสั่ง (เหลือ ≤7 วัน):")
        for a in warning:
            lines.append(
                f"  - {a['product_name']}: คงเหลือ {a['current_stock']} ชิ้น "
                f"(ขายเฉลี่ย {a['avg_daily_sales']:.1f}/วัน → หมดใน ~{a['days_left']:.0f} วัน)"
            )

    return "\n".join(lines)



# ════════════════════════════════════════════════════════════════
# BILL DELETIONS  (ติดตามการลบบิล)
# ════════════════════════════════════════════════════════════════

def log_bill_deletion(shop_id: str, username: str, bill: dict):
    """
    บันทึกทุกครั้งที่ร้านลบบิล
    bill ควรมี: time, total, items (list), pay
    """
    if DB_ENABLED:
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO bill_deletions
                            (shop_id, username, bill_time, bill_total, bill_items, bill_pay)
                        VALUES (%s, %s, %s, %s, %s, %s)
                    """, (
                        shop_id,
                        username,
                        bill.get('time', ''),
                        int(bill.get('total', 0)),
                        json.dumps(bill.get('items', []), ensure_ascii=False),
                        bill.get('pay', ''),
                    ))
        except Exception as e:
            logger.error(f"log_bill_deletion DB failed: {e}")

    # JSON fallback — เก็บไว้ใน shop folder
    logs = _json_read_shop(shop_id, 'bill_deletions', [])
    logs.append({
        'deleted_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'username':   username,
        'bill_time':  bill.get('time', ''),
        'bill_total': int(bill.get('total', 0)),
        'bill_items': bill.get('items', []),
        'bill_pay':   bill.get('pay', ''),
    })
    logs = logs[-500:]  # เก็บไว้สูงสุด 500 รายการ
    _json_write_shop(shop_id, 'bill_deletions', logs)


def get_bill_deletions(shop_id: str, limit: int = 100) -> list:
    """ดึง bill deletion log ของร้านนี้"""
    if DB_ENABLED:
        try:
            with get_conn() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute("""
                        SELECT username, deleted_at, bill_time, bill_total, bill_items, bill_pay
                        FROM bill_deletions
                        WHERE shop_id = %s
                        ORDER BY deleted_at DESC LIMIT %s
                    """, (shop_id, limit))
                    return [dict(r) for r in cur.fetchall()]
        except Exception as e:
            logger.warning(f"get_bill_deletions DB failed, fallback: {e}")

    logs = _json_read_shop(shop_id, 'bill_deletions', [])
    return list(reversed(logs[-limit:]))


def get_all_bill_deletions(days: int = 7, limit: int = 500) -> list:
    """ดึง bill deletion log ทุกร้าน (สำหรับ server admin)"""
    from datetime import timedelta
    if not DB_ENABLED:
        return []
    try:
        cutoff = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT d.shop_id, u.shop_name, d.username,
                           d.deleted_at, d.bill_time, d.bill_total,
                           d.bill_items, d.bill_pay
                    FROM bill_deletions d
                    LEFT JOIN users u ON u.shop_id = d.shop_id
                    WHERE d.deleted_at >= %s
                    ORDER BY d.deleted_at DESC LIMIT %s
                """, (cutoff, limit))
                return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        logger.warning(f"get_all_bill_deletions failed: {e}")
        return []


# ════════════════════════════════════════════════════════════════
# MENU ADDITIONS  (ติดตามเมนูใหม่)
# ════════════════════════════════════════════════════════════════

def log_menu_additions(shop_id: str, username: str, new_items: list):
    """
    บันทึกเมนูใหม่ที่ตรวจพบจากการ sync
    new_items = list ของ product dict ที่เพิ่งเพิ่มเข้ามา (มี name, price, key)
    """
    if not new_items:
        return

    if DB_ENABLED:
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    for item in new_items:
                        cur.execute("""
                            INSERT INTO menu_additions
                                (shop_id, username, item_name, item_price, item_key)
                            VALUES (%s, %s, %s, %s, %s)
                        """, (
                            shop_id,
                            username,
                            item.get('name', '').strip(),
                            int(item.get('price', 0)),
                            item.get('key', ''),
                        ))
        except Exception as e:
            logger.error(f"log_menu_additions DB failed: {e}")

    # JSON fallback
    logs = _json_read_shop(shop_id, 'menu_additions', [])
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    for item in new_items:
        logs.append({
            'added_at':   ts,
            'username':   username,
            'item_name':  item.get('name', '').strip(),
            'item_price': int(item.get('price', 0)),
            'item_key':   item.get('key', ''),
        })
    logs = logs[-1000:]
    _json_write_shop(shop_id, 'menu_additions', logs)


def detect_new_menu_items(shop_id: str, new_products: list) -> list:
    """
    เปรียบเทียบ new_products กับที่เก็บไว้ใน DB
    คืน list ของสินค้าที่เพิ่มใหม่ (ชื่อยังไม่เคยมีมาก่อน)
    """
    if not new_products:
        return []

    old_products = read_shop_data(shop_id, 'products', [])
    old_names = {
        p.get('name', '').strip().lower()
        for p in (old_products if isinstance(old_products, list) else [])
        if p.get('name', '').strip()
    }

    new_items = []
    for p in new_products:
        name = p.get('name', '').strip()
        if name and name.lower() not in old_names:
            new_items.append(p)

    return new_items


def get_menu_additions_trend(days: int = 30, limit: int = 50) -> list:
    """
    รวมสถิติเมนูที่ถูกเพิ่มบ่อยที่สุดข้ามทุกร้าน
    ใช้วิเคราะห์แนวโน้มว่าเมนูอะไรกำลังมาแรง
    Returns list of {item_name, shop_count, total_additions, avg_price}
    """
    from datetime import timedelta
    if not DB_ENABLED:
        return []
    try:
        cutoff = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT
                        item_name,
                        COUNT(DISTINCT shop_id)   AS shop_count,
                        COUNT(*)                  AS total_additions,
                        ROUND(AVG(item_price), 0) AS avg_price,
                        MAX(added_at)             AS last_seen
                    FROM menu_additions
                    WHERE added_at >= %s
                      AND item_name <> ''
                    GROUP BY item_name
                    ORDER BY shop_count DESC, total_additions DESC
                    LIMIT %s
                """, (cutoff, limit))
                return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        logger.warning(f"get_menu_additions_trend failed: {e}")
        return []



def db_status() -> dict:
    """คืนสถานะ DB สำหรับ /api/health"""
    return {
        'db_enabled':   DB_ENABLED,
        'db_mode':      'postgresql' if DB_ENABLED else 'json_only',
        'pg_available': _PG_AVAILABLE,
        'db_url_set':   bool(DATABASE_URL),
    }
