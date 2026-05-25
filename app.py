"""
app.py — Flask Backend สำหรับ KAASOD POS v13
รองรับ: serve หน้า POS, backup/sync ข้อมูล, export CSV, Gemini AI proxy
"""

from flask import Flask, request, jsonify, send_file, send_from_directory
import json, os, csv, io, urllib.request, urllib.error
from datetime import datetime
from pathlib import Path

app = Flask(__name__)

# ─── โฟลเดอร์เก็บข้อมูล ───────────────────────────────────────────────────────
DATA_DIR   = Path('pos_data')
BACKUP_DIR = DATA_DIR / 'backups'
EXPORT_DIR = DATA_DIR / 'exports'
STATIC_DIR = Path('static')

for d in [DATA_DIR, BACKUP_DIR, EXPORT_DIR, STATIC_DIR]:
    d.mkdir(parents=True, exist_ok=True)

INDEX_HTML = Path('index.html')

# ─── ไฟล์เก็บ Gemini Key (ฝั่ง server ปลอดภัยกว่า localStorage) ──────────────
GEMINI_KEY_FILE = DATA_DIR / 'gemini.key'


def _get_gemini_key() -> str:
    """อ่าน Gemini API Key จาก ENV ก่อน ถ้าไม่มีค่อยอ่านจากไฟล์"""
    return (
        os.environ.get('GEMINI_API_KEY', '').strip()
        or (GEMINI_KEY_FILE.read_text().strip() if GEMINI_KEY_FILE.exists() else '')
    )


# ════════════════════════════════════════════════════════════
#  PAGES
# ════════════════════════════════════════════════════════════

@app.route('/')
def index():
    """Serve หน้า POS หลัก"""
    if INDEX_HTML.exists():
        return INDEX_HTML.read_text(encoding='utf-8')
    return "<h1>❌ ไม่พบ index.html — วางไฟล์ไว้ที่รูทโปรเจกต์</h1>", 404


@app.route('/static/<path:filename>')
def static_files(filename):
    return send_from_directory(STATIC_DIR, filename)


# ════════════════════════════════════════════════════════════
#  API — GEMINI AI PROXY  🤖
#  Key อยู่ฝั่ง server — client ไม่เห็น key เลย
# ════════════════════════════════════════════════════════════

