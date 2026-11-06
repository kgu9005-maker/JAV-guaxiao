# -*- coding: utf-8 -*-
"""
视频自动整理脚本 v4（JAV + FC2 + 欧美）
- 扫描目标文件夹内的视频文件
- JAV:    从文件名提取番号，抓取 javbus 元数据（多线程并发）
- FC2:    fc2ppvdb -> FC2官网 -> javten(FC2hub) -> javbus 无码区
- 欧美:   ThePornDB API(可选,需Key) -> IAFD（自动识别 Brazzers 日期式命名等）
- 生成 Kodi/Plex 兼容的 movie.nfo + 竖版 poster.jpg + fanart.jpg + 演员头像
- 按番号建文件夹，名字后追加分辨率（1080p / 2K / 4K / 8K，用 ffprobe 读取真实分辨率）
- 成功的番号文件夹移入「完成」，失败的移入「失败」

用法: python jav_organizer.py [目标文件夹]
"""

import os
import re
import sys
import time
import random
import hashlib
import shutil
import subprocess
import ssl
import threading
import urllib.request
import urllib.error
import gzip
import json
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor, as_completed

# ============ 可配置项 ============
PROXY = os.environ.get("JAV_PROXY", "").strip() or None
WORKERS = 4          # 同时处理的视频数（默认4；JAV_WORKERS 环境变量或命令行第2个参数可覆盖）
MAX_WORKERS = 12     # 并发硬上限，再高 javbus 基本必被限流
ENABLE_WESTERN = False   # 欧美片刮削开关（False = 只刮 JAV 和 FC2）
DELAY = 0.25         # 请求间隔基准（秒），实际会在 0.6~1.7 倍之间抖动
TIMEOUT = 20
RETRY = 2
MAX_SAMPLES = 4      # 每个视频下载的预览图数量（0 = 关闭）
MAX_ACTORS = 4       # 每部最多抓几个演员头像
MIN_IMAGE = 5000     # 小于此字节数的图片视为占位图，丢弃
BASE = "https://www.javbus.com"

# ---- 命名规则：番号后追加分辨率 ----
# 结果示例：ABP-123 1080p / ABP-123 2K / ABP-123 4K
# 默认开启；在 bat 里设 JAV_APPEND_RES=0 可关闭
APPEND_RESOLUTION = os.environ.get("JAV_APPEND_RES", "1").strip() != "0"
# 后缀写法，改这里即可换风格：
#   " {res}"     -> ABP-123 1080p      （默认）
#   " [{res}]"   -> ABP-123 [1080p]
#   "-{res}"     -> ABP-123-1080p
RES_TAG_FORMAT = " {res}"
# 分辨率识别不出来时用什么；留空 = 不加后缀（也可写 "未知"）
RES_UNKNOWN = ""

# 已知占位图黑名单（MD5）：FC2「注意」防病毒公告图（下架商品页的 og:image）
PLACEHOLDER_MD5 = {"c62f9de3327e43b852b39364677b5daa"}

# ---- javten (FC2hub) 过 Cloudflare 盾 ----
# 脚本访问 javten 被 CF 拦时（日志会提示），这样手动注入一次即可：
#   1. 用浏览器打开 https://javten.com ，完成人机验证进入页面
#   2. 按 F12 -> Network(网络) -> 刷新页面 -> 点第一个请求 -> Request Headers
#   3. 复制 Cookie: 后面的完整内容，粘贴到下面第一行引号里
#   4. 复制 User-Agent: 后面的完整内容，粘贴到第二行引号里
# 注意：cf_clearance 绑定 IP 和 UA，换网络/换浏览器后需重新复制
JAVTEN_COOKIE = ""
JAVTEN_UA = ""

# ---- ThePornDB（欧美 + FC2 通用源，可选但强烈推荐）----
# 免费注册 https://theporndb.net 后在 Settings -> API Keys 复制
# "Read Access Token"，填到下面引号里。填上后欧美片和 FC2 的命中率、
# 封面质量都会大幅提升（Brazzers/Blacked/Vixen 等站点全覆盖）。
# 不填也能用：欧美自动走 IAFD 免登录兜底。
TPDB_API_KEY = ""

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".wmv", ".ts", ".mov", ".rmvb",
              ".m4v", ".flv", ".rm", ".mpg", ".mpeg", ".webm", ".iso"}

JUNK = {"cd", "hd", "fhd", "uhd", "x", "h", "u", "ch", "c", "4k", "8k",
        "part", "vol", "v", "eng", "sub", "unc", "uc", "cn", "jp", "day",
        "cam", "hdr", "dv", "leak", "l", "k", "w", "t", "m"}

try:
    from PIL import Image  # noqa
    HAS_PIL = True
except Exception:
    HAS_PIL = False

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Accept-Encoding": "gzip",
    "Referer": "https://www.javbus.com/",
    "Cookie": "dv=1; existmag=all",
}
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE
_opener = None
_opener_lock = threading.Lock()
_print_lock = threading.Lock()

# 全局 HTTP 并发闸门：多路刮削时限制同时进行中的请求数，
# 避免瞬间把 javbus 打出几十个连接（那是被封 IP 的最快方式）
_http_sem = None
_http_sem_lock = threading.Lock()


def _get_http_sem():
    global _http_sem
    if _http_sem is None:
        with _http_sem_lock:
            if _http_sem is None:
                _http_sem = threading.BoundedSemaphore(
                    max(6, WORKERS * 2))
    return _http_sem


def _pause():
    """请求间隔：并发下加随机抖动，避免多线程同频共振触发限流"""
    time.sleep(DELAY * random.uniform(0.6, 1.7))


def _get_opener():
    """全局复用 opener（旧版每次请求都新建，纯浪费）"""
    global _opener
    if _opener is None:
        with _opener_lock:
            if _opener is None:
                handlers = [urllib.request.HTTPSHandler(context=_CTX)]
                if PROXY:
                    handlers.append(urllib.request.ProxyHandler(
                        {"http": PROXY, "https": PROXY}))
                _opener = urllib.request.build_opener(*handlers)
    return _opener


_IAFD_LOCK = threading.Lock()
_IAFD_LAST = [0.0]


def http_get(url, headers=None):
    """纯标准库 GET，返回 bytes 或 None；headers 可覆盖默认请求头。
    IAFD 对并发/高频极敏感：全局节流，间隔至少 2 秒。"""
    if "iafd.com" in url:
        with _IAFD_LOCK:
            wait = 6.0 - (time.time() - _IAFD_LAST[0])
            if wait > 0:
                time.sleep(wait)
            try:
                return _http_get_inner(url, headers)
            finally:
                _IAFD_LAST[0] = time.time()
    return _http_get_inner(url, headers)


def _http_get_inner(url, headers=None):
    opener = _get_opener()
    sem = _get_http_sem()
    for i in range(RETRY):
        try:
            req = urllib.request.Request(url, headers=headers or _HEADERS)
            with sem:
                with opener.open(req, timeout=TIMEOUT) as resp:
                    if resp.status != 200:
                        return None
                    data = resp.read()
                    enc = resp.headers.get("Content-Encoding")
            if enc == "gzip":
                try:
                    data = gzip.decompress(data)
                except Exception:
                    pass
            return data
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if i + 1 < RETRY:
                time.sleep(1.5 if e.code in (403, 429) else 1)
        except Exception:
            if i + 1 < RETRY:
                time.sleep(1)
    return None


def log(msg):
    with _print_lock:
        print(msg, flush=True)


# ---------- 番号提取 ----------

