"""CLI 入口：python -m fare_watch [--once|--loop] [--grouped] [--dry-run] [--month 3,4] [--json] ..."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path

from . import __version__
from .fetchers import (
    DEFAULT_UA, AmadeusFetcher, BlockedError, DryRunFetcher, FetchResult, Fetcher, GoogleFlightsFetcher,
    jitter_sleep,
)
from .fli_fetcher import FliFetcher, FliFirstFetcher
from .grouped import (
    build_grouped_summary, origins_of, render_grouped_markdown, select_travelers, traveler_lines,
)
from .itineraries import WINDOW_END, WINDOW_START, Itinerary, format_date, generate
from .playwright_fetcher import PlaywrightFetcher
from .report import build_summary, render_markdown
from .storage import Storage
from .tz import now_taipei

log = logging.getLogger("fare_watch")


def load_dotenv(path: Path) -> None:
    """極簡 .env 讀取：KEY=VALUE、忽略註解，不覆寫已存在的環境變數。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def parse_months(value: str | None) -> list[int]:
    if not value:
        return []
    months = sorted({int(x) for x in value.replace(" ", "").split(",") if x})
    bad = [m for m in months if m not in (3, 4, 5)]
    if bad:
        raise argparse.ArgumentTypeError(f"--month only accepts 3, 4, 5 (got {bad})")
    return months


def parse_dates(value: str | None) -> list[str]:
    """`--dates` 只收視窗內的日期；打錯字直接報錯，不要靜靜過濾掉變成空跑。"""
    if not value:
        return []
    out = []
    for raw in value.replace(" ", "").split(","):
        if not raw:
            continue
        # 只收帶連字號的 YYYY-MM-DD：Python 3.11 的 fromisoformat 連 "20270401" 都吃，
        # 但那在不同版本行為不一樣，統一要求一種寫法比較不會踩到
        try:
            if len(raw) != 10 or raw[4] != "-" or raw[7] != "-":
                raise ValueError(raw)
            d = dt.date.fromisoformat(raw)
        except ValueError:
            raise argparse.ArgumentTypeError(f"--dates wants YYYY-MM-DD (got {raw!r})")
        if not WINDOW_START <= d <= WINDOW_END:
            raise argparse.ArgumentTypeError(f"--dates outside {WINDOW_START}..{WINDOW_END}: {raw}")
        out.append(d.isoformat())
    return sorted(set(out))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fare_watch", description="TPE/KHH→PUS 2027/03–05 5天4夜直飛機票監測")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="執行一輪後結束（預設）")
    mode.add_argument("--loop", action="store_true", help="常駐執行，每 --interval-minutes 跑一輪")
    p.add_argument("--dry-run", action="store_true", help="不連網路，只產生行程、請假計算與查詢 URL")
    p.add_argument("--grouped", action="store_true",
                   help="分組比價模式：同去同回、只看直飛、出發日涵蓋到 5/31；忽略 --origin；"
                        "輸出 data/grouped_latest.{json,md}。預設只找旅客A（TPE→PUS），"
                        "要三人一起查加 --all-travelers")
    p.add_argument("--all-travelers", action="store_true",
                   help="查完整三人：旅客A→TPE、旅客B→TPE/KHH、旅客C→KHH，並套用抵達時間差門檻。"
                        "不加這個旗標時只查旅客A TPE→PUS")
    p.add_argument("--month", type=parse_months, default=[], help="只看指定出發月份，例如 3 或 3,4")
    p.add_argument("--json", action="store_true", help="以 JSON 輸出摘要到 stdout")
    p.add_argument("--source", choices=["fli", "google", "google_playwright", "amadeus"],
                   default=os.environ.get("FARE_WATCH_SOURCE", "fli"),
                   help="價格來源：fli 先問可選的 fli 套件、失敗自動退回 google_playwright（預設）；"
                        "google_playwright 只用瀏覽器渲染；google 純 HTTP；amadeus API")
    p.add_argument("--origin", choices=["TPE", "KHH"], default="TPE",
                   help="出發機場：TPE 桃園、KHH 高雄")
    p.add_argument("--passengers", type=int, default=3, help="旅客人數（預設 3；報告只顯示每人價格，不列合計）")
    p.add_argument("--arrival-gap-min", type=int, default=120,
                   help="同日不同班機的抵達時間最大差距（分鐘，預設 120；供分組比對）")
    p.add_argument("--limit", type=int, default=0, help="只抓前 N 個行程（測試用）")
    p.add_argument("--dates", type=parse_dates, default=[],
                   help="只看指定出發日（逗號分隔 YYYY-MM-DD），例如 2027-03-15,2027-04-15；用來複查特定日期")
    p.add_argument("--max-fetches", type=int, default=0,
                   help="這一輪最多抓幾格（一格＝一個出發日 × 一個機場）；"
                        "優先抓沒抓過的、其次最舊的。搭配 --merge-stored 就能分批掃完整個視窗")
    p.add_argument("--merge-stored", action="store_true",
                   help="把 data/raw/ 裡上一輪落地的結果併進報告，這輪沒抓到的日期沿用舊資料（會標示 provenance 與時間）")
    p.add_argument("--data-dir", default=os.environ.get("FARE_WATCH_DATA_DIR", "data"))
    p.add_argument("--delay-min", type=float, default=float(os.environ.get("FARE_WATCH_DELAY_MIN", "3")))
    p.add_argument("--delay-max", type=float, default=float(os.environ.get("FARE_WATCH_DELAY_MAX", "7")))
    p.add_argument("--interval-minutes", type=int, default=int(os.environ.get("FARE_WATCH_INTERVAL_MINUTES", "720")))
    p.add_argument("--keep-html", action="store_true", help="成功時也保留完整 HTML（每頁約 2MB）")
    p.add_argument("--currency", default=os.environ.get("FARE_WATCH_CURRENCY", "TWD"))
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--version", action="version", version=__version__)
    return p


