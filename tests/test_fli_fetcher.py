"""fli 來源：優先走它，任何一點不對就安靜退回原本的 fetcher。

這裡**不裝也不需要 fli**。所有測試都用假的 search callable 或假的結果物件餵進轉換層，
因為要釘住的是「契約對不對、不合契約時退不退得回去」，不是 fli 本身能不能連線。
真實連線在隔離環境手動驗證，不進測試套件（fixture 不能冒充 production）。
"""

import datetime as dt
import json
import pathlib
import unittest
from types import SimpleNamespace
from unittest import mock

from fare_watch.fetchers import BlockedError, FetchResult, Fetcher, Offer
from fare_watch.fli_fetcher import (
    FliFetcher, FliFirstFetcher, FliUnusable, default_search, normalize_airline, offers_from_pairs,
)
from fare_watch.itineraries import build_itinerary
from fare_watch.playwright_fetcher import PlaywrightFetcher

IT = build_itinerary(dt.date(2027, 3, 15))


def _leg(airline="IT", number="606", dep="TPE", arr="PUS", dep_h=16, dep_m=50, arr_h=20, arr_m=0):
    return SimpleNamespace(
        airline=SimpleNamespace(name=airline, value="Tigerair Taiwan"),
        flight_number=number,
        departure_airport=SimpleNamespace(name=dep),
        arrival_airport=SimpleNamespace(name=arr),
        departure_datetime=dt.datetime(2027, 3, 15, dep_h, dep_m),
        arrival_datetime=dt.datetime(2027, 3, 15, arr_h, arr_m),
        duration=130,
    )


def _result(price=6223.0, currency="TWD", stops=0, legs=None, duration=130):
    return SimpleNamespace(price=price, currency=currency, stops=stops,
                           legs=legs if legs is not None else [_leg()], duration=duration)


def _pair(out_price=6223.0, ret_price=6223.0, **kw):
    out = _result(price=out_price, **kw)
    ret = _result(price=ret_price,
                  legs=[_leg(airline="IT", number="607", dep="PUS", arr="TPE",
                             dep_h=20, dep_m=50, arr_h=22, arr_m=30)])
    return (out, ret)


class NormalizeTests(unittest.TestCase):
    def test_strips_leading_underscore(self):
        """enum 名字不能以數字開頭，濟州航空是 `_7C`；底線不是代碼的一部分。"""
        self.assertEqual(normalize_airline("_7C"), "7C")
        self.assertEqual(normalize_airline("IT"), "IT")
        self.assertEqual(normalize_airline(""), "")


