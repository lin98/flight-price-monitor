"""把每輪結果落地到 data/：

- raw/<行程 key>/<時間戳>_<來源>.json   解析後的報價與原始資料片段（失敗時另存 .html.gz 供事後除錯）
- reports/fare_watch_<run_id>.{md,json} 每輪報告，另複寫 fare_watch_latest.* 方便排程後直接讀
- fare_watch_observations.jsonl          跨輪價格觀測，同日同來源同價去重，用來看價格走勢
- logs/                                   CLI 與 launchd 的 log
- grouped/grouped_<run_id>.{md,json}      三人分組比價每輪報告，另複寫 data/grouped_latest.*
                                          （--grouped 模式 raw/ 與觀測紀錄都多一層 origin）
"""

from __future__ import annotations

import datetime as dt
import gzip
import json
from pathlib import Path

from .fetchers import FetchResult
from .itineraries import Itinerary


class Storage:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.raw = self.root / "raw"
        self.reports = self.root / "reports"
        self.logs = self.root / "logs"
        for d in (self.raw, self.reports, self.logs):
            d.mkdir(parents=True, exist_ok=True)
        self.observations = self.root / "fare_watch_observations.jsonl"

    # ---------- 觀測紀錄 ----------

    @staticmethod
    def _dedupe_key(obs: dict) -> str:
        # 分組模式同一行程會分別查 TPE / KHH，兩者價格獨立，去重時要把機場算進去；舊紀錄沒有 origin 維持原格式
        origin = obs.get("origin")
        prefix = f"{origin}|" if origin else ""
        return f"{obs['observed_on']}|{prefix}{obs['key']}|{obs['source']}|{obs['min_price']}"

    def load_observations(self) -> list[dict]:
        if not self.observations.exists():
            return []
        out = []
        for line in self.observations.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
        return out

    def existing_keys(self) -> set[str]:
        return {self._dedupe_key(o) for o in self.load_observations()}

    def append_observation(self, it: Itinerary, res: FetchResult, run_id: str, known: set[str] | None = None,
                           origin: str | None = None) -> bool:
        """只記有價格的結果；同一天同來源（同機場）看到同樣的最低價不重複寫，回傳是否新增。"""
        if res.status != "ok" or res.min_price is None:
            return False
        if known is None:
            known = self.existing_keys()
        cheapest = res.cheapest
        obs = {
            "observed_on": res.fetched_at[:10], "fetched_at": res.fetched_at, "run_id": run_id,
            "key": it.key, "depart": it.depart.isoformat(), "return": it.return_.isoformat(),
            "leave_days": it.leave_days, "source": res.source, "min_price": res.min_price,
            "currency": cheapest.currency if cheapest else None,
            "cheapest_offer": cheapest.to_dict() if cheapest else None, "offers_count": len(res.offers),
        }
        if origin:
            obs["origin"] = origin
        k = self._dedupe_key(obs)
        if k in known:
            return False
        known.add(k)
        with self.observations.open("a", encoding="utf-8") as f:
            f.write(json.dumps(obs, ensure_ascii=False) + "\n")
        return True

    # ---------- 原始資料 ----------

    @staticmethod
    def _stamp(fetched_at: str) -> str:
        try:
            return dt.datetime.fromisoformat(fetched_at).strftime("%Y%m%dT%H%M%S")
        except ValueError:
            return "".join(c for c in fetched_at if c.isalnum())

    def save_raw(self, it: Itinerary, res: FetchResult, keep_html: bool = False, origin: str | None = None) -> Path:
        d = (self.raw / origin / it.key) if origin else (self.raw / it.key)
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{self._stamp(res.fetched_at)}_{res.source}.json"
        payload = {"itinerary": it.to_dict(), "result": res.to_dict()}
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        # 成功時 raw_block 已含解析所需資料；整頁 HTML 只在失敗（或明確要求）時保留，否則每輪多好幾十 MB
        if res.html and (res.status != "ok" or keep_html):
            with gzip.open(p.with_suffix(".html.gz"), "wt", encoding="utf-8") as f:
                f.write(res.html)
        return p

    def latest_raw(self, key: str, origin: str | None = None) -> tuple[FetchResult, Path] | None:
        """讀回某個（機場, 行程）最後一次落地的結果；沒有就回 None。

        檔名帶 `%Y%m%dT%H%M%S` 時間戳，同一目錄下字典序＝時間序，所以取檔名最大的那個即可，
        不必開檔比 `fetched_at`（92 個日期 × 2 機場 × 每輪一個檔，逐檔開太慢）。
        """
        d = (self.raw / origin / key) if origin else (self.raw / key)
        files = sorted(d.glob("*.json")) if d.is_dir() else []
        if not files:
            return None
        payload = json.loads(files[-1].read_text(encoding="utf-8"))
        result = payload.get("result")
        if not result:
            return None
        return FetchResult.from_dict(result), files[-1]

    def latest_raw_by_origin(self, keys: list[str], origins: tuple[str, ...]) -> dict[str, dict[str, FetchResult]]:
        """把指定行程 × 機場最後一次落地的結果全讀回來，形狀與 `run_grouped_once` 的 results 一致。"""
        out: dict[str, dict[str, FetchResult]] = {}
        for key in keys:
            for origin in origins:
                found = self.latest_raw(key, origin)
                if found:
                    out.setdefault(key, {})[origin] = found[0]
        return out

    # ---------- 報告 ----------

    def write_report(self, md: str, summary: dict) -> tuple[Path, Path]:
        run_id = summary["run"]["run_id"]
        dated_md = self.reports / f"fare_watch_{run_id}.md"
        dated_json = self.reports / f"fare_watch_{run_id}.json"
        latest_md = self.reports / "fare_watch_latest.md"
        latest_json = self.reports / "fare_watch_latest.json"
        body = json.dumps(summary, ensure_ascii=False, indent=2)
        for p in (dated_md, latest_md):
            p.write_text(md, encoding="utf-8")
        for p in (dated_json, latest_json):
            p.write_text(body, encoding="utf-8")
        return dated_md, latest_json

    def write_grouped(self, md: str, summary: dict) -> tuple[Path, Path]:
        """分組報告：dated 版放 grouped/，latest 版直接放 data/ 根目錄方便排程後讀取。"""
        run_id = summary["run"]["run_id"]
        grouped_dir = self.root / "grouped"
        grouped_dir.mkdir(parents=True, exist_ok=True)
        dated_md, dated_json = grouped_dir / f"grouped_{run_id}.md", grouped_dir / f"grouped_{run_id}.json"
        latest_md, latest_json = self.root / "grouped_latest.md", self.root / "grouped_latest.json"
        body = json.dumps(summary, ensure_ascii=False, indent=2)
        for p in (dated_md, latest_md):
            p.write_text(md, encoding="utf-8")
        for p in (dated_json, latest_json):
            p.write_text(body, encoding="utf-8")
        return dated_md, latest_json
