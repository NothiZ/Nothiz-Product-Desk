# -*- coding: utf-8 -*-
"""产品文案生成：一次 LLM 调用同时产出 中/英 标题 + 详情，再做本地清洗。"""
import json
import re
import threading

from llm_client import call_llm

_CN = ["核心特点", "功能", "材质", "结构", "做工细节", "使用方式",
       "适用场景", "清洁维护", "包装配件", "兼容性", "风格", "版型", "舒适度"]
_EN = ["Key Features", "Function", "Material", "Build", "Design Details", "How to Use",
       "Use Cases", "Care", "In the Box", "Compatibility", "Style", "Fit", "Comfort"]
_ZH2EN, _EN2ZH = dict(zip(_CN, _EN)), dict(zip(_EN, _CN))

_STOP_STYLE = {
    "color", "colour", "colors", "colours", "size", "sizes", "sizing",
    "black", "white", "red", "blue", "green", "brown", "beige", "grey", "gray",
    "pink", "yellow", "purple", "navy", "khaki", "camel",
    "s", "m", "l", "xl", "xxl", "xs", "2xl", "3xl", "small", "medium", "large", "free",
}
_SENSITIVE_FIELDS = {
    "产地", "原产地", "发货地", "制造地", "定制", "商标", "牌子", "品牌", "型号",
    "origin", "place of origin", "made in", "brand", "brand name", "logo",
    "custom", "customization", "customisation", "model", "model number", "model no",
}
_COLOR_SIZE_KEYS = ("color", "colour", "颜色", "size", "sizing", "尺码", "尺寸", "规格")


# ── 文本清洗 ──────────────────────────────────────────────────────────────────

def _tidy(s: str) -> str:
    s = re.sub(r"\s{2,}", " ", s).strip()
    s = re.sub(r"^(\*\*|__)(.*?)\1$", r"\2", s).strip()
    return s if re.search(r"[^\W_]", s) else ""


def _is_color_size_enum(line: str) -> bool:
    """只过滤“颜色：红、黄、蓝”这类纯枚举行；描述性用法（如“深色系百搭”）保留。"""
    m = re.match(r"^(.{1,16}?)\s*[:\uFF1A]\s*(.+)$", (line or "").strip())
    if not m or not any(k in m.group(1).lower() for k in _COLOR_SIZE_KEYS):
        return False
    val = m.group(2)
    if re.search(r"[。！？.!?]", val):
        return False
    tokens = [t for t in re.split(r"[,，、/\s]+", val.strip()) if t]
    return len(tokens) >= 2 and all(len(t) <= 8 for t in tokens)


def _is_sensitive_line(line: str) -> bool:
    # Only explicit attribute names are filtered; prose is selected by the prompt.
    m = re.match(r"^\s*[-*#•\s]*([^:：]+?)\s*[:：]", line or "")
    if not m:
        return False
    key = re.sub(r"\s+", " ", m.group(1).strip(" *#").rstrip(".")).casefold()
    return key in _SENSITIVE_FIELDS


def _cap_sentences(text: str, lang: str, n: int = 2) -> str:
    s = (text or "").strip()
    if not s:
        return s
    if lang == "zh":
        parts = re.split(r"([。！？；])", s)
        out, sent = [], 0
        for i in range(0, len(parts) - 1, 2):
            seg = (parts[i] + parts[i + 1]).strip()
            if seg:
                out.append(seg)
                sent += 1
                if sent >= n:
                    break
        return "".join(out).strip() or s
    # 英文只在真正的句末断句，排除小数点（4.32）和缩写（U.S.）
    ends = [m.end() for m in re.finditer(r"(?<!\d)(?<![A-Z])[.!?;](?=\s+[A-Z]|\s*$)", s)]
    return s if not ends or n >= len(ends) + 1 else s[:ends[n - 1]].strip()


def _style_keywords(url_keywords: list) -> list[str]:
    out = []
    for kw in url_keywords:
        kw = _tidy(str(kw))
        if kw and not _is_color_size_enum(kw) and kw.lower() not in _STOP_STYLE:
            out.append(kw)
    return list(dict.fromkeys(out))


def _extract_sections(text: str, labels: tuple[str, ...]) -> dict[str, str]:
    text = re.sub(r"```[a-zA-Z]*\n?", "", text or "").strip()
    out = dict.fromkeys(labels, "")
    cur, buf = None, []
    for line in text.splitlines():
        m = re.match(r"^\s*[*#\s]*([A-Za-z]+)[*\s]*[:：]\s*(.*?)\s*$", line)
        if m and m.group(1) in labels:
            if cur:
                out[cur] = "\n".join(buf).strip()
            value = m.group(2).strip()
            if line.lstrip(" #").startswith("**") and value.startswith("**"):
                value = value[2:].strip()
            cur, buf = m.group(1), ([value] if value else [])
        elif cur is not None:
            buf.append(line.rstrip())
    if cur:
        out[cur] = "\n".join(buf).strip()
    return out


