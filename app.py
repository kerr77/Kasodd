"""
app.py — ค้าสด (KAASOD) SaaS v3
แพ็กเกจ: Starter 199฿ (สรุปรายอาทิตย์) | Pro 399฿ (AI ประจำร้าน)
"""

from flask import Flask, request, jsonify, session, redirect
from pathlib import Path
import json, os, hashlib, secrets, csv, io
from datetime import datetime

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'kaasod-secret-2026')

# ─── Directories ──────────────────────────────────────────
DATA_DIR   = Path('pos_data')
SHOPS_DIR  = DATA_DIR / 'shops'
USERS_FILE = DATA_DIR / 'users.json'

for d in [DATA_DIR, SHOPS_DIR]:
    d.mkdir(parents=True, exist_ok=True)

ADMIN_KEY = os.environ.get('ADMIN_KEY', 'kaasod-admin-2026')
GEMINI_KEY = os.environ.get('GEMINI_API_KEY', '')

# ─── Helpers ──────────────────────────────────────────────
def load_users():
    if USERS_FILE.exists():
        return json.loads(USERS_FILE.read_text(encoding='utf-8'))
    return {}

def save_users(u):
    USERS_FILE.write_text(json.dumps(u, ensure_ascii=False, indent=2), encoding='utf-8')

