import datetime as dt
import re
import unittest
from pathlib import Path

from fare_watch.fetchers import BlockedError, FetchResult
from fare_watch.grouped import build_grouped_summary, render_grouped_markdown
from fare_watch.itineraries import build_itinerary
from fare_watch.playwright_fetcher import PlaywrightFetcher, parse_result_html

FIXTURE = Path(__file__).parent / "fixtures" / "playwright_gf_roundtrip_tpe_pus.html"
HTML = FIXTURE.read_text(encoding="utf-8")
IT = build_itinerary(dt.date(2027, 3, 1))
TS = "2026-08-29T10:00:00+08:00"


def _rows(html):
    return re.findall(r'<li class="pIav2d".*?</li>', html, re.S)


def _render(html, url="https://www.google.com/travel/flights"):
    """假的 renderer：不開瀏覽器，直接回傳固定 HTML 與最終網址。"""
    return lambda _url, _ua, _headless: (html, url)


class ParseTests(unittest.TestCase):
    def test_round_trip_fields(self):
        o = parse_result_html(HTML)[0]
        self.assertEqual(o.airline_name, "Tigerair Taiwan")
        self.assertEqual(o.flights, ["IT606"])
        self.assertEqual(o.depart_time, "16:50")
        self.assertEqual(o.arrive_time, "20:00")
        self.assertEqual(o.duration_min, 190)
        self.assertEqual(o.return_flights, ["IT607"])
        self.assertEqual(o.return_depart_time, "12:05")
        self.assertEqual(o.return_arrive_time, "13:40")
        self.assertEqual(o.return_duration_min, 155)
        self.assertEqual(o.stops, 0)
        self.assertEqual(o.return_stops, 0)
        self.assertEqual(o.price, 7000)
        self.assertEqual(o.currency, "TWD")
        self.assertEqual(o.return_time_status, "ok")
        self.assertTrue(o.nonstop)

    def test_midnight_return_and_priceless_row_skipped(self):
        offers = parse_result_html(HTML)
        # 4 個 <li> 中沒有價格的那一列（7C6256）整列略過，不用 0 填補
        self.assertEqual(len(_rows(HTML)), 4)
        self.assertEqual([o.flights[0] for o in offers], ["IT606", "BX586", "CI188"])
        bx = offers[1]
        self.assertEqual(bx.airline_name, "Air Busan")
        self.assertEqual(bx.return_flights, ["BX585"])
        self.assertEqual(bx.return_depart_time, "22:30")
        self.assertEqual(bx.return_arrive_time, "00:05")  # 12:05 AM+1 的跨日標記不得混進時刻
        self.assertNotIn("7C6255", [f for o in offers for f in o.return_flights])

    def test_connection_row_keeps_all_segments_and_stops(self):
        ci = parse_result_html(HTML)[2]
        self.assertEqual(ci.flights, ["CI188", "CI160"])
        self.assertEqual(ci.stops, 1)
        self.assertEqual(ci.duration_min, 600)  # "10 hr" 沒有分鐘部分
        self.assertEqual(ci.return_stops, 0)
        self.assertFalse(ci.nonstop)


class FetcherTests(unittest.TestCase):
    def test_ok_keeps_only_nonstop_both_ways(self):
        res = PlaywrightFetcher(origin="TPE", dest="PUS", render=_render(HTML)).fetch(IT)
        self.assertEqual(res.status, "ok")
        self.assertEqual(res.source, "playwright")
        self.assertEqual([o.flights[0] for o in res.offers], ["IT606", "BX586"])
        self.assertEqual(res.min_price, 7000)
        self.assertIn("google.com/travel/flights", res.url)
        self.assertIsNone(res.html)  # 預設不留 HTML

    def test_unavailable_reasons_are_distinct(self):
        empty = PlaywrightFetcher(render=_render("<html><body></body></html>")).fetch(IT)
        self.assertEqual(empty.status, "unavailable")
        self.assertTrue(empty.reason.startswith("no_offers_parsed"))

        only_connection = _rows(HTML)[2]
        res = PlaywrightFetcher(render=_render(only_connection)).fetch(IT)
        self.assertEqual(res.status, "unavailable")
        self.assertTrue(res.reason.startswith("no_direct_offers"))

    def test_block_raises_and_render_error_is_reported(self):
        blocked = PlaywrightFetcher(render=_render('<html><form id="captcha-form"></form></html>'))
        with self.assertRaises(BlockedError):
            blocked.fetch(IT)

        def boom(_url, _ua, _headless):
            raise RuntimeError("browser not installed")

        res = PlaywrightFetcher(render=boom).fetch(IT)
        self.assertEqual(res.status, "error")
        self.assertIn("render_failed", res.reason)
        self.assertIn("browser not installed", res.reason)


