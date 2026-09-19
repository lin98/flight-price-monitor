import argparse
from datetime import date, datetime, timedelta, timezone
import json

import pytest

from fare_watch.search import (Cache, airport, combinations, deduplicate, iso_date,
                               markdown, parse_label, parser, plan, query_url, run)
from fare_watch.fetchers import BlockedError

LABEL = ('From 4543 New Taiwan dollars. Nonstop flight with EASTAR JET. '
         'Leaves Gimhae International Airport at 10:00 PM on Monday, March 15 '
         'and arrives at Taiwan Taoyuan International Airport at 12:15 AM on Tuesday, March 16. '
         'Total duration 3 hr 15 min. Select flight')


def flight(price=4543):
    f = parse_label(LABEL, date(2027, 3, 15), 'PUS', 'TPE', 'Boeing 737\nZE 983\n10:00 PM\nPM 10')
    f['price'] = price
    return f


def test_dates_prices_and_flight_numbers():
    f = flight()
    assert f['depart_time'] == '22:00'
    assert f['arrive_time'] == '00:15'
    assert f['arrive_date'] == '2027-03-16'
    assert f['flights'] == ['ZE983']
    assert f['price'] == 4543
    assert f['price_type'] == 'one_way'
    assert parse_label(LABEL, date(2027, 3, 14), 'PUS', 'TPE') is None


def test_unpriced_and_round_trip_estimate_not_used_as_single_fare():
    for prefix in ['Total price is unavailable.', 'From 7083 New Taiwan dollars round trip total.']:
        f = parse_label(LABEL.replace('From 4543 New Taiwan dollars.', prefix), date(2027, 3, 15), 'PUS', 'TPE')
        assert f['price'] is None
        assert f['status'] == 'schedule_only'


def test_year_crossing_and_date_line():
    label = LABEL.replace('Monday, March 15', 'Thursday, December 31').replace('Tuesday, March 16', 'Friday, January 1')
    assert parse_label(label, date(2026, 12, 31), 'PUS', 'TPE')['arrive_date'] == '2027-01-01'
    label = LABEL.replace('Tuesday, March 16', 'Sunday, March 14')
    assert parse_label(label, date(2027, 3, 15), 'PUS', 'TPE')['arrive_date'] == '2027-03-14'


def test_aliases_validation_and_arbitrary_route():
    assert airport('台北') == 'TPE'
    assert airport('lax') == 'LAX'
    with pytest.raises(argparse.ArgumentTypeError):
        airport('東京')
    with pytest.raises(argparse.ArgumentTypeError):
        iso_date('20270311')
    url = query_url('LAX', 'NRT', date(2027, 7, 1))
    assert 'LAX' in url and 'NRT' in url and '2027-07-01' in url
    assert 'PUS' not in url


def test_default_scan_and_custom_nights():
    args = parser().parse_args(['台北', '釜山'])
    pairs = plan(args, date(2026, 9, 9))
    assert len(pairs) == 30
    assert pairs[0] == (date(2026, 9, 10), date(2026, 9, 14))
    args = parser().parse_args(['LAX', 'NRT', '--start', '2027-06-30', '--end', '2027-07-02', '--nights', '7'])
    pairs = plan(args, date(2026, 9, 9))
    assert len(pairs) == 3 and pairs[-1][1] == date(2027, 7, 9)


@pytest.mark.parametrize('argv', [
    ['TPE', 'TPE'], ['TPE', 'PUS', '2020-01-01'],
    ['TPE', 'PUS', '2027-03-15', '2027-03-11'],
    ['TPE', 'PUS', '--days', '181'], ['TPE', 'PUS', '--nights', '0'],
    ['TPE', 'PUS', '--end', '2027-03-15'],
    ['TPE', 'PUS', '2027-03-11', '--start', '2027-03-01'],
])
def test_invalid_plans(argv):
    with pytest.raises(ValueError):
        plan(parser().parse_args(argv), date(2026, 9, 9))