class PairConversionTests(unittest.TestCase):
    def _one(self, pair):
        offers, skipped = offers_from_pairs([pair], "TPE", "PUS", "TWD")
        return offers, skipped

    def test_happy_path_fills_both_legs(self):
        offers, skipped = self._one(_pair())
        self.assertEqual(skipped, [])
        o = offers[0]
        self.assertIsInstance(o, Offer)
        self.assertEqual((o.price, o.currency), (6223, "TWD"))
        self.assertEqual((o.depart_time, o.arrive_time), ("16:50", "20:00"))
        self.assertEqual((o.return_depart_time, o.return_arrive_time), ("20:50", "22:30"))
        self.assertEqual(o.flights, ["IT606"])
        self.assertEqual(o.return_flights, ["IT607"])
        self.assertTrue(o.nonstop)
        self.assertEqual(o.return_time_status, "ok")   # 有回程時間，不該標 unavailable
        self.assertEqual(o.section, "fli")

    def test_price_comes_from_return_leg_not_outbound(self):
        """去程 9,078 配回程 12,522 的組合，實際要付的是 12,522；取去程會低估。"""
        offers, _ = self._one(_pair(out_price=9078.0, ret_price=12522.0))
        self.assertEqual(offers[0].price, 12522)

    def test_none_price_is_rejected(self):
        offers, skipped = self._one(_pair(ret_price=None))
        self.assertEqual(offers, [])
        self.assertIn("return price is None", skipped[0])

    def test_non_positive_price_rejected(self):
        offers, skipped = self._one(_pair(ret_price=0))
        self.assertEqual(offers, [])
        self.assertIn("non-positive", skipped[0])

    def test_only_nonstop_accepted(self):
        offers, skipped = self._one(_pair(stops=1))
        self.assertEqual(offers, [])
        self.assertIn("only nonstop", skipped[0])

    def test_multi_leg_rejected_even_if_stops_says_zero(self):
        offers, skipped = self._one(_pair(legs=[_leg(), _leg()]))
        self.assertEqual(offers, [])
        self.assertIn("expected 1 leg", skipped[0])

    def test_missing_return_half_rejected(self):
        offers, skipped = offers_from_pairs([(_result(),)], "TPE", "PUS", "TWD")
        self.assertEqual(offers, [])
        self.assertIn("round-trip pair", skipped[0])

    def test_wrong_direction_rejected(self):
        """去程解析成 PUS→TPE 就不是我們要的那段，寧可不要。"""
        bad = (_result(legs=[_leg(dep="PUS", arr="TPE")]), _pair()[1])
        offers, skipped = offers_from_pairs([bad], "TPE", "PUS", "TWD")
        self.assertEqual(offers, [])
        self.assertIn("origin->dest", skipped[0])

    def test_seven_c_airline_normalized(self):
        pair = (_result(legs=[_leg(airline="_7C", number="6256")]),
                _result(legs=[_leg(airline="_7C", number="6255", dep="PUS", arr="TPE")]))
        offers, _ = offers_from_pairs([pair], "TPE", "PUS", "TWD")
        self.assertEqual(offers[0].flights, ["7C6256"])
        self.assertEqual(offers[0].airline, "7C")
        self.assertEqual(offers[0].return_flights, ["7C6255"])

    def test_good_pair_survives_alongside_bad_ones(self):
        offers, skipped = offers_from_pairs([_pair(ret_price=None), _pair(), _pair(stops=2)],
                                            "TPE", "PUS", "TWD")
        self.assertEqual(len(offers), 1)
        self.assertEqual(len(skipped), 2)


class FliFetcherTests(unittest.TestCase):
    def test_ok_result_carries_source_and_reason(self):
        f = FliFetcher(origin="TPE", dest="PUS", search=lambda *a: [_pair()])
        res = f.fetch(IT)
        self.assertEqual((res.source, res.status), ("fli", "ok"))
        self.assertEqual(res.min_price, 6223)
        self.assertIn("fli: 1 offers", res.reason)
        self.assertIn("google.com/travel/flights", res.url)

    def test_empty_results_raise_unusable(self):
        f = FliFetcher(search=lambda *a: [])
        with self.assertRaises(FliUnusable):
            f.fetch(IT)

    def test_all_pairs_unusable_raises_with_reason(self):
        f = FliFetcher(origin="TPE", dest="PUS", search=lambda *a: [_pair(ret_price=None)])
        with self.assertRaisesRegex(FliUnusable, "no_usable_offers"):
            f.fetch(IT)

    def test_search_is_called_with_itinerary_dates(self):
        seen = {}

        def spy(origin, dest, depart, return_, currency):
            seen.update(origin=origin, dest=dest, depart=depart, return_=return_, currency=currency)
            return [_pair()]

        FliFetcher(origin="TPE", dest="PUS", currency="TWD", search=spy).fetch(IT)
        self.assertEqual(seen, {"origin": "TPE", "dest": "PUS", "depart": "2027-03-15",
                                "return_": "2027-03-19", "currency": "TWD"})


class NotInstalledTests(unittest.TestCase):
    def test_default_search_reports_not_installed(self):
        """沒裝 fli 時要丟出可讀的 FliUnusable，不是 ImportError 往上炸。"""
        with mock.patch.dict("sys.modules", {"fli": None, "fli.models": None, "fli.search": None}):
            with self.assertRaisesRegex(FliUnusable, "fli_not_installed"):
                default_search("TPE", "PUS", "2027-03-15", "2027-03-19", "TWD")


