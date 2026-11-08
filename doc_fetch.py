"""远程文档取正文：MCP 只返回「文档名片」时，把全文找回来。

为什么需要这一层：知识库 MCP 的检索工具（资料中心实测是 query_documents）
返回的是**文档级引用**——文件名、相似度、最多一个 doc_id 或下载链接，
没有正文。卡片只有标题，销售看了等于没看。要给出「能照着说的一句话」，
必须先把正文取到手。

三条取文路径，按可靠性排序：
  1. 命中里带 url 且是 http(s) → 直接下载（最稳，不依赖对方工具）
  2. 对方暴露了 read/get/download 类工具 → 按它的 inputSchema 传 doc_id
  3. 两者都没有 → 明确报「只给了文件名」，而不是静默出空卡

解析：md/txt/html/docx/pdf 复用 kb_admin 的实现（原生库已处理 docx/pdf），
本文件补 pptx / xlsx —— 资料中心的资料绝大多数就是 Office 三件套，
pptx 用 zipfile 读 ppt/slides/*.xml 的 <a:t>，xlsx 读 sharedStrings + 工作表。
两者都只用标准库，不新增依赖。
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from xml.etree import ElementTree as ET

import kb_admin

CACHE_DIR = os.path.join("data", "cache", "docs")
CACHE_TTL = 7 * 24 * 3600          # 文档内容不常变，一周足够；改名/替换靠 doc_id 变
MAX_BYTES = 30 * 1024 * 1024       # 单文档上限，防对方返回大文件把内存打满
MAX_CHARS = 20000                  # 正文上限：够切 3 段喂模型，又不至于超上下文

_A_T = "{http://schemas.openxmlformats.org/drawingml/2006/main}t"
_A_P = "{http://schemas.openxmlformats.org/drawingml/2006/main}p"

# 取正文工具名：宁可多认几个，也不要因为叫 fetch_doc 就认不出来
_READ_HINTS = ("read", "get", "fetch", "download", "content", "detail",
               "preview", "open", "load")
# 这些是写操作或检索，绝不能当取正文用
_READ_EXCLUDE = ("search", "query", "retrieve", "recall", "list", "create",
                 "delete", "remove", "update", "upload", "write", "edit",
                 "index", "sync", "login", "token")

_DOCID_ARG_KEYS = ("doc_id", "docId", "document_id", "documentId", "file_id",
                   "fileId", "id", "key", "doc_name", "docName", "file_name",
                   "fileName", "path", "url", "uri", "name")


# --------------------------------------------------------------------------
# 解析
# --------------------------------------------------------------------------
def _xml_text(xml_bytes: bytes) -> str:
    """把一段 OOXML 里的所有文本节点按出现顺序拼起来。"""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return ""
    out = []
    for el in root.iter():
        if el.tag == _A_T and el.text:
            out.append(el.text)
        elif el.tag.endswith("}t") and el.text and "spreadsheet" in el.tag:
            out.append(el.text)
    return "".join(out)


def pptx_to_text(data: bytes) -> str:
    """pptx → 按页编号的纯文本。幻灯片的文字分布在若干 shape 里，
    读 <a:t> 就能拿到 99% 的可读内容（图片里的字拿不到，这是格式硬限制）。"""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ValueError("这不是有效的 pptx（不是 zip 结构）")
    names = [n for n in zf.namelist()
             if re.match(r"ppt/slides/slide\d+\.xml$", n)]
    if not names:
        raise ValueError("pptx 里没有幻灯片（ppt/slides 为空）")
    # slide10 必须排在 slide2 之后，按数字排而不是按字符串
    names.sort(key=lambda n: int(re.search(r"(\d+)\.xml$", n).group(1)))
    parts = []
    for i, n in enumerate(names, 1):
        try:
            root = ET.fromstring(zf.read(n))
        except ET.ParseError:
            continue
        # 按 a:p（文本框里的段落）切，而不是把所有 a:t 连成一串：
        # 否则「标题」和「正文第一句」会粘成一个词，抽出来的答案念不出口。
        lines = []
        for para in root.iter(_A_P):
            t = "".join(x.text or "" for x in para.iter(_A_T)).strip()
            if t:
                lines.append(t)
        text = "\n".join(lines) if lines else _xml_text(zf.read(n)).strip()
        if text:
            parts.append("[第%d页]\n%s" % (i, text))
    if not parts:
        return ""
    return "\n\n".join(parts)


def xlsx_to_text(data: bytes) -> str:
    """xlsx → 以工作表分节的文本。共享字符串表存的是全部单元格文字，
    直接读它比解析每个 sheet 的引用关系简单得多，且顺序基本一致。"""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ValueError("这不是有效的 xlsx（不是 zip 结构）")
    chunks = []
    if "xl/sharedStrings.xml" in zf.namelist():
        try:
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in root:
                txt = "".join(t.text or "" for t in si.iter() if t.tag.endswith("}t"))
                if txt.strip():
                    chunks.append(txt.strip())
        except ET.ParseError:
            pass
    if not chunks:
        # 没有共享字符串（全是数字/内联字符串）时退化为直接读 sheet 的 t 节点
        for n in sorted(x for x in zf.namelist()
                        if re.match(r"xl/worksheets/sheet\d+\.xml$", x)):
            t = _xml_text(zf.read(n)).strip()
            if t:
                chunks.append(t)
    return "\n".join(chunks)


def parse_bytes(filename: str, data: bytes) -> tuple[str, str]:
    """返回 (文本, 类型)。不支持的格式抛 ValueError，调用方转成人话。"""
    low = (filename or "").lower()
    if low.endswith(".pptx"):
        return pptx_to_text(data), "pptx"
    if low.endswith(".ppt"):                       # 老格式是二进制，读不了
        raise ValueError("老版 .ppt 无法直接解析，请让资料中心提供 pptx 或 pdf")
    if low.endswith(".xlsx") or low.endswith(".xlsm"):
        return xlsx_to_text(data), "xlsx"
    try:
        md, kind = kb_admin.to_markdown(filename, data)
        return md, kind
    except ValueError as e:
        raise ValueError(str(e))


# --------------------------------------------------------------------------
# 缓存
# --------------------------------------------------------------------------
def _cache_key(hit: dict) -> str:
    ident = (hit.get("doc_id") or hit.get("source") or hit.get("title") or "")
    return hashlib.sha1(ident.encode("utf-8")).hexdigest()[:20]


def _cache_get(key: str) -> dict | None:
    p = os.path.join(CACHE_DIR, key + ".json")
    try:
        if time.time() - os.path.getmtime(p) > CACHE_TTL:
            return None
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _cache_put(key: str, payload: dict) -> None:
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(os.path.join(CACHE_DIR, key + ".json"), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
    except OSError:
        pass


def cache_clear() -> int:
    n = 0
    try:
        for f in os.listdir(CACHE_DIR):
            if f.endswith(".json"):
                os.remove(os.path.join(CACHE_DIR, f))
                n += 1
    except OSError:
        pass
    return n


# --------------------------------------------------------------------------
# 取正文
# --------------------------------------------------------------------------
async def _download(url: str, timeout: float) -> tuple[bytes, str]:
    """下载：文件名优先取 Content-Disposition，其次取 URL 末段。"""
    def _do():
        req = urllib.request.Request(url, headers={"User-Agent": "qieshen-live/1.0"})
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with op.open(req, timeout=timeout) as r:
            raw = r.read(MAX_BYTES + 1)
            cd = r.headers.get("Content-Disposition") or ""
            name = ""
            m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", cd)
            if m:
                name = urllib.parse.unquote(m.group(1))
            if not name:
                name = os.path.basename(urllib.parse.urlparse(url).path)
            return raw, name

    raw, name = await asyncio.to_thread(_do)
    if len(raw) > MAX_BYTES:
        raise ValueError("文档超过 %d MB，不下载" % (MAX_BYTES // 1024 // 1024))
    return raw, name


def _pick_read_tool(tools):
    best, best_score = None, 0
    for t in tools or []:
        nm = str(t.get("name") or "").lower()
        desc = str(t.get("description") or "").lower()
        if any(x in nm for x in _READ_EXCLUDE):
            continue
        score = 0
        if any(h in nm for h in _READ_HINTS):
            score += 60
        if any(h in nm for h in ("doc", "file", "document", "content", "attachment")):
            score += 25
        if any(h in desc for h in ("正文", "内容", "全文", "content", "full text",
                                   "detail", "原文", "下载")):
            score += 15
        schema = t.get("inputSchema") or {}
        props = (schema.get("properties") or {}) if isinstance(schema, dict) else {}
        if any(k in props for k in _DOCID_ARG_KEYS):
            score += 15
        if score > best_score:
            best, best_score = t, score
    # 只有明确像「读单个文档」的工具才算，避免把随便一个 get_xxx 当取正文
    return (best, best_score) if best_score >= 50 else (None, best_score)


def _map_read_args(tool: dict, hit: dict) -> dict:
    schema = tool.get("inputSchema") or {}
    props = (schema.get("properties") or {}) if isinstance(schema, dict) else {}
    ident = hit.get("doc_id") or hit.get("source") or hit.get("title") or ""
    args = {}
    for k in props:
        if k in _DOCID_ARG_KEYS:
            # 传 url 的入参只接受 http 源，否则会把文件名当 URL
            if k.lower() in ("url", "uri") and not str(ident).startswith("http"):
                continue
            args[k] = ident
    if not args:
        # schema 没声明（少见）时按最常见命名兜底
        args["doc_id"] = ident
    return args


async def fetch_document(client, hit: dict, timeout: float = 8.0) -> dict:
    """取一份命中文档的正文。

    返回 {"text", "kind", "via", "error", "cached"}
    via ∈ url / mcp-read / cache / ""；失败时 text 为空且 error 说人话。
    """
    key = _cache_key(hit)
    cached = _cache_get(key)
    if cached and cached.get("text"):
        return dict(cached, cached=True)

    source = str(hit.get("source") or "")
    errs = []

    # 路径 1：命中自带下载链接
    if source.startswith("http"):
        try:
            raw, name = await _download(source, timeout)
            fname = name or hit.get("title") or "download"
            text, kind = parse_bytes(fname, raw)
            if text.strip():
                out = {"text": text[:MAX_CHARS], "kind": kind, "via": "url",
                       "error": "", "bytes": len(raw), "filename": fname}
                _cache_put(key, out)
                return dict(out, cached=False)
            errs.append("下载到了 %d 字节但解析不出文字" % len(raw))
        except Exception as e:
            errs.append("下载失败：%s" % str(e)[:90])

    # 路径 2：走对方的读文档工具
    if client is not None and client.transport != "local":
        try:
            tools = await client.list_tools()
        except Exception as e:
            tools = []
            errs.append("工具枚举失败：%s" % str(e)[:80])
        tool, score = _pick_read_tool(tools)
        if tool:
            sent = _map_read_args(tool, hit)
            try:
                res = await client.call(tool["name"], sent)
                text = _text_from_tool_result(res)
                if text.strip():
                    out = {"text": text[:MAX_CHARS], "kind": "mcp",
                           "via": "mcp-read:" + str(tool.get("name")),
                           "error": "", "filename": hit.get("title", "")}
                    _cache_put(key, out)
                    return dict(out, cached=False)
                errs.append("工具 %s 有返回但没文字（可能只给了下载链接）"
                            % tool.get("name"))
            except Exception as e:
                errs.append("调 %s 失败：%s" % (tool.get("name"), str(e)[:80]))
        elif tools:
            names = [str(t.get("name")) for t in tools][:8]
            errs.append("该服务没有读文档的工具（有：%s）" % "、".join(names))

    if not errs:
        errs.append("命中结果只给了文件名，既没有下载链接也没有 doc_id，"
                    "对方也没暴露读文档工具")
    return {"text": "", "kind": "", "via": "", "error": "；".join(errs)[:300],
            "cached": False}


def _text_from_tool_result(res) -> str:
    """读文档工具的返回可能是 dict / list / 纯文本，统一抠出文字。"""
    if isinstance(res, str):
        return res
    if not isinstance(res, dict):
        return json.dumps(res, ensure_ascii=False) if res else ""
    for k in ("content", "text", "markdown", "body", "full_text", "data",
              "result", "document"):
        v = res.get(k)
        if isinstance(v, str) and v.strip():
            return v
        if isinstance(v, dict):
            inner = _text_from_tool_result(v)
            if inner.strip():
                return inner
    # 结构化但没正文字段：把 JSON 当文本兜住，至少能选段
    s = json.dumps(res, ensure_ascii=False)
    return s if len(s) > 40 else ""


# --------------------------------------------------------------------------
# 选段
# --------------------------------------------------------------------------
def _bigrams(s: str) -> set:
    s = re.sub(r"[^\w\u4e00-\u9fff]+", "", s or "")
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) > 1 else set()


def split_passages(text: str, query: str, k: int = 3, max_chars: int = 420) -> list[str]:
    """按 query 相关度挑几段原文喂给答案合成。

    切段优先按空行/页码标记（pptx 的 [第N页]、md 的标题），
    兜底按句号切。打分用中文 bigram 重合度，对短 query 也有效。
    """
    if not text:
        return []
    # pptx 一页里标题和正文是不同行，按行切才不会把两者粘成一句话；
    # 普通 md/txt 按空行分段即可，按行切会把段落切得太碎。
    pptx_style = "[第" in text and "页]" in text
    sep = r"\n+|(?=^#{1,6}\s)" if pptx_style else r"\n\s*\n|(?=^#{1,6}\s)"
    blocks = re.split(sep, text, flags=re.M)
    blocks = [re.sub(r"[ \t]+", " ", b).strip() for b in blocks]
    blocks = [b for b in blocks if len(b) > 12]
    if not blocks:
        blocks = [re.sub(r"\s+", " ", text).strip()]

    q = _bigrams(query)
    # 主题词单独给一份权重：query 是「高空抛物 相机 型号」这种词组，
    # 单个词命中比 bigram 更容易覆盖到不同写法
    words = [w for w in re.split(r"[\s,，、/]+", query or "") if len(w) >= 2]

    scored = []
    for i, b in enumerate(blocks):
        bg = _bigrams(b)
        s = len(q & bg) * 2 if q else 0
        s += sum(3 for w in words if w in b)
        s += min(len(b), 200) / 1000.0        # 同分时略偏爱信息量大的段
        scored.append((s, i, b))
    scored.sort(key=lambda x: (-x[0], x[1]))
    picked = [b for _, _, b in scored[:k] if b]
    return [p[:max_chars] for p in picked]


def fetch_stats() -> dict:
    try:
        files = [f for f in os.listdir(CACHE_DIR) if f.endswith(".json")]
    except OSError:
        files = []
    total = 0
    for f in files:
        try:
            total += os.path.getsize(os.path.join(CACHE_DIR, f))
        except OSError:
            pass
    return {"cached_docs": len(files), "cache_kb": round(total / 1024.0, 1)}
