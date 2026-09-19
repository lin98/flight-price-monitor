"""分批掃描：把上一輪落地的結果讀回來、限制每輪抓幾格、報告標明涵蓋範圍。

整個視窗是 92 個出發日 × 2 個機場 = 184 格，用瀏覽器跑一格約 50 秒，一次跑完要三小時以上，
中途被擋就整輪白費。所以排程改成每輪只抓一小批（--max-fetches），報告靠 --merge-stored
把先前落地的結果併回來——這組測試就是在釘住「哪些格子是新抓的、哪些是沿用的、還缺哪些」。
"""

import datetime as dt
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from fare_watch import cli
from fare_watch.fetchers import BlockedError, FetchResult, Offer
from fare_watch.itineraries import build_itinerary
from fare_watch.storage import Storage

IT = build_itinerary(dt.date(2027, 3, 1))


def _offer(price=7000, flight="IT606", depart="16:50", arrive="20:00"):
    return Offer(price, "TWD", "IT", "Tigerair Taiwan", [flight], 0, depart, arrive, 130, "best",
                 return_depart_time="13:00", return_arrive_time="14:40", return_flights=["IT607"],
                 return_airline="IT", return_airline_name="Tigerair Taiwan", return_duration_min=160)


def _res(key, offers, at="2026-08-29T10:00:00+08:00", status="ok", reason=""):
    return FetchResult("google_playwright", key, status, "http://x", at, offers=offers, reason=reason)


class RehydrateTests(unittest.TestCase):
    def test_offer_round_trip_drops_derived_fields(self):
        o = _offer()
        back = Offer.from_dict(o.to_dict())
        self.assertEqual(back, o)
        self.assertEqual(back.time_tags, o.time_tags)

    def test_fetch_result_round_trip(self):
        r = _res(IT.key, [_offer(), _offer(8000, "BX586", "15:00", "18:25")])
        back = FetchResult.from_dict(r.to_dict())
        self.assertEqual(back.status, "ok")
        self.assertEqual(back.min_price, 7000)
        self.assertEqual([o.flights for o in back.offers], [["IT606"], ["BX586"]])
        self.assertIsNone(back.html)  # to_dict 不寫 html，還原後不能無中生有


class LatestRawTests(unittest.TestCase):
    def test_reads_back_newest_file_per_origin(self):
        with tempfile.TemporaryDirectory() as d:
            st = Storage(Path(d))
            st.save_raw(IT, _res(IT.key, [_offer(9000)], at="2026-08-29T10:00:00+08:00"), origin="TPE")
            st.save_raw(IT, _res(IT.key, [_offer(7000)], at="2026-08-29T22:00:00+08:00"), origin="TPE")
            st.save_raw(IT, _res(IT.key, [_offer(8600)], at="2026-08-29T21:00:00+08:00"), origin="KHH")
            got = st.latest_raw_by_origin([IT.key], ("TPE", "KHH"))
            self.assertEqual(got[IT.key]["TPE"].min_price, 7000)          # 取最新那份，不是最便宜那份
            self.assertEqual(got[IT.key]["TPE"].fetched_at, "2026-08-29T22:00:00+08:00")
            self.assertEqual(got[IT.key]["KHH"].min_price, 8600)

    def test_missing_returns_none_not_guess(self):
        with tempfile.TemporaryDirectory() as d:
            st = Storage(Path(d))
            self.assertIsNone(st.latest_raw(IT.key, "TPE"))
            self.assertEqual(st.latest_raw_by_origin([IT.key], ("TPE",)), {})


