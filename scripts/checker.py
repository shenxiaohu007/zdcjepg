#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
直播源检测核心（纯标准库，兼容 Python 3.8/3.9）
判定链路：m3u8 文本合法 -> 解析首个切片 -> 实抓字节 -> 判定可用
"""
import os
import re
import subprocess
import tempfile
import uuid
from urllib.parse import urljoin

UA = ("Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0 Mobile Safari/537.36")

FLAG = re.compile(
    r"\s*[\(\[]\s*(1080p|720p|540p|480p|360p|HD|SD|HEVC|Not\s*24/7|Geo-?blocked|"
    r"Multi-?stream|Timeout|Online|Test|Stream)\s*[\)\]]", re.I)

HAS_CURL = True


def _probe_curl():
    global HAS_CURL
    try:
        subprocess.run(["curl", "--version"], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=5, check=True)
        HAS_CURL = True
    except Exception:
        HAS_CURL = False


def _curl(url, out, timeout, rng=False):
    """返回 (code, ctype, size, final_url)；任何异常返回 code=0"""
    cmd = ["curl", "-sSL", "-m", str(timeout), "-A", UA, "--compressed",
           "-w", "%{http_code}|%{content_type}|%{size_download}|%{url_effective}",
           "-o", out]
    if rng:
        cmd += ["--range", "0-65535"]
    cmd.append(url)
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=int(timeout) + 6)
        meta = p.stdout.decode("utf-8", "ignore").strip().split("|")
        code = int(meta[0]) if len(meta) > 0 and meta[0].isdigit() else 0
        ctype = meta[1] if len(meta) > 1 else ""
        size = int(meta[2]) if len(meta) > 2 and meta[2].isdigit() else 0
        eff = meta[3] if len(meta) > 3 else url
        return code, ctype, size, eff
    except Exception:
        return 0, "", 0, url


def _py_get(url, timeout, rng=False):
    """curl 不存在时的降级实现（stdlib urllib）"""
    import urllib.request
    import urllib.error
    h = {"User-Agent": UA}
    if rng:
        h["Range"] = "bytes=0-65535"
    try:
        req = urllib.request.Request(url, headers=h)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            code = r.getcode()
            ctype = r.headers.get("Content-Type", "")
            body = r.read(65536 if rng else 200000)
            return code, ctype, len(body), r.geturl(), body
    except urllib.error.HTTPError as e:
        return e.code, "", 0, url, b""
    except Exception:
        return 0, "", 0, url, b""


def fetch(url, out_path, timeout, rng=False):
    if HAS_CURL:
        return _curl(url, out_path, timeout, rng=rng)
    code, ctype, size, eff, body = _py_get(url, timeout, rng=rng)
    try:
        with open(out_path, "wb") as f:
            f.write(body)
    except Exception:
        pass
    return code, ctype, size, eff


def check(item, timeout=8, deep=2):
    """返回 dict(ok, why)，item 需含 name/url"""
    tmp = os.path.join(tempfile.gettempdir(), "src_" + uuid.uuid4().hex + ".txt")
    try:
        code, ctype, size, eff = fetch(item["url"], tmp, timeout)
        if code not in (200, 206):
            return dict(item, ok=False, why="HTTP %s" % code)

        head = ""
        try:
            with open(tmp, "rb") as f:
                head = f.read(300).decode("utf-8", "ignore").lstrip("\ufeff")
        except Exception:
            pass

        if "#EXTM3U" not in head and "#EXTINF" not in head:
            if any(k in ctype for k in ("video", "mp4", "octet-stream", "mpegurl")):
                return dict(item, ok=True, why="直链 %s" % code, via="direct")
            return dict(item, ok=False, why="非 m3u8 (ctype=%s)" % ctype)

        seg = None
        try:
            with open(tmp, "r", encoding="utf-8", errors="ignore") as f:
                for line in f.read().splitlines():
                    s = line.strip()
                    if s and not s.startswith("#"):
                        seg = urljoin(eff, s)
                        break
        except Exception:
            return dict(item, ok=False, why="清单读取失败")
        if not seg:
            return dict(item, ok=False, why="清单内无切片")

        # 二级清单继续下钻，最多 deep 层，避免把 master playlist 当切片
        hops = 0
        while seg.split("?")[0].endswith(".m3u8") and hops < deep:
            c1, _, _, e1 = fetch(seg, tmp, timeout)
            if c1 not in (200, 206):
                break
            nxt = None
            try:
                with open(tmp, "r", encoding="utf-8", errors="ignore") as f:
                    for line in f.read().splitlines():
                        s = line.strip()
                        if s and not s.startswith("#"):
                            nxt = urljoin(e1, s)
                            break
            except Exception:
                pass
            if not nxt:
                break
            seg, hops = nxt, hops + 1

        c2, t2, s2, _ = fetch(seg, tmp, timeout, rng=True)
        if c2 not in (200, 206):
            return dict(item, ok=False, why="切片 HTTP %s" % c2)
        if "html" in t2 or "json" in t2:
            return dict(item, ok=False, why="切片返回非媒体 %s" % t2)
        if s2 < 100:
            return dict(item, ok=False, why="切片过小 %sB" % s2)
        return dict(item, ok=True, why="切片OK %s %s" % (c2, t2.split(";")[0]), via="hls")
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass


def clean_name(n):
    n = FLAG.sub("", n or "")
    n = re.sub(r"\s{2,}", " ", n).strip()
    return n or "未知频道"


def category(n):
    u = n.upper()
    if "CCTV" in u or u.startswith("CGTN") or u.startswith("CETV") or n.startswith("央视"):
        return "央视"
    if "卫视" in n:
        return "卫视"
    if any(k in n for k in ("凤凰", "翡翠", "TVB", "香港", "澳门", "台湾", "中天", "东森")):
        return "港澳台"
    if any(k in n for k in ("电影", "影院", "剧场", "体育", "纪实", "纪录", "动画",
                            "少儿", "音乐", "春晚", "CHC", "咪咕")):
        return "特色"
    return "地方台"


def parse_m3u_text(txt):
    """解析 m3u/txt 文本，返回 [{name, group, url}]"""
    items, name, group = [], "", ""
    for line in txt.splitlines():
        s = line.strip()
        if s.startswith("#EXTINF"):
            name = s.split(",")[-1].strip() if "," in s else "unknown"
            m = re.search(r'group-title="([^"]*)"', s)
            group = m.group(1) if m else ""
        elif s.startswith("#genre#"):
            group = s.split(",")[0].strip()
        elif s.startswith("http") and not s.startswith("#"):
            items.append({"name": name or group or "unknown", "group": group, "url": s})
            name = ""
    return items


_probe_curl()
