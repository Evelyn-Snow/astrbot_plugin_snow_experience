"""Bot 真实经历插件的纯逻辑（不依赖 AstrBot，可单独测试）。

覆盖：每周配额、触发时刻（按日期种子确定）、书架选书、txt/epub/pdf/docx 文本抽取、
Gutendex/arXiv 结果解析、事件记录渲染与聊天注入摘要。
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import html as _html
import re
import zipfile
from xml.etree import ElementTree

SUPPORTED_DOC_SUFFIXES = (".txt", ".epub", ".pdf", ".docx", ".md", ".html", ".htm")
PRIVATE_DOC_SUFFIXES = (".caj", ".kdh", ".nh")
MAX_DOC_CHARS = 4_000_000
PDF_MAX_PAGES = 400

# ---------------- 基础工具 ----------------


def week_key(ts: float | None = None) -> str:
    """ISO 周键，如 2026-W45；配额按周重置。"""
    if ts is None:
        import time

        ts = time.time()
    year, week, _ = _dt.datetime.fromtimestamp(ts).isocalendar()
    return f"{year}-W{week:02d}"


def deterministic_float(seed: str) -> float:
    """按种子的确定性伪随机（0..1），用于每天固定触发时刻。"""
    digest = hashlib.md5(str(seed).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") / float(1 << 64)


def _parse_window(spec: str) -> tuple[int, int] | None:
    match = re.match(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$", str(spec or ""))
    if not match:
        return None
    h1, m1, h2, m2 = map(int, match.groups())
    start, end = h1 * 60 + m1, h2 * 60 + m2
    if end <= start or max(start, end) >= 1440:
        return None
    return start, end


def event_time_for_date(
    date_str: str, window_spec: str, chance: float
) -> tuple[int, int] | None:
    """返回今天事件的触发分钟数（时:分元组），触发概率不足或窗口非法返回 None。

    同一天种子相同 → 触发时刻固定，重启不会重排。
    """
    roll = deterministic_float(f"experience::date::{date_str}")
    if roll >= max(0.0, min(1.0, float(chance))):
        return None
    window = _parse_window(window_spec)
    if window is None:
        return None
    start, end = window
    span = max(1, end - start)
    offset = int(deterministic_float(f"experience::time::{date_str}") * span)
    minute = start + offset
    return minute // 60, minute % 60


# ---------------- 每周配额 ----------------

_EVENT_TYPES = ("book", "research", "paper")


def quota_used(quotas: dict, week: str, event_type: str) -> int:
    data = quotas.get(week) if isinstance(quotas, dict) else None
    if not isinstance(data, dict):
        return 0
    try:
        return max(0, int(data.get(event_type) or 0))
    except (TypeError, ValueError):
        return 0


def quota_allow(
    quotas: dict, week: str, event_type: str, weekly_limits: dict[str, int]
) -> tuple[bool, int]:
    """返回 (是否允许, 剩余额度)。未知类型不允许。"""
    if event_type not in _EVENT_TYPES:
        return False, 0
    limit = max(0, int(weekly_limits.get(event_type) or 0))
    used = quota_used(quotas, week, event_type)
    return used < limit, max(0, limit - used)


def quota_record(quotas: dict, week: str, event_type: str) -> dict:
    """消耗一次配额，返回新的 quotas dict（跨周自动重置旧键）。"""
    data = quotas if isinstance(quotas, dict) else {}
    current = data.get(week)
    current = current if isinstance(current, dict) else {}
    current[event_type] = quota_used(data, week, event_type) + 1
    cleaned = {k: v for k, v in data.items() if k == week}
    cleaned[week] = current
    return cleaned


def pick_event_type(
    quotas: dict, week: str, weekly_limits: dict[str, int], roll: float
) -> str | None:
    """按剩余配额加权抽一种事件类型；全部用完返回 None。"""
    weights: list[tuple[str, int]] = []
    for event_type in _EVENT_TYPES:
        allowed, remaining = quota_allow(quotas, week, event_type, weekly_limits)
        if allowed and remaining > 0:
            weights.append((event_type, remaining))
    if not weights:
        return None
    total = sum(weight for _, weight in weights)
    point = float(roll) * total
    for event_type, weight in weights:
        point -= weight
        if point < 0:
            return event_type
    return weights[-1][0]


# ---------------- 书架与阅读 ----------------

READ_CHUNK = 6000


def book_shelf_entry(name: str, index: dict) -> dict:
    entry = (index or {}).get(name)
    return entry if isinstance(entry, dict) else {}


def pick_book(files: list[dict], index: dict) -> dict | None:
    """从书库文件里挑一本：没读过的优先，其次续读（按最近阅读时间），读完的跳过。

    files: [{"name": str, "path": str}]
    """
    unread, partial = [], []
    for file in files:
        entry = book_shelf_entry(file["name"], index)
        finished = bool(entry.get("finished"))
        offset = int(entry.get("offset") or 0)
        if finished:
            continue
        if offset > 0:
            partial.append((entry.get("last_ts") or 0, file))
        else:
            unread.append(file)
    if unread:
        return sorted(unread, key=lambda f: f["name"])[0]
    if partial:
        partial.sort(key=lambda pair: pair[0], reverse=True)
        return partial[0][1]
    return None


def _limit_text(text: str | None) -> str | None:
    text = str(text or "").strip()
    return text[:MAX_DOC_CHARS] if text else None


def _html_text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", raw)
    raw = re.sub(r"(?is)<br\s*/?>|</p>|</div>|</li>|</h[1-6]>", "\n", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    return re.sub(r"[ \t]+", " ", _html.unescape(raw))


def _pdf_text(path: str) -> str:
    """PDF 正文；没装 pypdf 或加密/扫描件无文字层时返回空串。"""
    try:
        from pypdf import PdfReader  # 可选依赖，缺了就退回摘要
    except Exception:
        return ""
    reader = PdfReader(path)
    if getattr(reader, "is_encrypted", False):
        try:
            reader.decrypt("")
        except Exception:
            return ""
    parts, total = [], 0
    for page in list(reader.pages)[:PDF_MAX_PAGES]:
        try:
            piece = page.extract_text() or ""
        except Exception:
            continue
        if piece.strip():
            parts.append(piece)
            total += len(piece)
        if total >= MAX_DOC_CHARS:
            break
    return "\n".join(parts)


def _docx_text_stdlib(path: str) -> str:
    """不依赖 python-docx 的兜底：直接读 word/document.xml 里的段落文字。"""
    with zipfile.ZipFile(path) as zf:
        xml = zf.read("word/document.xml")
    root = ElementTree.fromstring(xml)
    w = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    parts = []
    for para in root.iter(f"{w}p"):
        line = "".join(t.text or "" for t in para.iter(f"{w}t")).strip()
        if line:
            parts.append(line)
    return "\n".join(parts)


def _docx_text(path: str) -> str:
    try:
        from docx import Document  # 可选依赖：有它就能把表格也读出来
    except Exception:
        return _docx_text_stdlib(path)
    doc = Document(path)
    parts = [p.text.strip() for p in doc.paragraphs if p.text and p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text and c.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def extract_book_text(path: str, size_limit: int = 16 * 1024 * 1024) -> str | None:
    """读取文档正文：txt/md/epub/pdf/docx/html；私有格式（caj 等）返回 None。"""
    try:
        lower = str(path).lower()
        if lower.endswith((".txt", ".md")):
            with open(path, "rb") as f:
                raw = f.read(size_limit)
            return _limit_text(raw.decode("utf-8", errors="ignore"))
        if lower.endswith((".html", ".htm")):
            with open(path, "rb") as f:
                raw = f.read(size_limit)
            return _limit_text(_html_text(raw.decode("utf-8", errors="ignore")))
        if lower.endswith(".epub"):
            with zipfile.ZipFile(path) as zf:
                parts = []
                for name in sorted(zf.namelist()):
                    if name.lower().endswith((".xhtml", ".html", ".htm")):
                        raw = zf.read(name).decode("utf-8", errors="ignore")
                        parts.append(_html_text(raw))
                return _limit_text("\n".join(p for p in parts if p.strip()))
        if lower.endswith(".pdf"):
            return _limit_text(_pdf_text(path))
        if lower.endswith(".docx"):
            return _limit_text(_docx_text(path))
    except Exception:
        return None
    return None


def document_format_hint(path: str) -> str:
    """读不出正文时给一句人话原因，方便日志和聊天里解释。"""
    lower = str(path or "").lower()
    if lower.endswith(PRIVATE_DOC_SUFFIXES):
        return "知网 CAJ/KDH/NH 是私有格式，先用 CAJViewer 另存为 PDF"
    if lower.endswith(".doc"):
        return "老式 .doc 先另存为 .docx 或 PDF"
    if lower.endswith(SUPPORTED_DOC_SUFFIXES):
        return "文件可能损坏、加密，或是扫描件没有文字层"
    return "支持 txt/md/epub/pdf/docx/html"


def book_chunk(text: str, offset: int, chunk: int = READ_CHUNK) -> tuple[str, int]:
    """从 offset 取一段正文，返回（文本，新 offset）。文本不足时标记读完。"""
    text = str(text or "")
    offset = max(0, int(offset))
    piece = text[offset : offset + chunk]
    new_offset = min(len(text), offset + len(piece))
    return piece, new_offset


# ---------------- 外部数据解析 ----------------

DEFAULT_TOPIC_POOL = (
    "天文学", "中世纪史", "心理学", "航海技术", "游戏设计",
    "经典文学", "人工智能", "经济学", "植物学", "科幻小说",
)


def topic_pool(topics_cfg: str) -> list[str]:
    items = [t.strip() for t in re.split(r"[,，]", str(topics_cfg or "")) if t.strip()]
    return items or list(DEFAULT_TOPIC_POOL)


def pick_gutendex_book(payload: dict, used_ids: set[str]) -> tuple[str, str] | None:
    """从 Gutendex 结果里选一本有纯文本格式的书，返回 (gutenberg_id, 标题)。"""
    results = (payload or {}).get("results") or []
    for book in results:
        raw_id = book.get("id")
        # 只接受纯数字 id：它会被拼进 gutenberg.org 的路径，必须防路径注入
        if not str(raw_id or "").isdigit():
            continue
        gid = str(raw_id)
        title = str(book.get("title") or "").strip()
        if not gid or not title or gid in used_ids:
            continue
        formats = book.get("formats") or {}
        txt_url = next(
            (
                url
                for url in formats.values()
                if isinstance(url, str) and url.lower().endswith((".txt.utf-8", ".txt"))
            ),
            None,
        )
        if txt_url:
            return gid, title
    return None


def parse_arxiv_entries(atom_xml: str, limit: int = 5) -> list[dict]:
    """解析 arXiv Atom XML，返回 [{title, authors, summary, link, pdf_url}]。"""
    try:
        root = ElementTree.fromstring(str(atom_xml or ""))
    except ElementTree.ParseError:
        return []
    ns = {"a": "http://www.w3.org/2005/Atom"}
    out = []
    for entry in root.findall("a:entry", ns)[: max(1, limit)]:
        title = " ".join((entry.findtext("a:title", "", ns) or "").split())
        summary = " ".join((entry.findtext("a:summary", "", ns) or "").split())
        authors = [
            (a.findtext("a:name", "", ns) or "").strip()
            for a in entry.findall("a:author", ns)
        ]
        link = ""
        link_el = entry.find("a:link", ns)
        if link_el is not None:
            link = link_el.get("href", "")
        pdf_url = ""
        for el in entry.findall("a:link", ns):
            if el.get("type") == "application/pdf" or el.get("title") == "pdf":
                pdf_url = el.get("href", "")
                break
        if not pdf_url:
            raw_id = entry.findtext("a:id", "", ns)
            if "/abs/" in raw_id:
                pdf_url = raw_id.replace("/abs/", "/pdf/") + ".pdf"
        if title and summary:
            out.append(
                {
                    "title": title,
                    "authors": authors,
                    "summary": summary,
                    "link": link,
                    "pdf_url": pdf_url,
                }
            )
    return out


def pick_arxiv_paper(entries: list[dict], used_titles: set[str]) -> dict | None:
    for entry in entries:
        if entry["title"] not in used_titles:
            return entry
    return None


# ---------------- 记录与注入 ----------------

_EVENT_TYPE_NAMES = {"book": "读书", "research": "查资料", "paper": "读论文"}


def event_type_name(event_type: str) -> str:
    return _EVENT_TYPE_NAMES.get(event_type, event_type)


def render_event_record(ts: float, event_type: str, title: str, body: str) -> str:
    day = _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    lines = [
        f"# {event_type_name(event_type)}：{title}",
        "",
        f"- 日期：{day}",
        f"- 类型：{event_type_name(event_type)}",
        "",
        str(body or "").strip(),
        "",
    ]
    return "\n".join(lines)


def experience_summary(
    records: list[dict], limit: int = 300
) -> str:
    """把最近几条事件压成注入用文本。records: [{"ts","type","title","gist"}]。"""
    if not records:
        return ""
    lines = []
    for record in records[:2]:
        lines.append(f"「{record.get('title', '')}」（{event_type_name(record.get('type', ''))}）：{record.get('gist', '')}")
    text = f"[真实经历] 最近：{'；'.join(lines)}"
    if limit > 0 and len(text) > limit:
        text = text[:limit] + "…"
    return text
