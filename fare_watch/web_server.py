"""Loopback-only web UI for the existing flight search and SQLite history."""
from __future__ import annotations

import argparse
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time
from urllib.parse import urlparse, parse_qs

from .search import airport, iso_date, parser, plan, ALIASES
from .price_history import History

ROOT = Path(__file__).resolve().parents[1]
ASSETS = Path(__file__).with_name('web')


def search_arguments(payload, output, history_db):
    if not isinstance(payload, dict):
        raise ValueError('請提供查詢條件')
    origin = airport(str(payload.get('origin', '')))
    destination = airport(str(payload.get('destination', '')))
    mode = payload.get('mode', 'dates')
    args = [origin, destination]
    if mode == 'dates':
        depart = iso_date(str(payload.get('depart', '')))
        args += [str(depart)]
        if payload.get('return_date'):
            args += [str(iso_date(str(payload['return_date'])))]
    elif mode == 'scan':
        start = iso_date(str(payload.get('start', '')))
        try:
            days, nights = int(payload.get('days', 30)), int(payload.get('nights', 4))
        except (ValueError, TypeError):
            raise ValueError('掃描天數及住宿晚數必須是整數')
        args += ['--start', str(start), '--days', str(days), '--nights', str(nights)]
        if payload.get('details'):
            args += ['--details']
    else:
        raise ValueError('不支援的查詢模式')
    args += ['--output', str(output), '--history-db', str(history_db)]
    if payload.get('refresh'):
        args += ['--refresh']
    plan(parser().parse_args(args))
    return args


def airport_options():
    names = {
        'TPE': '桃園國際機場 Taoyuan Taipei', 'TSA': '臺北松山機場 Songshan Taipei',
        'KHH': '高雄國際機場 Kaohsiung', 'RMQ': '臺中國際機場 Taichung',
        'PUS': '釜山金海國際機場 Busan Gimhae', 'ICN': '首爾仁川國際機場 Seoul Incheon',
        'GMP': '首爾金浦國際機場 Seoul Gimpo', 'CJU': '濟州國際機場 Jeju',
        'NRT': '東京成田國際機場 Tokyo Narita', 'HND': '東京羽田機場 Tokyo Haneda',
        'KIX': '大阪關西國際機場 Osaka Kansai', 'FUK': '福岡機場 Fukuoka',
        'OKA': '沖繩那霸機場 Okinawa Naha', 'CTS': '札幌新千歲機場 Sapporo Chitose',
        'HKG': '香港國際機場 Hong Kong', 'MFM': '澳門國際機場 Macau',
        'SIN': '新加坡樟宜機場 Singapore Changi', 'BKK': '曼谷蘇凡納布機場 Bangkok Suvarnabhumi',
        'KUL': '吉隆坡國際機場 Kuala Lumpur', 'DMK': '曼谷廊曼機場 Bangkok Don Mueang',
        'SGN': '胡志明市新山一機場 Ho Chi Minh City Tan Son Nhat', 'HAN': '河內內排機場 Hanoi Noi Bai',
        'DAD': '峴港國際機場 Da Nang', 'MNL': '馬尼拉國際機場 Manila',
        'CEB': '宿霧國際機場 Cebu', 'DPS': '峇里島伍拉賴機場 Bali Denpasar',
        'LAX': '洛杉磯國際機場 Los Angeles', 'SFO': '舊金山國際機場 San Francisco',
        'JFK': '紐約甘迺迪國際機場 New York Kennedy', 'SEA': '西雅圖塔科馬機場 Seattle',
        'YVR': '溫哥華國際機場 Vancouver', 'LHR': '倫敦希斯洛機場 London Heathrow',
        'CDG': '巴黎戴高樂機場 Paris Charles de Gaulle', 'FRA': '法蘭克福機場 Frankfurt',
        'SYD': '雪梨國際機場 Sydney', 'MEL': '墨爾本機場 Melbourne',
    }
    return [{'code': code, 'name': name, 'aliases': [a for a, c in ALIASES.items() if c == code]}
            for code, name in names.items()]


