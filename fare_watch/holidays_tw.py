"""2027 年台灣政府行政機關放假資料（僅涵蓋本工具需要的 3–5 月，含前後緩衝）。

官方來源：行政院人事行政總處 115-05-21 新聞稿「行政院核定116年（西元2027年）
政府行政機關辦公日曆表」<https://www.dgpa.gov.tw/information?pid=12983&uid=82>
（2026-08-29 重新查閱；新聞稿全文另見中央社轉載
<https://www.cna.com.tw/postwrite/chi/434121>）。

新聞稿明文列出的安排：
- 4/4 兒童節逢星期日，於清明節之次日 4/6（二）補假
- 5/1 勞動節逢星期六，於 4/30（五）補假
- 全年放假 121 日，無「調整放假／補行上班」日

3/1 補假並未逐字列出，而是依同一新聞稿引述的「政府機關配合紀念日與節日補假及
調整放假處理要點」推得：放假日逢星期六者於前一個上班日補假，逢星期日者於次一個
上班日補假；2/28 為星期日，故 3/1 補假（新聞稿亦稱和平紀念日為 3 日連假）。

每筆資料帶 `basis` 說明依據，方便日後年度沿用時區分「官方明列」與「依規則推得」。

不確定性：
- 該日曆表適用於政府行政機關；民間依《紀念日及節日實施條例》放假，目前日期一致，
  但雇主可依勞動契約調整，實際請假天數以自身公司行事曆為準。
- 行政院可能因臨時決議調整（颱風假、追加彈性放假等），本表不會自動更新。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

OFFICIAL_SOURCE_URL = "https://www.dgpa.gov.tw/information?pid=12983&uid=82"
OFFICIAL_ANNOUNCED_ON = dt.date(2026, 5, 21)
RETRIEVED_ON = dt.date(2026, 8, 29)

# 供報告輸出用，讓每份報告都自帶資料出處與限制
HOLIDAY_SOURCE = {
    "name": "行政院人事行政總處 116 年（2027）政府行政機關辦公日曆表",
    "url": OFFICIAL_SOURCE_URL,
    "announced": OFFICIAL_ANNOUNCED_ON.isoformat(),
    "retrieved": RETRIEVED_ON.isoformat(),
    "makeup_workdays": [],  # 官方公告 2027 全年無補班日
    "caveat": "適用政府機關；民間依《紀念日及節日實施條例》放假，實際以公司行事曆為準",
}


@dataclass(frozen=True)
class Holiday:
    date: dt.date
    name: str
    basis: str  # "official_listed"（新聞稿明列）| "official_rule"（依官方補假規則推得）
    note: str = ""


HOLIDAYS_2027: tuple[Holiday, ...] = (
    Holiday(dt.date(2027, 2, 28), "和平紀念日", "official_listed", "適逢星期日"),
    Holiday(dt.date(2027, 3, 1), "和平紀念日補假", "official_rule", "2/28 逢星期日，次一上班日補假"),
    Holiday(dt.date(2027, 4, 4), "兒童節", "official_listed", "適逢星期日"),
    Holiday(dt.date(2027, 4, 5), "民族掃墓節（清明節）", "official_listed", ""),
    Holiday(dt.date(2027, 4, 6), "兒童節補假", "official_listed", "4/4 逢星期日，於清明節次日補假"),
    Holiday(dt.date(2027, 4, 30), "勞動節補假", "official_listed", "5/1 逢星期六，前一上班日補假"),
    Holiday(dt.date(2027, 5, 1), "勞動節", "official_listed", "適逢星期六"),
)

HOLIDAY_BY_DATE: dict[dt.date, Holiday] = {h.date: h for h in HOLIDAYS_2027}

# 2027 全年無週末補班日；保留此集合是為了讓 is_workday 的規則完整，日後年度可直接填入
MAKEUP_WORKDAYS_2027: frozenset[dt.date] = frozenset()


def is_holiday(day: dt.date) -> bool:
    return day in HOLIDAY_BY_DATE


def is_workday(day: dt.date, makeup_workdays: frozenset[dt.date] = MAKEUP_WORKDAYS_2027) -> bool:
    """補班日一律算上班日；否則週一至週五且非假日才算平日上班日。"""
    if day in makeup_workdays:
        return True
    return day.weekday() < 5 and not is_holiday(day)


def holidays_between(start: dt.date, end: dt.date) -> list[Holiday]:
    return [h for h in HOLIDAYS_2027 if start <= h.date <= end]


def long_weekends_2027() -> list[dict]:
    """3–5 月的連假區間（含週末），供報告使用。"""
    return [
        {"name": "和平紀念日連假", "start": "2027-02-27", "end": "2027-03-01", "days": 3},
        {"name": "兒童節及清明節連假", "start": "2027-04-03", "end": "2027-04-06", "days": 4},
        {"name": "勞動節連假", "start": "2027-04-30", "end": "2027-05-02", "days": 3},
    ]
