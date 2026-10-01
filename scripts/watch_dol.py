#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""侦察 DOL 有没有发布新一季度的 PERM 披露表。只读页面、只比文件名，不下载大文件。

为什么放在 GitHub Actions 上跑：www.dol.gov 按客户端 TLS 指纹拦脚本 —— Cloudflare 边缘和本机的
curl/urllib 都吃 403，只有真实浏览器能过。实测 runner 的 Chromium **能打开这个页面**（拿到完整
文件列表），只是下载 xlsx 会被 403。所以让 runner 当侦察兵（免费、不依赖任何人的电脑开着），
取原件那一步留给本机。

结果写进 data/perm_feed_watch.json，可以直接在浏览器里打开看：
  https://raw.githubusercontent.com/<user>/perm-data-feed/main/data/perm_feed_watch.json
"""
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

PERF_URL = "https://www.dol.gov/agencies/eta/foreign-labor/performance"
HERE = Path(__file__).resolve().parent.parent
WATCH_FILE = HERE / "data" / "perm_feed_watch.json"
STATUS_FILE = HERE / "data" / "perm_status.json"
PAGE_URL = "https://github.com/czjhfl/perm-data-feed/blob/main/data/perm_feed_watch.json"

NAME_RE = re.compile(r"PERM_Disclosure_Data_(FY\w+?)\.xlsx$", re.I)
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def rank(name):
    """按财年/季度排出新旧。FY2026_Q3 > FY2026_Q2 > FY2026；两年的比法：先比 4 位年份。"""
    m = re.match(r"FY(\d{4})_?(?:Q(\d))?(?:_EOY)?$", name, re.I)
    if not m:
        m2 = re.match(r"FY(\d{2})_?(?:Q(\d))?$", name, re.I)   # 老文件写作 FY17 / FY16
        if not m2:
            return (0, 0)
        return (2000 + int(m2.group(1)), int(m2.group(2) or 0))
    return (int(m.group(1)), int(m.group(2) or 0))


def latest_name(url):
    """从直链里取出基础文件名，去掉 New_Form 这种变体（本站管线用的是标准披露表）。"""
    base = url.rsplit("/", 1)[-1]
    if "New_Form" in base:
        return None
    m = NAME_RE.search(base)
    return m.group(1) if m else None


def read_page():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(user_agent=UA, locale="en-US")
        page = ctx.new_page()
        page.goto(PERF_URL, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(3000)
        html = page.content()
        if "Access Denied" in html or "edgesuite" in html:
            b.close()
            raise RuntimeError("页面被 Akamai 拦了（runner 这次的出口 IP 不行）")
        links = page.eval_on_selector_all("a[href*='.xlsx']", "els => els.map(e => e.href)")
        b.close()
    names = [n for n in (latest_name(u) for u in links) if n]
    if not names:
        raise RuntimeError("页面上没找到 PERM_Disclosure_Data_*.xlsx，官方可能改版了")
    return max(names, key=rank), len(names)


def known_source():
    try:
        return str(json.loads(STATUS_FILE.read_text(encoding="utf-8")).get("source") or "")
    except Exception:
        return ""


def load_prev():
    try:
        return json.loads(WATCH_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def main():
    prev, known = load_prev(), known_source()
    doc = {"checked_at": now_iso(), "page": PERF_URL, "readable_at": PAGE_URL}
    try:
        latest, total = read_page()
        print("页面上最新披露表：%s（共找到 %d 个 PERM 文件）" % (latest, total))
        doc["ok"] = True
        doc["latest_file"] = "PERM_Disclosure_Data_" + latest + ".xlsx"
        doc["files_on_page"] = total
        if known and doc["latest_file"] != known:
            doc["status"] = "new-file"
            doc["message"] = ("官方已发布 %s，比本站现有数据（%s）更新 —— "
                              "需要在能跑真实浏览器的机器上取一次数" %
                              (doc["latest_file"], known))
        else:
            doc["status"] = "up-to-date"
            doc["message"] = "还是 %s，本站点上的数据就是最新的，不用做任何事" % (known or doc["latest_file"])
    except Exception as exc:
        print("侦察失败：%s" % exc)
        # 失败不抹掉上一次的结论，只把 ok 置假；下周一还会再试
        doc["ok"] = False
        doc["error"] = str(exc)[:300]
        doc["latest_file"] = prev.get("latest_file", "")
        known_txt = known or prev.get("latest_file", "")
        if prev.get("status") == "new-file":
            doc["status"] = "new-file"
            doc["message"] = ("上次已发现新文件 %s，本次没能复核（%s）—— 仍需取数"
                              % (prev.get("latest_file"), str(exc)[:80]))
        else:
            doc["status"] = "unknown"
            doc["message"] = "这次没读到页面，下周一自动再试；本站数据仍是 %s" % (known_txt or "未知")
    WATCH_FILE.parent.mkdir(parents=True, exist_ok=True)
    WATCH_FILE.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print("已写入 %s：status=%s" % (WATCH_FILE, doc["status"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
