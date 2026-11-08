"""知识库管理：文档导入（md / txt / docx / pdf / 网页 URL）+ MCP 多源配置持久化。

设计取舍：
  * 所有导入结果统一落成「带 front-matter 的 markdown」，和内置 docs-kaiyi 完全同构。
    这样检索、切块、卡片出处全部沿用 DocStore 一套逻辑，不需要第二套索引。
  * docx 用 zipfile + ElementTree 直接解 word/document.xml —— docx 本质就是个 zip，
    为了读文字引入 python-docx 不划算。
  * pdf 必须外部库（内容流是 FlateDecode + 字体编码，纯标准库硬解容易出乱码），
    用 pypdf，缺失时给出明确提示而不是抛一堆栈。
  * 上传文件写在 data/kb/，内置 docs-kaiyi/ 只读；删除接口只允许删 data/kb/ 里的。
"""
from __future__ import annotations

import io
import os
import re
import time
import json
import zipfile
import urllib.request
import urllib.error
from html.parser import HTMLParser
import xml.etree.ElementTree as ET

MAX_BYTES = 25 * 1024 * 1024          # 单文件上限：25MB，挡住误传大视频
MAX_CHARS = 400_000                   # 单文档字符上限，防止一份 8 万字手册把检索拖慢

MD_EXT = (".md", ".markdown", ".txt")
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

_ILLEGAL = re.compile(r"[^\w\u4e00-\u9fff.\-]+", re.UNICODE)


# --------------------------------------------------------------------------
# 路径与文件名
# --------------------------------------------------------------------------
def safe_name(name: str, default: str = "doc") -> str:
    """把用户文件名收敛成可安全落盘的片段（去掉路径穿越与奇怪符号）。"""
    base = os.path.basename((name or "").replace("\\", "/"))
    base = os.path.splitext(base)[0] if base.lower().endswith(
        (".md", ".markdown", ".txt", ".docx", ".pdf", ".html", ".htm", ".csv")
    ) else base
    base = _ILLEGAL.sub("-", base).strip("-._")
    return (base or default)[:80]


def stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


