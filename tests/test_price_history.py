from copy import deepcopy
import json

import pytest

from fare_watch.price_history import History, export, html_report, markdown


def result(at='2026-09-09T01:00:00+00:00', price=3699, number='IT606', status='ok'):
    return {'origin': 'TPE', 'destination': 'PUS', 'date': '2027-03-11', 'fetched_at': at,
            'source': 'google_flights_browser', 'provenance': 'live', 'status': status,
            'flights': [{'origin': 'TPE', 'destination': 'PUS', 'depart_date': '2027-03-11',
                         'arrive_date': '2027-03-11', 'depart_time': '16:50', 'arrive_time': '20:05',
                         'airline': 'Tigerair Taiwan', 'flights': [number] if number else [],
                         'price': price, 'currency': 'TWD'}]}


def test_price_changes_same_flight_same_price_each_day_and_intraday(tmp_path):
    h = History(tmp_path / 'history.db')
    for at, price in [('2026-09-09T01:00:00Z', 3699), ('2026-09-10T01:00:00Z', 3699),
                      ('2026-09-10T09:00:00Z', 3299), ('2026-09-11T01:00:00Z', 3999)]:
        assert h.record(result(at, price)) == 1
    s = h.report('TPE', 'PUS')['series'][0]
    assert s['sample_count'] == 4
    assert s['change'] == 700
    assert s['lowest'] == 3299 and s['highest'] == 3999
    assert len(s['daily']) == 3
    assert s['daily'][1]['samples'] == 2 and s['daily'][1]['min_price'] == 3299
    assert s['daily'][1]['last_price'] == 3299
    assert s['daily'][1]['change_from_previous_quoted_day'] == -400
    assert s['daily'][2]['change_from_previous_quoted_day'] == 700


def test_cache_and_import_idempotency_no_fake_observations(tmp_path):
    h = History(tmp_path / 'history.db')
    r = result()
    assert h.record(r) == 1
    assert h.record(r) == 0
    assert h.record(dict(r, provenance='cache', fetched_at='2026-09-10T01:00:00Z')) == 0
    assert h.report('TPE', 'PUS')['series'][0]['sample_count'] == 1


def test_midnight_timezone_and_time_change_keeps_same_flight(tmp_path):
    h = History(tmp_path / 'history.db')
    h.record(result('2026-09-09T15:59:00Z'))
    r = result('2026-09-09T16:01:00Z', 4000)
    r['flights'][0]['depart_time'] = '17:00'
    h.record(r)
    series = h.report('TPE', 'PUS')['series']
    assert len(series) == 1
    assert [d['date'] for d in series[0]['daily']] == ['2026-09-09', '2026-09-10']
    assert series[0]['change'] == 301


def test_missing_price_and_failed_query_never_zero(tmp_path):
    h = History(tmp_path / 'history.db')
    h.record(result())
    h.record(result('2026-09-10T01:00:00Z', None))
    r = result('2026-09-11T01:00:00Z', status='blocked')
    r.update(flights=[], reason='HTTP 429')
    h.record(r)
    report = h.report('TPE', 'PUS')
    s = report['series'][0]
    assert s['lowest'] == 3699 and s['latest_observed_price'] is None
    assert s['change'] is None
    assert s['latest_status'] == 'query_failed_or_partial'
    assert report['queries'][-1]['status'] == 'blocked'
    assert 'HTTP 429' in markdown(report)


def test_different_flights_dates_routes_and_fallback_are_separate(tmp_path):
    h = History(tmp_path / 'history.db')
    r = result()
    r['flights'] += result(number='ZE984')['flights'] + result(number=None)['flights']
    h.record(r)
    other = result('2026-09-10T01:00:00Z')
    other['date'] = '2027-03-12'
    other['flights'][0]['depart_date'] = '2027-03-12'
    h.record(other)
    assert len(h.report('TPE', 'PUS')['series']) == 4
    assert len(h.report('TPE', 'PUS', depart='2027-03-11', flight='IT606')['series']) == 1
    assert h.report('PUS', 'TPE')['series'] == []


def test_scan_without_numbers_does_not_claim_known_flight_disappeared(tmp_path):
    h = History(tmp_path / 'history.db')
    h.record(result())
    h.record(result('2026-09-10T01:00:00Z', number=None))
    assert h.report('TPE', 'PUS', flight='IT606')['series'][0]['latest_status'] == 'identity_unconfirmed'


def test_import_only_oneway_search_reports_and_export(tmp_path):
    h = History(tmp_path / 'history.db')
    report = {'schema': 2, 'price_type': 'two_one_way_sum', 'dates': [{'outbound': result()}]}
    (tmp_path / 'sample.json').write_text(json.dumps(report))
    (tmp_path / 'old.json').write_text(json.dumps({'run': {}, 'price_type': 'round_trip', 'dates': report['dates']}))
    assert h.import_reports(tmp_path) == 1
    assert h.import_reports(tmp_path) == 0
    path = export(h.report('TPE', 'PUS'), tmp_path, 'history')
    assert path.exists() and path.with_suffix('.csv').exists()
    assert '<circle' in path.read_text() and '<line ' not in path.read_text()
    assert 'IT606' in path.with_suffix('.csv').read_text(encoding='utf-8-sig')


def test_invalid_price_transaction_rolls_back(tmp_path):
    h = History(tmp_path / 'history.db')
    with pytest.raises(ValueError):
        h.record(result(price=0))
    assert h.report('TPE', 'PUS')['queries'] == []
