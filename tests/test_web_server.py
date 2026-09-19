import json
import argparse
import threading
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import pytest
from fare_watch.web_server import Application, Handler, ThreadingHTTPServer, search_arguments


def test_arguments(tmp_path):
    args = search_arguments(dict(origin='台北', destination='釜山', depart='2027-03-11', return_date='2027-03-15'), tmp_path, tmp_path/'db')
    assert args[:4] == ['TPE', 'PUS', '2027-03-11', '2027-03-15']
    with pytest.raises(argparse.ArgumentTypeError):
        search_arguments(dict(origin='TPE', destination='PUS', depart='invalid'), tmp_path, tmp_path/'db')


def test_http_auth_history_and_job(tmp_path):
    app = Application(tmp_path)
    def runner(job_id, args, output):
        with app.lock:
            app.jobs[job_id].update(state='completed', report={'dates': []})
    app.runner = runner
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.app = app
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    try:
        assert app.token in urlopen(base).read().decode()
        assert b'onsubmit' in urlopen(base+'/app.js').read()
        assert json.load(urlopen(base+'/api/history?origin=TPE&destination=PUS'))['series'] == []
        body = json.dumps(dict(origin='TPE', destination='PUS', depart='2027-03-11')).encode()
        with pytest.raises(HTTPError) as err:
            urlopen(Request(base+'/api/search', data=body))
        assert err.value.code == 403
        job = json.load(urlopen(Request(base+'/api/search', data=body, headers={'X-Request-Token': app.token})))
        result = json.load(urlopen(base+'/api/jobs/'+job['id']))
        assert result['state'] == 'completed'
        assert 'output' not in result
        with pytest.raises(HTTPError) as err:
            urlopen(Request(base, headers={'Host':'evil.example'}))
        assert err.value.code == 403
    finally:
        server.shutdown()
        server.server_close()


def test_watch_persistence_dedup_and_removal(tmp_path):
    app = Application(tmp_path)
    payload = dict(mode='dates', origin='台北', destination='釜山', depart='2027-03-11', return_date='2027-03-15')
    rows = app.save_watch(payload)
    assert rows[0]['origin'] == 'TPE'
    assert rows[0]['destination'] == 'PUS'
    assert len(app.save_watch(payload)) == 1
    restarted = Application(tmp_path)
    assert restarted.watches() == rows
    assert restarted.remove_watch(rows[0]['id']) == []
    assert Application(tmp_path).watches() == []
    with pytest.raises(ValueError):
        app.save_watch(dict(payload, return_date='2027-03-01'))
    assert app.watches() == []


def test_airport_choices_disambiguate_cities():
    from fare_watch.web_server import airport_options
    rows = airport_options()
    assert len({r['code'] for r in rows}) == len(rows)
    assert {r['code'] for r in rows if 'Tokyo' in r['name']} == {'NRT', 'HND'}
    assert '台北' in next(r['aliases'] for r in rows if r['code'] == 'TPE')
