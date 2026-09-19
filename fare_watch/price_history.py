"""Append-only per-flight observations and daily price changes (SQLite, local time)."""
from __future__ import annotations

import argparse
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
import csv
import hashlib
import html
import json
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

DEFAULT_DB = 'data/search/history.sqlite3'
TAIPEI = ZoneInfo('Asia/Taipei')


def stamp():
    return datetime.now(timezone.utc).isoformat()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def identity(flight, result):
    numbers = sorted(set(flight.get('flights') or []))
    key = {'origin': result['origin'], 'destination': result['destination'],
           'depart_date': flight['depart_date'], 'currency': flight.get('currency', 'TWD'),
           'source': result.get('source', 'google_flights_browser'),
           'passengers': 1, 'cabin': 'economy', 'price_type': 'one_way', 'nonstop': True}
    if numbers:
        key['flights'] = numbers
        quality = 'flight_number'
    else:
        # Never imply that a time-only match is a verified flight-number match.
        key.update(airline=flight['airline'], depart_time=flight['depart_time'],
                   arrive_date=flight['arrive_date'], arrive_time=flight['arrive_time'])
        quality = 'schedule_only'
    return digest(key), quality


class History:
    def __init__(self, path=DEFAULT_DB):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS queries (
                    id TEXT PRIMARY KEY, origin TEXT NOT NULL, destination TEXT NOT NULL,
                    travel_date TEXT NOT NULL, fetched_at TEXT NOT NULL, observed_on TEXT NOT NULL,
                    source TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL, url TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS observations (
                    query_id TEXT NOT NULL REFERENCES queries(id), series_id TEXT NOT NULL,
                    identity_quality TEXT NOT NULL, airline TEXT NOT NULL, flights TEXT NOT NULL,
                    depart_date TEXT NOT NULL, depart_time TEXT NOT NULL,
                    arrive_date TEXT NOT NULL, arrive_time TEXT NOT NULL,
                    price INTEGER, currency TEXT NOT NULL, status TEXT NOT NULL,
                    PRIMARY KEY(query_id, series_id)
                );
                CREATE INDEX IF NOT EXISTS query_route ON queries(origin, destination, travel_date, fetched_at);
                CREATE INDEX IF NOT EXISTS observation_series ON observations(series_id);
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def record(self, result):
        if result.get('provenance') != 'live' or result.get('status') == 'not_fetched':
            return 0
        fetched = datetime.fromisoformat(result['fetched_at'])
        if fetched.tzinfo is None:
            raise ValueError('歷史資料必須包含報價時區')
        fetched_at = fetched.astimezone(timezone.utc).isoformat()
        source = result.get('source', 'google_flights_browser')
        # Same response imported twice or read from cache is not a fresh observation.
        query_id = digest([result['origin'], result['destination'], result['date'], fetched_at, source])
        with self.connect() as db:
            inserted = db.execute('INSERT OR IGNORE INTO queries VALUES (?,?,?,?,?,?,?,?,?,?)', (
                query_id, result['origin'], result['destination'], result['date'], fetched_at,
                fetched.astimezone(TAIPEI).date().isoformat(), source,
                result['status'], result.get('reason', ''), result.get('url', ''))).rowcount
            if not inserted:
                return 0
            for f in result.get('flights', []):
                series_id, quality = identity(f, result)
                price = f.get('price')
                if price is not None and (not isinstance(price, int) or isinstance(price, bool) or price <= 0):
                    raise ValueError('歷史價格必須為正整數或空值')
                db.execute('INSERT OR IGNORE INTO observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', (
                    query_id, series_id, quality, f['airline'], canonical(f.get('flights', [])),
                    f['depart_date'], f['depart_time'], f['arrive_date'], f['arrive_time'],
                    price, f.get('currency', 'TWD'), 'quoted' if price is not None else 'schedule_only'))
        return 1

    def import_reports(self, directory):
        count = 0
        # Only the general search's dated reports; never mix older round-trip totals.
        for path in sorted(Path(directory).glob('*.json')):
            if path.name in ('latest.json', 'history.json'):
                continue
            try:
                report = json.loads(path.read_text(encoding='utf-8'))
            except (ValueError, OSError):
                continue
            if not isinstance(report, dict) or report.get('schema') not in (1, 2) or report.get('price_type') not in ('one_way', 'two_one_way_sum'):
                continue
            for day in report.get('dates', []):
                for side in ('outbound', 'inbound'):
                    result = day.get(side)
                    if result and all(k in result for k in ('origin', 'destination', 'date', 'fetched_at')):
                        count += self.record(result)
        return count

    def report(self, origin, destination, depart=None, flight=None, since=None):
        where, values = ['q.origin=?', 'q.destination=?'], [origin, destination]
        if depart:
            where.append('q.travel_date=?')
            values.append(str(depart))
        if since:
            where.append('q.observed_on>=?')
            values.append(str(since))
        clause = ' AND '.join(where)
        with self.connect() as db:
            queries = [dict(r) for r in db.execute(f'SELECT q.* FROM queries q WHERE {clause} ORDER BY fetched_at', values)]
            observations = [dict(r) for r in db.execute(f'''
                SELECT o.*, q.origin, q.destination, q.fetched_at, q.observed_on, q.source,
                       q.status AS query_status FROM observations o JOIN queries q ON q.id=o.query_id
                WHERE {clause} ORDER BY q.fetched_at, o.series_id''', values)]
        groups = defaultdict(list)
        for row in observations:
            row['flights'] = json.loads(row['flights'])
            if flight and flight.upper().replace(' ', '') not in row['flights']:
                continue
            groups[row['series_id']].append(row)
        series = []
        for series_id, rows in groups.items():
            available = [r for r in rows if r['price'] is not None]
            last = rows[-1]
            previous = next((r for r in reversed(rows[:-1]) if r['price'] is not None), None)
            delta = last['price'] - previous['price'] if last['price'] is not None and previous else None
            days = defaultdict(list)
            for row in rows:
                days[row['observed_on']].append(row)
            daily = []
            previous_day_price = None
            for day, day_rows in sorted(days.items()):
                prices = [r['price'] for r in day_rows if r['price'] is not None]
                daily.append({'date': day, 'last_price': day_rows[-1]['price'],
                              'min_price': min(prices) if prices else None,
                              'max_price': max(prices) if prices else None, 'samples': len(day_rows),
                              'last_fetched_at': day_rows[-1]['fetched_at'],
                              'change_from_previous_quoted_day': day_rows[-1]['price'] - previous_day_price
                              if day_rows[-1]['price'] is not None and previous_day_price is not None else None})
                if day_rows[-1]['price'] is not None:
                    previous_day_price = day_rows[-1]['price']
            latest_query = next((q for q in reversed(queries) if q['travel_date'] == last['depart_date'] and q['source'] == last['source']), None)
            latest_status = last['status']
            if latest_query and latest_query['id'] != last['query_id']:
                latest_status = 'not_returned' if latest_query['status'] == 'ok' else 'query_failed_or_partial'
                if any(o['query_id'] == latest_query['id'] and o['identity_quality'] == 'schedule_only'
                       and o['airline'] == last['airline'] for o in observations):
                    latest_status = 'identity_unconfirmed'
            series.append({'series_id': series_id, 'identity_quality': last['identity_quality'],
                           'airline': last['airline'], 'flights': last['flights'], 'depart_date': last['depart_date'],
                           'currency': last['currency'], 'latest_status': latest_status,
                           'latest_observed_price': last['price'], 'latest_fetched_at': last['fetched_at'],
                           'previous_quoted_price': previous['price'] if previous else None,
                           'change': delta, 'change_percent': round(delta / previous['price'] * 100, 2) if delta is not None else None,
                           'lowest': min(r['price'] for r in available) if available else None,
                           'highest': max(r['price'] for r in available) if available else None,
                           'sample_count': len(rows), 'daily': daily, 'observations': rows})
        return {'origin': origin, 'destination': destination, 'timezone': 'Asia/Taipei',
                'generated_at': stamp(), 'queries': queries,
                'series': sorted(series, key=lambda s: (s['depart_date'], s['flights'] or [s['airline']]))}


def amount(value):
    return '—' if value is None else f'{value:,}'


def markdown(report):
    from .search import AIRLINES
    lines = [f'# {report["origin"]} → {report["destination"]} 航班票價歷史', '',
             '依搭乘日期＋航班號追蹤；報價日以台灣時間計算。金額為每位成人經濟艙單程起價（TWD）。',
             '只記錄實際連網觀測；快取不算新報價。同一天每次重查均保留，日表另列當日最後價／最低／最高。',
             '無報價、未出現在結果中或查詢失敗不代表售罄，也不算降到零元。票規、行李與賣方未核對，變價不一定是相同票種變價。', '',
             '| 搭乘日／航班 | 最近一次價格 | 較前次有價觀測 | 歷史最低 | 歷史最高 | 觀測數 | 最新狀態 |',
             '|---|---:|---:|---:|---:|---:|---|']
    for s in report['series']:
        label = '/'.join(s['flights']) or f'{s["airline"]}（依時刻辨識）'
        delta = '—' if s['change'] is None else f'{s["change"]:+,}（{s["change_percent"]:+.2f}%）'
        lines.append(f'| {s["depart_date"]} {label} | {amount(s["latest_observed_price"])} | {delta} | {amount(s["lowest"])} | {amount(s["highest"])} | {s["sample_count"]} | {s["latest_status"]} |')
    if not report['series']:
        lines += ['', '尚無符合條件的航班觀測。']
    for s in report['series']:
        label = '/'.join(s['flights']) or '航班號未取得（只依航空公司與時刻辨識）'
        lines += ['', f'## {s["depart_date"]} {AIRLINES.get(s["airline"], s["airline"])} {label}', '',
                  '| 報價日（台灣） | 當日最後價 | 較前個有價日 | 當日最低 | 當日最高 | 次數 |', '|---|---:|---:|---:|---:|---:|']
        for d in s['daily']:
            daily_delta = '—' if d['change_from_previous_quoted_day'] is None else f"{d['change_from_previous_quoted_day']:+,}"
            lines.append(f'| {d["date"]} | {amount(d["last_price"])} | {daily_delta} | {amount(d["min_price"])} | {amount(d["max_price"])} | {d["samples"]} |')
        lines += ['', '| 實際查詢時間（台灣） | 價格 | 起飛 → 抵達 |', '|---|---:|---|']
        for r in s['observations']:
            when = datetime.fromisoformat(r['fetched_at']).astimezone(TAIPEI).strftime('%Y-%m-%d %H:%M:%S')
            lines.append(f'| {when} | {amount(r["price"])} | {r["depart_time"]} → {r["arrive_date"]} {r["arrive_time"]} |')
    lines += ['', '## 查詢紀錄（包含失敗）', '', '| 報價日 | 搭乘日 | 查詢狀態 | 說明 |', '|---|---|---|---|']
    for q in report['queries']:
        lines.append(f'| {q["observed_on"]} | {q["travel_date"]} | {q["status"]} | {q["reason"].replace(chr(10), " ").replace("|", "/")} |')
    return '\n'.join(lines) + '\n'


def html_report(report):
    """Standalone HTML/SVG charts; no remote scripts, no interpolation across missing prices."""
    cards = []
    for s in report['series']:
        points = s['daily']
        prices = [p['last_price'] for p in points if p['last_price'] is not None]
        chart = ''
        if prices:
            lo, hi = min(prices), max(prices)
            def xy(i, price):
                offset = (datetime.fromisoformat(points[i]['date']) - datetime.fromisoformat(points[0]['date'])).days
                span = (datetime.fromisoformat(points[-1]['date']) - datetime.fromisoformat(points[0]['date'])).days
                return (55 + offset * 680 / max(span, 1), 165 - (price - lo) * 120 / max(hi - lo, 1))
            marks = []
            previous = None
            for i, p in enumerate(points):
                if p['last_price'] is None:
                    previous = None
                    continue
                x, y = xy(i, p['last_price'])
                if previous:
                    px, py, prev_day = previous
                    gap = (datetime.fromisoformat(p['date']) - datetime.fromisoformat(prev_day)).days
                    if gap == 1:
                        marks.append(f'<line x1="{px}" y1="{py}" x2="{x}" y2="{y}" stroke="#2563eb" stroke-width="2"/>')
                title = html.escape(f'{p["date"]} 最後價 {p["last_price"]:,} TWD；{p["samples"]} 次觀測')
                marks.append(f'<circle cx="{x}" cy="{y}" r="5" fill="#2563eb"><title>{title}</title></circle>')
                previous = x, y, p['date']
            chart = (f'<svg viewBox="0 0 800 210" role="img" aria-label="每日最後報價走勢">'
                     f'<text x="0" y="40">{hi:,}</text><text x="0" y="180">{lo:,}</text>' + ''.join(marks) +
                     f'<text x="55" y="202">{points[0]["date"]}</text><text x="650" y="202">{points[-1]["date"]}</text></svg>')
        title = html.escape(f'{s["depart_date"]} {s["airline"]} {"/".join(s["flights"]) or "依時刻辨識"}')
        rows = ''.join(f'<tr><td>{p["date"]}</td><td>{amount(p["last_price"])}</td><td>{amount(p["min_price"])}</td><td>{amount(p["max_price"])}</td><td>{p["samples"]}</td></tr>' for p in points)
        cards.append(f'<section><h2>{title}</h2><p>最新狀態：{s["latest_status"]} · 歷史最低 {amount(s["lowest"])} · 最高 {amount(s["highest"])}</p>{chart}<table><tr><th>報價日</th><th>最後價</th><th>最低</th><th>最高</th><th>次數</th></tr>{rows}</table></section>')
    return ('<!doctype html><html lang="zh-Hant"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>機票報價歷史</title><style>body{font:16px system-ui;max-width:1000px;margin:40px auto;padding:0 20px;background:#f5f7fb;color:#142238}section{background:white;padding:24px;border-radius:14px;margin:24px 0}table{border-collapse:collapse;width:100%}td,th{text-align:left;padding:10px;border-bottom:1px solid #ddd}svg{width:100%;max-width:800px}svg text{font-size:13px;fill:#475569}</style>'
            f'<h1>{html.escape(report["origin"])} → {html.escape(report["destination"])} 每日票價變化</h1>'
            '<p>台灣時間 · 每人單程起價（TWD） · 每個搭乘日、航班獨立追蹤。</p><p>滑鼠移到圓點可看報價。一天內變動保留在 CSV／Markdown；每日圖使用當天最後價。缺資料不連線，只有一天時只顯示一個點。</p>'
            '<p>快取不新增觀測；無價格不等於零元或售罄。未核對行李、賣方及票規，因此這是該航班可見起價變動。</p>' + ''.join(cards) + '</html>')


def export(report, directory, prefix='history'):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    base = directory / prefix
    base.with_suffix('.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    base.with_suffix('.md').write_text(markdown(report), encoding='utf-8')
    base.with_suffix('.html').write_text(html_report(report), encoding='utf-8')
    fields = ['observed_on', 'fetched_at', 'origin', 'destination', 'depart_date', 'flights', 'airline',
              'depart_time', 'arrive_date', 'arrive_time', 'price', 'currency', 'status', 'identity_quality']
    with base.with_suffix('.csv').open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        for s in report['series']:
            for r in s['observations']:
                writer.writerow(dict(r, flights='/'.join(r['flights'])))
    return base.with_suffix('.html')


def main(argv=None):
    from .search import airport, iso_date
    p = argparse.ArgumentParser(description='查看同一航班的每日歷史報價，不連網查價')
    p.add_argument('origin', type=airport)
    p.add_argument('destination', type=airport)
    p.add_argument('--depart', type=iso_date, help='搭乘日期')
    p.add_argument('--flight', help='航班號，例如 IT606')
    p.add_argument('--since', type=iso_date, help='報價日期起點，以台灣日期計')
    p.add_argument('--db', default=DEFAULT_DB)
    p.add_argument('--output', default='data/search')
    p.add_argument('--import-reports', action='append', default=[], help='匯入既有通用查價報告目錄，可重複指定')
    args = p.parse_args(argv)
    history = History(args.db)
    imported = sum(history.import_reports(path) for path in args.import_reports)
    report = history.report(args.origin, args.destination, args.depart, args.flight, args.since)
    target = export(report, args.output, f'history-{args.origin}-{args.destination}')
    print(markdown(report))
    print(f'匯入 {imported} 筆查詢；走勢圖：{target}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
