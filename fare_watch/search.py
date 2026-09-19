"""General route/date search. Prices are ONE-WAY quotes, never round-trip estimates.

Run: python -m fare_watch.search 台北 釜山 2027-03-11 2027-03-15
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlencode

from .fetchers import BlockedError
from .playwright_fetcher import _check_response, _raise_if_blocked, _to_hhmm

ALIASES = {
    '台北': 'TPE', '臺北': 'TPE', '桃園': 'TPE', '台北桃園': 'TPE',
    '松山': 'TSA', '台北松山': 'TSA', '高雄': 'KHH', '台中': 'RMQ',
    '釜山': 'PUS', '首爾': 'ICN', '仁川': 'ICN', '金浦': 'GMP', '濟州': 'CJU',
    '成田': 'NRT', '羽田': 'HND', '大阪': 'KIX', '關西': 'KIX',
    '福岡': 'FUK', '沖繩': 'OKA', '札幌': 'CTS', '香港': 'HKG',
    '澳門': 'MFM', '新加坡': 'SIN', '曼谷': 'BKK', '吉隆坡': 'KUL',
}
AIRLINES = {'Tigerair Taiwan': '台灣虎航', 'EASTAR JET': '易斯達航空',
            'Jeju Air': '濟州航空', 'Air Busan': '釜山航空', 'Korean Air': '大韓航空',
            'Jin Air': '真航空', 'China Airlines': '中華航空', 'EVA Air': '長榮航空',
            'STARLUX Airlines': '星宇航空'}
MONTHS = {name: i for i, name in enumerate(
    ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August',
     'September', 'October', 'November', 'December'], 1)}
LABEL_RE = re.compile(
    r'Nonstop flight with (?P<airline>.+?)\. Leaves (?P<origin>.+?) at '
    r'(?P<depart>\d{1,2}:\d{2} [AP]M) on \w+, (?P<dm>\w+) (?P<dd>\d{1,2})'
    r' and arrives at (?P<destination>.+?) at (?P<arrive>\d{1,2}:\d{2} [AP]M)'
    r' on \w+, (?P<am>\w+) (?P<ad>\d{1,2})\.')
FLIGHT_RE = re.compile(r'^(?!AM\b|PM\b)([A-Z0-9]{2})\s+(\d{1,4})$')
SCHEMA = 2


def airport(value):
    value = value.strip()
    if value in ALIASES:
        return ALIASES[value]
    if re.fullmatch('[A-Za-z]{3}', value):
        return value.upper()
    raise argparse.ArgumentTypeError(f'無法辨識機場「{value}」，請輸入三碼代碼，例如 TPE、PUS；東京請指定 NRT 或 HND')


def iso_date(value):
    try:
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            raise ValueError()
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError('日期請用 YYYY-MM-DD')


def query_url(origin, destination, day):
    return 'https://www.google.com/travel/flights?' + urlencode({
        'q': f'One way nonstop flights from {origin} to {destination} on {day}',
        'curr': 'TWD', 'hl': 'en', 'gl': 'TW'})


def parse_label(label, day, origin, destination, detail=''):
    """Use accessibility's explicit dates, not duration/timezone guesses or fare totals."""
    m = LABEL_RE.search(label)
    if not m:
        return None
    g = m.groupdict()
    if MONTHS.get(g['dm']) != day.month or int(g['dd']) != day.day:
        return None
    # Google does not put years in labels. Bound arrival to +/- 2 calendar days,
    # including westbound date-line flights and December/January crossings.
    arrivals = [day + timedelta(days=i) for i in range(-2, 4)]
    arrival = next((d for d in arrivals if d.month == MONTHS.get(g['am']) and d.day == int(g['ad'])), None)
    if arrival is None:
        return None
    price_match = re.match(r'From ([\d,]+) New Taiwan dollars\.', label)
    price = int(price_match[1].replace(',', '')) if price_match else None
    if price is not None and price <= 0:
        price = None
    flights = []
    for line in detail.splitlines():
        match = FLIGHT_RE.fullmatch(line.strip())
        if match and match[1] + match[2] not in flights:
            flights.append(match[1] + match[2])
    return {'origin': origin, 'destination': destination,
            'origin_name': g['origin'], 'destination_name': g['destination'],
            'depart_date': day.isoformat(), 'arrive_date': arrival.isoformat(),
            'depart_time': _to_hhmm(g['depart']), 'arrive_time': _to_hhmm(g['arrive']),
            'airline': g['airline'], 'flights': flights, 'price': price,
            'currency': 'TWD', 'price_type': 'one_way',
            'status': 'quoted' if price is not None else 'schedule_only'}


