"""
app_v7.py — ค้าสด (KAASOD) SaaS v7
NEW: PostgreSQL via Supabase (dual-write with JSON fallback)
     ถ้าไม่มี DATABASE_URL จะทำงาน JSON mode เหมือนเดิม 100%
"""

from flask import Flask, request, jsonify, session, redirect, Response
from pathlib import Path
import json, os, hashlib, secrets, urllib.request, urllib.error
from datetime import datetime

# ── import DB layer (แทน JSON helpers เดิม) ──────────────────────
import db as DB

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'kaasod-secret-2026')

ADMIN_KEY           = os.environ.get('ADMIN_KEY', 'kaasod-admin-2026')
GEMINI_KEY          = os.environ.get('GEMINI_API_KEY', '')
GEMINI_MODEL        = 'gemini-3.5-flash-lite'
STARTER_DAILY_LIMIT = 20

# ── สร้าง DB schema ตอน startup (ถ้า DB_ENABLED) ─────────────────
with app.app_context():
    try:
        DB.init_db()
    except Exception as e:
        app.logger.warning(f"init_db skipped: {e}")


# ══════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════

def hash_pw(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def shop_dir(sid):
    """ยังคงใช้สำหรับ legacy backup และ ai_usage fallback"""
    d = DB.SHOPS_DIR / sid
    d.mkdir(parents=True, exist_ok=True)
    return d

def is_admin():
    return session.get('is_admin') is True


# ══════════════════════════════════════════════════════
# PAGES
# ══════════════════════════════════════════════════════

@app.route('/')
def index():
    f = Path('landing.html')
    return f.read_text(encoding='utf-8') if f.exists() else ('<h1>ไม่พบ landing.html</h1>', 404)

@app.route('/pos')
def pos():
    if 'shop_id' not in session:
        return redirect('/')
    f = Path('index.html')
    return f.read_text(encoding='utf-8') if f.exists() else ('<h1>ไม่พบ index.html</h1>', 404)

@app.route('/admin')
def admin_page():
    f = Path('admin.html')
    return f.read_text(encoding='utf-8') if f.exists() else ('<h1>ไม่พบ admin.html</h1>', 404)

@app.route('/sw.js')
def sw():
    f = Path('sw.js')
    return Response(f.read_text(), mimetype='application/javascript') if f.exists() else ('', 404)


# ══════════════════════════════════════════════════════
# AUTH — USER
# ══════════════════════════════════════════════════════

@app.route('/api/register', methods=['POST'])
def register():
    d = request.get_json(silent=True) or {}
    shop_name = d.get('shop_name', '').strip()
    username  = d.get('username', '').strip().lower()
    password  = d.get('password', '').strip()
    phone     = d.get('phone', '').strip()
    plan      = d.get('plan', 'starter')
    if plan not in ('starter', 'pro'):
        plan = 'starter'
    if not all([shop_name, username, password, phone]):
        return jsonify({'ok': False, 'msg': 'กรอกข้อมูลให้ครบครับ'}), 400

    users = DB.load_users()
    if username in users:
        return jsonify({'ok': False, 'msg': 'ชื่อผู้ใช้นี้มีแล้วครับ'}), 400

    price   = 199 if plan == 'starter' else 399
    shop_id = secrets.token_hex(6)
    user_data = {
        'shop_id': shop_id, 'shop_name': shop_name, 'phone': phone,
        'password': hash_pw(password), 'plan': plan, 'price': price,
        'status': 'pending', 'created_at': datetime.now().strftime('%Y-%m-%d %H:%M'),
    }
    DB.save_user(username, user_data)
    shop_dir(shop_id)  # สร้าง folder สำหรับ JSON fallback
    return jsonify({'ok': True, 'plan': plan, 'price': price})


@app.route('/api/login', methods=['POST'])
def login():
    d = request.get_json(silent=True) or {}
    username = d.get('username', '').strip().lower()
    password = d.get('password', '').strip()
    users    = DB.load_users()
    user     = users.get(username)
    if not user or user['password'] != hash_pw(password):
        return jsonify({'ok': False, 'msg': 'ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง'}), 401
    if user['status'] == 'pending':
        return jsonify({'ok': False, 'msg': 'รอยืนยันการโอนเงินก่อนนะครับ ติดต่อ Line: @kaasod'}), 403
    session['shop_id']   = user['shop_id']
    session['shop_name'] = user['shop_name']
    session['username']  = username
    session['plan']      = user['plan']
    return jsonify({'ok': True, 'shop_name': user['shop_name'], 'plan': user['plan']})


@app.route('/api/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'ok': True})


@app.route('/api/me')
def me():
    if 'shop_id' not in session:
        return jsonify({'ok': False}), 401
    shop_id   = session['shop_id']
    plan      = session.get('plan', 'starter')
    usage     = DB.get_today_usage(shop_id)
    remaining = None if plan == 'pro' else max(0, STARTER_DAILY_LIMIT - usage)
    return jsonify({
        'ok': True,
        'shop_id':        shop_id,
        'shop_name':      session['shop_name'],
        'username':       session['username'],
        'plan':           plan,
        'ai_usage_today': usage,
        'ai_remaining':   remaining,
        'ai_daily_limit': STARTER_DAILY_LIMIT if plan == 'starter' else None,
    })


# ══════════════════════════════════════════════════════
# AI CHAT
# ══════════════════════════════════════════════════════

@app.route('/api/chat', methods=['POST'])
def chat():
    if 'shop_id' not in session:
        return jsonify({'error': {'code': 401, 'message': 'ไม่ได้ login', 'status': 'UNAUTHORIZED'}}), 401
    if not GEMINI_KEY:
        return jsonify({'error': {'code': 500, 'message': 'ยังไม่ได้ตั้งค่า GEMINI_API_KEY', 'status': 'NO_KEY'}}), 500

    shop_id = session['shop_id']
    plan    = session.get('plan', 'starter')

    if plan == 'starter':
        usage = DB.get_today_usage(shop_id)
        if usage >= STARTER_DAILY_LIMIT:
            return jsonify({'error': {
                'code': 429,
                'message': f'⚠️ ใช้ AI ครบ {STARTER_DAILY_LIMIT} ข้อความวันนี้แล้วครับ\n\n🚀 อัปเกรดเป็น Pro 399฿/เดือน ใช้ได้ไม่จำกัด!\nติดต่อ Line: @kaasod',
                'status': 'QUOTA_EXCEEDED'
            }}), 429

    data     = request.get_json(silent=True) or {}
    contents = data.get('contents', [])
    gen_cfg  = data.get('generationConfig', {'maxOutputTokens': 1000, 'temperature': 0.7})
    if not contents:
        return jsonify({'error': {'code': 400, 'message': 'ไม่มีข้อความ', 'status': 'EMPTY'}}), 400

    url  = f'https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_KEY}'
    body = json.dumps({'contents': contents, 'generationConfig': gen_cfg}).encode()
    req  = urllib.request.Request(url, data=body, headers={'Content-Type': 'application/json'})

    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            resp      = json.loads(r.read())
            new_count = DB.increment_usage(shop_id)

            # บันทึก chat log ผ่าน DB layer
            try:
                user_msg = ''
                ai_reply = ''
                for c in contents[::-1]:
                    if c.get('role') == 'user':
                        parts = c.get('parts', [])
                        user_msg = parts[0].get('text', '') if parts else ''
                        break
                cands = resp.get('candidates', [])
                if cands:
                    parts = cands[0].get('content', {}).get('parts', [])
                    ai_reply = parts[0].get('text', '') if parts else ''
                DB.append_chat_log(
                    shop_id,
                    session.get('username', ''),
                    user_msg,
                    ai_reply
                )
            except Exception:
                pass

            if plan == 'starter':
                remaining = max(0, STARTER_DAILY_LIMIT - new_count)
                resp['_quota'] = {
                    'used': new_count, 'remaining': remaining,
                    'limit': STARTER_DAILY_LIMIT, 'warn': remaining <= 5
                }
            return jsonify(resp)

    except urllib.error.HTTPError as e:
        err = json.loads(e.read().decode())
        return jsonify(err), e.code
    except Exception as e:
        return jsonify({'error': {'code': 500, 'message': str(e), 'status': 'SERVER_ERROR'}}), 500


@app.route('/api/chat/status')
def chat_status():
    if 'shop_id' not in session:
        return jsonify({'ai_ready': False})
    shop_id   = session['shop_id']
    plan      = session.get('plan', 'starter')
    usage     = DB.get_today_usage(shop_id)
    remaining = None if plan == 'pro' else max(0, STARTER_DAILY_LIMIT - usage)
    return jsonify({'ai_ready': bool(GEMINI_KEY), 'plan': plan,
                    'usage_today': usage, 'remaining': remaining})


# ══════════════════════════════════════════════════════
# CLOUD SYNC — ข้อมูลร้าน
# ══════════════════════════════════════════════════════

@app.route('/api/shop/sync', methods=['POST'])
def shop_sync_push():
    if 'shop_id' not in session:
        return jsonify({'ok': False, 'error': 'ไม่ได้ login'}), 401

    shop_id = session['shop_id']
    payload = request.get_json(silent=True) or {}
    ts      = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    allowed = ['products', 'stock', 'history', 'members', 'delivery', 'settings']
    for key in allowed:
        if key in payload:
            DB.write_shop_data(shop_id, key, payload[key])

    meta = DB.read_shop_data(shop_id, 'sync_meta', {})
    meta['last_sync']   = ts
    meta['keys_synced'] = [k for k in allowed if k in payload]
    DB.write_shop_data(shop_id, 'sync_meta', meta)

    return jsonify({'ok': True, 'synced_at': ts})


@app.route('/api/shop/sync', methods=['GET'])
def shop_sync_pull():
    if 'shop_id' not in session:
        return jsonify({'ok': False, 'error': 'ไม่ได้ login'}), 401

    shop_id = session['shop_id']
    result  = {}
    for key in ['products', 'stock', 'history', 'members', 'delivery', 'settings']:
        data = DB.read_shop_data(shop_id, key, None)
        if data is not None:
            result[key] = data

    meta = DB.read_shop_data(shop_id, 'sync_meta', {})
    return jsonify({
        'ok':        True,
        'has_data':  bool(result),
        'last_sync': meta.get('last_sync'),
        'data':      result,
    })


# ══════════════════════════════════════════════════════
# SALES
# ══════════════════════════════════════════════════════

@app.route('/api/shop/sale', methods=['POST'])
def record_sale_endpoint():
    if 'shop_id' not in session:
        return jsonify({'ok': False}), 401

    shop_id  = session['shop_id']
    username = session.get('username', '')
    sale     = request.get_json(silent=True) or {}
    ts       = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    today    = datetime.now().strftime('%Y-%m-%d')

    sale['_ts']       = ts
    sale['_date']     = today
    sale['_username'] = username

    DB.record_sale(shop_id, username, sale)
    return jsonify({'ok': True, 'ts': ts})


@app.route('/api/admin/sales')
def admin_sales():
    if not is_admin():
        return jsonify({'ok': False}), 401

    from datetime import timedelta, date as date_type
    users    = DB.load_users()
    target_u = request.args.get('username')
    date_str = request.args.get('date')
    days     = int(request.args.get('days', 1))

    today = datetime.now().date()
    if date_str:
        try:
            target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
            date_range  = [target_date]
        except Exception:
            date_range = [today]
    else:
        date_range = [today - timedelta(days=i) for i in range(days)]

    result = {}
    grand_total    = 0
    grand_bills    = 0
    grand_cash     = 0
    grand_transfer = 0

    for username, user in users.items():
        if target_u and username != target_u:
            continue
        if user['status'] != 'active':
            continue

        shop_id    = user['shop_id']
        shop_sales = DB.get_sales(shop_id, date_range)

        if not shop_sales and not target_u:
            continue

        total    = sum(s.get('total', 0) for s in shop_sales if s.get('pay') not in ('free',))
        cash     = sum(s.get('total', 0) for s in shop_sales if s.get('pay') == 'cash')
        transfer = sum(s.get('total', 0) for s in shop_sales if s.get('pay') == 'transfer')
        free_cnt = sum(1 for s in shop_sales if s.get('pay') == 'free')
        debt     = sum(s.get('debtRemaining', 0) for s in shop_sales)

        item_count = {}
        for s in shop_sales:
            for it in s.get('items', []):
                if it.get('price', 0) >= 0:
                    name = it.get('name', '?')
                    item_count[name] = item_count.get(name, 0) + it.get('qty', 1)
        top_items = sorted(item_count.items(), key=lambda x: -x[1])[:5]

        result[username] = {
            'shop_name': user['shop_name'],
            'plan':      user['plan'],
            'bills':     len(shop_sales),
            'total':     total,
            'cash':      cash,
            'transfer':  transfer,
            'free':      free_cnt,
            'debt':      debt,
            'top_items': [{'name': k, 'qty': v} for k, v in top_items],
            'sales':     shop_sales[-50:],
        }
        grand_total    += total
        grand_bills    += len(shop_sales)
        grand_cash     += cash
        grand_transfer += transfer

    return jsonify({
        'ok':      True,
        'date_range': [d.strftime('%Y-%m-%d') for d in date_range],
        'summary': {
            'total':    grand_total,
            'bills':    grand_bills,
            'cash':     grand_cash,
            'transfer': grand_transfer,
        },
        'shops': result,
    })


@app.route('/api/admin/sales/today')
def admin_sales_today():
    return admin_sales()


# ══════════════════════════════════════════════════════
# ADMIN — USERS / MANAGE
# ══════════════════════════════════════════════════════

@app.route('/api/admin/login', methods=['POST'])
def admin_login():
    d   = request.get_json(silent=True) or {}
    key = d.get('key', '').strip()
    if key != ADMIN_KEY:
        return jsonify({'ok': False, 'msg': 'รหัส Admin ไม่ถูกต้อง'}), 403
    session['is_admin'] = True
    return jsonify({'ok': True})

@app.route('/api/admin/logout', methods=['POST'])
def admin_logout():
    session.pop('is_admin', None)
    return jsonify({'ok': True})

@app.route('/api/admin/me')
def admin_me():
    if not is_admin():
        return jsonify({'ok': False}), 401
    return jsonify({'ok': True})


@app.route('/api/admin/users')
def admin_users():
    if not is_admin():
        return jsonify({'ok': False}), 401
    users     = DB.load_users()
    user_list = []
    for username, u in users.items():
        shop_id = u.get('shop_id', '')
        meta    = DB.read_shop_data(shop_id, 'sync_meta', {}) if shop_id else {}
        user_list.append({
            'username':   username,
            'shop_id':    shop_id,
            'shop_name':  u.get('shop_name', ''),
            'phone':      u.get('phone', ''),
            'plan':       u.get('plan', 'starter'),
            'price':      u.get('price', 199),
            'status':     u.get('status', 'pending'),
            'created_at': u.get('created_at', ''),
            'ai_today':   DB.get_today_usage(shop_id) if shop_id else 0,
            'last_sync':  meta.get('last_sync'),
        })
    user_list.sort(key=lambda x: (x['status'] != 'pending', x['created_at']), reverse=False)
    active  = sum(1 for u in users.values() if u['status'] == 'active')
    pending = sum(1 for u in users.values() if u['status'] == 'pending')
    revenue = sum(u['price'] for u in users.values() if u['status'] == 'active')
    return jsonify({'ok': True, 'users': user_list,
                    'summary': {'total': len(users), 'active': active,
                                'pending': pending, 'revenue': revenue}})


@app.route('/api/admin/approve/<username>', methods=['POST'])
def admin_approve(username):
    if not is_admin(): return jsonify({'ok': False}), 401
    users = DB.load_users()
    if username not in users: return jsonify({'ok': False, 'msg': f'ไม่พบ {username}'}), 404
    users[username]['status'] = 'active'
    DB.save_user(username, users[username])
    u = users[username]
    return jsonify({'ok': True, 'msg': f"อนุมัติ {username} ({u['shop_name']}) แล้ว"})


@app.route('/api/admin/suspend/<username>', methods=['POST'])
def admin_suspend(username):
    if not is_admin(): return jsonify({'ok': False}), 401
    users = DB.load_users()
    if username not in users: return jsonify({'ok': False, 'msg': f'ไม่พบ {username}'}), 404
    users[username]['status'] = 'pending'
    DB.save_user(username, users[username])
    u = users[username]
    return jsonify({'ok': True, 'msg': f"ระงับ {username} ({u['shop_name']}) แล้ว"})


@app.route('/api/admin/edit/<username>', methods=['POST'])
def admin_edit(username):
    if not is_admin(): return jsonify({'ok': False}), 401
    users = DB.load_users()
    if username not in users: return jsonify({'ok': False, 'msg': f'ไม่พบ {username}'}), 404
    d         = request.get_json(silent=True) or {}
    shop_name = d.get('shop_name', '').strip()
    phone     = d.get('phone', '').strip()
    plan      = d.get('plan', '')
    status    = d.get('status', '')
    if not shop_name or not phone: return jsonify({'ok': False, 'msg': 'กรอกข้อมูลให้ครบ'}), 400
    if plan not in ('starter', 'pro'): return jsonify({'ok': False, 'msg': 'แพ็กเกจไม่ถูกต้อง'}), 400
    if status not in ('active', 'pending'): return jsonify({'ok': False, 'msg': 'สถานะไม่ถูกต้อง'}), 400
    users[username].update({'shop_name': shop_name, 'phone': phone, 'plan': plan,
                            'price': 199 if plan == 'starter' else 399, 'status': status})
    DB.save_user(username, users[username])
    return jsonify({'ok': True, 'msg': f'อัปเดต {username} แล้ว'})


@app.route('/api/admin/reset_password/<username>', methods=['POST'])
def admin_reset_password(username):
    if not is_admin(): return jsonify({'ok': False}), 401
    users    = DB.load_users()
    if username not in users: return jsonify({'ok': False, 'msg': f'ไม่พบ {username}'}), 404
    d        = request.get_json(silent=True) or {}
    password = d.get('password', '').strip()
    if len(password) < 4: return jsonify({'ok': False, 'msg': 'รหัสผ่านต้องมีอย่างน้อย 4 ตัว'}), 400
    users[username]['password'] = hash_pw(password)
    DB.save_user(username, users[username])
    return jsonify({'ok': True, 'msg': f'รีเซ็ตรหัสผ่านของ {username} แล้ว'})


@app.route('/api/admin/delete/<username>', methods=['POST'])
def admin_delete(username):
    if not is_admin(): return jsonify({'ok': False}), 401
    users = DB.load_users()
    if username not in users: return jsonify({'ok': False, 'msg': f'ไม่พบ {username}'}), 404
    shop_name = users[username].get('shop_name', username)
    DB.delete_user(username)
    return jsonify({'ok': True, 'msg': f'ลบร้าน {shop_name} แล้ว'})


@app.route('/api/admin/chat-logs')
def admin_chat_logs():
    if not is_admin(): return jsonify({'ok': False}), 401
    users  = DB.load_users()
    result = {}
    for username, u in users.items():
        if u['status'] != 'active': continue
        shop_id = u.get('shop_id', '')
        logs    = DB.get_chat_logs(shop_id, limit=50)
        result[username] = {
            'shop_name': u['shop_name'], 'plan': u['plan'],
            'status': u['status'], 'logs': logs,
        }
    return jsonify({'ok': True, 'data': result})


# ══════════════════════════════════════════════════════
# LEGACY BACKUP (v5/v6 compat — ยังคงใช้ JSON files)
# ══════════════════════════════════════════════════════

@app.route('/api/sync', methods=['POST'])
def sync_data():
    if 'shop_id' not in session: return jsonify({'error': 'ไม่ได้ login'}), 401
    payload  = request.get_json(silent=True) or {}
    date_str = datetime.now().strftime('%Y-%m-%d')
    ts       = datetime.now().strftime('%Y%m%d_%H%M%S')
    d        = shop_dir(session['shop_id'])
    f        = d / f'backup_{date_str}.json'
    f.write_text(json.dumps({'synced_at': ts, 'data': payload}, ensure_ascii=False, indent=2), encoding='utf-8')
    return jsonify({'status': 'ok', 'synced_at': ts})

@app.route('/api/sync', methods=['GET'])
def get_sync():
    if 'shop_id' not in session: return jsonify({'status': 'empty', 'data': {}})
    d     = shop_dir(session['shop_id'])
    files = sorted(d.glob('backup_*.json'), reverse=True)
    if not files: return jsonify({'status': 'empty', 'data': {}})
    return jsonify({'status': 'ok', **json.loads(files[0].read_text(encoding='utf-8'))})


# ══════════════════════════════════════════════════════
# HEALTH
# ══════════════════════════════════════════════════════

@app.route('/api/health')
def health():
    users = DB.load_users()
    return jsonify({
        'status':  'ok',
        'version': 'v7',
        'model':   GEMINI_MODEL,
        'starter_limit': STARTER_DAILY_LIMIT,
        'time':    datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'total':   len(users),
        'active':  sum(1 for u in users.values() if u['status'] == 'active'),
        'pro':     sum(1 for u in users.values() if u['plan'] == 'pro' and u['status'] == 'active'),
        'starter': sum(1 for u in users.values() if u['plan'] == 'starter' and u['status'] == 'active'),
        'ai_key':  bool(GEMINI_KEY),
        **DB.db_status(),   # ← เพิ่ม db_mode / db_enabled
    })


# legacy admin URL
@app.route('/admin/approve/<username>')
def approve_legacy(username):
    if request.args.get('key') != ADMIN_KEY: return 'ไม่มีสิทธิ์', 403
    users = DB.load_users()
    if username not in users: return f'ไม่พบ {username}', 404
    users[username]['status'] = 'active'
    DB.save_user(username, users[username])
    u = users[username]
    return f"✅ อนุมัติ {username} ({u['shop_name']}) {u['plan'].upper()} {u['price']}฿ สำเร็จ"


if __name__ == '__main__':
    print(f"🌿 ค้าสด v7 | {GEMINI_MODEL} | starter limit: {STARTER_DAILY_LIMIT}/day | db: {DB.db_status()['db_mode']}")
    app.run(debug=True, host='0.0.0.0', port=5000)
