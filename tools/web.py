"""栖语 联网工具箱：免费公开接口，失败自动降级，不拖垮主链路
- web_search: Bing RSS 主（fast xml）/ Bing HTML 备 / Sogou / 百度移动 / 百度桌面 逐级降级（国内网络实测 Bing 间歇超时、DDG 被墙）
- fetch_page_meta: 通用网页元信息（og:title/description）
- video_info: B站/YouTube/抖音/通用 视频信息
- fetch_hotlist: 每日热门（B站热门视频 + DDG 综合热搜），本地缓存
"""
import asyncio
import ipaddress
import json
import re
import socket
import time
import html as html_mod
import urllib.parse
from pathlib import Path
from typing import Optional

import httpx
from loguru import logger

from pathutil import get_data_dir

_HDRS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

HOT_CACHE = get_data_dir() / "hot_cache.json"
HOT_TTL = 6 * 3600  # 6 小时内不重复抓


def _clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text or "")
    text = html_mod.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def is_public_http_url(url: str) -> bool:
    """只放行 http/https，且域名解析出的所有地址都是公网 IP。

    用于挡住 `/v1/web/link` 这类「用户给什么就抓什么」的入口：
    否则本机服务会被当成跳板去访问 192.168.2.x / 127.0.0.1 / 169.254.x（SSRF）。
    """
    try:
        p = urllib.parse.urlparse((url or "").strip())
        if p.scheme not in ("http", "https") or not p.hostname:
            return False
        port = p.port or (443 if p.scheme == "https" else 80)
        infos = socket.getaddrinfo(p.hostname, port, type=socket.SOCK_STREAM)
        if not infos:
            return False
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if (ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
                return False
        return True
    except Exception:
        return False


async def _get(url: str, params: dict = None, headers: dict = None, timeout: float = 15,
               allow_redirects: bool = True) -> httpx.Response:
    h = dict(_HDRS)
    if headers:
        h.update(headers)
    async with httpx.AsyncClient(timeout=timeout, headers=h,
                                 follow_redirects=allow_redirects) as client:
        return await client.get(url, params=params)


# ============ 搜索 ============


# ============ 天气兜底（wttr.in，真实数据） ============
_WEATHER_RE = re.compile(r"(天气|气温|预报|weather|下雨|下雪|降温)")
_WEATHER_STRIP_RE = re.compile(r"(帮我|帮|查|看|一下|请问|想|知道|那个|最近|现在|这会儿|今天|明天|后天|昨天|大后天|周末|周[一二三四五六日天]|星期[一二三四五六日天]|的)")
_WEATHER_CITY_RE = re.compile(r"([\u4e00-\u9fa5]{2,4})(?:天气|气温|预报)")


def _weather_city(query: str) -> str:
    q = _WEATHER_STRIP_RE.sub("", query)
    m = _WEATHER_CITY_RE.search(q)
    if m:
        return m.group(1)
    # 兜底：直辖市/省会/常见城市名出现在查询里也算
    for city in ("北京", "上海", "广州", "深圳", "成都", "重庆", "杭州", "武汉", "西安", "南京", "天津",
                 "苏州", "郑州", "长沙", "东莞", "沈阳", "青岛", "合肥", "佛山", "昆明", "大连", "厦门",
                 "哈尔滨", "济南", "温州", "南宁", "长春", "泉州", "石家庄", "贵阳", "南昌", "太原", "兰州", "海口"):
        if city in query:
            return city
    return ""


async def _weather_now(query: str) -> list:
    """识别天气类查询，用 wttr.in 取真实预报，返回 [{title,url,snippet}]；非天气查询/失败返回 []"""
    if not _WEATHER_RE.search(query):
        return []
    city = _weather_city(query)
    if not city:
        return []
    q = urllib.parse.quote(city)
    for _attempt in range(2):
        try:
            r = await _get(f"https://wttr.in/{q}", params={"format": "j1", "lang": "zh"}, timeout=10)
            r.raise_for_status()
            d = r.json()
            cur = (d.get("current_condition") or [{}])[0]
            temp = cur.get("temp_C", "?")
            desc = "".join(x.get("value", "") for x in cur.get("weatherDesc") or [])
            feels = cur.get("FeelsLikeC", "?")
            humid = cur.get("humidity", "?")
            wind = cur.get("windspeedKmph", "?")
            today = (d.get("weather") or [{}])[0]
            tmin = today.get("mintempC", "?")
            tmax = today.get("maxtempC", "?")
            date = today.get("date", "")
            snippet = (f"{city}实时天气：{temp}°C（{desc}，体感{feels}°C，湿度{humid}%，风速{wind}km/h）；"
                       f"今日{date} 最低{tmin}°C / 最高{tmax}°C。")
            return [{"title": f"{city}实时天气（wttr.in 真实数据）", "url": f"https://wttr.in/{q}", "snippet": snippet}]
        except Exception as e:
            logger.warning(f"[联网] 天气源重试: {e}")
    return []


_SEARCH_BUDGET = 20.0  # 单次搜索总时限：天气 + 多引擎降级链合计，避免离线/挂死时占用并发闸过久


async def web_search(query: str, top_k: int = 5) -> list:
    """搜索网页，返回 [{title, url, snippet}]，全部失败/超时返回 []。天气类查询先用 wttr.in 兜底（真实数据），
    再走降级链：Bing(主) → Sogou → 搜狗微信 → 百度联想 → 百度移动 → 百度桌面。"""
    out = []
    loop = asyncio.get_event_loop()
    deadline = loop.time() + _SEARCH_BUDGET

    async def _within(coro, max_wait: float) -> list:
        return await asyncio.wait_for(coro, timeout=max(1.0, min(max_wait, deadline - loop.time())))

    try:
        out.extend(await _within(_weather_now(query), 8.0))
    except asyncio.TimeoutError:
        logger.warning("[联网] 天气兜底超时，跳过")
    except Exception as e:
        logger.warning(f"[联网] 天气兜底失败: {e}")
    for fn in (_bing_search, _sogou_search, _sogou_wx_search, _baidu_mobile_search, _baidu_suggest, _baidu_search):
        if deadline - loop.time() <= 0:
            break
        try:
            got = await _within(fn(query, top_k), 7.0)
            if got:
                out.extend(got)
                break
        except asyncio.TimeoutError:
            logger.warning(f"[联网] 搜索降级: {fn.__name__} 超时")
        except Exception as e:
            logger.warning(f"[联网] 搜索降级: {type(e).__name__}: {e}")
    # 去重（同 URL 只留一条，天气结果优先）
    seen, merged = set(), []
    for it in out:
        u = it.get("url") or ""
        if u in seen:
            continue
        seen.add(u)
        merged.append(it)
    return merged[: max(top_k, 6)]


async def _ddg_search(query: str, top_k: int) -> list:
    r = await _get("https://lite.duckduckgo.com/lite/", params={"q": query}, timeout=4.0)
    r.raise_for_status()
    text = r.text
    results = []
    # DDG lite 结果：<a rel="nofollow" href="//duckduckgo.com/l/?uddg=...">标题</a> 后跟 snippet
    for m in re.finditer(r'<a[^>]+rel="nofollow"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', text, re.S):
        href = html_mod.unescape(m.group(1))
        title = _clean(m.group(2))
        if not title or title.startswith("!"):
            continue
        real = href
        if "uddg=" in href:
            real = urllib.parse.unquote(re.sub(r"^.*uddg=", "", href))
            real = re.sub(r"&rut=.*$", "", real)
        if real.startswith("//"):
            real = "https:" + real
        if not real.startswith("http"):
            continue
        # 找紧随其后的 snippet（到下一个 <a 为止）
        snippet = ""
        after = text[m.end():]
        nxt = re.search(r"<a ", after)
        if nxt:
            seg = after[:nxt.start()]
            snippet = _clean(re.sub(r"<[^>]+>", " ", seg))[:200]
        results.append({"title": title[:160], "url": real[:500], "snippet": snippet})
        if len(results) >= top_k:
            break
    return results


async def _bing_search(query: str, top_k: int) -> list:
    """Bing 搜索：RSS 格式优先（XML 稳定可解析），多主机轮询（国内网络下 www/global/www2/cn 不同主机可用性不同），
    全部失败再试 www 的 HTML 国际版（ensearch=1 绕过国内重定向/安全页）。"""
    # 1) RSS：<item><title>..</title><link>..</link><description>..</description>
    hosts = ["https://www.bing.com/search", "https://global.bing.com/search",
             "https://www2.bing.com/search", "https://cn.bing.com/search"]
    for host in hosts:
        try:
            r = await _get(host, params={"q": query, "format": "rss"}, timeout=6)
            r.raise_for_status()
            out = []
            for it in re.findall(r"<item>(.*?)</item>", r.text, re.S):
                tm = re.search(r"<title>(.*?)</title>", it, re.S)
                lm = re.search(r"<link>(.*?)</link>", it, re.S)
                dm = re.search(r"<description>(.*?)</description>", it, re.S)
                title = _clean(html_mod.unescape(tm.group(1))) if tm else ""
                url = html_mod.unescape(lm.group(1)).strip() if lm else ""
                if not title or not url.startswith("http"):
                    continue
                snippet = _clean(html_mod.unescape(dm.group(1))) if dm else ""
                out.append({"title": title[:160], "url": url[:500], "snippet": snippet[:200]})
                if len(out) >= top_k:
                    break
            if out:
                return out
        except Exception:
            continue
    # 2) HTML 国际版（ensearch=1）
    try:
        r = await _get("https://www.bing.com/search", params={"q": query, "ensearch": "1"}, timeout=8)
        r.raise_for_status()
        text = r.text
        results = []
        for block in re.findall(r'<li class="b_algo".*?</li>', text, re.S)[:top_k]:
            hm = re.search(r'<h2><a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
            if not hm:
                continue
            url = html_mod.unescape(hm.group(1))
            title = _clean(hm.group(2))
            pm = re.search(r"<p[^>]*>(.*?)</p>", block, re.S)
            snippet = _clean(pm.group(1)) if pm else ""
            results.append({"title": title[:160], "url": url[:500], "snippet": snippet[:200]})
        return results
    except Exception:
        return []


async def _sogou_search(query: str, top_k: int) -> list:
    """搜狗网页搜索（国内可直连，返回真实 HTML）。链接为 /link?url= 跳转，浏览器可直接打开。"""
    r = await _get("https://www.sogou.com/web", params={"query": query}, timeout=8)
    r.raise_for_status()
    text = r.text
    results = []
    # 结果块：<h3 class="vr-title"><a href="...">标题</a></h3>，随后的 .text-layout 为摘要
    for m in re.finditer(r'<h3[^>]*class="[^"]*vr-title[^"]*"[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', text, re.S):
        url = html_mod.unescape(m.group(1))
        title = _clean(m.group(2))
        if not title or not url.startswith("http") and not url.startswith("/link"):
            continue
        if url.startswith("/link"):
            url = "https://www.sogou.com" + url
        # 摘要：取 <p class="star-wiki" 或 .text-layout 或 .str_info
        tail = text[m.end():m.end() + 1200]
        sm = re.search(r'<(?:p|div)[^>]*class="[^"]*(?:text-layout|str_info|star-wiki|space-txt)[^"]*"[^>]*>(.*?)</(?:p|div)>', tail, re.S)
        snippet = _clean(sm.group(1)) if sm else ""
        results.append({"title": title[:160], "url": url[:500], "snippet": snippet[:200]})
        if len(results) >= top_k:
            break
    return results


async def _sogou_wx_search(query: str, top_k: int) -> list:
    """搜狗微信搜索（国内稳定可达，返回微信公众号文章结果）。"""
    try:
        r = await _get("https://weixin.sogou.com/weixin", params={"type": "2", "query": query}, timeout=7)
        r.raise_for_status()
    except Exception:
        return []
    text = r.text
    results = []
    for m in re.finditer(r'<div class="txt-box">(.*?)</div>', text, re.S):
        block = m.group(1)
        hm = re.search(r'<h3>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not hm:
            continue
        url = html_mod.unescape(hm.group(1))
        title = _clean(hm.group(2))
        if not title:
            continue
        if url.startswith("/link"):
            url = "https://weixin.sogou.com" + url
        if not url.startswith("http"):
            continue
        pm = re.search(r'<p class="txt-info"[^>]*>(.*?)</p>', block, re.S)
        snippet = _clean(pm.group(1)) if pm else ""
        results.append({"title": title[:160], "url": url[:500], "snippet": snippet[:200]})
        if len(results) >= top_k:
            break
    return results


async def _baidu_suggest(query: str, top_k: int) -> list:
    """百度搜索联想（suggestion.baidu.com 稳定可达，无验证页）。
    只返回「联想词 + 真实百度搜索入口链接」，用于全量搜索都失败时给用户一个可点的入口。"""
    try:
        r = await _get("https://suggestion.baidu.com/su", params={"wd": query}, timeout=5)
        r.raise_for_status()
    except Exception:
        return []
    m = re.search(r'window\.baidu\.sug\((.*?)\)\s*;', r.text, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except Exception:
        return []
    sgs = [str(x) for x in (data.get("s") or []) if str(x).strip()][:top_k]
    out = []
    for sg in sgs:
        out.append({
            "title": sg[:160],
            "url": "https://www.baidu.com/s?wd=" + urllib.parse.quote(sg),
            "snippet": f"百度搜索：{sg}",
        })
    return out


async def _baidu_mobile_search(query: str, top_k: int) -> list:
    """百度移动版（m.baidu.com 无安全验证，返回真实结果 HTML）。"""
    r = await _get("https://m.baidu.com/s", params={"word": query}, timeout=8)
    r.raise_for_status()
    text = r.text
    if "安全验证" in text or "百度安全" in text:
        return []
    results = []
    for m in re.finditer(r'<h3[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', text, re.S):
        url = html_mod.unescape(m.group(1))
        title = _clean(m.group(2))
        if not title or not url.startswith("http"):
            continue
        tail = text[m.end():m.end() + 700]
        seg = _clean(re.sub(r"<[^>]+>", " ", tail))
        snippet = seg[:200]
        results.append({"title": title[:160], "url": url[:500], "snippet": snippet})
        if len(results) >= top_k:
            break
    return results


async def _baidu_search(query: str, top_k: int) -> list:
    """百度网页搜索兜底（国内可直连）。百度结果 URL 是跳转链接，标题可直接用。"""
    r = await _get("https://www.baidu.com/s", params={"wd": query, "rn": 20})
    r.raise_for_status()
    text = r.text
    if "安全验证" in text or "verify" in text.lower() or "百度安全" in text:
        return []
    results = []
    for m in re.finditer(r'<h3[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', text, re.S):
        url = html_mod.unescape(m.group(1))
        title = _clean(m.group(2))
        if not title or not url.startswith("http"):
            continue
        snippet = ""
        tail = text[m.end():m.end() + 600]
        seg = _clean(re.sub(r"<[^>]+>", " ", tail))
        if seg:
            snippet = seg[:200]
        results.append({"title": title[:160], "url": url[:500], "snippet": snippet})
        if len(results) >= top_k:
            break
    return results


# ============ 图片搜索（免费源：Bing 图片主 / 百度图片备） ============

async def image_search(query: str, top_k: int = 3) -> list:
    """搜索图片，返回 [{title, url, image_url, source}]；全部失败返回 []。
    用户要看 xxx 长什么样时走这里：真的能找到图就把真实图片发过去。
    兜底：loremflickr 免费图源（国内可直连，返回真实 JPEG，按关键词给随机图）。"""
    for fn in (_bing_image_search, _baidu_image_search):
        try:
            out = await fn(query, top_k)
            if out:
                return out[:top_k]
        except Exception as e:
            logger.warning(f"[图片] 搜索降级: {type(e).__name__}: {e}")
    try:
        words = re.findall(r"[a-zA-Z]{3,}", query or "")
        seg = max(words, key=len) if words else "photo"
        seg = seg[:20].lower()
        return [{"title": f"网图（{query[:20]}）", "url": f"https://loremflickr.com/640/480/{seg}",
                 "image_url": f"https://loremflickr.com/640/480/{seg}", "source": "loremflickr"}]
    except Exception:
        return []


async def _bing_image_search(query: str, top_k: int) -> list:
    r = await _get("https://www.bing.com/images/search",
                   params={"q": query, "setlang": "zh-hans", "mkt": "zh-CN", "form": "HDRSC2"})
    r.raise_for_status()
    text = r.text
    out = []
    for m in re.finditer(r'\bm="(\{.*?\})"', text, re.S):
        try:
            d = json.loads(html_mod.unescape(m.group(1)))
        except Exception:
            continue
        murl = (d.get("murl") or d.get("turl") or "").strip()
        if not murl.startswith("http"):
            continue
        out.append({
            "title": _clean(d.get("t") or "")[:160],
            "url": (d.get("purl") or murl)[:500],
            "image_url": murl,
            "source": "bing",
        })
        if len(out) >= top_k:
            break
    return out


async def _baidu_image_search(query: str, top_k: int) -> list:
    r = await _get("https://image.baidu.com/search/acjson",
                   params={"tn": "resultjson_com", "ipn": "rj", "word": query,
                           "pn": 0, "rn": max(6, top_k * 2), "width": "", "height": "",
                           "face": 0, "istype": 2, "qc": "", "nc": 1, "fr": "", "simid": "",
                           "lm": -1, "ie": "utf-8", "oe": "utf-8", "cg": "", "z": "", "st": -1,
                           "cl": 2, "ct": "", "di": "", "pi": "", "tn": "resultjson_com"},
                   timeout=12)
    r.raise_for_status()
    data = r.json()
    out = []
    for it in (data.get("data") or []):
        if not isinstance(it, dict):
            continue
        img = (it.get("objURL") or it.get("thumbURL") or it.get("middleURL") or "").strip()
        if not img.startswith("http"):
            continue
        out.append({
            "title": _clean(it.get("fromPageTitle") or "")[:160],
            "url": (it.get("fromURLHost") or img)[:500],
            "image_url": img,
            "source": "baidu",
        })
        if len(out) >= top_k:
            break
    return out


# ============ 网页元信息 ============

async def fetch_page_meta(url: str) -> Optional[dict]:
    """抓网页 og 元信息（标题/描述），失败或非网页返回 None"""
    try:
        if not is_public_http_url(url):
            logger.warning(f"[联网] 拒绝非公网地址: {(url or '')[:120]}")
            return None
        # 不跟随重定向：防止公网地址 302 到 127.0.0.1/内网，绕过上面的校验
        r = await _get(url, timeout=12, allow_redirects=False)
        if r.status_code in (301, 302, 303, 307, 308):
            logger.warning(f"[联网] 跳过重定向地址: {(url or '')[:120]}")
            return None
        if r.status_code != 200:
            return None
        text = r.text
        title = ""
        desc = ""
        m = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', text, re.I) or \
            re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']', text, re.I)
        if m:
            title = _clean(m.group(1))
        if not title:
            m = re.search(r"<title[^>]*>(.*?)</title>", text, re.S | re.I)
            title = _clean(m.group(1)) if m else ""
        m = re.search(r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)["\']', text, re.I) or \
            re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:description["\']', text, re.I)
        if m:
            desc = _clean(m.group(1))
        if not desc:
            m = re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)["\']', text, re.I)
            desc = _clean(m.group(1)) if m else ""
        if not title and not desc:
            return None
        return {"title": title[:200], "description": desc[:300], "url": url, "platform": "网页"}
    except Exception as e:
        logger.warning(f"[联网] 抓页失败 {url}: {type(e).__name__}")
        return None


# ============ 视频信息 ============

async def video_info(url: str) -> Optional[dict]:
    """解析视频链接信息（B站/YouTube/抖音/通用 og），失败返回 None（模型可自然说'我手机里没有xxx'）"""
    url = (url or "").strip()
    if not url.startswith("http"):
        return None
    if not is_public_http_url(url):
        logger.warning(f"[联网] 拒绝非公网视频地址: {url[:120]}")
        return None
    if "bilibili.com" in url or "b23.tv" in url:
        info = await _bilibili_info(url)
        if info:
            return info
    if "youtube.com" in url or "youtu.be" in url:
        try:
            r = await _get("https://www.youtube.com/oembed", params={"url": url, "format": "json"})
            if r.status_code == 200:
                d = r.json()
                return {"platform": "youtube", "title": d.get("title", ""), "description": "", "author": d.get("author_name", ""), "url": url}
        except Exception:
            pass
    if "douyin.com" in url or "iesdouyin.com" in url:
        meta = await fetch_page_meta(url)
        if meta:
            meta["platform"] = "douyin"
            return meta
    # 通用
    meta = await fetch_page_meta(url)
    if meta:
        return meta
    return None


async def _bilibili_info(url: str) -> Optional[dict]:
    bvid = ""
    m = re.search(r"(BV[0-9A-Za-z]{10})", url)
    if m:
        bvid = m.group(1)
    elif "b23.tv" in url:
        try:
            r = await _get(url, timeout=10)
            url = str(r.url)
            m = re.search(r"(BV[0-9A-Za-z]{10})", url)
            if m:
                bvid = m.group(1)
        except Exception:
            pass
    if not bvid:
        return None
    try:
        r = await _get("https://api.bilibili.com/x/web-interface/view",
                       params={"bvid": bvid},
                       headers={"Referer": "https://www.bilibili.com/"})
        if r.status_code != 200:
            return None
        d = r.json().get("data") or {}
        if not d.get("title"):
            return None
        return {
            "platform": "bilibili",
            "title": d.get("title", ""),
            "description": (d.get("desc") or "")[:300],
            "author": (d.get("owner") or {}).get("name", ""),
            "url": f"https://www.bilibili.com/video/{bvid}",
        }
    except Exception as e:
        logger.warning(f"[联网] B站信息失败: {type(e).__name__}: {e}")
        return None


# ============ 每日热门 ============

async def fetch_hotlist(force: bool = False) -> list:
    """每日热门：B站热门视频 + DDG 综合热搜；本地缓存 6 小时"""
    if not force and HOT_CACHE.exists():
        try:
            data = json.loads(HOT_CACHE.read_text(encoding="utf-8"))
            if data.get("ts", 0) > time.time() - HOT_TTL and data.get("items"):
                return data["items"]
        except Exception:
            pass
    items = []
    try:
        r = await _get("https://api.bilibili.com/x/web-interface/popular",
                       params={"ps": 8, "pn": 1},
                       headers={"Referer": "https://www.bilibili.com/"})
        if r.status_code == 200:
            for v in (r.json().get("data") or {}).get("list", [])[:6]:
                bvid = v.get("bvid", "")
                title = _clean(v.get("title", ""))
                if title and bvid:
                    items.append({
                        "kind": "video",
                        "title": title[:120],
                        "url": f"https://www.bilibili.com/video/{bvid}",
                        "source": "B站热门",
                        "extra": f"UP主：{(v.get('owner') or {}).get('name', '')}",
                    })
    except Exception as e:
        logger.warning(f"[联网] B站热门抓取失败: {e}")
    try:
        for q in ("今日热搜 新闻", "今天 热门事件"):
            res = await web_search(q, top_k=3)
            for it in res:
                items.append({"kind": "news", "title": it["title"][:120], "url": it["url"], "source": "热搜", "extra": ""})
    except Exception as e:
        logger.warning(f"[联网] 热搜抓取失败: {e}")
    # 去重
    seen = set()
    uniq = []
    for it in items:
        key = it["title"][:30]
        if key in seen:
            continue
        seen.add(key)
        uniq.append(it)
    if uniq:
        try:
            HOT_CACHE.write_text(json.dumps({"ts": time.time(), "items": uniq}, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
    return uniq[:10]


# ============ 小红书 / 购物 ============

def is_shopish(url: str) -> bool:
    return any(k in url for k in ("xiaohongshu", "xhslink", "taobao", "tmall", "jd.com", "item.taobao", "detail.tmall"))


async def lookup_shop_url(url: str) -> Optional[dict]:
    """小红书/购物链接：尽力解析重定向与 og 元信息；登录墙内抓不到就返回 None，让模型像真人一样说'我手机里没有xxx'"""
    try:
        if "xhslink.com" in url or "xhslink.cn" in url:
            try:
                r = await _get(url, timeout=12)
                url = str(r.url)
            except Exception:
                pass
        meta = await fetch_page_meta(url)
        if meta:
            meta["platform"] = "小红书" if ("xiaohongshu" in url or "xhslink" in url) else "购物"
            return meta
        return None
    except Exception as e:
        logger.warning(f"[联网] 购物链接解析失败: {e}")
        return None