@app.route('/api/chat', methods=['POST'])
def proxy_chat():
    """
    Proxy คำถามไปยัง Gemini API
    Body: { contents: [...] }  ← Gemini contents format
    Response: Gemini JSON response ตรงๆ
    """
    api_key = _get_gemini_key()
    if not api_key:
        return jsonify({
            'error': 'ยังไม่ได้ตั้งค่า Gemini API Key',
            'hint':  'ติดต่อทีมงานเพื่อเปิดใช้งาน AI หรือตั้งค่า GEMINI_API_KEY ใน environment'
        }), 403

    payload = request.get_json(silent=True) or {}
    contents = payload.get('contents', [])
    gen_cfg  = payload.get('generationConfig', {
        'maxOutputTokens': 1000,
        'temperature': 0.7
    })

    model = 'gemini-2.0-flash-lite'   # อัปเดตเป็นรุ่นล่าสุด
    url   = (
        f'https://generativelanguage.googleapis.com/v1beta/models'
        f'/{model}:generateContent?key={api_key}'
    )
    body  = json.dumps({
        'contents': contents,
        'generationConfig': gen_cfg
    }).encode('utf-8')

    req = urllib.request.Request(
        url, data=body,
        headers={'Content-Type': 'application/json'},
        method='POST'
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return jsonify(json.loads(r.read()))
    except urllib.error.HTTPError as e:
        err_body = json.loads(e.read())
        return jsonify({'error': err_body}), e.code
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/chat/status', methods=['GET'])
def chat_status():
    """
    บอก frontend ว่า AI พร้อมใช้งานหรือเปล่า (ไม่ส่ง key กลับ)
    Response: { ai_ready: true/false }
    """
    return jsonify({'ai_ready': bool(_get_gemini_key())})


@app.route('/api/set-key', methods=['POST'])
def set_api_key():
    """
    ทีมงานเรียกครั้งเดียวเพื่อฝัง Gemini Key ให้ร้าน
    Header: X-Admin-Key: <ADMIN_KEY>
    Body:   { "key": "AIzaSy..." }
    """
    admin_key = request.headers.get('X-Admin-Key', '')
    expected  = os.environ.get('ADMIN_KEY', 'kaasod-admin-2026')
    if admin_key != expected:
        return jsonify({'error': 'Unauthorized'}), 401

    key = (request.get_json(silent=True) or {}).get('key', '').strip()
    if not key or not key.startswith('AIza'):
        return jsonify({'error': 'key ไม่ถูกต้อง (ต้องขึ้นต้นด้วย AIza)'}), 400

    GEMINI_KEY_FILE.write_text(key, encoding='utf-8')
    return jsonify({'status': 'ok', 'message': 'บันทึก Gemini Key แล้ว 🎉'})


@app.route('/api/remove-key', methods=['DELETE'])
def remove_api_key():
    """ลบ Gemini Key ออกจาก server (สำหรับยกเลิก package AI)"""
    admin_key = request.headers.get('X-Admin-Key', '')
    expected  = os.environ.get('ADMIN_KEY', 'kaasod-admin-2026')
    if admin_key != expected:
        return jsonify({'error': 'Unauthorized'}), 401

    if GEMINI_KEY_FILE.exists():
        GEMINI_KEY_FILE.unlink()
        return jsonify({'status': 'ok', 'message': 'ลบ Gemini Key แล้ว'})
    return jsonify({'status': 'not_found'}), 404


# ════════════════════════════════════════════════════════════
#  API — SYNC / BACKUP  (รับข้อมูลจาก localStorage ของ browser)
# ════════════════════════════════════════════════════════════

@app.route('/api/sync', methods=['POST'])
def sync_data():
    """
    รับ snapshot ข้อมูล POS ทั้งหมดจาก frontend แล้วบันทึกเป็น JSON backup
    Body: {
        stock:    {...},
        history:  [...],
        products: [...],
        members:  [...],
        petty:    [...],
        delivery: [...],
        shift:    {...},
    }
    """
    payload  = request.get_json(silent=True) or {}
    ts       = datetime.now().strftime('%Y%m%d_%H%M%S')
    date_str = datetime.now().strftime('%Y-%m-%d')

    # backup รายวัน (เขียนทับถ้าวันเดิม)
    daily_file = BACKUP_DIR / f'pos_backup_{date_str}.json'
    with open(daily_file, 'w', encoding='utf-8') as f:
        json.dump({'synced_at': ts, 'data': payload}, f, ensure_ascii=False, indent=2)

    # snapshot แยกทุกครั้ง (เก็บ 30 อันล่าสุด)
    snap_file = BACKUP_DIR / f'snap_{ts}.json'
    with open(snap_file, 'w', encoding='utf-8') as f:
        json.dump({'synced_at': ts, 'data': payload}, f, ensure_ascii=False, indent=2)

    _trim_snapshots(30)
    return jsonify({'status': 'ok', 'backup': str(daily_file), 'snapshot': str(snap_file)})


@app.route('/api/sync', methods=['GET'])
def get_latest_backup():
    """ดึงข้อมูล backup ล่าสุดให้ frontend โหลด (ใช้กู้คืน localStorage)"""
    snaps = sorted(BACKUP_DIR.glob('snap_*.json'), reverse=True)
    if not snaps:
        return jsonify({'status': 'empty', 'data': {}})
    with open(snaps[0], encoding='utf-8') as f:
        data = json.load(f)
    return jsonify({'status': 'ok', **data})


def _trim_snapshots(keep: int):
    snaps = sorted(BACKUP_DIR.glob('snap_*.json'), reverse=True)
    for old in snaps[keep:]:
        try:
            old.unlink()
        except Exception:
            pass


# ════════════════════════════════════════════════════════════
#  API — EXPORT
# ════════════════════════════════════════════════════════════

@app.route('/api/export/csv', methods=['POST'])
def export_csv():
    """
    สร้าง CSV สรุปยอดขาย
    Body: { history: [...], products: [...], stock: {...} }
    """
    payload  = request.get_json(silent=True) or {}
    history  = payload.get('history', [])
    products = payload.get('products', [])
    stock    = payload.get('stock', {})
    date_str = datetime.now().strftime('%Y-%m-%d')

    summary = {}
    for bill in history:
        for item in bill.get('items', []):
            key = item.get('key', '')
            summary[key] = summary.get(key, 0) + item.get('qty', 0)

    cash      = sum(b['total'] for b in history if b.get('pay') == 'cash')
    transfer  = sum(b['total'] for b in history if b.get('pay') == 'transfer')
    free_cnt  = sum(1 for b in history if b.get('pay') == 'free')

    output = io.StringIO()
    output.write('\ufeff')  # BOM for Excel
    w = csv.writer(output)

    w.writerow([f'รายงานยอดขาย KAASOD POS — {date_str}'])
    w.writerow([])
    w.writerow(['สินค้า', 'ราคา/หน่วย', 'จำนวนที่ขาย', 'ยอดเงิน', 'สต็อกเหลือ'])
    for p in products:
        if p.get('promoBase'):
            continue
        key = p.get('key', '')
        qty = summary.get(key, 0)
        amt = qty * p.get('price', 0)
        stk = stock.get(key, 0)
        w.writerow([p.get('name', ''), p.get('price', 0), qty, amt, stk])

    w.writerow([])
    w.writerow(['─── สรุปการชำระ ───'])
    w.writerow(['เงินสด',     f'฿{cash:,.0f}'])
    w.writerow(['โอน',        f'฿{transfer:,.0f}'])
    w.writerow(['รวม',        f'฿{cash + transfer:,.0f}'])
    w.writerow(['แลกฟรี',     f'{free_cnt} บิล'])
    w.writerow(['บิลทั้งหมด', f'{len(history)} ใบ'])

    w.writerow([])
    w.writerow(['─── ประวัติบิลแต่ละใบ ───'])
    w.writerow(['เวลา', 'สมาชิก', 'วิธีชำระ', 'รายการ', 'ยอด'])
    for bill in history:
        items_str = ', '.join(f"{i['name']} x{i['qty']}" for i in bill.get('items', []))
        w.writerow([
            bill.get('time', ''),
            bill.get('member', 'ทั่วไป'),
            bill.get('pay', ''),
            items_str,
            bill.get('total', 0),
        ])

    csv_bytes = output.getvalue().encode('utf-8-sig')
    fname     = f'pos_report_{date_str}.csv'
    (EXPORT_DIR / fname).write_bytes(csv_bytes)

    return send_file(
        io.BytesIO(csv_bytes),
        mimetype='text/csv; charset=utf-8-sig',
        as_attachment=True,
        download_name=fname,
    )


@app.route('/api/export/json', methods=['POST'])
def export_json():
    """Export ข้อมูลทั้งหมดเป็น JSON"""
    payload  = request.get_json(silent=True) or {}
    date_str = datetime.now().strftime('%Y-%m-%d')
    ts       = datetime.now().strftime('%Y%m%d_%H%M%S')

    payload['exported_at'] = ts
    json_bytes = json.dumps(payload, ensure_ascii=False, indent=2).encode('utf-8')
    fname = f'pos_export_{date_str}.json'
    (EXPORT_DIR / fname).write_bytes(json_bytes)

    return send_file(
        io.BytesIO(json_bytes),
        mimetype='application/json',
        as_attachment=True,
        download_name=fname,
    )


# ════════════════════════════════════════════════════════════
#  API — BACKUP MANAGEMENT
# ════════════════════════════════════════════════════════════

@app.route('/api/backups', methods=['GET'])
def list_backups():
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
