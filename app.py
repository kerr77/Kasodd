"""
app.py — Flask Backend สำหรับ POS Template v12
รองรับ: serve หน้า POS, backup/sync ข้อมูล, export CSV, ดู log ฝั่ง server
"""

from flask import Flask, render_template_string, request, jsonify, send_file, send_from_directory
import json, os, csv, io
from datetime import datetime
from pathlib import Path

app = Flask(__name__)

# ─── โฟลเดอร์เก็บข้อมูล ───────────────────────────────────────────────────────
DATA_DIR    = Path('pos_data')
BACKUP_DIR  = DATA_DIR / 'backups'
EXPORT_DIR  = DATA_DIR / 'exports'
STATIC_DIR  = Path('static')

for d in [DATA_DIR, BACKUP_DIR, EXPORT_DIR, STATIC_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# ─── ไฟล์ index.html (อยู่ที่รูทหรือใน templates/) ───────────────────────────
INDEX_HTML = Path('index.html')


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
        pins:     {...},   ← optional (ไม่บังคับส่ง)
    }
    """
    payload = request.get_json(silent=True) or {}
    ts      = datetime.now().strftime('%Y%m%d_%H%M%S')
    date_str = datetime.now().strftime('%Y-%m-%d')

    # บันทึก backup รายวัน (เขียนทับถ้าวันเดิม)
    daily_file = BACKUP_DIR / f'pos_backup_{date_str}.json'
    with open(daily_file, 'w', encoding='utf-8') as f:
        json.dump({'synced_at': ts, 'data': payload}, f, ensure_ascii=False, indent=2)

    # เก็บ snapshot แยกทุกครั้ง (เก็บไว้ 30 snapshot ล่าสุด)
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
    """ลบ snapshot เก่าเกิน keep ไฟล์"""
    snaps = sorted(BACKUP_DIR.glob('snap_*.json'), reverse=True)
    for old in snaps[keep:]:
        try: old.unlink()
        except: pass


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
    ts       = datetime.now().strftime('%Y%m%d_%H%M%S')
    date_str = datetime.now().strftime('%Y-%m-%d')

    # สรุปยอดขายแต่ละสินค้า
    summary = {}
    for bill in history:
        for item in bill.get('items', []):
            key = item.get('key', '')
            summary[key] = summary.get(key, 0) + item.get('qty', 0)

    cash = sum(b['total'] for b in history if b.get('pay') == 'cash')
    transfer = sum(b['total'] for b in history if b.get('pay') == 'transfer')
    free_cnt = sum(1 for b in history if b.get('pay') == 'free')

    output = io.StringIO()
    output.write('\ufeff')  # BOM for Excel
    w = csv.writer(output)

    w.writerow([f'รายงานยอดขาย POS — {date_str}'])
    w.writerow([])
    w.writerow(['สินค้า', 'ราคา/หน่วย', 'จำนวนที่ขาย', 'ยอดเงิน', 'สต็อกเหลือ'])
    for p in products:
        if p.get('promoBase'):
            continue  # ข้ามโปรโมชั่น
        key  = p.get('key', '')
        qty  = summary.get(key, 0)
        amt  = qty * p.get('price', 0)
        stk  = stock.get(key, 0)
        w.writerow([p.get('name', ''), p.get('price', 0), qty, amt, stk])

    w.writerow([])
    w.writerow(['─── สรุปการชำระ ───'])
    w.writerow(['เงินสด',  f'฿{cash:,.0f}'])
    w.writerow(['โอน',     f'฿{transfer:,.0f}'])
    w.writerow(['รวม',     f'฿{cash+transfer:,.0f}'])
    w.writerow(['แลกฟรี',  f'{free_cnt} บิล'])
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
    fname = f'pos_report_{date_str}.csv'
    filepath = EXPORT_DIR / fname
    filepath.write_bytes(csv_bytes)

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
    ts       = datetime.now().strftime('%Y%m%d_%H%M%S')
    date_str = datetime.now().strftime('%Y-%m-%d')

    payload['exported_at'] = ts
    json_bytes = json.dumps(payload, ensure_ascii=False, indent=2).encode('utf-8')
    fname = f'pos_export_{date_str}.json'
    filepath = EXPORT_DIR / fname
    filepath.write_bytes(json_bytes)

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
    """แสดงรายการ backup ทั้งหมด"""
    snaps = sorted(BACKUP_DIR.glob('snap_*.json'), reverse=True)
    result = []
    for f in snaps[:20]:  # แสดงแค่ 20 อันล่าสุด
        try:
            data = json.loads(f.read_text(encoding='utf-8'))
            history = data.get('data', {}).get('history', [])
            result.append({
                'file':      f.name,
                'synced_at': data.get('synced_at', ''),
                'bills':     len(history),
                'size_kb':   round(f.stat().st_size / 1024, 1),
            })
        except:
            result.append({'file': f.name, 'error': 'อ่านไม่ได้'})
    return jsonify({'status': 'ok', 'backups': result})


@app.route('/api/backups/<filename>', methods=['GET'])
def get_backup(filename):
    """โหลด backup ไฟล์เฉพาะ"""
    filepath = BACKUP_DIR / filename
    if not filepath.exists() or not filename.endswith('.json'):
        return jsonify({'error': 'ไม่พบไฟล์'}), 404
    data = json.loads(filepath.read_text(encoding='utf-8'))
    return jsonify({'status': 'ok', **data})


# ════════════════════════════════════════════════════════════
#  API — STOCK ALERT (server-side log)
# ════════════════════════════════════════════════════════════

@app.route('/api/stock/alert', methods=['POST'])
def log_stock_alert():
    """
    รับแจ้งเตือนสต็อกต่ำจาก frontend บันทึก log ฝั่ง server
    Body: { low_items: [{key, name, qty}], threshold: number }
    """
    payload    = request.get_json(silent=True) or {}
    low_items  = payload.get('low_items', [])
    threshold  = payload.get('threshold', 3)
    ts         = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    alert_log  = DATA_DIR / 'stock_alerts.jsonl'
    with open(alert_log, 'a', encoding='utf-8') as f:
        f.write(json.dumps({
            'time': ts,
            'threshold': threshold,
            'items': low_items,
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
        'server_time':  datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'backup_count': len(snaps),
        'data_dir':     str(DATA_DIR.resolve()),
    })


# ════════════════════════════════════════════════════════════
#  RUN
# ════════════════════════════════════════════════════════════

if __name__ == '__main__':
    print("=" * 55)
    print("  🌿 POS Server v12 — พร้อมให้บริการ")
    print("  http://localhost:5000")
    print("=" * 55)
    app.run(debug=True, host='0.0.0.0', port=5000)