class Application:
    def __init__(self, data_dir=ROOT / 'data', runner=None):
        self.data_dir = Path(data_dir)
        self.db = self.data_dir / 'search' / 'history.sqlite3'
        self.jobs = {}
        self.lock = threading.Lock()
        self.token = secrets.token_urlsafe(32)
        self.runner = runner or self.execute
        self.calendar_options = {}

    def watches(self):
        path = self.data_dir / 'web' / 'watches.json'
        with self.lock:
            return json.loads(path.read_text()) if path.exists() else []

    def save_watch(self, payload):
        if not isinstance(payload, dict):
            raise ValueError('請提供追蹤條件')
        search_arguments(payload, self.data_dir / 'web', self.db)
        item = {key: payload[key] for key in ('mode', 'depart', 'return_date', 'start', 'days', 'nights') if key in payload}
        item.update(origin=airport(payload['origin']), destination=airport(payload['destination']))
        path = self.data_dir / 'web' / 'watches.json'
        with self.lock:
            rows = json.loads(path.read_text()) if path.exists() else []
            if any(all(r.get(k) == v for k, v in item.items()) for r in rows):
                return rows
            if len(rows) >= 100:
                raise ValueError('最多儲存 100 組追蹤')
            item.update(id=secrets.token_hex(12), created_at=time.time())
            rows.append(item)
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix('.tmp')
            temp.write_text(json.dumps(rows, ensure_ascii=False))
            temp.replace(path)
            return rows

    def remove_watch(self, watch_id):
        path = self.data_dir / 'web' / 'watches.json'
        with self.lock:
            rows = json.loads(path.read_text()) if path.exists() else []
            rows = [r for r in rows if r['id'] != watch_id]
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix('.tmp')
            temp.write_text(json.dumps(rows, ensure_ascii=False))
            temp.replace(path)
            return rows

    def holidays(self):
        from .dgpa_calendar import CAVEAT, DATASET_PAGE, Calendar, upcoming_breaks
        today = date.today()
        loaded = Calendar(self.data_dir / 'holidays', **self.calendar_options).load(today)
        return {'today': today.isoformat(), 'breaks': upcoming_breaks(loaded['days'], today),
                'sources': loaded['sources'], 'errors': loaded['errors'],
                'source_name': '行政院人事行政總處 政府行政機關辦公日曆表', 'source_page': DATASET_PAGE, 'caveat': CAVEAT}

    def deals_request(self, payload):
        origin = airport(str(payload.get('origin', '')))
        start, end = iso_date(str(payload.get('start', ''))), iso_date(str(payload.get('end', '')))
        return origin, start, end, self.data_dir / 'web' / 'deals' / f'{origin}-{start}-{end}'

    def deals(self, payload):
        origin, start, end, output = self.deals_request(payload)
        path = output / 'latest.json'
        with self.lock:
            running = next((j['id'] for j in self.jobs.values()
                            if j['state'] == 'running' and j['output'] == str(output)), None)
        return {'report': json.loads(path.read_text()) if path.exists() else None, 'job': running}

    def start_deals(self, payload):
        origin, start, end, output = self.deals_request(payload)
        if not any(b['start'] == str(start) and b['end'] == str(end) for b in self.holidays()['breaks']):
            raise ValueError('這不是辦公日曆表上即將到來的連假')
        args = [origin, str(start), str(end), '--output', str(output), '--history-db', str(self.db),
                '--calendar-dir', str(self.data_dir / 'holidays')] + (['--refresh'] if payload.get('refresh') else [])
        return self.start(payload, args=args, output=output, module='fare_watch.holiday_deals')

    def start(self, payload, args=None, output=None, module='fare_watch.search'):
        job_id = secrets.token_hex(12)
        if args is None:
            output = self.data_dir / 'web' / 'runs' / job_id
            args = search_arguments(payload, output, self.db)
        with self.lock:
            if any(j['state'] == 'running' for j in self.jobs.values()):
                raise RuntimeError('已有查詢正在執行，請等它完成後再查下一組。')
            self.jobs[job_id] = {'id': job_id, 'state': 'running', 'logs': [], 'created_at': time.time(),
                                 'output': str(output), 'report': None, 'error': '', 'module': module}
            # Keep active and recent jobs without unbounded process-memory growth.
            for old in list(self.jobs)[:-30]:
                del self.jobs[old]
        thread = threading.Thread(target=self.runner, args=(job_id, args, output), daemon=True)
        thread.start()
        return job_id

    def execute(self, job_id, args, output):
        try:
            output.mkdir(parents=True, exist_ok=True)
            shared_cache = self.data_dir / 'web' / 'cache'
            shared_cache.mkdir(parents=True, exist_ok=True)
            if not (output / 'cache').exists():
                (output / 'cache').symlink_to(shared_cache.resolve(), target_is_directory=True)
            process = subprocess.Popen([sys.executable, '-m', self.jobs[job_id]['module'], *args], cwd=ROOT,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            for line in process.stderr:
                with self.lock:
                    self.jobs[job_id]['logs'] = (self.jobs[job_id]['logs'] + [line.strip()])[-20:]
            code = process.wait()
            report_path = output / 'latest.json'
            report = json.loads(report_path.read_text()) if report_path.exists() else None
            with self.lock:
                job = self.jobs[job_id]
                job.update(report=report, state='completed' if code == 0 else 'partial' if report else 'failed',
                           error='' if code == 0 else (report or {}).get('aborted_reason', '') or '部分資料未取得，請查看查詢狀態。',
                           finished_at=time.time())
        except Exception as exc:
            with self.lock:
                self.jobs[job_id].update(state='failed', error=str(exc), finished_at=time.time())

    def snapshot(self, job_id):
        with self.lock:
            if job_id not in self.jobs:
                return None
            job = dict(self.jobs[job_id])
        partial = Path(job['output']) / 'partial.json'
        if job['state'] == 'running' and job['module'].endswith('holiday_deals') and partial.exists():
            job['report'] = json.loads(partial.read_text())
        return {k: v for k, v in job.items() if k not in ('output', 'module')}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    @property
    def app(self):
        return self.server.app

    def trusted_host(self):
        return self.headers.get('Host') in {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}

    def send(self, status, body, content_type='application/json; charset=utf-8', filename=None):
        raw = json.dumps(body, ensure_ascii=False).encode() if isinstance(body, (dict, list)) else body
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(raw)))
        if filename:
            self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if not self.trusted_host():
            return self.send(403, {'error': '此服務只接受本機存取'})
        parsed = urlparse(self.path)
        if parsed.path in ('/', '/app.js', '/holiday.js', '/style.css'):
            name = 'index.html' if parsed.path == '/' else parsed.path[1:]
            body = (ASSETS / name).read_bytes()
            if name == 'index.html':
                body = body.replace(b'__TOKEN__', self.app.token.encode())
            kind = {'html': 'text/html', 'js': 'text/javascript', 'css': 'text/css'}[name.rsplit('.', 1)[1]]
            return self.send(200, body, kind + '; charset=utf-8')
        if parsed.path == '/api/holidays':
            return self.send(200, self.app.holidays())
        if parsed.path == '/api/deals':
            q = parse_qs(parsed.query)
            try:
                return self.send(200, self.app.deals({k: q.get(k, [''])[0] for k in ('origin', 'start', 'end')}))
            except (ValueError, argparse.ArgumentTypeError) as exc:
                return self.send(400, {'error': str(exc)})
        if parsed.path == '/api/airports':
            return self.send(200, airport_options())
        if parsed.path == '/api/watches':
            return self.send(200, self.app.watches())
        if parsed.path == '/api/config':
            return self.send(200, {'today': date.today().isoformat(), 'scheduled': False})
        if parsed.path.startswith('/api/jobs/'):
            job = self.app.snapshot(parsed.path.removeprefix('/api/jobs/'))
            return self.send(200, job) if job else self.send(404, {'error': '找不到這次查詢，可能服務已重新啟動'})
        if parsed.path == '/api/history':
            q = parse_qs(parsed.query)
            try:
                origin = airport(q.get('origin', [''])[0])
                destination = airport(q.get('destination', [''])[0])
                depart = iso_date(q['depart'][0]) if q.get('depart') else None
                flight = q.get('flight', [None])[0]
                if flight and len(flight) > 15:
                    raise ValueError('航班號過長')
                # An empty installation should render an empty state without creating data.
                report = History(self.app.db).report(origin, destination, depart, flight) if self.app.db.exists() else {
                    'origin': origin, 'destination': destination, 'series': [], 'queries': []}
                return self.send(200, report)
            except (ValueError, argparse.ArgumentTypeError) as exc:
                return self.send(400, {'error': str(exc)})
        return self.send(404, {'error': '找不到頁面'})

    def do_POST(self):
        if not self.trusted_host() or self.headers.get('X-Request-Token') != self.app.token:
            return self.send(403, {'error': '頁面驗證已過期，請重新整理'})
        origin = self.headers.get('Origin')
        if origin and origin not in {f'http://127.0.0.1:{self.server.server_port}', f'http://localhost:{self.server.server_port}'}:
            return self.send(403, {'error': '不允許其他網站啟動查詢'})
        if self.path not in ('/api/search', '/api/deals', '/api/watches', '/api/watches/remove'):
            return self.send(404, {'error': '找不到操作'})
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if size <= 0 or size > 8192:
                raise ValueError('查詢內容大小不正確')
            payload = json.loads(self.rfile.read(size))
            if not isinstance(payload, dict):
                raise ValueError('請提供 JSON 物件')
            if self.path == '/api/watches':
                return self.send(200, self.app.save_watch(payload))
            if self.path == '/api/watches/remove':
                if not isinstance(payload, dict) or not isinstance(payload.get('id'), str):
                    raise ValueError('追蹤編號格式不正確')
                return self.send(200, self.app.remove_watch(payload['id']))
            job_id = self.app.start_deals(payload) if self.path == '/api/deals' else self.app.start(payload)
            return self.send(202, {'id': job_id})
        except (ValueError, argparse.ArgumentTypeError) as exc:
            return self.send(400, {'error': str(exc)})
        except RuntimeError as exc:
            return self.send(409, {'error': str(exc)})


def main():
    p = argparse.ArgumentParser(description='啟動本機機票查詢網頁')
    p.add_argument('--port', type=int, default=8765)
    args = p.parse_args()
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    server.app = Application()
    print(f'機票查詢網頁：http://127.0.0.1:{server.server_port}（Ctrl+C 停止）', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
