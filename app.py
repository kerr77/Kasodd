"""
app_v5.py — ค้าสด (KAASOD) SaaS v5
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
GEMINI_MODEL = 'gemini-3.1-flash-lite-preview'
STARTER_DAILY_LIMIT = 20

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

def get_today_usage(shop_id):
    f     = shop_dir(shop_id) / 'ai_usage.json'
    today = datetime.now().strftime('%Y-%m-%d')
    if not f.exists(): return 0
    return json.loads(f.read_text(encoding='utf-8')).get(today, 0)

def increment_usage(shop_id):
    f     = shop_dir(shop_id) / 'ai_usage.json'
    today = datetime.now().strftime('%Y-%m-%d')
    data  = json.loads(f.read_text(encoding='utf-8')) if f.exists() else {}
    data[today] = data.get(today, 0) + 1
    keys = sorted(data.keys(), reverse=True)[:7]
    data = {k: data[k] for k in keys}
    f.write_text(json.dumps(data), encoding='utf-8')
    return data[today]

def is_admin():
    return session.get('is_admin') is True

def is_admin_request():
    """ยอมรับทั้ง session และ X-Admin-Key header หรือ ?key= query param"""
    if session.get('is_admin') is True:
        return True
    key = request.headers.get('X-Admin-Key', '') or request.args.get('key', '')
    return key == ADMIN_KEY

def log_chat(shop_id, username, contents, resp):
    """บันทึก chat log ไว้ใน shop dir สำหรับ admin ดู"""
    f = shop_dir(shop_id) / 'chat_log.json'
    logs = json.loads(f.read_text(encoding='utf-8')) if f.exists() else []
    ai_text = ''
    try:
        ai_text = resp['candidates'][0]['content']['parts'][0]['text'][:500]
    except Exception:
        pass
    user_msg = ''
    try:
        user_msg = contents[-1]['parts'][0]['text'][:300] if contents else ''
    except Exception:
        pass
    logs.append({
        'ts':       datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'user_msg': user_msg,
        'ai_reply': ai_text,
        'username': username,
    })
    logs = logs[-50:]  # เก็บ 50 รายการล่าสุด
    f.write_text(json.dumps(logs, ensure_ascii=False), encoding='utf-8')

def esc(t):
    return str(t).replace('&','&amp;').replace('<','&lt;').replace('>','&gt;').replace('"','&quot;').replace("'","&#39;")

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

# ── auth ───────────────────────────────────────────────────

@app.route('/api/register', methods=['POST'])
def register():
    d         = request.get_json(silent=True) or {}
    shop_name = d.get('shop_name','').strip()
    username  = d.get('username','').strip().lower()
    password  = d.get('password','').strip()
    phone     = d.get('phone','').strip()
    plan      = d.get('plan','starter')
    if plan not in ('starter','pro'): plan = 'starter'
    if not all([shop_name, username, password, phone]):
        return jsonify({'ok':False,'msg':'กรอกข้อมูลให้ครบครับ'}), 400
    users = load_users()
    if username in users:
        return jsonify({'ok':False,'msg':'ชื่อผู้ใช้นี้มีแล้วครับ'}), 400
    price   = 199 if plan == 'starter' else 399
    shop_id = secrets.token_hex(6)
    users[username] = {
        'shop_id':shop_id,'shop_name':shop_name,'phone':phone,
        'password':hash_pw(password),'plan':plan,'price':price,
        'status':'pending','created_at':datetime.now().strftime('%Y-%m-%d %H:%M'),
    }
    save_users(users)
    shop_dir(shop_id)
    return jsonify({'ok':True,'plan':plan,'price':price})

@app.route('/api/login', methods=['POST'])
def login():
    d        = request.get_json(silent=True) or {}
    username = d.get('username','').strip().lower()
    password = d.get('password','').strip()
    users    = load_users()
    user     = users.get(username)
    if not user or user['password'] != hash_pw(password):
        return jsonify({'ok':False,'msg':'ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง'}), 401
    if user['status'] == 'pending':
        return jsonify({'ok':False,'msg':'รอยืนยันการโอนเงินก่อนนะครับ ติดต่อ Line: @kaasod'}), 403
    session['shop_id']   = user['shop_id']
    session['shop_name'] = user['shop_name']
    session['username']  = username
    session['plan']      = user['plan']
    return jsonify({'ok':True,'shop_name':user['shop_name'],'plan':user['plan']})

@app.route('/api/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'ok':True})

@app.route('/api/me')
def me():
    if 'shop_id' not in session:
        return jsonify({'ok':False}), 401
    shop_id   = session['shop_id']
    plan      = session.get('plan','starter')
    usage     = get_today_usage(shop_id)
    remaining = None if plan == 'pro' else max(0, STARTER_DAILY_LIMIT - usage)
    return jsonify({
        'ok':True,'shop_id':shop_id,'shop_name':session['shop_name'],
        'username':session['username'],'plan':plan,
        'ai_usage_today':usage,'ai_remaining':remaining,
        'ai_daily_limit':STARTER_DAILY_LIMIT if plan == 'starter' else None,
    })

# ── AI chat ────────────────────────────────────────────────

@app.route('/api/chat', methods=['POST'])
def chat():
    if 'shop_id' not in session:
        return jsonify({'error':{'code':401,'message':'ไม่ได้ login','status':'UNAUTHORIZED'}}), 401
    if not GEMINI_KEY:
        return jsonify({'error':{'code':500,'message':'ยังไม่ได้ตั้งค่า GEMINI_API_KEY','status':'NO_KEY'}}), 500
    shop_id = session['shop_id']
    plan    = session.get('plan','starter')
    if plan == 'starter':
        usage = get_today_usage(shop_id)
        if usage >= STARTER_DAILY_LIMIT:
            return jsonify({'error':{'code':429,
                'message':f'⚠️ ใช้ AI ครบ {STARTER_DAILY_LIMIT} ข้อความวันนี้แล้วครับ\n\n🚀 อัปเกรดเป็น Pro 399฿/เดือน!\nติดต่อ Line: @kaasod',
                'status':'QUOTA_EXCEEDED'}}), 429
    data     = request.get_json(silent=True) or {}
    contents = data.get('contents',[])
    gen_cfg  = data.get('generationConfig',{'maxOutputTokens':1000,'temperature':0.7})
    if not contents:
        return jsonify({'error':{'code':400,'message':'ไม่มีข้อความ','status':'EMPTY'}}), 400
    url  = f'https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_KEY}'
    body = json.dumps({'contents':contents,'generationConfig':gen_cfg}).encode()
    req  = urllib.request.Request(url, data=body, headers={'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.loads(r.read())
        new_count = increment_usage(shop_id)
        log_chat(shop_id, session.get('username', ''), contents, resp)
        if plan == 'starter':
            remaining = max(0, STARTER_DAILY_LIMIT - new_count)
            resp['_quota'] = {'used':new_count,'remaining':remaining,
                              'limit':STARTER_DAILY_LIMIT,'warn':remaining<=5}
        return jsonify(resp)
    except urllib.error.HTTPError as e:
        return jsonify(json.loads(e.read().decode())), e.code
    except Exception as e:
        return jsonify({'error':{'code':500,'message':str(e),'status':'SERVER_ERROR'}}), 500

@app.route('/api/chat/status')
def chat_status():
    if 'shop_id' not in session:
        return jsonify({'ai_ready':False})
    shop_id   = session['shop_id']
    plan      = session.get('plan','starter')
    usage     = get_today_usage(shop_id)
    remaining = None if plan == 'pro' else max(0, STARTER_DAILY_LIMIT - usage)
    return jsonify({'ai_ready':bool(GEMINI_KEY),'plan':plan,'usage_today':usage,'remaining':remaining})

# ── sync ───────────────────────────────────────────────────

@app.route('/api/sync', methods=['POST'])
def sync_data():
    if 'shop_id' not in session:
        return jsonify({'error':'ไม่ได้ login'}), 401
    payload  = request.get_json(silent=True) or {}
    date_str = datetime.now().strftime('%Y-%m-%d')
    ts       = datetime.now().strftime('%Y%m%d_%H%M%S')
    d        = shop_dir(session['shop_id'])
    f        = d / f'backup_{date_str}.json'
    f.write_text(json.dumps({'synced_at':ts,'data':payload}, ensure_ascii=False, indent=2), encoding='utf-8')
    return jsonify({'status':'ok','synced_at':ts})

@app.route('/api/sync', methods=['GET'])
def get_sync():
    if 'shop_id' not in session:
        return jsonify({'status':'empty','data':{}})
    d     = shop_dir(session['shop_id'])
    files = sorted(d.glob('backup_*.json'), reverse=True)
    if not files:
        return jsonify({'status':'empty','data':{}})
    return jsonify({'status':'ok',**json.loads(files[0].read_text(encoding='utf-8'))})

# ── admin session API ──────────────────────────────────────

@app.route('/api/admin/login', methods=['POST'])
def admin_login():
    d   = request.get_json(silent=True) or {}
    key = d.get('key','').strip()
    if key != ADMIN_KEY:
        return jsonify({'ok':False,'msg':'รหัส Admin ไม่ถูกต้อง'}), 403
    session['is_admin'] = True
    return jsonify({'ok':True})

@app.route('/api/admin/logout', methods=['POST'])
def admin_logout():
    session.pop('is_admin', None)
    return jsonify({'ok':True})

@app.route('/api/admin/me')
def admin_me():
    if not is_admin():
        return jsonify({'ok':False}), 401
    return jsonify({'ok':True})

@app.route('/api/admin/users')
def admin_users():
    if not is_admin_request():
        return jsonify({'ok':False}), 401
    users = load_users()
    user_list = []
    for username, u in users.items():
        shop_id = u.get('shop_id','')
        user_list.append({
            'username':username,'shop_id':shop_id,
            'shop_name':u.get('shop_name',''),'phone':u.get('phone',''),
            'plan':u.get('plan','starter'),'price':u.get('price',199),
            'status':u.get('status','pending'),'created_at':u.get('created_at',''),
            'ai_today':get_today_usage(shop_id) if shop_id else 0,
        })
    user_list.sort(key=lambda x:(x['status']!='pending',x['created_at']))
    active  = sum(1 for u in users.values() if u['status']=='active')
    pending = sum(1 for u in users.values() if u['status']=='pending')
    revenue = sum(u['price'] for u in users.values() if u['status']=='active')
    return jsonify({'ok':True,'users':user_list,
        'summary':{'total':len(users),'active':active,'pending':pending,'revenue':revenue}})

@app.route('/api/admin/approve/<username>', methods=['POST'])
def api_admin_approve(username):
    if not is_admin(): return jsonify({'ok':False}), 401
    users = load_users()
    if username not in users: return jsonify({'ok':False,'msg':f'ไม่พบ {username}'}), 404
    users[username]['status'] = 'active'
    save_users(users)
    return jsonify({'ok':True,'msg':f"อนุมัติ {username} ({users[username]['shop_name']}) แล้ว"})

@app.route('/api/admin/suspend/<username>', methods=['POST'])
def api_admin_suspend(username):
    if not is_admin(): return jsonify({'ok':False}), 401
    users = load_users()
    if username not in users: return jsonify({'ok':False,'msg':f'ไม่พบ {username}'}), 404
    users[username]['status'] = 'pending'
    save_users(users)
    return jsonify({'ok':True,'msg':f"ระงับ {username} แล้ว"})

@app.route('/api/admin/edit/<username>', methods=['POST'])
def api_admin_edit(username):
    if not is_admin(): return jsonify({'ok':False}), 401
    users = load_users()
    if username not in users: return jsonify({'ok':False,'msg':f'ไม่พบ {username}'}), 404
    d         = request.get_json(silent=True) or {}
    shop_name = d.get('shop_name','').strip()
    phone     = d.get('phone','').strip()
    plan      = d.get('plan','')
    status    = d.get('status','')
    if not shop_name or not phone: return jsonify({'ok':False,'msg':'กรอกข้อมูลให้ครบ'}), 400
    if plan not in ('starter','pro'): return jsonify({'ok':False,'msg':'แพ็กเกจไม่ถูกต้อง'}), 400
    if status not in ('active','pending'): return jsonify({'ok':False,'msg':'สถานะไม่ถูกต้อง'}), 400
    users[username].update({'shop_name':shop_name,'phone':phone,'plan':plan,
                            'price':199 if plan=='starter' else 399,'status':status})
    save_users(users)
    return jsonify({'ok':True,'msg':f'อัปเดต {username} แล้ว'})

@app.route('/api/admin/reset_password/<username>', methods=['POST'])
def api_admin_reset_password(username):
    if not is_admin(): return jsonify({'ok':False}), 401
    users = load_users()
    if username not in users: return jsonify({'ok':False,'msg':f'ไม่พบ {username}'}), 404
    d        = request.get_json(silent=True) or {}
    password = d.get('password','').strip()
    if len(password) < 4: return jsonify({'ok':False,'msg':'รหัสผ่านต้องมีอย่างน้อย 4 ตัว'}), 400
    users[username]['password'] = hash_pw(password)
    save_users(users)
    return jsonify({'ok':True,'msg':f'รีเซ็ตรหัสผ่านของ {username} แล้ว'})

@app.route('/api/admin/delete/<username>', methods=['POST'])
def api_admin_delete(username):
    if not is_admin(): return jsonify({'ok':False}), 401
    users = load_users()
    if username not in users: return jsonify({'ok':False,'msg':f'ไม่พบ {username}'}), 404
    shop_name = users[username].get('shop_name', username)
    del users[username]
    save_users(users)
    return jsonify({'ok':True,'msg':f'ลบร้าน {shop_name} แล้ว'})

# ── admin/list — full HTML panel (key-based, no session needed) ──

@app.route('/admin/list')
def admin_list():
    key = request.args.get('key','')
    if key != ADMIN_KEY:
        return 'ไม่มีสิทธิ์', 403

    users   = load_users()
    active  = sum(1 for u in users.values() if u['status']=='active')
    pending = sum(1 for u in users.values() if u['status']=='pending')
    revenue = sum(u['price'] for u in users.values() if u['status']=='active')

    # action messages
    msg = request.args.get('msg','')
    msg_html = f'<div style="background:#1a3a1a;border:1px solid #5aaa6a;color:#5aaa6a;padding:10px 16px;border-radius:8px;margin-bottom:14px;">{esc(msg)}</div>' if msg else ''

    rows = ''
    for uname, u in sorted(users.items(), key=lambda x:(x[1]['status']!='pending', x[1].get('created_at',''))):
        sid      = u.get('shop_id','')
        usage    = get_today_usage(sid) if sid else 0
        plan_lbl = '🤖 Pro 399฿' if u['plan']=='pro' else '📋 Starter 199฿'
        st_color = '#5aaa6a' if u['status']=='active' else '#e07b3a'
        pw_hash  = u.get('password','')[:16] + '...'

        # backup files
        backups = ''
        if sid:
            bfiles = sorted((SHOPS_DIR/sid).glob('backup_*.json'), reverse=True)[:3]
            for bf in bfiles:
                backups += f'<a href="/admin/backup/{esc(uname)}/{esc(bf.name)}?key={esc(key)}" style="color:#5b8db8;font-size:11px;display:block;margin-top:2px;">📄 {esc(bf.name)}</a>'
        if not backups:
            backups = '<span style="color:#6a5e50;font-size:11px;">ยังไม่มีข้อมูล</span>'

        data_links = f'''<a href="/admin/analyze/{esc(uname)}?key={esc(key)}" style="color:#c9a84c;font-size:11px;font-weight:700;display:block;">🤖 AI วิเคราะห์</a>
<a href="/admin/export/{esc(uname)}?key={esc(key)}&mode=sales" style="color:#5b8db8;font-size:11px;display:block;margin-top:2px;">⬇️ ยอดขาย CSV</a>
<a href="/admin/export/{esc(uname)}?key={esc(key)}&mode=members" style="color:#5b8db8;font-size:11px;display:block;margin-top:2px;">⬇️ สมาชิก CSV</a>
<a href="/admin/export/{esc(uname)}?key={esc(key)}&mode=products" style="color:#5b8db8;font-size:11px;display:block;margin-top:2px;">⬇️ สินค้า CSV</a>'''

        approve_btn = ''
        if u['status'] == 'pending':
            approve_btn = f'<a href="/admin/action?key={esc(key)}&act=approve&u={esc(uname)}" style="background:rgba(90,170,106,0.2);color:#5aaa6a;border:1px solid rgba(90,170,106,0.4);padding:4px 10px;border-radius:6px;text-decoration:none;font-size:12px;font-weight:700;">✅ อนุมัติ</a>'
        else:
            approve_btn = f'<a href="/admin/action?key={esc(key)}&act=suspend&u={esc(uname)}" style="background:rgba(224,123,58,0.15);color:#e07b3a;border:1px solid rgba(224,123,58,0.4);padding:4px 10px;border-radius:6px;text-decoration:none;font-size:12px;font-weight:700;">⏸ ระงับ</a>'

        rows += f"""
        <tr id="row-{esc(uname)}">
          <td style="font-weight:700">{esc(uname)}</td>
          <td>
            <input name="shop_name" form="f-{esc(uname)}" value="{esc(u['shop_name'])}"
              style="background:#2a2520;border:1px solid #3e342a;color:#f0e8dc;padding:5px 8px;border-radius:6px;width:130px;font-size:13px;">
          </td>
          <td>
            <input name="phone" form="f-{esc(uname)}" value="{esc(u.get('phone',''))}"
              style="background:#2a2520;border:1px solid #3e342a;color:#f0e8dc;padding:5px 8px;border-radius:6px;width:110px;font-size:13px;">
          </td>
          <td>
            <select name="plan" form="f-{esc(uname)}" style="background:#2a2520;border:1px solid #3e342a;color:#c9a84c;padding:5px 6px;border-radius:6px;font-size:12px;">
              <option value="starter" {'selected' if u['plan']=='starter' else ''}>Starter 199฿</option>
              <option value="pro" {'selected' if u['plan']=='pro' else ''}>Pro 399฿</option>
            </select>
          </td>
          <td>
            <select name="status" form="f-{esc(uname)}" style="background:#2a2520;border:1px solid #3e342a;color:{st_color};padding:5px 6px;border-radius:6px;font-size:12px;">
              <option value="active" {'selected' if u['status']=='active' else ''}>✅ Active</option>
              <option value="pending" {'selected' if u['status']=='pending' else ''}>⏳ Pending</option>
            </select>
          </td>
          <td style="font-size:11px;color:#a89880">{esc(pw_hash)}<br>
            <input name="new_password" form="f-{esc(uname)}" placeholder="รหัสใหม่ (ถ้าจะเปลี่ยน)"
              style="background:#2a2520;border:1px solid #3e342a;color:#f0e8dc;padding:4px 7px;border-radius:6px;width:130px;font-size:12px;margin-top:4px;">
          </td>
          <td style="color:#{'e07b3a' if usage>15 else '5aaa6a'};font-weight:700">{usage} msg</td>
          <td style="font-size:11px;color:#a89880">{esc(u.get('created_at',''))}</td>
          <td>{backups}</td>
          <td>{data_links}</td>
          <td style="white-space:nowrap">
            <form id="f-{esc(uname)}" method="POST" action="/admin/action?key={esc(key)}&act=edit&u={esc(uname)}" style="display:inline">
              <button type="submit" style="background:rgba(201,168,76,0.15);color:#c9a84c;border:1px solid rgba(201,168,76,0.4);padding:4px 10px;border-radius:6px;cursor:pointer;font-size:12px;font-weight:700;">💾 บันทึก</button>
            </form>
            &nbsp;
            {approve_btn}
          </td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ค้าสด — Admin</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Sarabun',sans-serif;background:#0f0e0c;color:#f0e8dc;padding:16px;font-size:14px}}