def deduplicate(flights):
    unique = {}
    for f in flights:
        key = (f['airline'], f['depart_date'], f['depart_time'], f['arrive_date'], f['arrive_time'])
        previous = unique.get(key)
        if previous is None or (previous['price'] is None and f['price'] is not None):
            unique[key] = f
    return sorted(unique.values(), key=lambda f: (f['depart_time'], f['airline']))


class BrowserSource:
    """Reuse Chromium between dates; inspect all visible flight cards, including unpriced ones."""
    def __enter__(self):
        from playwright.sync_api import sync_playwright
        self.pw = sync_playwright().start()
        try:
            self.browser = self.pw.chromium.launch(headless=True)
            self.context = self.browser.new_context(locale='en-US', timezone_id='Asia/Taipei')
            self.page = self.context.new_page()
            self.page.set_default_timeout(10000)
        except Exception:
            self.pw.stop()
            raise
        return self

    def __exit__(self, *args):
        self.browser.close()
        self.pw.stop()

    def fetch(self, origin, destination, day, details=False):
        page = self.page
        url = query_url(origin, destination, day)
        response = page.goto(url, wait_until='domcontentloaded', timeout=45000)
        _check_response(response, [])
        _raise_if_blocked(page.content(), page.url)
        # Wait for the full set of accessible flight labels to stabilize.
        # Old code stopped on card count
        # alone, before Eastar/Starlux arrived in the second response batch.
        stable, last = 0, None
        labels = []
        settled = False
        for attempt in range(45):
            _raise_if_blocked(page.content(), page.url)
            labels = page.get_by_role('link').evaluate_all(
                "els => els.filter(e => e.getClientRects().length).map(e => e.getAttribute('aria-label') || '').filter(s => s.includes('Nonstop flight with '))")
            body = page.locator('body').inner_text()
            # Loading live-region text can remain in the DOM indefinitely.
            # Require stable full labels (including fares), not that stale text.
            stable = stable + 1 if labels == last else 0
            last = labels
            if attempt >= 10 and stable >= 6 and (labels or re.search(r'No (?:flights|results)|couldn.t find any flights', body, re.I)):
                settled = True
                break
            page.wait_for_timeout(1000)
        more = page.get_by_role('button', name='View more flights', exact=True)
        expanded_more = 0
        while more.count() and more.first.is_visible() and expanded_more < 10:
            more.first.click()
            page.wait_for_timeout(1000)
            _raise_if_blocked(page.content(), page.url)
            expanded_more += 1
        truncated = bool(more.count() and more.first.is_visible())
        # Validate selected UI, not merely the requested URL.
        one_way = page.get_by_role('combobox', name='Change ticket type. One way').count() > 0
        nonstop = page.get_by_role('button', name='Nonstop, Stops, Selected', exact=True).count() > 0
        from_ok = page.get_by_role('combobox', name=re.compile(r'Where from\?.*\b' + origin + r'\b')).count() > 0
        to_ok = page.get_by_role('combobox', name=re.compile(r'Where to\?.*\b' + destination + r'\b')).count() > 0
        if not (one_way and nonstop and from_ok and to_ok):
            raise ValueError('搜尋頁的單程／直飛／機場條件與請求不符，未採用報價')
        links = page.get_by_role('link').evaluate_all(
            "els => els.filter(e => e.getClientRects().length).map(e => e.getAttribute('aria-label') || '').filter(s => s.includes('Nonstop flight with '))")
        flights = []
        errors = []
        for label in dict.fromkeys(links):
            detail = ''
            if details:
                try:
                    link = page.get_by_role('link', name=label, exact=True).first
                    card = link.locator('..')
                    toggle = card.get_by_role('button', name=re.compile(r'^Flight details\.')).first
                    if toggle.get_attribute('aria-expanded') != 'true':
                        toggle.click()
                    detail = '\n'.join(card.locator('span').all_text_contents())
                except Exception as exc:
                    errors.append(f'航班明細未展開：{type(exc).__name__}')
            parsed = parse_label(label, day, origin, destination, detail)
            if parsed:
                if details and not parsed['flights']:
                    errors.append('一筆航班號未取得')
                flights.append(parsed)
            else:
                errors.append('一筆航班的日期或文字無法解析')
        _raise_if_blocked(page.content(), page.url)
        complete = settled and not truncated and not errors
        status = 'ok' if flights and complete else 'partial' if flights else 'unavailable'
        return {'origin': origin, 'destination': destination, 'date': day.isoformat(),
                'url': url, 'fetched_at': datetime.now(timezone.utc).isoformat(),
                'source': 'google_flights_browser', 'status': status,
                'coverage': 'visible_source_results' if complete else 'partial',
                'reason': '; '.join(errors + ([] if settled else ['結果載入未穩定']) +
                                    (['仍有未展開結果'] if truncated else [])),
                'flights': deduplicate(flights), 'details_requested': details,
                'raw_labels': links}


