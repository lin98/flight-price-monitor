"""行政院人事行政總處「政府行政機關辦公日曆表」：下載官方 CSV、找連假、排請假走法。

資料流：政府資料開放平臺的資料集 14718（人事行政總處發布）列出各年度檔案位址，
檔案本身放在 www.dgpa.gov.tw。位址裡的 GUID 每年都換，所以不能寫死，要先問索引。

與專案其他來源同一條原則：**拿不到就說拿不到**。下載失敗時沿用舊快取並標 `stale`；
連快取都沒有就回報缺年度，不用「週一到週五上班」去猜——猜出來的日曆會漏掉補假，
排出來的請假天數就是錯的。

`holidays_tw.py` 手寫的 2027 年 3–5 月資料是舊監測流程用的，這裡不取代它。
"""
from __future__ import annotations

import csv
import io
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

DATASET_URL = 'https://data.gov.tw/api/v2/rest/dataset/14718'
DATASET_PAGE = 'https://data.gov.tw/dataset/14718'
FILE_HOST = 'www.dgpa.gov.tw'
HEADER = ['西元日期', '星期', '是否放假', '備註']
MAX_AGE = timedelta(days=7)
CAVEAT = '適用政府行政機關；民間企業多數比照，但實際放假與請假天數以自己公司的行事曆為準。'


def http_get(url):
    # 用 requests 而不是 urllib：python.org 版 Python 的 urllib 沒有根憑證，連 dgpa 會驗證失敗；
    # requests 走 certifi。憑證驗證維持開啟，不為了抓得到而關掉。
    import requests
    response = requests.get(url, headers={'User-Agent': 'fare-watch (holiday calendar)'}, timeout=20)
    response.raise_for_status()
    return response.content


def resource_url(index, year):
    """從資料集索引挑出該年度的日曆 CSV；同年還有一份「Google行事曆專用」格式不同，要避開。"""
    wanted = f'{year - 1911}年中華民國政府行政機關辦公日曆表'
    for item in index.get('result', {}).get('distribution', []):
        if item.get('resourceDescription') == wanted and item.get('resourceFormat') == 'CSV':
            url = item.get('resourceDownloadUrl', '')
            parsed = urlparse(url)
            # 索引是外部資料；只接受人事行政總處自己的主機，不跟著索引去抓任意網址
            if parsed.scheme != 'https' or parsed.hostname != FILE_HOST:
                raise ValueError(f'日曆檔案位址不在 {FILE_HOST}')
            return url
    return None


def parse_csv(raw, year):
    rows = list(csv.reader(io.StringIO(raw.decode('utf-8-sig'))))
    if not rows or [c.strip() for c in rows[0][:4]] != HEADER:
        raise ValueError('日曆檔案欄位與預期不符')
    days = []
    for row in rows[1:]:
        if not any(c.strip() for c in row):
            continue
        day = datetime.strptime(row[0].strip(), '%Y%m%d').date()
        flag = row[2].strip()
        # 官方定義只有 0（上班）與 2（放假）；出現別的值代表格式變了，寧可失敗也不要誤判
        if day.year != year or flag not in ('0', '2'):
            raise ValueError(f'日曆檔案內容與預期不符：{row}')
        days.append({'date': day.isoformat(), 'off': flag == '2', 'note': row[3].strip() if len(row) > 3 else ''})
    if len(days) < 365:
        raise ValueError('日曆檔案天數不足一年')
    return days


class Calendar:
    def __init__(self, directory, fetch=http_get):
        self.directory = Path(directory)
        self.fetch = fetch

    def _cached(self, year):
        try:
            return json.loads((self.directory / f'{year}.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return None

    def year(self, year, now=None):
        """回傳 (年度資料或 None, 錯誤訊息)。資料帶 `stale` 表示這次沒更新成功、沿用舊檔。"""
        now = now or datetime.now(timezone.utc)
        cached = self._cached(year)
        if cached and now - datetime.fromisoformat(cached['fetched_at']) < MAX_AGE:
            return dict(cached, stale=False), ''
        try:
            url = resource_url(json.loads(self.fetch(DATASET_URL)), year)
            if url is None:
                # 隔年日曆通常年中才公告，沒有不算錯誤
                return (dict(cached, stale=True) if cached else None), f'{year} 年辦公日曆表尚未公告'
            fresh = {'year': year, 'url': url, 'fetched_at': now.isoformat(), 'days': parse_csv(self.fetch(url), year)}
        except Exception as exc:
            reason = f'{year} 年辦公日曆表下載失敗（{type(exc).__name__}）'
            return (dict(cached, stale=True) if cached else None), reason
        self.directory.mkdir(parents=True, exist_ok=True)
        temp = self.directory / f'{year}.tmp'
        temp.write_text(json.dumps(fresh, ensure_ascii=False), encoding='utf-8')
        temp.replace(self.directory / f'{year}.json')
        return dict(fresh, stale=False), ''

    def load(self, today=None):
        """今年加明年：年底的連假（元旦、春節）落在隔年的檔案裡。"""
        today = today or date.today()
        days, sources, errors = {}, [], []
        for year in (today.year, today.year + 1):
            data, error = self.year(year)
            if error:
                errors.append(error)
            if data:
                sources.append({k: data[k] for k in ('year', 'url', 'fetched_at', 'stale')})
                days.update({date.fromisoformat(d['date']): d for d in data['days']})
        return {'days': days, 'sources': sources, 'errors': errors}


def trip_options(days, start, end, today):
    """一個連假的四種走法：照放、前一天請假、後一天請假、前後各請一天。

    連假是「最長的連續放假區間」，所以它的前一天與後一天必然是上班日；
    去程只有 2 個候選日、回程也只有 2 個，4 次單程查價就能排出全部 4 種組合。
    """
    options = []
    for depart in (start - timedelta(days=1), start):
        for ret in (end, end + timedelta(days=1)):
            span = [depart + timedelta(days=i) for i in range((ret - depart).days + 1)]
            if depart <= today or any(d not in days for d in span):
                continue
            options.append({'depart': depart.isoformat(), 'return': ret.isoformat(),
                            'total_days': len(span), 'leave_days': sum(not days[d]['off'] for d in span)})
    return sorted(options, key=lambda o: (o['leave_days'], o['depart']))


def upcoming_breaks(days, today, min_days=3, limit=6):
    """今天之後才開始的連假（連續放假 ≥ min_days 天，含週末）。已經開始的連假來不及訂票，不列。"""
    breaks, run = [], []
    for day in sorted(days) + [None]:
        if day is not None and days[day]['off'] and (not run or day - run[-1] == timedelta(days=1)):
            run.append(day)
            continue
        if len(run) >= min_days and run[0] > today:
            notes = list(dict.fromkeys(days[d]['note'] for d in run if days[d]['note'] and days[d]['note'] != '補假'))
            options = trip_options(days, run[0], run[-1], today)
            if options:
                breaks.append({'name': '、'.join(notes) or '連假', 'start': run[0].isoformat(),
                               'end': run[-1].isoformat(), 'days': len(run), 'options': options})
        run = [day] if day is not None and days[day]['off'] else []
    return breaks[:limit]
