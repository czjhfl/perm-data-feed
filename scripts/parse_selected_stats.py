#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""解析 DOL 的 PERM Selected Statistics PDF → data/perm_quarter_stats.json。

为什么需要它：季度披露表里**只有已裁决的案件**，算不出"这个季度收到多少"和"还剩多少在排队"。
这份 PDF 是官方按季度发布的统计表，正好补上那两个分母（收到量、在办存量）。

依赖系统的 pdftotext（poppler-utils）。解析失败就抛错、不写文件 —— 上层管线会保留上一版数据。

用法：python3 -X utf8 github/scripts/parse_selected_stats.py <pdf 路径>
"""
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
OUT = HERE / "data" / "perm_quarter_stats.json"

NUM = r"(?:[\d,]+|--)"


def n(tok):
    tok = (tok or "").strip().replace(",", "")
    if tok in ("", "--", "-", "N/A"):
        return None
    return int(float(tok))


def text_of(pdf):
    if shutil.which("pdftotext") is None:
        raise RuntimeError("系统里没有 pdftotext（poppler-utils），无法解析统计表")
    r = subprocess.run(["pdftotext", "-layout", str(pdf), "-"],
                       capture_output=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError("pdftotext 失败：%s" % r.stderr.decode("utf-8", "replace")[:200])
    return r.stdout.decode("utf-8", "replace")


def parse(txt):
    head = re.search(r"Selected Statistics,?\s*Fiscal Year \(FY\)\s*(\d{4})\s*Q(\d)", txt, re.I)
    if not head:
        raise ValueError("没找到「Selected Statistics, Fiscal Year (FY) nnnn Qn」标题，官方可能改版")
    fy, quarter = int(head.group(1)), "Q" + head.group(2)

    # Applications Received：一行五个数（FY 合计 + Q1..Q4）+ 同比
    # 注意：PDF 是左右两栏排版，pdftotext 会把右栏的 Top 10 表格拼在同一行末尾，
    # 所以这里只锚定行首、不要求行尾结束。
    recv = None
    for line in txt.splitlines():
        m = re.match(r"^\s*([\d,]{3,9})\s+(" + NUM + r")\s+(" + NUM + r")\s+(" + NUM +
                     r")\s+(" + NUM + r")\s+(-?[\d.]+%)", line)
        if m:
            recv = {"fy": n(m.group(1)), "Q1": n(m.group(2)), "Q2": n(m.group(3)),
                    "Q3": n(m.group(4)), "Q4": n(m.group(5)),
                    "change_from_prior_fy_pct": float(m.group(6).rstrip("%"))}
            break
    if not recv:
        raise ValueError("没解析到 Applications Received 行")

    # Applications Processed：Certified / Denied / Withdrawn / Total 各一行
    proc = {}
    for label, key in (("Certified", "certified"), ("Denied", "denied"),
                       ("Withdrawn", "withdrawn"), ("Total", "total")):
        m = re.search(r"^\s*" + label + r"\s+([\d,]+)\s+(" + NUM + r")\s+(" + NUM +
                      r")\s+(" + NUM + r")\s+(" + NUM + r")", txt, re.M)
        if not m:
            raise ValueError("没解析到 Processed 的 %s 行" % label)
        proc[key] = {"fy": n(m.group(1)), "Q1": n(m.group(2)), "Q2": n(m.group(3)),
                     "Q3": n(m.group(4)), "Q4": n(m.group(5))}

    # 在办存量：正文那句 "120,429 applications remaining as of 6/30/2026.
    #   There were zero (0) cases pending for audit and one (1) case pending for supervised recruitment."
    back = re.search(r"([\d,]+)\s+applications remaining as of\s+(\d{1,2})/(\d{1,2})/(\d{4})", txt)
    if not back:
        raise ValueError("没解析到 applications remaining 那句存量数字")
    total_remaining = n(back.group(1))
    as_of = "%s-%02d-%02d" % (int(back.group(4)), int(back.group(2)), int(back.group(3)))
    audit = re.search(r"\((\d+)\)\s*cases? pending for audit", txt, re.I)
    sr = re.search(r"\((\d+)\)\s*case[s]? pending for\s+supervised recruitment", txt, re.I)
    audit_n = n(audit.group(1)) if audit else None
    sr_n = n(sr.group(1)) if sr else None
    known = sum(v for v in (audit_n, sr_n) if v is not None)
    backlog = {"total": total_remaining, "as_of": as_of, "audit": audit_n,
               "supervised_recruitment": sr_n, "other_in_review": total_remaining - known}

    # 交叉核对：Processed 的合计应当等于各分项之和
    parts = sum((proc[k]["fy"] or 0) for k in ("certified", "denied", "withdrawn"))
    if proc["total"]["fy"] != parts:
        raise ValueError("Processed 分项加不起来：%s != %s" % (parts, proc["total"]["fy"]))

    return {"schema": 1, "fiscal_year": fy, "quarter": quarter, "as_of": as_of,
            "received": recv, "processed": proc, "backlog": backlog}


def main():
    if len(sys.argv) < 2:
        sys.exit("用法：parse_selected_stats.py <PDF 路径>")
    pdf = Path(sys.argv[1])
    if not pdf.exists():
        sys.exit("找不到文件：%s" % pdf)
    doc = parse(text_of(pdf))
    doc["source_pdf"] = pdf.name
    doc["parsed_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print("已写入 %s" % OUT)
    print("  FY%d %s（截至 %s）：收到 %s｜处理 %s（认证 %s / 拒批 %s / 撤回 %s）｜在办 %s"
          % (doc["fiscal_year"], doc["quarter"], doc["as_of"],
             format(doc["received"]["fy"], ","), format(doc["processed"]["total"]["fy"], ","),
             format(doc["processed"]["certified"]["fy"], ","),
             format(doc["processed"]["denied"]["fy"], ","),
             format(doc["processed"]["withdrawn"]["fy"], ","),
             format(doc["backlog"]["total"], ",")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