h2{{color:#c9a84c;font-size:20px;margin-bottom:14px}}
.cards{{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:16px}}
.card{{background:#1a1815;border:1px solid #2e2820;border-radius:10px;padding:12px 18px;text-align:center;min-width:90px}}
.card b{{display:block;font-size:22px;color:#c9a84c}}
.card.g b{{color:#5aaa6a}}.card.o b{{color:#e07b3a}}.card.p b{{color:#9b6fd4}}
.wrap{{overflow-x:auto}}
table{{border-collapse:collapse;width:100%;min-width:900px}}
th{{background:#1a1815;color:#c9a84c;padding:8px 10px;text-align:left;border:1px solid #2e2820;white-space:nowrap}}
td{{border:1px solid #2e2820;padding:7px 10px;vertical-align:middle}}
tr:hover td{{background:#1a1815}}
input:focus,select:focus{{outline:2px solid #c9a84c;outline-offset:1px}}
a{{color:#e07b3a;text-decoration:none}}
</style>
</head>
<body>
<h2>🌿 ค้าสด — Admin Panel</h2>
{msg_html}
<div class="cards">
  <div class="card"><b>{len(users)}</b>ร้านทั้งหมด</div>
  <div class="card g"><b>{active}</b>Active</div>
  <div class="card o"><b>{pending}</b>รอยืนยัน</div>
  <div class="card p"><b>{revenue:,}฿</b>รายได้/เดือน</div>
</div>
<div class="wrap">
<table>
<tr>
  <th>Username</th><th>ชื่อร้าน</th><th>เบอร์</th>
  <th>แพ็กเกจ</th><th>สถานะ</th>
  <th>Password hash / รีเซ็ต</th>
  <th>AI วันนี้</th><th>สมัครเมื่อ</th>
  <th>Backup JSON</th><th>วิเคราะห์/Export</th><th>Actions</th>
</tr>
{rows}
</table>
</div>
</body>
</html>"""
    return html

# ── admin actions (POST/GET redirect) ─────────────────────

@app.route('/admin/action', methods=['GET','POST'])
def admin_action():
    key = request.args.get('key','')
    if key != ADMIN_KEY:
        return 'ไม่มีสิทธิ์', 403

    act   = request.args.get('act','')
    uname = request.args.get('u','')
    users = load_users()

    if uname not in users:
        return redirect(f'/admin/list?key={key}&msg=ไม่พบ+user+{uname}')

    if act == 'approve':
        users[uname]['status'] = 'active'
        save_users(users)
        return redirect(f'/admin/list?key={key}&msg=✅+อนุมัติ+{uname}+แล้ว')

    elif act == 'suspend':
        users[uname]['status'] = 'pending'
        save_users(users)
        return redirect(f'/admin/list?key={key}&msg=⏸+ระงับ+{uname}+แล้ว')

    elif act == 'edit':
        d         = request.form
        shop_name = d.get('shop_name','').strip()
        phone     = d.get('phone','').strip()
        plan      = d.get('plan','starter')
        status    = d.get('status','pending')
        new_pw    = d.get('new_password','').strip()
        if shop_name: users[uname]['shop_name'] = shop_name
        if phone:     users[uname]['phone']     = phone
        if plan in ('starter','pro'):
            users[uname]['plan']  = plan
            users[uname]['price'] = 199 if plan=='starter' else 399
        if status in ('active','pending'):
            users[uname]['status'] = status
        msg = f'💾+บันทึก+{uname}+แล้ว'
        if new_pw:
            if len(new_pw) >= 4:
                users[uname]['password'] = hash_pw(new_pw)
                msg = f'💾+บันทึก+และรีเซ็ต+password+ของ+{uname}+แล้ว'
            else:
                save_users(users)
                return redirect(f'/admin/list?key={key}&msg=❌+รหัสผ่านต้องมีอย่างน้อย+4+ตัว')
        save_users(users)
        return redirect(f'/admin/list?key={key}&msg={msg}')

    return redirect(f'/admin/list?key={key}')

# ── admin/backup — list backup files ─────────────────────

@app.route('/admin/backup/<username>/')
@app.route('/admin/backup/<username>')
def admin_backup_list(username):
    key = request.args.get('key', '')
    if key != ADMIN_KEY:
        return 'ไม่มีสิทธิ์', 403
    users = load_users()
    if username not in users:
        return 'ไม่พบ user', 404
    sid = users[username].get('shop_id', '')
    if not sid:
        return 'ไม่มี shop_id', 404

    bfiles = sorted((SHOPS_DIR / sid).glob('backup_*.json'), reverse=True)
    chat_log = SHOPS_DIR / sid / 'chat_log.json'

    rows = ''
    for bf in bfiles:
        size = bf.stat().st_size
        size_str = f'{size:,} bytes' if size < 1024 else f'{size//1024} KB'
        rows += f'''<tr>
          <td><a href="/admin/backup/{esc(username)}/{esc(bf.name)}?key={esc(key)}"
                 style="color:#5b8db8">📄 {esc(bf.name)}</a></td>
          <td style="color:#7a6a58">{size_str}</td>
          <td style="white-space:nowrap">
            <a href="/admin/export/{esc(username)}?key={esc(key)}&mode=sales"
               style="color:#5b8db8;font-size:11px;">⬇️ ยอดขาย</a>
            <a href="/admin/export/{esc(username)}?key={esc(key)}&mode=members"
               style="color:#5b8db8;font-size:11px;margin-left:6px;">⬇️ สมาชิก</a>
            <a href="/admin/export/{esc(username)}?key={esc(key)}&mode=products"
               style="color:#5b8db8;font-size:11px;margin-left:6px;">⬇️ สินค้า</a>
          </td>
        </tr>'''

    chat_row = ''
    if chat_log.exists():
        chat_row = f'''<tr>
          <td><a href="/admin/backup/{esc(username)}/chat_log.json?key={esc(key)}"
                 style="color:#4aaa6a">💬 chat_log.json</a></td>
          <td style="color:#7a6a58">{chat_log.stat().st_size:,} bytes</td>
          <td style="color:#7a6a58;font-size:11px;">ประวัติ AI Chat</td>
        </tr>'''

    u = users[username]
    html = f"""<!DOCTYPE html>
<html lang="th"><head><meta charset="UTF-8">
<title>Backup — {esc(username)}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Sarabun',sans-serif;background:#0f0e0c;color:#f0e8dc;padding:20px;font-size:14px}}
h2{{color:#c9a84c;margin-bottom:4px}}
.meta{{color:#7a6a58;font-size:12px;margin-bottom:16px}}
a.back{{color:#e07b3a;text-decoration:none}}
table{{border-collapse:collapse;width:100%;margin-top:12px}}
th{{background:#1a1815;color:#c9a84c;padding:8px 12px;text-align:left;border:1px solid #2e2820}}
td{{border:1px solid #2e2820;padding:8px 12px;vertical-align:middle}}
tr:hover td{{background:#1a1815}}
.analyze{{display:inline-block;margin-top:14px;background:rgba(201,168,76,.15);
  color:#c9a84c;border:1px solid rgba(201,168,76,.4);padding:8px 16px;
  border-radius:8px;text-decoration:none;font-weight:700;}}
</style></head><body>
<a class="back" href="/admin/list?key={esc(key)}">← กลับหน้า Admin</a>
<br><br>
<h2>📂 Backup ร้าน {esc(u.get('shop_name', username))}</h2>
<div class="meta">@{esc(username)} · {u.get('plan','').upper()} · {u.get('status','')}</div>
<table>
  <tr><th>ไฟล์</th><th>ขนาด</th><th>Actions</th></tr>
  {chat_row}
  {rows if rows else '<tr><td colspan="3" style="color:#6a5e50;text-align:center;padding:20px;">ยังไม่มีข้อมูล Backup</td></tr>'}
</table>
<a class="analyze" href="/admin/analyze/{esc(username)}?key={esc(key)}">🤖 AI วิเคราะห์ยอดขาย</a>
</body></html>"""
    return html


# ── admin/backup — view JSON file ─────────────────────────

@app.route('/admin/backup/<username>/<filename>')
def admin_backup(username, filename):
    key = request.args.get('key','')
    if key != ADMIN_KEY:
        return 'ไม่มีสิทธิ์', 403
    users = load_users()
    if username not in users:
        return 'ไม่พบ user', 404
    sid = users[username].get('shop_id','')
    if not sid:
        return 'ไม่มี shop_id', 404
    # sanitize filename — allow backup_*.json and chat_log.json
    if '..' in filename or '/' in filename:
        return 'invalid', 400
    if not (filename.startswith('backup_') or filename == 'chat_log.json'):
        return 'ไม่อนุญาตไฟล์นี้', 400
    f = SHOPS_DIR / sid / filename
    if not f.exists():
        return 'ไม่พบไฟล์', 404
    content = f.read_text(encoding='utf-8')
    # pretty HTML view
    html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<title>Backup — {esc(username)} — {esc(filename)}</title>
<style>body{{background:#0f0e0c;color:#f0e8dc;font-family:monospace;padding:20px}}
h3{{color:#c9a84c;margin-bottom:12px}}
a{{color:#e07b3a}}
pre{{background:#1a1815;border:1px solid #2e2820;border-radius:10px;padding:16px;
     overflow-x:auto;font-size:13px;line-height:1.6;white-space:pre-wrap;word-break:break-all}}
</style></head><body>
<h3>📄 {esc(username)} / {esc(filename)}</h3>
<a href="/admin/list?key={esc(key)}">← กลับหน้า Admin</a><br><br>
<pre>{esc(content)}</pre>
</body></html>"""
    return html

# ── legacy approve ─────────────────────────────────────────

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

# ── admin/export — CSV download ───────────────────────────

@app.route('/admin/export/<username>')
def admin_export(username):
    key = request.args.get('key', '')
    if key != ADMIN_KEY:
        return 'ไม่มีสิทธิ์', 403
    users = load_users()
    if username not in users:
        return 'ไม่พบ user', 404

    sid = users[username].get('shop_id', '')
    if not sid:
        return 'ไม่มี shop_id', 404

    mode = request.args.get('mode', 'sales')  # sales | members | products
    bfiles = sorted((SHOPS_DIR / sid).glob('backup_*.json'), reverse=True)
    if not bfiles:
        return 'ยังไม่มีข้อมูล backup', 404

    # รวมทุก backup (ไม่ซ้ำกัน)
    all_data = {}
    for bf in bfiles:
        try:
            raw = json.loads(bf.read_text(encoding='utf-8'))
            d   = raw.get('data', {})
            for k, v in d.items():
                if k not in all_data:
                    all_data[k] = v
        except Exception:
            pass

    import io, csv
    output = io.StringIO()
    w      = csv.writer(output)
    shop_name = users[username].get('shop_name', username)

    if mode == 'sales':
        # ประวัติการขาย
        history = all_data.get('history', [])
        if not history:
            return 'ไม่มีข้อมูลประวัติการขาย', 404
        w.writerow(['วันที่', 'เวลา', 'สินค้า', 'จำนวนชิ้น', 'ยอดรวม', 'ชำระด้วย', 'พนักงาน'])
        for item in history:
            items_txt = ', '.join([f"{x.get('name','')} x{x.get('qty',1)}" for x in item.get('items', [])])
            qty_total = sum(x.get('qty', 1) for x in item.get('items', []))
            ts        = item.get('timestamp', item.get('time', ''))
            date_part = ts[:10] if len(ts) >= 10 else ts
            time_part = ts[11:16] if len(ts) >= 16 else ''
            w.writerow([
                date_part, time_part, items_txt, qty_total,
                item.get('total', 0),
                item.get('payMethod', item.get('pay', '')),
                item.get('staff', item.get('cashier', '')),
            ])
        filename = f'sales_{username}_{datetime.now().strftime("%Y%m%d")}.csv'

    elif mode == 'members':
        # ข้อมูลสมาชิก CRM
        members = all_data.get('members', all_data.get('crm', []))
        if not members:
            return 'ไม่มีข้อมูลสมาชิก', 404
        w.writerow(['ชื่อ', 'เบอร์โทร', 'แต้มสะสม', 'ยอดซื้อรวม', 'จำนวนครั้งที่ซื้อ', 'วันสมัคร'])
        items_list = members if isinstance(members, list) else members.values()
        for m in items_list:
            w.writerow([
                m.get('name', ''), m.get('phone', ''),
                m.get('points', m.get('point', 0)),
                m.get('totalSpent', m.get('total_spent', 0)),
                m.get('visitCount', m.get('visit_count', 0)),
                m.get('createdAt', m.get('created_at', '')),
            ])
        filename = f'members_{username}_{datetime.now().strftime("%Y%m%d")}.csv'

    elif mode == 'products':
        # สินค้าและสต็อก
        products = all_data.get('products', all_data.get('items', []))
        if not products:
            return 'ไม่มีข้อมูลสินค้า', 404
        w.writerow(['ชื่อสินค้า', 'ราคา', 'สต็อกคงเหลือ', 'หน่วย'])
        items_list = products if isinstance(products, list) else products.values()
        for p in items_list:
            w.writerow([
                p.get('name', ''), p.get('price', 0),
                p.get('stock', p.get('qty', '')),
                p.get('unit', ''),
            ])
        filename = f'products_{username}_{datetime.now().strftime("%Y%m%d")}.csv'

    else:
        return 'mode ไม่ถูกต้อง (sales/members/products)', 400

    output.seek(0)
    # BOM for Excel Thai
    content = '\ufeff' + output.getvalue()
    return Response(
        content.encode('utf-8-sig'),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'}
    )

# ── admin/analyze — AI วิเคราะห์ยอดขาย ──────────────────

@app.route('/admin/analyze/<username>')
def admin_analyze(username):
    key = request.args.get('key', '')
    if key != ADMIN_KEY:
        return 'ไม่มีสิทธิ์', 403
    users = load_users()
    if username not in users:
        return 'ไม่พบ user', 404

    if not GEMINI_KEY:
        return 'ยังไม่ได้ตั้งค่า GEMINI_API_KEY', 500

    sid = users[username].get('shop_id', '')
    if not sid:
        return 'ไม่มี shop_id', 404

    bfiles = sorted((SHOPS_DIR / sid).glob('backup_*.json'), reverse=True)[:7]
    if not bfiles:
        return 'ยังไม่มีข้อมูล backup', 404

    # รวมข้อมูล
    all_history  = []
    all_members  = []
    all_products = []
    for bf in bfiles:
        try:
            raw = json.loads(bf.read_text(encoding='utf-8'))
            d   = raw.get('data', {})
            h   = d.get('history', [])
            if isinstance(h, list): all_history.extend(h)
            m = d.get('members', d.get('crm', []))
            if isinstance(m, list): all_members.extend(m)
            elif isinstance(m, dict): all_members.extend(m.values())
            p = d.get('products', d.get('items', []))
            if isinstance(p, list): all_products.extend(p)
            elif isinstance(p, dict): all_products.extend(p.values())
        except Exception:
            pass

    if not all_history:
        return 'ยังไม่มีข้อมูลยอดขาย', 404

    # กรองไม่ให้ข้อมูลใหญ่เกิน — เอาแค่ 200 รายการล่าสุด
    all_history  = all_history[-200:]
    all_members  = all_members[:100]
    all_products = all_products[:50]

    shop_name = users[username].get('shop_name', username)
    total_revenue = sum(item.get('total', 0) for item in all_history)

    prompt = f"""คุณเป็นที่ปรึกษาธุรกิจค้าปลีกมืออาชีพ วิเคราะห์ข้อมูลร้าน "{shop_name}" แล้วสรุปเป็นรายงานภาษาไทยที่เจ้าของร้านอ่านเข้าใจง่าย

ข้อมูลประวัติการขาย ({len(all_history)} รายการ, ยอดรวม {total_revenue:,.0f} บาท):
{json.dumps(all_history, ensure_ascii=False)[:3000]}

ข้อมูลสมาชิก ({len(all_members)} คน):
{json.dumps(all_members[:20], ensure_ascii=False)[:1000]}

สินค้า ({len(all_products)} รายการ):
{json.dumps(all_products[:20], ensure_ascii=False)[:500]}

กรุณาวิเคราะห์และสรุปในหัวข้อเหล่านี้:
1. 📊 ภาพรวมยอดขาย (ยอดรวม เฉลี่ยต่อบิล จำนวนบิล)
2. 🏆 สินค้าขายดี 5 อันดับแรก
3. ⏰ ช่วงเวลาขายดีที่สุด
4. 👥 สรุปพฤติกรรมลูกค้า/สมาชิก
5. 💡 คำแนะนำเพื่อเพิ่มยอดขาย 3 ข้อ
6. ⚠️ สิ่งที่ควรระวัง (ถ้ามี)

ตอบเป็น HTML ที่อ่านง่าย ใช้ emoji ประกอบ ไม่ต้องมี CSS ซับซ้อน"""

    url  = f'https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_KEY}'
    body = json.dumps({
        'contents': [{'parts': [{'text': prompt}], 'role': 'user'}],
        'generationConfig': {'maxOutputTokens': 2000, 'temperature': 0.4}
    }).encode()
    req = urllib.request.Request(url, data=body, headers={'Content-Type': 'application/json'})

    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            resp = json.loads(r.read())
        ai_html = resp['candidates'][0]['content']['parts'][0]['text']
        # clean markdown code fences if any
        ai_html = ai_html.replace('```html', '').replace('```', '').strip()
    except Exception as e:
        ai_html = f'<p style="color:red">เกิดข้อผิดพลาด: {esc(str(e))}</p>'

    page = f"""<!DOCTYPE html>
<html lang="th"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>วิเคราะห์ — {esc(shop_name)}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Sarabun',sans-serif;background:#0f0e0c;color:#f0e8dc;padding:16px;font-size:15px;line-height:1.7}}
h1{{color:#c9a84c;font-size:18px;margin-bottom:4px}}
.meta{{color:#6a5e50;font-size:12px;margin-bottom:16px}}
.back{{color:#e07b3a;text-decoration:none;font-size:13px}}
.export-links{{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px}}
.export-links a{{background:#1a1815;border:1px solid #2e2820;color:#5b8db8;padding:7px 14px;border-radius:8px;text-decoration:none;font-size:13px;font-weight:700}}
.export-links a:hover{{border-color:#5b8db8}}
.report{{background:#1a1815;border:1px solid #2e2820;border-radius:12px;padding:16px}}
.report h2,.report h3{{color:#c9a84c;margin:14px 0 6px}}
.report h2:first-child,.report h3:first-child{{margin-top:0}}
.report ul,.report ol{{padding-left:18px;margin:6px 0}}
.report li{{margin:4px 0}}
.report strong{{color:#e8c56a}}
.report p{{margin:6px 0}}
.report table{{border-collapse:collapse;width:100%;margin:8px 0}}
.report th{{background:#26201a;color:#c9a84c;padding:7px 10px;text-align:left;border:1px solid #2e2820}}
.report td{{border:1px solid #2e2820;padding:7px 10px}}
</style>
</head><body>
<a class="back" href="/admin/list?key={esc(key)}">← กลับหน้า Admin</a>
<br><br>
<h1>📊 รายงานวิเคราะห์ร้าน {esc(shop_name)}</h1>
<div class="meta">@{esc(username)} · วิเคราะห์เมื่อ {datetime.now().strftime('%d/%m/%Y %H:%M')} · {len(all_history)} บิล</div>

<div class="export-links">
  <a href="/admin/export/{esc(username)}?key={esc(key)}&mode=sales">⬇️ Export ยอดขาย CSV</a>
  <a href="/admin/export/{esc(username)}?key={esc(key)}&mode=members">⬇️ Export สมาชิก CSV</a>
  <a href="/admin/export/{esc(username)}?key={esc(key)}&mode=products">⬇️ Export สินค้า CSV</a>
</div>

<div class="report">
{ai_html}
</div>
</body></html>"""
    return page

# ── shop analytics APIs (for AI context) ──────────────────

@app.route('/api/shop/sales-report')
def shop_sales_report():
    """
    Detailed Sales Report: product_name, quantity_sold, total_amount_per_item
    AI ใช้เพื่อวิเคราะห์สินค้าขายดี และแนะนำการบริหารสต็อก
    """
    if 'shop_id' not in session:
        return jsonify({'ok': False, 'msg': 'ไม่ได้ login'}), 401
    shop_id = session['shop_id']

    bfiles = sorted(shop_dir(shop_id).glob('backup_*.json'), reverse=True)[:7]
    all_history = []
    for bf in bfiles:
        try:
            raw = json.loads(bf.read_text(encoding='utf-8'))
            h = raw.get('data', {}).get('history', [])
            if isinstance(h, list):
                all_history.extend(h)
        except Exception:
            pass

    # Aggregate by product name
    product_stats = {}
    for sale in all_history:
        for item in sale.get('items', []):
            name = item.get('name', 'ไม่ระบุ')
            qty  = item.get('qty', 1)
            price = item.get('price', 0)
            if name not in product_stats:
                product_stats[name] = {
                    'product_name': name,
                    'quantity_sold': 0,
                    'total_amount': 0,
                    'unit_price': price,
                }
            product_stats[name]['quantity_sold'] += qty
            product_stats[name]['total_amount']  += qty * price

    sorted_products = sorted(product_stats.values(),
                             key=lambda x: x['quantity_sold'], reverse=True)

    total_revenue = sum(s.get('total', 0) for s in all_history)
    total_bills   = len(all_history)
    avg_bill      = round(total_revenue / total_bills, 2) if total_bills > 0 else 0

    return jsonify({
        'ok': True,
        'summary': {
            'total_revenue': total_revenue,
            'total_bills': total_bills,
            'avg_bill_amount': avg_bill,
        },
        'products': sorted_products,
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    })


@app.route('/api/shop/low-stock')
def shop_low_stock():
    """
    Real-time Inventory Alert: สินค้าที่สต็อกต่ำกว่าเกณฑ์
    ?threshold=10 (default 10)
    """
    if 'shop_id' not in session:
        return jsonify({'ok': False, 'msg': 'ไม่ได้ login'}), 401
    shop_id   = session['shop_id']
    threshold = int(request.args.get('threshold', 10))

    bfiles = sorted(shop_dir(shop_id).glob('backup_*.json'), reverse=True)
    if not bfiles:
        return jsonify({'ok': True, 'threshold': threshold, 'low_stock': [], 'total': 0})

    try:
        raw      = json.loads(bfiles[0].read_text(encoding='utf-8'))
        products = raw.get('data', {}).get('products',
                   raw.get('data', {}).get('items', []))
    except Exception:
        return jsonify({'ok': True, 'threshold': threshold, 'low_stock': [], 'total': 0})

    items_list = products if isinstance(products, list) else list(products.values())

    low = []
    for p in items_list:
        st = p.get('stock', p.get('qty', None))
        if st is not None and isinstance(st, (int, float)) and st < threshold:
            low.append({
                'name':   p.get('name', ''),
                'stock':  st,
                'unit':   p.get('unit', 'ชิ้น'),
                'price':  p.get('price', 0),
                'status': 'หมด' if st == 0 else 'ใกล้หมด',
            })
    low.sort(key=lambda x: x['stock'])

    return jsonify({
        'ok': True,
        'threshold': threshold,
        'low_stock': low,
        'total': len(low),
        'checked_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    })


@app.route('/api/shop/member-insights')
def shop_member_insights():
    """
    Member Insight: สรุปพฤติกรรมสมาชิก สิทธิ์แลกฟรี ยอดสะสม
    AI ใช้วางแผนโปรโมชั่นและดูแลลูกค้าประจำ
    """
    if 'shop_id' not in session:
        return jsonify({'ok': False, 'msg': 'ไม่ได้ login'}), 401
    shop_id = session['shop_id']

    bfiles = sorted(shop_dir(shop_id).glob('backup_*.json'), reverse=True)
    if not bfiles:
        return jsonify({'ok': True, 'total_members': 0, 'insights': {}, 'top_members': []})

    try:
        raw     = json.loads(bfiles[0].read_text(encoding='utf-8'))
        members = raw.get('data', {}).get('members',
                  raw.get('data', {}).get('crm', []))
    except Exception:
        return jsonify({'ok': True, 'total_members': 0, 'insights': {}, 'top_members': []})

    items_list = members if isinstance(members, list) else list(members.values())

    total        = len(items_list)
    total_points = sum(m.get('points', m.get('point', 0)) for m in items_list)
    total_spent  = sum(m.get('totalSpend', m.get('totalSpent', m.get('total_spent', 0))) for m in items_list)
    total_visits = sum(m.get('totalBills', m.get('visitCount', m.get('visit_count', 0))) for m in items_list)
    loyalty_n    = raw.get('data', {}).get('loyaltyN', 10)
    redeemable   = sum(1 for m in items_list
                       if (m.get('points', m.get('point', 0)) // loyalty_n) > 0)

    top_list = sorted(items_list,
                      key=lambda x: x.get('totalSpend', x.get('totalSpent', x.get('total_spent', 0))),
                      reverse=True)[:5]
    top_out = [{
        'name':  m.get('name', ''),
        'phone': m.get('phone', ''),
        'total_spend': m.get('totalSpend', m.get('totalSpent', m.get('total_spent', 0))),
        'points': m.get('points', m.get('point', 0)),
        'bills': m.get('totalBills', m.get('visitCount', 0)),
        'redeemable': m.get('points', 0) // loyalty_n,
    } for m in top_list]

    return jsonify({
        'ok': True,
        'total_members': total,
        'loyalty_n': loyalty_n,
        'insights': {
            'total_points_outstanding': total_points,
            'total_member_spend': total_spent,
            'total_visits': total_visits,
            'members_with_redeemable_points': redeemable,
            'avg_spend_per_member': round(total_spent / total, 2) if total > 0 else 0,
            'avg_bills_per_member': round(total_visits / total, 2) if total > 0 else 0,
        },
        'top_members': top_out,
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    })


@app.route('/api/shop/payment-breakdown')
def shop_payment_breakdown():
    """
    Payment Breakdown: แยกประเภทการชำระ เงินสด/โอน/แลกฟรี
    JSON พร้อมใช้ ไม่ต้องแปลงเพิ่ม
    """
    if 'shop_id' not in session:
        return jsonify({'ok': False, 'msg': 'ไม่ได้ login'}), 401
    shop_id = session['shop_id']

    bfiles = sorted(shop_dir(shop_id).glob('backup_*.json'), reverse=True)[:7]
    all_history = []
    for bf in bfiles:
        try:
            raw = json.loads(bf.read_text(encoding='utf-8'))
            h   = raw.get('data', {}).get('history', [])
            if isinstance(h, list):
                all_history.extend(h)
        except Exception:
            pass

    breakdown = {
        'cash':     {'label': 'เงินสด',       'count': 0, 'total': 0},
        'transfer': {'label': 'โอนเงิน/QR',    'count': 0, 'total': 0},
        'free':     {'label': 'แลกฟรี/สิทธิ์', 'count': 0, 'total': 0},
        'other':    {'label': 'อื่นๆ',          'count': 0, 'total': 0},
    }
    CASH_KW     = {'cash', 'เงินสด', 'สด'}
    TRANSFER_KW = {'transfer', 'โอน', 'qr', 'promptpay', 'พร้อมเพย์', 'โอนเงิน'}
    FREE_KW     = {'free', 'ฟรี', 'แลกฟรี', 'redeem'}

    for sale in all_history:
        pay   = str(sale.get('payMethod', sale.get('pay', ''))).lower().strip()
        total = sale.get('total', 0)
        if pay in FREE_KW or any(k in pay for k in FREE_KW):
            breakdown['free']['count'] += 1
            # ไม่นับ revenue จากบิลฟรี
        elif pay in CASH_KW or any(k in pay for k in CASH_KW):
            breakdown['cash']['count'] += 1
            breakdown['cash']['total'] += total
        elif pay in TRANSFER_KW or any(k in pay for k in TRANSFER_KW):
            breakdown['transfer']['count'] += 1
            breakdown['transfer']['total'] += total
        else:
            breakdown['other']['count'] += 1
            breakdown['other']['total'] += total

    grand_total = sum(v['total'] for v in breakdown.values())
    for key in breakdown:
        t = breakdown[key]['total']
        breakdown[key]['percentage'] = round(t / grand_total * 100, 1) if grand_total > 0 else 0

    return jsonify({
        'ok': True,
        'breakdown': breakdown,
        'grand_total': grand_total,
        'total_transactions': len(all_history),
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    })


# ── admin chat-logs API ────────────────────────────────────

@app.route('/api/admin/chat-logs')
def admin_chat_logs():
    """
    ดึง Chat Log ทุกร้าน — admin.html ใช้แสดงประวัติ AI Chat
    รองรับทั้ง session และ X-Admin-Key header หรือ ?key=
    """
    if not is_admin_request():
        return jsonify({'ok': False}), 403

    users  = load_users()
    result = {}
    for uname, u in users.items():
        sid = u.get('shop_id', '')
        if not sid:
            continue
        f = shop_dir(sid) / 'chat_log.json'
        if f.exists():
            try:
                logs = json.loads(f.read_text(encoding='utf-8'))
                result[uname] = {
                    'shop_name': u.get('shop_name', uname),
                    'plan':      u.get('plan', 'starter'),
                    'status':    u.get('status', 'pending'),
                    'logs':      logs,
                }
            except Exception:
                pass
    return jsonify({'ok': True, 'data': result})


# ── health ─────────────────────────────────────────────────

@app.route('/api/health')
def health():
    users = load_users()
    return jsonify({
        'status':'ok','version':'v5','model':GEMINI_MODEL,
        'starter_limit':STARTER_DAILY_LIMIT,
        'time':datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'total':len(users),
        'active':sum(1 for u in users.values() if u['status']=='active'),
        'pro':sum(1 for u in users.values() if u['plan']=='pro' and u['status']=='active'),
        'starter':sum(1 for u in users.values() if u['plan']=='starter' and u['status']=='active'),
        'ai_key':bool(GEMINI_KEY),
    })

if __name__ == '__main__':
    print(f"🌿 ค้าสด v5 | {GEMINI_MODEL} | starter limit: {STARTER_DAILY_LIMIT}/day")
    app.run(debug=True, host='0.0.0.0', port=5000)
