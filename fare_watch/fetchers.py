"""票價來源（fetchers）：Google Flights 可分享查詢頁（預設）、Amadeus、dry-run。

所有來源都回傳 `FetchResult`，狀態只有四種：
- `ok`           解析到至少一筆報價，`offers` 依原頁面順序保留
- `unavailable`  頁面拿到了但沒有可靠數字（動態頁、沒有報價）；絕不推估
- `error`        重試耗盡（網路錯誤、5xx）
- `dry_run`      沒有連網

被來源封鎖（429/403、/sorry/、consent 頁）不回傳結果而是丟 `BlockedError`，
由 CLI 中止整輪：被擋之後繼續打只會讓封鎖更久。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import random
import re
import time
from dataclasses import asdict, dataclass, field, fields
from urllib.parse import quote, urlencode

import requests

from .amadeus import AmadeusClient
from .itineraries import Itinerary
from .tz import now_taipei

log = logging.getLogger("fare_watch.fetchers")

ORIGIN = "TPE"
DEST = "PUS"
PASSENGERS = 3
DEFAULT_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
HTTP_TIMEOUT = 25
MAX_ATTEMPTS = 3
RETRY_STATUSES = {500, 502, 503, 504}
BLOCK_STATUSES = {403, 429}

# Google Flights 把整頁資料塞在 <script> 的 AF_initDataCallback 裡，航班清單在 key 'ds:1'
_DS1_RE = re.compile(r"AF_initDataCallback\(\{key: 'ds:1'.*?data:\s*", re.S)
# 這兩個字串只會出現在 Google 的反機器人頁，正常結果頁不會有
_CAPTCHA_MARKERS = ("id=\"captcha-form\"", "unusual traffic from your computer network")


class BlockedError(Exception):
    """來源明確拒絕我們（限流、驗證碼、同意頁）；呼叫端應停止整輪而不是重試。"""


@dataclass
class Offer:
    price: int
    currency: str
    airline: str
    airline_name: str
    flights: list[str]
    stops: int
    depart_time: str
    arrive_time: str
    duration_min: int
    section: str  # Google 頁面的「best」或「other」分區；Amadeus 一律 "amadeus"
    # 回程段：來源真的有回程資料才填；Google 分享頁只伺服端渲染去程，所以通常是空值，
    # 空值代表「解析不到」而不是「沒有回程」，下游要顯示 return_time_unavailable、不得拿去程代替
    return_depart_time: str = ""
    return_arrive_time: str = ""
    return_flights: list[str] = field(default_factory=list)
    return_stops: int = 0
    return_airline: str = ""
    return_airline_name: str = ""
    # 0 代表「來源沒給回程時長」，與去程 duration_min 一樣不做推估
    return_duration_min: int = 0

    @property
    def nonstop(self) -> bool:
        """去程與（有解析到的）回程都不轉機。"""
        return self.stops == 0 and self.return_stops == 0

    @property
    def return_time_status(self) -> str:
        return "ok" if self.return_depart_time and self.return_arrive_time else "return_time_unavailable"

    @property
    def time_tags(self) -> list[str]:
        """依實際時間分類；未取得回程時間時不推估。"""
        tags = []
        if self.depart_time:
            h, m = map(int, self.depart_time.split(":"))
            if h < 6:
                tags.append("red_eye")
            if h >= 21:
                tags.append("late_departure")
        if self.return_depart_time:
            h, m = map(int, self.return_depart_time.split(":"))
            if h < 6:
                tags.append("red_eye")
            if h < 10:
                tags.append("early_return")
        else:
            tags.append("return_time_unavailable")
        return sorted(set(tags)) or ["normal"]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["time_tags"] = self.time_tags
        d["return_time_status"] = self.return_time_status
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Offer":
        """從落地的 raw JSON 還原；`to_dict()` 多寫的衍生欄位（time_tags 等）是 property，這裡要濾掉。"""
        allowed = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in allowed})


@dataclass
class FetchResult:
    source: str
    key: str
    status: str  # ok | unavailable | error | dry_run
    url: str
    fetched_at: str
    offers: list[Offer] = field(default_factory=list)
    raw_block: object = None  # 解析用的原始資料片段，落地到 raw/ 方便日後重新解析
    reason: str = ""
    html: str | None = None  # 只有失敗（或 --keep-html）才帶完整頁面

    @property
    def min_price(self) -> int | None:
        return min((o.price for o in self.offers), default=None)

    @property
    def cheapest(self) -> Offer | None:
        return min(self.offers, key=lambda o: o.price) if self.offers else None

    def to_dict(self) -> dict:
        """不含 html：完整頁面另存 .html.gz，避免 JSON 檔動輒數 MB。"""
        return {
            "source": self.source, "key": self.key, "status": self.status, "url": self.url,
            "fetched_at": self.fetched_at, "reason": self.reason, "min_price": self.min_price,
            "offers": [o.to_dict() for o in self.offers], "raw_block": self.raw_block,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "FetchResult":
        """從落地的 raw JSON 還原，讓上一輪抓到的結果能併進這一輪的報告。

        `min_price` 是 property，不還原；`html` 沒寫進 JSON（另存 .html.gz），還原後一律 None，
        所以還原的結果不會被再次寫成 raw，只拿來組報告。
        """
        return cls(
            source=d.get("source", ""), key=d.get("key", ""), status=d.get("status", "unavailable"),
            url=d.get("url", ""), fetched_at=d.get("fetched_at", ""),
            offers=[Offer.from_dict(o) for o in d.get("offers") or []],
            raw_block=d.get("raw_block"), reason=d.get("reason", ""),
        )


def _stamp() -> str:
    return now_taipei().isoformat(timespec="seconds")


def jitter_sleep(lo: float, hi: float, sleep=time.sleep) -> None:
    """行程之間隨機等待，避免固定節奏看起來像機器人。"""
    hi = max(hi, lo)
    if hi > 0:
        sleep(random.uniform(lo, hi))


# ---------- Google Flights ----------

def google_flights_url(it: Itinerary, origin: str = ORIGIN, dest: str = DEST, currency: str = "TWD") -> str:
    """Google Flights 支援用自然語言 q= 查詢，這個 URL 可直接貼到瀏覽器分享。"""
    q = f"Flights to {dest} from {origin} on {it.depart.isoformat()} through {it.return_.isoformat()}"
    return "https://www.google.com/travel/flights?" + urlencode(
        {"q": q, "curr": currency, "hl": "en", "gl": "TW"}, quote_via=quote)


def extract_init_data(html: str) -> list | None:
    m = _DS1_RE.search(html or "")
    if not m:
        return None
    try:
        data, _ = json.JSONDecoder().raw_decode(html, m.end())
    except ValueError:
        return None
    return data if isinstance(data, list) else None


def _get(obj, *path):
    for p in path:
        if not isinstance(obj, list) or not isinstance(p, int) or p >= len(obj):
            return None
        obj = obj[p]
    return obj


def _hhmm(v) -> str:
    if not isinstance(v, list) or not v or not isinstance(v[0], int):
        return ""
    h = v[0]
    mnt = v[1] if len(v) > 1 and isinstance(v[1], int) else 0
    return f"{h:02d}:{mnt:02d}"


def _leg(segs: list) -> dict:
    """把一段（去程或回程）的 segment 串整理成航班、航空公司、首段起飛、末段抵達。

    segment 欄位：[3] 起飛機場、[6] 抵達機場、[8] 起飛 [h, m]、[10] 抵達 [h, m]、[22] [代碼, 班號, _, 航空公司名]。
    """
    flights, codes, names = [], [], []
    for s in segs:
        code, num, name = _get(s, 22, 0), _get(s, 22, 1), _get(s, 22, 3)
        if isinstance(code, str) and isinstance(num, str):
            flights.append(f"{code}{num}")
            codes.append(code)
            names.append(name if isinstance(name, str) else code)
    return {
        "flights": flights,
        "airline": "/".join(dict.fromkeys(codes)), "airline_name": "/".join(dict.fromkeys(names)),
        "depart_time": _hhmm(_get(segs[0], 8)) if segs else "",
        "arrive_time": _hhmm(_get(segs[-1], 10)) if segs else "",
        "stops": max(len(segs) - 1, 0),
    }


def _split_legs(segs: list, dest: str | None) -> tuple[list, list]:
    """去程 = 第一個抵達目的地的 segment（含）之前；其後的 segment 若從目的地出發就是回程。

    Google 分享頁目前只給去程，回程會是空清單；但若哪天資料同時帶兩段（或 Amadeus 那種多段結構），
    這裡能正確切開而不是把回程航段誤當成去程轉機。
    """
    cut = next((i + 1 for i, s in enumerate(segs) if dest and _get(s, 6) == dest), len(segs))
    out, rest = segs[:cut], segs[cut:]
    ret = rest if rest and _get(rest[0], 3) == dest else []
    return out, ret


def _parse_item(item, section: str, currency: str, dest: str | None = None) -> Offer | None:
    itin = _get(item, 0)
    price = _get(item, 1, 0, 1)
    segs = _get(itin, 2)
    # 沒有價格的列（頁面顯示「價格不明」）直接略過，不用 0 或猜測值填補
    if not isinstance(price, (int, float)) or isinstance(price, bool) or price <= 0 or not isinstance(segs, list):
        return None
    out_segs, ret_segs = _split_legs(segs, dest or (_get(itin, 6) if isinstance(_get(itin, 6), str) else None))
    out, ret = _leg(out_segs), _leg(ret_segs)
    if not out["flights"]:
        return None
    names = _get(itin, 1)
    airline_name = "/".join(n for n in names if isinstance(n, str)) if isinstance(names, list) else ""
    duration = _get(itin, 9)
    return Offer(
        price=int(round(price)), currency=currency,
        airline=_get(itin, 0) if isinstance(_get(itin, 0), str) else out["airline"],
        airline_name=airline_name or out["airline_name"], flights=out["flights"], stops=out["stops"],
        # 去程時間以 itinerary 層級為準（既有行為），segment 層級只在缺值時補
        depart_time=_hhmm(_get(itin, 5)) or out["depart_time"], arrive_time=_hhmm(_get(itin, 8)) or out["arrive_time"],
        duration_min=duration if isinstance(duration, int) else 0, section=section,
        return_depart_time=ret["depart_time"], return_arrive_time=ret["arrive_time"],
        return_flights=ret["flights"], return_stops=ret["stops"],
        return_airline=ret["airline"], return_airline_name=ret["airline_name"],
    )


def parse_offers(data, currency: str = "TWD", dest: str | None = None) -> list[Offer]:
    """從 ds:1 資料取出 best（data[2][0]）與 other（data[3][0]）兩區的報價；任何形狀不對都跳過。

    不設航空公司白名單：Google 回傳的每家直飛都保留，轉機與否由呼叫端依 `Offer.nonstop` 過濾。
    `dest` 用來切分去程/回程段；不給就用 itinerary 自帶的抵達機場。
    """
    if not isinstance(data, list):
        return []
    offers: list[Offer] = []
    for section, idx in (("best", 2), ("other", 3)):
        block = _get(data, idx, 0)
        if not isinstance(block, list):
            continue
        for item in block:
            o = _parse_item(item, section, currency, dest)
            if o is not None:
                offers.append(o)
    return offers


def detect_block(html: str, url: str) -> str | None:
    url = url or ""
    if "consent.google.com" in url:
        return "consent_page"
    if "/sorry/" in url:
        return "captcha_or_sorry_page"
    if any(marker in (html or "") for marker in _CAPTCHA_MARKERS):
        return "captcha_or_sorry_page"
    return None


class Fetcher:
    name = "base"

    def fetch(self, it: Itinerary) -> FetchResult:  # pragma: no cover
        raise NotImplementedError


class GoogleFlightsFetcher(Fetcher):
    name = "google_flights"

    def __init__(self, currency: str = "TWD", user_agent: str = DEFAULT_UA, origin: str = ORIGIN, dest: str = DEST,
                 session=None, sleep=time.sleep, max_attempts: int = MAX_ATTEMPTS, keep_html: bool = False):
        self.currency, self.origin, self.dest = currency, origin, dest
        self.max_attempts, self.keep_html = max_attempts, keep_html
        self._sleep = sleep
        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,zh-TW;q=0.8",
        })

    def _result(self, it: Itinerary, url: str, status: str, **kw) -> FetchResult:
        return FetchResult(self.name, it.key, status, url, _stamp(), **kw)

    def fetch(self, it: Itinerary) -> FetchResult:
        url = google_flights_url(it, self.origin, self.dest, self.currency)
        last = ""
        for attempt in range(1, self.max_attempts + 1):
            if attempt > 1:
                # 指數退避：暫時性錯誤通常幾秒內恢復，太快重打只會撞上同一個問題
                self._sleep(2 ** (attempt - 1) + random.uniform(0, 1))
            try:
                resp = self.session.get(url, timeout=HTTP_TIMEOUT, allow_redirects=True)
            except requests.RequestException as e:
                last = f"{type(e).__name__}: {e}"
                log.warning("%s attempt %d failed: %s", it.key, attempt, last)
                continue
            status = resp.status_code
            if status in BLOCK_STATUSES:
                raise BlockedError(f"HTTP {status}")
            if status in RETRY_STATUSES:
                last = f"http_{status}"
                continue
            if status != 200:
                return self._result(it, url, "unavailable", reason=f"http_{status}")
            html = resp.text or ""
            block = detect_block(html, getattr(resp, "url", "") or url)
            if block:
                raise BlockedError(block)
            data = extract_init_data(html)
            if data is None:
                return self._result(it, url, "unavailable", html=html,
                                    reason="dynamic_content: no AF_initDataCallback ds:1 block (page requires JS)")
            offers = parse_offers(data, self.currency, dest=self.dest)
            # 使用者要求不轉機：去程與回程都得直飛；不可用轉機航班冒充可用報價
            offers = [o for o in offers if o.nonstop]
            raw_block = [_get(data, 2), _get(data, 3)]
            if not offers:
                return self._result(it, url, "unavailable", html=html, raw_block=raw_block,
                                    reason="no_direct_offers: parsed page had no nonstop offers")
            return self._result(it, url, "ok", offers=offers, raw_block=raw_block,
                                html=html if self.keep_html else None)
        return self._result(it, url, "error", reason=f"retries_exhausted after {self.max_attempts} attempts: {last}")


# ---------- Dry-run ----------

class DryRunFetcher(Fetcher):
    name = "dry_run"

    def __init__(self, currency: str = "TWD", origin: str = ORIGIN, dest: str = DEST):
        self.currency, self.origin, self.dest = currency, origin, dest

    def fetch(self, it: Itinerary) -> FetchResult:
        return FetchResult(self.name, it.key, "dry_run", google_flights_url(it, self.origin, self.dest, self.currency),
                           _stamp(), reason="dry-run: no network request made")


# ---------- Amadeus ----------

_ISO_DUR_RE = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?")


def _iso_duration_min(s) -> int:
    m = _ISO_DUR_RE.fullmatch(s or "")
    return (int(m.group(1) or 0) * 60 + int(m.group(2) or 0)) if m else 0


def _iso_hhmm(s) -> str:
    try:
        return dt.datetime.fromisoformat(s).strftime("%H:%M")
    except (TypeError, ValueError):
        return ""


def _amadeus_offer(o: dict, carriers: dict) -> Offer | None:
    try:
        price = int(round(float(o["price"]["grandTotal"])))
        currency = o["price"]["currency"]
        itins = o["itineraries"]
        segs = itins[0]["segments"]
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    if price <= 0 or not segs:
        return None
    codes = [s.get("carrierCode", "") for s in segs]
    airline = (o.get("validatingAirlineCodes") or codes)[0]
    # 來回查詢時 itineraries[1] 是回程；單程或缺資料就維持空值，交給下游標 return_time_unavailable
    ret_segs = itins[1].get("segments") or [] if len(itins) > 1 and isinstance(itins[1], dict) else []
    ret_codes = list(dict.fromkeys(s.get("carrierCode", "") for s in ret_segs))
    return Offer(
        price=price, currency=currency, airline=airline, airline_name=carriers.get(airline, airline),
        flights=[f"{s.get('carrierCode', '')}{s.get('number', '')}" for s in segs], stops=len(segs) - 1,
        depart_time=_iso_hhmm(segs[0].get("departure", {}).get("at")),
        arrive_time=_iso_hhmm(segs[-1].get("arrival", {}).get("at")),
        duration_min=_iso_duration_min(itins[0].get("duration")), section="amadeus",
        return_depart_time=_iso_hhmm(ret_segs[0].get("departure", {}).get("at")) if ret_segs else "",
        return_arrive_time=_iso_hhmm(ret_segs[-1].get("arrival", {}).get("at")) if ret_segs else "",
        return_flights=[f"{s.get('carrierCode', '')}{s.get('number', '')}" for s in ret_segs],
        return_stops=max(len(ret_segs) - 1, 0),
        return_airline="/".join(ret_codes), return_airline_name="/".join(carriers.get(c, c) for c in ret_codes),
    )


class AmadeusFetcher(Fetcher):
    name = "amadeus"

    def __init__(self, client_id: str, client_secret: str, currency: str = "TWD",
                 origin: str = ORIGIN, dest: str = DEST, client: AmadeusClient | None = None):
        # AmadeusClient 固定用 TWD 報價；currency 只用來標示，避免動到既有模組
        self.currency, self.origin, self.dest = currency, origin, dest
        self.client = client or AmadeusClient(client_id, client_secret)

    def fetch(self, it: Itinerary) -> FetchResult:
        url = f"{self.client.base_url}/v2/shopping/flight-offers?{self.origin}-{self.dest}/{it.key}"
        price, body = self.client.fetch(self.origin, self.dest, it.depart, it.return_)
        if price["status"] != "ok":
            return FetchResult(self.name, it.key, "unavailable", url, _stamp(), reason=price["reason"], raw_block=body)
        carriers = (body or {}).get("dictionaries", {}).get("carriers", {})
        offers = [o for o in (_amadeus_offer(x, carriers) for x in body.get("data", [])) if o and o.nonstop]
        if not offers:
            return FetchResult(self.name, it.key, "unavailable", url, _stamp(), reason="no_direct_offers", raw_block=body)
        return FetchResult(self.name, it.key, "ok", url, _stamp(), offers=offers, raw_block=body)