def make_fetcher(args, origin: str | None = None) -> Fetcher:
    origin = origin or args.origin
    if args.dry_run:
        return DryRunFetcher(args.currency, origin=origin)
    if args.source == "amadeus":
        fetcher = AmadeusFetcher()
        if not fetcher.client.configured:
            raise SystemExit("--source amadeus needs AMADEUS_CLIENT_ID / AMADEUS_CLIENT_SECRET in .env")
        return fetcher
    if args.source in ("google_playwright", "fli"):
        # keep_html 這裡要往下傳：PlaywrightFetcher 成功時預設丟掉 HTML，不傳就存不到整頁
        playwright = PlaywrightFetcher(currency=args.currency,
                                       user_agent=os.environ.get("FARE_WATCH_USER_AGENT") or DEFAULT_UA,
                                       origin=origin, dest="PUS", keep_html=args.keep_html)
        if args.source == "google_playwright":
            return playwright
        # fli 是可選捷徑：沒裝或查不到就退回上面那個瀏覽器來源，流程不因為少一個套件而壞掉
        return FliFirstFetcher(FliFetcher(currency=args.currency, origin=origin, dest="PUS"), playwright)
    return GoogleFlightsFetcher(currency=args.currency, user_agent=os.environ.get("FARE_WATCH_USER_AGENT") or DEFAULT_UA,
                                origin=origin, dest="PUS")


def make_grouped_fetchers(args) -> dict[str, Fetcher]:
    """依選到的旅客決定要查哪些機場（預設只有旅客A→TPE），與 --origin 無關。"""
    return {origin: make_fetcher(args, origin)
            for origin in origins_of(select_travelers(args.all_travelers))}


