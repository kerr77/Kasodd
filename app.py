"""
app.py — KAASOD SaaS Backend v2
รองรับ: Multi-tenant login, สมัครสมาชิก, POS per shop
"""

from flask import Flask, request, jsonify, send_file, redirect, url_for, session
import json, os, csv, io, hashlib, secrets
from datetime import datetime
from pathlib import Path

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'kaasod-secret-2026-change-this')

# ─── โฟลเดอร์ ─────────────────────────────────────────────────
DATA_DIR   = Path('pos_data')
SHOPS_DIR  = DATA_DIR / 'shops'
USERS_FILE = DATA_DIR / 'users.json'

for d in [DATA_DIR, SHOPS_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# ─── Helper: โหลด/บันทึก users ────────────────────────────────
def load_users():
    if USERS_FILE.exists():
        return json.loads(USERS_FILE.read_text(encoding='utf-8'))
    return {}

def save_users(users):
    USERS_FILE.write_text(json.dumps(users, ensure_ascii=False, indent=2), encoding='utf-8')

def hash_password(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def shop_dir(shop_id):
    d = SHOPS_DIR / shop_id
    d.mkdir(exist_ok=True)
    return d

# ════════════════════════════════════════════════════════════
#  PAGES — Serve HTML files
# ════════════════════════════════════════════════════════════

@app.route('/')
def index():
    """หน้า Landing / สมัครสมาชิก"""
    f = Path('landing.html')
    if f.exists():
        return f.read_text(encoding='utf-8')
    return "<h1>ไม่พบ landing.html</h1>", 404

@app.route('/pos')
def pos():
    """หน้า POS — ต้อง login ก่อน"""
    if 'shop_id' not in session:
        return redirect('/')
    f = Path('index.html')
    if f.exists():
        return f.read_text(encoding='utf-8')
    return "<h1>ไม่พบ index.html</h1>", 404

# ════════════════════════════════════════════════════════════
#  AUTH API
# ════════════════════════════════════════════════════════════

@app.route('/api/register', methods=['POST'])
def register():
    """สมัครสมาชิกใหม่"""
    data      = request.get_json(silent=True) or {}
    shop_name = data.get('shop_name', '').strip()
    username  = data.get('username', '').strip().lower()
    password  = data.get('password', '').strip()
    phone     = data.get('phone', '').strip()

    if not all([shop_name, username, password, phone]):
        return jsonify({'ok': False, 'msg': 'กรอกข้อมูลให้ครบครับ'}), 400

    users = load_users()
    if username in users:
        return jsonify({'ok': False, 'msg': 'ชื่อผู้ใช้นี้มีแล้วครับ'}), 400

    shop_id = secrets.token_hex(6)
    users[username] = {
        'shop_id':   shop_id,
        'shop_name': shop_name,
        'phone':     phone,
        'password':  hash_password(password),
        'plan':      'starter',
        'status':    'pending',   # pending = รอยืนยันการโอน
        'created_at': datetime.now().strftime('%Y-%m-%d %H:%M'),
    }
    save_users(users)
    shop_dir(shop_id)  # สร้างโฟลเดอร์ร้าน

    return jsonify({'ok': True, 'msg': 'สมัครสำเร็จ รอยืนยันการโอนเงินครับ', 'shop_id': shop_id})


@app.route('/api/login', methods=['POST'])
def login():
    """เข้าสู่ระบบ"""
    data     = request.get_json(silent=True) or {}
    username = data.get('username', '').strip().lower()
    password = data.get('password', '').strip()

    users = load_users()
    user  = users.get(username)

    if not user or user['password'] != hash_password(password):
        return jsonify({'ok': False, 'msg': 'ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง'}), 401

    if user['status'] == 'pending':
        return jsonify({'ok': False, 'msg': 'รอยืนยันการโอนเงินก่อนนะครับ ติดต่อ Line: @kaasod'}), 403

    session['shop_id']   = user['shop_id']
    session['shop_name'] = user['shop_name']
    session['username']  = username

    return jsonify({'ok': True, 'shop_name': user['shop_name']})


@app.route('/api/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'ok': True})


@app.route('/api/me', methods=['GET'])
def me():
    """ดึงข้อมูลร้านปัจจุบัน"""
    if 'shop_id' not in session:
        return jsonify({'ok': False}), 401
    return jsonify({
        'ok':        True,
        'shop_id':   session['shop_id'],
        'shop_name': session['shop_name'],
        'username':  session['username'],
    })

# ════════════════════════════════════════════════════════════
#  SYNC / BACKUP (per shop)
# ════════════════════════════════════════════════════════════

@app.route('/api/sync', methods=['POST'])
def sync_data():
    if 'shop_id' not in session:
        return jsonify({'error': 'ไม่ได้ login'}), 401

    payload  = request.get_json(silent=True) or {}
    ts       = datetime.now().strftime('%Y%m%d_%H%M%S')
    date_str = datetime.now().strftime('%Y-%m-%d')
    d        = shop_dir(session['shop_id'])

    daily = d / f'backup_{date_str}.json'
    daily.write_text(json.dumps({'synced_at': ts, 'data': payload}, ensure_ascii=False, indent=2), encoding='utf-8')

    return jsonify({'status': 'ok', 'backup': str(daily)})


@app.route('/api/sync', methods=['GET'])
def get_latest_backup():
    if 'shop_id' not in session:
        return jsonify({'status': 'empty', 'data': {}})

    d     = shop_dir(session['shop_id'])
    files = sorted(d.glob('backup_*.json'), reverse=True)
    if not files:
        return jsonify({'status': 'empty', 'data': {}})

    data = json.loads(files[0].read_text(encoding='utf-8'))
    return jsonify({'status': 'ok', **data})

# ════════════════════════════════════════════════════════════
#  ADMIN — ยืนยันการโอนเงิน (ใช้ URL ลับ)
# ════════════════════════════════════════════════════════════

ADMIN_KEY = os.environ.get('ADMIN_KEY', 'kaasod-admin-2026')

@app.route('/admin/approve/<username>', methods=['GET'])
def approve_user(username):
    key = request.args.get('key', '')
    if key != ADMIN_KEY:
        return "ไม่มีสิทธิ์", 403

    users = load_users()
    if username not in users:
        return f"ไม่พบ user: {username}", 404

    users[username]['status'] = 'active'
    save_users(users)
    return f"✅ อนุมัติ {username} ({users[username]['shop_name']}) สำเร็จ"


@app.route('/admin/list', methods=['GET'])
def list_users():
    key = request.args.get('key', '')
    if key != ADMIN_KEY:
        return "ไม่มีสิทธิ์", 403

    users = load_users()
    rows  = []
    for u, d in users.items():
        rows.append(f"""
        <tr>
          <td>{u}</td>
          <td>{d['shop_name']}</td>
          <td>{d['phone']}</td>
          <td>{d['status']}</td>
          <td>{d['created_at']}</td>
          <td><a href="/admin/approve/{u}?key={key}">✅ อนุมัติ</a></td>
        </tr>""")

    return f"""
    <html><head><meta charset="utf-8">
    <style>table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #ccc;padding:8px}}
    body{{font-family:sans-serif;padding:20px}}</style></head>
    <body>
    <h2>KAASOD — รายชื่อร้านค้า ({len(users)} ร้าน)</h2>
    <table><tr><th>Username</th><th>ชื่อร้าน</th><th>เบอร์</th><th>สถานะ</th><th>สมัครเมื่อ</th><th>Action</th></tr>
    {''.join(rows)}
    </table></body></html>
    """

# ════════════════════════════════════════════════════════════
#  HEALTH
# ════════════════════════════════════════════════════════════

@app.route('/')
def index():
    """Serve หน้า POS หลัก"""
    if INDEX_HTML.exists():
        html_content = INDEX_HTML.read_text(encoding='utf-8')
        
        # 1. ดึง API Key จาก Railway Environment Variable
        gemini_key = os.environ.get('GEMINI_API_KEY', '')
        
        # 2. ค้นหาบรรทัดรับค่าคีย์เดิม แล้วแทนที่ด้วยคีย์จาก Railway ทันที
        old_code = "let chatApiKey = localStorage.getItem('pos_chat_apikey') || '';"
        new_code = f"let chatApiKey = '{gemini_key}';"
        html_content = html_content.replace(old_code, new_code)
        
        return html_content
    return "<h1>❌ ไม่พบ index.html — วางไฟล์ไว้ที่รูทโปรเจกต์</h1>", 404

# ════════════════════════════════════════════════════════════
#  RUN
# ════════════════════════════════════════════════════════════

if __name__ == '__main__':
    print("=" * 55)
    print("  🌿 KAASOD SaaS v2 — พร้อมให้บริการ")
    print("  http://localhost:5000")
    print("=" * 55)
    app.run(debug=True, host='0.0.0.0', port=5000)
