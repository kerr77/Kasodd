"""
app_v5.py — ค้าสด (KAASOD) SaaS v5
- Starter 199฿: AI 20 ข้อความ/วัน
- Pro 399฿: AI ไม่จำกัด + วิเคราะห์ยอดขาย + ประวัติไม่จำกัด
- Admin Panel: session-based, full CRUD
"""

from flask import Flask, request, jsonify, session, redirect, Response
from pathlib import Path
import json, os, hashlib, secrets, urllib.request, urllib.error
from datetime import datetime

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'kaasod-secret-2026')

DATA_DIR   = Path('pos_data')
SHOPS_DIR  = DATA_DIR / 'shops'
USERS_FILE = DATA_DIR / 'users.json'
for d in [DATA_DIR, SHOPS_DIR]:
    d.mkdir(parents=True, exist_ok=True)

ADMIN_KEY    = os.environ.get('ADMIN_KEY', 'kaasod-admin-2026')
GEMINI_KEY   = os.environ.get('GEMINI_API_KEY', '')
GEMINI_MODEL = 'gemini-2.0-flash-lite'

STARTER_DAILY_LIMIT = 20  # ข้อความ/วัน สำหรับ starter

# ── helpers ────────────────────────────────────────────────

def load_users():
    if USERS_FILE.exists():
        return json.loads(USERS_FILE.read_text(encoding='utf-8'))
    return {}

def save_users(u):
    USERS_FILE.write_text(json.dumps(u, ensure_ascii=False, indent=2), encoding='utf-8')

