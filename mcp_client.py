"""MCP 客户端：local / http（streamable-http）/ stdio 三种传输，统一 call() 返回结构。

本项目的关键立场（见深度研究报告 11 章）：主链路是「确定性 tools/call」，
不让大模型决定调哪个工具、也不让模型转述结果。切换真实 MCP 只改 config.json。

streamable-http 的三个坑（都踩过）：
  1. 必须同时声明 Accept: application/json, text/event-stream，否则服务端可能拒；
  2. 响应可能是 SSE 包装（event: message\\ndata: {...}），要按 id 取对应那条，
     不能只抓第一行 data（通知类事件会串在前面）；
  3. 规范要求 initialize 后补一条 notifications/initialized；有状态服务端返回
     Mcp-Session-Id 头，后续请求必须回带，无状态服务端不发则可忽略。
"""
from __future__ import annotations

import asyncio
import json

from wsproto import http_request, json_rpc_http


def _header(headers, name: str) -> str:
    if not headers:
        return ""
    low = name.lower()
    for k, v in headers.items():
        if str(k).lower() == low:
            return str(v)
    return ""


def parse_sse(raw: str, rid=None):
    """从 SSE / 纯 JSON 响应里取出 jsonrpc 报文；优先匹配 id，否则取最后一条。"""
    text = (raw or "").strip()
    if not text:
        return None
    if text.startswith("{") or text.startswith("["):
        try:
            return json.loads(text)
        except Exception:
            return None
    picked = None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        body = line[5:].strip()
        if not body or body == "[DONE]":
            continue
        try:
            obj = json.loads(body)
        except Exception:
            continue
        if rid is not None and obj.get("id") == rid:
            return obj
        picked = obj
    return picked


