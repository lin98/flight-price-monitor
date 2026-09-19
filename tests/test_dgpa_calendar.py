"""人事行政總處辦公日曆表：fixture 是官方 CSV 原檔與精簡過的資料集索引，全部離線。"""
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from fare_watch.dgpa_calendar import DATASET_URL, Calendar, parse_csv, resource_url, upcoming_breaks

FIXTURES = Path(__file__).with_name('fixtures')
INDEX = (FIXTURES / 'dgpa_dataset_index.json').read_bytes()


def fake_fetch(calls=None, fail=False):
    def fetch(url):
        if calls is not None:
            calls.append(url)
        if fail:
            raise OSError('network down')
        if url == DATASET_URL:
            return INDEX
        year = 2026 if '202506' in url else 2027
        return (FIXTURES / f'dgpa_calendar_{year}.csv').read_bytes()
    return fetch


def days_of(*years):
    out = {}
    for year in years:
        for d in parse_csv((FIXTURES / f'dgpa_calendar_{year}.csv').read_bytes(), year):
            out[date.fromisoformat(d['date'])] = d
    return out


def test_parse_official_csv():
    days = {d['date']: d for d in parse_csv((FIXTURES / 'dgpa_calendar_2026.csv').read_bytes(), 2026)}
    assert len(days) == 365
    assert days['2026-09-28'] == {'date': '2026-09-28', 'off': True, 'note': '孔子誕辰紀念日/教師節'}
    assert days['2026-09-24']['off'] is False


def test_parse_rejects_unexpected_format():
    with pytest.raises(ValueError):
        parse_csv('date,off\n20260101,2\n'.encode(), 2026)
    # 官方只定義 0 與 2；出現別的值要失敗，不能默默當成上班日
    body = (FIXTURES / 'dgpa_calendar_2026.csv').read_bytes().decode('utf-8-sig').replace('20260102,五,0,', '20260102,五,1,')
    with pytest.raises(ValueError):
        parse_csv(body.encode(), 2026)
    with pytest.raises(ValueError):
        parse_csv((FIXTURES / 'dgpa_calendar_2026.csv').read_bytes(), 2027)


def test_resource_url_skips_google_variant_and_foreign_hosts():
    index = json.loads(INDEX)
    url = resource_url(index, 2026)
    assert url.startswith('https://www.dgpa.gov.tw/') and '202506' in url
    assert resource_url(index, 2030) is None
    index['result']['distribution'][0]['resourceDownloadUrl'] = 'https://evil.example/calendar.csv'
    with pytest.raises(ValueError):
        resource_url(index, 2026)


def test_upcoming_breaks_and_leave_options():
    breaks = upcoming_breaks(days_of(2026, 2027), date(2026, 9, 19))
    first = breaks[0]
    assert (first['start'], first['end'], first['days']) == ('2026-09-25', '2026-09-28', 4)
    assert first['name'] == '中秋節、孔子誕辰紀念日/教師節'
    assert [(o['depart'], o['return'], o['total_days'], o['leave_days']) for o in first['options']] == [
        ('2026-09-25', '2026-09-28', 4, 0), ('2026-09-24', '2026-09-28', 5, 1),
        ('2026-09-25', '2026-09-29', 5, 1), ('2026-09-24', '2026-09-29', 6, 2)]
    # 補假不當連假名稱；跨年的連假要接得起來
    assert [b['name'] for b in breaks[1:5]] == ['國慶日', '臺灣光復暨金門古寧頭大捷紀念日', '行憲紀念日', '開國紀念日']
    assert len(breaks) == 6


def test_breaks_drop_past_departures_and_started_breaks():
    days = days_of(2026, 2027)
    eve = upcoming_breaks(days, date(2026, 9, 24))[0]
    assert {o['depart'] for o in eve['options']} == {'2026-09-25'}
    assert upcoming_breaks(days, date(2026, 9, 25))[0]['start'] == '2026-10-09'


def test_calendar_caches_and_never_guesses(tmp_path):
    calls = []
    loaded = Calendar(tmp_path, fake_fetch(calls)).load(date(2026, 9, 19))
    assert len(loaded['days']) == 730 and loaded['errors'] == []
    assert [s['stale'] for s in loaded['sources']] == [False, False]
    fetched = len(calls)
    Calendar(tmp_path, fake_fetch(calls)).load(date(2026, 9, 19))
    assert len(calls) == fetched  # 一週內不重抓

    # 快取過期又抓不到：沿用舊檔並標 stale
    for path in tmp_path.glob('*.json'):
        data = json.loads(path.read_text())
        data['fetched_at'] = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        path.write_text(json.dumps(data))
    stale = Calendar(tmp_path, fake_fetch(fail=True)).load(date(2026, 9, 19))
    assert len(stale['days']) == 730 and [s['stale'] for s in stale['sources']] == [True, True]
    assert len(stale['errors']) == 2

    # 完全沒資料：回空，不用「平日上班」去推
    empty = Calendar(tmp_path / 'none', fake_fetch(fail=True)).load(date(2026, 9, 19))
    assert empty['days'] == {} and len(empty['errors']) == 2


def test_unpublished_year_is_reported_not_raised(tmp_path):
    loaded = Calendar(tmp_path, fake_fetch()).load(date(2027, 6, 1))
    assert {d.year for d in loaded['days']} == {2027}
    assert loaded['errors'] == ['2028 年辦公日曆表尚未公告']
