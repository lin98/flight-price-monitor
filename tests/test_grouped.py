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
from fare_watch.grouped import (
    EXCLUDED_ORIGINS, GROUP_ORIGINS, TRAVELERS, _traveler_cell, build_grouped_summary, build_options,
    option_sort_key, origins_of, render_grouped_markdown, select_travelers,
)
from fare_watch.itineraries import WINDOW_END, build_itinerary, generate
from fare_watch.storage import Storage

IT = build_itinerary(dt.date(2027, 3, 1))


def _offer(price, flight, depart, arrive, airline="IT", stops=0, ret_depart="", ret_arrive="",
           ret_flight="", duration=130, ret_duration=0, airline_name=None):
    name = airline if airline_name is None else airline_name
    return Offer(price, "TWD", airline, name, [flight], stops, depart, arrive, duration, "best",
                 return_depart_time=ret_depart, return_arrive_time=ret_arrive,
                 return_flights=[ret_flight] if ret_flight else [],
                 return_airline=airline if ret_flight else "",
                 return_airline_name=name if ret_flight else "",
                 return_duration_min=ret_duration)


def _res(origin, offers, status="ok", reason=""):
    return FetchResult("google_flights", IT.key, status, f"http://x/{origin}", "2026-08-29T10:00:00+08:00",
                       offers=offers, reason=reason)


def _find(json_obj, needle):
    """遞迴檢查 JSON 內有沒有出現指定 key。"""
    if isinstance(json_obj, dict):
        return any(k == needle or _find(v, needle) for k, v in json_obj.items())
    if isinstance(json_obj, list):
        return any(_find(v, needle) for v in json_obj)
    return False


class WindowTests(unittest.TestCase):
    def test_default_window_unchanged(self):
        self.assertEqual(len(generate()), 88)

    def test_depart_through_end_of_may(self):
        itins = generate(depart_through=WINDOW_END)
        self.assertEqual(len(itins), 92)
        self.assertEqual(itins[0].depart, dt.date(2027, 3, 1))
        self.assertEqual(itins[-1].depart, dt.date(2027, 5, 31))
        self.assertEqual(itins[-1].return_, dt.date(2027, 6, 4))
        self.assertEqual(len(generate([5], depart_through=WINDOW_END)), 31)
        self.assertEqual(itins[-1].leave_days, 5)  # 5/31(一)～6/4(五) 沒有假日


class SpecTests(unittest.TestCase):
    def test_travelers_and_origins(self):
        by_id = {t.id: t.origins for t in TRAVELERS}
        self.assertEqual(by_id, {"a": ("TPE",), "b": ("TPE", "KHH"), "c": ("KHH",)})
        self.assertEqual(GROUP_ORIGINS, ("TPE", "KHH"))
        self.assertIn("RMQ", EXCLUDED_ORIGINS)
        self.assertFalse(set(GROUP_ORIGINS) & EXCLUDED_ORIGINS)


