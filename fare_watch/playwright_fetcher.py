"""Playwright 來源：用真實瀏覽器跑完 Google Flights 的「兩階段」來回流程，再解析成 `Offer`。

為什麼一定要兩階段：Google Flights 的來回查詢，第一頁只列**去程**卡片（卡片上的價格是整趟
來回的最低估計總價）；回程航班要「點掉某張去程卡片」之後進到回程選擇頁才會出現。
純 HTTP 的 `GoogleFlightsFetcher` 連第一頁都拿不到（`dynamic_content`），
只渲染第一頁也只有去程——所以這裡真的會去點。

分層，每層都能單獨測：

1. `parse_leg()` / `parse_result_html()`：純函式，吃 HTML 或可見文字，不需要瀏覽器。
2. `build_offers()`：把「去程卡片 ＋ 該卡片的回程選擇頁」組成完整 `Offer`。
3. `_playwright_render()`：唯一碰瀏覽器的地方，回傳 `RenderedTrip`；建構子的 `render=` 可注入替換。

**選擇器策略**：class 名稱（`li.pIav2d`、`gvkrdb`…）是 Google 隨時會換的混淆字串，
所以每一步都是「候選選擇器清單逐一嘗試 → 最後退到可見文字比對」，實際命中哪一個記進
`RenderedTrip.notes`，並且會一路帶進 `FetchResult.reason`，線上跑壞時看 reason 就知道斷在哪。
解析也一樣：class / aria-label 優先，抓不到才退到可見文字。

**航班號要展開才有**：實測收合的卡片只畫「台灣虎航 8:50 PM–10:30 PM 直達」，
`IT 607` 只在按開「航班詳細資料」之後的明細裡；反過來「Nonstop」只寫在收合的摘要上。
所以每張要用的卡片都會展開拿一份 `detail_*`，再跟摘要合著解析（`card_leg()`），拿完收回去。

**抓不到就留空**：展不開就只讀摘要——少航班號，但時刻、航程、直達照樣解得出來。
解析不到就是空字串／0，由下游標 `return_time_unavailable`，
這裡不推估、不用去程冒充回程、不用 0 當價格。

**被擋就停**：HTTP 403/429、`/sorry/`、captcha、consent 頁一律丟 `BlockedError` 中止整輪。
不做任何繞過——不換 UA 重打、不解 captcha、不硬跳同意頁。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from functools import partial
from html import unescape
from pathlib import Path

from .fetchers import (
    BLOCK_STATUSES, DEFAULT_UA, DEST, ORIGIN, BlockedError, Fetcher, FetchResult, Offer, _stamp, detect_block,
    google_flights_url,
)
from .itineraries import Itinerary

log = logging.getLogger("fare_watch.playwright")

NAV_TIMEOUT_MS = 45_000
RESULT_TIMEOUT_MS = 30_000
RETURN_TIMEOUT_MS = 25_000
ACTION_TIMEOUT_MS = 15_000
# 清單先出殼、價格與航班細節後到；等到「卡片數連續兩次不變」才算長好（見 _wait_for_cards）
CARD_SETTLE_TIMEOUT_MS = 20_000
CARD_POLL_MS = 500
# 數量穩定要連續幾次才收手。兩次＝1 秒沒再長，足以跨過清單分批塞進來的空檔
CARD_STABLE_ROUNDS = 2
# 展開明細是 SPA 動畫，aria-expanded 先翻 true、內容才畫上去
EXPAND_TIMEOUT_MS = 6_000
EXPAND_SETTLE_MS = 600
# 回程卡片展開幾張。展開一張是兩次點擊，整頁全展開又慢又像機器人；
# 沒展開的卡片仍解得出時刻與航空公司，只是少航班號——退化而不是失敗
MAX_RETURN_EXPAND = 10
# 點幾張去程卡片。只有直飛會被點，這條線最多也就三五個班次，點太多既慢又像機器人
MAX_OUTBOUND_CARDS = 4

# 舊名字保留：等到它出現代表 JS 跑完了
RESULT_SELECTOR = "li.pIav2d"

# 航班卡片。前面兩個是目前已知的 class，後面兩個是 class 被換掉時的結構／文字退路
CARD_SELECTORS = (
    "li.pIav2d",
    "ul.Rk10dc > li",
    # jsname 是 Google 內部的節點識別碼，比 class 混淆字串換得少；IWWDBc/YdtKid 是兩個航班清單區塊
    "[jsname='IWWDBc'] li, [jsname='YdtKid'] li",
    "[role='main'] li:has([role='button'])",
    "li:has-text('Nonstop')",
)
# 「頁面可以開始抓了」的訊號：有卡片，或看得到結果數／區塊標題。
# 文案以實測頁面為準：標題是 "Top departing flights"（不是 Best），
# 結果數寫成 "13 results returned."；zh-TW 版是「找到 13 項結果」「去程航班」
READY_SELECTORS = CARD_SELECTORS + (
    "text=/departing flights/i",
    r"text=/\d+\s*results?\s*returned/i",
    r"text=/找到\s*\d+\s*項結果/",
    "text=/去程航班/",
)
# 已確認的去程卡片點擊目標：li.pIav2d 裡掛 jsaction 的是 div.yR1fYc，
# 卡片上的航空公司 span 沒有 role=button，泛用的 role/button 選擇器點不到它
OUTBOUND_CLICK_SELECTOR = ".yR1fYc"
# class 被換掉時的退路：卡片內部其他可能可點的節點；都找不到才點卡片本身
CARD_CLICK_SELECTORS = ("div[role='button']", "button[jsname]", "button", "[jsaction*='click']", "a[href]")
# 展開「航班詳細資料」的鈕。收合的卡片上沒有航班號，按開才會出現 `IT 607`
EXPAND_SELECTORS = (
    "button[aria-expanded]",
    "[aria-label*='Flight details' i]",
    "[aria-label*='航班詳細' i]",
    "[role='button'][aria-expanded]",
)
# 回程選擇頁的標題訊號
RETURN_READY_SELECTORS = (
    "text=/Returning flights?/i",
    "text=/Choose (a )?return/i",
    "text=/Select return/i",
    "text=/回程航班/",
    "text=/選擇回程/",
    "text=/回程/",
    "[aria-label*='Returning' i]",
)
# 同意頁的按鈕。優先「全部拒絕」：不需要 cookie 也能看結果，就不要收
CONSENT_SELECTORS = (
    "button:has-text('Reject all')",
    "button:has-text('Accept all')",
    "button:has-text('I agree')",
    "button:has-text('全部拒絕')",
    "button:has-text('全部接受')",
    "form[action*='consent'] button",
)

_ROW_RE = re.compile(r'<li class="pIav2d".*?</li>', re.S)
_LEG_SPLIT_RE = re.compile(r'<div class="QS0io"[^>]*>')
_DEPART_RE = re.compile(r'aria-label="Departure time:\s*([^"]+)"')
_ARRIVE_RE = re.compile(r'aria-label="Arrival time:\s*([^"]+)"')
_DURATION_RE = re.compile(r'<div class="gvkrdb[^"]*"[^>]*>([^<]+)</div>')
_DURATION_ARIA_RE = re.compile(r'aria-label="Total duration\s*([^"]+)"', re.I)
_STOPS_RE = re.compile(r'<div class="VG3hNb"[^>]*>([^<]+)</div>')
# 展開後的明細用 <span class="Xsgmwe ...">，收合的摘要用 <div class="Xsgmwe">，兩種都要收。
# 同一個 class 也拿去畫「Tigerair Taiwan」「Economy」「Airbus A320」，
# 所以撈出來一律再過 _FLIGHT_CODE_RE，不然公司名會被當成航班號
_FLIGHT_RE = re.compile(r'<(div|span) class="Xsgmwe[^"]*"[^>]*>([^<]+)</\1>')
# 航班號：兩碼英文（IT/BX/CI）或數字＋英文（7C）＋2~4 碼數字，中間可能夾 &nbsp;
_FLIGHT_CODE_RE = re.compile(r"^([A-Z]{2}|\d[A-Z])\s*(\d{2,4})$")
_AIRLINE_RE = re.compile(r'<div class="sSHqwe[^"]*"[^>]*>\s*<span[^>]*>([^<]+)</span>')

# NT$ / US$ 要排在裸 $ 前面，否則 "NT$7,000" 會被當成 USD
_PRICE_RE = re.compile(r'(NT\$|US\$|HK\$|₩|¥|€|£|\$)\s*([\d,]+)')
_PRICE_CODE_RE = re.compile(r'\b(TWD|USD|KRW|JPY|HKD|EUR|GBP)\s*([\d,]+)')
# 裸 `$` 故意不列：我們的 URL 自己帶 curr=，頁面用哪個符號畫都以請求的幣別為準，
# 猜成 USD 會把 6,323 TWD 標成 6,323 USD——寧可退回請求幣別
_CURRENCY_BY_SYMBOL = {"NT$": "TWD", "US$": "USD", "HK$": "HKD", "₩": "KRW", "¥": "JPY", "€": "EUR", "£": "GBP"}
_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})\s*(AM|PM)?", re.I)
# 純文字退路的航班號：兩碼英文（IT/BX/CI）或數字＋英文（7C）＋2~4 碼數字。
# 刻意不收「英文＋數字」的代號（B7），因為那會把 "Airbus A321" 當成航班；
# 也刻意要求至少兩碼數字，否則 "CO2 emissions" 會被當成 CO2 航班
_FLIGHT_TEXT_RE = re.compile(r"\b([A-Z]{2}|\d[A-Z])\s?(\d{2,4})\b")
_AIRPORT_PAIR_RE = re.compile(r"^[A-Z]{3}\s*[–—-]\s*[A-Z]{3}$")
_AIRLINE_SKIP = ("nonstop", "non-stop", "direct", "round trip", "one way", "separate tickets", "self transfer",
                 "operated by", "overnight", "change of", "cheapest", "emissions", "avg", "typical",
                 "直達", "直飛", "來回", "單程", "去程", "回程", "轉機", "排放")

_SCRIPT_RE = re.compile(r"<(script|style)\b.*?</\1>", re.I | re.S)
_BLOCK_TAG_RE = re.compile(
    r"</?(?:div|li|ul|ol|p|br|tr|td|th|h[1-6]|section|article|nav|header|footer|table|button)\b[^>]*>", re.I)
_ANY_TAG_RE = re.compile(r"<[^>]+>")


# ---------- 純解析（不需要瀏覽器） ----------

def _strip_tags(html: str) -> str:
    """HTML → 近似 innerText：區塊標籤換行、行內標籤直接拿掉。

    行內標籤不換行是關鍵：`<span>TPE</span>–<span>PUS</span>` 要留成一行 "TPE–PUS"，
    拆成三行的話 "TPE" 會被航空公司的文字啟發法誤認成公司名。
    """
    text = _SCRIPT_RE.sub(" ", html or "")
    text = _BLOCK_TAG_RE.sub("\n", text)
    text = _ANY_TAG_RE.sub("", text)
    return "\n".join(ln.strip() for ln in unescape(text).splitlines() if ln.strip())


def _to_hhmm(text: str) -> str:
    """'4:50 PM' → '16:50'；'12:05 AM+1' → '00:05'（跨日標記只影響日期，不影響時刻）。"""
    m = _TIME_RE.search(text or "")
    if not m:
        return ""
    h, mnt, ampm = int(m.group(1)), int(m.group(2)), (m.group(3) or "").upper()
    if ampm == "PM" and h != 12:
        h += 12
    elif ampm == "AM" and h == 12:
        h = 0
    return f"{h:02d}:{mnt:02d}" if 0 <= h < 24 and 0 <= mnt < 60 else ""


def _to_minutes(text: str) -> int:
    """'3 hr 10 min' → 190；'10 hr' → 600；'3 小時 15 分' → 195；解析不出來回 0（不明，不推估）。"""
    hours = re.search(r"(\d+)\s*(?:hr|hour|小時|時)", text or "", re.I)
    mins = re.search(r"(\d+)\s*(?:min|分鐘|分)", text or "", re.I)
    return (int(hours.group(1)) * 60 if hours else 0) + (int(mins.group(1)) if mins else 0)


def _explicit_stops(text: str | None) -> int | None:
    """只認頁面自己寫的直達／轉機標記；頁面沒寫就回 None，交給呼叫端用航段數推。"""
    low = (text or "").lower()
    if not low:
        return None
    if any(w in low for w in ("nonstop", "non-stop", "direct", "直達", "直飛")):
        return 0
    m = re.search(r"(\d+)\s*(?:stops?|次轉機)", low)
    if m:
        return int(m.group(1))
    # 中文頁有時只寫「轉機」不給次數；當成 1 是保守解——寧可漏掉也不要把轉機班當直飛報出去
    return 1 if "轉機" in low else None


def _to_stops(text: str | None, segment_count: int) -> int:
    """優先讀頁面自己寫的 'Nonstop' / '1 stop'；沒有這個標記才用航段數推。"""
    explicit = _explicit_stops(text)
    return explicit if explicit is not None else max(segment_count - 1, 0)


def _empty_leg() -> dict:
    return {"flights": [], "codes": [], "airline_name": "", "depart_time": "", "arrive_time": "",
            "duration_min": 0, "stops": None}


def _leg_from_html(html: str) -> dict:
    """已知 class / aria-label 的解析路徑；抓不到的欄位留空，由文字退路補。"""
    leg = _empty_leg()
    for _tag, raw in _FLIGHT_RE.findall(html or ""):
        m = _FLIGHT_CODE_RE.match(" ".join(unescape(raw).split()))
        if not m:
            continue
        flight = m.group(1) + m.group(2)
        # 展開的明細會把同一個航班號畫兩次（摘要列一次、機型列一次），去重
        if flight not in leg["flights"]:
            leg["flights"].append(flight)
            leg["codes"].append(m.group(1))
    depart, arrive = _DEPART_RE.search(html or ""), _ARRIVE_RE.search(html or "")
    duration = _DURATION_RE.search(html or "") or _DURATION_ARIA_RE.search(html or "")
    stops_text = _STOPS_RE.search(html or "")
    airline = _AIRLINE_RE.search(html or "")
    leg["depart_time"] = _to_hhmm(depart.group(1)) if depart else ""
    leg["arrive_time"] = _to_hhmm(arrive.group(1)) if arrive else ""
    leg["duration_min"] = _to_minutes(duration.group(1)) if duration else 0
    leg["stops"] = _explicit_stops(stops_text.group(1)) if stops_text else None
    leg["airline_name"] = airline.group(1).strip() if airline else ""
    return leg


def _airline_from_lines(lines: list[str]) -> str:
    """卡片文字裡挑航空公司名：第一行「沒有數字、不是機場對、不是版面字眼」的字。

    Google 會換 class 但不會不寫公司名，所以這條啟發法比認 class 耐用；
    認錯的代價也只是名稱難看，不會影響價格或時間這些會拿去比較的欄位。
    """
    for line in lines:
        low = line.lower()
        if any(ch.isdigit() for ch in line) or _AIRPORT_PAIR_RE.match(line):
            continue
        if len(line) < 2 or len(line) > 40 or any(w in low for w in _AIRLINE_SKIP):
            continue
        if line.isupper() and len(line) <= 3:  # 落單的機場代碼
            continue
        return line
    return ""


def _leg_from_text(text: str) -> dict:
    """可見文字的退路解析：class 全被換掉時，卡片長怎樣人看得懂，這裡就還讀得出來。"""
    leg = _empty_leg()
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    times = [t for t in (_to_hhmm(m.group(0)) for m in _TIME_RE.finditer(text or "")) if t]
    leg["depart_time"] = times[0] if times else ""
    leg["arrive_time"] = times[1] if len(times) > 1 else ""
    leg["duration_min"] = _to_minutes(text or "")
    leg["stops"] = _explicit_stops(text or "")
    leg["airline_name"] = _airline_from_lines(lines)
    for code, num in _FLIGHT_TEXT_RE.findall(text or ""):
        flight = f"{code}{num}"
        if flight not in leg["flights"]:
            leg["flights"].append(flight)
            leg["codes"].append(code)
    return leg


def parse_leg(html: str = "", text: str = "") -> dict:
    """一段（去程或回程）→ 航班號、起降時刻、時長、轉機次數、航空公司。

    先走 class / aria-label，缺什麼再拿可見文字補；兩邊都沒有就留空值代表「解析不到」。
    `text` 給的是瀏覽器的 innerText（最準）；沒給就從 HTML 自己還原一份。
    """
    text = text or _strip_tags(html)
    from_html, from_text = _leg_from_html(html), _leg_from_text(text)
    flights, codes = ((from_html["flights"], from_html["codes"]) if from_html["flights"]
                      else (from_text["flights"], from_text["codes"]))
    stops = from_html["stops"] if from_html["stops"] is not None else from_text["stops"]
    return {
        "flights": flights,
        "codes": codes,
        "airline": "/".join(dict.fromkeys(codes)),
        "airline_name": from_html["airline_name"] or from_text["airline_name"],
        "depart_time": from_html["depart_time"] or from_text["depart_time"],
        "arrive_time": from_html["arrive_time"] or from_text["arrive_time"],
        "duration_min": from_html["duration_min"] or from_text["duration_min"],
        "stops": stops if stops is not None else max(len(flights) - 1, 0),
    }


def _has_identity(leg: dict) -> bool:
    """一段至少要有航班號，或起降時刻兩個都在，才算解析成功。

    收合的卡片不見得寫航班號（真實頁面就只有「台灣虎航 16:50–20:05 直達」），
    所以不能像第一階段那樣硬性要求航班號，否則整頁都會被丟掉。
    """
    return bool(leg["flights"] or (leg["depart_time"] and leg["arrive_time"]))


def _price_from(html: str, text: str, fallback_currency: str) -> tuple[int, str] | None:
    """(金額, 幣別)；沒有價格或價格 <= 0 回 None——不用 0 或推估值填補。"""
    for source in (html or "", text or ""):
        code = _PRICE_CODE_RE.search(source)
        if code:
            amount = int(code.group(2).replace(",", ""))
            return (amount, code.group(1)) if amount > 0 else None
        symbol = _PRICE_RE.search(source)
        if symbol:
            amount = int(symbol.group(2).replace(",", ""))
            return (amount, _CURRENCY_BY_SYMBOL.get(symbol.group(1), fallback_currency)) if amount > 0 else None
    return None


def _parse_row(html: str, fallback_currency: str) -> Offer | None:
    """一列 `<li>` → `Offer`。同一列裡有兩段（去程/回程）就當來回，只有一段就只填去程。"""
    chunks = _LEG_SPLIT_RE.split(html)[1:]
    legs = [parse_leg(c) for c in chunks] if chunks else [parse_leg(html)]
    if not legs or not _has_identity(legs[0]):
        return None
    price = _price_from(html, "", fallback_currency)
    # 沒有價格的列（頁面顯示 Price unavailable）整列略過
    if price is None:
        return None
    amount, currency = price
    out = legs[0]
    ret = legs[1] if len(legs) > 1 else parse_leg("")
    row_airline = _AIRLINE_RE.search(html)
    airline_name = row_airline.group(1).strip() if row_airline else (out["airline_name"] or out["airline"])
    return Offer(
        price=amount, currency=currency,
        airline=out["airline"], airline_name=airline_name,
        flights=out["flights"], stops=out["stops"],
        depart_time=out["depart_time"], arrive_time=out["arrive_time"], duration_min=out["duration_min"],
        section="playwright",
        return_depart_time=ret["depart_time"], return_arrive_time=ret["arrive_time"],
        return_flights=ret["flights"], return_stops=ret["stops"],
        return_airline=ret["airline"],
        # 同一列的兩段通常同一家；回程沒有自己的名稱時才沿用列上的公司名
        return_airline_name=(ret["airline_name"] or (airline_name if ret["flights"] else ret["airline"])),
        return_duration_min=ret["duration_min"],
    )


def parse_result_html(html: str, currency: str = "TWD") -> list[Offer]:
    """從渲染後的結果頁抽出所有報價，維持頁面原有順序；任何形狀不對的列直接跳過。

    `currency` 只在頁面幣別無法辨識時當退路；能認出 NT$/US$/TWD 這類明確標示時以頁面為準。
    """
    offers = []
    for row in _ROW_RE.findall(html or ""):
        o = _parse_row(row, currency)
        if o is not None:
            offers.append(o)
    return offers


# ---------- 渲染結果的資料形狀 ----------

@dataclass
class Card:
    """結果清單裡的一張航班卡：`text` 是瀏覽器給的可見文字，`html` 是同一節點的 outerHTML。

    兩份都留：文字耐得住 class 改名，HTML 留著才能事後重新解析而不用再開一次瀏覽器。

    `detail_*` 是按開「航班詳細資料」之後的同一張卡。實測收合的卡片上根本沒有航班號
    （頁面只畫「台灣虎航 8:50 PM–10:30 PM 直達」），`IT 607` 只在展開的明細裡；
    反過來「Nonstop」只在收合的摘要上，所以兩份都要留、解析時合著看。
    """
    text: str = ""
    html: str = ""
    detail_text: str = ""
    detail_html: str = ""


@dataclass
class ReturnPage:
    """點掉第 `outbound_index` 張去程卡片之後看到的回程選擇頁。"""
    outbound_index: int = 0
    url: str = ""
    html: str = ""
    cards: list[Card] = field(default_factory=list)


@dataclass
class RenderedTrip:
    """一次渲染的全部產出。`notes` 記流程走到哪、命中哪個選擇器，會帶進 FetchResult.reason。"""
    outbound_html: str = ""
    final_url: str = ""
    outbound_cards: list[Card] = field(default_factory=list)
    return_pages: list[ReturnPage] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def card_leg(card: Card) -> dict:
    """一張卡片（收合摘要 ＋ 展開明細）→ 一段航程。

    兩份接起來丟給 `parse_leg`：摘要提供時刻／航程／直達標記，明細補上航班號；
    沒展開成功的卡片 `detail_*` 是空的，等於退回只讀摘要，欄位少但不會壞。
    """
    return parse_leg(card.html + card.detail_html, card.text + "\n" + card.detail_text)


def _as_rendered(result) -> RenderedTrip:
    """相容兩種 renderer：新的回 `RenderedTrip`，舊的（含測試替身）回 `(html, final_url)`。"""
    if isinstance(result, RenderedTrip):
        return result
    html, final_url = result
    return RenderedTrip(outbound_html=html or "", final_url=final_url or "")


# ---------- 兩階段組裝 ----------

def _return_candidates(page: ReturnPage, outbound: dict, currency: str) -> list[tuple[dict, tuple[int, str] | None]]:
    """回程頁上可用的直飛回程，依價格由低到高；沒有價格的排最後。

    回程卡片標的是「這個去程＋這個回程」的來回總價，所以比它就是比總價。
    會跳過與去程時刻完全相同的卡片——那是頁面頂端「已選去程」的摘要，不是回程選項。
    """
    out = []
    for card in page.cards:
        leg = card_leg(card)
        if not _has_identity(leg):
            continue
        if (leg["depart_time"], leg["arrive_time"]) == (outbound["depart_time"], outbound["arrive_time"]):
            continue
        if leg["stops"] != 0:  # 使用者只要直飛
            continue
        out.append((leg, _price_from(card.html, card.text, currency)))
    out.sort(key=lambda pair: pair[1][0] if pair[1] else float("inf"))
    return out


def _build_offer(card: Card, page: ReturnPage | None, currency: str) -> Offer | None:
    out = card_leg(card)
    if not _has_identity(out):
        return None
    price = _price_from(card.html, card.text, currency)
    ret, ret_price = parse_leg(""), None
    if page is not None:
        candidates = _return_candidates(page, out, currency)
        if candidates:
            ret, ret_price = candidates[0]
    # 回程頁的價格才是這個去程＋這個回程的來回總價；去程卡片上的只是「配最便宜回程」的估計
    price = ret_price or price
    if price is None:
        return None
    amount, resolved_currency = price
    return Offer(
        price=amount, currency=resolved_currency,
        airline=out["airline"], airline_name=out["airline_name"] or out["airline"],
        flights=out["flights"], stops=out["stops"],
        depart_time=out["depart_time"], arrive_time=out["arrive_time"], duration_min=out["duration_min"],
        section="playwright",
        return_depart_time=ret["depart_time"], return_arrive_time=ret["arrive_time"],
        return_flights=ret["flights"], return_stops=ret["stops"],
        return_airline=ret["airline"], return_airline_name=ret["airline_name"] or ret["airline"],
        return_duration_min=ret["duration_min"],
    )


def build_offers(rendered: RenderedTrip, currency: str = "TWD") -> list[Offer]:
    """渲染結果 → `Offer` 清單。三種形狀都吃：

    - 有回程選擇頁（正常的兩階段流程）→ 去程卡片 ＋ 該頁最便宜的直飛回程，欄位齊全
    - 只有去程卡片（沒點成功／回程頁沒等到）→ 只填去程，回程欄位留空標 return_time_unavailable
    - 只有整頁 HTML（舊 renderer，或單頁本來就含兩段）→ 退回 `parse_result_html()`
    """
    if not rendered.outbound_cards:
        return parse_result_html(rendered.outbound_html, currency)
    pages = {p.outbound_index: p for p in rendered.return_pages}
    offers = []
    for index, card in enumerate(rendered.outbound_cards):
        o = _build_offer(card, pages.get(index), currency)
        if o is not None:
            offers.append(o)
    return offers


# ---------- 瀏覽器層（唯一碰 Playwright 的地方） ----------

def _norm(text: str) -> str:
    return " ".join((text or "").split())


def _first_locator(scope, selectors: tuple[str, ...]):
    """回傳第一個真的有節點的 (locator, selector)；全部落空回 (None, "")。

    整段吞例外：選擇器語法在不同 Playwright 版本支援度不同，一個不行就換下一個，
    這裡的失敗不該讓整輪掛掉。
    """
    for sel in selectors:
        try:
            loc = scope.locator(sel)
            if loc.count():
                return loc, sel
        except Exception:
            continue
    return None, ""


def _wait_any(page, selectors: tuple[str, ...], timeout_ms: int, notes: list[str], label: str) -> str:
    """依序等候候選選擇器，命中就回傳它，全部落空回 ""。

    每個候選只分到總時間的一份：寧可少等幾秒換下一個寫法，也不要在死掉的選擇器上卡滿。
    """
    slice_ms = max(int(timeout_ms / max(len(selectors), 1)), 1_500)
    for sel in selectors:
        try:
            page.wait_for_selector(sel, timeout=slice_ms, state="attached")
            notes.append(f"{label}:{sel}")
            return sel
        except Exception:
            continue
    notes.append(f"{label}:none")
    return ""


def _count_cards(page) -> tuple[int, str]:
    loc, sel = _first_locator(page, CARD_SELECTORS)
    try:
        return (loc.count(), sel) if loc is not None else (0, "")
    except Exception:
        return 0, ""


def _wait_for_cards(page, notes: list[str], label: str, timeout_ms: int = CARD_SETTLE_TIMEOUT_MS) -> int:
    """輪詢到卡片真的長出來、而且數量不再變動為止；回傳最後看到的張數。

    固定沉澱一段時間在慢的時候會抓到空清單（outbound_cards:none），
    在快的時候又白等——改成看「連續 CARD_STABLE_ROUNDS 次數量沒變」，
    慢就多等、快就早走，而且清單分批塞進來時不會只抓到前半截。
    """
    waited, last, stable = 0, -1, 0
    sel = ""
    while waited < timeout_ms:
        count, sel = _count_cards(page)
        if count and count == last:
            stable += 1
            if stable >= CARD_STABLE_ROUNDS:
                notes.append(f"{label}_settled:{sel}={count}")
                return count
        else:
            stable = 0
        last = count
        page.wait_for_timeout(CARD_POLL_MS)
        waited += CARD_POLL_MS
    notes.append(f"{label}_settled:timeout={max(last, 0)}")
    return max(last, 0)


def _check_response(resp, notes: list[str]) -> None:
    """403/429 立刻當作被擋：中止整輪，不重試、不換身分再試。"""
    status = getattr(resp, "status", None)
    if not isinstance(status, int):
        return
    if status in BLOCK_STATUSES:
        raise BlockedError(f"HTTP {status}")
    if status >= 400:
        notes.append(f"http_{status}")


def _raise_if_blocked(html: str, url: str) -> None:
    block = detect_block(html, url)
    if block:
        raise BlockedError(block)


def _dismiss_consent(page, notes: list[str]) -> None:
    """按頁面自己提供的同意鈕（優先「全部拒絕」）；按不掉就留給 `detect_block` 判成被擋。

    這不是繞過：同意頁本來就是給人按的正常互動。真正的反機器人頁（captcha / sorry）一律不碰。
    """
    loc, sel = _first_locator(page, CONSENT_SELECTORS)
    if loc is None:
        return
    try:
        loc.first.click(timeout=ACTION_TIMEOUT_MS)
        page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT_MS)
        notes.append(f"consent:{sel}")
    except Exception as e:
        notes.append(f"consent_failed:{type(e).__name__}")


def _collect_cards(page, notes: list[str], label: str, limit: int = 0, expand: int = 0) -> list[Card]:
    """把結果清單的每張卡片抓成 (innerText, outerHTML)；`expand` 指定前幾張要展開拿航班號。"""
    loc, sel = _first_locator(page, CARD_SELECTORS)
    if loc is None:
        notes.append(f"{label}_cards:none")
        return []
    total = loc.count()
    notes.append(f"{label}_cards:{sel}={total}")
    cards = []
    for i in range(min(total, limit) if limit else total):
        el = loc.nth(i)
        try:
            card = Card(text=el.inner_text(timeout=ACTION_TIMEOUT_MS), html=el.evaluate("e => e.outerHTML"))
        except Exception as e:
            # 單張卡片抓不到（節點被重繪）不影響其他張
            notes.append(f"{label}_card{i}_failed:{type(e).__name__}")
            continue
        if i < expand:
            card.detail_text, card.detail_html = _expand_card(page, el, notes, f"{label}{i}")
        cards.append(card)
    return cards


def _click_click(target, notes: list[str], label: str) -> bool:
    """點一個節點：真實點擊不行就補一發 dispatch_event。

    卡片常被上層透明節點蓋住或正在重繪，真實點擊會被判成不可點而逾時；
    但換頁是 jsaction 收到 click 事件觸發的，所以 dispatch 是有效的退路。
    """
    try:
        target.click(timeout=ACTION_TIMEOUT_MS)
        notes.append(label)
        return True
    except Exception:
        pass
    try:
        target.dispatch_event("click")
        notes.append(f"{label}+dispatch")
        return True
    except Exception:
        return False


def _expand_card(page, card, notes: list[str], label: str) -> tuple[str, str]:
    """按開卡片的「航班詳細資料」拿航班號，拿完再按回去。

    航班號只在展開的明細裡；按回去是為了讓後續的點擊（去程卡片的 `.yR1fYc`）
    面對跟展開前一樣的版面，不然收合／展開兩種高度會讓座標點擊踩空。
    """
    toggle, sel = _first_locator(card, EXPAND_SELECTORS)
    if toggle is None:
        notes.append(f"{label}_expand:none")
        return "", ""
    button = toggle.first
    if not _click_click(button, notes, f"{label}_expand:{sel}"):
        notes.append(f"{label}_expand_failed:click")
        return "", ""
    detail_text = detail_html = ""
    if _wait_expanded(page, button, notes, label):
        try:
            detail_text = card.inner_text(timeout=ACTION_TIMEOUT_MS)
            detail_html = card.evaluate("e => e.outerHTML")
        except Exception as e:
            notes.append(f"{label}_expand_read_failed:{type(e).__name__}")
    _click_click(button, [], f"{label}_collapse")  # 收回去的成敗不值得記進 notes
    return detail_text, detail_html


def _wait_expanded(page, button, notes: list[str], label: str) -> bool:
    """等 aria-expanded 翻成 true 再讀：明細是動畫塞進來的，翻 true 的當下還沒畫完。"""
    waited = 0
    while waited < EXPAND_TIMEOUT_MS:
        try:
            if button.get_attribute("aria-expanded") == "true":
                page.wait_for_timeout(EXPAND_SETTLE_MS)
                return True
        except Exception:
            notes.append(f"{label}_expand:detached")
            return False
        page.wait_for_timeout(CARD_POLL_MS)
        waited += CARD_POLL_MS
    notes.append(f"{label}_expand:timeout")
    return False


def _click_outbound(page, index: int, notes: list[str]) -> bool:
    """點第 index 張去程卡片：優先點已確認的 div.yR1fYc，再退到其他節點與卡片本身。"""
    loc, _sel = _first_locator(page, CARD_SELECTORS)
    if loc is None or index >= loc.count():
        notes.append(f"click{index}:missing_card")
        return False
    card = loc.nth(index)
    try:
        confirmed = card.locator(OUTBOUND_CLICK_SELECTOR).first
        if confirmed.count() and _click_click(confirmed, notes, f"click{index}:{OUTBOUND_CLICK_SELECTOR}"):
            return True
    except Exception:
        pass
    for sel in CARD_CLICK_SELECTORS:
        try:
            target = card.locator(sel).first
            if target.count() and target.is_visible() and _click_click(target, notes, f"click{index}:{sel}"):
                return True
        except Exception:
            continue
    if _click_click(card, notes, f"click{index}:self"):
        return True
    notes.append(f"click{index}_failed:all_strategies")
    return False


def _wait_for_return(page, outbound_text: str, notes: list[str]) -> bool:
    """等回程選擇頁：先等「Returning flights」這類標題，退路是「清單第一張卡片換人了」。

    第二條退路是給標題文案改掉／換語系用的：Google Flights 點進回程後是 SPA 換內容，
    第一張卡片不再等於剛剛點的那張去程，就代表清單重繪成回程了。
    """
    if _wait_any(page, RETURN_READY_SELECTORS, RETURN_TIMEOUT_MS // 2, notes, "return_ready"):
        return True
    waited = 0
    while waited < RETURN_TIMEOUT_MS // 2:
        page.wait_for_timeout(CARD_POLL_MS)
        waited += CARD_POLL_MS
        probe = _collect_cards(page, [], "probe", limit=1)
        if probe and _norm(probe[0].text) != _norm(outbound_text):
            notes.append("return_ready:list_changed")
            return True
    notes.append("return_ready:timeout")
    return False


def _dump_debug(page, debug_dir: str | None, tag: str, notes: list[str]) -> None:
    """留下當下的 HTML 與整頁截圖，方便事後對照選擇器。沒設 `debug_dir` 就什麼都不寫。"""
    if not debug_dir:
        return
    try:
        target = Path(debug_dir)
        target.mkdir(parents=True, exist_ok=True)
        stem = f"{_stamp().replace(':', '')}_{tag}"
        (target / f"{stem}.html").write_text(page.content(), encoding="utf-8")
        page.screenshot(path=str(target / f"{stem}.png"), full_page=True)
        notes.append(f"debug:{stem}")
    except Exception as e:  # 除錯輸出壞掉不該讓整輪失敗
        notes.append(f"debug_failed:{type(e).__name__}")


def _outbound_indexes_to_click(cards: list[Card], limit: int) -> list[int]:
    """只點直飛的去程（其他反正會被過濾掉）；一張都認不出直飛時仍點前幾張。

    後者是防選擇器腐爛：直達標記解析不到就整批放棄的話，頁面一改我們就靜悄悄地零產出。
    """
    direct = [i for i, c in enumerate(cards) if card_leg(c)["stops"] == 0]
    return (direct or list(range(len(cards))))[:max(limit, 0)]


def _expand_outbound(page, index: int, card: Card, notes: list[str]) -> None:
    """就地補上第 index 張去程卡片的展開明細；抓不到節點就維持原樣（少航班號，不算失敗）。"""
    loc, _sel = _first_locator(page, CARD_SELECTORS)
    if loc is None or index >= loc.count():
        notes.append(f"out{index}_expand:missing_card")
        return
    card.detail_text, card.detail_html = _expand_card(page, loc.nth(index), notes, f"out{index}")


def _capture_return_page(page, index: int, outbound: Card, notes: list[str], debug_dir: str | None) -> ReturnPage | None:
    if not _click_outbound(page, index, notes):
        return None
    if not _wait_for_return(page, outbound.text, notes):
        _dump_debug(page, debug_dir, f"return_timeout_{index}", notes)
        return None
    _wait_for_cards(page, notes, f"return{index}", RETURN_TIMEOUT_MS)
    html, url = page.content(), page.url
    _raise_if_blocked(html, url)
    cards = _collect_cards(page, notes, f"return{index}", expand=MAX_RETURN_EXPAND)
    if not cards:
        _dump_debug(page, debug_dir, f"return_no_cards_{index}", notes)
        return None
    return ReturnPage(outbound_index=index, url=url, html=html, cards=cards)


def _back_to_outbound(page, url: str, notes: list[str]) -> bool:
    """回到去程清單準備點下一張；go_back 失敗就重新導航一次原始網址。"""
    try:
        page.go_back(timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")
    except Exception:
        try:
            _check_response(page.goto(url, timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded"), notes)
        except BlockedError:
            raise
        except Exception as e:
            notes.append(f"back_failed:{type(e).__name__}")
            return False
    if not _wait_any(page, READY_SELECTORS, RESULT_TIMEOUT_MS, notes, "back"):
        return False
    return _wait_for_cards(page, notes, "back") > 0


def _playwright_render(url: str, user_agent: str, headless: bool = True, *,
                       max_outbound: int = MAX_OUTBOUND_CARDS, debug_dir: str | None = None) -> RenderedTrip:
    """預設 renderer：開 headless Chromium 走完「去程清單 → 點卡片 → 回程選擇頁」。

    等不到結果、點不進回程都不算例外——回傳能拿到的部分並在 `notes` 說明，
    由呼叫端依內容決定是 unavailable 還是可用；只有被擋（403/429/captcha/consent）才丟例外。
    """
    from playwright.sync_api import sync_playwright

    notes: list[str] = []
    trip = RenderedTrip(notes=notes)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        try:
            context = browser.new_context(user_agent=user_agent, locale="en-US", timezone_id="Asia/Taipei",
                                          viewport={"width": 1440, "height": 2400})
            page = context.new_page()
            page.set_default_timeout(ACTION_TIMEOUT_MS)
            _check_response(page.goto(url, timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded"), notes)
            _dismiss_consent(page, notes)
            _wait_any(page, READY_SELECTORS, RESULT_TIMEOUT_MS, notes, "results")
            _wait_for_cards(page, notes, "outbound")
            trip.outbound_html, trip.final_url = page.content(), page.url
            _raise_if_blocked(trip.outbound_html, trip.final_url or url)
            trip.outbound_cards = _collect_cards(page, notes, "outbound")
            if not trip.outbound_cards:
                _dump_debug(page, debug_dir, "no_outbound_cards", notes)
                return trip
            for index in _outbound_indexes_to_click(trip.outbound_cards, max_outbound):
                # 只展開真的要點進去的那幾張：26 張全展開是 52 次點擊，慢且沒必要
                _expand_outbound(page, index, trip.outbound_cards[index], notes)
                captured = _capture_return_page(page, index, trip.outbound_cards[index], notes, debug_dir)
                if captured is not None:
                    trip.return_pages.append(captured)
                if not _back_to_outbound(page, url, notes):
                    notes.append("stopped:cannot_return_to_outbound")
                    break
            if not trip.return_pages:
                _dump_debug(page, debug_dir, "no_return_page", notes)
            log.info("rendered %s: %s", url, "; ".join(notes))
            return trip
        finally:
            browser.close()


class PlaywrightFetcher(Fetcher):
    name = "playwright"

    def __init__(self, currency: str = "TWD", user_agent: str = DEFAULT_UA, origin: str = ORIGIN, dest: str = DEST,
                 render=None, headless: bool = True, keep_html: bool = False,
                 max_outbound: int = MAX_OUTBOUND_CARDS, debug_dir: str | None = None):
        self.currency, self.origin, self.dest = currency, origin, dest
        self.user_agent, self.headless, self.keep_html = user_agent, headless, keep_html
        self.max_outbound, self.debug_dir = max_outbound, debug_dir
        self._render = render or partial(_playwright_render, max_outbound=max_outbound, debug_dir=debug_dir)

    def _result(self, it: Itinerary, url: str, status: str, **kw) -> FetchResult:
        return FetchResult(self.name, it.key, status, url, _stamp(), **kw)

    def fetch(self, it: Itinerary) -> FetchResult:
        url = google_flights_url(it, self.origin, self.dest, self.currency)
        try:
            rendered = _as_rendered(self._render(url, self.user_agent, self.headless))
        except BlockedError:
            raise  # 被擋要一路往上冒到 CLI 中止整輪，不能被下面的 error 吞掉
        except ImportError as e:
            # playwright 沒裝。它是 fli 的 fallback，只有真的要退回來時才需要——
            # 給一個固定可 grep 的代碼，報告看得出「不是抓不到，是這台機器沒有瀏覽器來源」
            return self._result(it, url, "error", reason=f"playwright_unavailable: {e}")
        except Exception as e:
            # 瀏覽器層的錯誤（啟動失敗、導航逾時）不重試：這裡沒有便宜的退路，回 error 讓下一輪再來
            return self._result(it, url, "error", reason=f"render_failed: {type(e).__name__}: {e}")
        for html, page_url in ([(rendered.outbound_html, rendered.final_url or url)]
                               + [(p.html, p.url or url) for p in rendered.return_pages]):
            _raise_if_blocked(html, page_url)
        offers = build_offers(rendered, self.currency)
        note = f" [{'; '.join(rendered.notes)}]" if rendered.notes else ""
        if not offers:
            return self._result(it, url, "unavailable", html=rendered.outbound_html,
                                reason=f"no_offers_parsed: rendered page had no parseable result rows{note}")
        # 使用者要求不轉機：去程與回程都得直飛
        direct = [o for o in offers if o.nonstop]
        if not direct:
            return self._result(it, url, "unavailable", html=rendered.outbound_html,
                                reason=f"no_direct_offers: parsed {len(offers)} offers, none nonstop both ways{note}")
        return self._result(it, url, "ok", offers=direct,
                            html=rendered.outbound_html if self.keep_html else None)