# --------------------------------------------------------------------------
# 解码：中文文档八成是 utf-8，剩下的大多是 gbk
# --------------------------------------------------------------------------
def decode_bytes(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "gb18030", "big5", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


# --------------------------------------------------------------------------
# docx
# --------------------------------------------------------------------------
def _w(tag: str) -> str:
    return "{%s}%s" % (W_NS, tag)


def docx_to_markdown(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        try:
            xml = z.read("word/document.xml")
        except KeyError:
            raise ValueError("这不是有效的 docx（缺少 word/document.xml）")
    root = ET.fromstring(xml)
    lines = []
    for p in root.iter(_w("p")):
        text = "".join(t.text or "" for t in p.iter(_w("t"))).strip()
        if not text:
            continue
        style = ""
        ppr = p.find(_w("pPr"))
        if ppr is not None:
            ps = ppr.find(_w("pStyle"))
            if ps is not None:
                style = (ps.get(_w("val")) or "").strip()
        low = style.lower()
        if low.startswith("heading") or style.startswith("标题"):
            digits = re.sub(r"\D", "", style) or "1"
            lv = min(3, max(1, int(digits) if digits.isdigit() else 1))
            lines.append("#" * lv + " " + text)
        else:
            lines.append(text)
    return "\n\n".join(lines)


# --------------------------------------------------------------------------
# pdf
# --------------------------------------------------------------------------
def pdf_to_markdown(data: bytes) -> str:
    try:
        from pypdf import PdfReader          # noqa: PLC0415  延迟导入，缺依赖时给友好提示
    except ImportError:
        raise RuntimeError(
            "服务端没有安装 pypdf，无法解析 PDF。"
            "请改用带依赖的镜像（requirements.txt 已声明），或先把 PDF 转成 md/txt 再导入。")
    reader = PdfReader(io.BytesIO(data))
    if getattr(reader, "is_encrypted", False):
        try:
            reader.decrypt("")               # 只处理「空密码加密」，真加密的只能先解密
        except Exception:
            raise ValueError("PDF 已加密，无法提取文字")
    pages = []
    for i, page in enumerate(reader.pages, 1):
        try:
            text = (page.extract_text() or "").strip()
        except Exception:
            text = ""
        if text:
            pages.append("## 第 %d 页\n\n%s" % (i, text))
    if not pages:
        raise ValueError("这份 PDF 里没有可提取的文字（大概率是扫描件，需要 OCR）")
    return "\n\n".join(pages)


# --------------------------------------------------------------------------
# 网页
# --------------------------------------------------------------------------
class _HtmlText(HTMLParser):
    """把网页正文抽成行。只保留内容区，导航/页脚/脚本一律丢掉。"""

    SKIP = {"script", "style", "noscript", "nav", "footer", "aside", "form",
            "iframe", "svg", "button", "header"}
    BLOCK = {"p", "div", "section", "article", "br", "tr", "li", "table",
             "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "blockquote"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.lines, self.buf = [], []
        self.skip_depth = 0
        self.title = ""
        self._in_title = False
        self.heading = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
        if tag in ("h1", "h2", "h3"):
            self.heading = int(tag[1])
        if tag in self.BLOCK:
            self._flush()

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if tag == "title":
            self._in_title = False
        if tag in ("h1", "h2", "h3"):
            self._flush()
            self.heading = 0
        if tag in self.BLOCK:
            self._flush()

    def handle_data(self, data):
        if self._in_title:
            self.title += data
            return
        if self.skip_depth:
            return
        t = data.strip()
        if t:
            self.buf.append(t)

    def _flush(self):
        if not self.buf:
            return
        text = re.sub(r"\s+", " ",
                      "".join(self.buf) if self.heading else " ".join(self.buf)).strip()
        self.buf = []
        if not text or len(text) < 2:
            return
        self.lines.append(("#" * self.heading + " " + text) if self.heading else text)

    def result(self):
        self._flush()
        return "\n\n".join(self.lines)


def html_to_markdown(html: str) -> tuple:
    p = _HtmlText()
    try:
        p.feed(html)
    except Exception:
        pass
    body = p.result()
    title = re.sub(r"\s+", " ", p.title).strip()
    if not title:
        first = next((ln for ln in body.splitlines() if ln.strip()), "")
        title = re.sub(r"^#+\s*", "", first)[:60] or "网页导入"
    return title, body


def fetch_url(url: str, timeout: float = 15.0) -> tuple:
    """抓取网页并转 markdown。返回 (title, markdown, final_url)。"""
    if not re.match(r"^https?://", url or "", re.I):
        raise ValueError("URL 必须以 http:// 或 https:// 开头")
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; qieshen-kb/1.0; +https://github.com/hieedaniel/your-pm)",
        "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read(MAX_BYTES)
            ctype = (r.headers.get("Content-Type") or "").lower()
            final = r.geturl()
    except urllib.error.HTTPError as e:
        raise ValueError("抓取失败 HTTP %s" % e.code)
    except urllib.error.URLError as e:
        raise ValueError("抓取失败：%s" % (e.reason,))
    except Exception as e:
        raise ValueError("抓取失败：%s: %s" % (type(e).__name__, e))

    if "pdf" in ctype:
        return (_basename(final), pdf_to_markdown(raw), final)
    text = decode_bytes(raw)
    if "html" not in ctype and "xml" not in ctype:
        # 纯文本 / markdown：原样收
        first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "网页导入")
        return (first[:60], text, final)
    title, body = html_to_markdown(text)
    if len(body) < 40:
        raise ValueError("这个页面没有可提取的正文（可能是纯前端渲染，或需要登录）")
    return (title, body, final)


def _basename(url: str) -> str:
    name = os.path.basename((url or "").split("?")[0].split("#")[0])
    return safe_name(name or "网页导入")


# --------------------------------------------------------------------------
# 统一入口：任何文件 → markdown
# --------------------------------------------------------------------------
def to_markdown(filename: str, data: bytes) -> tuple:
    """返回 (title, markdown, kind)。失败抛 ValueError / RuntimeError。"""
    if len(data) > MAX_BYTES:
        raise ValueError("文件超过 %d MB，请拆分后再导入" % (MAX_BYTES // 1024 // 1024))
    low = (filename or "").lower()
    if low.endswith(".docx"):
        md, kind = docx_to_markdown(data), "docx"
    elif low.endswith(".pdf"):
        md, kind = pdf_to_markdown(data), "pdf"
    elif low.endswith((".html", ".htm")):
        title, md = html_to_markdown(decode_bytes(data))
        kind = "html"
        md = _strip_leading_title(md, title)
        return (title or safe_name(filename), _clamp(md), kind)
    elif low.endswith((".md", ".markdown", ".txt")):
        md, kind = decode_bytes(data), "text"
    else:
        raise ValueError("不支持的格式：%s（支持 md / txt / docx / pdf / html）" %
                         (os.path.splitext(filename)[1] or "无扩展名"))
    title = _first_heading(md) or safe_name(filename)
    return (title, _clamp(md), kind)


def _strip_leading_title(md: str, title: str) -> str:
    lines = md.splitlines()
    if lines and lines[0].startswith("#") and title and title[:20] in lines[0]:
        return "\n".join(lines[1:]).lstrip("\n")
    return md


def _first_heading(md: str) -> str:
    for ln in md.splitlines():
        s = ln.strip()
        if s.startswith("#"):
            return re.sub(r"^#+\s*", "", s)[:80]
        if s and len(s) <= 60:
            return s
    return ""


def _clamp(md: str) -> str:
    return md[:MAX_CHARS]


# --------------------------------------------------------------------------
# 落盘
# --------------------------------------------------------------------------
def write_doc(kb_dir: str, title: str, md: str, source: str, tags=None,
              name_hint: str = "", doc_id: str = "") -> str:
    """写一份带 front-matter 的 md，返回 doc_id（= 文件名）。"""
    os.makedirs(kb_dir, exist_ok=True)
    base = safe_name(name_hint or title, "doc")
    if doc_id:
        fid = doc_id if doc_id.endswith(".md") else doc_id + ".md"
    else:
        fid = "%s-%s.md" % (stamp(), base)
    path = os.path.join(kb_dir, fid)
    n = 1
    while os.path.exists(path) and not doc_id:
        n += 1
        fid = "%s-%d-%s.md" % (stamp(), n, base)
        path = os.path.join(kb_dir, fid)

    fm = ["---",
          "title: %s" % (title or base).replace("\n", " ").strip(),
          "source: %s" % (source or "上传").replace("\n", " ").strip()]
    if tags:
        fm.append("tags: %s" % ",".join(tags))
    fm.append("as_of: %s" % time.strftime("%Y-%m-%d"))
    fm.append("visibility: internal")
    fm.append("---")
    text = "\n".join(fm) + "\n\n" + md.strip() + "\n"
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)          # 原子替换：导入中途被打断也不会留下半份文档
    return fid


def delete_doc(kb_dir: str, doc_id: str) -> bool:
    """只允许删 data/kb/ 里的文件；内置 docs-kaiyi 一律拒绝。"""
    name = os.path.basename(doc_id or "")
    if not name or not name.lower().endswith(MD_EXT):
        return False
    path = os.path.join(kb_dir, name)
    if os.path.dirname(os.path.abspath(path)) != os.path.abspath(kb_dir):
        return False
    if not os.path.isfile(path):
        return False
    try:
        os.remove(path)
        return True
    except OSError:
        return False


def kb_files(kb_dir: str) -> list:
    """data/kb/ 下的文件清单（含大小/时间），管理页用。"""
    out = []
    if not os.path.isdir(kb_dir):
        return out
    for name in sorted(os.listdir(kb_dir)):
        if not name.lower().endswith(MD_EXT):
            continue
        path = os.path.join(kb_dir, name)
        try:
            st = os.stat(path)
        except OSError:
            continue
        out.append({"doc_id": name, "bytes": st.st_size,
                    "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))})
    return out


# --------------------------------------------------------------------------
# MCP 多源配置（持久化在 data/kb/mcp_sources.json）
# --------------------------------------------------------------------------
def sources_path(kb_dir: str) -> str:
    return os.path.join(kb_dir, "_mcp_sources.json")


def load_sources(kb_dir: str) -> list:
    path = sources_path(kb_dir)
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return []
    raw = d.get("sources") if isinstance(d, dict) else d
    return normalize_sources(raw or [])


def normalize_sources(raw: list) -> list:
    """清洗前端传来的源列表：去空、去重、补 id 与默认值。"""
    out, seen = [], set()
    for i, s in enumerate(raw or []):
        if not isinstance(s, dict):
            continue
        url = (s.get("url") or "").strip()
        if not url or not re.match(r"^https?://", url, re.I):
            continue
        if url in seen:
            continue
        seen.add(url)
        name = (s.get("name") or "").strip() or ("源%d" % (i + 1))
        out.append({
            "id": (s.get("id") or "").strip() or ("s%d" % (i + 1)),
            "name": name[:40],
            "url": url,
            "token": (s.get("token") or "").strip(),
            "enabled": bool(s.get("enabled", True)),
            "weight": _num(s.get("weight"), 1.0, 0.1, 5.0),
            "adapter": (s.get("adapter") or "none").strip() or "none",
            "timeout_ms": int(_num(s.get("timeout_ms"), 2500, 500, 30000)),
        })
    return out


def save_sources(kb_dir: str, raw: list) -> list:
    os.makedirs(kb_dir, exist_ok=True)
    clean = normalize_sources(raw)
    path = sources_path(kb_dir)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"sources": clean}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return clean


def _num(v, default, lo, hi):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, f))


# ---------------------------------------------------------------------------
# 主题表：跟着知识库走
# ---------------------------------------------------------------------------
def taxonomy_from_docs(store, baseline=None, limit: int = 60) -> dict:
    """用当前知识库（内置 + 上传）生成主题表。

    为什么需要：主题表原本是照 docs-kaiyi 手写的 19 行。一旦换成自己的资料
    （导入文档或接 MCP），说的是完全不同的概念，模型/规则都没法归类，主题
    就永远空着。这里的做法是把「文档标题」当主题、「front-matter 里的 say 与
    tags」当别名，让表跟着库一起变。

    baseline 是仓库里那份手写表：同一个 label 的词会被合并进来。手写表里的
    say 是人工整理的客户说法，质量比自动抽的高，重建不该把它丢掉。

    跨文档重复的 tag 会被丢掉：像「解决方案」「整体方案」这种词出现在每篇里，
    留着只会让每句话都同时命中十几个主题。
    """
    from collections import Counter
    docs = getattr(store, "docs", {}) or {}
    base = {}
    for t in (baseline or {}).get("topics") or []:
        if t.get("label"):
            base[t["label"]] = t
    tag_count = Counter()
    for d in docs.values():
        for t in d.get("tags") or []:
            tag_count[t] += 1
    items = []
    for name, d in sorted(docs.items(), key=lambda kv: (kv[1].get("title") or kv[0])):
        label = str(d.get("title") or os.path.splitext(name)[0]).strip()
        if not label:
            continue
        b = base.get(label) or {}
        says, seen = [], set()
        for s in list(b.get("say") or []) + list(d.get("say") or []):
            s = str(s).strip()
            if len(s) >= 2 and s not in seen:
                seen.add(s)
                says.append(s[:20])
        tags, seen = [], set()
        for t in list(b.get("tags") or []) + list(d.get("tags") or []):
            t = str(t).strip()
            # 手写表里的词无条件保留，自动抽的只留本篇独有的
            keep = t in (b.get("tags") or []) or tag_count.get(t, 0) <= 1
            if len(t) >= 2 and t not in seen and keep:
                seen.add(t)
                tags.append(t[:20])
        items.append({"id": b.get("id") or ("T%02d" % (len(items) + 1)),
                      "label": label[:40], "doc_id": name,
                      "visibility": (d.get("visibility") or b.get("visibility")
                                     or "public"),
                      "say": says[:10], "tags": tags[:14]})
        if len(items) >= limit:
            break
    return {"version": 2, "source": "kb", "generated": int(time.time()),
            "topics": items}


def taxonomy_path(kb_dir: str) -> str:
    return os.path.join(kb_dir, "taxonomy.json")


def save_taxonomy(kb_dir: str, taxo: dict) -> str:
    os.makedirs(kb_dir, exist_ok=True)
    path = taxonomy_path(kb_dir)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(taxo, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path
