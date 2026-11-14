# -*- coding: utf-8 -*-
"""联网搜索：通过必应网页结果获取标题与摘要，供本地模型参考"""
import base64
import html
import re
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
_cache = {}
_CACHE_TTL = 300

# 搜索前去掉的口语化修饰词，避免必应被"今天/请问"等词带偏
_FILLERS = (
    "请问", "请帮我", "帮我", "帮忙", "麻烦", "我想知道", "想知道", "告诉我",
    "你好", "一下", "谢谢", "能不能", "可以", "吗", "呢", "吧",
    "现在", "目前", "当前", "今天", "最近",
    # 疑问句式去掉后更容易命中文档页而不是首页
    "有哪些", "有什么", "是什么", "怎么样", "怎么", "如何", "为什么",
    "什么时候", "何时", "哪一个", "哪个", "哪些", "是否", "有没有",
    "介绍一下", "介绍",
)

# 用户明确要求搜索的说法：命中时一定联网
_SEARCH_INTENT = (
    "搜索", "搜一下", "搜下", "查一下", "查下", "帮我查", "帮我搜",
    "联网查", "网上查", "查询一下", "搜搜", "查查",
)

_DROP_DOMAINS = ("bing.com", "microsoft.com/zh-cn/search", "go.microsoft.com")


def _clean_query(query: str) -> str:
    text = (query or "").strip()
    for word in _SEARCH_INTENT + _FILLERS:
        text = text.replace(word, " ")
    text = re.sub(r"[？?！!。，,；;：:\s]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or (query or "").strip()


def has_search_intent(query: str) -> bool:
    """用户是否明确要求联网搜索。"""
    text = query or ""
    return any(w in text for w in _SEARCH_INTENT)


_GREETINGS = (
    "你好", "您好", "嗨", "hi", "hello", "在吗", "早上好", "上午好", "中午好",
    "下午好", "晚上好", "晚安", "谢谢", "多谢", "辛苦了", "好的", "好", "嗯",
    "ok", "收到", "明白了", "再见",
)


def is_chitchat(query: str) -> bool:
    """纯寒暄/无信息量的短句：即使开了联网也没必要搜索。"""
    text = re.sub(r"[\s\W_]+", "", (query or "").lower())
    if not text:
        return True
    if len(text) <= 10 and any(text.startswith(g) for g in _GREETINGS):
        return True
    return len(text) <= 2


# 问句里对检索没帮助的尾巴，去掉后更容易命中实体页
_QUERY_TAILS = (
    "举办地", "举办国家", "举办城市", "举办时间", "举办地点", "在哪个国家",
    "在哪里", "是哪", "是哪个", "多少钱", "价格是多少", "是什么",
)


def _simplify(text: str) -> str:
    """把「2026年世界杯举办地」这类问句压成核心关键词「2026世界杯」。"""
    text = re.sub(r"(?<=\d)(年|月|日|号)", "", text)
    for word in _QUERY_TAILS:
        text = text.replace(word, " ")
    return re.sub(r"\s+", " ", text).strip()


def _build_queries(query: str):
    """构造多个检索词：清洗后的关键词、压缩后的核心词、原始问句。"""
    cleaned = _clean_query(query)
    out = [cleaned]
    simple = _simplify(cleaned)
    if simple and simple not in out:
        out.append(simple)
    raw = re.sub(r"\s+", " ", (query or "").strip())
    if raw and raw not in out:
        out.append(raw)
    return out[:3]


def _grams(text: str):
    """把文本切成二元组，用来衡量搜索结果与问题的相关度。"""
    text = re.sub(r"[\s\W_]+", "", (text or "").lower())
    if not text:
        return set()
    if len(text) == 1:
        return {text}
    return {text[i:i + 2] for i in range(len(text) - 1)}


def _relevance(text: str, grams) -> float:
    if not grams:
        return 0.0
    hit = sum(1 for g in grams if g in text)
    return hit / len(grams)


def _score_set(items, grams) -> float:
    """用前三条结果的平均相关度衡量这组结果好不好。"""
    if not items:
        return 0.0
    scores = sorted(
        (
            _relevance(f"{it.get('title') or ''} {it.get('snippet') or ''}", grams)
            for it in items[:5]
        ),
        reverse=True,
    )
    return sum(scores[:3]) / min(3, len(scores))


def _fetch_results(q: str, limit: int, timeout: int):
    for base in ("https://cn.bing.com/search", "https://www.bing.com/search"):
        try:
            found = _bing_html(f"{base}?q={urllib.parse.quote(q)}&mkt=zh-CN", limit, timeout)
        except Exception:
            found = []
        if found:
            return found
    for base in ("https://cn.bing.com/search", "https://www.bing.com/search"):
        try:
            found = _bing_rss(f"{base}?format=rss&q={urllib.parse.quote(q)}", limit, timeout)
        except Exception:
            found = []
        if found:
            return found
    return []


def search_web(query: str, limit: int = 5, timeout: int = 12):
    query = (query or "").strip()
    if not query:
        return []
    key = query[:120]
    cached = _cache.get(key)
    if cached and time.time() - cached[0] < _CACHE_TTL:
        return cached[1]

    grams = _grams(_simplify(_clean_query(query))) or _grams(query)
    best = []
    best_score = 0.0
    # 依次试不同检索词，取相关度最高的一组结果
    for q in _build_queries(query):
        found = _fetch_results(q, limit, timeout)
        score = _score_set(found, grams)
        if found and (not best or score > best_score):
            best, best_score = found, score
        if best_score >= 0.55:
            break

    results = _prepare(best, grams, limit)
    if results:
        _cache[key] = (time.time(), results)
    return results


def _prepare(results, grams, limit: int):
    """过滤广告/重复结果，按与问题的相关度排序，并补充前两条网页正文摘录。"""
    cleaned = []
    seen = set()
    for item in results:
        url = item.get("url") or ""
        if any(d in url for d in _DROP_DOMAINS):
            continue
        host = urllib.parse.urlparse(url).netloc.lower()
        title = item.get("title") or ""
        sig = (host, re.sub(r"[\s\W_]+", "", title)[:30])
        if sig in seen:
            continue
        seen.add(sig)
        snippet = item.get("snippet") or ""
        # 标题命中比摘要命中更能说明相关
        score = _relevance(title, grams) * 2 + _relevance(snippet, grams)
        cleaned.append((score, item))

    if not cleaned:
        return []
    cleaned.sort(key=lambda x: x[0], reverse=True)
    top = cleaned[0][0]
    # 只保留与最相关结果接近的条目，避免把无关内容塞给模型
    picked = [item for score, item in cleaned if score >= top * 0.6][:limit]
    if len(picked) < 2:
        picked = [item for _, item in cleaned[:limit]]

    # 并行抓取前两条网页正文，补充更具体的信息
    def _fill(item):
        try:
            item["excerpt"] = _fetch_excerpt(item.get("url") or "")
        except Exception:
            item["excerpt"] = ""

    threads = [threading.Thread(target=_fill, args=(it,), daemon=True) for it in picked[:2]]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=7)
    return picked