class Cache:
    def __init__(self, directory, hours=6):
        self.directory = Path(directory)
        self.hours = hours

    def path(self, origin, destination, day, details):
        key = f'{SCHEMA}:{origin}:{destination}:{day}:TWD:1:nonstop:one_way:{details}'
        return self.directory / (hashlib.sha256(key.encode()).hexdigest() + '.json')

    def get(self, origin, destination, day, details):
        if self.hours <= 0:
            return None
        try:
            result = json.loads(self.path(origin, destination, day, details).read_text())
            age = datetime.now(timezone.utc) - datetime.fromisoformat(result['fetched_at'])
            if not timedelta(0) <= age <= timedelta(hours=self.hours) or result['status'] != 'ok':
                return None
            return dict(result, provenance='cache')
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def put(self, result, details):
        if result['status'] != 'ok':
            return
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.path(result['origin'], result['destination'], result['date'], details)
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        tmp.replace(path)


def priced(result):
    return [f for f in result.get('flights', []) if f['price'] is not None]


def combinations(outbound, inbound):
    return sorted(({'outbound': a, 'inbound': b, 'price': a['price'] + b['price'],
                    'currency': 'TWD', 'price_type': 'two_one_way_sum'}
                   for a in priced(outbound) for b in priced(inbound)), key=lambda c: c['price'])


def plan(args, today=None):
    today = today or date.today()
    if args.origin == args.destination:
        raise ValueError('出發與目的機場不能相同')
    if args.return_date and not args.depart:
        raise ValueError('回程日期必須搭配出發日期')
    if args.depart and (args.start or args.end):
        raise ValueError('指定日期與掃描區間不能同時使用')
    if args.end and not args.start:
        raise ValueError('--end 必須搭配 --start')
    if not 1 <= args.days <= 180 or not 1 <= args.nights <= 60 or args.top < 1:
        raise ValueError('--days 限 1～180、--nights 限 1～60，--top 必須大於 0')
    if args.depart:
        if args.depart < today or (args.return_date and args.return_date <= args.depart):
            raise ValueError('出發日不能是過去，回程必須晚於出發日')
        return [(args.depart, args.return_date)]
    start = args.start or today + timedelta(days=1)
    end = args.end or start + timedelta(days=args.days - 1)
    if start < today or end < start or (end - start).days >= 180:
        raise ValueError('掃描區間須為今日以後、起迄順序正確且不超過 180 天')
    return [(start + timedelta(days=i), start + timedelta(days=i + args.nights))
            for i in range((end - start).days + 1)]


