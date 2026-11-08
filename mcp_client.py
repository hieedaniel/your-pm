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
        resp = await self._rpc("tools/call", {"name": name, "arguments": args})
        if not resp or "error" in resp:
            err = (resp or {}).get("error", "MCP 无响应")
            self._last_error = json.dumps(err, ensure_ascii=False)[:200]
            return {"error": err}
        result = resp.get("result") or {}
        if isinstance(result.get("structuredContent"), dict):
            return result["structuredContent"]
        for item in result.get("content") or []:
            if item.get("type") == "text":
                try:
                    return json.loads(item["text"])
                except Exception:
                    return {"text": item["text"]}
        return result

    async def _rpc(self, method: str, params: dict):
        payload = {"jsonrpc": "2.0", "id": self._next_id(),
                   "method": method, "params": params}
        try:
            if self.transport == "local" and self.local:
                return self.local.handle(payload)
            if self.transport == "http":
                return await self._http_rpc(payload)
            if self.transport == "stdio":
                return await self._stdio_rpc(payload)
        except Exception as e:
            self._last_error = "%s: %s" % (type(e).__name__, e)
            return {"error": self._last_error}
        return {"error": "未支持的 transport: %s" % self.transport}

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
    elif code in (401, 403):
        out["hint"] = "路径存在但凭据被拒：确认 Bearer 令牌有效、且是给这个服务的"
    elif "空响应" in err:
        out.update(hint="连上了但没返回 JSON-RPC：可能是网页或网关默认页，不是 MCP")
    else:
        out["hint"] = err or "未取到工具清单"
    out["error"] = err or "tools/list 为空"
    return out


__all__ = ["McpClient", "parse_sse", "json_rpc_http", "probe_mcp"]