_TAG_RE = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.S | re.I)


def _fetch_excerpt(url: str, timeout: int = 6) -> str:
    """抓取网页正文前若干字，失败返回空串。"""
    if not url.startswith("http"):
        return ""
    try:
        raw = _fetch(url, timeout)
    except Exception:
        return ""
    try:
        text = raw.decode("utf-8", "ignore")
    except Exception:
        return ""
    charset = re.search(r"charset=[\"']?([\w-]+)", text[:2000], re.I)
    if charset and charset.group(1).lower() in ("gbk", "gb2312", "gb18030"):
        try:
            text = raw.decode("gb18030", "ignore")
        except Exception:
            pass
    text = _TAG_RE.sub(" ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) < 80:
        return ""
    # 去掉导航/页脚常见噪声：取正文中段
    return text[:900]


def _fetch(url: str, timeout: int) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _decode_link(link: str) -> str:
    """解开必应跳转链接（/ck/a?...&u=a1<base64>）。"""
    if "bing.com/ck/a" not in link:
        return link
    try:
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(link).query)
        raw = (qs.get("u") or [""])[0]
        if raw.startswith("a1"):
            raw = raw[2:]
        raw += "=" * (-len(raw) % 4)
        url = base64.urlsafe_b64decode(raw.encode()).decode("utf-8", "ignore")
        if url.startswith("http"):
            return url
    except Exception:
        pass
    return link


_BLOCK_RE = re.compile(r'<li class="b_algo[^"]*".*?</li>', re.S)
_TITLE_RE = re.compile(r"<h2[^>]*>\s*<a[^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>", re.S)
_P_RE = re.compile(r"<p[^>]*>(.*?)</p>", re.S)


