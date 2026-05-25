"""
app.py — ค้าสด (KAASOD) SaaS v3
แพ็กเกจ: Starter 199฿ (สรุปรายอาทิตย์) | Pro 399฿ (AI ประจำร้าน)
"""

from flask import Flask, request, jsonify, session, redirect, Response
from pathlib import Path
import json, os, hashlib, secrets
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

def load_users():
    if USERS_FILE.exists():
        return json.loads(USERS_FILE.read_text(encoding='utf-8'))
    return {}

def save_users(u):
    USERS_FILE.write_text(json.dumps(u, ensure_ascii=False, indent=2), encoding='utf-8')

def hash_pw(pw):
    import hashlib
    return hashlib.sha256(pw.encode()).hexdigest()

def shop_dir(shop_id):
    d = SHOPS_DIR / shop_id
    d.mkdir(exist_ok=True)
    return d

# ════ PAGES ════════════════════════════════════════════════

@app.route('/')
def index():
    f = Path('landing.html')
    if f.exists():
        return f.read_text(encoding='utf-8')
    return "<h1>ไม่พบ landing.html</h1>", 404

@app.route('/pos')
def pos():
    if 'shop_id' not in session:
        return redirect('/')
    f = Path('index.html')
    if f.exists():
        return f.read_text(encoding='utf-8')
    return "<h1>ไม่พบ index.html</h1>", 404

@app.route('/sw.js')
def sw():
    f = Path('sw.js')
    if f.exists():
        return Response(f.read_text(), mimetype='application/javascript')
    return '', 404

# ════ AUTH ═════════════════════════════════════════════════

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
        'shop_id':    shop_id,
        'shop_name':  shop_name,
        'phone':      phone,
        'password':   hash_pw(password),
        'plan':       plan,
        'price':      price,
        'status':     'pending',
        'created_at': datetime.now().strftime('%Y-%m-%d %H:%M'),
    }
    save_users(users)
    shop_dir(shop_id)
    return jsonify({'ok': True, 'plan': plan, 'price': price})


@app.route('/api/login', methods=['POST'])
def login():
    d        = request.get_json(silent=True) or {}
    username = d.get('username', '').strip().lower()
    password = d.get('password', '').strip()

    users = load_users()
    user  = users.get(username)

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
        'ok':        True,
        'shop_id':   session['shop_id'],
        'shop_name': session['shop_name'],
        'username':  session['username'],
        'plan':      session.get('plan', 'starter'),
    })

# ════ AI CHAT — Pro Only ════════════════════════════════════