class OptionTests(unittest.TestCase):
    def test_traveler_b_can_pick_either_airport_and_gap_filter(self):
        tpe = _res("TPE", [_offer(7000, "IT606", "16:50", "20:00")])
        khh = _res("KHH", [_offer(8600, "BX586", "15:00", "18:25"), _offer(9200, "7C6256", "17:05", "20:35")])
        options, reason = build_options(IT, {"TPE": tpe, "KHH": khh}, 120)
        self.assertEqual(reason, "")
        # 旅客C只能 KHH：BX586 抵達 18:25 與 IT606 20:00 差 95 分可行；7C6256 20:35 差 35 分也可行
        gaps = sorted(o["arrival_gap_min"] for o in options)
        self.assertEqual(gaps[0], 35)
        self.assertTrue(all(o["arrival_gap_min"] <= 120 for o in options))
        best = options[0]
        self.assertEqual(best["arrival_gap_min"], 35)
        trav = {t["id"]: t for t in best["travelers"]}
        self.assertEqual(trav["a"]["origin"], "TPE")
        self.assertEqual(trav["c"]["origin"], "KHH")
        self.assertEqual(trav["c"]["flight"], "7C6256")
        self.assertIn(trav["b"]["origin"], ("TPE", "KHH"))
        # 旅客B在兩個機場都有選擇：至少有一個組合走 KHH、一個走 TPE
        self.assertEqual({t["origin"] for o in options for t in o["travelers"] if t["id"] == "b"},
                         {"TPE", "KHH"})

    def test_gap_too_wide_gives_reason(self):
        tpe = _res("TPE", [_offer(7000, "IT606", "16:50", "20:00")])
        khh = _res("KHH", [_offer(8600, "BX586", "08:00", "11:25")])
        options, reason = build_options(IT, {"TPE": tpe, "KHH": khh}, 120)
        self.assertEqual(options, [])
        self.assertEqual(reason, "arrival_gap_over_120")

    def test_missing_origin_gives_reason(self):
        tpe = _res("TPE", [_offer(7000, "IT606", "16:50", "20:00")])
        khh = _res("KHH", [], status="unavailable", reason="dynamic_content")
        options, reason = build_options(IT, {"TPE": tpe, "KHH": khh}, 120)
        self.assertEqual(options, [])
        self.assertEqual(reason, "no_offers_for:c")
        options, reason = build_options(IT, {"TPE": tpe}, 120)
        self.assertEqual(reason, "no_offers_for:c")

    def test_nonstop_only_and_unparseable_arrival_excluded(self):
        tpe = _res("TPE", [_offer(5000, "CI188", "10:00", "20:00", stops=1), _offer(7000, "IT606", "16:50", "20:00")])
        khh = _res("KHH", [_offer(8600, "BX586", "15:00", ""), _offer(9200, "7C6256", "17:05", "20:35")])
        options, _ = build_options(IT, {"TPE": tpe, "KHH": khh}, 120)
        flights = {t["flight"] for o in options for t in o["travelers"]}
        self.assertNotIn("CI188", flights)
        self.assertNotIn("BX586", flights)

    def test_duplicate_flight_keeps_cheapest(self):
        tpe = _res("TPE", [_offer(7500, "IT606", "16:50", "20:00"), _offer(7000, "IT606", "16:50", "20:00")])
        khh = _res("KHH", [_offer(9200, "7C6256", "17:05", "20:35")])
        options, _ = build_options(IT, {"TPE": tpe, "KHH": khh}, 120)
        fares = {t["fare"] for o in options for t in o["travelers"] if t["flight"] == "IT606"}
        self.assertEqual(fares, {7000})

    def test_sort_normal_then_gap_then_per_person_fare(self):
        it2 = build_itinerary(dt.date(2027, 3, 2))
        rows = [
            {"depart": "2027-03-03", "normal_hours": False, "arrival_gap_min": 0, "travelers": [{"fare": 1}] * 3},
            {"depart": "2027-03-02", "normal_hours": True, "arrival_gap_min": 30, "travelers": [{"fare": 9000}] * 3},
            {"depart": "2027-03-01", "normal_hours": True, "arrival_gap_min": 30,
             "travelers": [{"fare": 7000}, {"fare": 7000}, {"fare": 8600}]},
            {"depart": "2027-03-04", "normal_hours": True, "arrival_gap_min": 5, "travelers": [{"fare": 20000}] * 3},
        ]
        self.assertEqual([r["depart"] for r in sorted(rows, key=option_sort_key)],
                         ["2027-03-04", "2027-03-01", "2027-03-02", "2027-03-03"])
        self.assertEqual(it2.depart.isoformat(), "2027-03-02")

    def test_labels_red_eye_late_and_return_unavailable(self):
        tpe = _res("TPE", [_offer(7000, "IT600", "05:30", "09:00")])  # 紅眼
        khh = _res("KHH", [_offer(8600, "BX500", "05:45", "09:20")])
        options, _ = build_options(IT, {"TPE": tpe, "KHH": khh}, 120)
        best = options[0]
        self.assertFalse(best["normal_hours"])
        self.assertIn("red_eye", best["labels"])
        self.assertIn("return_time_unavailable", best["labels"])
        for t in best["travelers"]:
            self.assertEqual(t["return_time_status"], "return_time_unavailable")
            self.assertEqual(t["return_depart_time"], "")
        late_tpe = _res("TPE", [_offer(7000, "IT610", "21:30", "01:00")])
        late_khh = _res("KHH", [_offer(8600, "BX510", "21:40", "01:10")])
        options, _ = build_options(IT, {"TPE": late_tpe, "KHH": late_khh}, 120)
        self.assertIn("late_departure", options[0]["labels"])
        # 有回程時間才算 normal；不推估
        ok_tpe = _res("TPE", [_offer(7000, "IT606", "16:50", "20:00", ret_depart="12:00")])
        ok_khh = _res("KHH", [_offer(8600, "BX586", "17:00", "20:25", ret_depart="13:00")])
        options, _ = build_options(IT, {"TPE": ok_tpe, "KHH": ok_khh}, 120)
        self.assertTrue(options[0]["normal_hours"])
        self.assertEqual(options[0]["labels"], ["normal"])
        self.assertEqual(options[0]["travelers"][0]["return_time_status"], "ok")