def extract_codes(filename):
    stem = os.path.splitext(filename)[0]
    cleaned = re.sub(r"(1080|720|2160)p|x(264|265|h264|h265)|hevc|aac|flac"
                     r"|无码|有码|破解|字幕|中字|高清|泄露|流出", " ", stem, flags=re.I)

    codes = []
    m = re.search(r"FC2[-_ ]?PPV[-_ ]?(\d{6,10})", cleaned, re.I)
    if m:
        return [f"FC2-PPV-{m.group(1)}"]
    m = re.search(r"(?<![A-Za-z])FC2[-_ ](\d{6,10})", cleaned, re.I)
    if m:
        return [f"FC2-PPV-{m.group(1)}"]

    for m in re.finditer(r"([A-Za-z]{2,8})[-_ ]?(\d{2,7})", cleaned):
        letters, digits = m.group(1), m.group(2)
        if letters.lower() in JUNK:
            continue
        codes.append(f"{letters.upper()}-{digits}")

    m = re.search(r"\b(\d{2,4})([A-Za-z]{2,8})[-_ ](\d{2,7})", cleaned)
    if m:
        codes.insert(0, f"{m.group(1)}{m.group(2).upper()}-{m.group(3)}")

    # ---- 无码厂商番号格式 ----
    # 1) 日期式: MMDDYY-XXX / MMDDYY_XXX（Caribbean / 1Pondo / 10Mu / Pacopacomama）
    #    例: 010115-773 / 021810-300-cd1 / 062012-053-carib-whole_hd1
    for m in re.finditer(r"(?<![\dA-Za-z])(\d{6})([-_])(\d{2,3})(?![\dA-Za-z])", cleaned):
        c = f"{m.group(1)}{m.group(2)}{m.group(3)}"
        codes.insert(0, c)
    # 2) HEYZO: heyzo_hd_1212_full -> HEYZO-1212
    m = re.search(r"heyzo[^0-9]{0,10}?(\d{3,5})", cleaned, re.I)
    if m:
        codes.append(f"HEYZO-{m.group(1)}")
    # 3) Tokyo Hot: n0630 / n0630-C -> n0630
    m = re.search(r"(?<![A-Za-z\d])n(\d{3,4})(?![\d])", cleaned, re.I)
    if m:
        codes.append(f"n{m.group(1)}")
    # 4) 纯字母+字母数字编号: MKBD-S29
    for m in re.finditer(r"\b([A-Za-z]{2,8})[-_ ]([A-Za-z]\d{2,4})\b", cleaned):
        if m.group(1).lower() not in JUNK:
            codes.append(f"{m.group(1).upper()}-{m.group(2)}")
    # 5) 字母数字混合厂商: S2M-055
    for m in re.finditer(r"\b([A-Za-z]+\d[A-Za-z\d]*)[-_ ](\d{2,4})\b", cleaned):
        if m.group(1).lower() not in JUNK:
            codes.append(f"{m.group(1).upper()}-{m.group(2)}")

    out = []
    for c in codes:
        variants = [c]
        mm = re.match(r"^(.+?)-(\d+)$", c)
        if mm and len(mm.group(2)) < 3:
            variants.append(f"{mm.group(1)}-{mm.group(2).zfill(3)}")
        for v in variants:
            if v not in out:
                out.append(v)
    return out[:3]


def extract_part(filename):
    m = re.search(r"[-_ .(]?(cd|part|disc|disk)[-_. ]?(\d{1,2})[]).]?\s*$",
                  os.path.splitext(filename)[0], re.I)
    return f"-CD{m.group(2)}" if m else ""


# ---------- 欧美片文件名识别 ----------

MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
          "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}

# 欧美常见厂牌关键词（文件名里出现即视为欧美片）
WESTERN_SITES = ("brazzers", "blacked", "blacksonblondes", "vixen", "tushy",
                 "deeper", "bangbros", "realitykings", "mofos", "naughtyamerica",
                 "teamskeet", "team skeet", "doghouse", "pornbox", "legalporno",
                 "dorcel", "evilangel", "new sensations", "wicked",
                 "digital playground", "girlsway", "mofoslab", "nubiles",
                 "bang.com", "brazzer", "porndoe", "mylf", "bangbros18")


def parse_western(filename):
    """欧美片识别。命中返回 info 字典，否则返回 None。
    规则：1) 文件名整体就是站点编号（SZ2828.mp4）
          2) Brazzers/NA 日期式命名 b.16.05.20.leah.gotti
          3) 文件名含欧美厂牌关键词
          4) 文件名带日期（括号内外均可）：ISO / 日月年 / 美式
    """
    stem = os.path.splitext(filename)[0]
    info = {"title": "", "date": "", "actress": "", "site_code": "", "year": ""}

    jav_prefix = ("smd", "smbd", "smdv", "cwp", "cwdv", "laf", "mkbd", "s2m",
                  "t28", "rhj", "crd", "sky", "dbd", "sm3d")

    # 0) 整个文件名就是一个站点编号（如 SZ2828.mp4）
    m = re.fullmatch(r"\s*([A-Za-z]{2,6}\d{2,6})\s*", stem)
    if m and not m.group(1).lower().startswith(jav_prefix):
        info["site_code"] = m.group(1).upper()
        return info

    # 1) 站点日期式: 站名前缀(任意长度, 可含 .e26 集数) + YY.MM.DD + 描述
    #    如 tushy.19.05.01.emily.cutie / b.16.05.20.leah.gotti / 5kporn.e26.19.05.13.lena
    m = re.match(r"^([A-Za-z0-9][A-Za-z0-9\-]{0,20}?)(?:\.e\d{1,3})?\.(\d{2})\.(\d{1,2})\.(\d{1,2})\.(.+)$",
                 stem)
    if m and not m.group(1).lower().startswith(jav_prefix):
        yy, a, b, rest = int(m.group(2)), int(m.group(3)), int(m.group(4)), m.group(5)
        if a > 12 and b <= 12:               # 少数站用 DD.MM
            a, b = b, a
        year = (1900 if yy >= 70 else 2000) + yy
        name = re.sub(r"\b(2160|1080|720|480)p\b|\b[458]k\b|\bfh?d\b|\buhd\b"
                      r"|\bhevc\b|\bx(264|265)\b", " ", rest, flags=re.I)
        name = re.sub(r"[-_.\s]+", " ", name).strip()
        info.update(date=f"{year}-{a:02d}-{b:02d}", year=str(year),
                    actress=name, title=name,
                    site=m.group(1).lower())
        return info

    low = stem.lower()
    site_hit = any(s in low for s in WESTERN_SITES)

    # 2) 日期（括号内或裸露）：ISO YYYY-MM-DD / DD-MM-YYYY / 美式 MM-DD-YY
    date_m = (re.search(r"[\(\[](\d{1,4})[.\-/](\d{1,2})[.\-/](\d{1,4})[\)\]]", stem)
              or re.search(r"(?<![\dA-Za-z])(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})(?!\d)",
                           stem)
              or re.search(r"(?<![\dA-Za-z])(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{2,4})(?!\d)",
                           stem))
    if not site_hit and not date_m:
        # 3) 裸站点编号（SZ2446 / GIO1403 / IV171 等夹在标题里）
        m = re.search(r"\b([A-Za-z]{2,6}\d{2,6})\b", stem)
        if m and not m.group(1).lower().startswith(jav_prefix):
            info["site_code"] = m.group(1).upper()
            title = re.sub(re.escape(info["site_code"]), " ", stem, flags=re.I)
            info["title"] = re.sub(r"[-_.\s]+", " ", title).strip()
            if info["title"] or info["site_code"]:
                return info
        # 4) 纯拉丁字母标题（无番号、无 CJK、至少两个英文词）-> IAFD 标题搜索
        if (not re.search(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]", stem)
                and len(stem) >= 12
                and len(re.findall(r"[A-Za-z]{3,}", stem)) >= 2):
            info["title"] = re.sub(r"[-_.\s]+", " ", stem).strip()
            if info["title"]:
                return info
        return None

    if date_m:
        a, b, c = int(date_m.group(1)), int(date_m.group(2)), int(date_m.group(3))
        if len(date_m.group(1)) == 4:            # ISO: YYYY-MM-DD
            info["date"] = f"{a}-{b:02d}-{c:02d}"
        elif c > 31:                             # 末段 4 位年: DD-MM-YYYY / MM-DD-YYYY
            mon, d = (b, a) if a > 12 else (a, b)
            info["date"] = f"{c}-{mon:02d}-{d:02d}"
        elif a <= 12 and b <= 12:                # 末段两位年: 美式 MM-DD-YY
            year = (1900 if c >= 70 else 2000) + c
            info["date"] = f"{year}-{a:02d}-{b:02d}"
        else:                                    # 月份非法，不是日期，忽略
            date_m = None
        if date_m:
            info["year"] = info["date"][:4]

    # 站点内部编号：无连字符的字母+数字（如 SZ2828），区别于日式番号
    m = re.search(r"\b([A-Za-z]{2,6}\d{2,6})\b", stem)
    if m and not m.group(1).lower().startswith(jav_prefix):
        info["site_code"] = m.group(1).upper()

    title = re.sub(r"\b(1080|720|2160|480)p\b|x(264|265)|h\.?264|h\.?265|hevc|hdr",
                   " ", stem, flags=re.I)
    title = re.sub(r"[\(\[].*?[\)\]]", " ", title)          # 去掉括号段（日期等）
    if info["site_code"]:
        title = re.sub(re.escape(info["site_code"]), " ", title, flags=re.I)
    if date_m:
        title = title.replace(date_m.group(0), " ")
    info["title"] = re.sub(r"[-_.\s]+", " ", title).strip()
    if not (info["actress"] or info["site_code"] or info["title"]):
        return None
    return info


# ---------- 网页解析 ----------

