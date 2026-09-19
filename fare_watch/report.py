"""把一輪查詢結果整理成摘要：最低票價候選、請假最少候選。

最低票價候選**只**從 status == "ok" 的行程挑；unavailable 沒有數字可比，硬排序就是假答案，
所以拿不到報價時候選清單為空並附說明。
"""

from __future__ import annotations

from collections import Counter

from .fetchers import FetchResult
from .grouped import leg_card
from .holidays_tw import HOLIDAY_SOURCE, long_weekends_2027
from .itineraries import Itinerary, format_date

TOP_N = 5
_FAILED = ("unavailable", "error", "missing")


def _row(it: Itinerary, res: FetchResult | None) -> dict:
    row = it.to_dict()
    if res is None:
        row.update(status="missing", reason="no result recorded", source=None, url="", fetched_at=None,
                   min_price=None, currency=None, cheapest_offer=None, offers_count=0,
                   time_tags=["missing"])
        return row
    cheapest = res.cheapest
    row.update(status=res.status, reason=res.reason, source=res.source, url=res.url, fetched_at=res.fetched_at,
               min_price=res.min_price, currency=cheapest.currency if cheapest else None,
               time_tags=(cheapest.time_tags if cheapest else ["unavailable"]),
               cheapest_offer=cheapest.to_dict() if cheapest else None, offers_count=len(res.offers))
    return row


def build_summary(run_meta: dict, itins: list[Itinerary], results: dict[str, FetchResult]) -> dict:
    rows = [_row(it, results.get(it.key)) for it in itins]
    priced = [r for r in rows if r["status"] == "ok" and r["min_price"] is not None]
    status_counts = Counter(r["status"] for r in rows)
    reasons = Counter(r["reason"] or "unknown" for r in rows if r["status"] in _FAILED)

    cheapest_overall = sorted(priced, key=lambda r: (r["min_price"], r["depart"]))[:TOP_N]
    min_leave = min((r["leave_days"] for r in rows), default=None)
    fewest = [r for r in rows if r["leave_days"] == min_leave]
    # 請假最少當中有報價的依價格排在前面，讓「省假又省錢」一眼可見；沒報價的維持日期順序附在後面
    fewest_sorted = (sorted([r for r in fewest if r["status"] == "ok"], key=lambda r: r["min_price"])
                     + [r for r in fewest if r["status"] != "ok"])

    if priced:
        note = f"從 {len(priced)} 筆有報價的行程中取最低 {len(cheapest_overall)} 筆"
    elif run_meta.get("mode") == "dry-run":
        note = "dry-run 未查價"
    else:
        note = "本輪沒有任何行程取得報價，無法排出最低票價（不對 unavailable 排序）"

    return {
        "run": dict(run_meta),
        "counts": {
            "itineraries": len(rows), "priced": len(priced),
            "status": dict(status_counts), "unavailable_reasons": dict(reasons),
        },
        "min_leave_days": min_leave,
        "fewest_leave": fewest_sorted,
        "cheapest_overall": cheapest_overall,
        "cheapest_note": note,
        "holiday_source": HOLIDAY_SOURCE,
        "long_weekends": long_weekends_2027(),
        "itineraries": rows,
    }


# ---------- Markdown ----------

def _price_cell(r: dict) -> str:
    if r["status"] == "ok":
        return f"{r['min_price']:,} {r['currency']}"
    return r["status"].replace("_", "-")


def _flight_cell(r: dict) -> str:
    """去程與回程各自完整列出（班機號、日期、航空公司、起降時刻）。

    與分組報告共用 `leg_card()`：回程航空公司可能跟去程不同家，不能沿用去程那一個。
    """
    o = r.get("cheapest_offer")
    if not o:
        return "-"
    out = leg_card("去程", r["depart"], r.get("origin", ""), r.get("dest", ""),
                   o["airline_name"], o["airline"], "/".join(o["flights"]),
                   o["depart_time"], o["arrive_time"], o.get("duration_min", 0))
    ret = leg_card("回程", r["return"], r.get("dest", ""), r.get("origin", ""),
                   o.get("return_airline_name", ""), o.get("return_airline", ""),
                   "/".join(o.get("return_flights") or []),
                   o.get("return_depart_time", ""), o.get("return_arrive_time", ""),
                   o.get("return_duration_min", 0), missing_note="return_time_unavailable")
    return f"{out}<br>{ret}"


def _table(rows: list[dict]) -> list[str]:
    out = ["| 出發 | 回程 | 請假 | 每人最低 | 時間標籤 | 航班 | 命中假日 | 查詢 |", "|---|---|---:|---:|---|---|---|---|"]
    for r in rows:
        link = f"[link]({r['url']})" if r.get("url") else "-"
        out.append(f"| {format_date(r['depart'])} | {format_date(r['return'])} | {r['leave_days']} | {_price_cell(r)} | "
                   f"{', '.join(r.get('time_tags', []))} | {_flight_cell(r)} | {', '.join(r['holidays_hit']) or '-'} | {link} |")
    return out


def render_markdown(summary: dict) -> str:
    run, c = summary["run"], summary["counts"]
    route = run.get("route", "")
    lines = [
        f"# {route + ' ' if route else ''}機票監測報告",
        "",
        f"- run：`{run['run_id']}`，模式 {run['mode']}，來源 {run['source']}",
        f"- 時間：{run['started_at']} ～ {run['finished_at']}（{run['duration_sec']}s，{run['timezone']}）",
        f"- 出發月份：{', '.join(str(m) for m in run['months'])}；旅客 {run.get('passengers', 3)} 人；僅直飛；"
        f"抵達時間差門檻 {run.get('arrival_gap_min', 120)} 分鐘；行程數 {c['itineraries']}，有報價 {c['priced']}，"
        f"狀態 {c['status']}",
    ]
    if c["unavailable_reasons"]:
        lines.append("- 失敗原因：" + "；".join(f"{k}（{v}）" for k, v in c["unavailable_reasons"].items()))
    if run.get("aborted_reason"):
        lines.append(f"- **本輪中止**：{run['aborted_reason']}")
    lines += ["", "## 最低票價候選", "", summary["cheapest_note"], ""]
    if summary["cheapest_overall"]:
        lines += _table(summary["cheapest_overall"])
    lines += ["", f"## 請假最少候選（{summary['min_leave_days']} 天）", ""]
    lines += _table(summary["fewest_leave"])
    lines += ["", "## 全部行程", ""]
    lines += _table(summary["itineraries"])
    hs = summary["holiday_source"]
    lines += ["", "## 假日資料", "",
              f"- 來源：{hs['name']}（{hs['url']}），公告 {hs['announced']}，查閱 {hs['retrieved']}",
              f"- 補班日：{'無' if not hs['makeup_workdays'] else ', '.join(hs['makeup_workdays'])}",
              f"- 限制：{hs['caveat']}", ""]
    return "\n".join(lines)