class MissingPlaywrightTests(unittest.TestCase):
    def test_import_error_reports_playwright_unavailable(self):
        """playwright 沒裝時要給固定可 grep 的代碼，而不是混在一般 render_failed 裡。

        它是 fli 的 fallback，只有真的要退回來才需要；分不出來的話，
        「這台機器沒裝瀏覽器」會被誤讀成「這個日期抓不到票」。
        """
        def missing(*a, **kw):
            raise ImportError("No module named 'playwright'")

        res = PlaywrightFetcher(origin="TPE", dest="PUS", render=missing).fetch(build_itinerary(dt.date(2027, 3, 15)))
        self.assertEqual(res.status, "error")
        self.assertTrue(res.reason.startswith("playwright_unavailable:"), res.reason)
        self.assertEqual(res.offers, [])


class GroupedMarkdownTests(unittest.TestCase):
    def _summary(self):
        by_flight = {o.flights[0]: o for o in parse_result_html(HTML)}
        results = {IT.key: {
            "TPE": FetchResult("playwright", IT.key, "ok", "http://x/TPE", TS, offers=[by_flight["IT606"]]),
            "KHH": FetchResult("playwright", IT.key, "ok", "http://x/KHH", TS, offers=[by_flight["BX586"]]),
        }}
        meta = {"run_id": "r1", "mode": "once", "source": "playwright", "months": [3], "arrival_gap_min": 120,
                "started_at": TS, "finished_at": TS, "duration_sec": 5.0, "timezone": "Asia/Taipei",
                "aborted_reason": ""}
        return build_grouped_summary(meta, [IT], results, 120)

    def test_traveler_cell_shows_both_legs_with_durations(self):
        md = render_grouped_markdown(self._summary())
        self.assertIn("去程 3/1(一) IT606 Tigerair Taiwan TPE 16:50 → PUS 20:00（3h10m）<br>"
                      "回程 3/5(五) IT607 Tigerair Taiwan PUS 12:05 → TPE 13:40（2h35m）<br>"
                      "每人來回票價 7,000 TWD（去回程整筆，來源未提供單程價）", md)
        # BX585 22:30→00:05 跨日，回程日期仍是行程的 2027-03-05，不推算抵達日
        self.assertIn("去程 3/1(一) BX586 Air Busan KHH 15:00 → PUS 18:25（3h25m）<br>"
                      "回程 3/5(五) BX585 Air Busan PUS 22:30 → KHH 00:05（2h35m）<br>"
                      "每人來回票價 8,600 TWD（去回程整筆，來源未提供單程價）", md)
        # 兩段時刻都解析到了：旅客欄位裡不該出現缺漏標記（說明段落本身有這個詞，所以比對整段）
        self.assertNotIn("→ TPE return_time_unavailable", md)
        self.assertNotIn("→ KHH return_time_unavailable", md)
        self.assertNotIn("合計", md)

    def test_summary_carries_return_leg_fields(self):
        row = self._summary()["ranked"][0]
        traveler_a = next(t for t in row["travelers"] if t["id"] == "a")
        self.assertEqual(traveler_a["return_flight"], "IT607")
        self.assertEqual(traveler_a["return_duration_min"], 155)
        self.assertEqual(traveler_a["return_time_status"], "ok")
        self.assertEqual(row["arrival_gap_min"], 95)


if __name__ == "__main__":
    unittest.main()