def run(args, source_factory=BrowserSource, today=None):
    pairs = plan(args, today)
    details = bool(args.depart) or getattr(args, "details", False)
    cache = Cache(Path(args.output) / 'cache', 0 if args.refresh else args.cache_hours)
    jobs = list(dict.fromkeys((a, b, d) for depart, ret in pairs
                             for a, b, d in [(args.origin, args.destination, depart)] +
                             ([(args.destination, args.origin, ret)] if ret else [])))
    from .price_history import History, DEFAULT_DB
    history = History(getattr(args, "history_db", DEFAULT_DB))
    results = {}
    aborted = ''
    source = None
    entered = False
    try:
        for i, (origin, dest, day) in enumerate(jobs):
            result = cache.get(origin, dest, day, details)
            if result is None:
                try:
                    if source is None:
                        source = source_factory()
                        source.__enter__()
                        entered = True
                    result = source.fetch(origin, dest, day, details)
                    result['provenance'] = 'live'
                    cache.put(result, details)
                except BlockedError as exc:
                    aborted = str(exc)
                    result = {'status': 'blocked', 'reason': aborted, 'flights': [], 'provenance': 'live'}
                except Exception as exc:
                    if not entered:
                        aborted = f'無法啟動查詢來源：{exc}'
                    result = {'status': 'error', 'reason': f'{type(exc).__name__}: {exc}', 'flights': [], 'provenance': 'live'}
            result.setdefault('origin', origin)
            result.setdefault('destination', dest)
            result.setdefault('date', day.isoformat())
            result.setdefault('fetched_at', datetime.now(timezone.utc).isoformat())
            result.setdefault('source', 'google_flights_browser')
            history.record(result)
            results[(origin, dest, day)] = result
            print(f'[{i+1}/{len(jobs)}] {day} {origin}→{dest} {result["status"]} ({result["provenance"]})', file=sys.stderr)
            if aborted:
                break
            if result['provenance'] == 'live' and i + 1 < len(jobs):
                time.sleep(args.delay)
    finally:
        if entered:
            source.__exit__(None, None, None)
    rows = []
    for depart, ret in pairs:
        out = results.get((args.origin, args.destination, depart), {'status': 'not_fetched', 'flights': []})
        inbound = results.get((args.destination, args.origin, ret), {'status': 'not_fetched', 'flights': []}) if ret else None
        combos = combinations(out, inbound) if inbound else []
        single = sorted(priced(out), key=lambda f: f['price'])
        minimum = combos[0]['price'] if combos else single[0]['price'] if not ret and single else None
        rows.append({'depart': depart.isoformat(), 'return': ret.isoformat() if ret else None,
                     'min_price': minimum, 'outbound': out, 'inbound': inbound,
                     'combination_count': len(combos), 'combinations': combos if details else combos[:1]})
    ranked = sorted((r for r in rows if r['min_price'] is not None), key=lambda r: (r['min_price'], r['depart']))
    return {'schema': SCHEMA, 'origin': args.origin, 'destination': args.destination,
            'mode': 'dates' if args.depart else 'scan', 'currency': 'TWD', 'passengers': 1,
            'price_type': 'two_one_way_sum' if pairs[0][1] else 'one_way',
            'generated_at': datetime.now(timezone.utc).isoformat(),
            'requested_queries': len(jobs), 'completed_queries': len(results),
            'successful_queries': sum(r['status'] == 'ok' for r in results.values()),
            'aborted_reason': aborted, 'ranked': ranked, 'dates': rows}


def flight_text(f):
    airline = AIRLINES.get(f['airline'], f['airline'])
    number = '/'.join(f['flights']) or '航班號未取得'
    overnight = f'（{f["arrive_date"]}）' if f['arrive_date'] != f['depart_date'] else ''
    return f'{airline} {number} {f["depart_time"]} → {f["arrive_time"]}{overnight}'


def markdown(report, top=10):
    lines = [f'# {report["origin"]} → {report["destination"]} 機票查詢', '',
             '每位成人／經濟艙／直飛／TWD。時間皆為機場當地時間。',
             '**來回比較是兩張單程報價相加，不是航空公司來回票報價；行李與票規尚未核對。**',
             '只涵蓋本次來源可見結果，不能保證全市場完整；無票價的班次仍保留。',
             f'產生時間：{report["generated_at"]}',
             f'查詢進度：{report["completed_queries"]}/{report["requested_queries"]}；完整成功 {report["successful_queries"]}。', '']
    if report['aborted_reason']:
        lines += [f'本輪停止原因：{report["aborted_reason"]}', '']
    if report['mode'] == 'scan':
        lines += ['## 已查到的低價日期', '', '| 出發 | 回程 | 每人兩張單程合計 |', '|---|---|---:|']
        for row in report['ranked'][:top]:
            lines.append(f'| {row["depart"]} | {row["return"]} | {row["min_price"]:,} |')
    else:
        row = report['dates'][0]
        for label, result in [('去程', row['outbound']), ('回程', row['inbound'])]:
            if result is None:
                continue
            lines += ['', f'## {label}', '', f'狀態：{result["status"]}；{result.get("reason", "")}',
                      f'查詢時間：{result.get("fetched_at", "未取得")}；來源：{result.get("provenance", "未查詢")}',
                      f'[開啟查詢]({result.get("url", "")})', '', '| 航班與時間 | 單程起價 |', '|---|---:|']
            for f in result['flights']:
                price = f'{f["price"]:,}' if f['price'] is not None else '無報價（僅班次資訊）'
                lines.append(f'| {flight_text(f)} | {price} |')
        if row['inbound']:
            lines += ['', f'## 最低價組合（共 {row["combination_count"]} 組；顯示前 {top} 組）', '',
                      '| 去程 | 回程 | 兩張單程合計 |', '|---|---|---:|']
            for c in row['combinations'][:top]:
                lines.append(f'| {flight_text(c["outbound"])} | {flight_text(c["inbound"])} | {c["price"]:,} |')
    if not report['ranked']:
        lines += ['', '本次沒有足夠的有效報價可排名，不代表航線沒有航班。']
    if report['mode'] == 'scan':
        lines += ['', '## 每天查詢狀態', '', '| 出發 | 去程／回程狀態 | 報價時間（UTC） |', '|---|---|---|']
        for r in report['dates']:
            out, inc = r['outbound'], r['inbound'] or {}
            lines.append(f'| {r["depart"]} | {out["status"]}／{inc.get("status", "—")} | {out.get("fetched_at", "—")}／{inc.get("fetched_at", "—")} |')
    return '\n'.join(lines) + '\n'


