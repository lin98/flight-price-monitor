"""分組比價：同一去程日 / 同一回程日（5 天 4 夜），各自從允許的機場直飛 PUS。

- 旅客A 只能從 TPE；旅客B 可 TPE 或 KHH；旅客C 只能從 KHH。RMQ 一律排除。
- 旅客可以搭不同班機，但去程抵達 PUS 的時間差必須在門檻內（預設 120 分鐘），
  這樣才能在機場會合一起進市區。
- **只列每人單價，不計算也不輸出合計**：各自買票，合計沒有決策價值，
  還容易被誤讀成「三人團體價」。排序時也只比較單人票價（先比組合裡最貴的那一位，再依序比其餘），
  刻意避開加總。
- 回程：來源有回程資料就照實列（Amadeus 來回查詢、Google 資料若帶回程段）；Google 分享頁目前只伺服端渲染去程，
  解析不到就明確標 `return_time_unavailable`，不用去程時間或常識推估。
- 不設航空公司白名單：來源回傳的每家直飛都保留；只排除轉機與 RMQ。

排序：正常時段（沒有紅眼 / 深夜出發 / 清晨回程）優先 → 抵達時間差小者優先 → 每人票價低者優先 → 日期。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from .fetchers import FetchResult, Offer
from .holidays_tw import HOLIDAY_SOURCE
from .itineraries import WINDOW_END, WINDOW_START, Itinerary, format_date, generate


@dataclass(frozen=True)
class Traveler:
    id: str
    label: str
    origins: tuple[str, ...]


TRAVELERS: tuple[Traveler, ...] = (
    Traveler("a", "旅客A", ("TPE",)),
    Traveler("b", "旅客B", ("TPE", "KHH")),
    Traveler("c", "旅客C", ("KHH",)),
)
EXCLUDED_ORIGINS: frozenset[str] = frozenset({"RMQ"})


def origins_of(travelers: tuple[Traveler, ...]) -> tuple[str, ...]:
    """這批旅客實際需要查的機場（保持宣告順序、去重）。"""
    return tuple(dict.fromkeys(o for t in travelers for o in t.origins))


# 依旅客設定推得需要查詢的機場，並在載入時就確認沒有排除機場混進來
GROUP_ORIGINS: tuple[str, ...] = origins_of(TRAVELERS)
assert not (set(GROUP_ORIGINS) & EXCLUDED_ORIGINS), "excluded origin appears in traveler spec"

# 預設只找旅客A：最常見的用法是一個人查票，多人會合是進階情境。
# 三人分組的邏輯完全保留，用 --all-travelers 打開（見 cli）。
DEFAULT_TRAVELER_IDS: tuple[str, ...] = ("a",)


def select_travelers(all_travelers: bool = False) -> tuple[Traveler, ...]:
    """預設只回旅客A；`all_travelers=True` 回完整三人。"""
    if all_travelers:
        return TRAVELERS
    return tuple(t for t in TRAVELERS if t.id in DEFAULT_TRAVELER_IDS)

DEST = "PUS"
DEFAULT_ARRIVAL_GAP_MIN = 120
TOP_ALTERNATIVES = 3  # 每個日期除了最佳組合外另留幾個備案
# 這幾個標籤代表「時段不理想」；return_time_unavailable 只是資訊不足，不算不理想
_OFF_HOURS_TAGS = {"red_eye", "late_departure", "early_return"}


def _minutes(hhmm: str) -> int | None:
    try:
        h, m = hhmm.split(":")
        h, m = int(h), int(m)
    except (AttributeError, ValueError):
        return None
    return h * 60 + m if 0 <= h < 24 and 0 <= m < 60 else None


def usable_offers(res: FetchResult | None) -> list[Offer]:
    """只留直飛（去程與回程都不轉機）且抵達時間可解析的報價；同一班機出現多次時留最便宜的一筆。"""
    if res is None or res.status != "ok":
        return []
    best: dict[tuple, Offer] = {}
    for o in res.offers:
        if not o.nonstop or _minutes(o.arrive_time) is None:
            continue
        k = (tuple(o.flights), o.depart_time)
        if k not in best or o.price < best[k].price:
            best[k] = o
    return list(best.values())


def _traveler_row(it: Itinerary, t: Traveler, origin: str, o: Offer) -> dict:
    """單一旅客的去程 / 回程資訊。

    兩段的日期一律取自 `Itinerary.depart` / `Itinerary.return_`，不從起降時刻推算：
    跨日航班（例如 22:30→00:05）的抵達日無法從時刻得知，猜一個日期比只標出發日更糟，
    所以每段只掛它自己的出發日，且即使跨日也照樣標出來。
    """
    return {
        "id": t.id, "label": t.label, "origin": origin, "dest": DEST,
        "depart_date": it.depart.isoformat(),
        "airline": o.airline, "airline_name": o.airline_name,
        "flight": "/".join(o.flights), "flights": list(o.flights),
        "depart_time": o.depart_time, "arrive_time": o.arrive_time, "duration_min": o.duration_min,
        "fare": o.price, "currency": o.currency, "labels": o.time_tags,
        "return_date": it.return_.isoformat(), "return_origin": DEST, "return_dest": origin,
        "return_depart_time": o.return_depart_time, "return_arrive_time": o.return_arrive_time,
        "return_airline": o.return_airline, "return_airline_name": o.return_airline_name,
        "return_flight": "/".join(o.return_flights), "return_flights": list(o.return_flights),
        "return_stops": o.return_stops, "return_duration_min": o.return_duration_min,
        "return_time_status": "ok" if o.return_depart_time else "return_time_unavailable",
    }


def _option(it: Itinerary, picks: list[tuple[Traveler, str, Offer]], gap: int) -> dict:
    travelers = [_traveler_row(it, t, origin, o) for t, origin, o in picks]
    tags = {tag for row in travelers for tag in row["labels"]} - {"normal"}
    by_arrival = sorted(travelers, key=lambda row: _minutes(row["arrive_time"]))
    earliest, latest = by_arrival[0]["arrive_time"], by_arrival[-1]["arrive_time"]
    return {
        "depart": it.depart.isoformat(), "return": it.return_.isoformat(), "key": it.key,
        "leave_days": it.leave_days, "holidays_hit": list(it.holidays_hit),
        "normal_hours": not (tags & _OFF_HOURS_TAGS),
        "arrival_gap_min": gap, "earliest_arrival": earliest, "latest_arrival": latest,
        "labels": sorted(tags) or ["normal"],
        "travelers": travelers,
    }


def option_sort_key(opt: dict):
    """正常時段優先 → 抵達差 → 每人票價（由高到低逐位比較，不加總）→ 日期。"""
    fares_desc = sorted((t["fare"] for t in opt["travelers"]), reverse=True)
    return (0 if opt["normal_hours"] else 1, opt["arrival_gap_min"], fares_desc, opt["depart"])


def build_options(it: Itinerary, results: dict[str, FetchResult], gap_limit: int,
                  travelers: tuple[Traveler, ...] = TRAVELERS) -> tuple[list[dict], str]:
    """列出這個日期所有可行的組合（已排序）；沒有可行組合時回傳空清單與原因碼。

    `travelers` 預設是完整三人，呼叫端可只傳旅客A 一位。只有一位時抵達差恆為 0
    （沒有第二個人要會合），門檻自然不會擋掉任何班機。
    """
    pools = {origin: usable_offers(results.get(origin)) for origin in origins_of(travelers)}
    choices = {t.id: [(t, origin, o) for origin in t.origins for o in pools[origin]] for t in travelers}
    missing = [t.id for t in travelers if not choices[t.id]]
    if missing:
        return [], "no_offers_for:" + ",".join(missing)

    options: list[dict] = []

    def walk(idx: int, picked: list[tuple[Traveler, str, Offer]]) -> None:
        if idx == len(travelers):
            arr = [_minutes(o.arrive_time) for _, _, o in picked]
            gap = max(arr) - min(arr)
            if gap <= gap_limit:
                options.append(_option(it, picked, gap))
            return
        for pick in choices[travelers[idx].id]:
            walk(idx + 1, picked + [pick])

    walk(0, [])
    if not options:
        return [], f"arrival_gap_over_{gap_limit}"
    options.sort(key=option_sort_key)
    return options, ""


def _fetch_cell(res: FetchResult | None, provenance: str = "this_run") -> dict:
    """單一（日期, 機場）的抓取狀態。

    `provenance` 分「這輪剛抓的」與「沿用上一輪落地的結果」：分批掃描時報告會混著兩種來源，
    不標出來的話沒人分得清哪些數字是新的。
    """
    if res is None:
        return {"status": "missing", "reason": "not fetched yet", "url": "", "fetched_at": None,
                "offers_count": 0, "provenance": "none", "source": None}
    # source 記的是「這格實際是誰抓到的」：fli 走通是 fli，退回去就是 playwright，
    # 配上 reason 裡的 fli_fallback[...] 才看得出哪些日期走了慢路
    return {"status": res.status, "reason": res.reason, "url": res.url, "fetched_at": res.fetched_at,
            "offers_count": len(res.offers), "provenance": provenance, "source": res.source}


def _coverage(dates: list[dict], origins: tuple[str, ...]) -> dict:
    """涵蓋狀況：期望幾個日期、每個機場實際拿到幾個、資料新舊、有多少是這輪抓的。

    分批掃描（`--max-fetches`）下這是唯一能回答「報告到底涵蓋了多少視窗」的地方。
    """
    by_origin: dict[str, dict] = {}
    for origin in origins:
        cells = [d["fetch"][origin] for d in dates]
        stamps = sorted(c["fetched_at"] for c in cells if c["fetched_at"])
        by_origin[origin] = {
            "status": dict(Counter(c["status"] for c in cells)),
            "provenance": dict(Counter(c["provenance"] for c in cells)),
            "source": dict(Counter(c["source"] for c in cells if c.get("source"))),
            "dates_with_offers": sum(1 for c in cells if c["offers_count"]),
            "oldest_fetched_at": stamps[0] if stamps else None,
            "newest_fetched_at": stamps[-1] if stamps else None,
        }
    departs = sorted(d["depart"] for d in dates)
    return {
        "window": {"start": WINDOW_START.isoformat(), "end": WINDOW_END.isoformat()},
        "window_dates_total": len(generate(depart_through=WINDOW_END)),
        "dates_in_report": len(dates),
        "depart_first": departs[0] if departs else None,
        "depart_last": departs[-1] if departs else None,
        "dates_fetched_this_run": sum(1 for d in dates for c in d["fetch"].values() if c["provenance"] == "this_run"),
        "dates_from_store": sum(1 for d in dates for c in d["fetch"].values() if c["provenance"] == "stored"),
        "dates_never_fetched": sum(1 for d in dates if all(c["provenance"] == "none" for c in d["fetch"].values())),
        "dates_all_origins_ok": sum(1 for d in dates if all(c["status"] == "ok" for c in d["fetch"].values())),
        "by_origin": by_origin,
    }


def build_grouped_summary(run_meta: dict, itins: list[Itinerary],
                          results: dict[str, dict[str, FetchResult]], gap_limit: int,
                          fetched_now: set[tuple[str, str]] | None = None,
                          travelers: tuple[Traveler, ...] = TRAVELERS) -> dict:
    """`fetched_now` 是這一輪真的去抓的 (行程 key, 機場)；沒給就當整份 results 都是這輪抓的。

    `travelers` 預設完整三人；只帶旅客A 時，報告的機場、表頭與涵蓋統計都只會有 TPE。
    """
    origins = origins_of(travelers)
    dates: list[dict] = []
    fetch_status: Counter = Counter()
    reasons: Counter = Counter()
    for it in itins:
        per_origin = results.get(it.key, {})
        options, why = build_options(it, per_origin, gap_limit, travelers)
        entry = it.to_dict()
        entry["fetch"] = {
            origin: _fetch_cell(per_origin.get(origin),
                                "this_run" if fetched_now is None or (it.key, origin) in fetched_now else "stored")
            for origin in origins
        }
        for cell in entry["fetch"].values():
            fetch_status[cell["status"]] += 1
            if cell["status"] in ("unavailable", "error", "missing"):
                reasons[cell["reason"] or "unknown"] += 1
        entry.update(best=options[0] if options else None,
                     alternatives=options[1:1 + TOP_ALTERNATIVES],
                     options_count=len(options), no_option_reason=why)
        dates.append(entry)

    ranked = sorted((d["best"] for d in dates if d["best"]), key=option_sort_key)
    if ranked:
        note = f"{len(ranked)} 個日期有可行組合；以下依 正常時段 → 抵達差 → 每人票價 排序"
    elif run_meta.get("mode") == "dry-run":
        note = "dry-run 未查價"
    else:
        note = "本輪沒有任何日期湊得出可行組合（缺報價或抵達時間差超過門檻）；不對 unavailable 排序"

    return {
        "run": dict(run_meta),
        "rules": {
            "trip": "同去同回 5 天 4 夜，只看直飛",
            "arrival_gap_min": gap_limit,
            "pricing": "只列每人單價，不加總",
            "sort": ["normal_hours_first", "arrival_gap_min", "per_person_fare", "depart"],
            "return_time": "來源解析不到回程時間時標 return_time_unavailable，不推估",
        },
        "travelers": [{"id": t.id, "label": t.label, "allowed_origins": list(t.origins)} for t in travelers],
        "origins": list(origins),
        "excluded_origins": sorted(EXCLUDED_ORIGINS),
        "counts": {
            "dates": len(dates), "dates_with_option": len(ranked),
            "fetch_status": dict(fetch_status), "unavailable_reasons": dict(reasons),
        },
        "coverage": _coverage(dates, origins),
        "ranked_note": note,
        "ranked": ranked,
        "dates": dates,
        "holiday_source": HOLIDAY_SOURCE,
    }


# ---------- Markdown ----------

def _duration(minutes: int) -> str:
    """0 代表來源沒給時長，這時不顯示括號而不是印 0h00m。"""
    if not minutes or minutes <= 0:
        return ""
    h, m = divmod(minutes, 60)
    return f"（{h}h{m:02d}m）" if h else f"（{m}m）"


def _airline_label(name: str, code: str) -> str:
    """航空公司顯示名。去程與回程可能不同家（例：去 KE2086、回 CI187），各自解析、不互相沿用。"""
    return name or code or "航空公司未提供"


def leg_card(kind: str, date: str, origin: str, dest: str, airline_name: str, airline: str,
             flight: str, depart_time: str, arrive_time: str, duration_min: int,
             missing_note: str = "時刻未提供") -> str:
    """一段航程的完整揭示：`去程 3/11(四) IT606 台灣虎航 TPE 16:50 → PUS 20:00（3h10m）`。

    每個欄位都是這一段自己的，不從另一段推：航空公司、航班號、起降機場與時刻都各自列。
    日期取自 `Itinerary`，不從時刻推算（跨日航班的抵達日無從得知）。
    時刻缺漏時仍列出日期、航線、航班與航空公司——那幾項不受來源缺漏影響。
    """
    head = f"{kind} {format_date(date)} {flight or '班機號未提供'} {_airline_label(airline_name, airline)}"
    if depart_time and arrive_time:
        return f"{head} {origin} {depart_time} → {dest} {arrive_time}{_duration(duration_min)}"
    return f"{head} {origin} → {dest} {missing_note}"


def traveler_lines(t: dict) -> list[str]:
    """一位旅客的完整揭示：去程卡、回程卡、票價。Markdown 與 CLI 共用同一份欄位。

    **票價只有一個數字**：來源給的是「這個去回組合」的每人價（fli 取回程那一筆＝組合實際價），
    沒有任何來源提供拆開的單程價。所以兩張卡片共用同一個票價並明講它是去回程整筆——
    分成「去程票價」「回程票價」兩個數字會讓人相加，那是捏造出來的總價。
    """
    return [
        leg_card("去程", t["depart_date"], t["origin"], t["dest"],
                 t["airline_name"], t["airline"], t["flight"],
                 t["depart_time"], t["arrive_time"], t["duration_min"]),
        leg_card("回程", t["return_date"], t["return_origin"], t["return_dest"],
                 t["return_airline_name"], t["return_airline"], t["return_flight"],
                 t["return_depart_time"], t["return_arrive_time"], t["return_duration_min"],
                 missing_note="return_time_unavailable"),
        f"每人來回票價 {t['fare']:,} {t['currency']}（去回程整筆，來源未提供單程價）",
    ]


def _traveler_cell(t: dict) -> str:
    """表格內的旅客欄位。表格格不能換行，用 <br> 讓三張卡片各自成行。"""
    return "<br>".join(traveler_lines(t))


def _coverage_lines(cov: dict | None) -> list[str]:
    """涵蓋範圍：報告蓋到哪幾天、每個機場的抓取狀態與資料新舊。

    分批掃描時報告會同時含「這輪剛抓的」與「上一輪落地的」，這一段就是讓人一眼看出比例與時間。
    """
    if not cov:
        return []
    w = cov["window"]
    out = ["",
           "## 涵蓋範圍",
           "",
           f"- 視窗：{w['start']} ～ {w['end']}（全視窗 {cov['window_dates_total']} 個出發日）；"
           f"本報告 {cov['dates_in_report']} 天（{format_date(cov['depart_first'])} ～ "
           f"{format_date(cov['depart_last'])}）" if cov['depart_first'] else
           f"本報告 {cov['dates_in_report']} 天",
           f"- 資料來源：這輪抓 {cov['dates_fetched_this_run']} 格、沿用上輪 {cov['dates_from_store']} 格、"
           f"完全沒抓過 {cov['dates_never_fetched']} 天；所有機場都 ok 的日期 {cov['dates_all_origins_ok']} 天",
           "",
           "| 機場 | 抓取狀態 | 資料來源 | 實際來源 | 有報價的日期 | 最舊 | 最新 |",
           "|---|---|---|---|---:|---|---|"]
    for origin, v in cov["by_origin"].items():
        status = "、".join(f"{k} {n}" for k, n in v["status"].items()) or "-"
        prov = "、".join(f"{k} {n}" for k, n in v["provenance"].items()) or "-"
        src = "、".join(f"{k} {n}" for k, n in v.get("source", {}).items()) or "-"
        out.append(f"| {origin} | {status} | {prov} | {src} | {v['dates_with_offers']} | "
                   f"{v['oldest_fetched_at'] or '-'} | {v['newest_fetched_at'] or '-'} |")
    return out


def _links(entry_or_fetch: dict) -> str:
    return " ".join(f"[{origin}]({cell['url']})" for origin, cell in entry_or_fetch.items() if cell.get("url")) or "-"


def _ranked_table(rows: list[dict], fetch_by_key: dict[str, dict], travelers: list[dict]) -> list[str]:
    """表頭從 summary 的 travelers 來，不讀模組全域——只查旅客A 時就只該有一欄。"""
    heads = " | ".join(t["label"] for t in travelers)
    out = [f"| # | 出發 | 回程 | 請假 | 時段 | 抵達差(分) | {heads} | 標籤 | 查詢 |",
           "|---:|---|---|---:|---|---:|" + "---|" * len(travelers) + "---|---|"]
    for i, r in enumerate(rows, 1):
        cells = " | ".join(_traveler_cell(t) for t in r["travelers"])
        out.append(f"| {i} | {format_date(r['depart'])} | {format_date(r['return'])} | {r['leave_days']} | "
                   f"{'正常' if r['normal_hours'] else '非正常'} | {r['arrival_gap_min']} | {cells} | "
                   f"{', '.join(r['labels'])} | {_links(fetch_by_key.get(r['key'], {}))} |")
    return out


_COUNT_ZH = {2: "兩", 3: "三", 4: "四"}


def _title(travelers: list[dict], origins: list[str]) -> str:
    """只有一位旅客時「分組比價」是誤導，標題直接寫他的名字。"""
    route = f"（{'/'.join(origins)} → {DEST}，僅直飛）"
    if len(travelers) == 1:
        return f"# {travelers[0]['label']}機票比價{route}"
    return f"# {_COUNT_ZH.get(len(travelers), len(travelers))}人分組比價{route}"


def render_grouped_markdown(summary: dict) -> str:
    run, c, rules = summary["run"], summary["counts"], summary["rules"]
    who = "；".join(f"{t['label']}→{'/'.join(t['allowed_origins'])}" for t in summary["travelers"])
    lines = [
        _title(summary["travelers"], summary["origins"]),
        "",
        f"- run：`{run['run_id']}`，模式 {run['mode']}，來源 {run['source']}",
        f"- 時間：{run['started_at']} ～ {run['finished_at']}（{run['duration_sec']}s，{run['timezone']}）",
        f"- 規則：{who}；排除 {', '.join(summary['excluded_origins'])}；{rules['trip']}；"
        f"去程抵達時間差 ≤ {rules['arrival_gap_min']} 分鐘；{rules['pricing']}",
        f"- 出發月份：{', '.join(str(m) for m in run['months'])}；日期數 {c['dates']}，有可行組合 {c['dates_with_option']}；"
        f"抓取狀態 {c['fetch_status']}",
        f"- 回程時間：{rules['return_time']}",
    ]
    if c["unavailable_reasons"]:
        lines.append("- 失敗原因：" + "；".join(f"{k}（{v}）" for k, v in c["unavailable_reasons"].items()))
    if run.get("aborted_reason"):
        lines.append(f"- **本輪中止**：{run['aborted_reason']}")
    lines += _coverage_lines(summary.get("coverage"))
    fetch_by_key = {d["key"]: d["fetch"] for d in summary["dates"]}
    lines += ["", "## 排名（正常時段 → 抵達差 → 每人票價）", "", summary["ranked_note"], ""]
    if summary["ranked"]:
        lines += _ranked_table(summary["ranked"], fetch_by_key, summary["travelers"])

    no_option = [d for d in summary["dates"] if not d["best"]]
    if no_option:
        lines += ["", f"## 無可行組合的日期（{len(no_option)}）", "",
                  "| 出發 | 回程 | 請假 | 原因 | " + " | ".join(f"{o} 狀態" for o in summary["origins"]) + " | 查詢 |",
                  "|---|---|---:|---|" + "---|" * len(summary["origins"]) + "---|"]
        for d in no_option:
            status = " | ".join(f"{d['fetch'][o]['status']}" + (f"（{d['fetch'][o]['reason']}）" if d['fetch'][o]['reason'] else "")
                                for o in summary["origins"])
            lines.append(f"| {format_date(d['depart'])} | {format_date(d['return'])} | {d['leave_days']} | "
                         f"{d['no_option_reason']} | {status} | "
                         f"{_links(d['fetch'])} |")

    hs = summary["holiday_source"]
    lines += ["", "## 假日資料", "",
              f"- 來源：{hs['name']}（{hs['url']}），公告 {hs['announced']}，查閱 {hs['retrieved']}",
              f"- 補班日：{'無' if not hs['makeup_workdays'] else ', '.join(hs['makeup_workdays'])}",
              f"- 限制：{hs['caveat']}",
              "- 6/1～6/4 回程日的請假天數只依平日計算（該區間無國定假日）", ""]
    return "\n".join(lines)
