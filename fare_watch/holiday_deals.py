"""連假便宜機票：對一個連假的幾種請假走法，比較多個直飛目的地的兩張單程合計。

Run: python -m fare_watch.holiday_deals TPE 2026-09-25 2026-09-28

查價、快取、被擋就停的規則全部沿用 `search.py`；這裡只多做「多個目的地 × 連假走法」的編排。
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import json
from pathlib import Path
import sys
import time

from .dgpa_calendar import Calendar, upcoming_breaks
from .fetchers import BlockedError
from .search import BrowserSource, Cache, airport, combinations, iso_date

# 預設比較的地點：桃園直飛、班次多、連假常見的短程目的地。
# 清單刻意短：每多一個地點就多 4 次查詢，一長首頁就等不到結果。想看別的地點用 --destination 單獨加。
DESTINATIONS = ('OKA', 'KIX', 'NRT', 'FUK', 'ICN', 'PUS', 'HKG', 'BKK')
SCHEMA = 1


def deal_order(deal):
    return deal['price'], deal['leave_days'], deal['destination']


def find_break(calendar_dir, start, end, today=None):
    """只接受官方日曆算出來的連假，網頁傳進來的日期不直接拿去查價。"""
    today = today or date.today()
    loaded = Calendar(calendar_dir).load(today)
    for item in upcoming_breaks(loaded['days'], today, limit=50):
        if item['start'] == start.isoformat() and item['end'] == end.isoformat():
            return item
    raise ValueError('找不到這個連假；可能日曆尚未下載，或連假已經開始')


def build(origin, holiday, results, aborted='', destinations=DESTINATIONS):
    deals, status = [], []
    for dest in destinations:
        for option in holiday['options']:
            out = results.get((origin, dest, option['depart']))
            back = results.get((dest, origin, option['return']))
            if not out or not back:
                continue
            combos = combinations(out, back)
            if combos:
                best = combos[0]
                deals.append(dict(option, destination=dest, price=best['price'],
                                  outbound=best['outbound'], inbound=best['inbound']))
    for (a, b, day), r in results.items():
        status.append({'origin': a, 'destination': b, 'date': day, 'status': r['status'],
                       'reason': r.get('reason', ''), 'fetched_at': r.get('fetched_at'),
                       'provenance': r.get('provenance')})
    requested = len(destinations) * (len({o['depart'] for o in holiday['options']}) + len({o['return'] for o in holiday['options']}))
    return {'schema': SCHEMA, 'kind': 'holiday_deals', 'origin': origin, 'holiday': holiday,
            'destinations': list(destinations), 'currency': 'TWD', 'price_type': 'two_one_way_sum',
            'generated_at': datetime.now(timezone.utc).isoformat(),
            'requested_queries': requested, 'completed_queries': len(results),
            'successful_queries': sum(r['status'] == 'ok' for r in results.values()),
            'aborted_reason': aborted,
            'deals': sorted(deals, key=deal_order),
            'queries': status}


def merge(old, new):
    """把「只查一個地點」的結果併進既有報告：使用者多加一個想去的地方，不該重跑其他 8 個。"""
    added = set(new['destinations'])
    queries = [q for q in old['queries'] if not {q['origin'], q['destination']} & added] + new['queries']
    destinations = old['destinations'] + [d for d in new['destinations'] if d not in old['destinations']]
    per_destination = new['requested_queries'] // len(new['destinations'])
    return dict(new, destinations=destinations, queries=queries,
                deals=sorted([d for d in old['deals'] if d['destination'] not in added] + new['deals'], key=deal_order),
                requested_queries=per_destination * len(destinations), completed_queries=len(queries),
                successful_queries=sum(q['status'] == 'ok' for q in queries))


def run(origin, holiday, output, history_db, refresh=False, cache_hours=6, delay=2,
        source_factory=BrowserSource, on_progress=None, destinations=DESTINATIONS):
    from .price_history import History
    output = Path(output)
    cache = Cache(output / 'cache', 0 if refresh else cache_hours)
    history = History(history_db)
    departs = sorted({o['depart'] for o in holiday['options']})
    returns = sorted({o['return'] for o in holiday['options']})
    # 出發地本身若在清單裡（例如從釜山出發）就跳過，不然會查「PUS→PUS」
    destinations = tuple(d for d in destinations if d != origin)
    jobs = [(a, b, day) for dest in destinations
            for a, b, day in [(origin, dest, d) for d in departs] + [(dest, origin, d) for d in returns]]
    results, aborted, source = {}, '', None
    try:
        for i, (a, b, day) in enumerate(jobs):
            result = cache.get(a, b, day, False)
            if result is None:
                try:
                    if source is None:
                        source = source_factory()
                        source.__enter__()
                    result = source.fetch(a, b, date.fromisoformat(day), False)
                    result['provenance'] = 'live'
                    cache.put(result, False)
                except BlockedError as exc:
                    aborted = str(exc)
                    result = {'status': 'blocked', 'reason': aborted, 'flights': [], 'provenance': 'live'}
                except Exception as exc:
                    result = {'status': 'error', 'reason': f'{type(exc).__name__}: {exc}', 'flights': [], 'provenance': 'live'}
            result.setdefault('origin', a)
            result.setdefault('destination', b)
            result.setdefault('date', day)
            result.setdefault('fetched_at', datetime.now(timezone.utc).isoformat())
            history.record(result)
            results[(a, b, day)] = result
            print(f'[{i+1}/{len(jobs)}] {day} {a}→{b} {result["status"]} ({result["provenance"]})', file=sys.stderr, flush=True)
            # 每查完一個地點（或被擋）就落地一次，網頁才能邊查邊顯示，中途被擋也不會白費
            if on_progress and (aborted or (i + 1) % (len(departs) + len(returns)) == 0):
                on_progress(build(origin, holiday, results, aborted, destinations))
            if aborted:
                break
            if result['provenance'] == 'live' and i + 1 < len(jobs):
                time.sleep(delay)
    finally:
        if source is not None:
            source.__exit__(None, None, None)
    return build(origin, holiday, results, aborted, destinations)


def write(report, output, name='latest.json'):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    temp = output / (name + '.tmp')
    temp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(output / name)


def finish(report, output):
    """進度一律寫 partial.json；latest.json 只在這輪沒被中止、或本來就沒有舊報告時才覆寫。

    重新查價中途被擋時，上一輪完整的排行是真的查到過的價格，不該被只有一兩個地點的殘缺結果蓋掉。
    """
    write(report, output, 'partial.json')
    if not report['aborted_reason'] or not (Path(output) / 'latest.json').exists():
        write(report, output)


def main(argv=None):
    p = argparse.ArgumentParser(description='連假便宜機票：依人事行政總處辦公日曆的連假，比較多個直飛目的地')
    p.add_argument('origin', type=airport)
    p.add_argument('start', type=iso_date, help='連假第一天 YYYY-MM-DD')
    p.add_argument('end', type=iso_date, help='連假最後一天 YYYY-MM-DD')
    p.add_argument('--output', default='data/deals')
    p.add_argument('--history-db', default='data/search/history.sqlite3')
    p.add_argument('--calendar-dir', default='data/holidays')
    p.add_argument('--refresh', action='store_true')
    p.add_argument('--destination', type=airport, help='只查這一個目的地，結果併進既有報告（預設查內建的熱門地點）')
    args = p.parse_args(argv)
    try:
        holiday = find_break(args.calendar_dir, args.start, args.end)
    except ValueError as exc:
        p.error(str(exc))
    previous = None
    if args.destination:
        if args.destination == args.origin:
            p.error('出發與目的機場不能相同')
        try:
            previous = json.loads((Path(args.output) / 'latest.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            pass
    combine = (lambda r: merge(previous, r)) if previous else (lambda r: r)
    report = combine(run(args.origin, holiday, args.output, args.history_db, args.refresh,
                         on_progress=lambda r: write(combine(r), args.output, 'partial.json'),
                         destinations=(args.destination,) if args.destination else DESTINATIONS))
    finish(report, args.output)
    for d in report['deals'][:10]:
        print(f'{d["destination"]} {d["depart"]}→{d["return"]} 請假{d["leave_days"]}天 {d["price"]:,}')
    return 0 if report['deals'] and not report['aborted_reason'] and report['successful_queries'] == report['requested_queries'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