class DatesFlagTests(unittest.TestCase):
    def test_parses_and_sorts(self):
        self.assertEqual(cli.parse_dates("2027-04-15, 2027-03-01,2027-03-01"), ["2027-03-01", "2027-04-15"])

    def test_rejects_bad_and_out_of_window(self):
        for bad in ("2027-13-01", "20270401", "2027-06-01", "2026-03-01"):
            with self.assertRaises(Exception):
                cli.parse_dates(bad)

    def test_filters_itineraries(self):
        with tempfile.TemporaryDirectory() as d:
            buf = io.StringIO()
            with redirect_stdout(buf):
                cli.main(["--grouped", "--all-travelers", "--dry-run", "--json", "--data-dir", d,
                          "--dates", "2027-03-15,2027-05-31"])
            out = json.loads(buf.getvalue())
            self.assertEqual([x["depart"] for x in out["dates"]], ["2027-03-15", "2027-05-31"])
            self.assertEqual(out["dates"][-1]["return"], "2027-06-04")
            self.assertEqual(out["coverage"]["dates_in_report"], 2)
            self.assertEqual(out["coverage"]["window_dates_total"], 92)


def _fetcher(name="google_playwright", side_effect=None):
    f = mock.Mock()
    f.name = name
    f.fetch = mock.Mock(side_effect=side_effect)
    return f


class MaxFetchesTests(unittest.TestCase):
    """--max-fetches 只抓一小批：沒抓過的優先，其次資料最舊的。"""

    def _args(self, d, *extra, limit="3"):
        return cli.build_parser().parse_args(["--grouped", "--all-travelers", "--month", "3",
                                              "--limit", limit, "--data-dir", d, *extra])

    def test_caps_jobs_and_marks_rest_missing(self):
        with tempfile.TemporaryDirectory() as d, mock.patch("fare_watch.cli.jitter_sleep"):
            st = Storage(Path(d))
            args = self._args(d, "--max-fetches", "2")
            fetchers = {"TPE": _fetcher(side_effect=lambda it: _res(it.key, [_offer()])),
                        "KHH": _fetcher(side_effect=lambda it: _res(it.key, [_offer(8600, "BX586", "15:00", "18:25")]))}
            summary = cli.run_grouped_once(args, st, fetchers)
        self.assertEqual(summary["run"]["fetch_jobs"], 2)
        self.assertEqual(summary["counts"]["fetch_status"], {"ok": 2, "missing": 4})
        cov = summary["coverage"]
        self.assertEqual((cov["dates_fetched_this_run"], cov["dates_from_store"]), (2, 0))
        self.assertEqual(cov["dates_never_fetched"], 2)     # 3/2、3/3 這輪沒排到
        self.assertEqual(cov["dates_all_origins_ok"], 1)    # 只有 3/1 兩個機場都抓到
        # 兩格都給了 3/1（第一個日期的 TPE 與 KHH），不是散在不同日期
        self.assertEqual(summary["dates"][0]["fetch"]["TPE"]["provenance"], "this_run")
        self.assertEqual(summary["dates"][1]["fetch"]["TPE"]["provenance"], "none")
        self.assertEqual(summary["dates"][1]["fetch"]["TPE"]["reason"], "not fetched yet")

    def test_prefers_never_fetched_then_oldest(self):
        with tempfile.TemporaryDirectory() as d, mock.patch("fare_watch.cli.jitter_sleep"):
            st = Storage(Path(d))
            d1, d2 = build_itinerary(dt.date(2027, 3, 1)), build_itinerary(dt.date(2027, 3, 2))
            # 3/1 兩個機場都抓過（TPE 比較舊），3/2 只抓過 KHH → 這輪應該先補 3/2 TPE，再刷最舊的 3/1 TPE
            st.save_raw(d1, _res(d1.key, [_offer()], at="2026-08-20T10:00:00+08:00"), origin="TPE")
            st.save_raw(d1, _res(d1.key, [_offer()], at="2026-08-28T10:00:00+08:00"), origin="KHH")
            st.save_raw(d2, _res(d2.key, [_offer()], at="2026-08-27T10:00:00+08:00"), origin="KHH")
            args = self._args(d, "--max-fetches", "2", "--merge-stored", limit="2")
            seen = []

            def grab(origin):
                def _f(it):
                    seen.append((it.depart.isoformat(), origin))
                    return _res(it.key, [_offer()], at="2026-08-30T10:00:00+08:00")
                return _f

            cli.run_grouped_once(args, st, {"TPE": _fetcher(side_effect=grab("TPE")),
                                            "KHH": _fetcher(side_effect=grab("KHH"))})
        self.assertEqual(sorted(seen), [("2027-03-01", "TPE"), ("2027-03-02", "TPE")])