def run_once(args, storage: Storage, fetcher: Fetcher) -> dict:
    run_id = now_taipei().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    started = now_taipei()
    itins: list[Itinerary] = generate(args.month)
    if args.limit:
        itins = itins[: args.limit]
    log.info("run %s: %d itineraries, source=%s dry_run=%s", run_id, len(itins), fetcher.name, args.dry_run)

    results: dict[str, FetchResult] = {}
    known = storage.existing_keys()
    aborted_reason = ""
    new_obs = 0
    for i, it in enumerate(itins):
        if it.key in results:  # generate() 已保證唯一，這裡是防呆
            continue
        try:
            res = fetcher.fetch(it)
        except BlockedError as e:
            aborted_reason = f"source blocked us ({e}); stopped without retrying to stay compliant"
            log.error("%s", aborted_reason)
            for rest in itins[i:]:
                results[rest.key] = FetchResult(fetcher.name, rest.key, "unavailable", "", now_taipei().isoformat(timespec="seconds"),
                                                reason="aborted_after_block")
            break
        results[it.key] = res
        log.info("%s %s %s %s", it.key, res.status, res.min_price if res.min_price is not None else "-", res.reason)
        if not args.dry_run:
            storage.save_raw(it, res, keep_html=args.keep_html)
            if storage.append_observation(it, res, run_id, known):
                new_obs += 1
            if i < len(itins) - 1:
                jitter_sleep(args.delay_min, args.delay_max)

    finished = now_taipei()
    run_meta = {
        "run_id": run_id, "version": __version__, "origin": getattr(args, "origin", "TPE"),
        "route": f"{getattr(args, 'origin', 'TPE')}→PUS", "passengers": getattr(args, "passengers", 3),
        "arrival_gap_min": getattr(args, "arrival_gap_min", 120),
        "mode": "dry-run" if args.dry_run else ("loop" if args.loop else "once"),
        "source": fetcher.name, "months": args.month or [3, 4, 5], "limit": args.limit,
        "started_at": started.isoformat(timespec="seconds"), "finished_at": finished.isoformat(timespec="seconds"),
        "duration_sec": round((finished - started).total_seconds(), 1),
        "new_observations": new_obs, "aborted_reason": aborted_reason, "timezone": "Asia/Taipei",
    }
    summary = build_summary(run_meta, itins, results)
    md = render_markdown(summary)
    dated, latest_json = storage.write_report(md, summary)
    summary["run"]["report_md"] = str(dated)
    summary["run"]["report_json"] = str(latest_json)
    log.info("report written: %s", dated)
    return summary


