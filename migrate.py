"""
migrate.py — KAASOD: JSON → PostgreSQL Migration Script
รันครั้งเดียวหลังจาก set DATABASE_URL แล้ว

Usage:
    DATABASE_URL=postgresql://... python migrate.py

จะไม่แตะไฟล์ JSON เดิม (read-only) — safe to re-run
"""

import json
import os
import sys
import logging
from pathlib import Path
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger('migrate')

# ── ตรวจสอบ environment ─────────────────────────────────────────
DATABASE_URL = os.environ.get('DATABASE_URL', '')
if not DATABASE_URL:
    logger.error("❌ กรุณาตั้งค่า DATABASE_URL ก่อนรัน script นี้")
    sys.exit(1)

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    logger.error("❌ ไม่พบ psycopg2 — รัน: pip install psycopg2-binary")
    sys.exit(1)

# ── import db layer ─────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))
from db import init_db, get_conn

# ── JSON paths ──────────────────────────────────────────────────
DATA_DIR  = Path('pos_data')
SHOPS_DIR = DATA_DIR / 'shops'
USERS_FILE = DATA_DIR / 'users.json'


# ════════════════════════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════════════════════════

def safe_json(path: Path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception as e:
        logger.warning(f"  ⚠️  อ่าน {path} ไม่ได้: {e}")
        return None

stats = {
    'users': 0,
    'shop_data': 0,
    'sales': 0,
    'chat_logs': 0,
    'ai_usage': 0,
    'errors': 0,
}


# ════════════════════════════════════════════════════════════════
# MIGRATE USERS
# ════════════════════════════════════════════════════════════════

def migrate_users(conn):
    logger.info("👤 Migrating users...")
    if not USERS_FILE.exists():
        logger.warning("  ไม่พบ users.json — ข้าม")
        return

    users = safe_json(USERS_FILE)
    if not users:
        return

    with conn.cursor() as cur:
        for username, u in users.items():
            try:
                cur.execute("""
                    INSERT INTO users
                        (username, shop_id, shop_name, phone, password,
                         plan, price, status, created_at)
                    VALUES
                        (%(username)s, %(shop_id)s, %(shop_name)s, %(phone)s,
                         %(password)s, %(plan)s, %(price)s, %(status)s, %(created_at)s)
                    ON CONFLICT (username) DO UPDATE SET
                        shop_name  = EXCLUDED.shop_name,
                        phone      = EXCLUDED.phone,
                        plan       = EXCLUDED.plan,
                        price      = EXCLUDED.price,
                        status     = EXCLUDED.status
                """, {
                    'username':   username,
                    'shop_id':    u.get('shop_id', ''),
                    'shop_name':  u.get('shop_name', ''),
                    'phone':      u.get('phone', ''),
                    'password':   u.get('password', ''),
                    'plan':       u.get('plan', 'starter'),
                    'price':      u.get('price', 199),
                    'status':     u.get('status', 'pending'),
                    'created_at': u.get('created_at', ''),
                })
                stats['users'] += 1
                logger.info(f"  ✅ {username} ({u.get('shop_name', '')})")
            except Exception as e:
                stats['errors'] += 1
                logger.error(f"  ❌ {username}: {e}")

    conn.commit()
    logger.info(f"  → {stats['users']} users migrated\n")


# ════════════════════════════════════════════════════════════════
# MIGRATE SHOP DATA (products / stock / members / settings / etc.)
# ════════════════════════════════════════════════════════════════

SHOP_DATA_KEYS = [
    'products', 'stock', 'history', 'members',
    'delivery', 'settings', 'sync_meta',
]

def migrate_shop_data(conn, shop_id: str, shop_name: str):
    for key in SHOP_DATA_KEYS:
        f = SHOPS_DIR / shop_id / f'{key}.json'
        if not f.exists():
            continue
        data = safe_json(f)
        if data is None:
            continue
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO shop_data (shop_id, key, value, updated_at)
                    VALUES (%s, %s, %s, NOW())
                    ON CONFLICT (shop_id, key) DO UPDATE SET
                        value      = EXCLUDED.value,
                        updated_at = NOW()
                """, (shop_id, key, json.dumps(data, ensure_ascii=False)))
            conn.commit()
            stats['shop_data'] += 1
            logger.info(f"    📦 {key} → ok")
        except Exception as e:
            stats['errors'] += 1
            logger.error(f"    ❌ shop_data {key}: {e}")


# ════════════════════════════════════════════════════════════════
# MIGRATE SALES LOG
# ════════════════════════════════════════════════════════════════

def migrate_sales(conn, shop_id: str, username: str):
    shop_path = SHOPS_DIR / shop_id
    sale_files = sorted(shop_path.glob('sales_*.json'))
    if not sale_files:
        return

    logger.info(f"    💰 Sales files: {len(sale_files)}")
    for sf in sale_files:
        # ดึงวันที่จากชื่อไฟล์ sales_YYYY-MM-DD.json
        try:
            date_str = sf.stem.replace('sales_', '')  # "YYYY-MM-DD"
            sale_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            logger.warning(f"    ⚠️  รูปแบบชื่อไฟล์ผิด: {sf.name}")
            continue

        sales = safe_json(sf)
        if not sales:
            continue

        with conn.cursor() as cur:
            for sale in sales:
                try:
                    sale_ts_str = sale.get('_ts', '')
                    try:
                        sale_ts = datetime.strptime(sale_ts_str, '%Y-%m-%d %H:%M:%S')
                    except Exception:
                        sale_ts = datetime.combine(sale_date, datetime.min.time())

                    cur.execute("""
                        INSERT INTO sales_log
                            (shop_id, username, sale_date, sale_ts,
                             total, pay, member, items, raw)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """, (
                        shop_id,
                        sale.get('_username', username),
                        sale_date,
                        sale_ts,
                        sale.get('total', 0),
                        sale.get('pay', ''),
                        sale.get('member', ''),
                        json.dumps(sale.get('items', []), ensure_ascii=False),
                        json.dumps(sale, ensure_ascii=False),
                    ))
                    stats['sales'] += 1
                except Exception as e:
                    stats['errors'] += 1
                    logger.error(f"    ❌ sale record: {e}")

        conn.commit()
        logger.info(f"    ✅ {sf.name} → {len(sales)} records")


# ════════════════════════════════════════════════════════════════
# MIGRATE CHAT LOGS
# ════════════════════════════════════════════════════════════════

def migrate_chat_logs(conn, shop_id: str, username: str):
    f = SHOPS_DIR / shop_id / 'chat_logs.json'
    if not f.exists():
        return
    logs = safe_json(f)
    if not logs:
        return

    with conn.cursor() as cur:
        for log in logs:
            try:
                ts_str = log.get('ts', '')
                try:
                    ts = datetime.strptime(ts_str, '%Y-%m-%d %H:%M:%S')
                except Exception:
                    ts = datetime.now()

                cur.execute("""
                    INSERT INTO chat_logs (shop_id, username, ts, user_msg, ai_reply)
                    VALUES (%s, %s, %s, %s, %s)
                """, (
                    shop_id,
                    log.get('username', username),
                    ts,
                    log.get('user_msg', '')[:300],
                    log.get('ai_reply', '')[:500],
                ))
                stats['chat_logs'] += 1
            except Exception as e:
                stats['errors'] += 1
                logger.error(f"    ❌ chat_log: {e}")

    conn.commit()
    logger.info(f"    💬 chat_logs → {len(logs)} records")


# ════════════════════════════════════════════════════════════════
# MIGRATE AI USAGE
# ════════════════════════════════════════════════════════════════

def migrate_ai_usage(conn, shop_id: str):
    f = SHOPS_DIR / shop_id / 'ai_usage.json'
    if not f.exists():
        return
    data = safe_json(f)
    if not data:
        return

    with conn.cursor() as cur:
        for date_str, count in data.items():
            try:
                cur.execute("""
                    INSERT INTO ai_usage (shop_id, use_date, count)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (shop_id, use_date) DO UPDATE
                        SET count = GREATEST(ai_usage.count, EXCLUDED.count)
                """, (shop_id, date_str, count))
                stats['ai_usage'] += 1
            except Exception as e:
                stats['errors'] += 1
                logger.error(f"    ❌ ai_usage {date_str}: {e}")

    conn.commit()


# ════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════

def main():
    logger.info("=" * 55)
    logger.info("🚀 KAASOD Migration: JSON → PostgreSQL")
    logger.info("=" * 55)

    # 1. สร้าง schema
    logger.info("\n📐 Initializing schema...")
    init_db()

    # 2. เปิด connection ครั้งเดียวสำหรับ migrate
    conn = psycopg2.connect(DATABASE_URL, sslmode='require')

    try:
        # 3. Migrate users
        migrate_users(conn)

        # 4. โหลด users เพื่อวน loop shops
        users = json.loads(USERS_FILE.read_text(encoding='utf-8')) if USERS_FILE.exists() else {}

        for username, u in users.items():
            shop_id   = u.get('shop_id', '')
            shop_name = u.get('shop_name', username)
            shop_path = SHOPS_DIR / shop_id

            if not shop_path.exists():
                logger.info(f"\n🏪 {shop_name} ({shop_id}) — ไม่มีไฟล์ข้อมูล ข้าม")
                continue

            logger.info(f"\n🏪 Migrating shop: {shop_name} ({username})")

            migrate_shop_data(conn, shop_id, shop_name)
            migrate_sales(conn, shop_id, username)
            migrate_chat_logs(conn, shop_id, username)
            migrate_ai_usage(conn, shop_id)

    except KeyboardInterrupt:
        logger.warning("\n⚠️  หยุดโดย user")
    finally:
        conn.close()

    # 5. สรุป
    logger.info("\n" + "=" * 55)
    logger.info("📊 Migration Summary")
    logger.info("=" * 55)
    logger.info(f"  Users migrated    : {stats['users']}")
    logger.info(f"  Shop data records : {stats['shop_data']}")
    logger.info(f"  Sales records     : {stats['sales']}")
    logger.info(f"  Chat log records  : {stats['chat_logs']}")
    logger.info(f"  AI usage records  : {stats['ai_usage']}")
    logger.info(f"  Errors            : {stats['errors']}")
    logger.info("=" * 55)

    if stats['errors'] == 0:
        logger.info("✅ Migration completed successfully!")
        logger.info("\nNext steps:")
        logger.info("  1. ตั้งค่า DATABASE_URL ใน Railway environment")
        logger.info("  2. Deploy app.py เวอร์ชันใหม่")
        logger.info("  3. ตรวจสอบ /api/health ว่า db_mode = 'postgresql'")
        logger.info("  4. Monitor logs 24 ชม. ก่อนลบไฟล์ JSON")
    else:
        logger.warning(f"⚠️  มี {stats['errors']} errors — กรุณาตรวจสอบก่อน deploy")


if __name__ == '__main__':
    main()