class MergeStoredTests(unittest.TestCase):
    def _seed(self, st, day, origin, price, at):
        it = build_itinerary(day)
        st.save_raw(it, _res(it.key, [_offer(price)], at=at), origin=origin)

    def test_stored_cells_fill_report_and_are_labelled(self):
        with tempfile.TemporaryDirectory() as d, mock.patch("fare_watch.cli.jitter_sleep"):
            st = Storage(Path(d))
            for origin in ("TPE", "KHH"):
                self._seed(st, dt.date(2027, 3, 2), origin, 9000, "2026-08-25T10:00:00+08:00")
            args = cli.build_parser().parse_args(["--grouped", "--all-travelers", "--month", "3", "--limit", "2",
                                                  "--data-dir", d, "--dates", "2027-03-01,2027-03-02",
                                                  "--max-fetches", "2", "--merge-stored"])
            summary = cli.run_grouped_once(args, st, {
                "TPE": _fetcher(side_effect=lambda it: _res(it.key, [_offer(7000)], at="2026-08-30T10:00:00+08:00")),
                "KHH": _fetcher(side_effect=lambda it: _res(it.key, [_offer(8600, "BX586", "15:00", "18:25")],
                                                            at="2026-08-30T10:00:00+08:00"))})
        by_date = {d_["depart"]: d_ for d_ in summary["dates"]}
        self.assertEqual(by_date["2027-03-01"]["fetch"]["TPE"]["provenance"], "this_run")
        self.assertEqual(by_date["2027-03-02"]["fetch"]["TPE"]["provenance"], "stored")
        self.assertEqual(by_date["2027-03-02"]["fetch"]["TPE"]["fetched_at"], "2026-08-25T10:00:00+08:00")
        # 沿用的資料照樣能湊組合，兩個日期都排得出來
        self.assertEqual(len(summary["ranked"]), 2)
        cov = summary["coverage"]
        self.assertEqual((cov["dates_fetched_this_run"], cov["dates_from_store"]), (2, 2))
        self.assertEqual(cov["dates_all_origins_ok"], 2)
        self.assertEqual(cov["by_origin"]["TPE"]["oldest_fetched_at"], "2026-08-25T10:00:00+08:00")
        self.assertEqual(cov["by_origin"]["TPE"]["newest_fetched_at"], "2026-08-30T10:00:00+08:00")
        self.assertEqual(cov["by_origin"]["TPE"]["provenance"], {"this_run": 1, "stored": 1})

    def test_block_does_not_clobber_stored_prices(self):
        """被擋要中止整輪，但沿用的舊價格是真的抓到過的，不該被 unavailable 蓋掉。"""
        with tempfile.TemporaryDirectory() as d, mock.patch("fare_watch.cli.jitter_sleep"):
            st = Storage(Path(d))
            for origin in ("TPE", "KHH"):
                self._seed(st, dt.date(2027, 3, 1), origin, 9000, "2026-08-25T10:00:00+08:00")
            args = cli.build_parser().parse_args(["--grouped", "--all-travelers", "--month", "3", "--limit", "2",
                                                  "--data-dir", d, "--merge-stored"])
            summary = cli.run_grouped_once(args, st, {
                "TPE": _fetcher(side_effect=BlockedError("HTTP 429")),
                "KHH": _fetcher(side_effect=BlockedError("HTTP 429"))})
        first = summary["dates"][0]["fetch"]
        self.assertEqual(first["TPE"]["status"], "ok")
        self.assertEqual(first["TPE"]["provenance"], "stored")
        self.assertIn("blocked", summary["run"]["aborted_reason"])
        # 沒有舊資料的 3/2 才標成 aborted_after_block
        self.assertEqual(summary["dates"][1]["fetch"]["TPE"]["reason"], "aborted_after_block")


