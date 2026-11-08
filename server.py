"""切身产品经理 · 实时音频链路后台（Python 3.13 纯标准库 / 零第三方依赖）

启动：
    cd qieshen-live && python server.py
    浏览器打开 http://127.0.0.1:8780

端点：
    GET  /                 前端单页
    GET  /audio_worklet.js 浏览器重采样处理器
    GET  /sample.wav       内置示例音频（无麦克风一键验证音频上行）
    WS   /audio            音频上行（binary 200ms PCM + text 控制帧）
    GET  /events           SSE 事件流（字幕 / 主题 / 提示卡 / audio.stats / 状态）
    GET  /api/state        当前配置与统计
    GET  /api/probe        真实豆包握手探测（密钥 + Resource ID 是否可用）
    GET  /api/wma-probe    WMA 控制面探测（企业 ID + pt_/ck_ 凭据 + 服务是否开通）
    GET  /api/tools        MCP tools/list
    GET  /api/docs         文档清单
    GET  /api/demo         演示会谈话轮
    POST /turn             注入一个定稿话轮（无麦克风联调 / curl 测试）
    POST /reset            重置统计与抑制状态

所有外部凭据只在后台：豆包 X-Api-Key、WMA Bearer、MCP token 都不会出现在前端。
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from urllib.parse import urlsplit, parse_qs

from wsproto import (_read_headers, ws_server_handshake, read_frame,
                     send_frame)
from doubao_asr import (build_asr, validate_asr_config, probe_handshake,
                        RESOURCE_IDS, ENDPOINTS)
from mcp_docs import LocalMcpServer
from mcp_client import McpClient, probe_mcp
from mcp_multi import MultiSourceSearch
from mcp_perf import PerfSearchProxy
from wma_agent import build_extractor, extractor_info, probe_wma
from orchestrator import Orchestrator

ROOT = os.path.dirname(os.path.abspath(__file__))
# 容器化部署：配置文件路径与监听地址允许用环境变量覆盖。
# 镜像里不放 config.json（里面有豆包 Key、WMA 凭据），部署时用卷挂进来；
# 监听地址不写死，容器内必须 0.0.0.0，本机开发仍是 127.0.0.1。
CFG_PATH = os.environ.get("QS_CONFIG") or os.path.join(ROOT, "config.json")
with open(CFG_PATH, "r", encoding="utf-8") as _f:
    CFG = json.load(_f)
CFG.setdefault("server", {})
for _env, _key in (("QS_HOST", "host"), ("QS_PORT", "port")):
    _v = os.environ.get(_env)
    if _v:
        CFG["server"][_key] = int(_v) if _key == "port" else _v

SUBSCRIBERS = set()
SESSIONS = {}
LOCAL, MCP, EXTRACTOR, ORCH = None, None, None, None
SEARCH = None      # 编排器实际拿到的检索入口：适配层 / 多源合成 / 原生 MCP
SOURCE_CLIENTS = []  # [{"name","client"}] 每个启用的数据源一个客户端
ASR_CHECK = None
VOCAB = {}         # 领域词表，WMA 热切换时复用

DEMO_SETS = {
    # 本地知识库 docs-kaiyi/（凯翼智慧园区方案）时用这套：
    # 每句话都对着文档里真有的场景，客户怎么说就怎么念，不用产品黑话
    "kaiyi": [
        ("客户", "我们园区现在访客来了还要前台打电话确认，太麻烦。", "zh-CN"),
        ("客户", "一期装的摄像头挺多，可出了问题只能事后翻录像。", "zh-CN"),
        ("客户", "配电室还得人天天去抄表，能不能远程看？", "zh-CN"),
        ("客户", "一个月电费好几十万，到底哪儿浪费了说不清。", "zh-CN"),
        ("销售", "这块我先记一下，回头给您一个对比口径。", "zh-CN"),
        ("客户", "消防主机报警了，还得保安跑过去确认，太慢了。", "zh-CN"),
        ("客户", "中午食堂排队都排到门外了，能刷脸直接扣吗？", "zh-CN"),
        ("客户", "会议室经常有人订了不用，别人又订不上。", "zh-CN"),
        ("客户", "这些系统能不能都汇总到一个大屏上看？", "zh-CN"),
        ("客户", "月台一天卸不了几车，司机都在外面排队等。", "zh-CN"),
        ("客户", "同行的园区都做到什么程度了？有没有案例？", "zh-CN"),
        ("客户", "预算有限，能不能分期建设，先做一块？", "zh-CN"),
        ("客户", "您好，路上堵车了吧，先喝杯水。", "zh-CN"),
    ],
    # 接真实 perf 系统时用这套：齿科导航产品线，话里带的是真实需求/问题单的词
    "perf": [
        ("客户", "无CT模式下的运动采集，数据准不准？", "zh-CN"),
        ("客户", "AI 预设计颌垫现在能自动出方案吗？", "zh-CN"),
        ("客户", "方案对比只能对下颌吗？我们上下颌都要看。", "zh-CN"),
        ("客户", "髁突干扰分析这块你们现在有了没有？", "zh-CN"),
        ("客户", "3.0.1 版本创建剖面的时候会闪退吧？", "zh-CN"),
        ("客户", "我们现有电子面弓 Nexjaw，能和你们软件打通吗？", "zh-CN"),
        ("销售", "这个我先确认一下能力边界。", "zh-CN"),
        ("客户", "这个报价太贵了，预算不好过。", "zh-CN"),
        ("客户", "您好，路上堵车了吧，先喝杯水。", "zh-CN"),
    ],
    # 离线（内置 docs/）时用这套：边缘网关场景，和 docs/ 里的文档对得上
    "docs": [
        ("客户", "灵犀Pro在车间零下二十度能扛得住吗？", "zh-CN"),
        ("客户", "那能内置5G模组吗？我们现场没有有线网络。", "zh-CN"),
        ("销售", "这个我先确认一下能力边界。", "zh-CN"),
        ("客户", "你们标称 10 万点/秒，是单机还是集群？我们只有两台机器。", "zh-CN"),
        ("客户", "为什么不做成公有云 SaaS？别家都是云交付。", "zh-CN"),
        ("客户", "竞品X报价便宜一些，技术上到底差在哪？", "zh-CN"),
        ("客户", "星河平台在信创环境现在支持吗？", "zh-CN"),
        ("客户", "下一代网关大概什么规格？有没有 A200？", "zh-CN"),
        ("客户", "Can the edge gateway run fanless at low temperature?", "en-US"),
        ("客户", "灵犀Pro报价多少？太贵了预算不好过。", "zh-CN"),
        ("客户", "您好，路上堵车了吧，先喝杯水。", "zh-CN"),
        ("客户", "交期多久？能不能热更新不停机？", "zh-CN"),
    ],
}
DEMO_TURNS = DEMO_SETS["docs"]


# --------------------------------------------------------------------------
# 广播
# --------------------------------------------------------------------------
async def emit(event: str, data: dict):
    payload = "event: %s\ndata: %s\n\n" % (
        event, json.dumps(data, ensure_ascii=False))
    for q in list(SUBSCRIBERS):
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            pass


# --------------------------------------------------------------------------
# 启动装配
# --------------------------------------------------------------------------
def build_sources(mcfg: dict, vocab: dict):
    """按 config.mcp.sources 组装数据源；没有 sources 就退回旧的单源写法。"""
    global SOURCE_CLIENTS
    SOURCE_CLIENTS = []
    # transport=local 时不建任何外部源：否则下面的兼容分支会拿 mcp.http.url
    # 当「默认源」，本地知识库还没接上就被一条过期的 perf 地址顶掉了
    if (mcfg.get("transport") or "local") == "local":
        return []
    raw = mcfg.get("sources") or []
    if not raw:      # 兼容旧配置：mcp.transport / mcp.http / mcp.adapter
        raw = [{"name": "default", "enabled": True, "adapter": mcfg.get("adapter", "none"),
                "url": mcfg.get("http", {}).get("url", ""),
                "token": mcfg.get("http", {}).get("token", ""),
                "weight": 1.0, "timeout_ms": mcfg.get("timeout_ms", 6000)}]
    entries = []
    for s in raw:
        if not s.get("enabled", True):
            continue
        name = s.get("name") or "source"
        client = McpClient({"transport": "http",
                            "timeout_ms": s.get("timeout_ms", 2500),
                            "http": {"url": s.get("url", ""), "token": s.get("token", "")}})
        SOURCE_CLIENTS.append({"name": name, "client": client})
        ad = s.get("adapter", "none")
        if ad == "perf":
            proxy = PerfSearchProxy(client, mcfg.get("perf"), vocab)
            prefix = "perf:"
        else:
            proxy = client        # 期望该源原生提供 docs.search
            prefix = name + ":"
        entries.append({"name": name, "proxy": proxy, "adapter": ad,
                        "weight": s.get("weight", 1.0), "prefix": prefix,
                        "timeout_ms": s.get("timeout_ms", 2500), "enabled": True})
    return entries


def bootstrap():
    global LOCAL, MCP, EXTRACTOR, ORCH, ASR_CHECK, SEARCH, DEMO_TURNS
    # 接入形态自检：Resource ID / 版本 / 计费 / 鉴权 / endpoint / StreamMode / 项目
    ASR_CHECK = validate_asr_config(
        CFG["asr"], strict=(CFG["asr"].get("provider") == "doubao"))
    mcfg = CFG["mcp"]
    docs_dir = mcfg["docs_dir"]
    if not os.path.isabs(docs_dir):
        docs_dir = os.path.join(ROOT, docs_dir)
    LOCAL = LocalMcpServer(docs_dir)
    MCP = McpClient(mcfg, LOCAL)

    tags = LOCAL.store.hotwords(60)
    # 别名只在「同一个概念有多个叫法」时才需要；其余概念靠文档 tags 覆盖
    aliases = {
        "智慧园区": ["智慧园区", "园区智能化"],
        "数字化月台": ["月台", "数字化月台"],
        "一脸通": ["一脸通", "无感通行"],
        "showOS": ["showos", "大屏系统"],
    }
    vocab = {"tags": tags, "aliases": aliases}
    VOCAB.clear()
    VOCAB.update(vocab)
    EXTRACTOR = build_extractor(CFG["wma"], vocab)

    orch_cfg = dict(mcfg)
    orch_cfg["policy"] = CFG["policy"]
    # 适配层：真实 MCP 的工具是业务 CRUD，编排器要的是 docs.search 语义。
    # 换回本地文档只改 config.mcp.adapter = "none"。
    entries = build_sources(mcfg, vocab)
    if len(entries) > 1:
        SEARCH = MultiSourceSearch(entries)
    elif entries:
        SEARCH = entries[0]["proxy"]
    elif mcfg.get("adapter") == "perf":
        SEARCH = PerfSearchProxy(MCP, mcfg.get("perf"), vocab)
    else:
        SEARCH = MCP
    # 演示脚本跟着数据源走，否则播出来全是空卡
    use_perf = (any(e.get("adapter") == "perf" for e in entries)
                or mcfg.get("adapter") == "perf")
    DEMO_TURNS = (DEMO_SETS.get(mcfg.get("demo_set") or "")
                  or (DEMO_SETS["perf"] if use_perf else DEMO_SETS["docs"]))
    ORCH = Orchestrator(orch_cfg, SEARCH, EXTRACTOR, emit)

    # 热词：配置 + 文档标签，去重后只保留 20 个（双向流式上限 100 tokens）
    hw, seen = [], set()
    for w in list(CFG["asr"].get("hotwords", [])):
        if w and w not in seen:
            seen.add(w)
            hw.append(w)
    for w in tags:
        if w not in seen and len(hw) < 20:
            seen.add(w)
            hw.append(w)
    CFG["asr"]["hotwords"] = hw[:20]
    return LOCAL, MCP, EXTRACTOR, ORCH


# --------------------------------------------------------------------------
# HTTP 响应
# --------------------------------------------------------------------------
def _resp(status: int, body: bytes, ctype: str = "application/json; charset=utf-8",
          extra: str = "") -> bytes:
    reason = {200: "OK", 201: "Created", 400: "Bad Request",
              404: "Not Found", 500: "Internal Server Error"}.get(status, "OK")
    head = ("HTTP/1.1 %d %s\r\nContent-Type: %s\r\nContent-Length: %d\r\n"
            "Cache-Control: no-store\r\nConnection: close\r\n%s\r\n" %
            (status, reason, ctype, len(body), extra))
    return head.encode("latin-1") + body


def _json(obj) -> bytes:
    return _resp(200, json.dumps(obj, ensure_ascii=False).encode("utf-8"))


def _wma_ready() -> dict:
    """WMA 配置就绪度：一眼看出申请到哪一步、还差哪一项。"""
    a = CFG["wma"].get("acp") or {}
    return {
        "enterprise_id_configured": bool(a.get("enterprise_id")),
        "credential": "pt_应用Key" if a.get("app_key") else
                      ("ck_个人Key" if a.get("user_key") else ""),
        "agent_id_configured": bool(a.get("agent_id")),
        "direct_link": bool(a.get("link") and a.get("token")),
    }


def state():
    return {
        "server": CFG["server"],
        "asr": {"provider": CFG["asr"].get("provider"),
                "default_lang": CFG["asr"].get("default_lang"),
                "stream_langs": CFG["asr"].get("stream_langs"),
                "segment_ms": CFG["asr"].get("segment_ms"),
                "hotwords": len(CFG["asr"].get("hotwords", [])),
                "auth_configured": bool(CFG["asr"].get("api_key") or CFG["asr"].get("app_key")),
                "shape": ASR_CHECK["shape"] if ASR_CHECK else {},
                "validation": ASR_CHECK or {},
                "resource_ids": RESOURCE_IDS,
                "endpoints": ENDPOINTS},
        "wma": dict(extractor_info(EXTRACTOR), **_wma_ready()),
        "mcp": {"sources": [{"name": n["name"], "info": n["client"].info()}
                            for n in SOURCE_CLIENTS] or [MCP.info() if MCP else {}],
                "adapter": CFG["mcp"].get("adapter", "none"),
                "search": SEARCH.stats() if hasattr(SEARCH, "stats") else {}},
        "docs": (LOCAL.store.listing() if (LOCAL and CFG["mcp"].get("adapter") != "perf"
                                          and len(SOURCE_CLIENTS) <= 1) else []),
        "chunks": len(LOCAL.store.chunks) if (LOCAL and CFG["mcp"].get("adapter") != "perf") else 0,
        "sessions": len(SESSIONS),
        "stats": ORCH.stats() if ORCH else {},
        "policy": CFG["policy"],
    }


async def handle_http(writer, method, path, headers, body: bytes):
    u = urlsplit(path)
    p = u.path
    if p in ("/", "/index.html"):
        with open(os.path.join(ROOT, "index.html"), "rb") as f:
            data = f.read()
        writer.write(_resp(200, data, "text/html; charset=utf-8"))
    elif p == "/audio_worklet.js":
        with open(os.path.join(ROOT, "audio_worklet.js"), "rb") as f:
            data = f.read()
        writer.write(_resp(200, data, "application/javascript; charset=utf-8"))
    elif p == "/sample.wav":
        with open(os.path.join(ROOT, "sample.wav"), "rb") as f:
            data = f.read()
        writer.write(_resp(200, data, "audio/wav"))
    elif p == "/api/state":
        writer.write(_json(state()))
    elif p == "/api/probe":
        # 真实握手探测：只回答问题「这个 Key + 这个 Resource ID 能不能用」，不发音频
        r = await probe_handshake(CFG["asr"])
        if r.get("ok"):
            await emit("status", {"level": "info",
                                  "message": "豆包 ASR 密钥检测通过 · %s" % r["resource_id"]})
        else:
            await emit("status", {"level": "warn",
                                  "message": "豆包 ASR 密钥检测失败 HTTP %s · %s" %
                                             (r.get("status") or "?", (r.get("message") or "")[:80])})
        writer.write(_json(r))
    elif p == "/api/wma-probe":
        # 只发一次控制面 GET /agents：验证企业 ID + 凭据 + 服务是否开通。
        # 不建会话、不建 Agent、不产生会话时长计费。
        r = await probe_wma(CFG["wma"].get("acp") or {})
        level = "info" if r.get("ok") else "warn"
        await emit("status", {"level": level,
                              "message": "WMA 检测%s · %s" %
                                         ("通过" if r.get("ok") else "未通过",
                                          (r.get("message") or "")[:90])})
        writer.write(_json(r))
    elif p == "/api/tools":
        tools = []
        if len(SOURCE_CLIENTS) > 1:
            for n in SOURCE_CLIENTS:
                try:
                    for t in await asyncio.wait_for(n["client"].list_tools(), 8):
                        tools.append(dict(t, source_name=n["name"]))
                except Exception:
                    continue
        else:
            tools = await MCP.list_tools()
        writer.write(_json({"tools": tools,
                            "hotwords": CFG["asr"].get("hotwords", [])}))
    elif p == "/api/mcp-probe":
        q = parse_qs(u.query)
        url = (q.get("url") or [""])[0] or CFG["mcp"].get("http", {}).get("url", "")
        tok = (q.get("token") or [""])[0] or CFG["mcp"].get("http", {}).get("token", "")
        writer.write(_json(await probe_mcp(url, tok, 8.0)))
    elif p == "/api/docs":
        if CFG["mcp"].get("adapter") == "perf":
            writer.write(_json({"docs": await SEARCH.listing(), "source": "perf"}))
        else:
            writer.write(_json({"docs": LOCAL.store.listing()}))
    elif p == "/api/demo":
        writer.write(_json({"turns": [{"speaker": s, "text": t, "lang": l}
                                      for s, t, l in DEMO_TURNS]}))
    elif p == "/turn" and method == "POST":
        try:
            d = json.loads(body.decode("utf-8", "replace") or "{}")
        except Exception:
            d = {}
        text = (d.get("text") or "").strip()
        if not text:
            writer.write(_json({"ok": False, "error": "text 为空"}))
        else:
            # 编排器异常不能把 HTTP 连接挂死：浏览器等不到响应会以为是服务死了
            try:
                await ORCH.on_final(text, d.get("lang", CFG["asr"]["default_lang"]),
                                    {"speaker": d.get("speaker", "客户"), "source": "inject"})
                writer.write(_json({"ok": True, "turns": ORCH.turn_no}))
            except Exception as e:
                print(" [错误] /turn 处理失败：%s: %s" % (type(e).__name__, str(e)[:200]))
                import traceback
                traceback.print_exc()
                writer.write(_json({"ok": False, "error": "%s: %s" %
                                    (type(e).__name__, str(e)[:160])}))
    elif p == "/reset" and method == "POST":
        ORCH.reset()
        await emit("stats", ORCH.stats())
        writer.write(_json({"ok": True}))
    else:
        writer.write(_resp(404, b'{"error":"not found"}'))
    await writer.drain()


# --------------------------------------------------------------------------
# SSE /events
# --------------------------------------------------------------------------
async def handle_sse(writer):
    head = ("HTTP/1.1 200 OK\r\nContent-Type: text/event-stream; charset=utf-8\r\n"
            "Cache-Control: no-cache, no-transform\r\nConnection: keep-alive\r\n"
            "X-Accel-Buffering: no\r\n\r\n")
    writer.write(head.encode("latin-1"))
    await writer.drain()
    q = asyncio.Queue(maxsize=300)
    SUBSCRIBERS.add(q)
    try:
        for ev, data in [
            ("hello", {"ok": True, "t": round(time.time(), 3)}),
            ("status", {"level": "info", "message": "已连接 SSE 事件流",
                        "t": round(time.time(), 3)}),
            ("status", {"level": "info", "message":
                        "ASR=%s · WMA=%s · MCP=%s · 文档 %d 篇" %
                        (CFG["asr"].get("provider"),
                         extractor_info(EXTRACTOR).get("provider"),
                         CFG["mcp"].get("transport"), len(LOCAL.store.docs))}),
            ("stats", ORCH.stats()),
        ]:
            writer.write(("event: %s\ndata: %s\n\n" %
                          (ev, json.dumps(data, ensure_ascii=False))).encode("utf-8"))
        await writer.drain()
        while True:
            try:
                item = await asyncio.wait_for(q.get(), 15)
            except asyncio.TimeoutError:
                item = "event: ping\ndata: {}\n\n"
            writer.write(item.encode("utf-8") if isinstance(item, str) else item)
            await writer.drain()
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass
    finally:
        SUBSCRIBERS.discard(q)


# --------------------------------------------------------------------------
# WS /audio
# --------------------------------------------------------------------------
async def handle_audio(reader, writer, parsed, path):
    _, req_headers = await ws_server_handshake(reader, writer, parsed)
    q = parse_qs(urlsplit(path).query)
    sid = (q.get("session") or ["s%d" % int(time.time() * 1000)])[0]
    lang = (q.get("lang") or [CFG["asr"]["default_lang"]])[0]

    callbacks = {
        "on_partial": ORCH.on_partial,
        "on_final": ORCH.on_final,
        "on_status": ORCH.on_status,
    }
    async def make_asr(new_lang):
        inst = build_asr(CFG["asr"], callbacks, sid)
        try:
            await inst.start(new_lang, CFG["asr"].get("hotwords", []))
        except Exception as e:
            # 不截断：鉴权失败的诊断包含项目一致性 / Resource ID 拼写 / X-Tt-Logid
            await emit("status", {"level": "warn",
                                  "message": "真实 ASR 连接失败，自动降级 mock：%s" % str(e)})
            inst = build_asr({**CFG["asr"], "provider": "mock"}, callbacks, sid)
            await inst.start(new_lang, CFG["asr"].get("hotwords", []))
        return inst

    asr = await make_asr(lang)
    SESSIONS[sid] = asr
    await emit("status", {"level": "info",
                          "message": "音频会话 %s 开始 · provider=%s · lang=%s · track=%s" %
                                     (sid, asr.mode, lang, getattr(asr, "track", "mock"))})
    audio_frames = audio_bytes = 0
    last_audio_stat = 0.0
    chunk_s = CFG["asr"].get("chunk_ms", 200) / 1000.0

    # 会话是否能吃音频。control:stop 之后为 False —— 但 WS 还开着，前端「再开始录音」
    # 时如果复用同一条连接（旧版本就是这么做的），后续帧会打到已 stop 的实例上：
    # DoubaoASR 的 writer 已被置 None，feed() 里 send_frame(None) 抛 AttributeError，
    # 而这个异常不在下面的 except 列表里，整个 WS 被直接打死，表现就是「第二次录音
    # 一个字都不出」。所以这里不假设一条 WS = 一次录音，而是允许会话原地复活。
    state = {"alive": True}

    async def revive(reason: str):
        nonlocal asr
        try:
            await asr.stop()
        except Exception:
            pass
        asr = await make_asr(lang)
        SESSIONS[sid] = asr
        state["alive"] = True
        await emit("status", {"level": "info",
                              "message": "音频会话 %s 重新开始（%s）· provider=%s · track=%s" %
                                         (sid, reason, asr.mode, getattr(asr, "track", "mock"))})

    async def audio_stat(force=False):
        now_t = time.time()
        if not force and audio_frames > 1 and now_t - last_audio_stat < 0.8:
            return
        await emit("audio.stats", {
            "session": sid, "frames": audio_frames, "bytes": audio_bytes,
            "seconds": round(audio_frames * chunk_s, 1),
            "provider": asr.mode, "lang": lang,
            "track": getattr(asr, "track", "mock"),
            "voice_frames": getattr(asr, "voice_frames", 0),
        })

    try:
        while True:
            op, payload, _ = await read_frame(reader)
            if op == 0x8:
                break
            if op == 0x9:
                await send_frame(writer, 0xA, payload)
                continue
            if op == 0x2:
                if not state["alive"]:
                    # 前端没发 control:start 就继续推流（旧版前端 / 直接回放音频）：
                    # 含语音的帧到了就自动重启会话，宁可多开一次连接也不静音。
                    await revive("收到停止后的音频帧")
                try:
                    await asr.feed(payload)
                except Exception as e:
                    # 任何单帧失败都只能影响这一帧，绝不能掀掉整条 WS
                    await emit("status", {"level": "warn",
                                          "message": "音频帧写入失败，会话重启：%s: %s" %
                                                     (type(e).__name__, e)})
                    await revive("音频帧写入异常")
                    continue
                audio_frames += 1
                audio_bytes += len(payload)
                if audio_frames == 1 or time.time() - last_audio_stat >= 0.8:
                    last_audio_stat = time.time()
                    await audio_stat(force=True)
                continue
            if op == 0x1:
                try:
                    d = json.loads(payload.decode("utf-8", "replace"))
                except Exception:
                    continue
                typ = d.get("type")
                if typ in ("mock_say", "text_turn"):
                    if not state["alive"]:
                        await revive("文本注入前补开会话")
                    try:
                        await asr.inject_text(d.get("text", ""),
                                              d.get("speaker", "客户"),
                                              {"lang": d.get("lang", lang)})
                    except Exception as e:
                        await emit("status", {"level": "warn",
                                              "message": "文本注入失败：%s: %s" %
                                                         (type(e).__name__, e)})
                elif typ == "control":
                    action = d.get("action")
                    if action == "set_lang":
                        new_lang = d.get("lang", lang)
                        await asr.stop()
                        asr = await make_asr(new_lang)
                        lang = new_lang
                        SESSIONS[sid] = asr
                        state["alive"] = True
                        await emit("status", {"level": "info",
                                              "message": "语种切换为 %s（%s 轨）" %
                                                         (new_lang, getattr(asr, "track", "mock"))})
                    elif action == "stop":
                        await asr.stop()
                        state["alive"] = False
                    elif action == "start":
                        # 前端「再开始录音」在复用旧连接时发这一帧
                        await revive("control:start")
                elif typ == "ping":
                    await send_frame(writer, 0x1, b'{"type":"pong"}')
    except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError, OSError):
        pass
    finally:
        if audio_frames:
            await audio_stat(force=True)   # 收尾回报，保证前端拿到最终帧数
        await asr.stop()
        SESSIONS.pop(sid, None)
        await emit("status", {"level": "info",
                              "message": "音频会话 %s 结束 · 共 %d 帧 / %d B" %
                                         (sid, audio_frames, audio_bytes)})
        writer.close()


# --------------------------------------------------------------------------
# 连接分发
# --------------------------------------------------------------------------
async def handle_conn(reader, writer):
    try:
        parsed = await asyncio.wait_for(_read_headers(reader), 15)
    except Exception:
        writer.close()
        return
    first, headers = parsed
    parts = first.split()
    if len(parts) < 2:
        writer.close()
        return
    method, path = parts[0], parts[1]
    try:
        if headers.get("upgrade", "").lower() == "websocket" and path.startswith("/audio"):
            await handle_audio(reader, writer, parsed, path)
            return
        if path == "/events":
            await handle_sse(writer)
            return
        n = int(headers.get("content-length", "0") or 0)
        body = await reader.readexactly(n) if n else b""
        await handle_http(writer, method, path, headers, body)
    except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError, OSError):
        pass
    except Exception as e:
        print("[server] handler error: %s: %s" % (type(e).__name__, e))
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def _warm_wma():
    """provider=auto：启动时探一次控制面，通了就热切到云端主题提取。

    设计意图：凭据办下来之后只需要重启服务，不用改配置、不用改代码；
    凭据没通时静默沿用本地规则，演示不因此中断。
    """
    global EXTRACTOR
    wcfg = CFG.get("wma") or {}
    acp = wcfg.get("acp") or {}
    if wcfg.get("provider") != "auto":
        return
    t0 = time.perf_counter()
    try:
        r = await probe_wma(acp)
    except Exception as e:  # noqa: BLE001
        print(" WMA 预热    : 探测异常 %s: %s" % (type(e).__name__, str(e)[:90]))
        return
    if not r.get("ok"):
        print(" WMA 预热    : 未接通，沿用本地规则 — %s" % (r.get("message") or "未知原因"))
        if r.get("hint"):
            print("               下一步：%s" % r["hint"])
        return

    # 控制面 200 只证明「凭据 + 企业 ID 对得上」，不代表能开会话：
    # 订阅到期 / 欠费 / Agent 不存在都会在建会话那一步才炸。
    # 所以这里再往前走一步，通不了就别切 —— 否则每轮都要白等一次失败的超时。
    cand = build_extractor(dict(wcfg, provider="acp"), VOCAB)
    try:
        await cand.primary._create_session()
        await cand.primary._user_token()
        reuse = True
    except Exception as e:  # noqa: BLE001
        reuse = False
        reason = str(e)
        print(" WMA 预热    : 控制面已通（Agent %s 个），但数据面不可用，沿用本地规则"
              % r.get("agents_total"))
        print("               原因：%s" % reason[:160])
        if "PrecheckBlocked" in reason or "订阅" in reason:
            print("               下一步：企业控制台 → 订阅与用量 → 开通/续费 Managed Agents"
                  "（企业订阅到期时控制面照常返回，只有建会话被拦）")
        return

    EXTRACTOR = cand
    ORCH.extractor = EXTRACTOR
    print(" WMA 预热    : 已切到云端主题提取（Agent %s 个，会话 %s，探测 %.0f ms）" %
          (r.get("agents_total"), "复用" if reuse else "新建",
           (time.perf_counter() - t0) * 1000))


async def main():
    bootstrap()
    # 预热真实 MCP：initialize 握手约 1.1s，放在启动期做掉，
    # 否则第一轮话轮的 1.2s 检索预算会被握手吃掉，直接零命中。
    for n in SOURCE_CLIENTS:
        try:
            t0 = time.perf_counter()
            tools = await asyncio.wait_for(n["client"].list_tools(), 12)
            print(" MCP 预热    : %-12s %s 握手 %.0f ms，%d 个工具" %
                  (n["name"], n["client"].info().get("server") or "已连接",
                   (time.perf_counter() - t0) * 1000, len(tools or [])))
        except Exception as e:
            print(" [自检错误] MCP 预热失败（%s）：%s: %s" %
                  (n["name"], type(e).__name__, str(e)[:110]))
    s = CFG["server"]
    srv = await asyncio.start_server(handle_conn, s["host"], s["port"])
    sh = ASR_CHECK["shape"] if ASR_CHECK else {}
    print("=" * 76)
    print(" 切身产品经理 · 实时音频链路 (v1.2 · 豆包接入形态矩阵)")
    print(" 地址        : http://%s:%d" % (s["host"], s["port"]))
    print(" ASR         : %s | 模型 %s·%s版 | %s | 语种 %s | 热词 %d" %
          (CFG["asr"].get("provider"), sh.get("version", "?"), sh.get("billing", "?"),
           sh.get("resource_id", "?"), CFG["asr"].get("default_lang"),
           len(CFG["asr"].get("hotwords", []))))
    print(" endpoint    : stream  = %s" % sh.get("endpoint_stream", ""))
    print("               segment = %s" % sh.get("endpoint_segment", ""))
    print(" StreamMode  : %s（0=不发送；1/2 属 RTC ASRConfig，非原生 sauc 字段）" %
          sh.get("stream_mode", 0))
    print(" 控制台项目  : %s（模型开通项目须与 API Key 同项目）" %
          (sh.get("project") or "<未填>"))
    print(" WMA         : %s%s | MCP %s/%s | %s" %
          (extractor_info(EXTRACTOR).get("provider"),
           "（auto：下面自检后决定用云端还是本地）"
           if CFG["wma"].get("provider") == "auto" else "",
           CFG["mcp"].get("transport"),
           CFG["mcp"].get("adapter", "none"),
           ("文档 %d 篇 / %d 块" % (len(LOCAL.store.docs), len(LOCAL.store.chunks))
            if CFG["mcp"].get("adapter") != "perf"
            else CFG["mcp"].get("http", {}).get("url", ""))))
    for e in (ASR_CHECK["errors"] if ASR_CHECK else []):
        print(" [自检错误] " + e)
    for w in (ASR_CHECK["warnings"] if ASR_CHECK else []):
        print(" [自检提示] " + w)
    print(" 纯标准库    : 零第三方依赖；无密钥也能跑通全链路演示")
    print("=" * 76)
    # 放在 banner 之后：连不上 WMA 时那几行诊断是给人看的下一步，不该插在抬头中间
    await _warm_wma()
    async with srv:
        await srv.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n已停止")
