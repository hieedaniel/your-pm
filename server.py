"""切身产品经理 · 实时音频链路后台（Python 3.13 纯标准库 / 零第三方依赖）

启动：
    cd qieshen-live && python server.py
    浏览器打开 http://127.0.0.1:8780

端点：
    GET  /                 前端单页（手机 UA 自动切 /m，?desk=1 强制桌面版）
    GET  /m                手机精简版（只显示主题 + 提示卡 + 服务状态）
    GET  /audio_worklet.js 浏览器重采样处理器
    GET  /sample.wav       内置示例音频（无麦克风一键验证音频上行）
    WS   /audio            音频上行（binary 200ms PCM + text 控制帧）
    GET  /events           SSE 事件流（字幕 / 主题 / 提示卡 / audio.stats / 状态）
    GET  /api/state        当前配置与统计
    GET  /api/selfcheck    全链路自检（主题表 / 规则词典 / 例句 / 检索源）
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
import base64
import binascii
import json
import os
import re
import secrets
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
from wma_agent import (build_extractor, extractor_info, probe_wma,
                       load_taxonomy)
from orchestrator import Orchestrator
import kb_admin as KB

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

# 知识库可写目录：内置 docs-kaiyi/ 只读，管理页上传的文档落在这里。
# 容器部署时必须挂出来（否则重建容器会丢），见 docker-compose.server.yml。
KB_DIR = os.environ.get("QS_KB_DIR") or os.path.join(ROOT, "data", "kb")
DATA_DIR = os.path.dirname(KB_DIR)


def _resolve_admin_token():
    """管理口令三级来源：环境变量 > config.admin.token > data/.admin_token（首次自动生成）。

    自动生成是为了「不给口令也能跑」，但绝不静默放行：生成一次就落盘，
    重启不变，启动日志里会明确打出来。公网部署建议显式设 QS_ADMIN_TOKEN。
    """
    t = (os.environ.get("QS_ADMIN_TOKEN")
         or (CFG.get("admin") or {}).get("token") or "").strip()
    if t:
        return t, "环境变量/配置"
    path = os.path.join(DATA_DIR, ".admin_token")
    try:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                old = f.read().strip()
            if old:
                return old, "data/.admin_token"
        t = secrets.token_urlsafe(18)
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(t)
        os.chmod(path, 0o600)
        return t, "首次自动生成"
    except OSError:
        # 只读文件系统：退化成内存口令，本次进程内有效
        return secrets.token_urlsafe(18), "内存（无写权限，重启会变）"


ADMIN_TOKEN, ADMIN_TOKEN_SRC = _resolve_admin_token()

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
    """按「管理页保存的源 > config.mcp.sources > 旧版单源写法」组装数据源。"""
    global SOURCE_CLIENTS
    SOURCE_CLIENTS = []
    raw = KB.load_sources(KB_DIR) or mcfg.get("sources") or []
    if not raw:
        # transport=local 且没有任何外部源：不建连接。否则下面的兼容分支会拿
        # mcp.http.url 当「默认源」，本地知识库还没接上就被一条过期地址顶掉。
        if (mcfg.get("transport") or "local") == "local":
            return []
        # 兼容旧配置：mcp.transport / mcp.http / mcp.adapter
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
    # 本地库（内置 19 篇 + 上传文档）永远是基线源，放在最后一个兜底。
    # 以前只要配了一个 MCP 源，SEARCH 就整个换成那条远程连接，本地资料直接
    # 不参与检索——远程源不通、或工具名不是 docs.search 时，现场看到的就是
    # 「一配 MCP 就什么卡都没有」。
    local_client = (MCP if (mcfg.get("transport") or "local") == "local"
                    else McpClient({"transport": "local"}, LOCAL))
    if local_client is not None:
        entries.append({"name": "本地知识库", "proxy": local_client,
                        "adapter": "local", "weight": 1.0, "prefix": "",
                        "timeout_ms": mcfg.get("timeout_ms", 6000), "enabled": True})
    return entries


def rebuild_search():
    """（重新）装配检索入口。管理页改了 MCP 源后调它即可热生效，不用重启容器。"""
    global SEARCH
    mcfg = CFG["mcp"]
    entries = build_sources(mcfg, VOCAB)
    if len(entries) > 1:
        SEARCH = MultiSourceSearch(entries)
    elif entries:
        SEARCH = entries[0]["proxy"]
    elif mcfg.get("adapter") == "perf":
        SEARCH = PerfSearchProxy(MCP, mcfg.get("perf"), VOCAB)
    else:
        SEARCH = MCP
    if ORCH:
        ORCH.mcp = SEARCH
    return entries


def active_taxonomy_path() -> str:
    """管理页重建过就用生成的那份，否则用仓库里那份。"""
    gen = KB.taxonomy_path(KB_DIR)
    if os.path.isfile(gen):
        return gen
    p = CFG["wma"].get("taxonomy_file") or os.path.join("prompts", "taxonomy.json")
    return p if os.path.isabs(p) else os.path.join(ROOT, p)


def _apply_taxonomy(path: str) -> dict:
    """把主题表灌进 VOCAB（本地规则的词典来源）。

    只动 VOCAB，不重建 EXTRACTOR：调用方自己决定要不要换提取器，
    否则每次改表都会把云端会话重建一遍。

    生成的表若坏了（空文件 / JSON 截断），不能就这么算了：调用方以为「已经
    灌进去了」，规则词典实际只剩几条别名，主题从此全空。这里退回到仓库里
    那份手写表——有表总比没表好，坏表的存在感应该只出现在日志里。
    """
    tax = load_taxonomy(path)
    if not tax.get("topics"):
        repo = os.path.join(ROOT, "prompts", "taxonomy.json")
        if os.path.abspath(path) != os.path.abspath(repo):
            print(" [警告] 主题表 %s 没有可用主题，回退到仓库自带表" % path)
            tax = load_taxonomy(repo)
    if not tax.get("topics"):
        print(" [警告] 主题表为空：本地规则将无法识别主题（卡片仍按原话检索）")
        return tax
    VOCAB["topics"] = tax["topics"]
    bucket = VOCAB.setdefault("aliases", {})
    for label, says in (tax.get("aliases") or {}).items():
        cur = bucket.setdefault(label, [])
        for s in says:
            if s not in cur:
                cur.append(s)
    return tax


async def rebuild_taxonomy():
    """按当前知识库重建主题表并热生效（只影响本地规则，云端提示词另行部署）。"""
    global EXTRACTOR
    # 基线用仓库里那份手写表：人工整理的客户说法优先保留，
    # 自动生成只负责补「新导入的文档」这些手写表覆盖不到的部分。
    base = load_taxonomy(os.path.join(ROOT, "prompts", "taxonomy.json"))
    taxo = KB.taxonomy_from_docs(LOCAL.store, baseline=base)
    if not taxo.get("topics"):
        return {"ok": False, "error": "知识库里没有可用文档，先导入或恢复内置资料"}
    path = KB.save_taxonomy(KB_DIR, taxo)
    _apply_taxonomy(path)
    applied = False
    if getattr(EXTRACTOR, "provider", "") == "rule":
        EXTRACTOR = build_extractor(dict(CFG["wma"], provider="rule"), VOCAB)
        if ORCH:
            ORCH.extractor = EXTRACTOR
        applied = True
    await emit("status", {"level": "info",
                          "message": "主题表已按知识库重建 · %d 个主题%s" %
                                     (len(taxo["topics"]),
                                      "（本地规则已生效）" if applied
                                      else "（云端主题需同步部署提示词）")})
    return {"ok": True, "count": len(taxo["topics"]), "path": path,
            "applied": applied,
            "labels": [t["label"] for t in taxo["topics"][:12]]}


def bootstrap():
    global LOCAL, MCP, EXTRACTOR, ORCH, ASR_CHECK, SEARCH, DEMO_TURNS
    # 接入形态自检：Resource ID / 版本 / 计费 / 鉴权 / endpoint / StreamMode / 项目
    ASR_CHECK = validate_asr_config(
        CFG["asr"], strict=(CFG["asr"].get("provider") == "doubao"))
    mcfg = CFG["mcp"]
    docs_dir = mcfg["docs_dir"]
    if not os.path.isabs(docs_dir):
        docs_dir = os.path.join(ROOT, docs_dir)
    # 两个目录一套索引：内置只读文档 + 管理页上传的可写文档
    os.makedirs(KB_DIR, exist_ok=True)
    LOCAL = LocalMcpServer([docs_dir, KB_DIR])
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
    # 主题表同时是本地规则的词典：label / say / tags 三条都在里面。
    # 不并进来时规则只剩上面那份手写别名，工程类问题几乎命中不了，
    # 结果就是「主题空 → 不推卡」——线上看起来像整个服务停了。
    _apply_taxonomy(active_taxonomy_path())
    EXTRACTOR = build_extractor(CFG["wma"], VOCAB)
    # 启动就把「主题表几个主题 / 规则几个词」打进日志：出问题第一眼看 docker logs
    _ri = _rule_info(EXTRACTOR)
    print(" 主题/规则   : 主题表 %d 个 · 规则词典 %d 条（label %d / say %d / tags %d）" %
          (len(VOCAB.get("topics") or []), _ri.get("terms", 0), _ri.get("from_label", 0),
           _ri.get("from_say", 0), _ri.get("from_tags", 0)))
    print(" 主题来源    : %s" % active_taxonomy_path())
    print(" 镜像版本    : %s" % (os.environ.get("QS_BUILD_SHA", "")[:12] or "未知（旧镜像）"))

    orch_cfg = dict(mcfg)
    orch_cfg["policy"] = CFG["policy"]
    # 适配层：真实 MCP 的工具是业务 CRUD，编排器要的是 docs.search 语义。
    # 换回本地文档只改 config.mcp.adapter = "none"。
    entries = rebuild_search()
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
    reason = {200: "OK", 201: "Created", 400: "Bad Request", 401: "Unauthorized",
              403: "Forbidden", 404: "Not Found", 413: "Payload Too Large",
              422: "Unprocessable Entity", 500: "Internal Server Error",
              503: "Service Unavailable"}.get(status, "OK")
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


def search_active() -> list:
    """当前真正参与检索的源。诊断「配了 MCP 却没结果」时先看这里。"""
    srcs = getattr(SEARCH, "sources", None)
    if isinstance(srcs, list) and srcs and isinstance(srcs[0], dict):
        return [{"name": x.get("name"), "adapter": x.get("adapter"),
                 "weight": x.get("weight")} for x in srcs]
    if SEARCH is None:
        return []
    return [{"name": "本地知识库", "adapter": "local", "weight": 1.0}]


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
                "active": search_active(),
                "errors": list(getattr(SEARCH, "last_errors", []) or [])[-3:],
                "search": SEARCH.stats() if hasattr(SEARCH, "stats") else {}},
        "docs": (LOCAL.store.listing() if (LOCAL and CFG["mcp"].get("adapter") != "perf"
                                          and len(SOURCE_CLIENTS) <= 1) else []),
        "chunks": len(LOCAL.store.chunks) if (LOCAL and CFG["mcp"].get("adapter") != "perf") else 0,
        "sessions": len(SESSIONS),
        "stats": ORCH.stats() if ORCH else {},
        # 主题表在页面上直接可见：主题全空时第一眼看的就是这个数字
        "taxonomy": {"topics": len(VOCAB.get("topics") or []),
                     "terms": len(getattr(EXTRACTOR, "_terms", []) or []),
                     "path": active_taxonomy_path()},
        "build": {"sha": os.environ.get("QS_BUILD_SHA", "")[:12]},
        "policy": CFG["policy"],
    }


# --------------------------------------------------------------------------
# 自检：一条请求回答「主题为什么空 / 卡片为什么不出」
# --------------------------------------------------------------------------
SELFCHECK_SAMPLES = [
    "我们园区现在访客登记还是前台打电话，没人接就很麻烦",
    "有没有智慧宿舍的解决方案？",
    "摄像头夜里看不清，有人翻墙也发现不了",
]


def _rule_info(ex) -> dict:
    """把规则词典的规模挖出来（可能包在 Resilient 里）。"""
    for cand in (ex, getattr(ex, "fallback", None)):
        if cand is not None and hasattr(cand, "info"):
            try:
                return cand.info()
            except Exception:
                pass
    return {}


def _read_taxonomy_file(path: str) -> dict:
    """只读文件、不碰 wma_agent 的全局表——自检不该有副作用。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:80])}
    rows = [t for t in (data.get("topics") or []) if str(t.get("label") or "").strip()]
    return {"ok": True, "topics": len(rows), "source": data.get("source", ""),
            "generated": data.get("generated", 0),
            "labels": [t["label"] for t in rows[:8]],
            "sample_says": [s for t in rows for s in (t.get("say") or [])][:8]}


