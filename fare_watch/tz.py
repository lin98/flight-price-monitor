import datetime as dt
from zoneinfo import ZoneInfo

TAIPEI = ZoneInfo("Asia/Taipei")


def now_taipei() -> dt.datetime:
    return dt.datetime.now(tz=TAIPEI)
