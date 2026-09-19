import datetime as dt
import unittest

from fare_watch.holidays_tw import HOLIDAY_BY_DATE, HOLIDAYS_2027, holidays_between, is_holiday, is_workday


class HolidayTests(unittest.TestCase):
    def test_official_holidays_in_window(self):
        self.assertTrue(is_holiday(dt.date(2027, 3, 1)))   # 228 逢週日補假
        self.assertTrue(is_holiday(dt.date(2027, 4, 5)))   # 清明
        self.assertTrue(is_holiday(dt.date(2027, 4, 6)))   # 兒童節逢週日補假
        self.assertTrue(is_holiday(dt.date(2027, 4, 30)))  # 勞動節逢週六補假
        self.assertFalse(is_holiday(dt.date(2027, 3, 2)))

    def test_every_entry_has_official_basis(self):
        self.assertTrue(all(h.basis in ("official_listed", "official_rule") for h in HOLIDAYS_2027))
        # 3/1 補假是依官方補假規則推得，新聞稿沒逐字列出，必須標示清楚
        self.assertEqual(HOLIDAY_BY_DATE[dt.date(2027, 3, 1)].basis, "official_rule")

    def test_workday_excludes_weekend_and_holiday(self):
        self.assertTrue(is_workday(dt.date(2027, 3, 2)))    # Tue
        self.assertFalse(is_workday(dt.date(2027, 3, 6)))   # Sat
        self.assertFalse(is_workday(dt.date(2027, 3, 1)))   # Mon holiday

    def test_holidays_between(self):
        names = [h.name for h in holidays_between(dt.date(2027, 4, 1), dt.date(2027, 4, 30))]
        self.assertEqual(names, ["兒童節", "民族掃墓節（清明節）", "兒童節補假", "勞動節補假"])


if __name__ == "__main__":
    unittest.main()