async def selfcheck():
    """按「配置 → 词典 → 主题 → 检索」四层依次体检，每层都给出可执行的下一步。"""
    verdict, actions = [], []
    gen_path = KB.taxonomy_path(KB_DIR)
    tax_path = active_taxonomy_path()
    tax = _read_taxonomy_file(tax_path)
    rule = _rule_info(EXTRACTOR)
    ex_info = extractor_info(EXTRACTOR)

    build_sha = os.environ.get("QS_BUILD_SHA", "")[:12]
    build_at = os.environ.get("QS_BUILD_AT", "")
    if not build_sha:
        verdict.append({"level": "warn", "text": "镜像是旧版（没有构建版本号）",
                        "detail": "带版本号的镜像会显示 40 位 commit 前 12 位；"
                                  "为空说明这台服务器跑的还不是最新构建。"})
        actions.append("docker compose pull && docker compose up -d（先拉到新镜像再看自检）")

    # ① 主题模型
    if ex_info.get("provider") == "rule":
        verdict.append({"level": "warn", "text": "主题走本地规则（WMA 主题模型未接入）",
                        "detail": "wma.provider 为 rule/auto 且云端不可用时会落到这里；"
                                  "提示词侧的主题不会出现，但本地规则照样能识别。"})
    else:
        verdict.append({"level": "ok", "text": "主题模型：%s" % ex_info.get("provider"),
                        "detail": "降级次数 %s · 最近错误 %s" %
                                  (ex_info.get("degraded_count"), ex_info.get("last_error") or "无")})

    # ② 主题表 + 规则词典：这两层任一为空，主题必然全空
    if not tax.get("ok"):
        verdict.append({"level": "error", "text": "主题表读不出来",
                        "detail": "%s（%s）" % (tax.get("error"), tax_path)})
        actions.append("去 /kb 点「按知识库重建主题表」，或恢复仓库里的 prompts/taxonomy.json")
    elif not tax.get("topics"):
        verdict.append({"level": "error", "text": "主题表里 0 个主题",
                        "detail": "文件在（%s）但内容为空" % tax_path})
        actions.append("去 /kb 点「按知识库重建主题表」")
    else:
        verdict.append({"level": "ok", "text": "主题表 %d 个主题" % tax["topics"],
                        "detail": "%s · %s" % (tax_path,
                                               "按知识库生成" if gen_path == tax_path else "仓库内置")})
    if rule.get("terms"):
        verdict.append({"level": "ok", "text": "规则词典 %d 条（label %d · say %d · tags %d）" %
                        (rule["terms"], rule.get("from_label", 0),
                         rule.get("from_say", 0), rule.get("from_tags", 0)), "detail": ""})
    else:
        verdict.append({"level": "error", "text": "规则词典 0 条：本地规则不可能识别出任何主题",
                        "detail": "主题表没被灌进 VOCAB（旧镜像或加载失败）"})
        actions.append("确认镜像版本：自检里 build.sha 为空 = 旧镜像，先拉新镜像")

    # ③ 拿真实例句跑一遍：数字之外还要看到「这句话到底出什么主题」
    samples = []
    for s in SELFCHECK_SAMPLES:
        t0 = time.perf_counter()
        try:
            r = await asyncio.wait_for(EXTRACTOR.extract(s, {"lang": "zh-CN"}), 6.0)
        except Exception as e:
            r = {"topics": [], "should_push": False,
                 "push_reason": "%s: %s" % (type(e).__name__, str(e)[:60])}
        samples.append({"text": s, "topics": [x.get("label") for x in (r.get("topics") or [])],
                        "should_push": bool(r.get("should_push")),
                        "provider": r.get("provider", ""), "degraded": bool(r.get("degraded")),
                        "degrade_reason": r.get("degrade_reason", ""),
                        "query": (r.get("query") or "")[:60],
                        "ms": round((time.perf_counter() - t0) * 1000, 1)})
    hit = [x for x in samples if x["topics"]]
    if not hit:
        verdict.append({"level": "error", "text": "三句典型客户话，一个主题都没识别出来",
                        "detail": "词典为空、或主题表里没有这些说法（看 samples 详情）"})
    elif len(hit) < len(samples):
        verdict.append({"level": "warn", "text": "%d/%d 句识别出主题" %
                        (len(hit), len(samples)), "detail": "未命中的说法可补进主题表 say 字段"})
    else:
        verdict.append({"level": "ok", "text": "%d/%d 句识别出主题" %
                        (len(hit), len(samples)), "detail": ""})

    # ④ 检索链路：有主题但检索不到，跟主题空是两种病
    probe = {"ok": False, "hits": 0, "error": ""}
    try:
        res = await asyncio.wait_for(
            SEARCH.call(ORCH.tool_search if ORCH else "docs.search",
                        {"query": SELFCHECK_SAMPLES[0], "top_k": 3, "lang": "zh-CN"}), 8.0)
        hits = (res or {}).get("hits") or []
        probe = {"ok": bool(hits), "hits": len(hits),
                 "titles": [h.get("title") for h in hits][:3],
                 "top_score": hits[0].get("score") if hits else None,
                 "error": ""}
    except Exception as e:
        probe["error"] = "%s: %s" % (type(e).__name__, str(e)[:100])
    if probe["ok"]:
        verdict.append({"level": "ok", "text": "检索链路通：样例问题命中 %d 条" % probe["hits"],
                        "detail": " · ".join(str(x) for x in probe.get("titles") or [])})
    else:
        verdict.append({"level": "error", "text": "检索链路无命中",
                        "detail": probe.get("error") or "查询正常但结果为空：知识库里没有相关内容"})
        actions.append("去 /kb 看「当前生效源」，远程源报错就临时停用，只留本地知识库")

    active = search_active()
    errs = list(getattr(SEARCH, "last_errors", []) or [])[-3:]
    if errs:
        verdict.append({"level": "warn", "text": "检索源有报错", "detail": " / ".join(errs)})
        # 有本地基线在，远程源挂掉不影响出卡；但要说清楚这是「降级可用」而非正常
        if len(active) > 1:
            actions.append("远程源报错已自动回退本地知识库（不影响出卡）；"
                           "要去掉这条警告，去 /kb 停用出错的源或查它的 URL/Token")

    return {
        "ok": not any(v["level"] == "error" for v in verdict),
        "build": {"sha": build_sha, "built_at": build_at},
        "taxonomy": dict(tax, path=tax_path, generated_path=gen_path),
        "rule": rule,
        "extractor": ex_info,
        "sources": active,
        "source_detail": [{"name": n["name"], "info": n["client"].info()}
                          for n in SOURCE_CLIENTS],
        "search_stats": SEARCH.stats() if hasattr(SEARCH, "stats") else {},
        "probe": probe,
        "samples": samples,
        "verdict": verdict,
        "actions": actions,
        "docs": len(LOCAL.store.docs) if LOCAL else 0,
        "chunks": len(LOCAL.store.chunks) if LOCAL else 0,
        "now": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


# --------------------------------------------------------------------------
# 知识库管理接口（全部要求 ADMIN_TOKEN）
# --------------------------------------------------------------------------
def _admin_ok(headers, u) -> bool:
    got = (headers.get("x-admin-token") or "").strip()
    if not got:
        got = (parse_qs(u.query).get("token") or [""])[0].strip()
    return bool(ADMIN_TOKEN) and secrets.compare_digest(got, ADMIN_TOKEN)


def _kb_docs():
    """内置 + 上传合并成一份清单，前端据此区分能不能删。"""
    uploaded = {f["doc_id"]: f for f in KB.kb_files(KB_DIR)}
    out = []
    for d in (LOCAL.store.listing() if LOCAL else []):
        f = uploaded.get(d.get("doc_id")) or {}
        out.append(dict(d, uploaded=bool(f), bytes=f.get("bytes", 0),
                        mtime=f.get("mtime", "")))
    return out


async def _after_import(title: str, kind: str, fid: str, warn: str = ""):
    LOCAL.store.load()                 # 重建切块与 DF；检索对象持有同一个 store，无需重建
    msg = "知识库已导入：%s（%s）· 当前 %d 篇 / %d 块" % (
        title, kind, len(LOCAL.store.docs), len(LOCAL.store.chunks))
    await emit("status", {"level": "warn" if warn else "info",
                          "message": msg + ("；" + warn if warn else "")})


async def kb_api(p: str, method: str, u, headers, body: bytes):
    """返回 (status, obj)。所有分支都要求口令，避免公网被人上传垃圾或改 MCP 地址。"""
    if not _admin_ok(headers, u):
        return 401, {"ok": False,
                     "error": "未授权：请求头 X-Admin-Token 缺失或不匹配"}

    q = parse_qs(u.query)

    def payload():
        try:
            return json.loads(body.decode("utf-8", "replace") or "{}")
        except Exception:
            return {}

    if p == "/api/kb/state":
        files = KB.kb_files(KB_DIR)
        return 200, {
            "ok": True, "kb_dir": KB_DIR, "admin_token_src": ADMIN_TOKEN_SRC,
            "docs": len(LOCAL.store.docs), "chunks": len(LOCAL.store.chunks),
            "uploaded": len(files), "files": files,
            "sources": KB.load_sources(KB_DIR),
            "active_sources": [n["name"] for n in SOURCE_CLIENTS],
            "search": SEARCH.stats() if hasattr(SEARCH, "stats") else {},
            "pdf_ready": _pdf_ready(),
        }

    if p == "/api/kb/docs":
        return 200, {"ok": True, "docs": _kb_docs()}

    if p == "/api/kb/reindex":
        LOCAL.store.load()
        await emit("status", {"level": "info",
                              "message": "知识库已重建索引 · %d 篇 / %d 块" %
                                         (len(LOCAL.store.docs), len(LOCAL.store.chunks))})
        return 200, {"ok": True, "docs": len(LOCAL.store.docs),
                     "chunks": len(LOCAL.store.chunks)}

    if p == "/api/kb/upload" and method == "POST":
        d = payload()
        name = d.get("filename") or ""
        b64 = d.get("data") or ""
        if not b64:
            return 400, {"ok": False, "error": "data 为空（应为文件内容的 base64）"}
        if len(b64) > 36 * 1024 * 1024:
            return 413, {"ok": False, "error": "文件过大（上限 25MB）"}
        try:
            raw = base64.b64decode(b64, validate=False)
        except (binascii.Error, ValueError):
            return 400, {"ok": False, "error": "base64 解不开"}
        tags = [t.strip() for t in (d.get("tags") or "").split(",") if t.strip()]
        try:
            title, md, kind = KB.to_markdown(name, raw)
        except (ValueError, RuntimeError) as e:
            return 422, {"ok": False, "error": str(e)}
        fid = KB.write_doc(KB_DIR, title, md, "上传：" + (name or title),
                           tags=tags, name_hint=name or title)
        await _after_import(title, kind, fid)
        return 200, {"ok": True, "doc_id": fid, "title": title, "kind": kind,
                     "chars": len(md)}

    if p == "/api/kb/text" and method == "POST":
        d = payload()
        text = (d.get("text") or "").strip()
        title = (d.get("title") or "").strip() or "手工录入"
        if len(text) < 10:
            return 400, {"ok": False, "error": "正文太短（至少 10 字）"}
        tags = [t.strip() for t in (d.get("tags") or "").split(",") if t.strip()]
        fid = KB.write_doc(KB_DIR, title, text, "手工录入", tags=tags,
                           name_hint=title)
        await _after_import(title, "text", fid)
        return 200, {"ok": True, "doc_id": fid, "title": title, "chars": len(text)}

    if p == "/api/kb/url" and method == "POST":
        d = payload()
        url = (d.get("url") or "").strip()
        tags = [t.strip() for t in (d.get("tags") or "").split(",") if t.strip()]
        try:
            title, md, final = KB.fetch_url(url)
        except (ValueError, RuntimeError) as e:
            return 422, {"ok": False, "error": str(e)}
        fid = KB.write_doc(KB_DIR, title, md, "网页：" + final, tags=tags,
                           name_hint=title)
        await _after_import(title, "url", fid)
        return 200, {"ok": True, "doc_id": fid, "title": title,
                     "chars": len(md), "url": final}

    if p == "/api/kb/delete" and method == "POST":
        d = payload()
        doc_id = (d.get("doc_id") or "").strip()
        if not KB.delete_doc(KB_DIR, doc_id):
            return 400, {"ok": False,
                         "error": "删不掉：只有上传的文档可以删，内置知识库是只读的"}
        LOCAL.store.load()
        await emit("status", {"level": "info", "message": "已删除文档 %s" % doc_id})
        return 200, {"ok": True, "docs": len(LOCAL.store.docs),
                     "chunks": len(LOCAL.store.chunks)}

    if p == "/api/kb/sources" and method == "POST":
        d = payload()
        clean = KB.save_sources(KB_DIR, d.get("sources") or [])
        entries = rebuild_search()
        active = [e["name"] for e in entries] or ["本地知识库"]
        await emit("status", {"level": "info",
                              "message": "MCP 知识源已更新 · 生效 %d 个：%s" %
                                         (len(entries), "、".join(active))})
        return 200, {"ok": True, "sources": clean, "active": active}

    if p == "/api/kb/taxonomy" and method == "GET":
        path = active_taxonomy_path()
        tax = load_taxonomy(path)
        rows = [{"id": t.get("id", ""), "label": t["label"],
                 "doc_id": t.get("doc_id", ""),
                 "say": len(t.get("say") or []), "tags": len(t.get("tags") or [])}
                for t in tax.get("topics") or []]
        return 200, {"ok": True, "path": path, "count": len(rows), "topics": rows}

    if p == "/api/kb/taxonomy/rebuild" and method == "POST":
        r = await rebuild_taxonomy()
        return (200 if r.get("ok") else 400), r

    if p == "/api/kb/sources/test" and method == "POST":
        d = payload()
        url = (d.get("url") or "").strip()
        tok = (d.get("token") or "").strip()
        if not url:
            return 400, {"ok": False, "error": "url 为空"}
        r = await probe_mcp(url, tok, 8.0)
        return 200, {"ok": bool(r.get("ok")), "result": r}

    return 404, {"ok": False, "error": "未知接口 %s" % p}


def _pdf_ready() -> bool:
    try:
        import pypdf            # noqa: F401,PLC0415
        return True
    except ImportError:
        return False


MOBILE_UA = re.compile(r"(?i)(android|iphone|ipod|ipad|harmonyos|mobile|okhttp|wv\))").search


def _serve_html(writer, name: str):
    with open(os.path.join(ROOT, name), "rb") as f:
        data = f.read()
    writer.write(_resp(200, data, "text/html; charset=utf-8"))


async def handle_http(writer, method, path, headers, body: bytes):
    u = urlsplit(path)
    p = u.path
    if p in ("/", "/index.html"):
        # 手机（含 APK 的 WebView）直接给精简版；桌面版永远留在 /?desk=1，
        # 这样 APK 里的旧地址不用改，装上就看到手机界面。
        q = parse_qs(u.query)
        want_desk = (q.get("desk") or [""])[0] in ("1", "true")
        if not want_desk and MOBILE_UA(headers.get("user-agent") or ""):
            _serve_html(writer, "m.html")
        else:
            _serve_html(writer, "index.html")
    elif p in ("/m", "/m/", "/m.html"):
        _serve_html(writer, "m.html")
    elif p == "/manifest.webmanifest":
        with open(os.path.join(ROOT, "manifest.webmanifest"), "rb") as f:
            data = f.read()
        writer.write(_resp(200, data, "application/manifest+json; charset=utf-8"))
    elif p == "/audio_worklet.js":
        with open(os.path.join(ROOT, "audio_worklet.js"), "rb") as f:
            data = f.read()
        writer.write(_resp(200, data, "application/javascript; charset=utf-8"))
    elif p == "/sample.wav":
        with open(os.path.join(ROOT, "sample.wav"), "rb") as f:
            data = f.read()
        writer.write(_resp(200, data, "audio/wav"))
    elif p in ("/kb", "/kb/"):
        with open(os.path.join(ROOT, "kb.html"), "rb") as f:
            data = f.read()
        writer.write(_resp(200, data, "text/html; charset=utf-8"))
    elif p.startswith("/api/kb/"):
        code, obj = await kb_api(p, method, u, headers, body)
        writer.write(_resp(code, json.dumps(obj, ensure_ascii=False).encode("utf-8")))
    elif p == "/api/state":
        writer.write(_json(state()))
    elif p == "/api/selfcheck":
        # 一条请求回答「主题为什么空 / 卡片为什么不出」：配置 → 词典 → 主题 → 检索
        writer.write(_json(await selfcheck()))
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
    print(" 知识库      : 内置+上传 %d 篇 / %d 块 · 可写目录 %s" %
          (len(LOCAL.store.docs), len(LOCAL.store.chunks), KB_DIR))
    print(" 管理页      : http://%s:%d/kb   口令 %s（%s）" %
          (s["host"], s["port"], ADMIN_TOKEN, ADMIN_TOKEN_SRC))
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
