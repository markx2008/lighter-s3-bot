#!/usr/bin/env python3
"""Generate a TWSE daily market brief (HTML + PDF) and print a short text summary.

Designed for an 08:00 Asia/Taipei cron: it automatically finds the most recent
trading date (walks back up to 7 days).

Data sources (public):
- TWSE afterTrading/MI_INDEX (indices, market stats, daily close)
- TWSE fund/BFI82U (3 major institutions net buy/sell amount)
- TWSE fund/TWT38U (foreign net buy summary)
- TWSE fund/T86 (3 major institutions net buy/sell by security)
- TWSE openapi t187ap03_L (listed companies basic data for industry codes)

Notes:
- This is informational only (not investment advice).
- We intentionally label any "picks" as a watchlist.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

TZ = dt.timezone(dt.timedelta(hours=8))  # Asia/Taipei fixed offset


def curl_json(url: str, timeout: int = 60) -> Any:
    """Fetch JSON via curl (works around occasional Python SSL verification issues)."""
    cmd = ["curl", "-ksSL", "--compressed", "-H", "User-Agent: Mozilla/5.0", url]
    out = subprocess.check_output(cmd, timeout=timeout)
    return json.loads(out.decode("utf-8"))


def yyyymmdd(d: dt.date) -> str:
    return d.strftime("%Y%m%d")


def find_table(obj: dict, title_contains: list[str]) -> dict | None:
    for t in obj.get("tables", []) or []:
        title = t.get("title", "") or ""
        if all(s in title for s in title_contains):
            return t
    return None


def fnum(x: Any) -> float | None:
    if x is None:
        return None
    s = str(x).strip()
    if s in {"--", "—", "", "None"}:
        return None
    s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def sign_from_html(s: Any) -> int:
    s = str(s)
    if "red" in s or "+" in s:
        return 1
    if "green" in s or "-" in s:
        return -1
    return 0


@dataclass
class IndexRow:
    name: str
    close: float
    chg: float
    pct: float


def parse_indices(mi_obj: dict) -> dict[str, IndexRow]:
    t = find_table(mi_obj, ["價格指數(臺灣證券交易所)"])
    out: dict[str, IndexRow] = {}
    if not t:
        return out
    for r in t.get("data", []) or []:
        name = r[0]
        close = fnum(r[1])
        chg = fnum(r[3])
        pct = fnum(r[4])
        sgn = sign_from_html(r[2])
        if close is None or chg is None or pct is None:
            continue
        out[name] = IndexRow(name, close, sgn * chg, sgn * pct)
    return out


@dataclass
class StockRow:
    code: str
    name: str
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    diff: float | None
    sign: int

    @property
    def prev_close(self) -> float | None:
        if self.close is None or self.diff is None or self.sign == 0:
            return None
        return self.close - self.sign * self.diff

    @property
    def pct(self) -> float | None:
        p = self.prev_close
        if p is None or p <= 0 or self.close is None:
            return None
        return (self.close - p) / p * 100.0


def parse_daily_close_all(mi_obj: dict) -> list[StockRow]:
    t = None
    for table in mi_obj.get("tables", []) or []:
        title = table.get("title", "") or ""
        if "每日收盤行情" in title and "全部" in title:
            t = table
            break
    if not t:
        return []

    fields = t.get("fields", [])
    col = {k: i for i, k in enumerate(fields)}
    need = ["證券代號", "證券名稱", "開盤價", "最高價", "最低價", "收盤價", "漲跌(+/-)", "漲跌價差"]
    for k in need:
        if k not in col:
            return []

    out: list[StockRow] = []
    for r in t.get("data", []) or []:
        code = str(r[col["證券代號"]]).strip()
        name = str(r[col["證券名稱"]]).strip()
        sgn = sign_from_html(r[col["漲跌(+/-)"]])
        out.append(
            StockRow(
                code=code,
                name=name,
                open=fnum(r[col["開盤價"]]),
                high=fnum(r[col["最高價"]]),
                low=fnum(r[col["最低價"]]),
                close=fnum(r[col["收盤價"]]),
                diff=fnum(r[col["漲跌價差"]]),
                sign=sgn,
            )
        )
    return out


def is_common_stock(code: str) -> bool:
    return len(code) == 4 and code.isdigit()


def detect_limit_up(rows: Iterable[StockRow]) -> list[StockRow]:
    res = []
    for r in rows:
        if not is_common_stock(r.code):
            continue
        if r.close is None or r.high is None:
            continue
        pct = r.pct
        if pct is None:
            continue
        # heuristic
        if abs(r.close - r.high) < 1e-9 and pct >= 9.8:
            res.append(r)
    return res


INDUSTRY_LABELS = {
    "01": "水泥工業",
    "02": "食品工業",
    "03": "塑膠工業",
    "04": "紡織纖維",
    "05": "電機機械",
    "06": "電器電纜",
    "08": "玻璃陶瓷",
    "09": "造紙工業",
    "10": "鋼鐵工業",
    "11": "橡膠工業",
    "12": "汽車工業",
    "13": "電子工業",
    "14": "建材營造",
    "15": "航運業",
    "16": "觀光餐旅",
    "17": "金融保險",
    "18": "貿易百貨",
    "19": "綜合",
    "20": "其他",
    "21": "化學工業",
    "22": "生技醫療業",
    "23": "油電燃氣業",
    "24": "半導體業",
    "25": "電腦及週邊設備業",
    "26": "光電業",
    "27": "通信網路業",
    "28": "電子零組件業",
    "29": "電子通路業",
    "30": "資訊服務業",
    "31": "其他電子業",
}


def load_industry_map() -> dict[str, str]:
    obj = curl_json("https://openapi.twse.com.tw/v1/opendata/t187ap03_L")
    out = {}
    for it in obj:
        code = str(it.get("公司代號", "")).zfill(4)
        ind = (it.get("產業別") or "").strip()
        if code and ind:
            out[code] = ind
    return out


def most_recent_trading_date(today: dt.date) -> dt.date:
    # Probe MI_INDEX type=MS because it's small.
    for i in range(0, 8):
        d = today - dt.timedelta(days=i)
        url = f"https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={yyyymmdd(d)}&type=MS&response=json"
        obj = curl_json(url)
        if obj.get("stat") != "OK":
            continue
        # Must have market stats table with data
        ok = False
        for t in obj.get("tables", []) or []:
            if t.get("title", "").endswith("大盤統計資訊") and (t.get("data") or []):
                ok = True
                break
        if ok:
            return d
    return today


def parse_market_stats(ms_obj: dict) -> dict[str, str]:
    out = {}
    for t in ms_obj.get("tables", []) or []:
        if t.get("title", "") and t.get("title", "").endswith("大盤統計資訊"):
            for row in t.get("data", []) or []:
                if str(row[0]).startswith("總計"):
                    out["total_amount"] = row[1]
                if str(row[0]).startswith("證券合計"):
                    out["securities_amount"] = row[1]
        if t.get("title", "") == "漲跌證券數合計":
            # rows: 上漲(漲停), 下跌(跌停), etc.
            for row in t.get("data", []) or []:
                out[str(row[0]).strip()] = " / ".join(str(x) for x in row[1:])
    return out


def parse_institution_amounts(bfi_obj: dict) -> dict[str, int]:
    # amounts in TWD
    out = {}
    for row in bfi_obj.get("data", []) or []:
        name, buy, sell, net = row
        out[name] = int(str(net).replace(",", ""))
    return out


def parse_foreign_netbuy_top(twt38_obj: dict, topn: int = 10) -> list[tuple[str, str, int]]:
    # Fields: code/name/buy/sell/net ... net is string with commas
    rows = []
    for row in twt38_obj.get("data", []) or []:
        code = str(row[1]).strip()
        name = str(row[2]).strip()
        net = int(str(row[5]).replace(",", ""))
        if not is_common_stock(code):
            continue
        rows.append((code, name, net))
    rows.sort(key=lambda x: x[2], reverse=True)
    return rows[:topn]


def parse_investment_trust_top(t86_obj: dict, topn: int = 10) -> list[tuple[str, str, int]]:
    # fields include 投信買賣超股數
    fields = t86_obj.get("fields", [])
    col = {k: i for i, k in enumerate(fields)}
    if "投信買賣超股數" not in col:
        return []
    rows = []
    for r in t86_obj.get("data", []) or []:
        code = str(r[col["證券代號"]]).strip()
        name = str(r[col["證券名稱"]]).strip()
        net = int(str(r[col["投信買賣超股數"]]).replace(",", ""))
        if not is_common_stock(code):
            continue
        rows.append((code, name, net))
    rows.sort(key=lambda x: x[2], reverse=True)
    return rows[:topn]


def fmt_int(n: int) -> str:
    sign = "+" if n > 0 else ""
    return f"{sign}{n:,}"


def fmt_money(n: int) -> str:
    # n is TWD
    sign = "+" if n > 0 else ""
    return f"{sign}{n/1e8:.1f}億"


def build_html(
    date: dt.date,
    indices: dict[str, IndexRow],
    market: dict[str, str],
    inst_amounts: dict[str, int],
    limitups: list[StockRow],
    limitup_ind_rank: list[tuple[str, int]],
    foreign_top: list[tuple[str, str, int]],
    it_top: list[tuple[str, str, int]],
    watchlist: list[dict[str, Any]],
) -> str:
    dstr = date.strftime("%Y-%m-%d")

    def idx_line(key: str) -> str:
        r = indices.get(key)
        if not r:
            return "-"
        return f"{r.close:,.2f} ({r.chg:+.2f}, {r.pct:+.2f}%)"

    total_net = sum(inst_amounts.values())

    def tr(cols: list[str]) -> str:
        return "<tr>" + "".join(f"<td>{c}</td>" for c in cols) + "</tr>"

    limitups_n = len(limitups)

    html = f"""<!doctype html>