def hash_pw(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def shop_dir(shop_id):
    d = SHOPS_DIR / shop_id
    d.mkdir(exist_ok=True)
    return d

def current_user():
    if 'username' not in session:
        return None
    return load_users().get(session['username'])

# ════════════════════════════════════════════════════════
#  PAGES
# ════════════════════════════════════════════════════════

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
        from flask import Response
        return Response(f.read_text(), mimetype='application/javascript')
    return '', 404

# ════════════════════════════════════════════════════════
#  AUTH
# ════════════════════════════════════════════════════════

@app.route('/api/register', methods=['POST'])
def register():
    d         = request.get_json(silent=True) or {}
    shop_name = d.get('shop_name', '').strip()
    username  = d.get('username', '').strip().lower()
    password  = d.get('password', '').strip()
    phone     = d.get('phone', '').strip()
    plan      = d.get('plan', 'starter')  # starter | pro

    if plan not in ('starter', 'pro'):
        plan = 'starter'

    if not all([shop_name, username, password, phone]):
        return jsonify({'ok': False, 'msg': 'กรอกข้อมูลให้ครบครับ'}), 400

    users = load_users()
    if username in users:
        return jsonify({'ok': False, 'msg': 'ชื่อผู้ใช้นี้มีแล้วครับ'}), 400

    shop_id = secrets.token_hex(6)
    price   = 199 if plan == 'starter' else 399

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

# ════════════════════════════════════════════════════════
#  AI CHAT — Pro เท่านั้น
# ════════════════════════════════════════════════════════

@app.route('/api/chat', methods=['POST'])
def chat():
    if 'shop_id' not in session:
        return jsonify({'error': 'ไม่ได้ login'}), 401

    # ตรวจ plan
    if session.get('plan') != 'pro':
        return jsonify({
            'error': 'plan_required',
            'msg':   'AI ประจำร้านสำหรับแพ็กเกจ Pro 399฿/เดือน เท่านั้นครับ 🤖'
        }), 403

    data     = request.get_json(silent=True) or {}
    messages = data.get('messages', [])
    system   = data.get('system', '')

    if not GEMINI_KEY:
        return jsonify({'error': 'ยังไม่ได้ตั้งค่า GEMINI_API_KEY'}), 500

    import urllib.request, urllib.error
    url     = f'https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_KEY}'
    contents = []
    if system:
        contents.append({'role': 'user',  'parts': [{'text': '[SYSTEM]\n' + system}]})
        contents.append({'role': 'model', 'parts': [{'text': 'รับทราบครับ พร้อมให้บริการ'}]})
    contents.extend(messages)

    body = json.dumps({'contents': contents}).encode()
    req  = urllib.request.Request(url, data=body, headers={'Content-Type': 'application/json'})

    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.loads(r.read())
        text = resp['candidates'][0]['content']['parts'][0]['text']
        return jsonify({'ok': True, 'text': text})
    except urllib.error.HTTPError as e:
        return jsonify({'error': e.read().decode()}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# Weekly summary — Starter ขึ้นไปใช้ได้
@app.route('/api/summary/weekly', methods=['POST'])
def weekly_summary():
    if 'shop_id' not in session:
        return jsonify({'error': 'ไม่ได้ login'}), 401

    data = request.get_json(silent=True) or {}
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

    top = sorted(top_items.items(), key=lambda x: x[1], reverse=True)[:3]
    top_text = ', '.join([f"{n} ({q} ชิ้น)" for n, q in top]) or 'ไม่มีข้อมูล'

    summary = (
        f"📊 สรุปยอดขายสัปดาห์นี้\n"
        f"รายได้รวม: {total:,.0f} บาท\n"
        f"จำนวนบิล: {count} บิล\n"
        f"เฉลี่ย/บิล: {avg:,.0f} บาท\n"
        f"สินค้าขายดี: {top_text}"
    )
    return jsonify({'ok': True, 'summary': summary})

# ════════════════════════════════════════════════════════
#  SYNC / BACKUP
# ════════════════════════════════════════════════════════

@app.route('/api/sync', methods=['POST'])
def sync_data():
    if 'shop_id' not in session:
        return jsonify({'error': 'ไม่ได้ login'}), 401

    payload  = request.get_json(silent=True) or {}
    date_str = datetime.now().strftime('%Y-%m-%d')
    ts       = datetime.now().strftime('%Y%m%d_%H%M%S')
    d        = shop_dir(session['shop_id'])

    f = d / f'backup_{date_str}.json'
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

# ════════════════════════════════════════════════════════
#  ADMIN
# ════════════════════════════════════════════════════════

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

    users = load_users()
    rows  = []
    for u, d in users.items():
        plan_badge = '🤖 Pro' if d['plan'] == 'pro' else '📋 Starter'
        status_color = '#5aaa6a' if d['status'] == 'active' else '#e07b3a'
        rows.append(f"""
        <tr>
          <td>{u}</td>
          <td>{d['shop_name']}</td>
          <td>{d['phone']}</td>
          <td>{plan_badge} — {d['price']}฿</td>
          <td style="color:{status_color};font-weight:bold">{d['status']}</td>
          <td>{d['created_at']}</td>
          <td><a href="/admin/approve/{u}?key={request.args.get('key')}">✅ อนุมัติ</a></td>
        </tr>""")

    active  = sum(1 for d in users.values() if d['status'] == 'active')
    pending = sum(1 for d in users.values() if d['status'] == 'pending')
    revenue = sum(d['price'] for d in users.values() if d['status'] == 'active')

    return f"""
    <html><head><meta charset="utf-8">
    <style>
      body{{font-family:sans-serif;padding:20px;background:#0f0e0c;color:#f0e8dc}}
      table{{border-collapse:collapse;width:100%}}
      td,th{{border:1px solid #2e2820;padding:10px;text-align:left}}
      th{{background:#1a1815;color:#c9a84c}}
      tr:hover{{background:#1a1815}}
      .stat{{display:inline-block;background:#1a1815;border:1px solid #2e2820;
             border-radius:10px;padding:12px 20px;margin:8px;text-align:center}}
      .stat b{{display:block;font-size:24px;color:#c9a84c}}
      a{{color:#e07b3a}}
    </style></head>
    <body>
    <h2 style="color:#c9a84c">ค้าสด — Admin Panel</h2>
    <div>
      <div class="stat"><b>{len(users)}</b>ร้านทั้งหมด</div>
      <div class="stat"><b style="color:#5aaa6a">{active}</b>Active</div>
      <div class="stat"><b style="color:#e07b3a">{pending}</b>รอยืนยัน</div>
      <div class="stat"><b>{revenue:,}฿</b>รายได้/เดือน</div>
    </div>
    <br>
    <table>
      <tr><th>Username</th><th>ชื่อร้าน</th><th>เบอร์</th><th>แพ็กเกจ</th><th>สถานะ</th><th>สมัครเมื่อ</th><th>Action</th></tr>
      {''.join(rows)}
    </table>
    </body></html>
    """

# ════════════════════════════════════════════════════════
#  HEALTH
# ════════════════════════════════════════════════════════

@app.route('/api/health')
def health():
    users = load_users()
    return jsonify({
        'status':      'ok',
        'time':        datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'total_shops': len(users),
        'active':      sum(1 for u in users.values() if u['status'] == 'active'),
        'pro':         sum(1 for u in users.values() if u['plan'] == 'pro' and u['status'] == 'active'),
        'starter':     sum(1 for u in users.values() if u['plan'] == 'starter' and u['status'] == 'active'),
    })


if __name__ == '__main__':
    print("🌿 ค้าสด SaaS v3 — http://localhost:5000")
    app.run(debug=True, host='0.0.0.0', port=5000)
_backups():
    snaps  = sorted(BACKUP_DIR.glob('snap_*.json'), reverse=True)
    result = []
    for f in snaps[:20]:
        try:
            data = json.loads(f.read_text(encoding='utf-8'))
            history = data.get('data', {}).get('history', [])
            result.append({
                'file':      f.name,
                'synced_at': data.get('synced_at', ''),
                'bills':     len(history),
                'size_kb':   round(f.stat().st_size / 1024, 1),
            })
        except Exception:
            result.append({'file': f.name, 'error': 'อ่านไม่ได้'})
    return jsonify({'status': 'ok', 'backups': result})


@app.route('/api/backups/<filename>', methods=['GET'])
def get_backup(filename):
    filepath = BACKUP_DIR / filename
    if not filepath.exists() or not filename.endswith('.json'):
        return jsonify({'error': 'ไม่พบไฟล์'}), 404
    data = json.loads(filepath.read_text(encoding='utf-8'))
    return jsonify({'status': 'ok', **data})


# ════════════════════════════════════════════════════════════
#  API — STOCK ALERT
# ════════════════════════════════════════════════════════════

@app.route('/api/stock/alert', methods=['POST'])
def log_stock_alert():
    payload   = request.get_json(silent=True) or {}
    low_items = payload.get('low_items', [])
    threshold = payload.get('threshold', 3)
    ts        = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    alert_log = DATA_DIR / 'stock_alerts.jsonl'
    with open(alert_log, 'a', encoding='utf-8') as f:
        f.write(json.dumps({
            'time': ts, 'threshold': threshold, 'items': low_items
        }, ensure_ascii=False) + '\n')

    return jsonify({'status': 'ok', 'logged': len(low_items)})


# ════════════════════════════════════════════════════════════
#  API — HEALTH CHECK
# ════════════════════════════════════════════════════════════

@app.route('/api/health', methods=['GET'])
def health():
    snaps = list(BACKUP_DIR.glob('snap_*.json'))
    return jsonify({
        'status':       'ok',
        'version':      'v13',
        'server_time':  datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'backup_count': len(snaps),
        'ai_ready':     bool(_get_gemini_key()),
        'data_dir':     str(DATA_DIR.resolve()),
    })


# ════════════════════════════════════════════════════════════
#  RUN
# ════════════════════════════════════════════════════════════

if __name__ == '__main__':
    print("=" * 55)
    print("  🌿 KAASOD POS Server v13 — พร้อมให้บริการ")
    print("  http://localhost:5000")
    print(f"  AI Ready: {'✅ YES' if _get_gemini_key() else '❌ ยังไม่ได้ตั้งค่า key'}")
    print("=" * 55)
    app.run(debug=True, host='0.0.0.0', port=5000)
