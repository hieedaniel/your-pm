"""纯标准库网络原语。

虽名为 wsproto，但本文件承担三类协议原语，供后台全网使用：
  1) WebSocket RFC6455 服务端握手与帧编解码（前端 /audio）
  2) WebSocket WSS 客户端握手（豆包流式 ASR）
  3) 极简 HTTP / HTTPS 客户端与 SSE 长连接（WMA ACP、MCP Streamable HTTP）

设计取舍：本机 Python 3.13 且零第三方包，aiohttp 安装不通。手写协议层让
「无依赖即可跑通全链路」成立；真实服务接入只需替换 provider，不动业务代码。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import ssl
import struct
from urllib.parse import urlsplit

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_CONT, OP_TEXT, OP_BIN, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


# --------------------------------------------------------------------------
# HTTP 头读取（WebSocket 握手、HTTP 响应、SSE 响应共用）
# --------------------------------------------------------------------------
async def _read_headers(reader: asyncio.StreamReader):
    raw = await reader.readuntil(b"\r\n\r\n")
    lines = raw.decode("latin-1").split("\r\n")
    first = lines[0]
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return first, headers


# --------------------------------------------------------------------------
# WebSocket 服务端
# --------------------------------------------------------------------------
async def ws_server_handshake(reader, writer, parsed=None):
    """读取浏览器 Upgrade 请求并回 101。返回 (path, headers)。

    parsed 可传入已由 _read_headers 解析好的 (first, headers)，供统一连接分发器使用，
    避免同一条连接被读两次请求头。
    """
    first, headers = parsed if parsed is not None else await _read_headers(reader)
    key = headers.get("sec-websocket-key", "")
    accept = base64.b64encode(
        hashlib.sha1((key + WS_GUID).encode("ascii")).digest()
    ).decode("ascii")
    resp = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n"
        "\r\n"
    )
    writer.write(resp.encode("latin-1"))
    await writer.drain()
    parts = first.split()
    return (parts[1] if len(parts) > 1 else "/"), headers


def build_frame(opcode: int, payload: bytes = b"", mask: bool = False) -> bytes:
    """构造单个 WebSocket 帧。客户端发送必须掩码，服务端发送不掩码。"""
    n = len(payload)
    b0 = 0x80 | opcode
    if n < 126:
        b1, ext = n, b""
    elif n < 65536:
        b1, ext = 126, struct.pack(">H", n)
    else:
        b1, ext = 127, struct.pack(">Q", n)
    if mask:
        b1 |= 0x80
        key = os.urandom(4)
        body = bytes(c ^ key[i % 4] for i, c in enumerate(payload))
        return bytes([b0, b1]) + ext + key + body
    return bytes([b0, b1]) + ext + payload


async def send_frame(writer, opcode: int, payload: bytes = b"", mask: bool = False):
    writer.write(build_frame(opcode, payload, mask))
    await writer.drain()


async def read_frame(reader):
    """返回 (opcode, payload, fin)。控制帧由调用方处理。"""
    b0, b1 = await reader.readexactly(2)
    fin = bool(b0 & 0x80)
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    n = b1 & 0x7F
    if n == 126:
        n = struct.unpack(">H", await reader.readexactly(2))[0]
    elif n == 127:
        n = struct.unpack(">Q", await reader.readexactly(8))[0]
    key = await reader.readexactly(4) if masked else None
    payload = await reader.readexactly(n) if n else b""
    if key:
        payload = bytes(c ^ key[i % 4] for i, c in enumerate(payload))
    return opcode, payload, fin


# --------------------------------------------------------------------------
# WebSocket 客户端（用于豆包 WSS，鉴权头在此注入）
# --------------------------------------------------------------------------
class WsHandshakeError(ConnectionError):
    """握手非 101 时抛出，保留状态码、X-Tt-Logid 与响应体供上层做精准诊断。

    火山在 403 时会返回 JSON 体，例如：
        {"error":"[resource_id=volc.seedasr.sauc.duration] requested resource not granted"}
    只看状态码只能得到「403」，拿到 body 才能区分「密钥错」「资源未开通」「项目不一致」。
    """

    def __init__(self, status_line: str, headers: dict, body: str = ""):
        self.status_line = (status_line or "").strip()
        self.headers = headers or {}
        self.body = body or ""
        self.status = 0
        try:
            self.status = int(self.status_line.split()[1])
        except (IndexError, ValueError):
            pass
        self.logid = self.headers.get("x-tt-logid", "")
        self.code = None
        self.message = ""
        if self.body:
            try:
                obj = json.loads(self.body)
                self.code = (obj.get("header") or {}).get("code", obj.get("code"))
                self.message = ((obj.get("header") or {}).get("message")
                                or obj.get("error") or obj.get("message") or "")
            except Exception:
                self.message = self.body.strip()[:300]
        super().__init__("WebSocket 握手失败: %s%s%s" % (
            self.status_line,
            (" | code=%s" % self.code) if self.code else "",
            (" | %s" % self.message) if self.message else ""))


async def _read_err_body(reader, headers: dict, timeout: float = 2.0) -> str:
    """非 101 时尽力读出响应体：优先 Content-Length，否则读到 EOF / 超时为止。"""
    try:
        n = int(headers.get("content-length", "0") or 0)
    except ValueError:
        n = 0
    try:
        if n > 0:
            return (await asyncio.wait_for(reader.readexactly(n), timeout)).decode("utf-8", "replace")
        chunks = []
        while True:
            d = await asyncio.wait_for(reader.read(4096), timeout)
            if not d:
                break
            chunks.append(d)
            if sum(len(c) for c in chunks) > 8192:
                break
        return b"".join(chunks).decode("utf-8", "replace")
    except (asyncio.TimeoutError, asyncio.IncompleteReadError, Exception):
        return ""


async def ws_client_connect(url: str, headers: dict | None = None, timeout: float = 8.0):
    u = urlsplit(url)
    tls = u.scheme in ("wss", "https")
    host = u.hostname
    port = u.port or (443 if tls else 80)
    kwargs = {}
    if tls:
        kwargs["ssl"] = ssl.create_default_context()
        kwargs["server_hostname"] = host
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port, **kwargs), timeout
    )
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    path = u.path or "/"
    if u.query:
        path += "?" + u.query
    lines = [
        f"GET {path} HTTP/1.1",
        f"Host: {host}:{port}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {key}",
        "Sec-WebSocket-Version: 13",
    ]
    for k, v in (headers or {}).items():
        lines.append(f"{k}: {v}")
    lines += ["", ""]
    writer.write("\r\n".join(lines).encode("latin-1"))
    await writer.drain()
    status_line, resp_headers = await asyncio.wait_for(_read_headers(reader), timeout)
    if "101" not in status_line:
        body = await _read_err_body(reader, resp_headers)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        raise WsHandshakeError(status_line, resp_headers, body)
    return reader, writer, resp_headers


# --------------------------------------------------------------------------
# HTTP / SSE 客户端原语（WMA ACP、MCP Streamable HTTP、OpenAI 兼容接口）
# --------------------------------------------------------------------------
def _open(url: str, timeout: float):
    u = urlsplit(url)
    tls = u.scheme == "https"
    host = u.hostname
    port = u.port or (443 if tls else 80)
    kwargs = {}
    if tls:
        kwargs["ssl"] = ssl.create_default_context()
        kwargs["server_hostname"] = host
    return asyncio.wait_for(asyncio.open_connection(host, port, **kwargs), timeout), (u, host, port)


def _http_head(method: str, u, host: str, headers: dict, body: bytes,
               connection: str = "close") -> bytes:
    path = u.path or "/"
    if u.query:
        path += "?" + u.query
    lines = [
        f"{method} {path} HTTP/1.1",
        f"Host: {host}",
        f"Connection: {connection}",
        "User-Agent: qieshen-live/1.0",
    ]
    for k, v in headers.items():
        lines.append(f"{k}: {v}")
    if body:
        lines.append(f"Content-Length: {len(body)}")
    lines += ["", ""]
    return "\r\n".join(lines).encode("latin-1") + body


async def _read_body(reader, headers: dict) -> bytes:
    if headers.get("transfer-encoding", "").lower() == "chunked":
        out = bytearray()
        while True:
            line = (await reader.readline()).decode("latin-1").strip()
            size = int(line.split(";")[0], 16)
            if size == 0:
                await reader.readuntil(b"\r\n")
                break
            out += await reader.readexactly(size)
            await reader.readexactly(2)
        return bytes(out)
    n = int(headers.get("content-length", "0") or 0)
    return await reader.readexactly(n) if n else b""


async def http_request(url: str, method: str = "GET", headers: dict | None = None,
                       body: bytes | None = None, timeout: float = 10.0):
    """返回 (status:int, headers:dict, body:bytes)。一次性请求，非流式。"""
    coro, (u, host, port) = _open(url, timeout)
    reader, writer = await coro
    try:
        writer.write(_http_head(method, u, host, headers or {}, body or b""))
        await writer.drain()
        status_line, resp_headers = await asyncio.wait_for(_read_headers(reader), timeout)
        data = await asyncio.wait_for(_read_body(reader, resp_headers), timeout)
        return int(status_line.split()[1]), resp_headers, data
    finally:
        writer.close()


class ChunkedStream:
    """把 chunked 传输解成逐行流。

    SSE 服务端（含网关）常以 Transfer-Encoding: chunked 回包，直接 readline
    会把 12/3a4 这类长度行当成事件内容，症状是"连上了但收到一堆乱码"。
    """

    def __init__(self, reader):
        self.r = reader
        self.buf = b""
        self.eof = False

    async def _fill(self):
        if self.eof:
            return
        line = await self.r.readline()
        if not line:
            self.eof = True
            return
        try:
            size = int(line.strip().split(b";")[0] or b"0", 16)
        except ValueError:
            self.eof = True
            return
        if size == 0:
            self.eof = True
            try:
                await self.r.readuntil(b"\r\n")
            except Exception:
                pass
            return
        self.buf += await self.r.readexactly(size)
        await self.r.readexactly(2)

    async def readline(self):
        while b"\n" not in self.buf and not self.eof:
            await self._fill()
        i = self.buf.find(b"\n")
        if i < 0:
            out, self.buf = self.buf, b""
            return out
        out, self.buf = self.buf[:i + 1], self.buf[i + 1:]
        return out


async def open_sse(url: str, headers: dict | None = None, timeout: float = 10.0):
    """建立 SSE 长连接，返回 (reader, writer, response_headers)。调用方逐行读。

    两个必须做对的地方：Connection 不能是 close（否则服务端在首个响应后拆连接），
    chunked 必须解包（否则读到的不是 data: 行）。
    """
    coro, (u, host, port) = _open(url, timeout)
    reader, writer = await coro
    h = dict(headers or {})
    h.setdefault("Accept", "text/event-stream")
    writer.write(_http_head("GET", u, host, h, b"", connection="keep-alive"))
    await writer.drain()
    status_line, resp_headers = await asyncio.wait_for(_read_headers(reader), timeout)
    if "200" not in status_line and "201" not in status_line:
        writer.close()
        raise ConnectionError(f"SSE 连接失败: {status_line.strip()}")
    if resp_headers.get("transfer-encoding", "").lower() == "chunked":
        reader = ChunkedStream(reader)
    return reader, writer, resp_headers


async def json_rpc_http(url: str, payload: dict, headers: dict | None = None,
                        timeout: float = 10.0):
    """MCP Streamable HTTP 的 JSON-RPC 调用，兼容纯 JSON 与 SSE 包装两种返回。"""
    import json
    h = {"Content-Type": "application/json",
         "Accept": "application/json, text/event-stream"}
    h.update(headers or {})
    status, _, raw = await http_request(
        url, "POST", h, json.dumps(payload, ensure_ascii=False).encode("utf-8"), timeout
    )
    text = raw.decode("utf-8", "replace").strip()
    if text.startswith("event:") or text.startswith("data:"):
        for line in text.splitlines():
            if line.startswith("data:"):
                text = line[5:].strip()
                break
    if not text:
        return {"error": {"code": status, "message": "空响应"}}
    try:
        return json.loads(text)
    except Exception:
        return {"error": {"code": status, "message": text[:200]}}
