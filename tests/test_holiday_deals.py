"""連假查價的編排：查價來源用替身，不連網。"""
from datetime import date, datetime, timezone
import json
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from fare_watch.dgpa_calendar import Calendar, upcoming_breaks
from fare_watch.fetchers import BlockedError
from fare_watch.holiday_deals import DESTINATIONS, finish, merge, run
from fare_watch.web_server import Application, Handler, ThreadingHTTPServer
from test_dgpa_calendar import days_of, fake_fetch

HOLIDAY = upcoming_breaks(days_of(2026, 2027), date(2026, 9, 19))[0]
# 去程連假當天比前一天貴、回程連假最後一天比隔天貴：請假才便宜，排序要反映出來
PRICES = {'2026-09-24': 3000, '2026-09-25': 6000, '2026-09-28': 5000, '2026-09-29': 2500}


class FakeSource:
    def __init__(self, block_at=None):
        self.calls, self.block_at = [], block_at

    def __call__(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def fetch(self, origin, destination, day, details):
        self.calls.append((origin, destination, day.isoformat()))
        if len(self.calls) == self.block_at:
            raise BlockedError('captcha')
        far = bool({'BKK', 'CTS'} & {origin, destination})
        flight = {'airline': 'Test Air', 'depart_date': day.isoformat(), 'arrive_date': day.isoformat(),
                  'depart_time': '09:00', 'arrive_time': '12:00', 'flights': [],
                  'price': None if 'HKG' in (origin, destination) else PRICES[day.isoformat()] + (4000 if far else 0)}
        return {'origin': origin, 'destination': destination, 'date': day.isoformat(), 'status': 'ok',
                'fetched_at': datetime.now(timezone.utc).isoformat(), 'flights': [flight]}


def test_run_ranks_destinations_and_leave_options(tmp_path):
    source, progress = FakeSource(), []
    report = run('TPE', HOLIDAY, tmp_path, tmp_path / 'h.sqlite3', delay=0, source_factory=source, on_progress=progress.append)
    assert report['requested_queries'] == report['completed_queries'] == len(DESTINATIONS) * 4 == len(source.calls)
    assert len(progress) == len(DESTINATIONS)
    best = report['deals'][0]
    assert (best['depart'], best['return'], best['leave_days'], best['price']) == ('2026-09-24', '2026-09-29', 2, 5500)
    no_leave = [d for d in report['deals'] if d['leave_days'] == 0]
    assert {d['price'] for d in no_leave} == {11000, 19000}
    # 沒報價的地點不以 0 元上榜
    assert 'HKG' not in {d['destination'] for d in report['deals']}
    assert [d['price'] for d in report['deals']] == sorted(d['price'] for d in report['deals'])
    assert report['aborted_reason'] == ''


def test_block_stops_immediately_and_keeps_finished_destinations(tmp_path):
    source, progress = FakeSource(block_at=6), []
    report = run('TPE', HOLIDAY, tmp_path, tmp_path / 'h.sqlite3', delay=0, source_factory=source, on_progress=progress.append)
    assert len(source.calls) == 6 and report['aborted_reason'] == 'captcha'
    assert {d['destination'] for d in report['deals']} == {DESTINATIONS[0]}
    assert progress[-1]['aborted_reason'] == 'captcha'


def test_aborted_refresh_keeps_previous_complete_report(tmp_path):
    full = run('TPE', HOLIDAY, tmp_path, tmp_path / 'h.sqlite3', delay=0, source_factory=FakeSource())
    finish(full, tmp_path)
    blocked = run('TPE', HOLIDAY, tmp_path, tmp_path / 'h.sqlite3', refresh=True, delay=0, source_factory=FakeSource(block_at=6))
    finish(blocked, tmp_path)
    assert len(json.loads((tmp_path / 'latest.json').read_text())['deals']) == len(full['deals'])
    assert json.loads((tmp_path / 'partial.json').read_text())['aborted_reason'] == 'captcha'
    # 從沒查過的連假：被擋也要留下已查到的部分，總比空白好
    finish(blocked, tmp_path / 'fresh')
    assert json.loads((tmp_path / 'fresh' / 'latest.json').read_text())['aborted_reason'] == 'captcha'


def test_extra_destination_queries_only_itself_and_merges(tmp_path):
    full = run('TPE', HOLIDAY, tmp_path, tmp_path / 'h.sqlite3', delay=0, source_factory=FakeSource())
    source = FakeSource()
    extra = run('TPE', HOLIDAY, tmp_path, tmp_path / 'h.sqlite3', delay=0, source_factory=source, destinations=('CTS',))
    assert len(source.calls) == 4 and {c[1] for c in source.calls} | {c[0] for c in source.calls} == {'TPE', 'CTS'}
    merged = merge(full, extra)
    assert merged['destinations'] == list(DESTINATIONS) + ['CTS']
    assert merged['requested_queries'] == merged['completed_queries'] == merged['successful_queries'] == 36
    assert [d['price'] for d in merged['deals']] == sorted(d['price'] for d in merged['deals'])
    assert len([d for d in merged['deals'] if d['destination'] == 'CTS']) == 4
    # 同一個地點再查一次是取代，不是疊加
    again = merge(merged, extra)
    assert again['destinations'] == merged['destinations'] and len(again['deals']) == len(merged['deals'])
    assert again['completed_queries'] == 36


def test_origin_inside_default_list_is_skipped(tmp_path):
    source = FakeSource()
    report = run('PUS', HOLIDAY, tmp_path, tmp_path / 'h.sqlite3', delay=0, source_factory=source)
    assert 'PUS' not in report['destinations'] and len(source.calls) == (len(DESTINATIONS) - 1) * 4
    assert all(a != b for a, b, _ in source.calls)


def test_cli_without_arguments_lists_breaks_without_pricing(tmp_path, monkeypatch, capsys):
    import fare_watch.holiday_deals as deals
    monkeypatch.setattr(deals, 'Calendar', lambda directory: Calendar(directory, fake_fetch()))
    monkeypatch.setattr(deals, 'run', lambda *a, **k: pytest.fail('listing breaks must not query fares'))
    assert deals.main(['--calendar-dir', str(tmp_path)]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed['errors'] == [] and {'breaks', 'sources'} <= set(listed)
    with pytest.raises(SystemExit):
        deals.main(['TPE', '2026-09-25', '--calendar-dir', str(tmp_path)])


def test_second_run_uses_cache(tmp_path):
    run('TPE', HOLIDAY, tmp_path, tmp_path / 'h.sqlite3', delay=0, source_factory=FakeSource())
    again = FakeSource()
    report = run('TPE', HOLIDAY, tmp_path, tmp_path / 'h.sqlite3', delay=0, source_factory=again)
    assert again.calls == [] and len(report['deals']) > 0
    assert {q['provenance'] for q in report['queries']} == {'cache'}


def test_http_holidays_and_deals(tmp_path, monkeypatch):
    import fare_watch.web_server as web

    class Today(date):
        @classmethod
        def today(cls):
            return cls(2026, 9, 19)
    monkeypatch.setattr(web, 'date', Today)
    app = Application(tmp_path)
    app.calendar_options = {'fetch': fake_fetch()}
    started = []
    def runner(job_id, args, output):
        started.append(args)
        with app.lock:
            app.jobs[job_id].update(state='completed')
    app.runner = runner
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.app = app
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{server.server_port}'
    post = lambda body: urlopen(Request(base + '/api/deals', data=json.dumps(body).encode(), headers={'X-Request-Token': app.token}))
    try:
        assert b'holiday.js' in urlopen(base).read()
        assert b'/api/holidays' in urlopen(base + '/holiday.js').read()
        holidays = json.load(urlopen(base + '/api/holidays'))
        assert holidays['breaks'][0]['start'] == '2026-09-25' and holidays['errors'] == []
        assert json.load(urlopen(base + '/api/deals?origin=TPE&start=2026-09-25&end=2026-09-28')) == {'report': None, 'job': None}
        job = json.load(post(dict(origin='TPE', start='2026-09-25', end='2026-09-28')))
        assert started[0][:3] == ['TPE', '2026-09-25', '2026-09-28'] and '--refresh' not in started[0]
        assert json.load(urlopen(base + '/api/jobs/' + job['id']))['state'] == 'completed'
        post(dict(origin='台北', start='2026-09-25', end='2026-09-28', destination='札幌', refresh=True))
        assert started[1][:3] == ['TPE', '2026-09-25', '2026-09-28'] and started[1][-3:] == ['--refresh', '--destination', 'CTS']
        with pytest.raises(HTTPError) as err:
            post(dict(origin='TPE', start='2026-09-25', end='2026-09-28', destination='台北'))
        assert err.value.code == 400
        # 只查日曆上的連假：任意日期不能拿來驅動查價
        with pytest.raises(HTTPError) as err:
            post(dict(origin='TPE', start='2026-11-03', end='2026-11-05'))
        assert err.value.code == 400
        with pytest.raises(HTTPError) as err:
            urlopen(Request(base + '/api/deals', data=b'{}'))
        assert err.value.code == 403
    finally:
        server.shutdown()
        server.server_close()
