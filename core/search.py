# -*- coding: utf-8 -*-
"""联网搜索：多引擎召回 + 实体过滤 + 相关性重排，给本地模型提供可靠资料"""
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

# 用户明确要求搜索的说法
_SEARCH_INTENT = (
    "搜索", "搜一下", "搜下", "查一下", "查下", "帮我查", "帮我搜",
    "联网查", "网上查", "查询一下", "搜搜", "查查",
)

_DROP_DOMAINS = ("bing.com", "microsoft.com/zh-cn/search", "go.microsoft.com", "so.com/link")

# 问句里对检索没帮助的尾巴，去掉后更容易命中实体页
_QUERY_TAILS = (
    "举办地", "举办国家", "举办城市", "举办时间", "举办地点", "在哪个国家",
    "在哪里", "是哪", "是哪个", "价格是多少", "是什么",
)

# 英文虚词，不作为检索实体
_LATIN_STOP = {
    "the", "and", "for", "what", "when", "how", "with", "from", "this", "that",
    "are", "was", "were", "is", "of", "to", "in", "on", "at", "by", "or", "an",
}


# ---------- 基础文本处理 ----------
def _clean(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _decode_page(raw: bytes) -> str:
    head = raw[:3000].decode("ascii", "ignore").lower()
    match = re.search(r"charset=[\"']?([\w-]+)", head)
    enc = (match.group(1) if match else "utf-8").lower()
    if enc in ("gbk", "gb2312", "gb18030"):
        enc = "gb18030"
    try:
        return raw.decode(enc, "ignore")
    except Exception:
        return raw.decode("utf-8", "ignore")


def _clean_query(query: str) -> str:
    text = (query or "").strip()
    for word in _SEARCH_INTENT + _FILLERS:
        text = text.replace(word, " ")
    text = re.sub(r"[？?！!。，,；;：:、\s]+", " ", text)
    # 中英文/数字之间补空格，搜索引擎分词更准（A380显卡 -> A380 显卡）
    text = re.sub(r"([A-Za-z0-9])([\u4e00-\u9fff])", r"\1 \2", text)
    text = re.sub(r"([\u4e00-\u9fff])([A-Za-z0-9])", r"\1 \2", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or (query or "").strip()


def _simplify(text: str) -> str:
    """把「2026 年世界杯举办地」这类问句压成核心关键词「2026世界杯」。"""
    text = re.sub(r"(?<=\d)\s*(年|月|日|号)", "", text)
    for word in _QUERY_TAILS:
        text = text.replace(word, " ")
    return re.sub(r"\s+", " ", text).strip()


def _entities(text: str):
    """提取产品名/型号等实体（Intel、A380、RTX4090、2026），检索时作为硬条件。"""
    tokens = re.findall(r"[A-Za-z]+[A-Za-z0-9\-]*|\d{2,}", text or "")
    out = []
    for token in tokens:
        low = token.lower()
        if low in _LATIN_STOP or len(low) < 2:
            continue
        if token not in out:
            out.append(token)
    return out[:6]


_CATEGORY_WORDS = (
    "显卡", "主板", "内存", "硬盘", "处理器", "笔记本", "台式机", "手机", "平板",
    "相机", "镜头", "耳机", "音箱", "手表", "电视", "显示器", "路由器", "游戏",
    "汽车", "电动车", "打印机", "nas", "cpu", "gpu",
)


def _model_variant(query: str) -> str:
    """构造以型号为核心的检索词，例如「A380显卡」；型号是最不容易跑偏的关键词。"""
    models = [e for e in _entities(query) if any(ch.isdigit() for ch in e)]
    if not models:
        return ""
    core = models[-1]
    text = _clean_query(query)
    category = next((w for w in _CATEGORY_WORDS if w in text.lower()), "")
    if category:
        # 型号 + 类别词，去掉价格/评测等泛词，避免被带偏
        return core + category
    # 没有类别词时，至少保留「型号 + 其余实体」，例如 iPhone 17
    rest = [e for e in _entities(query) if e.lower() != core.lower()]
    if rest and len(core) >= 2:
        return " ".join(rest[:1] + [core])
    return "" if len(core) < 3 else core


def _build_queries(query: str):
    """构造多个检索词；含型号等实体时，丢弃丢失实体的变体，避免越搜越偏。"""
    cleaned = _clean_query(query)
    raw = re.sub(r"\s+", " ", (query or "").strip())
    variants = [cleaned]
    simple = _simplify(cleaned)
    if simple and simple not in variants:
        variants.append(simple)
    model = _model_variant(query)
    if model and model not in variants:
        variants.append(model)
    if raw and raw not in variants:
        variants.append(raw)

    entities = _entities(raw)
    if entities:
        kept = [v for v in variants if any(e.lower() in v.lower() for e in entities)]
        variants = kept or [cleaned]
    return variants[:4]


def _grams(text: str):
    """二元组，用来衡量结果与问题的相关度。"""
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


# ---------- 网络请求 ----------
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


# ---------- 必应 ----------
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
    data = _decode_page(_fetch(url, timeout))
    items = []
    for block in _BLOCK_RE.findall(data):
        match = _TITLE_RE.search(block)
        if not match:
            continue
        title = _clean(match.group(2))
        link = _decode_link(html.unescape(match.group(1)).strip())
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


def _bing_search(query: str, limit: int, timeout: int):
    for base in ("https://cn.bing.com/search", "https://www.bing.com/search"):
        try:
            items = _bing_html(
                f"{base}?q={urllib.parse.quote(query)}&mkt=zh-CN", limit, timeout
            )
        except Exception:
            items = []
        if items:
            return items
    for base in ("https://cn.bing.com/search", "https://www.bing.com/search"):
        try:
            items = _bing_rss(
                f"{base}?format=rss&q={urllib.parse.quote(query)}", limit, timeout
            )
        except Exception:
            items = []
        if items:
            return items
    return []


# ---------- 360 搜索 ----------
_SO_BLOCK_RE = re.compile(r'<li class="res-list".*?</li>', re.S)
_SO_TITLE_RE = re.compile(
    r'<h3 class="res-title[^"]*"[^>]*>\s*<a([^>]*)>(.*?)</a>', re.S
)
_SO_DESC_RE = re.compile(r'<p class="res-desc"[^>]*>(.*?)</p>', re.S)


def _so_search(query: str, limit: int, timeout: int):
    url = "https://www.so.com/s?q=" + urllib.parse.quote(query)
    data = _decode_page(_fetch(url, timeout))
    items = []
    for block in _SO_BLOCK_RE.findall(data):
        match = _SO_TITLE_RE.search(block)
        if not match:
            continue
        attrs, raw_title = match.group(1), match.group(2)
        md = re.search(r'data-mdurl="([^"]+)"', attrs)
        href = re.search(r'href="([^"]+)"', attrs)
        link = html.unescape((md.group(1) if md else (href.group(1) if href else ""))).strip()
        if not link.startswith("http") or "so.com/link" in link:
            continue
        title = _clean(raw_title)
        if not title:
            continue
        desc = _SO_DESC_RE.search(block)
        snippet = _clean(desc.group(1)) if desc else ""
        items.append({"title": title[:120], "url": link, "snippet": snippet[:300]})
        if len(items) >= limit:
            break
    return items


# ---------- 多引擎并行召回 ----------
_ENGINES = (("bing", _bing_search), ("360", _so_search))


def _multi_search(query: str, limit: int, timeout: int):
    items = []
    lock = threading.Lock()

    def run(name, func):
        try:
            found = func(query, limit, timeout)
        except Exception:
            found = []
        with lock:
            for item in found:
                item = dict(item)
                item["engine"] = name
                items.append(item)

    threads = [
        threading.Thread(target=run, args=(name, func), daemon=True)
        for name, func in _ENGINES
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=timeout)
    return items


def _merge(items):
    """按 URL 去重，同一结果被多个引擎返回时加分。"""
    merged = {}
    for item in items:
        url = (item.get("url") or "").strip()
        if not url.startswith("http"):
            continue
        key = url.split("#")[0].rstrip("/")
        engines = item.get("engine") or ""
        current = merged.get(key)
        if current:
            for name in engines.split(","):
                if name and name not in current["engines"].split(","):
                    current["engines"] += "," + name
            if len(item.get("snippet") or "") > len(current.get("snippet") or ""):
                current["snippet"] = item["snippet"]
        else:
            merged[key] = {
                "title": item.get("title") or "",
                "url": url,
                "snippet": item.get("snippet") or "",
                "engines": engines,
            }
    return list(merged.values())


def _rank(items, grams, entities):
    """实体硬过滤 + 标题/摘要相关度打分 + 多引擎一致加成。"""
    # 有带数字的型号（A380、17、4090）时优先按型号过滤，避免搜到同名大类首页
    model_tokens = [e for e in entities if any(ch.isdigit() for ch in e)]
    required = model_tokens or entities
    scored = []
    for item in items:
        title = item.get("title") or ""
        snippet = item.get("snippet") or ""
        text = f"{title} {snippet}".lower()
        if required and not any(e.lower() in text for e in required):
            # 结果里连型号/产品名都没有，几乎肯定跑题
            continue
        agreement = max(0, len([e for e in (item.get("engines") or "").split(",") if e]) - 1)
        score = (
            _relevance(title, grams) * 2
            + _relevance(snippet, grams)
            + 0.12 * agreement
        )
        scored.append((score, item))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return scored


# ---------- 对外接口 ----------
_TAG_RE = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.S | re.I)


def _fetch_excerpt(url: str, timeout: int = 6) -> str:
    """抓取网页正文前若干字，失败返回空串。"""
    if not url.startswith("http"):
        return ""
    try:
        raw = _fetch(url, timeout)
    except Exception:
        return ""
    text = _decode_page(raw)
    text = _TAG_RE.sub(" ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) < 80:
        return ""
    return text[:900]


def _prepare(scored, limit: int):
    """取与最相关结果接近的条目，并补充前两条网页正文摘录。"""
    if not scored:
        return []
    top = scored[0][0]
    picked = [item for score, item in scored if score >= max(0.25, top * 0.55)][:limit]
    if len(picked) < 2:
        picked = [item for _, item in scored[:limit]]
    picked = [dict(item) for item in picked]

    def fill(item):
        try:
            item["excerpt"] = _fetch_excerpt(item.get("url") or "")
        except Exception:
            item["excerpt"] = ""

    threads = [
        threading.Thread(target=fill, args=(item,), daemon=True)
        for item in picked[:2]
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=7)
    return picked


def search_web(query: str, limit: int = 5, timeout: int = 12):
    query = (query or "").strip()
    if not query:
        return []
    key = query[:120]
    cached = _cache.get(key)
    if cached and time.time() - cached[0] < _CACHE_TTL:
        return cached[1]

    grams = _grams(_simplify(_clean_query(query))) or _grams(query)
    entities = _entities(query)
    best = []
    pool = []
    for variant in _build_queries(query):
        scored = _rank(_merge(_multi_search(variant, limit, timeout)), grams, entities)
        pool.extend(scored)
        if scored and (not best or scored[0][0] > best[0][0]):
            best = scored
        # 只有标题几乎完全命中才提前结束，否则继续换检索词再试
        if best and best[0][0] >= 1.8:
            break

    # 最合适的检索词结果太少时，把其它检索词的结果也合进来补充
    if len(best) < 2 and pool:
        pool.sort(key=lambda pair: pair[0], reverse=True)
        seen = set()
        merged = []
        for score, item in pool:
            key = (item.get("url") or "").split("#")[0].rstrip("/")
            if key in seen:
                continue
            seen.add(key)
            merged.append((score, item))
        best = merged

    results = _prepare(best, limit)
    if results:
        _cache[key] = (time.time(), results)
    return results


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