class SummaryTests(unittest.TestCase):
    def _summary(self):
        it1, it2 = build_itinerary(dt.date(2027, 3, 1)), build_itinerary(dt.date(2027, 3, 2))
        results = {
            it1.key: {"TPE": _res("TPE", [_offer(7000, "IT606", "16:50", "20:00")]),
                      "KHH": _res("KHH", [_offer(8600, "BX586", "15:00", "18:25")])},
            it2.key: {"TPE": _res("TPE", [_offer(7000, "IT606", "16:50", "20:00")]),
                      "KHH": _res("KHH", [], status="unavailable", reason="dynamic_content")},
        }
        meta = {"run_id": "r1", "mode": "once", "source": "google_flights", "months": [3], "arrival_gap_min": 120,
                "started_at": "2026-08-29T10:00:00+08:00", "finished_at": "2026-08-29T10:00:05+08:00",
                "duration_sec": 5.0, "timezone": "Asia/Taipei", "aborted_reason": ""}
        return build_grouped_summary(meta, [it1, it2], results, 120)

    def test_summary_shape_and_no_total(self):
        s = self._summary()
        self.assertEqual(s["counts"]["dates"], 2)
        self.assertEqual(s["counts"]["dates_with_option"], 1)
        self.assertEqual(s["counts"]["fetch_status"], {"ok": 3, "unavailable": 1})
        self.assertEqual(len(s["ranked"]), 1)
        row = s["ranked"][0]
        self.assertEqual(row["depart"], "2027-03-01")
        self.assertEqual(row["return"], "2027-03-05")
        self.assertEqual(row["leave_days"], 4)
        self.assertEqual(row["arrival_gap_min"], 95)
        self.assertEqual([t["origin"] for t in row["travelers"]], ["TPE", "TPE", "KHH"])
        self.assertEqual([t["fare"] for t in row["travelers"]], [7000, 7000, 8600])
        self.assertEqual(s["dates"][1]["no_option_reason"], "no_offers_for:c")
        self.assertEqual(s["dates"][1]["fetch"]["KHH"]["status"], "unavailable")
        self.assertEqual(s["excluded_origins"], ["RMQ"])
        for needle in ("total", "total_price", "group_total", "sum"):
            self.assertFalse(_find(s, needle), needle)
        blob = json.dumps(s, ensure_ascii=False)
        self.assertNotIn("22600", blob)  # 7000+7000+8600 不得出現在任何地方
        self.assertNotIn("合計", blob)

    def test_markdown(self):
        md = render_grouped_markdown(self._summary())
        self.assertIn("三人分組比價", md)
        self.assertIn("| 旅客A | 旅客B | 旅客C |", md)
        # 每位旅客的兩段航程都要自帶日期（取自行程），回程時刻缺漏也不影響日期與航線
        self.assertIn("去程 3/1(一) IT606 IT TPE 16:50 → PUS 20:00（2h10m）<br>"
                      "回程 3/5(五) 班機號未提供 航空公司未提供 PUS → TPE return_time_unavailable<br>"
                      "每人來回票價 7,000 TWD（去回程整筆，來源未提供單程價）", md)
        self.assertIn("去程 3/1(一) BX586 IT KHH 15:00 → PUS 18:25（2h10m）<br>"
                      "回程 3/5(五) 班機號未提供 航空公司未提供 PUS → KHH return_time_unavailable<br>"
                      "每人來回票價 8,600 TWD（去回程整筆，來源未提供單程價）", md)
        self.assertIn("no_offers_for:c", md)
        self.assertNotIn("合計", md)
        self.assertNotIn("22,600", md)


