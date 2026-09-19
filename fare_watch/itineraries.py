"""產生 5 天 4 夜行程並計算需請假天數。"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass

from .holidays_tw import HOLIDAY_BY_DATE, is_workday

# 台灣習慣的星期寫法，對應 date.weekday() 的 0=星期一
WEEKDAYS_TW = ("一", "二", "三", "四", "五", "六", "日")

TRIP_DAYS = 5  # 5 天 4 夜：去程日 + 3 個完整天 + 回程日
WINDOW_START = dt.date(2027, 3, 1)
WINDOW_END = dt.date(2027, 5, 31)


@dataclass(frozen=True)
class Itinerary:
    depart: dt.date
    return_: dt.date
    leave_days: int
    leave_dates: tuple[dt.date, ...]
    holidays_hit: tuple[str, ...]

    @property
    def key(self) -> str:
        return f"{self.depart.isoformat()}_{self.return_.isoformat()}"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["depart"] = self.depart.isoformat()
        d["return"] = self.return_.isoformat()
        del d["return_"]
        d["leave_dates"] = [x.isoformat() for x in self.leave_dates]
        d["holidays_hit"] = list(self.holidays_hit)
        d["key"] = self.key
        return d


def format_date(value) -> str:
    """顯示用日期：`2027-03-11` → `3/11(四)`。吃 ISO 字串或 `date` 都可以。

    只用在使用者看得到的地方（Markdown 報告、CLI 摘要）。**JSON 的 canonical 欄位維持
    ISO `YYYY-MM-DD` 不動**——查詢 URL、排序、請假計算、觀測紀錄去重全都吃 ISO，
    在資料層加星期會把這些一起弄壞。
    """
    d = value if isinstance(value, dt.date) else dt.date.fromisoformat(str(value))
    return f"{d.month}/{d.day}({WEEKDAYS_TW[d.weekday()]})"


def build_itinerary(depart: dt.date) -> Itinerary:
    return_ = depart + dt.timedelta(days=TRIP_DAYS - 1)
    days = [depart + dt.timedelta(days=i) for i in range(TRIP_DAYS)]
    leave = tuple(d for d in days if is_workday(d))
    hit = tuple(
        f"{d.isoformat()} {HOLIDAY_BY_DATE[d].name}" for d in days if d in HOLIDAY_BY_DATE
    )
    return Itinerary(depart, return_, len(leave), leave, hit)


def generate(months: list[int] | None = None, depart_through: dt.date | None = None) -> list[Itinerary]:
    """回傳所有去程與回程都落在 2027-03-01..05-31 內的行程，依去程日排序。

    `months` 以「去程月份」過濾（例如 [3] 只留 3 月出發）。
    `depart_through` 指定最後一個允許的去程日：預設要求回程也落在視窗內（去程最晚 5/27）；
    分組比價要涵蓋 5/31 出發（回程 6/4），就傳 `WINDOW_END`。
    """
    last_depart = depart_through or (WINDOW_END - dt.timedelta(days=TRIP_DAYS - 1))
    out: list[Itinerary] = []
    day = WINDOW_START
    while day <= last_depart:
        if not months or day.month in months:
            out.append(build_itinerary(day))
        day += dt.timedelta(days=1)
    return out


def fewest_leave(itins: list[Itinerary]) -> list[Itinerary]:
    if not itins:
        return []
    best = min(i.leave_days for i in itins)
    return [i for i in itins if i.leave_days == best]