def strip_tags(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", s)).strip()


def abs_url(u):
    if u and u.startswith("/"):
        return BASE + u
    return u


def parse_detail(html):
    d = {}
    m = re.search(r"<title>(.*?)\s*[-–]\s*JavBus", html, re.S)
    if m:
        d["title"] = strip_tags(m.group(1))
    m = re.search(r'bigImage"\s+href="([^"]+)"', html)
    if m:
        d["cover"] = abs_url(m.group(1))

    for chunk in re.findall(r"<p[^>]*>(.*?)</p>", html, re.S):
        m = re.match(r'\s*<span class="header">([^:<]+):?</span>(.*)', chunk, re.S)
        if not m:
            continue
        label, text = m.group(1).strip(), strip_tags(m.group(2))
        if any(k in label for k in ("識別碼", "番号", "识别码", "ID")):
            d["id"] = text
        elif any(k in label for k in ("發行日期", "发行日期", "発売日", "Released", "Date")):
            d["date"] = text
        elif any(k in label for k in ("長度", "长度", "Length", "Duration")):
            digits = re.search(r"\d+", text)
            d["runtime"] = digits.group(0) if digits else ""
        elif any(k in label for k in ("導演", "导演", "Director")):
            d["director"] = text
        elif any(k in label for k in ("製作", "制作", "Studio", "製片")):
            d["studio"] = text
        elif any(k in label for k in ("發行", "发行", "Label", "Publisher")):
            d["label"] = text

    pairs = []
    for href, name in re.findall(r'href="([^"]*/star/[^"]*)"[^>]*>\s*([^<]+?)\s*<', html):
        name = strip_tags(name)
        if not name or re.fullmatch(r"[\s\-–—·.]+", name):
            continue
        if (href, name) not in pairs:
            pairs.append((href, name))
    d["actor_pairs"] = pairs[:MAX_ACTORS]
    d["actors"] = [n for _, n in pairs]
    d["genres"] = list(dict.fromkeys(
        re.findall(r'href="[^"]*/genre/[^"]*"[^>]*>\s*([^<]+?)\s*<', html)))
    d["samples"] = [abs_url(s) for s in
                    re.findall(r'class="sample-box"[^>]*href="([^"]+)"', html)[:MAX_SAMPLES]]
    return d


def fetch_actor_avatar(page_url):
    """抓演员头像：只认 /pics/actress/ 站内图，排除 Discuz 默认头像"""
    raw = http_get(page_url)
    if not raw:
        return None
    text = raw.decode("utf-8", "ignore")
    for m in re.finditer(r'<img[^>]+src="([^"]+)"', text):
        u = m.group(1)
        if "/pics/actress/" not in u:
            continue
        if "default" in u or "avatar" in u:
            continue
        return abs_url(u)
    return None


def search_first_result(code, uncensored=False):
    """返回 (详情页URL, 缩略图, 搜索页兜底meta)。详情页被 403 时用 meta 兜底"""
    path = f"/uncensored/search/{quote(code)}&type=1" if uncensored \
        else f"/search/{quote(code)}&type=1"
    raw = http_get(BASE + path)
    if not raw:
        return None, None, None
    text = raw.decode("utf-8", "ignore")
    m = re.search(r'class="movie-box"\s+href="([^"]+)"([\s\S]{0,900}?)</a>', text)
    if not m:
        return None, None, None
    url, block = m.group(1), m.group(2)
    img = re.search(r'<img src="([^"]+)"', block)
    thumb = abs_url(img.group(1)) if img else None
    dates = re.findall(r"<date>([^<]*)</date>", block)
    title_m = re.search(r'<img[^>]*title="([^"]*)"', block)
    item = None
    if dates:
        item = {"title": strip_tags(title_m.group(1)) if title_m else "",
                "id": dates[0].strip(),
                "date": dates[1].strip().replace("-", "/") if len(dates) > 1 else "",
                "cover": thumb, "actors": [], "actor_pairs": [],
                "genres": [], "samples": [], "partial": True}
    return url, thumb, item


def scrape(codes, western=None):
    """欧美 -> FC2 -> javbus 直连详情页 -> 有码搜索 -> 无码搜索；返回 (meta, 番号) 或 None"""
    if isinstance(codes, str):
        codes = [codes]
    if western:                          # 欧美片走专属源，不再拿字母数字去 javbus 瞎试
        return scrape_western(western)
    for cand in codes:
        if cand.upper().startswith("FC2-PPV-"):
            r = scrape_fc2(cand)
            if r:
                return r
            continue                    # FC2 专用源失败后不无限重试 javbus

        # 无码专属格式（日期式/n###/HEYZO）直接走无码区，省两次白请求
        date_style = bool(re.match(r"^\d{6}[-_]\d{2,3}$", cand))
        unc_only = date_style or bool(re.match(r"^[Nn]\d{3,4}$", cand)) \
            or cand.upper().startswith("HEYZO-")
        # 搜索变体：日期式两种分隔符都试；HEYZO 补纯数字
        variants = [cand]
        if date_style:
            variants.append(cand.replace("-", "_") if "-" in cand
                            else cand.replace("_", "-"))
        if cand.upper().startswith("HEYZO-"):
            variants.append(cand.split("-", 1)[1])

        meta, thumb = None, None
        if not unc_only:
            raw = http_get(f"{BASE}/{cand}")
            if raw:
                meta = parse_detail(raw.decode("utf-8", "ignore"))
        if not (meta and meta.get("id")) and not unc_only:
            _pause()
            url, thumb, item = search_first_result(cand, uncensored=False)
            if url:
                raw = http_get(url)
                if raw:
                    meta = parse_detail(raw.decode("utf-8", "ignore"))
            if not (meta and meta.get("id")) and item:
                meta = item          # 详情页被 403 等拦截 -> 搜索页兜底
        if not (meta and meta.get("id")):
            # 无码区搜索（有码片也兜底一次，覆盖 DBD/MKBD/S2M 等）
            for v in variants:
                _pause()
                url, th2, item2 = search_first_result(v, uncensored=True)
                m2 = None
                if url:
                    raw = http_get(url)
                    if raw:
                        m2 = parse_detail(raw.decode("utf-8", "ignore"))
                    if m2 and m2.get("id"):
                        meta, thumb = m2, th2
                        break
                if item2 and not (m2 and m2.get("id")):
                    meta, thumb = item2, th2
                    break
        if meta and meta.get("id"):
            if not thumb and not HAS_PIL:
                # 只有没有 Pillow 时才需要列表页竖版缩略图做海报兜底
                _, thumb, _ = search_first_result(cand)
            if thumb:
                meta["poster_thumb"] = thumb
            pairs = meta.get("actor_pairs", [])
            avatars = {}
            if pairs:
                with ThreadPoolExecutor(max_workers=min(4, len(pairs))) as ex:
                    futs = {ex.submit(fetch_actor_avatar, p): p for p, _ in pairs}
                    for f in as_completed(futs):
                        av = f.result()
                        if av:
                            avatars[futs[f]] = av
            meta["actor_avatars"] = avatars
            return meta, meta["id"]
        _pause()
    return None


# ---------- FC2 专用数据源 ----------

def _is_cf_blocked(text):
    return "Just a moment" in text or "cf-browser-verification" in text


def _meta_from_og(text, code):
    """通用 og 标签解析，供 FC2 源使用"""
    og = dict(re.findall(
        r'<meta[^>]+property=["\']og:(\w+)["\'][^>]+content=["\']([^"\']*)["\']', text))
    title = strip_tags(og.get("title", "")).strip()
    if not title:
        return None
    m = re.search(r"(\d{4})[/\-.年](\d{1,2})[/\-.月](\d{1,2})", text)
    d = {"title": title, "id": code,
         "date": f"{m.group(1)}/{m.group(2)}/{m.group(3)}" if m else "",
         "cover": og.get("image", ""), "actors": [], "genres": []}
    return d


def scrape_fc2_official(num):
    """FC2 官网（已下架的片子会 404）"""
    raw = http_get(f"https://adult.contents.fc2.com/article/{num}/")
    if not raw:
        return None
    text = raw.decode("utf-8", "ignore")
    if _is_cf_blocked(text):
        log("  ⚠ FC2 官网被 Cloudflare 拦截，建议设置 JAV_PROXY")
        return None
    if "couldn't find" in text or "見つかりません" in text or "商品が見つかりません" in text:
        return None
    # 已下架/售罄的商品页仍存在，但正文是「注意」防病毒公告，og:image 是统一公告图
    if "正規でないルート" in text or "ウイルスに感染したり" in text:
        return None
    meta = _meta_from_og(text, f"FC2-PPV-{num}")
    if not meta:
        return None
    m = re.search(r"販売日[^0-9]*(\d{4}[/\-.]\d{1,2}[/\-.]\d{1,2})", text)
    if m:
        meta["date"] = m.group(1)
    m = re.search(r"販売者", text)
    if m:
        seg = strip_tags(text[m.end():m.end() + 300])
        seg = seg.strip(" :：\t ")
        if seg:
            meta["studio"] = seg.split(" ")[0][:60]
    return meta


def scrape_fc2_javten(num):
    """javten.com（原 FC2hub，FC2 元数据最全；有 Cloudflare 盾）"""
    hdr = None
    if JAVTEN_COOKIE.strip() and JAVTEN_UA.strip():
        hdr = dict(_HEADERS)
        hdr["Cookie"] = JAVTEN_COOKIE.strip()
        hdr["User-Agent"] = JAVTEN_UA.strip()
    raw = http_get(f"https://javten.com/pages/{num}", headers=hdr)
    if not raw:
        return None
    text = raw.decode("utf-8", "ignore")
    if _is_cf_blocked(text):
        if hdr:
            log("  ⚠ javten 仍被 Cloudflare 拦截，cf_clearance 可能过期，请重新从浏览器复制")
        else:
            log("  ⚠ javten 被 Cloudflare 拦截。解决：浏览器打开 javten.com 过验证后，"
                "按脚本顶部注释把 Cookie 和 User-Agent 填入 JAVTEN_COOKIE/JAVTEN_UA")
        return None
    meta = _meta_from_og(text, f"FC2-PPV-{num}")
    if not meta:
        return None
    m = re.search(r"(?:販売日|発売日|Release[d]?[ _-]?date)[:：\s]*([\d/\-.年月日]+)",
                  text, re.I)
    if m:
        meta["date"] = m.group(1).strip("日 ")
    # 演员名（javten 列在 actress 区块）
    m = re.search(r'actress[^>]*>([\s\S]{0,2000}?)</(?:div|ul|section)>', text, re.I)
    if m:
        names = [strip_tags(x) for x in re.findall(r'>([^<>]{2,30})<', m.group(1))]
        meta["actors"] = [n for n in names if n][:MAX_ACTORS]
    return meta


def scrape_fc2_fc2ppvdb(num):
    """fc2ppvdb.com（无 Cloudflare 盾，FC2 元数据最全的公开库，优先用）"""
    raw = http_get(f"https://fc2ppvdb.com/articles/{num}")
    if not raw:
        return None
    text = raw.decode("utf-8", "ignore")
    if _is_cf_blocked(text):
        log("  ⚠ fc2ppvdb 被 Cloudflare 拦截，建议设置 JAV_PROXY")
        return None
    meta = _meta_from_og(text, f"FC2-PPV-{num}")
    if not meta:
        return None
    m = re.search(r"(?:発売日|販売日|発売)[:：\s]*([\d/\-.年月日]+)", text)
    if m:
        meta["date"] = m.group(1).strip("日 ")
    acts = [strip_tags(n) for n in
            re.findall(r'href="[^"]*/actresses/[^"]*"[^>]*>\s*([^<]+?)\s*<', text)]
    meta["actors"] = list(dict.fromkeys(acts))[:MAX_ACTORS]
    tags = [strip_tags(g) for g in
            re.findall(r'href="[^"]*/tags/[^"]*"[^>]*>\s*([^<]+?)\s*<', text)]
    meta["genres"] = list(dict.fromkeys(tags))[:15]
    return meta


def scrape_fc2_njavtv(num):
    """FC2 兜底源：njavtv（og:image 封面 + 标题 + 発売日/时间，无 CF 盾）"""
    raw = http_get(f"https://njavtv.com/dm31/ja/fc2-ppv-{num}")
    if not raw:
        return None
    t = raw.decode("utf-8", "ignore")
    title_m = (re.search(r'property="og:title"[^>]+content="([^"]+)"', t)
               or re.search(r"<title>([^<]+)</title>", t))
    title = ""
    if title_m:
        title = title_m.group(1)
        # 页面标题常带 "FC2-PPV-xxxx" 前缀和站点后缀，清理
        title = re.sub(r"^FC2-PPV-\d+\s*", "", title).strip(" -|")
    if not title or len(t) < 5000:
        return None
    cover = ""
    oc = re.search(r'property="og:image"[^>]+content="([^"]+)"', t)
    if oc:
        cover = oc.group(1)
    date = ""
    md = re.search(r"発売日[^\d]{0,20}(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})", t)
    if md:
        date = f"{md.group(1)}/{int(md.group(2)):02d}/{int(md.group(3)):02d}"
    runtime = ""
    rt = re.search(r"時間\s*(\d{1,2}):(\d{2}):(\d{2})", t)
    if rt:
        runtime = str(int(rt.group(1)) * 60 + int(rt.group(2)))
    return {"title": title, "id": f"FC2-PPV-{num}", "date": date,
            "cover": cover, "runtime": runtime, "director": "",
            "studio": "FC2", "actors": [], "actor_thumbs": {},
            "genres": ["素人"], "samples": []}


def _clean_123av_title(t):
    t = re.sub(r"^FC2-PPV-\d+\s*—?\s*", "", t)
    t = re.sub(r"\s*—\s*123AV\s*$", "", t)
    return t.strip(" -—").strip()


def scrape_fc2_123av(num):
    """FC2 最终兜底：123av 直连（可拿到 icdn 真封面），失败再退 jina 纯文本"""
    code = f"fc2-ppv-{num}"
    raw = http_get(f"https://123av.com/ja/v/{code}")
    t = raw.decode("utf-8", "ignore") if raw else ""
    cover = ""
    if t:
        mc = re.search(r"icdn\.123av\.me/img2/[^\"'\s\\]*cover\.jpg[^\"'\s\\]*", t)
        if mc:
            cover = "https://" + mc.group(0)
        mo = re.search(r'property="og:title" content="([^"]+)"', t)
        if not mo:
            mo = re.search(r"<title>([^<]+)", t)
        title = _clean_123av_title(mo.group(1)) if mo else ""
        date = ""
        md = re.search(r"発売日(?:\s|<[^>]*>|&nbsp;)*(20\d{2})-(\d{2})-(\d{2})", t)
        if md:
            date = f"{md.group(1)}/{md.group(2)}/{md.group(3)}"
        runtime = ""
        rt = re.search(r"再生時間(?:\s|<[^>]*>|&nbsp;)*(\d{1,3}):(\d{2})", t)
        if rt:
            runtime = rt.group(1)
        if title or cover:
            return {"title": title, "id": f"FC2-PPV-{num}", "date": date,
                    "cover": cover, "runtime": runtime, "director": "",
                    "studio": "FC2", "actors": [], "actor_thumbs": {},
                    "genres": ["素人"], "samples": []}
    # 直连失败（CF 盾等）→ jina 纯文本兜底（无封面）
    raw = http_get(f"https://r.jina.ai/https://123av.com/ja/v/{code}")
    if not raw:
        return None
    t = raw.decode("utf-8", "ignore")
    if "404" in t[:200] or "No query results" in t:
        return None
    mt = re.search(r"Title:\s*(FC2-PPV-\d+[^\n]*)", t)
    if not mt:
        return None
    title = _clean_123av_title(mt.group(1))
    date = ""
    md = re.search(r"発売日\s*(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})", t)
    if md:
        date = f"{md.group(1)}/{int(md.group(2)):02d}/{int(md.group(3)):02d}"
    runtime = ""
    rt = re.search(r"時間\s*(\d{1,2}):(\d{2}):(\d{2})", t)
    if rt:
        runtime = str(int(rt.group(1)) * 60 + int(rt.group(2)))
    return {"title": title, "id": f"FC2-PPV-{num}", "date": date,
            "cover": "", "runtime": runtime, "director": "",
            "studio": "FC2", "actors": [], "actor_thumbs": {},
            "genres": ["素人"], "samples": []}


def scrape_fc2(code):
    """FC2 番号专用链路：fc2ppvdb -> 官网 -> javten(FC2hub) -> TPDB -> javbus 无码区"""
    m = re.search(r"(\d+)$", code)
    num = m.group(1) if m else code
    chain = [(scrape_fc2_fc2ppvdb, "fc2ppvdb"),
             (scrape_fc2_official, "fc2_official"),
             (scrape_fc2_javten, "fc2_javten"),
             (scrape_fc2_njavtv, "fc2_njavtv"),
             (scrape_fc2_123av, "fc2_123av")]
    for fn, src in chain:
        _pause()
        try:
            meta = fn(num)
        except Exception as e:
            log(f"  ⚠ {fn.__name__} 异常: {e}")
            meta = None
        if meta:
            log(f"  ✓ FC2 命中 [{src}]: {meta.get('title', '')[:40]}")
            return meta, meta["id"]
    if TPDB_API_KEY.strip():             # TPDB 也收录了大量 FC2
        _pause()
        try:
            meta = scrape_tpdb({"site_code": code, "actress": "", "title": "", "year": ""})
        except Exception as e:
            log(f"  ⚠ TPDB 异常: {e}")
            meta = None
        if meta:
            log(f"  ✓ FC2 命中 [TPDB]: {meta.get('title', '')[:40]}")
            return meta, meta["id"]
    # 最后兜底：javbus 无码区搜索
    url, _thumb, _item = search_first_result(code, uncensored=True)
    if url:
        raw = http_get(url)
        if raw:
            meta = parse_detail(raw.decode("utf-8", "ignore"))
            if meta.get("id"):
                return meta, meta["id"]
    return None


# ---------- 欧美专用数据源 ----------

def _western_id(info, meta):
    """欧美片文件夹/文件命名：站点编号 > 演员名+日期 > 标题"""
    if info.get("site_code"):
        return info["site_code"]
    actors = meta.get("actors") or []
    name = (actors[0] if actors else "").strip()
    # 文件名里写了演员名时优先用它（防止匹配到的条目主演是别人，张冠李戴）
    fn_actress = (info.get("actress") or "").strip()
    if fn_actress:
        low = fn_actress.lower()
        if any(a.strip().lower() == low for a in actors):
            name = fn_actress          # 条目演员表里有她，用文件名写法
        elif not actors:
            name = fn_actress
        else:
            name = fn_actress          # 条目里没有她 = 疑似错配，文件名优先
    date = (meta.get("date") or info.get("date", "")).replace("/", "-")[:10]
    if name and date:
        base = f"{name} {date}"
    elif name:
        base = name
    elif date:
        base = date
    else:
        base = info.get("title") or "Western"
    return re.sub(r'[\\/:*?"<>|]', "_", base).strip()[:60] or "Western"


def scrape_tpdb(info):
    """ThePornDB（需 TPDB_API_KEY），欧美 + FC2 通用"""
    q = info.get("site_code") or info.get("actress") or info.get("title", "")
    if not q:
        return None
    hdr = {"Authorization": "Bearer " + TPDB_API_KEY.strip(),
           "Accept": "application/json", "User-Agent": _HEADERS["User-Agent"]}
    raw = http_get(f"https://api.theporndb.net/scenes?searchTerm={quote(q)}", headers=hdr)
    if not raw:
        return None
    try:
        items = json.loads(raw.decode("utf-8", "ignore")).get("data") or []
    except Exception:
        return None
    best, best_s = None, -1
    for it in items[:8]:
        a = it.get("attributes") or {}
        s = 0
        if info.get("year") and (a.get("date") or "")[:4] == info["year"]:
            s += 3
        t = (a.get("title") or "").lower()
        if info.get("actress"):
            for w in info["actress"].lower().split():
                if w in t:
                    s += 1
        if s > best_s:
            best, best_s = a, s
    if best is None:
        return None
    a = best
    posters = a.get("posters") or {}
    img = a.get("image") or posters.get("large") or posters.get("URL") or ""
    site = a["site"].get("name", "") if isinstance(a.get("site"), dict) else ""
    actors = []
    for p in a.get("performers") or []:
        if isinstance(p, dict) and p.get("name"):
            actors.append(p["name"])
        elif isinstance(p, str):
            actors.append(p)
    actors = actors or a.get("performer_names") or []
    return {"title": a.get("title") or info.get("title") or q,
            "id": a.get("title") or q, "date": a.get("date") or "",
            "cover": img, "studio": site, "runtime": "",
            "actors": actors[:MAX_ACTORS], "genres": []}


def _iafd_rows(html):
    out = []
    for row in re.finditer(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        r = row.group(1)
        m = re.search(r'href="([^"]*title\.rme/id=[^"]*)"[^>]*>(.*?)</a>', r, re.S)
        if not m:
            continue
        year = ""
        for c in re.findall(r"<td[^>]*>(.*?)</td>", r, re.S):
            ym = re.search(r"(19|20)\d{2}", c)
            if ym:
                year = ym.group(0)
                break
        out.append({"href": m.group(1), "title": strip_tags(m.group(2)),
                    "year": year, "row": r})
    return out


def _iafd_rank(info, item):
    s = 0
    text = (item["title"] + " " + re.sub(r"<[^>]+>", " ", item["row"])).lower()
    if info.get("year") and item["year"] == info["year"]:
        s += 3
    if info.get("site_code") and info["site_code"].lower() in text:
        s += 3
    if info.get("site") and len(info["site"]) >= 3 and info["site"] in text:
        s += 4                          # 文件名站名前缀与 IAFD 厂牌吻合
    if info.get("actress"):
        for w in info["actress"].lower().split():
            if len(w) > 2 and w in text:
                s += 1
    return s


def _iafd_detail(url):
    if not url.startswith("http"):
        url = "https://www.iafd.com/" + url.lstrip("/")
    raw = http_get(url, headers=dict(_HEADERS, Referer="https://www.iafd.com/"))
    if not raw:
        return None
    text = raw.decode("utf-8", "ignore")
    m = re.search(r"<h1>(.*?)</h1>", text, re.S)
    title = strip_tags(m.group(1)) if m else ""
    if not title:
        return None

    bio = {}
    for m in re.finditer(
            r'bioheading">\s*([^<]+?)\s*</p>\s*<p class="biodata">(.*?)</p>',
            text, re.S):
        bio[m.group(1).strip()] = strip_tags(m.group(2))

    date = ""
    md = re.search(r"([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})",
                   bio.get("Release Date", ""))
    if md and md.group(1)[:3].lower() in MONTHS:
        date = (f"{md.group(3)}/{MONTHS[md.group(1)[:3].lower()]:02d}/"
                f"{int(md.group(2)):02d}")

    cover = ""
    for m in re.finditer(r"<img[^>]+>", text):
        if "frontcover" in m.group(0):
            ms = re.search(r'src="([^"]+)"', m.group(0))
            if ms:
                cover = ms.group(1)
            break

    actors, thumbs = [], {}
    for box in re.findall(r"castbox.*?</div>", text, re.S):
        for m in re.finditer(
                r'href="/person\.rme[^"]*"[^>]*>\s*(?:<img[^>]*src="([^"]+)"[^>]*>)?'
                r"\s*(?:<br\s*/?>\s*)?([^<]+?)\s*</a>", box):
            name, th = strip_tags(m.group(2)), m.group(1)
            if not name or name in thumbs:
                continue
            # 只保留女演员（IAFD 头像 URL 里 _m_=男演员，跳过）
            if th and "_m_" in th:
                continue
            actors.append(name)
            if th:
                thumbs[name] = th
    actors = actors[:MAX_ACTORS]

    director = bio.get("Director", "")
    return {"title": title, "id": title, "date": date, "cover": cover,
            "runtime": re.sub(r"\D", "", bio.get("Minutes", "")) or "",
            "director": "" if director.lower() == "no data" else director,
            "studio": bio.get("Studio") or bio.get("Distributor") or "",
            "actors": actors,
            "actor_thumbs": {k: thumbs[k] for k in actors if k in thumbs},
            "genres": [], "samples": []}


def _date_matches(want, got):
    """文件名日期与详情页日期是否同一天（容错分隔符差异）"""
    if not want:
        return False
    norm = lambda s: re.sub(r"\D", "", s)[:8]
    return norm(want) == norm(got) if got else False


def scrape_iafd(info):
    """IAFD 免登录欧美库：按 站点编号 / 演员 / 标题 依次搜索"""
    queries = []
    for k in ("site_code", "actress"):
        if info.get(k):
            queries.append(info[k])
    if info.get("title"):
        ws = info["title"].split()
        if len(ws) >= 2:
            queries.append(" ".join(ws[:2]))       # 标题前两词（多为演员名）
        t = " ".join(ws[:7])
        if t:
            queries.append(t)
    if info.get("actress"):
        ws = info["actress"].split()
        if len(ws) >= 2:
            queries.append(" ".join(ws[:2]))
    seen = set()
    for q in queries:
        if q.lower() in seen:
            continue
        seen.add(q.lower())
        raw = None
        for attempt in range(3):           # IAFD 限流：0.25s → 10s → 25s 退避重试
            time.sleep((DELAY, 10, 25)[attempt])
            raw = http_get("https://www.iafd.com/results.asp?searchtype=comprehensive"
                           f"&searchstring={quote(q)}")
            if raw:
                rows = _iafd_rows(raw.decode("utf-8", "ignore"))
                if rows:
                    break
                if attempt < 2:
                    log("  ⚠ IAFD 搜索无结果，可能限流，稍后重试...")
        if not raw:
            continue
        rows = _iafd_rows(raw.decode("utf-8", "ignore"))
        if not rows:
            continue
        rows.sort(key=lambda r: _iafd_rank(info, r), reverse=True)
        if _iafd_rank(info, rows[0]) <= 0 and len(rows) > 1:
            continue                       # 结果与文件名毫无关联，换查询词
        # 有明确站名线索但结果里没有任何厂牌吻合的行 -> 直接换查询词（省请求）
        site = (info.get("site") or "").lower()
        if site and len(site) >= 3 and not any(
                site in (r["title"] + " " + r["row"]).lower() for r in rows):
            continue
        fallback = None
        for cand in rows[:5]:              # 逐个核对发行日期，日期吻合优先
            _pause()
            meta = _iafd_detail(cand["href"])
            if not meta:
                continue
            if _date_matches(info.get("date"), meta.get("date")):
                return meta
            if fallback is None:
                fallback = meta
        if fallback:
            if info.get("date") and not _date_matches(info["date"], fallback.get("date")):
                site = (info.get("site") or "").lower()
                if site and len(site) >= 3:
                    ftext = ((fallback.get("studio") or "") + " "
                             + (fallback.get("title") or "")).lower()
                    if site not in ftext:
                        log("  ⚠ 日期与厂牌均不符，放弃兜底结果（避免张冠李戴）")
                        continue        # 换下一个查询词
                log("  ⚠ 未找到发行日期完全吻合的条目，已选同演员/同年最相近结果"
                    "（注册 theporndb.net 填入 TPDB_API_KEY 可精确匹配）")
            return fallback
    return None


SITE_ALIAS = {
    "b": ["blacked", "brazzers", "babes"],
    "t": ["tushy", "twistys"],
    "v": ["vixen"],
    "bw": ["blackedraw"],
    "p": ["private", "pornpros"],
    "w": ["wicked"],
}


def scrape_iafd_person(info):
    """IAFD 演员作品表兜底：按 演员+厂牌(站名前缀) 精确定位，避免综合搜索列表不全"""
    site = (info.get("site") or "").lower()
    actress = (info.get("actress") or "").lower()
    if not actress and info.get("title"):
        actress = info["title"].lower()    # 标题前两词常为演员名
    if not site or not actress:
        return None
    words = re.findall(r"[a-z]+", actress)
    if len(words) < 2:
        return None
    if len(site) >= 3:
        sites = [(site, True)]             # 明确站名：允许厂牌吻合的兜底
    else:
        # 单字母/缩写前缀：常见站名别名，必须日期精确吻合才接受
        sites = [(a, False) for a in SITE_ALIAS.get(site, [])]
    first, last = words[0], words[1]
    for site_name, allow_fallback in sites:
        meta = _iafd_person_site(first, last, site_name, info, allow_fallback)
        if meta:
            return meta
    return None


def _iafd_person_site(first, last, site, info, allow_fallback):
    url = (f"https://www.iafd.com/person.rme/perfid={first}{last}"
           f"/gender=f/{first}-{last}.htm")
    _pause()
    raw = http_get(url, headers=dict(_HEADERS, Referer="https://www.iafd.com/"))
    if not raw:
        return None
    text = raw.decode("utf-8", "ignore")
    if "<h1>" not in text or "couldn't find" in text.lower():
        return None
    cands = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", text, re.S):
        if site not in row.lower():
            continue
        m = re.search(r'href="([^"]*title\.rme/id=[^"]+)"', row)
        if m:
            cands.append(m.group(1))
    if not cands:
        return None
    log(f"  演员作品表命中 {len(cands)} 条 {site} 条目，核对日期...")
    fallback = None
    for href in cands[:4]:
        _pause()
        meta = _iafd_detail(href)
        if not meta:
            continue
        if _date_matches(info.get("date"), meta.get("date")):
            return meta
        if fallback is None:
            fallback = meta
    if fallback and allow_fallback:
        log("  ⚠ 厂牌吻合但日期不完全一致，已选该厂牌最相近条目")
        return fallback
    return None


def scrape_western(info):
    """欧美片链路：TPDB(可选) -> IAFD -> IAFD演员作品表"""
    log("  欧美源: " + ("TPDB -> " if TPDB_API_KEY.strip() else "") + "IAFD")
    if TPDB_API_KEY.strip():
        _pause()
        try:
            meta = scrape_tpdb(info)
        except Exception as e:
            log(f"  ⚠ TPDB 异常: {e}")
            meta = None
        if meta:
            log(f"  ✓ TPDB 命中: {meta.get('title', '')[:40]}")
            return meta, _western_id(info, meta)
    _pause()
    try:
        meta = scrape_iafd(info)
    except Exception as e:
        log(f"  ⚠ IAFD 异常: {e}")
        meta = None
    if meta:
        log(f"  ✓ IAFD 命中: {meta.get('title', '')[:40]}")
        # 结果可信度校验：站名前缀与厂牌/日期不符时，尝试演员作品表精确匹配
        site = (info.get("site") or "").lower()
        ok = True
        if site and len(site) >= 3:
            ftext = ((meta.get("studio") or "") + " "
                     + (meta.get("title") or "")).lower()
            ok = site in ftext
        elif site:
            ok = _date_matches(info.get("date"), meta.get("date"))
        if ok:
            return meta, _western_id(info, meta)
        log("  IAFD 结果与文件名线索不符，尝试演员作品表精确匹配...")
    try:
        meta2 = scrape_iafd_person(info)
    except Exception as e:
        log(f"  ⚠ IAFD 演员表异常: {e}")
        meta2 = None
    if meta2:
        log(f"  ✓ IAFD 演员表命中: {meta2.get('title', '')[:40]}")
        return meta2, _western_id(info, meta2)
    if meta:
        return meta, _western_id(info, meta)   # 无更优结果时保留原兜底
    return None


# ---------- NFO / 图片 / 文件 ----------

def make_nfo(meta, code):
    date = meta.get("date", "")
    m = re.search(r"(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})", date)
    year = m.group(1) if m else ""
    premiered = f"{m.group(1)}-{m.group(2).zfill(2)}-{m.group(3).zfill(2)}" if m else ""

    def esc(s):
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    lines = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>', "<movie>",
             f"  <title>{esc(meta.get('title'))}</title>",
             f"  <originaltitle>{esc(meta.get('title'))}</originaltitle>",
             f"  <id>{esc(code)}</id>", f"  <num>{esc(code)}</num>",
             f"  <premiered>{premiered}</premiered>"]
    if year:
        lines.append(f"  <year>{year}</year>")
    if meta.get("runtime"):
        lines.append(f"  <runtime>{meta['runtime']}</runtime>")
    if meta.get("director"):
        lines.append(f"  <director>{esc(meta['director'])}</director>")
    for key in ("studio", "label"):
        if meta.get(key):
            lines.append(f"  <studio>{esc(meta[key])}</studio>")
            break
    for g in meta.get("genres", []):
        lines.append(f"  <genre>{esc(g)}</genre>")
    thumbs = meta.get("actor_thumbs", {})
    avatars = meta.get("actor_avatars", {})
    pairs = meta.get("actor_pairs")
    if pairs is None:                    # 欧美/FC2: 演员名单 + 名字索引的头像
        pairs = [(None, a) for a in meta.get("actors", [])]
    for page, a in pairs:
        lines += ["  <actor>", f"    <name>{esc(a)}</name>"]
        thumb = thumbs.get(a) or (avatars.get(page) if page else None)
        if thumb:
            lines.append(f"    <thumb>{esc(thumb)}</thumb>")
        lines.append("    <type>Actor</type>")
        lines.append("  </actor>")
    lines.append("</movie>")
    return "\n".join(lines)


def make_portrait_poster(cover_path, dest_path):
    """从横版封面裁竖版海报（保留右侧人物区）；失败返回 False"""
    if not HAS_PIL:
        return False
    try:
        import io as _io
        with open(cover_path, "rb") as f:
            im = Image.open(_io.BytesIO(f.read()))
        im = im.convert("RGB")
        w, h = im.size
        new_w = int(h * 0.707)
        if new_w < w:
            im = im.crop((w - new_w, 0, w, h))
        im.save(dest_path, "JPEG", quality=92)
        return True
    except Exception:
        return False


def make_text_poster(dest_path, title, sublines):
    """连兜底图都没有时，用 PIL 生成一张干净的文字海报，保证 poster.jpg 必有"""
    if not HAS_PIL:
        return False
    try:
        from PIL import Image, ImageDraw, ImageFont
        W, H = 700, 1000
        im = Image.new("RGB", (W, H))
        d = ImageDraw.Draw(im)
        for y in range(H):                      # 深色纵向渐变
            c = int(20 + 34 * y / H)
            d.line([(0, y), (W, y)], fill=(c, c + 4, c + 12))
        d.rectangle([0, 0, W, 8], fill=(196, 30, 58))

        def load(sz):
            for name in ("msyhbd.ttc", "msyh.ttc", "arialbd.ttf",
                         "arial.ttf", "calibri.ttf"):
                try:
                    return ImageFont.truetype(name, sz)
                except Exception:
                    continue
            return ImageFont.load_default()

        f_t, f_s = load(44), load(28)
        y = 110
        line = ""
        for w in (title or "Video").split():    # 标题自动换行
            t = (line + " " + w).strip()
            if d.textlength(t, font=f_t) > W - 80 and line:
                d.text((40, y), line, font=f_t, fill=(242, 242, 242))
                y += 58
                line = w
            else:
                line = t
        if line:
            d.text((40, y), line, font=f_t, fill=(242, 242, 242))
        y += 96
        for s in sublines:
            if s:
                d.text((40, y), str(s)[:44], font=f_s, fill=(168, 178, 192))
                y += 44
        im.save(dest_path, "JPEG", quality=90)
        return True
    except Exception:
        return False


def make_headshot_poster(dest_path, headshot_url, title, sublines):
    """用演员头像合成竖版海报（上半头像 + 下半信息条），失败返回 False"""
    if not HAS_PIL:
        return False
    tmp = dest_path + ".tmp_head.img"
    if not download_image(headshot_url, tmp, min_bytes=3000):
        return False
    try:
        from PIL import Image, ImageDraw, ImageFont
        import io as _io
        with open(tmp, "rb") as f:
            face = Image.open(_io.BytesIO(f.read())).convert("RGB")
        W, H, fh = 700, 1000, 700
        fw = max(1, int(face.width * fh / face.height))
        face = face.resize((fw, fh))
        canvas = Image.new("RGB", (W, H), (16, 18, 24))
        if fw >= W:
            left = (fw - W) // 2
            canvas.paste(face.crop((left, 0, left + W, fh)), (0, 0))
        else:
            canvas.paste(face, ((W - fw) // 2, 0))
        d = ImageDraw.Draw(canvas)
        d.rectangle([0, fh, W, H], fill=(16, 18, 24))

        def load(sz):
            for name in ("msyhbd.ttc", "msyh.ttc", "arialbd.ttf",
                         "arial.ttf", "calibri.ttf"):
                try:
                    return ImageFont.truetype(name, sz)
                except Exception:
                    continue
            return ImageFont.load_default()

        f_t, f_s = load(40), load(26)
        d.text((36, fh + 26), (title or "")[:26], font=f_t, fill=(242, 242, 242))
        y = fh + 86
        for s in sublines:
            if s:
                d.text((36, y), str(s)[:46], font=f_s, fill=(168, 178, 192))
                y += 40
        canvas.save(dest_path, "JPEG", quality=90)
        return True
    except Exception:
        return False
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


# ---------- Bing 图片兜底（欧美片无封面时） ----------

BING_BLOCK = ("alicdn.", "taobao.", "suning.", "funko.", "geilicdn.", "hdslb.",
              "yandex.", "wikipedia.", "wikimedia.", "aliexpress.", "jd.com",
              "pinduoduo", "kinopoisk", "imdb.", "tmdb.", "dreamstime.",
              "shutterstock.", "gettyimages.", "123rf.", "alamy.",
              "depositphotos.", "freepik.", "istockphoto.", "vecteezy.",
              "pngtree.", "popcultcha", "figurine", "wallpaper")
BING_HINT = ("scene", "porn", "xxx", "sex", "tube", "adult", "hd", "cum",
             "fuck", "nude", "boobs", "girl", "teen", "milf")


def bing_image_urls(q, limit=6):
    """Bing 图片搜索（关闭安全搜索），返回过滤后的原图 URL 列表"""
    url = ("https://www.bing.com/images/search?q=" + quote(q)
           + "&adlt=off&form=HDRSC2&first=1")
    hdr = dict(_HEADERS, Cookie="SRCHHPGUSR=ADLT=DEMOTE",
               Referer="https://www.bing.com/")
    raw = http_get(url, headers=hdr)
    if not raw:
        return []
    text = raw.decode("utf-8", "ignore")
    cand = []
    for m in re.finditer(r'murl&quot;:&quot;([^&"\s]+)', text):
        for u in m.group(1).split(","):          # 个别 murl 字段是逗号拼接的
            low = u.lower()
            if not low.startswith("http"):
                continue
            if not low.endswith((".jpg", ".jpeg", ".png")):
                continue                          # webp/gif 一律不要，保证 fanart 是真 JPEG
            if any(b in low for b in BING_BLOCK):
                continue
            if u not in cand:
                cand.append(u)
    cand.sort(key=lambda u: (any(h in u.lower() for h in BING_HINT), 0), reverse=True)
    return cand[:limit]


def _headers_for(url):
    """按目标选请求头：图片请求用图片 Accept，IAFD/fourhoi 图片必须带站内 Referer"""
    if "iafd.com" in url:
        return dict(_HEADERS, Referer="https://www.iafd.com/")
    if "fourhoi.com" in url:
        return dict(_HEADERS, Referer="https://njavtv.com/",
                    Accept="image/avif,image/webp,image/apng,image/*,*/*;q=0.8")
    if "contents.fc2.com" in url:
        return dict(_HEADERS, Referer="https://adult.contents.fc2.com/",
                    Accept="image/avif,image/webp,image/apng,image/*,*/*;q=0.8")
    if url.lower().endswith((".jpg", ".jpeg", ".png", ".webp")) or "/graphics/" in url:
        return dict(_HEADERS, Accept="image/avif,image/webp,image/apng,image/*,*/*;q=0.8")
    return _HEADERS


def download_image(url, dest, min_bytes=MIN_IMAGE):
    """下载并校验真图片（JPEG/PNG 魔数 + PIL 可解析），失败不落盘"""
    raw = http_get(url, headers=_headers_for(url))
    if not raw or len(raw) < min_bytes:
        return False
    if not (raw[:3] == b"\xff\xd8\xff" or raw[:8] == b"\x89PNG\r\n\x1a\n"):
        return False
    # FC2「注意」防病毒公告图黑名单（下架商品页的 og:image 统一是这张）
    if hashlib.md5(raw).hexdigest() in PLACEHOLDER_MD5:
        return False
    if HAS_PIL:
        try:
            import io as _io
            im = Image.open(_io.BytesIO(raw))
            im.verify()
            im = Image.open(_io.BytesIO(raw))
            # 过小的图基本都是占位图/缩略图标
            if min(im.size) < 240:
                return False
        except Exception:
            return False
    with open(dest, "wb") as f:
        f.write(raw)
    return True


def download(url, dest, min_bytes=MIN_IMAGE):
    if os.path.exists(dest) and os.path.getsize(dest) >= min_bytes:
        return True                      # 已存在（CD1/CD2 共用文件夹时不重复下载）
    raw = http_get(url, headers=_headers_for(url))
    if raw and len(raw) >= min_bytes:
        with open(dest, "wb") as f:
            f.write(raw)
        return True
    return False


def move_to(src, dest_dir, dest_name=None):
    os.makedirs(dest_dir, exist_ok=True)
    target = os.path.join(dest_dir, dest_name or os.path.basename(src))
    if os.path.exists(target):
        base, ext = os.path.splitext(target)
        n = 2
        while os.path.exists(f"{base} ({n}){ext}"):
            n += 1
        target = f"{base} ({n}){ext}"
    shutil.move(src, target)
    return target


_code_locks = {}
_code_locks_guard = threading.Lock()


def _code_lock(code):
    """同一个番号一把锁：多路并发时 CD1/CD2 不会同时写同一文件夹"""
    with _code_locks_guard:
        lk = _code_locks.get(code)
        if lk is None:
            lk = threading.Lock()
            _code_locks[code] = lk
        return lk


# ============ 分辨率识别 + 命名后缀 ============
# 优先用 ffprobe 读视频真实分辨率；没有 ffprobe 时退回文件名里的标签
_RES_FROM_NAME = re.compile(
    r"(?<!\d)(4320p|8k|2160p|4k|uhd|1440p|2k|qhd|1920x1080|1080p|fhd|"
    r"1280x720|720p|480p|sd)(?!\d)", re.I)
_RES_ALIAS = {
    "4320p": "8K", "8k": "8K",
    "2160p": "4K", "4k": "4K", "uhd": "4K",
    "1440p": "2K", "2k": "2K", "qhd": "2K",
    "1920x1080": "1080p", "1080p": "1080p", "fhd": "1080p",
    "1280x720": "720p", "720p": "720p",
    "480p": "480p", "sd": "480p",
}


def _res_label(w, h):
    """按宽/高映射到 480p / 720p / 1080p / 2K / 4K / 8K"""
    if not w or not h:
        return ""
    if w >= 7680 or h >= 4320:
        return "8K"
    if w >= 3840 or h >= 2160:
        return "4K"
    if w >= 2560 or h >= 1440:
        return "2K"
    if w >= 1920 or h >= 1080:
        return "1080p"
    if w >= 1280 or h >= 720:
        return "720p"
    return "480p"


def detect_resolution(fname, workdir):
    """返回 '1080p' / '2K' / '4K' 等；识别不到返回 RES_UNKNOWN"""
    if not APPEND_RESOLUTION:
        return ""
    path = os.path.join(workdir, fname)
    # 1) ffprobe 读真实分辨率（最准）
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height",
             "-of", "csv=p=0:s=x", path],
            stderr=subprocess.DEVNULL, timeout=20, text=True).strip()
        m = re.search(r"(\d{3,5})x(\d{3,5})", out)
        if m:
            label = _res_label(int(m.group(1)), int(m.group(2)))
            if label:
                return label
    except Exception:
        pass
    # 2) 文件名标签兜底（如 xxx_4K.mp4 / xxx-1080p.mkv）
    m = _RES_FROM_NAME.search(os.path.splitext(fname)[0])
    if m:
        return _RES_ALIAS.get(m.group(1).lower(), "")
    return RES_UNKNOWN


def res_suffix(fname, workdir):
    """生成要追加到名字后面的后缀，例如 ' 4K'"""
    res = detect_resolution(fname, workdir)
    return RES_TAG_FORMAT.format(res=res) if res else ""


def process_video(fname, workdir, done_dir, fail_dir, idx, total):
    t0 = time.time()
    src = os.path.join(workdir, fname)
    ext = os.path.splitext(fname)[1]
    tag = f"[{idx}/{total}] {fname}"

    codes = extract_codes(fname)
    western = parse_western(fname) if ENABLE_WESTERN else None
    if not codes and not western:
        log(f"{tag}\n  ✗ 无法识别番号 -> 失败")
        move_to(src, fail_dir)
        return "fail"

    part = extract_part(fname)
    suffix = res_suffix(fname, workdir)
    for c in codes:
        # 兼容旧命名（无分辨率后缀）和新命名，避免重复刮削
        for d in (os.path.join(done_dir, c), os.path.join(done_dir, c + suffix)):
            if not os.path.isdir(d):
                continue
            low = [x.lower() for x in os.listdir(d)]
            has_video = any(os.path.splitext(x)[1] in VIDEO_EXTS for x in low)
            if (f"{c}{part}{ext}".lower() in low
                    or f"{c}{part}{suffix}{ext}".lower() in low
                    or (has_video and not part)):
                log(f"{tag}\n  ↷ 已归档过 完成/{os.path.basename(d)}，跳过")
                return "skip"

    result = scrape(codes, western)
    if not result:
        log(f"{tag}\n  ✗ 刮削失败 -> 失败")
        move_to(src, fail_dir)
        return "fail"

    meta, final_id = result
    # 同一番号的多个文件（CD1/CD2）串行落盘，避免并发写同一文件夹互相覆盖
    with _code_lock(final_id):
        try:
            return _write_outputs(meta, final_id, fname, src, ext, workdir,
                                  done_dir, western, tag, t0, suffix)
        except Exception as e:
            log(f"{tag}\n  ✗ 写文件异常: {e}")
            try:
                move_to(src, fail_dir)
            except Exception:
                pass
            return "fail"


def _write_outputs(meta, final_id, fname, src, ext, workdir, done_dir,
                   western, tag, t0, suffix=""):
    part = extract_part(fname)
    new_stem = f"{final_id}{part}{suffix}"
    folder = os.path.join(workdir, f"{final_id}{suffix}")
    os.makedirs(folder, exist_ok=True)   # 直接在番号文件夹内下载，避免多线程临时文件冲突

    with open(os.path.join(folder, f"{new_stem}.nfo"), "w", encoding="utf-8") as f:
        f.write(make_nfo(meta, final_id))

    cover_path = os.path.join(folder, "fanart.jpg")
    poster_path = os.path.join(folder, "poster.jpg")
    have_cover = download(meta["cover"], cover_path) if meta.get("cover") else False

    # 封面兜底 1：Bing 图片搜索（仅当有站点编号/标题等唯一性较强的词时，
    # 纯演员名查询极易撞车——人名撞地名/电影名，宁可用头像海报）
    if not have_cover and western and (western.get("site_code")
                                       or western.get("title")):
        q = " ".join(x for x in (western.get("site_code"),
                                 western.get("actress")
                                 or western.get("title")) if x)
        log(f"  无封面，Bing 图片兜底: {q}")
        for u in bing_image_urls(q):
            if download_image(u, cover_path):
                have_cover = True
                log("  ✓ Bing 兜底封面已下载（可能非本片精确封面）")
                break
        else:
            log("  ⚠ Bing 兜底未找到可用图片")

    have_poster = os.path.exists(poster_path) and os.path.getsize(poster_path) >= MIN_IMAGE
    if not have_poster and have_cover:
        have_poster = make_portrait_poster(cover_path, poster_path)
    if not have_poster and meta.get("poster_thumb"):
        have_poster = download(meta["poster_thumb"], poster_path)
    if not have_poster and have_cover and not os.path.exists(poster_path):
        shutil.copyfile(cover_path, poster_path)
    # 封面兜底 2：演员头像合成海报
    if not have_poster and meta.get("actor_thumbs"):
        hs = next(iter(meta["actor_thumbs"].values()), None)
        subs = [(meta.get("date") or "")[:10], meta.get("studio") or "",
                ", ".join((meta.get("actors") or [])[1:4])]
        if make_headshot_poster(poster_path, hs,
                                (meta.get("actors") or [final_id])[0], subs):
            log("  ✓ 已用演员头像合成海报")
    # 封面兜底 3：文字海报，保证 poster.jpg 始终存在
    if not have_poster:
        subs = [meta.get("studio") or "", (meta.get("date") or "")[:10],
                ", ".join((meta.get("actors") or [])[:3])]
        if make_text_poster(poster_path, meta.get("title") or final_id, subs):
            log("  ✓ 已生成文字海报")

    # 预览图 + 演员头像并发下载（都落到番号文件夹里，文件名唯一）
    jobs, avatars = [], []
    for i, s in enumerate(meta.get("samples", []), 1):
        jobs.append((s, os.path.join(folder, f"sample_{i:02d}.jpg")))
    for i, (page, name) in enumerate(meta.get("actor_pairs", [])):
        url = meta.get("actor_avatars", {}).get(page)
        if not url:
            continue
        safe = re.sub(r'[\\/:*?"<>|]', "_", name).strip() or f"actor_{i+1}"
        dest = os.path.join(folder, f"{safe}.jpg")
        jobs.append((url, dest))
        avatars.append(dest)
    for i, (name, url) in enumerate(meta.get("actor_thumbs", {}).items()):
        safe = re.sub(r'[\\/:*?"<>|]', "_", name).strip() or f"actor_{i+1}"
        dest = os.path.join(folder, f"{safe}.jpg")
        jobs.append((url, dest))
        avatars.append(dest)

    ok_count = 0
    if jobs:
        with ThreadPoolExecutor(max_workers=min(6, len(jobs))) as ex:
            futs = {ex.submit(download, u, d): d for u, d in jobs}
            for f in as_completed(futs):
                try:
                    if f.result():
                        ok_count += 1
                    else:
                        d = futs[f]
                        if os.path.exists(d) and os.path.getsize(d) < MIN_IMAGE:
                            os.remove(d)
                except Exception:
                    pass

    # 移动视频 + 同名伴随文件（字幕等）
    move_to(src, folder, f"{new_stem}{ext}")
    stem0 = os.path.splitext(fname)[0]
    try:
        names = os.listdir(workdir)
    except OSError:
        names = []
    for f2 in names:
        p2 = os.path.join(workdir, f2)
        try:
            if not os.path.isfile(p2):
                continue
        except OSError:
            continue
        s2, e2 = os.path.splitext(f2)
        if s2 == stem0 and e2.lower() not in VIDEO_EXTS:
            try:
                move_to(p2, folder, f"{new_stem}{e2}")
            except OSError:
                pass

    # 番号文件夹移入「完成」（若已存在则合并进去，兼容 CD1/CD2）
    target_dir = os.path.join(done_dir, f"{final_id}{suffix}")
    if os.path.isdir(target_dir):
        for item in os.listdir(folder):
            src_p = os.path.join(folder, item)
            dst_p = os.path.join(target_dir, item)
            # 海报/背景是同番号共用资源，已有就保留，别堆出 poster (2).jpg
            if item in ("poster.jpg", "fanart.jpg") and os.path.exists(dst_p):
                try:
                    os.remove(src_p)
                except OSError:
                    pass
                continue
            move_to(src_p, target_dir)
        try:
            os.rmdir(folder)
        except OSError:
            pass
    else:
        shutil.move(folder, target_dir)

    log(f"{tag}\n  ✓ {final_id}{suffix} | {(meta.get('title') or '')[:30]} "
        f"| {ok_count} 图 | {time.time()-t0:.1f}s")
    return "ok"


def _resolve_workers(argv):
    """并发数优先级：命令行第2个参数 > JAV_WORKERS 环境变量 > 默认 WORKERS"""
    raw = ""
    if len(argv) > 2:
        raw = argv[2].strip()
    if not raw:
        raw = os.environ.get("JAV_WORKERS", "").strip()
    n = WORKERS
    if raw:
        try:
            n = int(raw)
        except ValueError:
            print(f"[warn] 并发数 '{raw}' 不是整数，改用默认 {WORKERS}")
            n = WORKERS
    n = max(1, min(n, MAX_WORKERS))
    if raw and str(n) != raw and raw.lstrip("+-").isdigit():
        print(f"[warn] 并发数 {raw} 超出范围 1~{MAX_WORKERS}，已按 {n} 执行")
    return n


def main():
    global WORKERS
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    WORKERS = _resolve_workers(sys.argv)

    workdir = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else \
        os.path.dirname(os.path.abspath(__file__))
    done_dir = os.path.join(workdir, "完成")
    fail_dir = os.path.join(workdir, "失败")
    os.makedirs(done_dir, exist_ok=True)
    os.makedirs(fail_dir, exist_ok=True)

    videos = [f for f in os.listdir(workdir)
              if os.path.isfile(os.path.join(workdir, f))
              and os.path.splitext(f)[1].lower() in VIDEO_EXTS
              and not f.startswith(".")]
    if not videos:
        print("未发现视频文件。")
        return

    print(f"共 {len(videos)} 个视频，{WORKERS} 路并发，开始处理...\n")
    if WORKERS > 6:
        print("[提示] 并发 >6 时 javbus 触发反爬的概率明显上升，"
              "若出现大量失败请降到 4~6 或设置 JAV_PROXY\n")
    stats = {"ok": 0, "fail": 0, "skip": 0}
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(process_video, f, workdir, done_dir, fail_dir, i, len(videos)): f
                for i, f in enumerate(videos, 1)}
        for f in as_completed(futs):
            try:
                stats[f.result()] += 1
            except Exception as e:
                stats["fail"] += 1
                log(f"  ! {futs[f]} 异常: {e}")

    print("=" * 46)
    print(f"耗时 {time.time()-t0:.0f}s | 成功 {stats['ok']} | 跳过 {stats['skip']} | 失败 {stats['fail']}")
    print(f"成功 -> {done_dir}")
    print(f"失败 -> {fail_dir}")


if __name__ == "__main__":
    main()
