import datetime as dt
import unittest
from pathlib import Path
from unittest import mock

import requests

from fare_watch.fetchers import (
    BlockedError, GoogleFlightsFetcher, detect_block, extract_init_data, google_flights_url, parse_offers,
)
from fare_watch.itineraries import build_itinerary

FIXTURE = Path(__file__).parent / "fixtures" / "google_flights_tpe_pus_2027-03-01.html"
IT = build_itinerary(dt.date(2027, 3, 1))


class FakeResponse:
    def __init__(self, status=200, text="", url="https://www.google.com/travel/flights?x"):
        self.status_code, self.text, self.url = status, text, url


class ParserTests(unittest.TestCase):
    def setUp(self):
        self.html = FIXTURE.read_text(encoding="utf-8")

    def test_url_is_shareable_query(self):
        url = google_flights_url(IT)
        self.assertIn("curr=TWD", url)
        self.assertIn("Flights%20to%20PUS%20from%20TPE%20on%202027-03-01%20through%202027-03-05", url)

    def test_extract_and_parse_real_fixture(self):
        data = extract_init_data(self.html)
        self.assertIsNotNone(data)
        offers = parse_offers(data)
        self.assertGreater(len(offers), 5)
        cheapest = min(offers, key=lambda o: o.price)
        self.assertEqual(cheapest.price, 7623)
        self.assertEqual(cheapest.airline, "IT")
        self.assertEqual(cheapest.flights, ["IT606"])
        self.assertEqual(cheapest.stops, 0)
        self.assertEqual(cheapest.depart_time, "16:50")
        self.assertEqual(cheapest.arrive_time, "20:00")
        self.assertEqual(cheapest.currency, "TWD")
        self.assertTrue(all(o.price > 1000 for o in offers))
        self.assertEqual({o.section for o in offers}, {"best", "other"})

    def test_extract_missing_block(self):
        self.assertIsNone(extract_init_data("<html><body>nothing</body></html>"))
        self.assertIsNone(extract_init_data("AF_initDataCallback({key: 'ds:1', hash: '1', data:[broken"))

    def test_parse_garbage_returns_empty(self):
        self.assertEqual(parse_offers([[], None, [None], "x"]), [])
        self.assertEqual(parse_offers(None), [])

    def test_detect_block(self):
        self.assertEqual(detect_block("", "https://consent.google.com/m?continue=x"), "consent_page")
        self.assertEqual(detect_block("", "https://www.google.com/sorry/index?continue=x"), "captcha_or_sorry_page")
        self.assertIsNone(detect_block(self.html, "https://www.google.com/travel/flights?q=x"))


class FetcherTests(unittest.TestCase):
    def _fetcher(self, responses):
        session = mock.Mock()
        session.headers = {}
        session.get = mock.Mock(side_effect=responses)
        return GoogleFlightsFetcher(session=session, sleep=lambda s: None), session

    def test_ok(self):
        f, s = self._fetcher([FakeResponse(text=FIXTURE.read_text(encoding="utf-8"))])
        res = f.fetch(IT)
        self.assertEqual(res.status, "ok")
        self.assertEqual(res.min_price, 7623)
        self.assertIsNone(res.html)
        self.assertEqual(s.get.call_count, 1)

    def test_dynamic_page_is_unavailable_with_reason(self):
        f, _ = self._fetcher([FakeResponse(text="<html><script>app()</script></html>")])
        res = f.fetch(IT)
        self.assertEqual(res.status, "unavailable")
        self.assertTrue(res.reason.startswith("dynamic_content"))
        self.assertIsNotNone(res.html)

    def test_retry_then_ok(self):
        f, s = self._fetcher([
            requests.ConnectionError("boom"),
            FakeResponse(status=503),
            FakeResponse(text=FIXTURE.read_text(encoding="utf-8")),
        ])
        self.assertEqual(f.fetch(IT).status, "ok")
        self.assertEqual(s.get.call_count, 3)

    def test_retries_exhausted(self):
        f, _ = self._fetcher([requests.Timeout("t")] * 3)
        res = f.fetch(IT)
        self.assertEqual(res.status, "error")
        self.assertIn("retries_exhausted", res.reason)

    def test_blocked_raises(self):
        f, _ = self._fetcher([FakeResponse(status=429)])
        with self.assertRaises(BlockedError):
            f.fetch(IT)
        f, _ = self._fetcher([FakeResponse(text="x", url="https://www.google.com/sorry/index")])
        with self.assertRaises(BlockedError):
            f.fetch(IT)


if __name__ == "__main__":
    unittest.main()
