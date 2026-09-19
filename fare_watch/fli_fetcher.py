"""fli 來源（**可選**）：先問 fli，問不出來就退回原本的 fetcher。

fli（PyPI 套件叫 `flights`，import 名字叫 `fli`）直接打 Google Flights 的內部端點，
不開瀏覽器，一次查詢約 5~10 秒——比 `PlaywrightFetcher` 的 ~50 秒快一個數量級，
而且來回兩段都拿得到航班號與起降時刻。

**但它不是必要條件**：沒裝、查失敗、回空、缺欄位，一律安靜退回原本的來源，
整條流程不因為少一個可選套件而壞掉。所以：

- 全程 **lazy import**，模組層不 import fli；`requirements.txt` 不加它
- 任何不對勁都收斂成 `FliUnusable(reason)`，由 `FliFirstFetcher` 轉成 fallback
- fallback 的 `BlockedError` 照舊往上冒（被擋要中止整輪），fli 自己的錯誤則不算被擋——
  fli 失敗只代表這個可選捷徑沒走通，真正的判斷交給原本的來源

**價格語意的三個坑**（實測 2027-03-15 TPE→PUS 驗出來的）：

1. `adults=3` 回傳的是**三人總價**（18,669 = 6,223×3）。本專案只列每人單價，
   所以一律用 `adults=1` 查，不用除法——除法在混艙等/兒童票時會錯。
2. 來回 pair 的兩筆 `price` 通常一樣（＝該組合總價），但選到不同回程時不一樣
   （去程 KE2086 9,078 配回程 CI187 顯示 12,522）。**組合價要取回程那一筆**，取去程會低估。
3. `price=None` 真的會出現（實測 BR163）。依「不捏造」原則直接丟掉那個組合，不當 0。

另外 `Airline` enum 的名字不能以數字開頭，所以濟州航空是 `_7C`——要正規化回 `7C`。
"""

from __future__ import annotations

import logging

from .fetchers import DEST, ORIGIN, BlockedError, Fetcher, FetchResult, Offer, _stamp, google_flights_url
from .itineraries import Itinerary

log = logging.getLogger("fare_watch.fli")

# 只接受直飛：本專案的硬性條件，也是 pair 驗證的第一道關
REQUIRED_STOPS = 0
# 一次要幾筆。Google 對同一組日期的直飛班次本來就不多，要太多只是多花時間
TOP_N = 20


class FliUnusable(Exception):
    """fli 這條捷徑沒走通（沒裝、查失敗、資料不合契約）。呼叫端應改用 fallback，不是中止整輪。"""


def normalize_airline(code: str) -> str:
    """`_7C` → `7C`：enum 名字不能以數字開頭，前面補的底線不是航空公司代碼的一部分。"""
    return (code or "").lstrip("_")


def _hhmm(value) -> str:
    """datetime → 'HH:MM'；拿不到就回空字串，交由下游標 unavailable，不推估。"""
    try:
        return f"{value.hour:02d}:{value.minute:02d}"
    except AttributeError:
        return ""


def _airline_name(airline) -> str:
    """enum 的 value 若是可讀名稱就用它，否則退回代碼本身。"""
    value = getattr(airline, "value", None)
    return value if isinstance(value, str) and value else normalize_airline(getattr(airline, "name", ""))


def _leg_of(result) -> object:
    """直飛只該有一段；多段代表它其實會轉機，不收。"""
    legs = list(getattr(result, "legs", None) or [])
    if len(legs) != 1:
        raise FliUnusable(f"expected 1 leg for nonstop, got {len(legs)}")
    return legs[0]


def _airport_code(value) -> str:
    return normalize_airline(getattr(value, "name", "") or str(value or ""))


def _pair_to_offer(pair, origin: str, dest: str, currency: str) -> Offer:
    """把 (去程, 回程) 轉成 `Offer`；任何一項不合契約就丟 `FliUnusable`，不勉強湊。"""
    if not isinstance(pair, (tuple, list)) or len(pair) != 2:
        raise FliUnusable("not a round-trip pair (missing outbound or return)")
    out, ret = pair
    for tag, r in (("outbound", out), ("return", ret)):
        if getattr(r, "stops", None) != REQUIRED_STOPS:
            raise FliUnusable(f"{tag} stops={getattr(r, 'stops', None)!r}, only nonstop accepted")
    out_leg, ret_leg = _leg_of(out), _leg_of(ret)

    # 價格取回程那一筆＝這個組合的實際總價；取去程會低估（見模組說明）
    price = getattr(ret, "price", None)
    if price is None:
        raise FliUnusable("return price is None")
    try:
        price = int(round(float(price)))
    except (TypeError, ValueError):
        raise FliUnusable(f"unparseable price {price!r}")
    if price <= 0:
        raise FliUnusable(f"non-positive price {price}")

    depart_time, arrive_time = _hhmm(out_leg.departure_datetime), _hhmm(out_leg.arrival_datetime)
    ret_depart, ret_arrive = _hhmm(ret_leg.departure_datetime), _hhmm(ret_leg.arrival_datetime)
    if not (depart_time and arrive_time):
        raise FliUnusable("outbound leg has no parseable times")
    if not (ret_depart and ret_arrive):
        raise FliUnusable("return leg has no parseable times")

    # 方向要對：去程 origin→dest、回程 dest→origin。搞反了寧可不要
    if _airport_code(out_leg.departure_airport) != origin or _airport_code(out_leg.arrival_airport) != dest:
        raise FliUnusable("outbound leg is not origin->dest")
    if _airport_code(ret_leg.departure_airport) != dest or _airport_code(ret_leg.arrival_airport) != origin:
        raise FliUnusable("return leg is not dest->origin")

    out_code = normalize_airline(getattr(out_leg.airline, "name", ""))
    ret_code = normalize_airline(getattr(ret_leg.airline, "name", ""))
    return Offer(
        price=price,
        currency=getattr(ret, "currency", None) or currency,
        airline=out_code, airline_name=_airline_name(out_leg.airline),
        flights=[f"{out_code}{out_leg.flight_number}"],
        stops=REQUIRED_STOPS,
        depart_time=depart_time, arrive_time=arrive_time,
        duration_min=int(getattr(out, "duration", 0) or 0),
        section="fli",
        return_depart_time=ret_depart, return_arrive_time=ret_arrive,
        return_flights=[f"{ret_code}{ret_leg.flight_number}"],
        return_stops=REQUIRED_STOPS,
        return_airline=ret_code, return_airline_name=_airline_name(ret_leg.airline),
        return_duration_min=int(getattr(ret, "duration", 0) or 0),
    )