@app.route('/api/chat', methods=['POST'])
def chat():
    if 'shop_id' not in session:
        return jsonify({'error': 'ไม่ได้ login'}), 401
    if session.get('plan') != 'pro':
        return jsonify({
            'error': 'plan_required',
            'msg':   '🤖 AI ประจำร้านสำหรับแพ็กเกจ Pro 399฿/เดือน เท่านั้นครับ\nอัปเกรดได้เลยที่ Line: @kaasod'
        }), 403

    data     = request.get_json(silent=True) or {}

    # รองรับทั้ง 2 รูปแบบ: contents ตรงๆ (จาก index.html) หรือ messages+system
    contents = data.get('contents', [])
    if not contents:
        messages = data.get('messages', [])
        system   = data.get('system', '')
        if system:
            contents.append({'role': 'user',  'parts': [{'text': '[SYSTEM]\n' + system}]})
            contents.append({'role': 'model', 'parts': [{'text': 'รับทราบครับ พร้อมให้บริการ'}]})
        contents.extend(messages)

    if not contents:
        return jsonify({'error': 'ไม่มีข้อความ'}), 400

    if not GEMINI_KEY:
        return jsonify({'error': 'ยังไม่ได้ตั้งค่า GEMINI_API_KEY'}), 500

    import urllib.request, urllib.error
    url = f'https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite-preview:generateContent?key={GEMINI_KEY}'

    body = json.dumps({'contents': contents}).encode()
    req  = urllib.request.Request(url, data=body, headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.loads(r.read())
        text = resp['candidates'][0]['content']['parts'][0]['text']
        return jsonify({'ok': True, 'text': text})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/summary/weekly', methods=['POST'])
def weekly_summary():
    if 'shop_id' not in session:
        return jsonify({'error': 'ไม่ได้ login'}), 401

    data  = request.get_json(silent=True) or {}
    sales = data.get('sales', [])
    if not sales:
        return jsonify({'ok': True, 'summary': 'ยังไม่มีข้อมูลยอดขายสัปดาห์นี้ครับ'})

    total     = sum(s.get('total', 0) for s in sales)
    count     = len(sales)
    avg       = total / count if count else 0
    top_items = {}
    for s in sales:
        for item in s.get('items', []):
            name = item.get('name', '')
            top_items[name] = top_items.get(name, 0) + item.get('qty', 1)

    top      = sorted(top_items.items(), key=lambda x: x[1], reverse=True)[:3]
    top_text = ', '.join([f"{n} ({q} ชิ้น)" for n, q in top]) or 'ไม่มีข้อมูล'
    summary  = (
        f"📊 สรุปยอดขายสัปดาห์นี้\n"
        f"รายได้รวม: {total:,.0f} บาท\n"
        f"จำนวนบิล: {count} บิล\n"
        f"เฉลี่ย/บิล: {avg:,.0f} บาท\n"
        f"สินค้าขายดี: {top_text}"
    )
    return jsonify({'ok': True, 'summary': summary})

# ════ SYNC ═════════════════════════════════════════════════

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
    return jsonify({'status': 'ok'})


@app.route('/api/sync', methods=['GET'])
def get_sync():
    if 'shop_id' not in session:
        return jsonify({'status': 'empty', 'data': {}})
    d     = shop_dir(session['shop_id'])
    files = sorted(d.glob('backup_*.json'), reverse=True)
    if not files:
        return jsonify({'status': 'empty', 'data': {}})
    data = json.loads(files[0].read_text(encoding='utf-8'))
    return jsonify({'status': 'ok', **data})

# ════ ADMIN ════════════════════════════════════════════════

@app.route('/admin/approve/<username>')
def approve(username):
    if request.args.get('key') != ADMIN_KEY:
        return "ไม่มีสิทธิ์", 403
    users = load_users()
    if username not in users:
        return f"ไม่พบ {username}", 404
    users[username]['status'] = 'active'
    save_users(users)
    u = users[username]
    return f"✅ อนุมัติ {username} ({u['shop_name']}) แพ็กเกจ {u['plan'].upper()} {u['price']}฿ สำเร็จ"


@app.route('/admin/list')
def admin_list():
    if request.args.get('key') != ADMIN_KEY:
        return "ไม่มีสิทธิ์", 403
    users   = load_users()
    active  = sum(1 for d in users.values() if d['status'] == 'active')
    pending = sum(1 for d in users.values() if d['status'] == 'pending')
    revenue = sum(d['price'] for d in users.values() if d['status'] == 'active')
    rows    = []
    for u, d in users.items():
        plan_badge   = '🤖 Pro 399฿' if d['plan'] == 'pro' else '📋 Starter 199฿'
        status_color = '#5aaa6a' if d['status'] == 'active' else '#e07b3a'
        rows.append(f"""<tr>
          <td>{u}</td><td>{d['shop_name']}</td><td>{d['phone']}</td>
          <td>{plan_badge}</td>
          <td style="color:{status_color};font-weight:bold">{d['status']}</td>
          <td>{d['created_at']}</td>
          <td><a href="/admin/approve/{u}?key={request.args.get('key')}">✅ อนุมัติ</a></td>
        </tr>""")

    return f"""<html><head><meta charset="utf-8">
    <style>body{{font-family:sans-serif;padding:20px;background:#0f0e0c;color:#f0e8dc}}
    table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #2e2820;padding:10px}}
    th{{background:#1a1815;color:#c9a84c}}.stat{{display:inline-block;background:#1a1815;
    border:1px solid #2e2820;border-radius:10px;padding:12px 20px;margin:8px;text-align:center}}
    .stat b{{display:block;font-size:24px;color:#c9a84c}}a{{color:#e07b3a}}</style></head>
    <body><h2 style="color:#c9a84c">ค้าสด — Admin Panel</h2>
    <div><div class="stat"><b>{len(users)}</b>ร้านทั้งหมด</div>
    <div class="stat"><b style="color:#5aaa6a">{active}</b>Active</div>
    <div class="stat"><b style="color:#e07b3a">{pending}</b>รอยืนยัน</div>
    <div class="stat"><b>{revenue:,}฿</b>รายได้/เดือน</div></div><br>
    <table><tr><th>Username</th><th>ชื่อร้าน</th><th>เบอร์</th><th>แพ็กเกจ</th>
    <th>สถานะ</th><th>สมัครเมื่อ</th><th>Action</th></tr>
    {''.join(rows)}</table></body></html>"""


@app.route('/api/health')
def health():
    users = load_users()
    return jsonify({
        'status':  'ok',
        'time':    datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'total':   len(users),
        'active':  sum(1 for u in users.values() if u['status'] == 'active'),
        'pro':     sum(1 for u in users.values() if u['plan'] == 'pro' and u['status'] == 'active'),
        'starter': sum(1 for u in users.values() if u['plan'] == 'starter' and u['status'] == 'active'),
    })


if __name__ == '__main__':
    print("🌿 ค้าสด SaaS v3 — http://localhost:5000")
    app.run(debug=True, host='0.0.0.0', port=5000)
