"""
app_v8.py — ค้าสด (KAASOD) SaaS v8
Phase 1: Multi-Branch Dashboard + AI Auto-Restock Prediction
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
GEMINI_MODEL        = 'gemini-3.1-flash-lite'
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

    # ── inject restock context เข้า system_instruction ───────────
    restock_text = DB.get_restock_summary_text(shop_id)
    system_instruction = data.get('system_instruction')
    if restock_text:
        if system_instruction and isinstance(system_instruction, dict):
            existing = system_instruction.get('parts', [{}])[0].get('text', '')
            system_instruction['parts'] = [{'text': existing + '\n\n' + restock_text}]
        else:
            system_instruction = {'parts': [{'text': restock_text}]}

    api_body: dict = {'contents': contents, 'generationConfig': gen_cfg}
    if system_instruction:
        api_body['system_instruction'] = system_instruction

    url  = f'https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_KEY}'
    body = json.dumps(api_body).encode()
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
            # ── ตรวจ menu ใหม่ก่อน write (เปรียบเทียบกับของเดิม) ──────
            if key == 'products':
                new_items = DB.detect_new_menu_items(shop_id, payload[key])
                if new_items:
                    username = session.get('username', '')
                    DB.log_menu_additions(shop_id, username, new_items)
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


@app.route('/api/shop/sale/delete', methods=['POST'])
def delete_sale_endpoint():
    """
    ร้านส่งข้อมูลบิลที่ถูกลบมาบันทึก — server admin จะเห็นได้
    body: { bill: { time, total, items, pay } }
    """
    if 'shop_id' not in session:
        return jsonify({'ok': False}), 401

    shop_id  = session['shop_id']
    username = session.get('username', '')
    d        = request.get_json(silent=True) or {}
    bill     = d.get('bill', d)   # รองรับทั้ง {bill: ...} และส่ง bill ตรงๆ

    DB.log_bill_deletion(shop_id, username, bill)
    return jsonify({'ok': True})


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
# MULTI-BRANCH
# ══════════════════════════════════════════════════════

@app.route('/api/branch/list')
def branch_list():
    """ดึงรายการสาขาทั้งหมดของ owner พร้อม summary ยอดขายวันนี้"""
    if 'shop_id' not in session:
        return jsonify({'ok': False}), 401

    from datetime import timedelta
    owner_shop_id = session['shop_id']
    branches      = DB.get_branches(owner_shop_id)

    if not branches:
        return jsonify({'ok': True, 'branches': [], 'has_branches': False})

    days = int(request.args.get('days', 1))
    today = datetime.now().date()
    date_range = [today - timedelta(days=i) for i in range(days)]

    # รวม owner เองใน summary ด้วย
    all_shop_ids = [owner_shop_id] + [b['branch_shop_id'] for b in branches]
    summary_rows = DB.get_branch_summary(all_shop_ids, date_range)

    # group by shop_id
    summary_map: dict = {}
    for row in summary_rows:
        sid = row['shop_id']
        if sid not in summary_map:
            summary_map[sid] = {'bills': 0, 'revenue': 0, 'cash': 0, 'transfer': 0}
        summary_map[sid]['bills']    += int(row['bills'] or 0)
        summary_map[sid]['revenue']  += int(row['revenue'] or 0)
        summary_map[sid]['cash']     += int(row['cash'] or 0)
        summary_map[sid]['transfer'] += int(row['transfer'] or 0)

    # ดึงชื่อร้านแต่ละสาขาจาก users
    users = DB.load_users()
    shop_name_map = {u['shop_id']: u['shop_name'] for u in users.values()}

    result = []
    for b in branches:
        sid  = b['branch_shop_id']
        stat = summary_map.get(sid, {'bills': 0, 'revenue': 0, 'cash': 0, 'transfer': 0})
        result.append({
            'branch_shop_id': sid,
            'branch_name':    b['branch_name'],
            'shop_name':      shop_name_map.get(sid, b['branch_name']),
            'created_at':     str(b.get('created_at', '')),
            **stat,
        })

    # owner summary
    owner_stat = summary_map.get(owner_shop_id, {'bills': 0, 'revenue': 0, 'cash': 0, 'transfer': 0})
    grand = {
        'bills':    sum(r['bills']    for r in result) + owner_stat['bills'],
        'revenue':  sum(r['revenue']  for r in result) + owner_stat['revenue'],
        'cash':     sum(r['cash']     for r in result) + owner_stat['cash'],
        'transfer': sum(r['transfer'] for r in result) + owner_stat['transfer'],
    }

    return jsonify({
        'ok':          True,
        'has_branches': True,
        'owner_shop_id': owner_shop_id,
        'owner_name':   session.get('shop_name', ''),
        'owner_today':  owner_stat,
        'branches':     result,
        'grand_total':  grand,
        'date_range':   [d.strftime('%Y-%m-%d') for d in date_range],
    })


@app.route('/api/branch/add', methods=['POST'])
def branch_add():
    """เพิ่มสาขาใหม่ — ต้องใช้ plan pro"""
    if 'shop_id' not in session:
        return jsonify({'ok': False}), 401
    if session.get('plan') != 'pro':
        return jsonify({'ok': False, 'msg': 'ฟีเจอร์ Multi-Branch ใช้ได้เฉพาะแพ็กเกจ Pro ครับ\nอัปเกรดได้ที่ Line: @kaasod'}), 403

    d              = request.get_json(silent=True) or {}
    branch_username = d.get('branch_username', '').strip().lower()
    branch_name    = d.get('branch_name', '').strip()

    if not branch_username or not branch_name:
        return jsonify({'ok': False, 'msg': 'กรอกข้อมูลให้ครบ'}), 400

    # ตรวจสอบว่า branch_username มีอยู่จริง
    users = DB.load_users()
    if branch_username not in users:
        return jsonify({'ok': False, 'msg': f'ไม่พบ username: {branch_username}'}), 404

    branch_user    = users[branch_username]
    branch_shop_id = branch_user['shop_id']
    owner_shop_id  = session['shop_id']

    if branch_shop_id == owner_shop_id:
        return jsonify({'ok': False, 'msg': 'ไม่สามารถเพิ่มร้านตัวเองเป็นสาขาได้'}), 400

    ok = DB.add_branch(owner_shop_id, branch_shop_id, branch_name)
    if not ok:
        return jsonify({'ok': False, 'msg': 'เพิ่มสาขาไม่สำเร็จ กรุณาลองใหม่'}), 500

    return jsonify({'ok': True, 'msg': f'เพิ่มสาขา "{branch_name}" ({branch_username}) แล้ว',
                    'branch_shop_id': branch_shop_id})


@app.route('/api/branch/remove', methods=['POST'])
def branch_remove():
    """ลบสาขาออกจาก group"""
    if 'shop_id' not in session:
        return jsonify({'ok': False}), 401

    d              = request.get_json(silent=True) or {}
    branch_shop_id = d.get('branch_shop_id', '').strip()
    if not branch_shop_id:
        return jsonify({'ok': False, 'msg': 'ต้องระบุ branch_shop_id'}), 400

    ok = DB.remove_branch(session['shop_id'], branch_shop_id)
    return jsonify({'ok': ok})


# ══════════════════════════════════════════════════════
# AI RESTOCK PREDICTION
# ══════════════════════════════════════════════════════

@app.route('/api/restock/alerts')
def restock_alerts():
    """ดึง restock alerts สำหรับร้านตัวเอง"""
    if 'shop_id' not in session:
        return jsonify({'ok': False}), 401

    shop_id      = session['shop_id']
    lookback     = int(request.args.get('days', 14))
    alerts       = DB.calc_restock_alerts(shop_id, lookback_days=lookback)
    critical_cnt = sum(1 for a in alerts if a['urgency'] == 'critical')
    warning_cnt  = sum(1 for a in alerts if a['urgency'] == 'warning')

    return jsonify({
        'ok':           True,
        'shop_id':      shop_id,
        'lookback_days': lookback,
        'critical':     critical_cnt,
        'warning':      warning_cnt,
        'alerts':       alerts,
    })


@app.route('/api/restock/branch-alerts')
def restock_branch_alerts():
    """ดึง restock alerts รวมทุกสาขา (pro only)"""
    if 'shop_id' not in session:
        return jsonify({'ok': False}), 401
    if session.get('plan') != 'pro':
        return jsonify({'ok': False, 'msg': 'Pro เท่านั้น'}), 403

    owner_shop_id = session['shop_id']
    branches      = DB.get_branches(owner_shop_id)
    all_ids       = [owner_shop_id] + [b['branch_shop_id'] for b in branches]

    users        = DB.load_users()
    shop_name_map = {u['shop_id']: u['shop_name'] for u in users.values()}
    shop_name_map[owner_shop_id] = session.get('shop_name', 'สาขาหลัก')

    result = []
    for sid in all_ids:
        alerts = DB.calc_restock_alerts(sid)
        critical = [a for a in alerts if a['urgency'] != 'ok']
        if critical:
            result.append({
                'shop_id':   sid,
                'shop_name': shop_name_map.get(sid, sid),
                'alerts':    critical,
            })

    return jsonify({'ok': True, 'shops': result})




@app.route('/api/admin/popular-items')
def admin_popular_items():
    """
    สถิติสินค้ายอดนิยมข้ามร้านทุกร้าน
    Query params:
      days=7        — ย้อนหลังกี่วัน (default 7)
      shop_type=''  — กรองตามประเภทร้าน (ถ้าร้านเก็บ settings.shop_type)
      limit=20      — จำนวนสินค้าสูงสุด
    Response:
      global_top   — top items รวมทุกร้าน
      by_shop_type — top items แยกตามประเภทร้าน
      shop_count   — จำนวนร้านที่มีข้อมูล
    """
    if not is_admin():
        return jsonify({'ok': False}), 401

    from datetime import timedelta
    days      = int(request.args.get('days', 7))
    limit     = int(request.args.get('limit', 20))
    shop_type = request.args.get('shop_type', '').strip()

    today      = datetime.now().date()
    date_range = [today - timedelta(days=i) for i in range(days)]

    users = DB.load_users()
    active_users = {u: d for u, d in users.items() if d.get('status') == 'active'}

    global_count: dict = {}          # item_name → total_qty (all shops)
    by_type_count: dict = {}         # shop_type → {item_name → qty}
    shops_with_data = set()

    for username, user in active_users.items():
        sid = user.get('shop_id', '')
        if not sid:
            continue

        # ดึง shop_type จาก settings (ถ้ามี)
        settings  = DB.read_shop_data(sid, 'settings', {})
        this_type = (settings.get('shop_type') or settings.get('type') or 'ทั่วไป').strip()

        # กรองตาม shop_type ถ้าระบุ
        if shop_type and this_type != shop_type:
            continue

        sales = DB.get_sales(sid, date_range)
        if not sales:
            continue

        shops_with_data.add(sid)
        for sale in sales:
            for item in sale.get('items', []):
                # ข้าม item ที่ราคาติดลบ (เช่น discount line) หรือ promo marker
                if item.get('price', 0) < 0:
                    continue
                if item.get('key', '') == '_promo_':
                    continue
                name = item.get('name', '').strip()
                qty  = int(item.get('qty', 1))
                if not name:
                    continue

                # global
                global_count[name] = global_count.get(name, 0) + qty

                # by_type
                if this_type not in by_type_count:
                    by_type_count[this_type] = {}
                by_type_count[this_type][name] = by_type_count[this_type].get(name, 0) + qty

    # เรียง + จำกัดจำนวน
    global_top = sorted(
        [{'name': k, 'qty': v, 'rank': 0} for k, v in global_count.items()],
        key=lambda x: -x['qty']
    )[:limit]
    for i, it in enumerate(global_top):
        it['rank'] = i + 1

    by_type_result = {}
    for stype, counts in by_type_count.items():
        ranked = sorted(
            [{'name': k, 'qty': v} for k, v in counts.items()],
            key=lambda x: -x['qty']
        )[:limit]
        by_type_result[stype] = ranked

    return jsonify({
        'ok':          True,
        'days':        days,
        'date_from':   date_range[-1].strftime('%Y-%m-%d'),
        'date_to':     date_range[0].strftime('%Y-%m-%d'),
        'shop_count':  len(shops_with_data),
        'global_top':  global_top,
        'by_shop_type': by_type_result,
    })



@app.route('/api/admin/bill-deletions')
def admin_bill_deletions():
    """
    ดู log การลบบิลของทุกร้าน (หรือร้านใดร้านหนึ่ง)
    Query params:
      username=''   — กรองร้านเดียว
      days=7        — ย้อนหลังกี่วัน (DB mode)
      limit=200     — จำนวน record สูงสุด
    """
    if not is_admin():
        return jsonify({'ok': False}), 401

    days     = int(request.args.get('days', 7))
    limit    = int(request.args.get('limit', 200))
    target_u = request.args.get('username', '').strip()

    if target_u:
        # ดูเฉพาะร้านเดียว
        users = DB.load_users()
        u     = users.get(target_u)
        if not u:
            return jsonify({'ok': False, 'msg': f'ไม่พบ {target_u}'}), 404
        logs = DB.get_bill_deletions(u['shop_id'], limit=limit)
        for row in logs:
            row['shop_name'] = u['shop_name']
            row['username']  = target_u
        return jsonify({'ok': True, 'count': len(logs), 'logs': logs})

    # ดูทุกร้าน
    logs = DB.get_all_bill_deletions(days=days, limit=limit)

    # สรุปสถิติ per shop
    summary: dict = {}
    for row in logs:
        sid = row.get('shop_id', '')
        if sid not in summary:
            summary[sid] = {
                'shop_id':   sid,
                'shop_name': row.get('shop_name', sid),
                'count':     0,
                'total_amt': 0,
            }
        summary[sid]['count']     += 1
        summary[sid]['total_amt'] += int(row.get('bill_total', 0))

    return jsonify({
        'ok':           True,
        'days':         days,
        'total_deletions': len(logs),
        'shops_summary': sorted(summary.values(), key=lambda x: -x['count']),
        'logs':          logs,
    })


@app.route('/api/admin/new-menus')
def admin_new_menus():
    """
    ดูเมนูใหม่ที่ร้านต่างๆ เพิ่งเพิ่มเข้ามา — วิเคราะห์แนวโน้มเมนูยอดฮิต
    Query params:
      days=30       — ย้อนหลังกี่วัน
      limit=50      — จำนวน item สูงสุดใน trend
    """
    if not is_admin():
        return jsonify({'ok': False}), 401

    days  = int(request.args.get('days', 30))
    limit = int(request.args.get('limit', 50))

    trend = DB.get_menu_additions_trend(days=days, limit=limit)

    # แปลง Decimal / datetime เป็น str/float สำหรับ JSON
    for row in trend:
        if 'avg_price' in row and row['avg_price'] is not None:
            row['avg_price'] = float(row['avg_price'])
        if 'last_seen' in row and hasattr(row['last_seen'], 'strftime'):
            row['last_seen'] = row['last_seen'].strftime('%Y-%m-%d %H:%M')

    return jsonify({
        'ok':    True,
        'days':  days,
        'count': len(trend),
        'trend': trend,
    })


    users = DB.load_users()
    return jsonify({
        'status':  'ok',
        'version': 'v8',
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
    print(f"🌿 ค้าสด v8 | {GEMINI_MODEL} | starter limit: {STARTER_DAILY_LIMIT}/day | db: {DB.db_status()['db_mode']}")
    app.run(debug=True, host='0.0.0.0', port=5000)