class TravelerDateTests(unittest.TestCase):
    """每位旅客的航班資訊必須明確帶日期，且日期只能來自 Itinerary。"""

    def _row(self, it, tpe_offer, khh_offer):
        options, reason = build_options(it, {"TPE": _res("TPE", [tpe_offer]), "KHH": _res("KHH", [khh_offer])}, 120)
        self.assertEqual(reason, "")
        return options[0]

    def test_json_travelers_carry_itinerary_dates(self):
        it = build_itinerary(dt.date(2027, 3, 1))
        row = self._row(it, _offer(7000, "IT606", "16:50", "20:00"), _offer(8600, "BX586", "15:00", "18:25"))
        for t in row["travelers"]:
            self.assertEqual(t["depart_date"], it.depart.isoformat())
            self.assertEqual(t["return_date"], it.return_.isoformat())
            self.assertEqual(t["return_origin"], "PUS")
            self.assertEqual(t["return_dest"], t["origin"])
            self.assertEqual(t["dest"], "PUS")

    def test_dates_follow_itinerary_not_offer_times(self):
        """換一個行程日，時刻不變但日期必須跟著行程走。"""
        offers = (_offer(7000, "IT606", "16:50", "20:00"), _offer(8600, "BX586", "15:00", "18:25"))
        it = build_itinerary(dt.date(2027, 4, 20))
        row = self._row(it, *offers)
        dates = {(t["depart_date"], t["return_date"]) for t in row["travelers"]}
        self.assertEqual(dates, {("2027-04-20", "2027-04-24")})

    def test_overnight_leg_keeps_its_date(self):
        """回程 22:30→00:05 跨日：仍標行程的回程日，不推算抵達日、也不把日期拿掉。"""
        it = build_itinerary(dt.date(2027, 3, 1))
        row = self._row(it,
                        _offer(7000, "IT606", "16:50", "20:00", ret_depart="22:30", ret_arrive="00:05",
                               ret_flight="IT607", ret_duration=155),
                        _offer(8600, "BX586", "15:00", "18:25", ret_depart="22:40", ret_arrive="00:15",
                               ret_flight="BX585", ret_duration=155))
        traveler_a = next(t for t in row["travelers"] if t["id"] == "a")
        self.assertEqual(traveler_a["return_date"], "2027-03-05")
        self.assertIn("回程 3/5(五) IT607 IT PUS 22:30 → TPE 00:05（2h35m）", _traveler_cell(traveler_a))

    def test_cell_shows_both_legs_with_dates_airline_flight_and_fare(self):
        it = build_itinerary(dt.date(2027, 3, 1))
        row = self._row(it,
                        _offer(9800, "KE2086", "17:30", "20:45", airline="KE", airline_name="大韓航空",
                               duration=135, ret_depart="14:50", ret_arrive="16:30", ret_flight="KE2085",
                               ret_duration=160),
                        _offer(8600, "BX586", "15:35", "19:00", ret_depart="13:00", ret_arrive="14:35",
                               ret_flight="BX585", ret_duration=155))
        traveler_a = next(t for t in row["travelers"] if t["id"] == "a")
        self.assertEqual(
            _traveler_cell(traveler_a),
            "去程 3/1(一) KE2086 大韓航空 TPE 17:30 → PUS 20:45（2h15m）<br>"
            "回程 3/5(五) KE2085 大韓航空 PUS 14:50 → TPE 16:30（2h40m）<br>"
            "每人來回票價 9,800 TWD（去回程整筆，來源未提供單程價）")

    def test_return_airline_is_not_inherited_from_outbound(self):
        """去 KE、回 CI：回程要顯示回程自己的航空公司與班機號，不能沿用去程那一家。"""
        it = build_itinerary(dt.date(2027, 3, 11))
        out = _offer(9078, "KE2086", "17:30", "20:45", airline="KE", airline_name="大韓航空",
                     duration=135, ret_depart="19:50", ret_arrive="21:30", ret_flight="CI187",
                     ret_duration=160)
        out.return_airline, out.return_airline_name = "CI", "中華航空"
        row = self._row(it, out, _offer(8600, "BX586", "17:05", "20:35", ret_depart="14:05",
                                        ret_arrive="16:05", ret_flight="BX585", ret_duration=180))
        cell = _traveler_cell(next(t for t in row["travelers"] if t["id"] == "a"))
        self.assertIn("去程 3/11(四) KE2086 大韓航空 TPE 17:30 → PUS 20:45", cell)
        self.assertIn("回程 3/15(一) CI187 中華航空 PUS 19:50 → TPE 21:30", cell)
        self.assertEqual(cell.count("大韓航空"), 1)   # 沒有被沿用到回程那一段
        self.assertIn("每人來回票價 9,078 TWD", cell)

    def test_each_traveler_can_show_a_different_fare(self):
        """三人各自買票：每個人的票價分別顯示，不加總。"""
        it = build_itinerary(dt.date(2027, 3, 11))
        row = self._row(it,
                        _offer(9078, "KE2086", "17:30", "20:45", airline="KE", airline_name="大韓航空",
                               ret_depart="14:50", ret_arrive="16:30", ret_flight="KE2085"),
                        _offer(8688, "7C6256", "17:05", "20:35", airline="7C", airline_name="濟州航空",
                               ret_depart="14:05", ret_arrive="16:05", ret_flight="7C6255"))
        cells = [_traveler_cell(t) for t in row["travelers"]]
        self.assertIn("每人來回票價 9,078 TWD", cells[0])          # 旅客A走 TPE
        self.assertIn("每人來回票價 8,688 TWD", cells[2])          # 旅客C走 KHH
        self.assertNotIn("17,766", "".join(cells))                # 9,078+8,688 不得出現
        self.assertNotIn("26,454", "".join(cells))                # 三人總價不得出現