def parser():
    p = argparse.ArgumentParser(description='任意航線直飛查價：指定日期或掃描低價日期（每人單程價）')
    p.add_argument('origin', type=airport, help='出發機場，例如 台北 或 TPE')
    p.add_argument('destination', type=airport, help='目的機場，例如 釜山 或 PUS')
    p.add_argument('depart', nargs='?', type=iso_date, help='YYYY-MM-DD；省略則掃描日期')
    p.add_argument('return_date', nargs='?', type=iso_date, help='回程 YYYY-MM-DD；只給出發日期則查單程')
    p.add_argument('--start', type=iso_date, help='低價掃描開始日，預設明天')
    p.add_argument('--end', type=iso_date, help='低價掃描最後出發日（含當日）')
    p.add_argument('--days', type=int, default=30, help='掃描幾個出發日，預設 30')
    p.add_argument('--nights', type=int, default=4, help='停留幾晚，預設 4（5 天 4 夜）')
    p.add_argument('--top', type=int, default=10, help='顯示前幾個日期或組合；JSON 保留指定日期的全部組合')
    p.add_argument('--cache-hours', type=float, default=6, help='快取有效時數，預設 6')
    p.add_argument('--refresh', action='store_true', help='忽略快取，重新連線查詢')
    p.add_argument('--delay', type=float, default=2, help='每次連網查詢之間間隔秒數，預設 2')
    p.add_argument('--output', default='data/search', help='報告與快取目錄')
    p.add_argument('--history-db', default='data/search/history.sqlite3', help='永久報價歷史資料庫，與快取分開')
    p.add_argument('--details', action='store_true', help='掃描模式也抓航班號，以精確追蹤同一航班（較慢）')
    p.add_argument('--json', action='store_true', help='stdout 輸出 JSON')
    p.add_argument('--dry-run', action='store_true', help='只列查詢日期與連結，不連網不寫檔')
    return p


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == 'history':
        from .price_history import main as history_main
        return history_main(argv[1:])
    p = parser()
    args = p.parse_args(argv)
    try:
        pairs = plan(args)
        if not all(math.isfinite(v) and v >= 0 for v in (args.delay, args.cache_hours)):
            raise ValueError('間隔與快取時數必須是非負有限數字')
    except ValueError as exc:
        p.error(str(exc))
    if args.dry_run:
        print(json.dumps([{'depart': str(d), 'return': str(r) if r else None,
                           'outbound_url': query_url(args.origin, args.destination, d),
                           'inbound_url': query_url(args.destination, args.origin, r) if r else None}
                          for d, r in pairs], ensure_ascii=False, indent=2))
        return 0
    print(f'{args.origin} → {args.destination}；{len(pairs)} 個出發日；每人直飛單程報價。首次掃描可能需要數分鐘。', file=sys.stderr)
    report = run(args)
    directory = Path(args.output)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    stem = directory / f'{args.origin}-{args.destination}-{stamp}'
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    md = markdown(report, args.top)
    stem.with_suffix('.json').write_text(payload, encoding='utf-8')
    stem.with_suffix('.md').write_text(md, encoding='utf-8')
    (directory / 'latest.json').write_text(payload, encoding='utf-8')
    (directory / 'latest.md').write_text(md, encoding='utf-8')
    from .price_history import History, export
    history = History(args.history_db)
    for origin, dest in [(args.origin, args.destination)] + ([(args.destination, args.origin)] if args.return_date or not args.depart else []):
        export(history.report(origin, dest), directory, f'history-{origin}-{dest}')
    print(payload if args.json else md)
    print(f'報告：{stem.with_suffix(".md")}', file=sys.stderr)
    return 0 if report['ranked'] and not report['aborted_reason'] and report['successful_queries'] == report['requested_queries'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