<html lang=\"zh-Hant\">
<head>
<meta charset=\"utf-8\"/>
<title>龍蝦AI選股報告 {dstr}</title>
<style>
  body {{ font-family: -apple-system,BlinkMacSystemFont,'Noto Sans TC','PingFang TC','Microsoft JhengHei',Arial,sans-serif; margin: 28px; color: #111; }}
  h1 {{ margin: 0 0 6px 0; font-size: 22px; }}
  h2 {{ margin: 18px 0 8px; font-size: 16px; }}
  .muted {{ color: #666; font-size: 12px; }}
  .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }}
  .card {{ border: 1px solid #ddd; border-radius: 10px; padding: 12px; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 12px; }}
  th, td {{ border-bottom: 1px solid #eee; padding: 6px 6px; text-align: left; vertical-align: top; }}
  th {{ background: #fafafa; }}
  .tag {{ display: inline-block; padding: 2px 6px; border: 1px solid #ccc; border-radius: 999px; font-size: 11px; margin-right: 4px; }}
</style>
</head>
<body>
  <h1>龍蝦AI選股報告（觀察清單）— {dstr}</h1>
  <div class=\"muted\">資料來源：TWSE 公開資料（實價/法人/收盤行情）。僅供資訊整理，不構成投資建議。</div>

  <div class=\"grid\" style=\"margin-top:12px\">
    <div class=\"card\">
      <h2>市場概況</h2>
      <div>加權指數：<b>{idx_line('發行量加權股價指數')}</b></div>
      <div>電子類：{idx_line('電子工業類指數')}</div>
      <div>半導體：{idx_line('半導體類指數')}</div>
      <div>成交金額：{market.get('total_amount','-')}（證券合計 {market.get('securities_amount','-')}）</div>
      <div>漲跌家數：上漲(漲停) {market.get('上漲(漲停)','-')}；下跌(跌停) {market.get('下跌(跌停)','-')}</div>
    </div>

    <div class=\"card\">
      <h2>三大法人（買賣差額，金額）</h2>
      <div>外資及陸資：<b>{fmt_money(inst_amounts.get('外資及陸資(不含外資自營商)',0))}</b></div>
      <div>投信：{fmt_money(inst_amounts.get('投信',0))}</div>
      <div>自營商(自行)：{fmt_money(inst_amounts.get('自營商(自行買賣)',0))}</div>
      <div>自營商(避險)：{fmt_money(inst_amounts.get('自營商(避險)',0))}</div>
      <div style=\"margin-top:6px\">合計：<b>{fmt_money(total_net)}</b></div>
    </div>
  </div>

  <h2>漲停概況（上市普通股，估算）</h2>
  <div class=\"muted\">以「4碼股票 + 收盤=最高 + 漲幅≥9.8%」估算。抓到 {limitups_n} 檔作為樣本。</div>
  <div class=\"card\" style=\"margin-top:8px\">
    <div><b>漲停族群 Top</b>：{"、".join([f"{k}({v})" for k,v in limitup_ind_rank[:6]]) if limitup_ind_rank else '-'}</div>
  </div>

  <div class=\"grid\" style=\"margin-top:12px\">
    <div class=\"card\">
      <h2>外資買超 Top {len(foreign_top)}</h2>
      <table>
        <thead><tr><th>代號</th><th>名稱</th><th>買超(股)</th></tr></thead>
        <tbody>
          {"".join(tr([c,n,f"{v:,}"]) for c,n,v in foreign_top) if foreign_top else tr(['-','-','-'])}
        </tbody>
      </table>
    </div>
    <div class=\"card\">
      <h2>投信買超 Top {len(it_top)}</h2>
      <table>
        <thead><tr><th>代號</th><th>名稱</th><th>買超(股)</th></tr></thead>
        <tbody>
          {"".join(tr([c,n,f"{v:,}"]) for c,n,v in it_top) if it_top else tr(['-','-','-'])}
        </tbody>
      </table>
    </div>
  </div>

  <h2>觀察清單（非建議買賣）</h2>
  <div class=\"card\">
    <table>
      <thead><tr><th>代號</th><th>名稱</th><th>產業</th><th>理由</th></tr></thead>
      <tbody>
        {"".join(tr([w['code'], w['name'], w.get('industry','-'), w.get('reason','-')]) for w in watchlist) if watchlist else tr(['-','-','-','-'])}
      </tbody>
    </table>
  </div>

  <div class=\"muted\" style=\"margin-top:12px\">
    提醒：題材股波動大；自住/長投與短線策略風險不同。請自行評估資金控管與停損。
  </div>
</body>
</html>"""
    return html


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", default=str(Path("reports") / "lobster"), help="output dir")
    ap.add_argument("--date", default="auto", help="YYYYMMDD or auto")
    ap.add_argument("--pdf", action="store_true", help="also render pdf via chrome")
    args = ap.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    today = dt.datetime.now(TZ).date()
    if args.date != "auto":
        d = dt.datetime.strptime(args.date, "%Y%m%d").date()
    else:
        d = most_recent_trading_date(today)

    # Fetch
    mi_all = curl_json(f"https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={yyyymmdd(d)}&type=ALL&response=json")
    mi_small = curl_json(f"https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={yyyymmdd(d)}&type=ALLBUT0999&response=json")
    ms_obj = curl_json(f"https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={yyyymmdd(d)}&type=MS&response=json")
    bfi = curl_json(f"https://www.twse.com.tw/rwd/zh/fund/BFI82U?type=day&dayDate={yyyymmdd(d)}&response=json")
    twt38 = curl_json(f"https://www.twse.com.tw/rwd/zh/fund/TWT38U?date={yyyymmdd(d)}&response=json")
    t86 = curl_json(f"https://www.twse.com.tw/rwd/zh/fund/T86?date={yyyymmdd(d)}&selectType=ALL&response=json")

    indices = parse_indices(mi_small)
    market = parse_market_stats(ms_obj)
    inst_amounts = parse_institution_amounts(bfi)

    daily = parse_daily_close_all(mi_all)
    limitups = detect_limit_up(daily)

    ind_map = load_industry_map()
    ind_cnt = Counter()
    ind_bucket = defaultdict(list)
    for r in limitups:
        ind_code = ind_map.get(r.code)
        ind_name = INDUSTRY_LABELS.get(ind_code, ind_code or "未知")
        ind_cnt[ind_name] += 1
        ind_bucket[ind_name].append(r)
    ind_rank = ind_cnt.most_common()

    foreign_top = parse_foreign_netbuy_top(twt38, topn=10)
    it_top = parse_investment_trust_top(t86, topn=10)

    # Build a conservative watchlist: prefer names that are (limit-up OR in top foreign/it)
    daily_by_code = {r.code: r for r in daily if is_common_stock(r.code)}
    tags = defaultdict(set)
    for r in limitups:
        tags[r.code].add("漲停")
    for code, _, _ in foreign_top:
        tags[code].add("外資買超")
    for code, _, _ in it_top:
        tags[code].add("投信買超")

    # score: limit-up>foreign>it
    def score(code: str) -> int:
        t = tags.get(code, set())
        return (100 if "漲停" in t else 0) + (10 if "外資買超" in t else 0) + (5 if "投信買超" in t else 0)

    candidates = sorted(tags.keys(), key=lambda c: (score(c), c), reverse=True)
    watch = []
    for code in candidates:
        row = daily_by_code.get(code)
        if not row:
            continue
        ind_code = ind_map.get(code)
        ind_name = INDUSTRY_LABELS.get(ind_code, ind_code or "未知")
        reason = "、".join(sorted(tags[code]))
        watch.append({"code": code, "name": row.name, "industry": ind_name, "reason": reason})
        if len(watch) >= 10:
            break

    html = build_html(
        date=d,
        indices=indices,
        market=market,
        inst_amounts=inst_amounts,
        limitups=limitups,
        limitup_ind_rank=ind_rank,
        foreign_top=foreign_top,
        it_top=it_top,
        watchlist=watch,
    )

    date_tag = d.strftime("%Y%m%d")
    html_path = outdir / f"lobster_{date_tag}.html"
    pdf_path = outdir / f"lobster_{date_tag}.pdf"
    html_path.write_text(html, encoding="utf-8")

    pdf_ok = False
    if args.pdf:
        file_url = f"file://{html_path.resolve()}"
        cmd = [
            "google-chrome",
            "--headless",
            "--disable-gpu",
            "--no-sandbox",
            f"--print-to-pdf={pdf_path}",
            file_url,
        ]
        subprocess.check_call(cmd, timeout=60)
        pdf_ok = pdf_path.exists() and pdf_path.stat().st_size > 0

    # Print summary for the agent/cron runner
    idx = indices.get("發行量加權股價指數")
    total_net = sum(inst_amounts.values())
    top_inds = "、".join([f"{k}({v})" for k, v in ind_rank[:3]]) if ind_rank else "-"

    summary_lines = [
        f"【龍蝦AI選股報告 {d.strftime('%Y-%m-%d')}】",
        f"加權：{idx.close:,.2f} ({idx.chg:+.2f}, {idx.pct:+.2f}%)" if idx else "加權：-",
        f"三大法人合計：{fmt_money(total_net)}（外資 {fmt_money(inst_amounts.get('外資及陸資(不含外資自營商)',0))}）",
        f"漲停族群Top：{top_inds}",
        "觀察清單：" + "、".join([f"{w['name']}({w['code']})" for w in watch[:6]]),
        f"HTML: {html_path}",
        f"PDF: {pdf_path}" if pdf_ok else f"PDF: (not generated)",
    ]
    print("\n".join(summary_lines))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
