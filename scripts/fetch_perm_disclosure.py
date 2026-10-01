#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""方案一核心脚本：用真实浏览器下载 DOL 的 PERM 季度披露表，聚合成小 JSON 提交回仓库。

为什么必须用 Playwright：www.dol.gov 在 Akamai 后面，普通 HTTP 客户端（含 Cloudflare
边缘、本机浏览器之外的任何脚本）一律 403。这里用 headless Chromium 打开 performance
页面，取最新那份 PERM_Disclosure_Data_FYxxxx_Qx.xlsx，再用同一个浏览器上下文下载。

只发布**聚合结果**，不发布逐案清单：披露文件本身含雇主名称与案号，提交到公开仓库等于
做一个"按雇主搜案件"的库，这不是我们要的。

用法：
  python scripts/fetch_perm_disclosure.py                 # 正常：浏览器下载 + 聚合
  python scripts/fetch_perm_disclosure.py --file X.xlsx   # 手动：用已下载好的文件聚合
输出：
  data/perm_case_stats.json   聚合数据（给 Worker 读）
  data/perm_status.json       本次运行状态（成功/失败、来源、行数）
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

PERF_URL = "https://www.dol.gov/agencies/eta/foreign-labor/performance"
OUT_DIR = Path(__file__).resolve().parent.parent / "data"
STATS_FILE = OUT_DIR / "perm_case_stats.json"
STATUS_FILE = OUT_DIR / "perm_status.json"

# 跨财年的列名变体（参考同类实现 + 官方记录布局）
RECEIVED_COLS = ["RECEIVED_DATE", "CASE_RECEIVED_DATE", "CASE_RECEIVED", "RECEIVED"]
DECISION_COLS = ["DECISION_DATE", "CASE_DECISION_DATE", "DECISION"]
STATUS_COLS = ["CASE_STATUS", "STATUS", "DECISION"]
EMPLOYER_COLS = ["EMP_BUSINESS_NAME", "EMP_TRADE_NAME", "EMPLOYER_NAME", "EMPLOYER", "EMPLOYER_NAME_AS"]
PROGRAM_COLS = ["PROGRAM", "CASE_TYPE", "PROGRAM_NAME"]

STATUS_KEYS = {
    "certified": "certified",
    "certified - expired": "certifiedExpired",
    "certified-expired": "certifiedExpired",
    "certified expired": "certifiedExpired",
    "denied": "denied",
    "withdrawn": "withdrawn",
}

MAX_DECISION_MONTHS = 24      # 控制 JSON 体积
MAX_SUBMIT_MONTHS = 36        # 老递交月队列才有可用的裁决前沿，见 letter_progress 注释
MIN_LETTER_CASES = 3          # 样本太少的字母不输出，避免"1 个案子"式噪声
LETTERS = [chr(c) for c in range(ord("A"), ord("Z") + 1)]