def run_grouped_once(args, storage: Storage, fetchers: dict[str, Fetcher]) -> dict:
    run_id = now_taipei().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    started = now_taipei()
    # 分組比價要涵蓋 5/31 出發（回程 6/4），所以放寬預設視窗
    travelers = select_travelers(args.all_travelers)
    itins: list[Itinerary] = generate(args.month, depart_through=WINDOW_END)
    if args.dates:
        itins = [it for it in itins if it.depart.isoformat() in args.dates]
    if args.limit:
        itins = itins[: args.limit]
    source = next(iter(fetchers.values())).name

    # 上一輪落地的結果：--merge-stored 拿來補報告，--max-fetches 拿來決定先抓誰（沒抓過的最優先）
    stored: dict[str, dict[str, FetchResult]] = {}
    if not args.dry_run and (args.merge_stored or args.max_fetches):
        stored = storage.latest_raw_by_origin([it.key for it in itins], tuple(fetchers))

    results: dict[str, dict[str, FetchResult]] = {
        it.key: (dict(stored.get(it.key, {})) if args.merge_stored else {}) for it in itins
    }
    jobs = [(it, origin) for it in itins for origin in fetchers]
    if args.max_fetches:
        # 沒抓過的 fetched_at 當空字串，排序時自然排最前面；同樣新舊時照日期順序走
        jobs.sort(key=lambda j: (stored.get(j[0].key, {}).get(j[1]).fetched_at
                                 if stored.get(j[0].key, {}).get(j[1]) else "", j[0].depart, j[1]))
        jobs = jobs[: args.max_fetches]
        jobs.sort(key=lambda j: (j[0].depart, j[1]))  # 真正去抓時仍照日期順序，log 比較好讀
    fetched_now: set[tuple[str, str]] = set()

    log.info("grouped run %s: %d dates x %d origins, %d fetch jobs, source=%s dry_run=%s merge_stored=%s",
             run_id, len(itins), len(fetchers), len(jobs), source, args.dry_run, args.merge_stored)

    known = storage.existing_keys()
    aborted_reason = ""
    new_obs = 0
    fetch_ok = 0
    for i, (it, origin) in enumerate(jobs):
        try:
            res = fetchers[origin].fetch(it)
        except BlockedError as e:
            aborted_reason = f"source blocked us ({e}); stopped without retrying to stay compliant"
            log.error("%s", aborted_reason)
            for rest_it, rest_origin in jobs[i:]:
                # 被擋之後標 unavailable，但如果有沿用的舊資料就別覆蓋掉——舊價格仍然是真的抓到過的
                if rest_origin not in results[rest_it.key]:
                    results[rest_it.key][rest_origin] = FetchResult(
                        source, rest_it.key, "unavailable", "", now_taipei().isoformat(timespec="seconds"),
                        reason="aborted_after_block")
                    fetched_now.add((rest_it.key, rest_origin))
            break
        results[it.key][origin] = res
        fetched_now.add((it.key, origin))
        fetch_ok += res.status == "ok"
        log.info("%s %s %s %s %s", origin, it.key, res.status,
                 res.min_price if res.min_price is not None else "-", res.reason)
        if not args.dry_run:
            storage.save_raw(it, res, keep_html=args.keep_html, origin=origin)
            if storage.append_observation(it, res, run_id, known, origin=origin):
                new_obs += 1
            if i < len(jobs) - 1:
                jitter_sleep(args.delay_min, args.delay_max)

    finished = now_taipei()
    run_meta = {
        "run_id": run_id, "version": __version__, "grouped": True, "origins": list(fetchers),
        "route": f"{'/'.join(fetchers)}→PUS", "arrival_gap_min": args.arrival_gap_min,
        "mode": "dry-run" if args.dry_run else ("loop" if args.loop else "once"),
        "source": source, "months": args.month or [3, 4, 5], "limit": args.limit,
        "dates_filter": list(args.dates), "max_fetches": args.max_fetches, "merge_stored": bool(args.merge_stored),
        "travelers": [t.id for t in travelers], "all_travelers": bool(args.all_travelers),
        "fetch_jobs": len(jobs), "fetch_ok": fetch_ok,
        "started_at": started.isoformat(timespec="seconds"), "finished_at": finished.isoformat(timespec="seconds"),
        "duration_sec": round((finished - started).total_seconds(), 1),
        "new_observations": new_obs, "aborted_reason": aborted_reason, "timezone": "Asia/Taipei",
    }
    summary = build_grouped_summary(run_meta, itins, results, args.arrival_gap_min, fetched_now, travelers)
    md = render_grouped_markdown(summary)
    dated, latest_json = storage.write_grouped(md, summary)
    summary["run"]["report_md"] = str(dated)
    summary["run"]["report_json"] = str(latest_json)
    log.info("grouped report written: %s", dated)
    return summary