class McpClient:
    def __init__(self, cfg: dict, local_server=None):
        self.cfg = cfg
        self.transport = cfg.get("transport", "local")
        self.local = local_server
        self.proc = None
        self._id = 0
        self._stdio_lock = asyncio.Lock()
        self._init_lock = asyncio.Lock()
        self._inited = False
        self._session_id = ""
        self._server_info = {}
        self._last_error = ""
        # 自适应检索的缓存：None=还没认过，dict=认到的工具，False=认过但没有
        self._search_tool = None
        self._search_tool_score = 0
        self.timeout = float(cfg.get("timeout_ms", 6000)) / 1000.0

    def _next_id(self):
        self._id += 1
        return self._id

    def info(self):
        if self.transport == "local":
            ready = bool(self.local)
        elif self.transport == "http":
            ready = bool(self.cfg.get("http", {}).get("url"))
        else:
            ready = bool(self.cfg.get("stdio", {}).get("command"))
        out = {"transport": self.transport, "tools_ready": ready,
               "adapter": self.cfg.get("adapter", "none"),
               "url": self.cfg.get("http", {}).get("url", "") if self.transport == "http" else "",
               "session": bool(self._session_id), "inited": self._inited,
               "server": self._server_info.get("name", "")}
        if self._last_error:
            out["last_error"] = self._last_error
        return out

    async def list_tools(self):
        resp = await self._rpc("tools/list", {})
        return ((resp or {}).get("result") or {}).get("tools", [])

    async def call(self, name: str, args: dict) -> dict:
        """按语义调用。name="docs.search" 时走自适应检索（见 search_auto）。

        为什么要这一层：外部知识库（资料中心、OA、Confluence…）暴露的工具名
        五花八门（search_knowledge / kb_query / retrieve…），入参与返回结构也
        各不相同。以前这里硬编码按 docs.search 调，遇到别家就是一直「无命中」，
        而「无命中」和「资料里没有」长得一模一样，现场根本分不出来。
        """
        if name == "docs.search" and self.transport != "local":
            return await self.search_auto(args)
        resp = await self._rpc("tools/call", {"name": name, "arguments": args})
        if not resp or "error" in resp:
            err = (resp or {}).get("error", "MCP 无响应")
            self._last_error = json.dumps(err, ensure_ascii=False)[:200]
            return {"error": err}
        return self._unwrap(resp)

    def _unwrap(self, resp: dict) -> dict:
        result = (resp or {}).get("result") or {}
        if isinstance(result.get("structuredContent"), dict):
            return result["structuredContent"]
        for item in result.get("content") or []:
            if item.get("type") == "text":
                try:
                    return json.loads(item["text"])
                except Exception:
                    return {"text": item["text"]}
        return result

    # ---------------- 自适应检索：认工具名 + 认入参 + 认返回结构 ----------------
    _SEARCH_NAME_HINTS = ("search", "query", "retrieve", "recall", "lookup",
                          "检索", "搜索", "查询")
    _SEARCH_DESC_HINTS = ("检索", "搜索", "查询", "知识", "文档", "search", "retriev")
    _ARG_ALIASES = {
        "query": ("query", "q", "text", "keyword", "keywords", "question",
                  "content", "message", "input", "searchText", "search_text"),
        "top_k": ("top_k", "topK", "limit", "size", "count", "topN", "n", "maxResults"),
        "lang": ("lang", "language", "locale"),
        "prefer": ("prefer", "filters", "tags", "scope", "filter"),
    }
    _EXCLUDE_HINTS = ("list", "create", "update", "delete", "remove", "history",
                      "upload", "write", "add", "login", "auth")

    def _pick_search_tool(self, tools):
        best, best_score = None, 0
        for t in tools or []:
            nm = str(t.get("name") or "")
            low = nm.lower()
            desc = str(t.get("description") or "").lower()
            if low == "docs.search":
                return t, 100
            if any(x in low for x in self._EXCLUDE_HINTS):
                continue
            score = 0
            if any(h in low for h in ("search", "query", "retrieve", "recall")):
                score += 70
            if any(h in low for h in ("doc", "kb", "knowledge", "chunk", "file")):
                score += 25
            if any(h in desc for h in self._SEARCH_DESC_HINTS):
                score += 20
            schema = t.get("inputSchema") or {}
            props = (schema.get("properties") or {}) if isinstance(schema, dict) else {}
            if any(k in props for k in self._ARG_ALIASES["query"]):
                score += 15
            if score > best_score:
                best, best_score = t, score
        return (best, best_score) if best_score >= 40 else (None, best_score)

    def _map_args(self, tool: dict, args: dict) -> dict:
        schema = tool.get("inputSchema") or {}
        props = (schema.get("properties") or {}) if isinstance(schema, dict) else {}
        out = {}
        for key, val in (args or {}).items():
            if val in (None, "", []):
                continue
            names = self._ARG_ALIASES.get(key, (key,))
            hit = next((nm for nm in names if nm in props), None)
            if hit is None:
                # 没有 schema 时按原名透传；有 schema 但没这个字段就别乱塞（会被校验拒）
                if not props:
                    hit = names[0]
                elif key in props:
                    hit = key
            if hit:
                out[hit] = val
        return out

    _TITLE_KEYS = ("title", "doc_title", "docTitle", "name", "doc_name", "docName",
                   "document_name", "file_name", "fileName", "subject")
    _SNIP_KEYS = ("snippet", "content", "summary", "text", "chunk", "passage",
                  "abstract", "highlight", "body", "description")
    _ANS_KEYS = ("answer", "conclusion", "result", "answer_text")
    _SCORE_KEYS = ("score", "similarity", "relevance", "match_score", "confidence")
    _DOCID_KEYS = ("doc_id", "docId", "document_id", "documentId", "id", "file_id", "fileId")
    _SRC_KEYS = ("source", "url", "link", "doc_url", "docUrl", "path")

    def _first(self, d: dict, keys) -> str:
        for k in keys:
            v = d.get(k)
            if v not in (None, "") and not isinstance(v, (dict, list)):
                return str(v)
        return ""

    def _norm_hits(self, obj) -> list:
        """把各家返回结构收敛成编排器认的 hits。认出多少算多少，认不出就返回空。"""
        if isinstance(obj, list):
            rows = obj
        elif isinstance(obj, dict):
            rows = []
            for k in ("hits", "results", "items", "data", "records", "documents",
                      "chunks", "list", "matches"):
                v = obj.get(k)
                if isinstance(v, list):
                    rows = v
                    break
                if isinstance(v, dict) and k == "data":
                    for k2 in ("hits", "results", "items", "list", "records"):
                        if isinstance(v.get(k2), list):
                            rows = v[k2]
                            break
            if not rows and isinstance(obj.get("text"), str):
                return [{"title": "检索结果", "snippet": obj["text"][:600],
                         "answer": "", "score": 0.6, "doc_id": "", "chunk_id": "",
                         "visibility": "public", "source": "", "as_of": "未标注"}]
        else:
            rows = []
        hits = []
        for i, r in enumerate(rows[:20]):
            if not isinstance(r, dict):
                if isinstance(r, str) and r.strip():
                    hits.append({"title": "检索结果", "snippet": r[:600], "answer": "",
                                 "score": 0.6, "doc_id": "", "chunk_id": "",
                                 "visibility": "public", "source": "", "as_of": "未标注"})
                continue
            title = self._first(r, self._TITLE_KEYS)
            snippet = self._first(r, self._SNIP_KEYS)
            answer = self._first(r, self._ANS_KEYS)
            if not title and not snippet and not answer:
                continue
            try:
                score = float(self._first(r, self._SCORE_KEYS) or 0)
                if score > 1.0:                     # 百分制
                    score = score / 100.0
            except ValueError:
                score = 0.0
            if score <= 0:
                # 没给分数的按排名给个递减分：第一命中仍能过 0.55 的推卡线
                score = max(0.55, 0.95 - 0.05 * i)
            vis = str(r.get("visibility") or r.get("level") or r.get("sensitivity") or "").lower()
            visibility = "internal" if any(x in vis for x in ("internal", "内部", "private", "secret")) else "public"
            hits.append({
                "title": title or "知识库结果",
                "snippet": snippet[:600],
                "answer": answer[:600],
                "score": round(score, 3),
                "doc_id": self._first(r, self._DOCID_KEYS),
                "chunk_id": self._first(r, ("chunk_id", "chunkId", "chunk_index", "segment_id")),
                "visibility": visibility,
                "source": self._first(r, self._SRC_KEYS),
                "as_of": self._first(r, ("as_of", "asOf", "updated_at", "update_time", "date")),
            })
        return hits

    async def search_auto(self, args: dict) -> dict:
        """把「检索」这件事翻译成对面那套工具：认工具名 → 认入参 → 认返回。"""
        if self._search_tool is False:
            return {"hits": [], "error": self._last_error or "该服务没有可用的检索工具"}
        if not isinstance(self._search_tool, dict):
            try:
                tools = await self.list_tools()
            except Exception as e:
                self._last_error = "%s: %s" % (type(e).__name__, str(e)[:120])
                return {"hits": [], "error": self._last_error}
            tool, score = self._pick_search_tool(tools)
            if not tool:
                self._search_tool = False
                names = [t.get("name") for t in (tools or [])][:8]
                if names:
                    self._last_error = ("该服务没有看起来像「检索」的工具（工具有：%s）"
                                        % "、".join(str(x) for x in names))
                else:
                    # 工具一个都没枚举到：多半是凭据/网关问题，把底层原因带上，
                    # 否则「没有工具」会被人当成「这家服务不支持知识检索」。
                    self._last_error = ("工具枚举为空：%s"
                                        % (self._last_error or "服务未暴露任何工具"))
                return {"hits": [], "error": self._last_error}
            self._search_tool = tool
            self._search_tool_score = score
        tool = self._search_tool
        name = tool.get("name") or "search"
        sent = self._map_args(tool, args)
        if not sent:
            self._last_error = "工具 %s 的入参对不上（schema: %s）" % (
                name, json.dumps(tool.get("inputSchema") or {}, ensure_ascii=False)[:160])
            return {"hits": [], "error": self._last_error}
        resp = await self._rpc("tools/call", {"name": name, "arguments": sent})
        if not resp or "error" in resp:
            err = (resp or {}).get("error", "MCP 无响应")
            self._last_error = "%s: %s" % (name, json.dumps(err, ensure_ascii=False)[:180])
            return {"hits": [], "error": self._last_error}
        obj = self._unwrap(resp)
        hits = self._norm_hits(obj)
        if not hits:
            self._last_error = "工具 %s 有返回但结构认不出，原始返回前 200 字：%s" % (
                name, json.dumps(obj, ensure_ascii=False)[:200])
        return {"hits": hits, "tool": name, "raw_hits": len(hits)}

    def search_tool_info(self) -> dict:
        """自检/管理页用：当前认定成检索工具的是哪个、怎么认出来的。"""
        t = self._search_tool
        if isinstance(t, dict):
            return {"name": t.get("name"), "score": getattr(self, "_search_tool_score", 0),
                    "schema": (t.get("inputSchema") or {})}
        if t is False:
            return {"name": "", "error": self._last_error}
        return {"name": "", "resolved": False}

    async def _rpc(self, method: str, params: dict):
        payload = {"jsonrpc": "2.0", "id": self._next_id(),
                   "method": method, "params": params}
        try:
            if self.transport == "local" and self.local:
                resp = self.local.handle(payload)
            elif self.transport == "http":
                resp = await self._http_rpc(payload)
            elif self.transport == "stdio":
                resp = await self._stdio_rpc(payload)
            else:
                return {"error": "未支持的 transport: %s" % self.transport}
        except Exception as e:
            self._last_error = "%s: %s" % (type(e).__name__, e)
            return {"error": self._last_error}
        # JSON-RPC 层的错误也要落到 _last_error：网关的「缺少访问令牌 / Token 无效」
        # 是 HTTP 200 + error 体，不记下来，探测页只会显示「tools/list 为空」。
        if isinstance(resp, dict) and resp.get("error"):
            err = resp["error"]
            if isinstance(err, dict):
                self._last_error = "JSON-RPC %s: %s" % (err.get("code", "?"),
                                                        str(err.get("message") or "")[:150])
            else:
                self._last_error = str(err)[:180]
        return resp

    # ---------------- streamable-http ----------------
    async def _http_rpc(self, payload: dict):
        url = self.cfg.get("http", {}).get("url", "")
        if not url:
            return {"error": "mcp.http.url 未配置"}
        if payload.get("method") != "initialize":
            await self._ensure_init()
        return await self._post(url, payload, nid=payload.get("id"))

    async def _post(self, url: str, payload: dict, nid=None, timeout=None):
        h = {"Content-Type": "application/json",
             "Accept": "application/json, text/event-stream"}
        h.update(self.cfg.get("http", {}).get("headers") or {})
        token = self.cfg.get("http", {}).get("token", "")
        if token:
            h["Authorization"] = h.get("Authorization") or ("Bearer " + token)
        if self._session_id:
            h["Mcp-Session-Id"] = self._session_id
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        status, headers, raw = await http_request(
            url, "POST", h, body, timeout or self.timeout)
        sid = _header(headers, "mcp-session-id")
        if sid:
            self._session_id = sid
        text = (raw or b"").decode("utf-8", "replace")
        if status >= 400:
            self._last_error = "HTTP %s %s" % (status, text[:180].replace("\n", " "))
            if status == 401:
                self._last_error += "（凭据无效或过期）"
            return {"error": {"code": status, "message": text[:400] or "HTTP %s" % status}}
        if payload.get("id") is None:      # 通知，无响应
            return {}
        obj = parse_sse(text, nid)
        if obj is None:
            return {"error": {"code": status, "message": "空响应（%d 字节）" % len(text)}}
        return obj

    async def _ensure_init(self):
        if self._inited or self.transport != "http":
            return
        async with self._init_lock:
            if self._inited:
                return
            url = self.cfg.get("http", {}).get("url", "")
            proto = self.cfg.get("http", {}).get("protocol_version", "2025-06-18")
            resp = await self._post(url, {
                "jsonrpc": "2.0", "id": self._next_id(), "method": "initialize",
                "params": {"protocolVersion": proto, "capabilities": {},
                           "clientInfo": {"name": "qieshen-live", "version": "1.0"}}})
            if resp and not resp.get("error"):
                self._server_info = ((resp.get("result") or {}).get("serverInfo") or {})
                # 规范：initialize 之后补一条 initialized 通知
                await self._post(url, {"jsonrpc": "2.0",
                                       "method": "notifications/initialized",
                                       "params": {}})
            self._inited = True

    # ---------------- stdio ----------------
    async def _stdio_rpc(self, payload: dict):
        async with self._stdio_lock:
            if not self.proc or self.proc.returncode is not None:
                c = self.cfg.get("stdio", {})
                self.proc = await asyncio.create_subprocess_exec(
                    c.get("command", ""), *(c.get("args") or []),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL)
            self.proc.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode())
            await self.proc.stdin.drain()
            line = await asyncio.wait_for(self.proc.stdout.readline(), 6.0)
            return json.loads(line.decode("utf-8", "replace")) if line else {}

    async def close(self):
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()