def _normalize_points(text: str, lang: str, max_points: int = 10) -> str:
    """规范化参数列表，过滤明确属性字段，不按关键词修改正文或补造参数。"""
    zh = lang == "zh"
    key_re = r"^([^:：]{1,32}?)\s*[:：]\s*(.*)$"
    label_map = _EN2ZH if zh else _ZH2EN
    counts: dict[str, int] = {}
    lines = []
    for raw in (text or "").splitlines():
        ln = raw.strip()
        if not ln or _is_sensitive_line(ln):
            continue
        m = re.match(key_re, re.sub(r"^[\-\*\u2022]\s*", "", ln))
        if not m:
            continue
        k = re.sub(r"\s{2,}", " ", m.group(1).strip())
        k = re.sub(r"^[\-\*\u2022\s]+", "", k)
        k = re.sub(r"[\-–—\|/]+$", "", k).strip()
        k = label_map.get(k[:14] if zh else k[:32], k[:14] if zh else k[:32])
        v = _tidy(m.group(2))
        if not k or not v or _is_color_size_enum(v):
            continue
        counts[k] = counts.get(k, 0) + 1
        if counts[k] > 2:
            continue
        v = _cap_sentences(v, lang)[:120 if zh else 160].rstrip()
        lines.append(f"{k}：{v}" if zh else f"{k}: {v}")
        if len(lines) >= max_points:
            break
    return "\n".join(lines)


# ── Prompt（全部静态，import 时拼好）────────────────────────────────────────────

_PROMPT = """根据以下产品信息，一次性生成中文标题、中文详情、英文标题、英文详情，共四个字段。

【内容筛选规则】
- 仅使用输入中有依据的产品事实，不编造缺失的材质、功能或参数，不用通用套话补齐条目。
- 由你根据上下文去除品牌名、平台名、促销话术、产地、产品型号，以及批发、代工、OEM/ODM、贴牌和LOGO定制服务内容；这些规则同时适用于标题和详情。
- 不要把大写词或标题首词自动认作品牌。USB、LED、LCD、PVC、TPU、ABS、GPS、RGB、HD、UV、PU、EVA 等技术或材质词应按产品事实保留。
- 保留有意义的设计、版型、材质和兼容性描述。Original design、Customer favorite、Custom fit、Off-shoulder、bone china 等表达不等于产地、定制服务或促销信息，须结合上下文判断，不能按词的一部分删除。

【中文标题规则】
- 取材于 Page Title，提炼核心产品词，简短适合独立站展示。
- 去掉平台词/促销词（淘宝、天猫、包邮、OEM等）及品牌名。
- Style hints 非空时，融入至少一个非颜色/非尺寸词使标题更具体。
- 不要有年份出现。

【中文详情规则】
{cn_rules}

【英文标题规则】
- 将中文标题直译为英文，不改写不增删。

【英文详情规则】
{en_rules}

【产品输入】
Page Title (raw): {raw_title}
Page Title (clean): {clean_title}
Style hints: {style}
URL Keywords: {keywords}
Extracted details: {details}

【严格按此格式输出，不加任何解释或多余字段】
四个字段都必须有有效内容，不能只输出标签或分段标记。标签使用英文冒号，不加 Markdown 加粗或代码块。
ChineseTitle:
ChineseDescription:
EnglishTitle:
EnglishDescription:"""

_RULES = {
    "template1": (
        "- 格式：类目名称：内容（一行一条，不留空行）。\n"
        "- 默认 4-6 条；信息丰富可写到 8-10 条，不凑数。\n"
        f"- 类目参考：{', '.join(_CN[:10])}\n"
        "- 说明性语气；不列举颜色/尺寸枚举。\n"
        "- 不要写入产品型号（如“型号：XXX”），正文里也不要提及型号。\n"
        "- 【绝对禁忌】严禁写入任何涉及“支持定制”、“提供LOGO定制”、“OEM/ODM”、“贴牌”、“代工”、“批发”等面向B端商家的内容！不要出现年份。\n"
        "- 示例：\n" + "\n".join(f"{k}：一句话说明" for k in _CN[:5]),
        "- 将中文详情逐行翻译，行数顺序完全一致。\n"
        f"- 类目名对照：{json.dumps(_ZH2EN, ensure_ascii=False)}\n"
        "- 中文冒号\"：\"改为英文冒号\": \"；内容忠实翻译，不精简。\n"
        "- Do NOT include model numbers (e.g. \"Model Number: XXX\") anywhere.\n"
        "- 【绝对禁忌】严禁出现 \"customization\", \"logo printing\", \"wholesale\" 等任何定制或B端导向词汇！",
    ),
    "template2": (
        "分两部分，格式严格如下（含“详情”与“规格”标记行）：\n\n"
        "详情\n（3-5句介绍性段落：描述产品整体定位、核心功能和使用场景，语气自然，不夸大，不要有年份。）\n\n"
        "规格\n（逐行列出关键参数，格式为\"参数名：值\"，4-8条。）\n\n"
        "- 【绝对禁忌】规格中严禁出现“原产地”、“产地”、“发货地”、“制造地”（如：产地：中国 等）信息！"
        "严禁出现任何“定制logo”、“支持OEM”、“批发”等面向B端订货的内容！\n"
        "- 【绝对禁忌】不要写入产品型号（如“型号：XXX”），详情和规格里都不要出现型号。",
        "分两部分，格式严格如下（含“Details”与“Specifications”标记行）：\n\n"
        "Details\n（将中文\"详情\"段落忠实翻译为英文，3-5句，流畅自然。）\n\n"
        "Specifications\n（将中文\"规格\"逐行翻译，格式为\"Parameter: Value\"，行数与中文完全一致。）\n\n"
        "- 【绝对禁忌】严禁在 Specifications 中输出 \"Place of Origin\", \"Origin\", \"Made in China\" 或者是 "
        "\"Customization\" 等敏感的产地及定制词汇！\n"
        "- Do NOT include model numbers (e.g. \"Model Number: XXX\") in Details or Specifications.",
    ),
}


