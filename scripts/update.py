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
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from checker import check, clean_name, category, parse_m3u_text, fetch  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, ".cache")
ORDER = ["央视", "卫视", "港澳台", "地方台", "特色"]

# 广告池落点特征：这套源站 302 后会落到 /applive/{mkt|yss|102}.m3u8 时是购物广告
AD_POOL = re.compile(r'/applive/(mkt|yss|102)\.m3u8', re.I)


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


def _norm_name(name):
    """频道名规范化，与 export() 保持一致：CCTV5 / CCTV 5 -> CCTV-5"""
    n = clean_name(name)
    return re.sub(r'^CCTV[\s\-]*(\d+)', lambda m: 'CCTV-%s' % m.group(1), n, flags=re.I)


def probe_ad_hits(url, samples=5, gap=3, timeout=6):
    """对同一个入口采样 samples 次，返回 (落广告池次数, 成功请求次数)

    这套源站每次请求会随机/轮换落到不同池子：
      真台池 cctv3md / gzws / hews ...      -> 正常
      广告池 mkt / yss / 102                -> 购物广告
    所以单次检测会漏判，必须多次采样数比例。
    """
    hits = 0
    ok = 0
    tmp = os.path.join(tempfile.gettempdir(), "ad_probe.m3u8")
    for i in range(samples):
        try:
            code, _, _, eff = fetch(url, tmp, timeout)
        except Exception:
            code, eff = 0, ""
        if code == 200 and eff:
            ok += 1
            if AD_POOL.search(eff):
                hits += 1
        if i < samples - 1:
            time.sleep(gap)
    return hits, ok


def filter_ads(passed, cfg, outdir):
    """实时检出广告入口并剔除；同频道已有干净源时优先放弃偶发跳转的源"""
    if not passed:
        return passed
    samples = int(cfg.get("ad_samples", 5))
    gap = float(cfg.get("ad_gap", 3))
    timeout = int(cfg.get("ad_timeout", 6))
    workers = int(cfg.get("ad_workers", 16))
    threshold = int(cfg.get("ad_threshold", 3))   # 5 次采样中 >=3 次落广告池即判定为广告入口

    t0 = time.time()
    hits_map = {}
    fail_map = {}

    def work(it):
        h, ok = probe_ad_hits(it["url"], samples, gap, timeout)
        return it["url"], h, ok

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for url, h, ok in ex.map(work, passed):
            hits_map[url] = h
            fail_map[url] = (ok == 0)

    kept, dropped = [], []
    for it in passed:
        h = hits_map.get(it["url"], 0)
        if h >= threshold:
            dropped.append(it)
        else:
            it["_ad_hits"] = h
            kept.append(it)

    # 同一频道已经有完全干净的源时，偶发跳转的源不再占用名额
    clean_names = set(_norm_name(it["name"]) for it in kept if it["_ad_hits"] == 0)
    final = [it for it in kept if it["_ad_hits"] == 0 or _norm_name(it["name"]) not in clean_names]
    soft = len(kept) - len(final)

    log("[广告检测] 采样%d次/间隔%.0fs：待检 %d，固定广告 %d，偶发跳转 %d，采样全失败 %d，最终保留 %d (%.0fs)"
        % (samples, gap, len(passed), len(dropped), soft,
           sum(1 for v in fail_map.values() if v), len(final), time.time() - t0))

    if dropped or soft:
        if not os.path.isdir(outdir):
            os.makedirs(outdir)
        p = os.path.join(outdir, "ad_dropped.txt")
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write("# 本次运行剔除的广告入口（仅记录，不是黑名单，下次运行重新检测）\n")
            f.write("# 频道,URL,采样的%d次中落广告池次数\n" % samples)
            for it in dropped:
                f.write("%s,%s,%d\n" % (it["name"], it["url"], hits_map.get(it["url"], 0)))
            for it in kept:
                if it["_ad_hits"] > 0 and _norm_name(it["name"]) in clean_names:
                    f.write("%s,%s,%d  # 同频道已有干净源，跳过\n"
                            % (it["name"], it["url"], it["_ad_hits"]))
                elif fail_map.get(it["url"]):
                    f.write("%s,%s,0  # 采样时全部连不上（已通过连检，保留待下轮验证）\n"
                            % (it["name"], it["url"]))
        log("  剔除清单：%s" % p)

    return final


def export(passed, cfg, outdir):
    if not os.path.isdir(outdir):
        os.makedirs(outdir)
    seen, uniq = set(), []
    for x in passed:
        n = clean_name(x["name"])
        # 去掉画质/参数后缀：CCTV-14 HD -> CCTV-14，CCTV-9 (576i) -> CCTV-9
        n = re.sub(r'[\s\u0028\uFF08]*[\(（]?\s*(HD|SD|FHD|UHD|4K|8K|576I|720P|1080P)\s*[\)）]?\s*$',
                   '', n, flags=re.I).strip()
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
        raw = name.strip()
        # 排除 4K/8K 画质版本、CCTV-5+ 这类衍生频道、CCTV-4 Asia 这类多语种版本
        if "4K" in n or "8K" in n:
            return False
        if any(b in n for b in ("ASIA", "EUROPE", "AMERICA", "PLUS")):
            return False
        if "+" in n:
            return False
        # 所有含"卫视"的频道都留，但纯"卫视"这种无名条目不要
        if "卫视" in raw:
            return raw != "卫视"
        # CCTV-1 到 CCTV-17（兼容 CCTV5 / CCTV 5 / CCTV-5）
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
    passed = filter_ads(passed, cfg, outdir)      # 广告台实时检测与剔除
    txt, m3u, rep, n, ch = export(passed, cfg, outdir)
    log("导出完成：%d 条 / %d 个频道" % (n, ch))
    log("  %s" % txt)
    log("  %s" % m3u)
    log("  %s" % rep)


if __name__ == "__main__":
    main()