class DefaultTravelerAOnlyTests(unittest.TestCase):
    """預設只找旅客A：JSON / Markdown / CLI 三個出口都不該出現旅客B、旅客C、KHH。

    三人分組的邏輯完全保留，用 --all-travelers 打開（見 GroupedCliTests）。
    """

    def test_select_travelers_default_and_optin(self):
        self.assertEqual([t.id for t in select_travelers()], ["a"])
        self.assertEqual([t.id for t in select_travelers(True)], ["a", "b", "c"])
        self.assertEqual(origins_of(select_travelers()), ("TPE",))
        self.assertEqual(origins_of(select_travelers(True)), ("TPE", "KHH"))

    def test_default_only_fetches_tpe(self):
        args = cli.build_parser().parse_args(["--grouped", "--dry-run"])
        self.assertFalse(args.all_travelers)
        self.assertEqual(set(cli.make_grouped_fetchers(args)), {"TPE"})

    def test_default_json_and_markdown_have_no_khh_or_other_travelers(self):
        with tempfile.TemporaryDirectory() as d:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli.main(["--grouped", "--dry-run", "--json", "--data-dir", d,
                               "--dates", "2027-03-11"])
            out = json.loads(buf.getvalue())
            md = (Path(d) / "grouped_latest.md").read_text(encoding="utf-8")
        self.assertEqual(rc, 0)
        self.assertEqual(out["origins"], ["TPE"])
        self.assertEqual([t["id"] for t in out["travelers"]], ["a"])
        self.assertEqual(out["run"]["travelers"], ["a"])
        self.assertFalse(out["run"]["all_travelers"])
        self.assertEqual(out["counts"]["fetch_status"], {"dry_run": 1})     # 一個日期只查一次
        self.assertEqual(set(out["dates"][0]["fetch"]), {"TPE"})
        self.assertEqual(set(out["coverage"]["by_origin"]), {"TPE"})
        for banned in ("旅客B", "旅客C", "KHH"):
            self.assertNotIn(banned, md)
            self.assertNotIn(banned, json.dumps(out, ensure_ascii=False))
        self.assertIn("# 旅客A機票比價（TPE → PUS，僅直飛）", md)
        self.assertIn("| 出發 | 回程 | 請假 | 原因 | TPE 狀態 | 查詢 |", md)   # 只剩一欄機場狀態

    def test_default_single_traveler_still_ranks_and_shows_both_legs(self):
        it = build_itinerary(dt.date(2027, 3, 11))
        offer = _offer(9078, "KE2086", "17:30", "20:45", airline="KE", airline_name="大韓航空",
                       duration=135, ret_depart="14:50", ret_arrive="16:30", ret_flight="KE2085",
                       ret_duration=160)
        meta = {"run_id": "r1", "mode": "once", "source": "fli", "months": [3], "arrival_gap_min": 120,
                "started_at": "x", "finished_at": "y", "duration_sec": 1.0, "timezone": "Asia/Taipei"}
        summary = build_grouped_summary(meta, [it], {it.key: {"TPE": _res("TPE", [offer])}}, 120,
                                        travelers=select_travelers())
        self.assertEqual(len(summary["ranked"]), 1)
        best = summary["ranked"][0]
        self.assertEqual([t["label"] for t in best["travelers"]], ["旅客A"])
        self.assertEqual(best["arrival_gap_min"], 0)      # 只有一個人，沒有會合問題
        md = render_grouped_markdown(summary)
        self.assertIn("| # | 出發 | 回程 | 請假 | 時段 | 抵達差(分) | 旅客A | 標籤 | 查詢 |", md)
        self.assertIn("去程 3/11(四) KE2086 大韓航空 TPE 17:30 → PUS 20:45", md)
        self.assertIn("回程 3/15(一) KE2085 大韓航空 PUS 14:50 → TPE 16:30", md)
        self.assertIn("每人來回票價 9,078 TWD", md)

    def test_default_cli_summary_only_shows_traveler_a(self):
        it = build_itinerary(dt.date(2027, 3, 11))
        offer = _offer(9078, "KE2086", "17:30", "20:45", airline="KE", airline_name="大韓航空",
                       ret_depart="14:50", ret_arrive="16:30", ret_flight="KE2085")
        meta = {"run_id": "r1", "mode": "once", "source": "fli", "months": [3], "arrival_gap_min": 120,
                "started_at": "x", "finished_at": "y", "duration_sec": 1.0, "timezone": "Asia/Taipei",
                "report_md": "/tmp/x.md"}
        summary = build_grouped_summary(meta, [it], {it.key: {"TPE": _res("TPE", [offer])}}, 120,
                                        travelers=select_travelers())
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.print_grouped_summary(summary, as_json=False)
        out = buf.getvalue()
        self.assertIn("旅客A（TPE）", out)
        for banned in ("旅客B", "旅客C", "KHH"):
            self.assertNotIn(banned, out)