class _RecordingFallback(Fetcher):
    name = "playwright"

    def __init__(self, result=None, exc=None):
        self.result, self.exc, self.calls = result, exc, 0

    def fetch(self, it):
        self.calls += 1
        if self.exc:
            raise self.exc
        return self.result or FetchResult(self.name, it.key, "ok", "http://pw", "2026-08-30T12:00:00+08:00",
                                          offers=[Offer(7000, "TWD", "IT", "Tigerair", ["IT606"], 0,
                                                        "16:50", "20:00", 130, "best")],
                                          reason="")


class FallbackTests(unittest.TestCase):
    def _wrap(self, search, fallback):
        return FliFirstFetcher(FliFetcher(origin="TPE", dest="PUS", search=search), fallback)

    def test_fli_success_skips_fallback(self):
        fb = _RecordingFallback()
        res = self._wrap(lambda *a: [_pair()], fb).fetch(IT)
        self.assertEqual(fb.calls, 0)
        self.assertEqual(res.source, "fli")
        self.assertEqual(res.min_price, 6223)

    def test_not_installed_falls_back(self):
        fb = _RecordingFallback()

        def missing(*a):
            raise FliUnusable("fli_not_installed: No module named 'fli'")

        res = self._wrap(missing, fb).fetch(IT)
        self.assertEqual(fb.calls, 1)
        self.assertEqual(res.source, "playwright")
        self.assertIn("fli_fallback[fli_not_installed", res.reason)

    def test_search_failure_falls_back(self):
        fb = _RecordingFallback()

        def boom(*a):
            raise FliUnusable("fli_search_failed: SearchHTTPError: 429")

        res = self._wrap(boom, fb).fetch(IT)
        self.assertEqual((fb.calls, res.source), (1, "playwright"))
        self.assertIn("fli_search_failed", res.reason)

    def test_unexpected_error_falls_back_instead_of_crashing(self):
        fb = _RecordingFallback()

        def weird(*a):
            raise RuntimeError("protobuf changed")

        res = self._wrap(weird, fb).fetch(IT)
        self.assertEqual((fb.calls, res.source), (1, "playwright"))
        self.assertIn("fli_unexpected: RuntimeError", res.reason)

    def test_bad_data_falls_back(self):
        """回了東西但價格是 None：不能用，要退回去而不是硬吞。"""
        fb = _RecordingFallback()
        res = self._wrap(lambda *a: [_pair(ret_price=None)], fb).fetch(IT)
        self.assertEqual((fb.calls, res.source), (1, "playwright"))
        self.assertIn("no_usable_offers", res.reason)

    def test_fallback_reason_is_preserved(self):
        fb = _RecordingFallback(result=FetchResult("playwright", IT.key, "unavailable", "http://pw",
                                                   "2026-08-30T12:00:00+08:00", reason="no_direct_offers"))
        res = self._wrap(lambda *a: [], fb).fetch(IT)
        self.assertIn("fli_fallback[", res.reason)
        self.assertIn("no_direct_offers", res.reason)

    def test_blocked_from_fallback_still_aborts(self):
        """被擋要中止整輪的規則不因為多包了一層而被吞掉。"""
        fb = _RecordingFallback(exc=BlockedError("HTTP 429"))
        with self.assertRaises(BlockedError):
            self._wrap(lambda *a: [], fb).fetch(IT)

    def test_name_shows_both_sources(self):
        self.assertEqual(self._wrap(lambda *a: [], _RecordingFallback()).name, "fli+playwright")