def test_all_combinations_and_unknown_price_excluded():
    outbound = {'flights': [dict(flight(10), flights=[f'IT{i}']) for i in range(13)] + [flight(None)]}
    inbound = {'flights': [dict(flight(i + 20), flights=[f'ZE{i}']) for i in range(12)]}
    combos = combinations(outbound, inbound)
    assert len(combos) == 156  # no four-card/cheapest-return limit
    assert combos[0]['price'] == 30 and combos[-1]['price'] == 41
    assert all(c['price_type'] == 'two_one_way_sum' for c in combos)
    assert len(deduplicate([flight(None), flight(4543)])) == 1
    assert deduplicate([flight(None), flight(4543)])[0]['price'] == 4543


def test_cache_route_detail_ttl_and_corruption(tmp_path):
    cache = Cache(tmp_path)
    result = {'origin': 'PUS', 'destination': 'TPE', 'date': '2027-03-15', 'status': 'ok',
              'fetched_at': datetime.now(timezone.utc).isoformat(), 'flights': [flight()]}
    cache.put(result, True)
    assert cache.get('PUS', 'TPE', '2027-03-15', True)['provenance'] == 'cache'
    assert cache.get('TPE', 'PUS', '2027-03-15', True) is None
    assert cache.get('PUS', 'TPE', '2027-03-15', False) is None
    assert Cache(tmp_path, 0).get('PUS', 'TPE', '2027-03-15', True) is None
    result['fetched_at'] = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
    cache.put(result, True)
    assert cache.get('PUS', 'TPE', '2027-03-15', True) is None
    cache.path('PUS', 'TPE', '2027-03-15', True).write_text('{broken')
    assert cache.get('PUS', 'TPE', '2027-03-15', True) is None


class FakeSource:
    calls = []
    blocked_at = None
    def __enter__(self):
        self.browser = True
        return self
    def __exit__(self, *args):
        pass
    def fetch(self, origin, destination, day, details=False):
        self.calls.append((origin, destination, day, details))
        if len(self.calls) == self.blocked_at:
            raise BlockedError('HTTP 429')
        return {'origin': origin, 'destination': destination, 'date': str(day), 'status': 'ok',
                'fetched_at': datetime.now(timezone.utc).isoformat(), 'flights': [flight(day.day * 100)],
                'url': query_url(origin, destination, day)}


def test_scan_rank_cache_and_blocked_coverage(tmp_path):
    args = parser().parse_args(['TPE', 'PUS', '--start', '2027-03-11', '--days', '3', '--output', str(tmp_path), '--history-db', str(tmp_path / 'history.sqlite3'), '--delay', '0'])
    FakeSource.calls = []
    FakeSource.blocked_at = None
    report = run(args, FakeSource, date(2026, 9, 9))
    assert report['requested_queries'] == 6 and report['successful_queries'] == 6
    assert report['ranked'][0]['min_price'] == 2600
    assert report['ranked'][0]['depart'] == '2027-03-11'
    run(args, FakeSource, date(2026, 9, 9))
    assert len(FakeSource.calls) == 6  # cached run never starts a browser
    args.refresh = True
    FakeSource.calls = []
    FakeSource.blocked_at = 3
    report = run(args, FakeSource, date(2026, 9, 9))
    assert len(FakeSource.calls) == 3
    assert report['aborted_reason'] == 'HTTP 429'
    assert len(report['ranked']) == 1
    assert report['dates'][-1]['outbound']['status'] == 'not_fetched'
    assert 'HTTP 429' in markdown(report)
    FakeSource.blocked_at = None


def test_existing_entry_routes_to_search_without_old_window(capsys):
    from fare_watch.cli import main
    assert main(['search', 'LAX', 'NRT', '2028-07-01', '--dry-run']) == 0
    assert '2028-07-01' in capsys.readouterr().out


def test_browser_start_failure_stops_once(tmp_path):
    class BrokenSource:
        attempts = 0
        def __enter__(self):
            self.attempts += 1
            raise RuntimeError('Chromium missing')
    source = BrokenSource()
    args = parser().parse_args(['TPE', 'PUS', '--days', '3', '--output', str(tmp_path), '--history-db', str(tmp_path / 'history.sqlite3'), '--delay', '0'])
    result = run(args, lambda: source, date(2026, 9, 9))
    assert source.attempts == 1
    assert result['completed_queries'] == 1
    assert result['ranked'] == []
    assert 'Chromium missing' in result['aborted_reason']