class ExitCodeTests(unittest.TestCase):
    """排程看的是離開碼：被擋、或這輪抓的每一格都失敗才算要人處理。

    「分批掃描到現在還湊不出組合」不算異常——才抓半個視窗當然可能沒有組合，
    讓它回非 0 只會讓 launchd 每天亮紅燈，真正被擋的那天反而沒人注意。
    """

    def _run(self, d, side_effects, *extra):
        fetchers = {o: _fetcher(side_effect=side_effects[o]) for o in ("TPE", "KHH")}
        with mock.patch("fare_watch.cli.make_grouped_fetchers", return_value=fetchers), \
                mock.patch("fare_watch.cli.jitter_sleep"), redirect_stdout(io.StringIO()):
            rc = cli.main(["--grouped", "--all-travelers", "--month", "3", "--limit", "2",
                           "--data-dir", d, *extra])
        return rc, sum(f.fetch.call_count for f in fetchers.values())

    def test_blocked_run_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as d:
            rc, _ = self._run(d, {o: BlockedError("HTTP 429") for o in ("TPE", "KHH")})
        self.assertEqual(rc, 2)

    def test_all_fetches_unavailable_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as d:
            rc, _ = self._run(d, {o: (lambda it: _res(it.key, [], status="unavailable", reason="no_offers"))
                                  for o in ("TPE", "KHH")})
        self.assertEqual(rc, 2)

    def test_partial_scan_without_options_still_exits_zero(self):
        """這輪只排到一格、抓到了報價，但少了另一個機場所以湊不出三人組合 → 仍算成功。

        兩個機場都給 ok，所以不管排程先抓哪一個（同樣沒抓過時是照機場代碼排的，KHH 在前），
        結論都一樣：湊不出組合是因為視窗只掃了一半，不是抓取出問題。
        """
        with tempfile.TemporaryDirectory() as d:
            rc, calls = self._run(d, {"TPE": (lambda it: _res(it.key, [_offer()])),
                                      "KHH": (lambda it: _res(it.key, [_offer(8600, "BX586", "15:00", "18:25")]))},
                                  "--max-fetches", "1")
        self.assertEqual(calls, 1)   # --max-fetches 真的只抓了一格
        self.assertEqual(rc, 0)

    def test_single_failed_fetch_still_exits_nonzero(self):
        """對照組：這輪只排到一格而那一格失敗，就是「這輪抓的全失敗」，要亮紅燈。"""
        with tempfile.TemporaryDirectory() as d:
            rc, calls = self._run(d, {o: (lambda it: _res(it.key, [], status="error", reason="timeout"))
                                      for o in ("TPE", "KHH")}, "--max-fetches", "1")
        self.assertEqual(calls, 1)
        self.assertEqual(rc, 2)


class CoverageMarkdownTests(unittest.TestCase):
    def test_markdown_has_coverage_table(self):
        with tempfile.TemporaryDirectory() as d:
            with redirect_stdout(io.StringIO()):
                cli.main(["--grouped", "--all-travelers", "--dry-run", "--month", "5", "--data-dir", d])
            md = (Path(d) / "grouped_latest.md").read_text(encoding="utf-8")
        self.assertIn("## 涵蓋範圍", md)
        self.assertIn("全視窗 92 個出發日", md)
        self.assertIn("本報告 31 天（5/1(六) ～ 5/31(一)）", md)
        self.assertIn("| 機場 | 抓取狀態 | 資料來源 | 實際來源 | 有報價的日期 | 最舊 | 最新 |", md)


if __name__ == "__main__":
    unittest.main()
