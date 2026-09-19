"""Amadeus Flight Offers Search 價格來源。

原則：任何拿不到可靠數字的情況（無憑證、網路錯誤、非 2xx、格式不符、無報價、
重試耗盡）一律回 `{"status": "unavailable", "reason": ...}`，絕不回傳估算、快取或
預設值。

環境變數：
- AMADEUS_CLIENT_ID / AMADEUS_CLIENT_SECRET  未設定即整輪 unavailable
- AMADEUS_HOST            `test`（預設）| `production`；也接受完整主機名或 https:// 開頭的 base URL
- AMADEUS_MIN_INTERVAL    連續請求最短間隔秒數（預設 0.5；Amadeus test 環境限制每 100ms 一次）
- AMADEUS_MAX_ATTEMPTS    每個行程最多嘗試次數（預設 3）
"""

from __future__ import annotations

import os
import random
import time

import requests

HOST_ALIASES = {
    "test": "https://test.api.amadeus.com",
    "production": "https://api.amadeus.com",
    "prod": "https://api.amadeus.com",
}
DEFAULT_HOST = "test"
HTTP_TIMEOUT = 20
# 429 與 5xx 屬暫時性，值得退避後重試；4xx 其他狀態（400 參數錯、401 憑證錯）重試也不會好
RETRY_STATUSES = {429, 500, 502, 503, 504}


def resolve_base_url(host: str | None = None) -> str:
    """把 AMADEUS_HOST 的各種寫法統一成 base URL。"""
    raw = (host if host is not None else os.environ.get("AMADEUS_HOST", DEFAULT_HOST)).strip()
    if not raw:
        raw = DEFAULT_HOST
    if raw.lower() in HOST_ALIASES:
        return HOST_ALIASES[raw.lower()]
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw.rstrip("/")
    return f"https://{raw.rstrip('/')}"


def unavailable(reason: str, **extra) -> dict:
    return {"status": "unavailable", "reason": reason, **extra}


