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


def _ok(it, price):
    return FetchResult("google_flights", it.key, "ok", "http://x", "2026-08-29T10:00:00+08:00",
                       offers=[Offer(price, "TWD", "IT", "台灣虎航", ["IT606"], 0, "16:50", "20:00", 130, "best")],
                       raw_block=[1, 2])


class StorageTests(unittest.TestCase):
    def test_dedupe_observations(self):
        with tempfile.TemporaryDirectory() as d:
            st = Storage(Path(d))
            it = build_itinerary(dt.date(2027, 3, 1))
            self.assertTrue(st.append_observation(it, _ok(it, 7623), "r1"))
            self.assertFalse(st.append_observation(it, _ok(it, 7623), "r2"))   # 同日同價 → 去重
            self.assertTrue(st.append_observation(it, _ok(it, 7000), "r3"))    # 價格變了 → 新紀錄
            self.assertEqual(len(st.load_observations()), 2)

    def test_save_raw_keeps_html_only_on_failure(self):
        with tempfile.TemporaryDirectory() as d:
            st = Storage(Path(d))
            it = build_itinerary(dt.date(2027, 3, 1))
            ok = _ok(it, 7623)
            ok.html = "<html>"
            p = st.save_raw(it, ok)
            self.assertTrue(p.exists())
            self.assertEqual(list(p.parent.glob("*.html.gz")), [])
            bad = FetchResult("google_flights", it.key, "unavailable", "http://x", "2026-08-29T10:00:01+08:00",
                              reason="dynamic_content", html="<html>")
            st.save_raw(it, bad)
            self.assertEqual(len(list(p.parent.glob("*.html.gz"))), 1)


class CliTests(unittest.TestCase):
    def test_dry_run_json(self):
        with tempfile.TemporaryDirectory() as d:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli.main(["--once", "--dry-run", "--month", "4", "--json", "--data-dir", d])
            self.assertEqual(rc, 0)
            out = json.loads(buf.getvalue())
            self.assertEqual(out["run"]["mode"], "dry-run")
            self.assertEqual(out["counts"]["itineraries"], 30)
            self.assertEqual(out["counts"]["status"], {"dry_run": 30})
            self.assertEqual(out["min_leave_days"], 1)
            self.assertEqual([r["depart"] for r in out["fewest_leave"]], ["2027-04-02", "2027-04-03"])
            self.assertTrue((Path(d) / "reports" / "fare_watch_latest.md").exists())
            self.assertTrue((Path(d) / "reports" / "fare_watch_latest.json").exists())
            self.assertFalse((Path(d) / "observations.jsonl").exists())

    def test_month_validation(self):
        with self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["--month", "6"])
        self.assertEqual(cli.parse_months("3,5"), [3, 5])

    def test_block_aborts_run_and_marks_rest_unavailable(self):
        fetcher = mock.Mock()
        fetcher.name = "google_flights"
        it0 = build_itinerary(dt.date(2027, 3, 1))
        fetcher.fetch = mock.Mock(side_effect=[_ok(it0, 7623), BlockedError("HTTP 429")])
        with tempfile.TemporaryDirectory() as d, mock.patch("fare_watch.cli.jitter_sleep"):
            args = cli.build_parser().parse_args(["--once", "--month", "3", "--limit", "4", "--data-dir", d])
            summary = cli.run_once(args, Storage(Path(d)), fetcher)
        self.assertEqual(summary["counts"]["priced"], 1)
        self.assertEqual(summary["counts"]["status"], {"ok": 1, "unavailable": 3})
        self.assertIn("blocked", summary["run"]["aborted_reason"])
        self.assertEqual(fetcher.fetch.call_count, 2)


if __name__ == "__main__":
    unittest.main()
