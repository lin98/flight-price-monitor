import datetime as dt
import unittest

from fare_watch.itineraries import build_itinerary, fewest_leave, format_date, generate


class ItineraryTests(unittest.TestCase):
    def test_window_and_count(self):
        itins = generate()
        self.assertEqual(itins[0].depart, dt.date(2027, 3, 1))
        self.assertEqual(itins[-1].return_, dt.date(2027, 5, 31))
        self.assertEqual(itins[-1].depart, dt.date(2027, 5, 27))
        self.assertEqual(len(itins), 88)  # 3/1..5/27
        self.assertEqual(len({i.key for i in itins}), 88)

    def test_month_filter(self):
        self.assertTrue(all(i.depart.month == 3 for i in generate([3])))
        self.assertEqual(len(generate([3])), 31)
        self.assertEqual(len(generate([5])), 27)

    def test_leave_days_around_qingming(self):
        it = build_itinerary(dt.date(2027, 4, 2))  # Fri → Tue 4/6
        self.assertEqual(it.leave_days, 1)
        self.assertEqual(it.leave_dates, (dt.date(2027, 4, 2),))
        self.assertIn("2027-04-05 民族掃墓節（清明節）", it.holidays_hit)

    def test_leave_days_plain_week(self):
        it = build_itinerary(dt.date(2027, 3, 8))  # Mon → Fri
        self.assertEqual(it.leave_days, 5)
        it = build_itinerary(dt.date(2027, 3, 1))  # 3/1 補假 Mon → Fri
        self.assertEqual(it.leave_days, 4)

    def test_fewest_leave(self):
        best = fewest_leave(generate())
        self.assertEqual([i.depart.isoformat() for i in best], ["2027-04-02", "2027-04-03"])
        self.assertTrue(all(i.leave_days == 1 for i in best))

    def test_to_dict(self):
        d = build_itinerary(dt.date(2027, 4, 30)).to_dict()
        self.assertEqual(d["key"], "2027-04-30_2027-05-04")
        self.assertEqual(d["leave_days"], 2)
        self.assertEqual(d["leave_dates"], ["2027-05-03", "2027-05-04"])


if __name__ == "__main__":
    unittest.main()


class FormatDateTests(unittest.TestCase):
    """顯示用的 `M/D(星期)`。canonical 的 ISO 欄位不經過這裡，見 CanonicalDateTests。"""

    def test_spec_examples(self):
        self.assertEqual(format_date("2027-03-11"), "3/11(四)")
        self.assertEqual(format_date("2027-03-15"), "3/15(一)")

    def test_trip_span_2027_03_11_to_03_15(self):
        """規格指定的那一趟：出發 3/11(四)、回程 3/15(一)。"""
        it = build_itinerary(dt.date(2027, 3, 11))
        self.assertEqual(it.return_, dt.date(2027, 3, 15))
        self.assertEqual((format_date(it.depart), format_date(it.return_)), ("3/11(四)", "3/15(一)"))

    def test_all_seven_weekdays(self):
        """2027-03-01 是星期一，連續七天要剛好走完一輪。"""
        got = [format_date(dt.date(2027, 3, 1) + dt.timedelta(days=i)) for i in range(7)]
        self.assertEqual(got, ["3/1(一)", "3/2(二)", "3/3(三)", "3/4(四)",
                               "3/5(五)", "3/6(六)", "3/7(日)"])

    def test_crosses_month_boundary(self):
        """5/28 出發、6/1 回程：月份要跟著跳，星期要各自算。"""
        it = build_itinerary(dt.date(2027, 5, 28))
        self.assertEqual((format_date(it.depart), format_date(it.return_)), ("5/28(五)", "6/1(二)"))
        self.assertEqual(format_date("2027-03-31"), "3/31(三)")
        self.assertEqual(format_date("2027-04-01"), "4/1(四)")

    def test_crosses_year_boundary(self):
        """跨年不能算錯：格式沒有年份，但星期必須是該年真正的星期。"""
        self.assertEqual(format_date("2026-12-31"), "12/31(四)")
        self.assertEqual(format_date("2027-01-01"), "1/1(五)")

    def test_leap_day(self):
        self.assertEqual(format_date("2028-02-29"), "2/29(二)")

    def test_accepts_date_object_and_iso_string_alike(self):
        self.assertEqual(format_date(dt.date(2027, 4, 4)), format_date("2027-04-04"))

    def test_no_zero_padding(self):
        """M/D 不補零：3/1 不是 03/01。"""
        self.assertEqual(format_date("2027-03-01"), "3/1(一)")


class CanonicalDateTests(unittest.TestCase):
    """顯示格式改了，資料層的 ISO 不能跟著改——URL、排序、去重全都吃它。"""

    def test_to_dict_stays_iso(self):
        d = build_itinerary(dt.date(2027, 3, 11)).to_dict()
        self.assertEqual(d["depart"], "2027-03-11")
        self.assertEqual(d["return"], "2027-03-15")
        self.assertEqual(d["key"], "2027-03-11_2027-03-15")
        for value in (d["depart"], d["return"], *d["leave_dates"]):
            self.assertRegex(value, r"^\d{4}-\d{2}-\d{2}$")

    def test_key_is_unchanged_by_formatting(self):
        it = build_itinerary(dt.date(2027, 3, 11))
        self.assertEqual(it.key, "2027-03-11_2027-03-15")
        self.assertNotIn("(", it.key)