def log(msg: str) -> None:
    print(msg, flush=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_status(**kw) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    STATUS_FILE.write_text(json.dumps(kw, ensure_ascii=False, indent=1), encoding="utf-8")


def download_with_browser() -> tuple[bytes, str]:
    """返回 (xlsx 字节, 来源文件名)。被拦时抛异常，由调用方写失败状态。"""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
            locale="en-US",
        )
        page = ctx.new_page()
        page.goto(PERF_URL, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(3000)
        body = page.content()
        if "Access Denied" in body or "errors.edgesuite.net" in body:
            browser.close()
            raise RuntimeError("DOL 拦了这台 runner 的 IP（Access Denied）")
        hrefs = page.eval_on_selector_all("a[href*='.xlsx']", "els => els.map(e => e.href)")
        best, best_rank = None, (-1, -1)
        for href in hrefs:
            m = re.search(r"PERM_Disclosure_Data_FY(\d{4})_Q(\d)", href, re.I)
            if m and (int(m.group(1)), int(m.group(2))) > best_rank:
                best, best_rank = href, (int(m.group(1)), int(m.group(2)))
        if not best:
            browser.close()
            raise RuntimeError("页面上没找到 PERM_Disclosure_Data_FYxxxx_Qx.xlsx，可能改版")
        resp = ctx.request.get(best, timeout=180000)
        if not resp.ok:
            browser.close()
            raise RuntimeError("下载失败 HTTP %s" % resp.status)
        data = resp.body()
        browser.close()
    name = best.rsplit("/", 1)[-1]
    log("下载 %s：%s 字节" % (name, format(len(data), ",")))
    return data, name


def pick(cols, cands):
    for c in cands:
        if c in cols:
            return c
    for c in cands:                       # 再宽松匹配一次
        for col in cols:
            if c in col:
                return col
    return None


def month_of(ts) -> str:
    return "" if ts is None or pd.isna(ts) else ts.strftime("%Y-%m")


def day_of(ts) -> str:
    return "" if ts is None or pd.isna(ts) else ts.strftime("%Y-%m-%d")


def median(vals):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    n = len(vals)
    return int(round(vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2.0))


def quantile(vals, q):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    idx = min(len(vals) - 1, max(0, int(round(q * (len(vals) - 1)))))
    return int(vals[idx])


def aggregate(xlsx_bytes: bytes, source_name: str) -> dict:
    global pd
    import pandas as pd

    df = pd.read_excel(io.BytesIO(xlsx_bytes), engine="openpyxl",
                       usecols=lambda c: True, dtype=object)
    df.columns = [str(c).strip().upper() for c in df.columns]
    cols = list(df.columns)
    received, decision, status = pick(cols, RECEIVED_COLS), pick(cols, DECISION_COLS), pick(cols, STATUS_COLS)
    employer, program = pick(cols, EMPLOYER_COLS), pick(cols, PROGRAM_COLS)
    if not (received and decision and status):
        raise ValueError("列名没认出来，前 12 列=%s" % cols[:12])
    log("列名：received=%s decision=%s status=%s employer=%s program=%s"
        % (received, decision, status, employer, program))

    total_rows = len(df)
    if program:
        perm_mask = df[program].astype(str).str.upper().str.contains("PERM", na=False)
        if perm_mask.sum() > 0:
            df = df[perm_mask]
    df[received] = pd.to_datetime(df[received], errors="coerce")
    df[decision] = pd.to_datetime(df[decision], errors="coerce")
    df["_days"] = (df[decision] - df[received]).dt.days
    df["_status"] = df[status].astype(str).str.strip().str.lower()
    df["_letter"] = (df[employer].astype(str).str.strip().str.upper().str[0]
                     if employer else "")
    df["_letter"] = df["_letter"].where(df["_letter"].isin(LETTERS), "")
    decided = df.dropna(subset=[decision])
    log("总行数 %s，其中已裁决 %s" % (format(total_rows, ","), format(len(decided), ",")))

    # 1) 按裁决月
    by_decision = []
    for month, g in decided.groupby(decided[decision].dt.strftime("%Y-%m")):
        counts = g["_status"].map(STATUS_KEYS).dropna().value_counts()
        merits = g[g["_status"] != "withdrawn"]
        mdays = merits["_days"].tolist()
        by_decision.append({
            "month": month, "total": int(len(g)),
            "certified": int(counts.get("certified", 0)),
            "certifiedExpired": int(counts.get("certifiedExpired", 0)),
            "denied": int(counts.get("denied", 0)),
            "withdrawn": int(counts.get("withdrawn", 0)),
            "medianDays": median(g["_days"].tolist()),
            "p25Days": quantile(g["_days"].tolist(), 0.25),
            "p75Days": quantile(g["_days"].tolist(), 0.75),
            "merits": {"n": int(len(merits)), "p25Days": quantile(mdays, 0.25),
                       "medianDays": median(mdays), "p75Days": quantile(mdays, 0.75)},
        })
    by_decision.sort(key=lambda r: r["month"])
    by_decision = by_decision[-MAX_DECISION_MONTHS:]

    # 2) 按递交月队列
    #    merits = 排除 withdrawn 的已裁决案件。撤回案往往几天就结案，会把它混进来的
    #    队列天数中位数大幅拉低（2025-09 递交月：全部裁决口径 180 天 vs 排除撤回 280 天）。
    by_submit = []
    for month, g in df.groupby(df[received].dt.strftime("%Y-%m"), dropna=True):
        dec = g.dropna(subset=[decision])
        counts = dec["_status"].map(STATUS_KEYS).dropna().value_counts()
        merits = dec[dec["_status"] != "withdrawn"]
        mdays = merits["_days"].tolist()
        by_submit.append({
            "month": month, "received": int(len(g)), "decided": int(len(dec)),
            "certified": int(counts.get("certified", 0)),
            "denied": int(counts.get("denied", 0)),
            "withdrawn": int(counts.get("withdrawn", 0)),
            "medianDays": median(dec["_days"].tolist()),
            "p75Days": quantile(dec["_days"].tolist(), 0.75),
            "merits": {"n": int(len(merits)), "p25Days": quantile(mdays, 0.25),
                       "medianDays": median(mdays), "p75Days": quantile(mdays, 0.75)},
        })
    by_submit.sort(key=lambda r: r["month"])
    by_submit = by_submit[-MAX_SUBMIT_MONTHS:]

    # 3) 字母进度：每个递交月里，各首字母已经裁决到哪个收到日。
    #    注意：披露表只含"本期已裁决"的案件，队列越新，样本越偏向"几天内就结案"的那一小撮，
    #    前沿值就没有参考价值。所以这里保留全部递交月（老队列才有意义），由前端按队列年龄决定是否出示。
    letter_rows = []
    df["_sm"] = df[received].dt.strftime("%Y-%m")
    for month, gm in df.groupby("_sm"):
        for letter in LETTERS:
            sub = gm[gm["_letter"] == letter]
            if len(sub) < MIN_LETTER_CASES:
                continue
            done = sub.dropna(subset=[decision])
            letter_rows.append({
                "month": month, "letter": letter, "cases": int(len(sub)),
                "decided": int(len(done)),
                "lastDecidedReceived": (day_of(max(done[received])) if len(done) else ""),
                "medianDays": median(done["_days"].tolist()),
            })

    # 4) 日增量（只统计已裁决案件的裁决日；收到日分布有偏，标记出来）
    daily_dec, daily_rec = defaultdict(int), defaultdict(int)
    for _, row in decided.iterrows():
        daily_dec[day_of(row[decision])] += 1
        daily_rec[day_of(row[received])] += 1
    recent_days = sorted({d for d in daily_dec if d})[-45:]

    return {
        "schema": 1,
        "generated_at": now_iso(),
        "source": source_name,
        "rows_total": int(total_rows),
        "rows_decided": int(len(decided)),
        "columns": {"received": received, "decision": decision, "status": status,
                    "employer": employer, "program": program},
        "by_decision_month": by_decision,
        "by_submit_month": by_submit,
        "letter_progress": letter_rows,
        "daily": {
            "decided": [{"date": d, "count": daily_dec.get(d, 0)} for d in recent_days],
            "received_biased": [{"date": d, "count": daily_rec.get(d, 0)} for d in recent_days],
            "note": "received_biased 只包含已裁决案件的收到日，越靠近今天的月份越不完整，不能当新增量用",
        },
        "coverage": {"first_received": day_of(min(df[received].dropna())) if df[received].notna().any() else "",
                     "last_decision": (day_of(decided[decision].max()) if len(decided) else "")},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", help="用本地已下载的 xlsx（手动兜底）")
    args = ap.parse_args()

    try:
        if args.file:
            name = Path(args.file).name
            data = Path(args.file).read_bytes()
            log("使用本地文件 %s（%s 字节）" % (name, format(len(data), ",")))
        else:
            data, name = download_with_browser()
        doc = aggregate(data, name)
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        STATS_FILE.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        size = STATS_FILE.stat().st_size
        write_status(ok=True, at=doc["generated_at"], source=name,
                     rows_total=doc["rows_total"], rows_decided=doc["rows_decided"],
                     stats_bytes=size,
                     last_decision_month=doc["by_decision_month"][-1]["month"]
                     if doc["by_decision_month"] else "")
        log("已写入 %s：%d 字节，裁决月 %s ~ %s"
            % (STATS_FILE, size,
               doc["by_decision_month"][0]["month"] if doc["by_decision_month"] else "-",
               doc["by_decision_month"][-1]["month"] if doc["by_decision_month"] else "-"))
        return 0
    except Exception as exc:
        log("失败：%s" % exc)
        write_status(ok=False, at=now_iso(), error=str(exc)[:300])
        return 1


if __name__ == "__main__":
    sys.exit(main())