class SourceAvailabilityTests(unittest.TestCase):
    """fli 與 playwright 各自可有可無，四種組合都要有明確結果。

    「fli 不可用 → 有 playwright 才 fallback」由 FallbackTests 覆蓋，這裡不重複。
    """

    def _pw(self, render):
        return PlaywrightFetcher(origin="TPE", dest="PUS", render=render)

    @staticmethod
    def _no_playwright(*a, **kw):
        raise ImportError("No module named 'playwright'")

    def test_fli_works_without_playwright(self):
        """有 fli、沒 playwright：照樣走 fli，根本不該碰到 fallback。"""
        pw = self._pw(self._no_playwright)
        f = FliFirstFetcher(FliFetcher(origin="TPE", dest="PUS", search=lambda *a: [_pair()]), pw)
        res = f.fetch(IT)
        self.assertEqual((res.source, res.status), ("fli", "ok"))
        self.assertEqual(res.min_price, 6223)
        self.assertNotIn("playwright_unavailable", res.reason)

    def test_neither_source_available_is_explicit_error(self):
        """兩個都不可用：要是 error 且說得出兩邊各自為什麼，不能假裝成功。"""
        def missing_fli(*a):
            raise FliUnusable("fli_not_installed: No module named 'fli'")

        f = FliFirstFetcher(FliFetcher(origin="TPE", dest="PUS", search=missing_fli),
                            self._pw(self._no_playwright))
        res = f.fetch(IT)
        self.assertEqual(res.status, "error")
        self.assertEqual(res.offers, [])
        self.assertIsNone(res.min_price)
        self.assertIn("fli_not_installed", res.reason)          # fli 為什麼不能用
        self.assertIn("playwright_unavailable", res.reason)     # playwright 為什麼不能用


class NoSourceExitCodeTests(unittest.TestCase):
    def test_run_exits_nonzero_when_no_source_usable(self):
        """兩個來源都不可用時，排程要看得到非 0 離開碼。"""
        import io, tempfile
        from contextlib import redirect_stdout

        def missing_fli(*a):
            raise FliUnusable("fli_not_installed: No module named 'fli'")

        def missing_pw(*a, **kw):
            raise ImportError("No module named 'playwright'")

        def fetchers(_args):
            return {o: FliFirstFetcher(FliFetcher(origin=o, dest="PUS", search=missing_fli),
                                       PlaywrightFetcher(origin=o, dest="PUS", render=missing_pw))
                    for o in ("TPE", "KHH")}

        from fare_watch import cli
        with tempfile.TemporaryDirectory() as d, \
                mock.patch("fare_watch.cli.make_grouped_fetchers", side_effect=fetchers), \
                mock.patch("fare_watch.cli.jitter_sleep"), redirect_stdout(io.StringIO()):
            rc = cli.main(["--grouped", "--all-travelers", "--source", "fli",
                           "--dates", "2027-03-15", "--data-dir", d])
            summary = json.loads((pathlib.Path(d) / "grouped_latest.json").read_text(encoding="utf-8"))
        self.assertEqual(rc, 2)
        self.assertEqual(summary["counts"]["fetch_status"], {"error": 2})
        cell = summary["dates"][0]["fetch"]["TPE"]
        self.assertIn("playwright_unavailable", cell["reason"])
        self.assertEqual(summary["counts"]["dates_with_option"], 0)


class CliWiringTests(unittest.TestCase):
    def test_source_fli_wraps_playwright(self):
        from fare_watch import cli
        args = cli.build_parser().parse_args(["--grouped", "--all-travelers", "--source", "fli"])
        fetchers = cli.make_grouped_fetchers(args)
        self.assertEqual(set(fetchers), {"TPE", "KHH"})
        for origin, f in fetchers.items():
            self.assertIsInstance(f, FliFirstFetcher)
            self.assertEqual(f.name, "fli+playwright")
            self.assertEqual(f.fli.origin, origin)
            self.assertEqual(f.fallback.origin, origin)

    def test_source_google_playwright_is_unwrapped(self):
        """明確指定 google_playwright 就是純瀏覽器，不偷偷加 fli。"""
        from fare_watch import cli
        from fare_watch.playwright_fetcher import PlaywrightFetcher
        args = cli.build_parser().parse_args(["--source", "google_playwright"])
        self.assertIsInstance(cli.make_fetcher(args), PlaywrightFetcher)

    def test_dry_run_ignores_fli(self):
        from fare_watch import cli
        from fare_watch.fetchers import DryRunFetcher
        args = cli.build_parser().parse_args(["--source", "fli", "--dry-run"])
        self.assertIsInstance(cli.make_fetcher(args), DryRunFetcher)


if __name__ == "__main__":
    unittest.main()