async def probe_mcp(url: str, token: str = "", timeout: float = 8.0) -> dict:
    """对任意地址做一次「是不是 MCP」的体检：握手 + tools/list，不建会话、不计费。

    现实里最常见的错不是凭据错，而是**地址根本不是 MCP 入口**（贴了前端页面地址），
    所以这里要把 HTTP 层的症状翻译成人话，而不是只回一个 405。
    """
    url = (url or "").strip()
    out = {"url": url, "ok": False}
    if not url:
        out.update(error="未提供地址", hint="传 ?url=你的 MCP 地址")
        return out
    cfg = {"transport": "http", "timeout_ms": int(timeout * 1000),
           "http": {"url": url, "token": token}}
    c = McpClient(cfg)
    try:
        await asyncio.wait_for(c._ensure_init(), timeout)
        tools = await asyncio.wait_for(c.list_tools(), timeout)
    except asyncio.TimeoutError:
        out.update(error="超时 %.0fs" % timeout,
                   hint="域名解析或网络不通：确认本机能访问该地址、没被代理拦截")
        return out
    except Exception as e:
        out.update(error="%s: %s" % (type(e).__name__, str(e)[:150]),
                   hint="连接层失败，先确认地址可访问")
        return out

    info = c.info()
    out["server"] = info.get("server", "")
    out["tools_count"] = len(tools or [])
    out["tools"] = [{"name": t.get("name"),
                     "description": (t.get("description") or "")[:100]} for t in (tools or [])]
    if out["tools_count"]:
        out["ok"] = True
        out["hint"] = "握手与工具枚举均成功，可以直接接入"
        return out

    # 没拿到工具，把错误码翻成人话
    err = c._last_error or ""
    code = 0
    try:
        code = int(str(err).split()[1])
    except Exception:
        pass
    if code == 405:
        out["hint"] = ("该路径不接受 POST。405 常见于「贴了前端页面地址」——这是 SPA 路由，"
                       "不是 MCP 服务端。请找产品文档里的 API/MCP 入口（常是另一个域名或 /api/mcp）")
    elif code == 404:
        out["hint"] = "路径不存在：核对大小写、结尾斜杠，以及是不是少了 /mcp 之类的前缀"
    if code in (401, 403):
        out["hint"] = "路径存在但凭据被拒：确认 Bearer 令牌有效、且是给这个服务的"
    elif "令牌" in err or "Token" in err or "token" in err or "AUTH" in err:
        # 网关常见形态：HTTP 200 + JSON-RPC error 1001（缺少访问令牌 / Token 无效）
        out["hint"] = ("这个地址要访问令牌：把资料中心签发的 Token 填到「Bearer Token」栏。"
                       "注意它走的是 Authorization: Bearer 头，不是 URL 参数、也不是 X-Api-Key")
    elif "空响应" in err:
        out.update(hint="连上了但没返回 JSON-RPC：可能是网页或网关默认页，不是 MCP")
    else:
        out["hint"] = err or "未取到工具清单"
    out["error"] = err or "tools/list 为空"
    return out


__all__ = ["McpClient", "parse_sse", "json_rpc_http", "probe_mcp"]