def hash_pw(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def shop_dir(sid):
    d = SHOPS_DIR / sid
    d.mkdir(exist_ok=True)
    return d

def get_usage_file(shop_id):
    return shop_dir(shop_id) / 'ai_usage.json'

def get_today_usage(shop_id):
    """นับจำนวน AI message วันนี้ของร้านนี้"""
    f     = get_usage_file(shop_id)
    today = datetime.now().strftime('%Y-%m-%d')
    if not f.exists():
        return 0
    data = json.loads(f.read_text(encoding='utf-8'))
    return data.get(today, 0)

def increment_usage(shop_id):
    """เพิ่ม count AI message วันนี้"""
    f     = get_usage_file(shop_id)
    today = datetime.now().strftime('%Y-%m-%d')
    data  = json.loads(f.read_text(encoding='utf-8')) if f.exists() else {}
    data[today] = data.get(today, 0) + 1
    # เก็บแค่ 7 วันล่าสุด
    keys = sorted(data.keys(), reverse=True)[:7]
    data = {k: data[k] for k in keys}
    f.write_text(json.dumps(data), encoding='utf-8')
    return data[today]

def is_admin():
    return session.get('is_admin') is True

# ── pages ──────────────────────────────────────────────────

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

# ── auth (user) ─────────────────────────────────────────────

@app.route('/api/register', methods=['POST'])
def register():
    d         = request.get_json(silent=True) or {}
    shop_name = d.get('shop_name', '').strip()
    username  = d.get('username', '').strip().lower()
    password  = d.get('password', '').strip()
    phone     = d.get('phone', '').strip()
    plan      = d.get('plan', 'starter')
    if plan not in ('starter', 'pro'):
        plan = 'starter'
    if not all([shop_name, username, password, phone]):
        return jsonify({'ok': False, 'msg': 'กรอกข้อมูลให้ครบครับ'}), 400
    users = load_users()
    if username in users:
        return jsonify({'ok': False, 'msg': 'ชื่อผู้ใช้นี้มีแล้วครับ'}), 400
    price   = 199 if plan == 'starter' else 399
    shop_id = secrets.token_hex(6)
    users[username] = {
        'shop_id': shop_id, 'shop_name': shop_name, 'phone': phone,
        'password': hash_pw(password), 'plan': plan, 'price': price,
        'status': 'pending', 'created_at': datetime.now().strftime('%Y-%m-%d %H:%M'),
    }
    save_users(users)
    shop_dir(shop_id)
    return jsonify({'ok': True, 'plan': plan, 'price': price})

@app.route('/api/login', methods=['POST'])
def login():
    d        = request.get_json(silent=True) or {}
    username = d.get('username', '').strip().lower()
    password = d.get('password', '').strip()
    users    = load_users()
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
    shop_id = session['shop_id']
    plan    = session.get('plan', 'starter')
    usage   = get_today_usage(shop_id)
    remaining = None if plan == 'pro' else max(0, STARTER_DAILY_LIMIT - usage)
    return jsonify({
        'ok': True,
        'shop_id':   shop_id,
        'shop_name': session['shop_name'],
        'username':  session['username'],
        'plan':      plan,
        'ai_usage_today':  usage,
        'ai_remaining':    remaining,
        'ai_daily_limit':  STARTER_DAILY_LIMIT if plan == 'starter' else None,
    })

# ── AI chat ────────────────────────────────────────────────

@app.route('/api/chat', methods=['POST'])
def chat():
    if 'shop_id' not in session:
        return jsonify({'error': {'code': 401, 'message': 'ไม่ได้ login', 'status': 'UNAUTHORIZED'}}), 401

    if not GEMINI_KEY:
        return jsonify({'error': {'code': 500, 'message': 'ยังไม่ได้ตั้งค่า GEMINI_API_KEY', 'status': 'NO_KEY'}}), 500

    shop_id = session['shop_id']
    plan    = session.get('plan', 'starter')

    if plan == 'starter':
        usage = get_today_usage(shop_id)
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
            resp = json.loads(r.read())

        new_count = increment_usage(shop_id)

        if plan == 'starter':
            remaining = max(0, STARTER_DAILY_LIMIT - new_count)
            resp['_quota'] = {
                'used': new_count,
                'remaining': remaining,
                'limit': STARTER_DAILY_LIMIT,
                'warn': remaining <= 5
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
    usage     = get_today_usage(shop_id)
    remaining = None if plan == 'pro' else max(0, STARTER_DAILY_LIMIT - usage)
    return jsonify({
        'ai_ready':    bool(GEMINI_KEY),
        'plan':        plan,
        'usage_today': usage,
        'remaining':   remaining,
    })

# ── sync/backup ────────────────────────────────────────────

@app.route('/api/sync', methods=['POST'])
def sync_data():
    if 'shop_id' not in session:
        return jsonify({'error': 'ไม่ได้ login'}), 401
    payload  = request.get_json(silent=True) or {}
    date_str = datetime.now().strftime('%Y-%m-%d')
    ts       = datetime.now().strftime('%Y%m%d_%H%M%S')
    d        = shop_dir(session['shop_id'])
    f        = d / f'backup_{date_str}.json'
    f.write_text(json.dumps({'synced_at': ts, 'data': payload}, ensure_ascii=False, indent=2), encoding='utf-8')
    return jsonify({'status': 'ok', 'synced_at': ts})

@app.route('/api/sync', methods=['GET'])
def get_sync():
    if 'shop_id' not in session:
        return jsonify({'status': 'empty', 'data': {}})
    d     = shop_dir(session['shop_id'])
    files = sorted(d.glob('backup_*.json'), reverse=True)
    if not files:
        return jsonify({'status': 'empty', 'data': {}})
    return jsonify({'status': 'ok', **json.loads(files[0].read_text(encoding='utf-8'))})

# ── admin auth ─────────────────────────────────────────────

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

# ── admin users ────────────────────────────────────────────

@app.route('/api/admin/users')
def admin_users():
    if not is_admin():
        return jsonify({'ok': False}), 401
    users = load_users()
    user_list = []
    for username, u in users.items():
        shop_id = u.get('shop_id', '')
        user_list.append({
            'username':   username,
            'shop_id':    shop_id,
            'shop_name':  u.get('shop_name', ''),
            'phone':      u.get('phone', ''),
            'plan':       u.get('plan', 'starter'),
            'price':      u.get('price', 199),
            'status':     u.get('status', 'pending'),
            'created_at': u.get('created_at', ''),
            'ai_today':   get_today_usage(shop_id) if shop_id else 0,
        })
    # pending ขึ้นก่อน แล้วเรียงตาม created_at
    user_list.sort(key=lambda x: (x['status'] != 'pending', x['created_at']), reverse=False)
    active  = sum(1 for u in users.values() if u['status'] == 'active')
    pending = sum(1 for u in users.values() if u['status'] == 'pending')
    revenue = sum(u['price'] for u in users.values() if u['status'] == 'active')
    return jsonify({
        'ok': True,
        'users': user_list,
        'summary': {
            'total':   len(users),
            'active':  active,
            'pending': pending,
            'revenue': revenue,
        }
    })

@app.route('/api/admin/approve/<username>', methods=['POST'])
def admin_approve(username):
    if not is_admin():
        return jsonify({'ok': False}), 401
    users = load_users()
    if username not in users:
        return jsonify({'ok': False, 'msg': f'ไม่พบ {username}'}), 404
    users[username]['status'] = 'active'
    save_users(users)
    u = users[username]
    return jsonify({'ok': True, 'msg': f"อนุมัติ {username} ({u['shop_name']}) แล้ว"})

@app.route('/api/admin/suspend/<username>', methods=['POST'])
def admin_suspend(username):
    if not is_admin():
        return jsonify({'ok': False}), 401
    users = load_users()
    if username not in users:
        return jsonify({'ok': False, 'msg': f'ไม่พบ {username}'}), 404
    users[username]['status'] = 'pending'
    save_users(users)
    u = users[username]
    return jsonify({'ok': True, 'msg': f"ระงับ {username} ({u['shop_name']}) แล้ว"})

@app.route('/api/admin/edit/<username>', methods=['POST'])
def admin_edit(username):
    if not is_admin():
        return jsonify({'ok': False}), 401
    users = load_users()
    if username not in users:
        return jsonify({'ok': False, 'msg': f'ไม่พบ {username}'}), 404
    d         = request.get_json(silent=True) or {}
    shop_name = d.get('shop_name', '').strip()
    phone     = d.get('phone', '').strip()
    plan      = d.get('plan', '')
    status    = d.get('status', '')
    if not shop_name or not phone:
        return jsonify({'ok': False, 'msg': 'กรอกข้อมูลให้ครบ'}), 400
    if plan not in ('starter', 'pro'):
        return jsonify({'ok': False, 'msg': 'แพ็กเกจไม่ถูกต้อง'}), 400
    if status not in ('active', 'pending'):
        return jsonify({'ok': False, 'msg': 'สถานะไม่ถูกต้อง'}), 400
    users[username]['shop_name'] = shop_name
    users[username]['phone']     = phone
    users[username]['plan']      = plan
    users[username]['price']     = 199 if plan == 'starter' else 399
    users[username]['status']    = status
    save_users(users)
    return jsonify({'ok': True, 'msg': f'อัปเดต {username} แล้ว'})

@app.route('/api/admin/reset_password/<username>', methods=['POST'])
def admin_reset_password(username):
    if not is_admin():
        return jsonify({'ok': False}), 401
    users = load_users()
    if username not in users:
        return jsonify({'ok': False, 'msg': f'ไม่พบ {username}'}), 404
    d        = request.get_json(silent=True) or {}
    password = d.get('password', '').strip()
    if len(password) < 4:
        return jsonify({'ok': False, 'msg': 'รหัสผ่านต้องมีอย่างน้อย 4 ตัว'}), 400
    users[username]['password'] = hash_pw(password)
    save_users(users)
    return jsonify({'ok': True, 'msg': f'รีเซ็ตรหัสผ่านของ {username} แล้ว'})

@app.route('/api/admin/delete/<username>', methods=['POST'])
def admin_delete(username):
    if not is_admin():
        return jsonify({'ok': False}), 401
    users = load_users()
    if username not in users:
        return jsonify({'ok': False, 'msg': f'ไม่พบ {username}'}), 404
    shop_name = users[username].get('shop_name', username)
    del users[username]
    save_users(users)
    return jsonify({'ok': True, 'msg': f'ลบร้าน {shop_name} แล้ว'})

# ── legacy admin routes (backward compat) ──────────────────

@app.route('/admin/approve/<username>')
def approve(username):
    if request.args.get('key') != ADMIN_KEY:
        return 'ไม่มีสิทธิ์', 403
    users = load_users()
    if username not in users:
        return f'ไม่พบ {username}', 404
    users[username]['status'] = 'active'
    save_users(users)
    u = users[username]
    return f"✅ อนุมัติ {username} ({u['shop_name']}) {u['plan'].upper()} {u['price']}฿ สำเร็จ"

# ── health ─────────────────────────────────────────────────

@app.route('/api/health')
def health():
    users = load_users()
    return jsonify({
        'status':        'ok',
        'version':       'v5',
        'model':         GEMINI_MODEL,
        'starter_limit': STARTER_DAILY_LIMIT,
        'time':          datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'total':         len(users),
        'active':        sum(1 for u in users.values() if u['status'] == 'active'),
        'pro':           sum(1 for u in users.values() if u['plan'] == 'pro' and u['status'] == 'active'),
        'starter':       sum(1 for u in users.values() if u['plan'] == 'starter' and u['status'] == 'active'),
        'ai_key':        bool(GEMINI_KEY),
    })

if __name__ == '__main__':
    print(f"🌿 ค้าสด v5 | {GEMINI_MODEL} | starter limit: {STARTER_DAILY_LIMIT}/day")
    app.run(debug=True, host='0.0.0.0', port=5000)