def offers_from_pairs(pairs, origin: str, dest: str, currency: str) -> tuple[list[Offer], list[str]]:
    """能轉的都轉；轉不動的記下原因，讓 reason 說得出「為什麼只剩這幾筆」。"""
    offers, skipped = [], []
    for pair in pairs or []:
        try:
            offers.append(_pair_to_offer(pair, origin, dest, currency))
        except FliUnusable as e:
            skipped.append(str(e))
    return offers, skipped


def default_search(origin: str, dest: str, depart: str, return_: str, currency: str):
    """真的呼叫 fli。沒裝就丟 `FliUnusable`，讓呼叫端安靜退回原本的來源。

    **一定要 adults=1**：fli 的 price 是「全部乘客加起來」，本專案只列每人單價。
    """
    try:
        from fli.models import (Airport, FlightSearchFilters, FlightSegment, MaxStops, PassengerInfo,
                                SeatType, SortBy, TripType)
        from fli.search import SearchFlights
    except ImportError as e:
        raise FliUnusable(f"fli_not_installed: {e}") from e

    try:
        origin_ap, dest_ap = getattr(Airport, origin), getattr(Airport, dest)
    except AttributeError as e:
        raise FliUnusable(f"unknown airport for fli: {e}") from e

    filters = FlightSearchFilters(
        trip_type=TripType.ROUND_TRIP,
        passenger_info=PassengerInfo(adults=1),   # 每人單價，見 docstring
        flight_segments=[
            FlightSegment(departure_airport=[[origin_ap, 0]], arrival_airport=[[dest_ap, 0]], travel_date=depart),
            FlightSegment(departure_airport=[[dest_ap, 0]], arrival_airport=[[origin_ap, 0]], travel_date=return_),
        ],
        stops=MaxStops.NON_STOP,
        seat_type=SeatType.ECONOMY,
        sort_by=SortBy.CHEAPEST,
    )
    try:
        return SearchFlights().search(filters, top_n=TOP_N, currency=currency, country="TW", language="en")
    except Exception as e:
        # fli 自己的例外型別要 import 才拿得到，這裡一律收斂成「捷徑沒走通」。
        # 刻意不轉成 BlockedError：被不被擋由原本的來源判斷，這裡不代它下結論
        raise FliUnusable(f"fli_search_failed: {type(e).__name__}: {e}") from e


class FliFetcher(Fetcher):
    """只用 fli 的來源。拿不到東西一律丟 `FliUnusable`，不自己 fallback。"""

    name = "fli"

    def __init__(self, currency: str = "TWD", origin: str = ORIGIN, dest: str = DEST, search=None):
        self.currency, self.origin, self.dest = currency, origin, dest
        self._search = search or default_search

    def fetch(self, it: Itinerary) -> FetchResult:
        url = google_flights_url(it, self.origin, self.dest, self.currency)
        pairs = self._search(self.origin, self.dest, it.depart.isoformat(), it.return_.isoformat(), self.currency)
        if not pairs:
            raise FliUnusable("fli returned no results")
        offers, skipped = offers_from_pairs(pairs, self.origin, self.dest, self.currency)
        if not offers:
            why = "; ".join(dict.fromkeys(skipped)) or "no usable pairs"
            raise FliUnusable(f"no_usable_offers: {why}")
        reason = f"fli: {len(offers)} offers" + (f"; skipped {len(skipped)}" if skipped else "")
        return FetchResult(self.name, it.key, "ok", url, _stamp(), offers=offers, reason=reason)


class FliFirstFetcher(Fetcher):
    """先問 fli，不行就用原本的 fetcher。

    fallback 的 `BlockedError` 直接往上冒——被擋要中止整輪的規則不因為多了一層而改變。
    退回時把 fli 的失敗原因接在 fallback 的 reason 前面，報告看得出為什麼走了慢路。
    """

    def __init__(self, fli: FliFetcher, fallback: Fetcher):
        self.fli, self.fallback = fli, fallback
        self.name = f"fli+{fallback.name}"

    def fetch(self, it: Itinerary) -> FetchResult:
        try:
            return self.fli.fetch(it)
        except FliUnusable as e:
            why = str(e)
        except BlockedError:
            raise
        except Exception as e:  # fli 是可選捷徑，任何沒預料到的錯都不該讓整輪掛掉
            why = f"fli_unexpected: {type(e).__name__}: {e}"
        log.info("%s: fli unusable (%s); falling back to %s", it.key, why, self.fallback.name)
        res = self.fallback.fetch(it)
        res.reason = f"fli_fallback[{why}] -> {self.fallback.name}" + (f": {res.reason}" if res.reason else "")
        return res