class CliPrintTests(unittest.TestCase):
    """終端摘要也要完整揭示去回程，不能只有 Markdown 有。"""

    def _summary_with_different_airlines(self):
        it = build_itinerary(dt.date(2027, 3, 11))
        tpe = _offer(9078, "KE2086", "17:30", "20:45", airline="KE", airline_name="大韓航空",
                     duration=135, ret_depart="19:50", ret_arrive="21:30", ret_flight="CI187",
                     ret_duration=160)
        tpe.return_airline, tpe.return_airline_name = "CI", "中華航空"
        khh = _offer(8688, "7C6256", "17:05", "20:35", airline="7C", airline_name="濟州航空",
                     duration=150, ret_depart="14:05", ret_arrive="16:05", ret_flight="7C6255",
                     ret_duration=180)
        meta = {"run_id": "r1", "mode": "once", "source": "fli", "months": [3], "arrival_gap_min": 120,
                "started_at": "2026-08-29T10:00:00+08:00", "finished_at": "2026-08-29T10:00:05+08:00",
                "duration_sec": 5.0, "timezone": "Asia/Taipei", "report_md": "/tmp/x.md"}
        results = {it.key: {"TPE": _res("TPE", [tpe]), "KHH": _res("KHH", [khh])}}
        return build_grouped_summary(meta, [it], results, 120)

    def test_cli_prints_both_legs_with_every_field(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.print_grouped_summary(self._summary_with_different_airlines(), as_json=False)
        out = buf.getvalue()

        self.assertIn("3/11(四)~3/15(一)", out)                 # 出發／回程日都帶星期
        # 旅客A：去 KE、回 CI，兩段的班機號／航空公司／機場／時刻各自完整
        self.assertIn("去程 3/11(四) KE2086 大韓航空 TPE 17:30 → PUS 20:45", out)
        self.assertIn("回程 3/15(一) CI187 中華航空 PUS 19:50 → TPE 21:30", out)
        # 旅客C：另一家航空、另一個票價
        self.assertIn("去程 3/11(四) 7C6256 濟州航空 KHH 17:05 → PUS 20:35", out)
        self.assertIn("回程 3/15(一) 7C6255 濟州航空 PUS 14:05 → KHH 16:05", out)
        self.assertIn("每人來回票價 9,078 TWD", out)
        self.assertIn("每人來回票價 8,688 TWD", out)
        for label in ("旅客A", "旅客B", "旅客C"):
            self.assertIn(label, out)
        self.assertNotIn("合計", out)
        self.assertNotIn("26,844", out)   # 三人加總不得出現

    def test_cli_json_mode_is_untouched(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.print_grouped_summary(self._summary_with_different_airlines(), as_json=True)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["ranked"][0]["depart"], "2027-03-11")   # JSON 仍是 ISO


class TravelerSchemaTests(unittest.TestCase):
    def test_traveler_row_keys_unchanged(self):
        """顯示層改寫不得動到 JSON schema——下游與 raw 重解析都吃這些欄位。"""
        it = build_itinerary(dt.date(2027, 3, 11))
        results = {it.key: {"TPE": _res("TPE", [_offer(9078, "KE2086", "17:30", "20:45")]),
                            "KHH": _res("KHH", [_offer(8688, "7C6256", "17:05", "20:35")])}}
        meta = {"run_id": "r1", "mode": "once", "source": "fli", "months": [3], "arrival_gap_min": 120,
                "started_at": "x", "finished_at": "y", "duration_sec": 1.0, "timezone": "Asia/Taipei"}
        t = build_grouped_summary(meta, [it], results, 120)["ranked"][0]["travelers"][0]
        self.assertEqual(set(t), {
            "id", "label", "origin", "dest", "depart_date", "airline", "airline_name", "flight", "flights",
            "depart_time", "arrive_time", "duration_min", "fare", "currency", "labels",
            "return_date", "return_origin", "return_dest", "return_depart_time", "return_arrive_time",
            "return_airline", "return_airline_name", "return_flight", "return_flights",
            "return_stops", "return_duration_min", "return_time_status"})
        self.assertEqual(t["depart_date"], "2027-03-11")     # ISO，不是 3/11(四)
        self.assertEqual(t["return_date"], "2027-03-15")
        self.assertIsInstance(t["fare"], int)


class GroupedCliTests(unittest.TestCase):
    def test_dry_run_writes_grouped_latest(self):
        with tempfile.TemporaryDirectory() as d:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli.main(["--grouped", "--all-travelers", "--dry-run", "--month", "5",
                               "--json", "--data-dir", d])
            self.assertEqual(rc, 0)
            out = json.loads(buf.getvalue())
            self.assertEqual(out["run"]["mode"], "dry-run")
            self.assertEqual(out["counts"]["dates"], 31)
            self.assertEqual(out["counts"]["dates_with_option"], 0)
            self.assertEqual(out["counts"]["fetch_status"], {"dry_run": 62})
            self.assertEqual(out["dates"][-1]["depart"], "2027-05-31")
            self.assertEqual(out["dates"][-1]["return"], "2027-06-04")
            self.assertTrue((Path(d) / "grouped_latest.json").exists())
            self.assertTrue((Path(d) / "grouped_latest.md").exists())
            saved = json.loads((Path(d) / "grouped_latest.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["counts"]["dates"], 31)
            self.assertTrue(all("RMQ" not in f for f in (Path(d) / "grouped_latest.md").read_text().split("\n")
                                if "google.com" in f))

    def test_fetches_both_origins_and_aborts_on_block(self):
        it0 = build_itinerary(dt.date(2027, 3, 1))
        tpe, khh = mock.Mock(), mock.Mock()
        tpe.name = khh.name = "google_flights"
        tpe.fetch = mock.Mock(side_effect=[_res("TPE", [_offer(7000, "IT606", "16:50", "20:00")]), BlockedError("HTTP 429")])
        khh.fetch = mock.Mock(side_effect=[_res("KHH", [_offer(8600, "BX586", "15:00", "18:25")])])
        with tempfile.TemporaryDirectory() as d, mock.patch("fare_watch.cli.jitter_sleep"):
            args = cli.build_parser().parse_args(["--grouped", "--all-travelers", "--month", "3",
                                                  "--limit", "3", "--data-dir", d])
            st = Storage(Path(d))
            summary = cli.run_grouped_once(args, st, {"TPE": tpe, "KHH": khh})
            obs = st.load_observations()
            self.assertTrue((Path(d) / "raw" / "TPE" / it0.key).is_dir())
            self.assertTrue((Path(d) / "raw" / "KHH" / it0.key).is_dir())
        self.assertEqual(summary["counts"]["dates"], 3)
        self.assertEqual(summary["counts"]["dates_with_option"], 1)
        self.assertEqual(summary["counts"]["fetch_status"], {"ok": 2, "unavailable": 4})
        self.assertIn("blocked", summary["run"]["aborted_reason"])
        self.assertEqual(tpe.fetch.call_count, 2)
        self.assertEqual(khh.fetch.call_count, 1)
        self.assertEqual({o["origin"] for o in obs}, {"TPE", "KHH"})
        self.assertEqual(summary["ranked"][0]["depart"], "2027-03-01")

    def test_markdown_shows_weekday_while_json_and_url_stay_iso(self):
        """同一輪產出：Markdown 用 M/D(星期)，JSON 與查詢 URL 維持 ISO。"""
        with tempfile.TemporaryDirectory() as d:
            with redirect_stdout(io.StringIO()):
                cli.main(["--grouped", "--all-travelers", "--dry-run", "--data-dir", d,
                          "--dates", "2027-03-11,2027-05-31"])
            md = (Path(d) / "grouped_latest.md").read_text(encoding="utf-8")
            saved = json.loads((Path(d) / "grouped_latest.json").read_text(encoding="utf-8"))

        # Markdown：出發日與回程日都帶星期
        self.assertIn("| 3/11(四) | 3/15(一) |", md)
        self.assertIn("| 5/31(一) | 6/4(五) |", md)
        self.assertIn("本報告 2 天（3/11(四) ～ 5/31(一)）", md)

        # JSON：canonical 欄位仍是 ISO，沒有被塞進星期
        self.assertEqual([e["depart"] for e in saved["dates"]], ["2027-03-11", "2027-05-31"])
        self.assertEqual([e["return"] for e in saved["dates"]], ["2027-03-15", "2027-06-04"])
        self.assertEqual(saved["dates"][0]["key"], "2027-03-11_2027-03-15")
        self.assertEqual(saved["coverage"]["depart_first"], "2027-03-11")
        for e in saved["dates"]:
            self.assertNotIn("(", e["depart"] + e["return"])

        # 查詢 URL 仍用 ISO，星期沒有漏進 querystring
        url = saved["dates"][0]["fetch"]["TPE"]["url"]
        self.assertIn("2027-03-11", url)
        self.assertIn("2027-03-15", url)
        for bad in ("(", "%28", "四", "一"):
            self.assertNotIn(bad, url)

    def test_grouped_ignores_origin_flag(self):
        args = cli.build_parser().parse_args(["--grouped", "--all-travelers", "--origin", "KHH", "--dry-run"])
        fetchers = cli.make_grouped_fetchers(args)
        self.assertEqual(set(fetchers), {"TPE", "KHH"})
        self.assertEqual(fetchers["TPE"].origin, "TPE")
        self.assertEqual(fetchers["KHH"].origin, "KHH")


class StorageOriginTests(unittest.TestCase):
    def test_observation_dedupe_is_per_origin(self):
        with tempfile.TemporaryDirectory() as d:
            st = Storage(Path(d))
            r = _res("TPE", [_offer(7000, "IT606", "16:50", "20:00")])
            self.assertTrue(st.append_observation(IT, r, "r1", origin="TPE"))
            self.assertTrue(st.append_observation(IT, r, "r1", origin="KHH"))   # 不同機場不是重複
            self.assertFalse(st.append_observation(IT, r, "r2", origin="TPE"))  # 同機場同價才去重
            self.assertTrue(st.append_observation(IT, r, "r3"))                 # 舊格式（無 origin）互不干擾
            self.assertEqual(len(st.load_observations()), 3)


if __name__ == "__main__":
    unittest.main()
