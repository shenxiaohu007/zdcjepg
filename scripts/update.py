#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
直播源自动抓取 + 检测 + 导出（纯标准库，适配 GitHub Actions / 本机 cron）

用法：
    python3 scripts/update.py                  # 用 config.json
    python3 scripts/update.py --limit 300       # 只测 300 条（调试）
    python3 scripts/update.py --sources a.m3u   # 只测指定清单
"""
import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from checker import check, clean_name, category, parse_m3u_text, fetch  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, ".cache")
ORDER = ["央视", "卫视", "港澳台", "地方台", "特色"]


def log(msg):
    print("[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg), flush=True)


def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def download_sources(sources, timeout):
    """下载所有清单，返回合并后的 m3u 文本"""
    if not os.path.isdir(CACHE):
        os.makedirs(CACHE)
    texts = []
    for i, url in enumerate(sources):
        out = os.path.join(CACHE, "src_%02d.m3u" % i)
        code, _, size, _ = fetch(url, out, timeout)
        if code in (200, 206) and size > 100:
            try:
                with open(out, "r", encoding="utf-8", errors="ignore") as f:
                    txt = f.read()
                n = txt.count("\nhttp") + txt.count("\nHTTP")
                log("源[%d] OK %s 条  %s" % (i, n, url))
                texts.append(txt)
            except Exception as e:
                log("源[%d] 读取失败 %s (%s)" % (i, url, e))
        else:
            log("源[%d] 跳过 HTTP %s  %s" % (i, code, url))
    return "\n".join(texts)


def run_checks(items, cfg, rounds=2):
    timeout = cfg.get("timeout", 8)
    workers = cfg.get("concurrency", 40)
    passed = items
    for r in range(1, rounds + 1):
        t0 = time.time()
        out, done = [], 0
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for res in ex.map(lambda it: check(it, timeout=timeout), passed):
                out.append(res)
                done += 1
                if done % 100 == 0:
                    log("  第%s轮 %d/%d 通过 %d  %.0fs" %
                        (r, done, len(passed), sum(1 for x in out if x["ok"]), time.time() - t0))
        passed = [x for x in out if x["ok"]]
        log("第%s轮结束：%d 条通过 (%.0fs)" % (r, len(passed), time.time() - t0))
        if not passed:
            break
    return passed


def export(passed, cfg, outdir):
    if not os.path.isdir(outdir):
        os.makedirs(outdir)
    seen, uniq = set(), []
    for x in passed:
        n = clean_name(x["name"])
        # 规范化频道名：CCTV5 / CCTV 5 → CCTV-5，统一横杠写法
        n = re.sub(r'^CCTV[\s\-]*(\d+)', lambda m: 'CCTV-%s' % m.group(1), n, flags=re.I)
        if (n, x["url"]) in seen:
            continue
        seen.add((n, x["url"]))
        uniq.append({"name": n, "url": x["url"], "cat": category(n)})

    bycat = {}
    for x in uniq:
        bycat.setdefault(x["cat"], {}).setdefault(x["name"], []).append(x["url"])

    max_per = int(cfg.get("max_per_channel", 3))

    # 排序：CCTV 按数字 1→17，其他频道按名字
    def sort_key(name):
        m = re.match(r'^CCTV-(\d+)(.*)', name, re.I)
        if m:
            return (0, int(m.group(1)), m.group(2), name)
        return (1, 0, '', name)

    # TVBox / 骆驼后台通用 TXT
    lines = []
    for c in ORDER:
        if c not in bycat:
            continue
        lines.append("%s,#genre#" % c)
        for n in sorted(bycat[c].keys(), key=sort_key):
            for u in bycat[c][n][:max_per]:
                lines.append("%s,%s" % (n, u))
        lines.append("")
    live_txt = os.path.join(outdir, "live.txt")
    with open(live_txt, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).strip() + "\n")

    # M3U
    m3u = ["#EXTM3U"]
    for c in ORDER:
        for n, urls in sorted(bycat.get(c, {}).items(), key=lambda x: sort_key(x[0])):
            for u in urls[:max_per]:
                m3u.append('#EXTINF:-1 group-title="%s",%s' % (c, n))
                m3u.append(u)
    live_m3u = os.path.join(outdir, "live.m3u")
    with open(live_m3u, "w", encoding="utf-8") as f:
        f.write("\n".join(m3u) + "\n")

    # 报告
    rep = ["直播源检测报告",
           "生成时间：%s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "通过条目：%d" % len(uniq),
           "覆盖频道：%d" % len(set(x["name"] for x in uniq)),
           ""]
    for c in ORDER:
        if c in bycat:
            rep.append("%-4s 频道 %3d  链接 %3d" %
                       (c, len(bycat[c]), sum(len(v) for v in bycat[c].values())))
    report_txt = os.path.join(outdir, "report.txt")
    with open(report_txt, "w", encoding="utf-8") as f:
        f.write("\n".join(rep) + "\n")
    channels = len(set(x["name"] for x in uniq))
    return live_txt, live_m3u, report_txt, len(uniq), channels


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(ROOT, "config.json"))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--sources", nargs="*", default=None)
    ap.add_argument("--rounds", type=int, default=0)
    args = ap.parse_args()

    cfg = load_config(args.config)
    sources = args.sources or cfg["sources"]
    rounds = args.rounds or cfg.get("rounds", 2)
    outdir = os.path.join(ROOT, cfg.get("output_dir", "output"))

    log("读取 %d 个清单源" % len(sources))
    text = download_sources(sources, cfg.get("fetch_timeout", 20))
    items = parse_m3u_text(text)

    inc = cfg.get("include_keywords") or []
    exc = cfg.get("exclude_keywords") or []
    if inc:
        items = [x for x in items if any(k.lower() in x["name"].lower() for k in inc)]
    if exc:
        items = [x for x in items if not any(k.lower() in x["name"].lower() for k in exc)]

    # 白名单：只保留 CCTV-1~17 和 卫视，其他全砍
    def is_allowed(name):
        n = name.upper().strip()
        # 所有含"卫视"的频道都留
        if "卫视" in name:
            return True
        # CCTV-1 到 CCTV-17（兼容 CCTV5 / CCTV 5 / CCTV-5，排除 4K/8K 衍生频道）
        m = re.match(r'^CCTV[\s\-]*(\d+)(?!\w)', n)
        if m and 1 <= int(m.group(1)) <= 17:
            return True
        return False
    before = len(items)
    items = [x for x in items if is_allowed(x["name"])]
    log("白名单过滤：%d → %d 条（只留 CCTV-1~17 + 卫视）" % (before, len(items)))

    # 去重 URL
    seen, dedup = set(), []
    for x in items:
        if x["url"] in seen:
            continue
        seen.add(x["url"])
        dedup.append(x)

    limit = args.limit or cfg.get("max_candidates", 0)
    if limit and len(dedup) > limit:
        # 截断时优先保留重点频道（央视/卫视），避免被清单顺序埋没
        pri = [k.lower() for k in cfg.get("priority_keywords", ["CCTV", "卫视", "CGTN"])]
        first = [x for x in dedup if any(k in x["name"].lower() for k in pri)]
        rest = [x for x in dedup if not any(k in x["name"].lower() for k in pri)]
        dedup = (first + rest)[:limit]
        log("候选 %d 条，截断到 %d 条（其中重点频道 %d 条）" % (len(first) + len(rest), limit, min(len(first), limit)))

    log("开始检测 %d 条，共 %d 轮" % (len(dedup), rounds))
    passed = run_checks(dedup, cfg, rounds=rounds)
    txt, m3u, rep, n, ch = export(passed, cfg, outdir)
    log("导出完成：%d 条 / %d 个频道" % (n, ch))
    log("  %s" % txt)
    log("  %s" % m3u)
    log("  %s" % rep)


if __name__ == "__main__":
    main()
