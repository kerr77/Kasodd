
"""
app_v3.py — ค้าสด (KAASOD) SaaS v3
- landing.html = หน้าแรก สมัคร/login
- /pos = หน้า POS (ต้อง login ก่อน)
- AI chat ส่ง Gemini format ตรงๆ กลับให้ index.html
- แยก plan: starter=199, pro=399 (AI ใช้ได้เฉพาะ pro)
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

ADMIN_KEY  = os.environ.get('ADMIN_KEY', 'kaasod-admin-2026')
GEMINI_KEY = os.environ.get('GEMINI_API_KEY', '')
GEMINI_MODEL = 'gemini-3.1-flash-lite'

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

# ── pages ──────────────────────────────────────────────────

@app.route('/')
def index():
    f = Path('landing.html')
    return f.read_text(encoding='utf-8') if f.exists() else ("<h1>ไม่พบ landing.html</h1>", 404)

@app.route('/pos')
def pos():
    if 'shop_id' not in session:
        return redirect('/')
    f = Path('index.html')
    return f.read_text(encoding='utf-8') if f.exists() else ("<h1>ไม่พบ index.html</h1>", 404)

@app.route('/sw.js')
def sw():
    f = Path('sw.js')
    return Response(f.read_text(), mimetype='application/javascript') if f.exists() else ('', 404)

# ── auth ───────────────────────────────────────────────────

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
    return jsonify({
        'ok': True, 'shop_id': session['shop_id'],
        'shop_name': session['shop_name'], 'username': session['username'],
        'plan': session.get('plan', 'starter'),
    })

# ── AI chat ────────────────────────────────────────────────

@app.route('/api/chat', methods=['POST'])
def chat():
    if 'shop_id' not in session:
        return jsonify({'error': 'ไม่ได้ login'}), 401

    # ตรวจ plan
    if session.get('plan') != 'pro':
        return jsonify({
            'error': {'code': 403, 'message': 'AI ประจำร้านสำหรับแพ็กเกจ Pro 399฿/เดือนเท่านั้นครับ\nอัปเกรดได้ที่ Line: @kaasod', 'status': 'PLAN_REQUIRED'}
        }), 403

    if not GEMINI_KEY:
        return jsonify({'error': {'code': 500, 'message': 'ยังไม่ได้ตั้งค่า GEMINI_API_KEY', 'status': 'NO_KEY'}}), 500

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
        # ส่ง Gemini format ตรงๆ กลับไป — index.html อ่านได้เลย
        return jsonify(resp)
    except urllib.error.HTTPError as e:
        err = json.loads(e.read().decode())
        return jsonify(err), e.code
    except Exception as e:
        return jsonify({'error': {'code': 500, 'message': str(e), 'status': 'SERVER_ERROR'}}), 500

@app.route('/api/chat/status')
def chat_status():
    return jsonify({'ai_ready': bool(GEMINI_KEY) and session.get('plan') == 'pro'})

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

# ── admin ──────────────────────────────────────────────────

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

@app.route('/admin/list')
def admin_list():
    if request.args.get('key') != ADMIN_KEY:
        return 'ไม่มีสิทธิ์', 403
    users   = load_users()
    active  = sum(1 for u in users.values() if u['status'] == 'active')
    pending = sum(1 for u in users.values() if u['status'] == 'pending')
    revenue = sum(u['price'] for u in users.values() if u['status'] == 'active')
    rows = ''
    for u, d in users.items():
        plan_label   = '🤖 Pro 399฿' if d['plan'] == 'pro' else '📋 Starter 199฿'
        status_color = '#5aaa6a' if d['status'] == 'active' else '#e07b3a'
        rows += f"""<tr>
          <td>{u}</td><td>{d['shop_name']}</td><td>{d['phone']}</td>
          <td>{plan_label}</td>
          <td style="color:{status_color};font-weight:bold">{d['status']}</td>
          <td>{d['created_at']}</td>
          <td><a href="/admin/approve/{u}?key={request.args.get('key')}">✅ อนุมัติ</a></td>
        </tr>"""
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
    <style>body{{font-family:sans-serif;padding:20px;background:#0f0e0c;color:#f0e8dc}}
    table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #2e2820;padding:10px}}
    th{{background:#1a1815;color:#c9a84c}}.s{{display:inline-block;background:#1a1815;
    border:1px solid #2e2820;border-radius:10px;padding:12px 20px;margin:8px;text-align:center}}
    .s b{{display:block;font-size:24px;color:#c9a84c}}a{{color:#e07b3a}}</style></head>
    <body><h2 style="color:#c9a84c">ค้าสด — Admin</h2>
    <div>
      <div class="s"><b>{len(users)}</b>ร้านทั้งหมด</div>
      <div class="s"><b style="color:#5aaa6a">{active}</b>Active</div>
      <div class="s"><b style="color:#e07b3a">{pending}</b>รอยืนยัน</div>
      <div class="s"><b>{revenue:,}฿</b>รายได้/เดือน</div>
    </div><br>
    <table><tr><th>Username</th><th>ชื่อร้าน</th><th>เบอร์</th>
    <th>แพ็กเกจ</th><th>สถานะ</th><th>สมัครเมื่อ</th><th>Action</th></tr>
    {rows}</table></body></html>"""

# ── health ─────────────────────────────────────────────────

@app.route('/api/health')
def health():
    users = load_users()
    return jsonify({
        'status':  'ok',
        'model':   GEMINI_MODEL,
        'time':    datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'total':   len(users),
        'active':  sum(1 for u in users.values() if u['status'] == 'active'),
        'pro':     sum(1 for u in users.values() if u['plan'] == 'pro' and u['status'] == 'active'),
        'starter': sum(1 for u in users.values() if u['plan'] == 'starter' and u['status'] == 'active'),
        'ai_key':  bool(GEMINI_KEY),
    })

if __name__ == '__main__':
    print(f"🌿 ค้าสด v3 | model: {GEMINI_MODEL} | http://localhost:5000")
    app.run(debug=True, host='0.0.0.0', port=5000)