def _split_template2(text: str, tag_intro: str, tag_spec: str, lang: str) -> str:
    """拆成「详情段落」+「规格列表」。只有整行等于标记词才算标记行，
    避免正文里出现“规格”“Details”等字样时被误判成分段标记。"""
    def is_tag(ln: str, tag: str) -> bool:
        return ln.strip(" :：*#").lower() == tag.lower()

    intro, spec, in_spec = [], [], False
    for ln in (x.strip() for x in text.splitlines() if x.strip()):
        if is_tag(ln, tag_intro):
            continue
        if is_tag(ln, tag_spec):
            in_spec = True
        elif in_spec:
            spec.append(ln)
        else:
            if not _is_sensitive_line(ln):
                intro.append(_tidy(ln))
    points = _normalize_points("\n".join(spec), lang, 8)
    if not any(intro) and not points:
        return ""
    return f"{tag_intro}\n" + "\n".join(intro) + f"\n\n{tag_spec}\n" + points


# ── 标题去重（线程安全）──────────────────────────────────────────────────────

class TitleDedupe:
    """跨产品标题去重。可用已有结果初始化，续跑时不会和上一轮的标题撞车。"""

    def __init__(self, cn=(), en=()):
        self._lock = threading.Lock()
        self._seen = {"cn": {t for t in cn if t}, "en": {t for t in en if t}}

    def unique(self, kind: str, title: str) -> str:
        if not title:
            return title
        cjk = kind == "cn"
        with self._lock:
            seen = self._seen[kind]
            # Raw URL hints have not passed the model's content selection.
            cands = [title] + [f"{title}{i}" if cjk else f"{title} {i}" for i in range(2, 1000)]
            pick = next(c for c in cands if c not in seen)
            seen.add(pick)
            return pick


# ── 主入口 ────────────────────────────────────────────────────────────────────

def generate_content(product: dict, template: str = "template1", dedupe: TitleDedupe | None = None) -> dict:
    """返回 {"title_cn", "title_en", "desc_cn", "desc_en"}。template: template1 | template2"""
    template = template if template in _RULES else "template1"
    raw_title = str(product.get("page_title_raw") or "").strip()
    clean_title = str(product.get("title") or "").strip()
    url_kw = product.get("url_keywords") or []
    style = _style_keywords(url_kw)
    details = "\n".join(ln for ln in str(product.get("description") or "").strip().splitlines()
                        if not _is_color_size_enum(ln))

    cn_rules, en_rules = _RULES[template]
    prompt = _PROMPT.format(
        cn_rules=cn_rules, en_rules=en_rules, raw_title=raw_title, clean_title=clean_title,
        style=", ".join(style), keywords=", ".join(str(x) for x in url_kw if str(x).strip()), details=details,
    )
    sec = _extract_sections(call_llm(prompt, temperature=0.2),
                            ("ChineseTitle", "ChineseDescription", "EnglishTitle", "EnglishDescription"))

    title_cn = _tidy(sec["ChineseTitle"])
    title_en = _tidy(sec["EnglishTitle"])
    if template == "template1":
        desc_cn = _normalize_points(sec["ChineseDescription"], "zh", 10)
        desc_en = _normalize_points(sec["EnglishDescription"], "en", 10)
    else:
        desc_cn = _split_template2(sec["ChineseDescription"], "详情", "规格", "zh")
        desc_en = _split_template2(sec["EnglishDescription"], "Details", "Specifications", "en")

    # Validate before deduplication so failed rows cannot consume valid titles.
    missing = [name for name, value in (("ChineseTitle", title_cn), ("EnglishTitle", title_en),
                                       ("ChineseDescription", desc_cn), ("EnglishDescription", desc_en)) if not value]
    if missing:
        raise ValueError("AI 输出缺少有效内容：" + ", ".join(missing))

    if dedupe:
        title_cn = dedupe.unique("cn", title_cn)
        title_en = dedupe.unique("en", title_en)
    return {"title_cn": title_cn, "title_en": title_en, "desc_cn": desc_cn, "desc_en": desc_en}