class AmadeusClient:
    """單一執行週期共用一個 client：token 只取一次、請求之間節流、失敗退避重試。

    `session` / `sleep` / `clock` 可注入，方便測試不打網路、不真的等待。
    """

    source = "amadeus"

    def __init__(self, client_id: str | None = None, client_secret: str | None = None,
                 host: str | None = None, min_interval: float | None = None,
                 max_attempts: int | None = None, session=None,
                 sleep=time.sleep, clock=time.monotonic):
        env = os.environ
        self.client_id = client_id if client_id is not None else env.get("AMADEUS_CLIENT_ID", "")
        self.client_secret = (client_secret if client_secret is not None
                              else env.get("AMADEUS_CLIENT_SECRET", ""))
        self.base_url = resolve_base_url(host)
        self.min_interval = (min_interval if min_interval is not None
                             else float(env.get("AMADEUS_MIN_INTERVAL", "0.5")))
        self.max_attempts = (max_attempts if max_attempts is not None
                             else int(env.get("AMADEUS_MAX_ATTEMPTS", "3")))
        self._session = session
        self._sleep = sleep
        self._clock = clock
        self._token: str | None = None
        self._last_request_at: float | None = None

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    @property
    def host_label(self) -> str:
        """報告裡顯示的來源環境，讓讀者一眼知道是 test 抽樣資料還是 production。"""
        for alias in ("test", "production"):
            if self.base_url == HOST_ALIASES[alias]:
                return alias
        return self.base_url

    def describe(self) -> dict:
        return {"source": self.source, "host": self.host_label, "base_url": self.base_url,
                "configured": self.configured, "min_interval_sec": self.min_interval,
                "max_attempts": self.max_attempts}

    # ---------- 內部 ----------

    @property
    def session(self):
        if self._session is None:
            self._session = requests.Session()
        return self._session

    def _throttle(self) -> None:
        if self._last_request_at is not None and self.min_interval > 0:
            wait = self.min_interval - (self._clock() - self._last_request_at)
            if wait > 0:
                self._sleep(wait)
        self._last_request_at = self._clock()

    def _backoff(self, attempt: int) -> None:
        # 指數退避加隨機抖動，避免多個排程同時重試打在同一秒
        self._sleep(2 ** (attempt - 1) + random.uniform(0, 0.5))

    def _get_token(self, force: bool = False) -> str:
        if self._token and not force:
            return self._token
        self._throttle()
        resp = self.session.post(
            f"{self.base_url}/v1/security/oauth2/token",
            data={"grant_type": "client_credentials",
                  "client_id": self.client_id, "client_secret": self.client_secret},
            timeout=HTTP_TIMEOUT)
        if resp.status_code != 200:
            raise RuntimeError(f"auth failed: HTTP {resp.status_code} {resp.text[:200]}")
        self._token = resp.json()["access_token"]
        return self._token

    # ---------- 對外 ----------

    def fetch(self, origin: str, dest: str, depart, ret) -> tuple[dict, dict | None]:
        """回傳 (price, raw_body)。price 必為 ok 或 unavailable；raw_body 只在有回應時提供。"""
        if not self.configured:
            return unavailable("no price source configured "
                               "(set AMADEUS_CLIENT_ID / AMADEUS_CLIENT_SECRET)",
                               source=self.source, host=self.host_label), None

        params = {"originLocationCode": origin, "destinationLocationCode": dest,
                  "departureDate": depart.isoformat(), "returnDate": ret.isoformat(),
                  "adults": 1, "currencyCode": "TWD", "max": 20}
        last_reason = "unknown"
        last_body = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                token = self._get_token(force=(last_reason.startswith("http_401")))
                self._throttle()
                resp = self.session.get(
                    f"{self.base_url}/v2/shopping/flight-offers", params=params,
                    headers={"Authorization": f"Bearer {token}"}, timeout=HTTP_TIMEOUT)
            except Exception as exc:  # 網路、認證、JSON 皆視為暫時失敗
                last_reason = f"{type(exc).__name__}: {exc}"
                if attempt < self.max_attempts:
                    self._backoff(attempt)
                continue

            status = resp.status_code
            if status in RETRY_STATUSES:
                last_reason = f"http_{status}" + (" rate_limited" if status == 429 else "")
                if attempt < self.max_attempts:
                    self._backoff(attempt)
                continue
            if status == 401:
                # token 過期或環境不符；換一次新 token 再試
                last_reason = "http_401 unauthorized"
                self._token = None
                continue
            if status != 200:
                detail = _error_detail(resp)
                return unavailable(f"http_{status}: {detail}", source=self.source,
                                   host=self.host_label), _safe_json(resp)

            body = _safe_json(resp)
            last_body = body
            if body is None:
                last_reason = "invalid_json"
                continue
            offers = body.get("data") or []
            priced = []
            for o in offers:
                try:
                    priced.append((float(o["price"]["grandTotal"]), o["price"]["currency"], o))
                except (KeyError, TypeError, ValueError):
                    continue
            if not priced:
                return unavailable("source returned no offers for these dates",
                                   source=self.source, host=self.host_label), body
            amount, currency, best = min(priced, key=lambda p: p[0])
            return {"status": "ok", "amount": amount, "currency": currency,
                    "source": self.source, "host": self.host_label,
                    "offers_count": len(priced),
                    "cheapest_offer": _summarize_offer(best)}, body

        return unavailable(f"retries_exhausted after {self.max_attempts} attempts: {last_reason}",
                           source=self.source, host=self.host_label), last_body


def _safe_json(resp):
    try:
        return resp.json()
    except ValueError:
        return None


def _error_detail(resp) -> str:
    body = _safe_json(resp)
    if isinstance(body, dict) and body.get("errors"):
        e = body["errors"][0]
        return f"{e.get('code', '')} {e.get('title', '')} {e.get('detail', '')}".strip()
    return resp.text[:200]


def _summarize_offer(offer: dict) -> dict:
    """只留給人看的重點；完整 offer 存在 raw 檔裡。"""
    legs = []
    for itin in offer.get("itineraries", []):
        segs = itin.get("segments", [])
        legs.append({
            "flights": [f"{s.get('carrierCode', '')}{s.get('number', '')}" for s in segs],
            "depart_at": segs[0].get("departure", {}).get("at") if segs else None,
            "arrive_at": segs[-1].get("arrival", {}).get("at") if segs else None,
            "stops": max(len(segs) - 1, 0),
        })
    return {"id": offer.get("id"), "validating_airlines": offer.get("validatingAirlineCodes", []),
            "legs": legs}