def print_grouped_summary(summary: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return
    c, cov = summary["counts"], summary.get("coverage") or {}
    print(f"[fare_watch] grouped run {summary['run']['run_id']} mode={summary['run']['mode']} "
          f"dates={c['dates']} with_option={c['dates_with_option']} fetch={c['fetch_status']}")
    if cov:
        print(f"  coverage: {cov['dates_in_report']}/{cov['window_dates_total']} dates "
              f"({format_date(cov['depart_first'])}~{format_date(cov['depart_last'])}) "
              f"this_run={cov['dates_fetched_this_run']} "
              f"stored={cov['dates_from_store']} never={cov['dates_never_fetched']} "
              f"both_ok={cov['dates_all_origins_ok']}")
        for origin, v in cov["by_origin"].items():
            print(f"    {origin}: {v['status']} newest={v['newest_fetched_at'] or '-'}")
    if c["unavailable_reasons"]:
        print(f"  unavailable: {c['unavailable_reasons']}")
    if summary["run"].get("aborted_reason"):
        print(f"  ABORTED: {summary['run']['aborted_reason']}")
    for r in summary["ranked"][:3]:
        print(f"  {format_date(r['depart'])}~{format_date(r['return'])} leave={r['leave_days']} "
              f"gap={r['arrival_gap_min']}m [{', '.join(r['labels'])}]")
        # 每位旅客的去程／回程各自完整列出，與 Markdown 共用 traveler_lines()，欄位不會兩邊走鐘
        for t in r["travelers"]:
            head, *rest = traveler_lines(t)
            print(f"    {t['label']}（{t['origin']}） {head}")
            for line in rest:
                print(f"      {' ' * len(t['label'])}   {line}")
    print(f"  report: {summary['run'].get('report_md', '-')}")


def print_summary(summary: dict, as_json: bool) -> None:
    if as_json:
        slim = {k: v for k, v in summary.items() if k != "itineraries"}
        print(json.dumps(slim, ensure_ascii=False, indent=2))
        return
    c = summary["counts"]
    print(f"[fare_watch] run {summary['run']['run_id']} mode={summary['run']['mode']} "
          f"itineraries={c['itineraries']} priced={c['priced']} status={c['status']}")
    if c["unavailable_reasons"]:
        print(f"  unavailable: {c['unavailable_reasons']}")
    if summary["run"].get("aborted_reason"):
        print(f"  ABORTED: {summary['run']['aborted_reason']}")
    print(f"  fewest leave ({summary['min_leave_days']} day): "
          + ", ".join(f"{format_date(r['depart'])}~{format_date(r['return'])}" for r in summary["fewest_leave"]))
    for r in summary["cheapest_overall"][:3]:
        o = r["cheapest_offer"] or {}
        print(f"  cheapest: {format_date(r['depart'])}~{format_date(r['return'])} leave={r['leave_days']} "
              f"{r['currency']} {r['min_price']:,} {o.get('airline_name', '')} {'/'.join(o.get('flights', []))}")
    print(f"  report: {summary['run'].get('report_md', '-')}")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "search":
        from .search import main as search_main
        return search_main(argv[1:])
    load_dotenv(Path(".env"))
    args = build_parser().parse_args(argv)
    if not args.loop:
        args.once = True
    storage = Storage(Path(args.data_dir))
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stderr), logging.FileHandler(storage.logs / "fare_watch.log", encoding="utf-8")],
    )
    if args.grouped:
        fetchers = make_grouped_fetchers(args)
    else:
        fetcher = make_fetcher(args)
    while True:
        if args.grouped:
            summary = run_grouped_once(args, storage, fetchers)
            print_grouped_summary(summary, args.json)
            # 分批掃描時「這輪沒湊出組合」是正常的（可能才抓到半個視窗），不該讓排程一直亮紅燈；
            # 真正要排程注意的是「被擋」與「這輪抓的每一格都失敗」
            run = summary["run"]
            ok = not run["aborted_reason"] and (not run["fetch_jobs"] or run["fetch_ok"] > 0)
        else:
            summary = run_once(args, storage, fetcher)
            print_summary(summary, args.json)
            ok = bool(summary["counts"]["priced"])
        if not args.loop:
            return 0 if ok or args.dry_run else 2
        log.info("sleeping %d minutes", args.interval_minutes)
        time.sleep(args.interval_minutes * 60)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