def _bing_html(url: str, limit: int, timeout: int):
    data = _fetch(url, timeout).decode("utf-8", "ignore")
    items = []
    for block in _BLOCK_RE.findall(data):
        m = _TITLE_RE.search(block)
        if not m:
            continue
        title = _clean(m.group(2))
        link = _decode_link(html.unescape(m.group(1)).strip())
        if not title or not link.startswith("http"):
            continue
        snippet = ""
        pm = _P_RE.search(block)
        if pm:
            snippet = _clean(pm.group(1))
        items.append({"title": title[:120], "url": link, "snippet": snippet[:300]})
        if len(items) >= limit:
            break
    return items


def _bing_rss(url: str, limit: int, timeout: int):
    try:
        root = ET.fromstring(_fetch(url, timeout))
    except ET.ParseError:
        return []
    items = []
    for item in root.iter("item"):
        title = _clean(item.findtext("title") or "")
        link = (item.findtext("link") or "").strip()
        desc = _clean(item.findtext("description") or "")
        if not title or not link:
            continue
        if link.startswith("http://www.bing.com") or "go.microsoft.com" in link:
            continue
        items.append({"title": title[:120], "url": link, "snippet": desc[:300]})
        if len(items) >= limit:
            break
    return items


def _clean(text: str) -> str:
    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", text).strip()


# 需要联网才能答准的关键词：时效性、实时数据、具体事实查询
_AUTO_KEYWORDS = (
    "今天", "现在", "当前", "目前", "最新", "最近", "新闻", "消息", "热点",
    "价格", "多少钱", "股价", "汇率", "金价", "油价", "房价", "天气", "气温",
    "比分", "赛况", "赛程", "排行", "排名", "票房", "销量", "市值", "发布",
    "上市", "今年", "去年", "明年", "几月", "几号", "星期几", "日期", "时间",
    "谁", "什么时候", "哪一年", "哪里", "在哪", "多少", "是否", "有没有",
    "最新版本", "官网", "下载",
    "today", "now", "latest", "news", "price", "weather", "who", "when",
    "release", "version",
)


_PURE_DATE_RE = re.compile(
    r"^(请问)?\s*(现在|今天)?\s*(是)?\s*(几号|几月几号|星期几|礼拜几|几点|多少号|什么日子|什么日期|什么时间)"
)


def should_search(query: str) -> bool:
    """判断问题是否值得先联网检索（启发式，尽量覆盖时效性问题）。"""
    text = (query or "").strip()
    if not text:
        return False
    if len(text) > 200:
        return False
    # 纯问日期/时间的问题，直接用注入的当前时间回答，不必联网
    if _PURE_DATE_RE.search(text) and not any(
        k in text for k in ("天气", "新闻", "股价", "赛事", "节日", "放假", "农历")
    ):
        return False
    low = text.lower()
    if any(k in low for k in _AUTO_KEYWORDS):
        return True
    # 含 4 位年份（如 2025/2026）通常涉及时效信息
    if re.search(r"(19|20)\d{2}", text):
        return True
    return False


def build_material(results) -> str:
    """把搜索结果整理成资料块，直接放进用户消息，小模型更容易照着回答。"""
    lines = ["以下是我刚从网上搜索到的资料（可能包含最新、有时效性的信息）：", ""]
    for index, item in enumerate(results, 1):
        title = (item.get("title") or "").strip()
        snippet = (item.get("snippet") or "").strip()
        excerpt = (item.get("excerpt") or "").strip()
        url = (item.get("url") or "").strip()
        lines.append(f"[{index}] {title}")
        if snippet:
            lines.append(f"摘要：{snippet[:220]}")
        if excerpt:
            lines.append(f"正文摘录：{excerpt[:500]}")
        if url:
            lines.append(f"链接：{url}")
        lines.append("")
    lines.append(
        "回答要求：\n"
        "1. 只要资料里有答案，就直接用资料的内容回答，不要凭记忆编造；\n"
        "2. 回答末尾用「参考：[编号]」列出实际用到的资料，例如「参考：[1][2]」；\n"
        "3. 资料没覆盖的部分，直接说明「资料中未找到相关内容」；\n"
        "4. 不要复述资料原文，直接给出结论；\n"
        "5. 只回答一次，不要重复同一段内容。"
    )
    return "\n".join(lines)
