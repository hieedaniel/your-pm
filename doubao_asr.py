"""豆包 / 火山引擎大模型流式 ASR 适配器（含 mock 兜底）。

协议事实（见架构设计 01 章）：
  * 鉴权头只能由后台注入（浏览器 WebSocket 不支持自定义 Header）。
  * audio.language 与 enable_auto_lang 仅 bigmodel_nostream 支持；
    enable_nonstream / definite 仅 bigmodel_async 支持 → 因此做双轨。
  * WebSocket payload 是自定义二进制包：4B header + 4B payload size(大端) + payload。
  * 定稿标志是 result.utterances[].definite=true，不是 sentence_end。

对外只暴露三个异步回调：on_partial / on_final / on_status。
无论 mock 还是真实豆包，下游拿到的是完全同构的事件，业务链路零改动。
"""
from __future__ import annotations

import asyncio
import gzip
import inspect
import json
import math
import re
import struct
import time
import uuid
from collections import deque

from wsproto import ws_client_connect, send_frame, read_frame, WsHandshakeError

# --------------------------------------------------------------------------
# 接入形态矩阵（官方事实，见架构设计 01 章）
#   * X-Api-Resource-Id 决定【模型版本 + 计费方式】：seedasr=2.0 / bigasr=1.0
#   * WebSocket endpoint 决定【交互模式】，与版本正交，二者不可按 1.0/2.0 二选一
#       bigmodel_async    = 双向流式优化版：enable_nonstream 二遍识别 / definite
#       bigmodel_nostream = 流式输入（一句话）：language 指定 25 语种 / enable_auto_lang
#       bigmodel          = 旧双向流式：每包返回，不支持二遍识别
#   * 新版控制台只发 X-Api-Key；旧版的 X-Api-App-Key / X-Api-Access-Key 不要混用
#   * 控制台「模型开通项目」必须与「API Key 所属项目」一致，否则 403
# --------------------------------------------------------------------------
RESOURCE_IDS = {
    "2.0.duration":      "volc.seedasr.sauc.duration",
    "2.0.concurrent":    "volc.seedasr.sauc.concurrent",
    "1.0.duration":      "volc.bigasr.sauc.duration",
    "1.0.concurrent":    "volc.bigasr.sauc.concurrent",
    "file.2.0.async":    "volc.seedasr.auc",   # 录音文件识别，非实时 WebSocket 链路
}
_RID_TO_SHAPE = {v: k for k, v in RESOURCE_IDS.items()}
ENDPOINTS = {
    "stream":  "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async",
    "segment": "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_nostream",
    "legacy":  "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel",
}


def validate_asr_config(cfg: dict, strict: bool | None = None) -> dict:
    """纯配置自检，不发网络请求。返回 {ok, errors, warnings, shape}。

    strict=False（mock 模式）时，缺凭据只算 warning，避免每次启动刷错误。
    """
    if strict is None:
        strict = cfg.get("provider") == "doubao"
    errors, warnings = [], []
    rid = str(cfg.get("resource_id") or "").strip()
    shape_key = _RID_TO_SHAPE.get(rid)
    version = billing = "?"
    if not shape_key:
        errors.append("未知 resource_id=%r，合法值见 config.json 的 resource_ids" % rid)
    else:
        # 键形如 "2.0.duration"；版本号本身含点，必须从右侧切，不能 split(".")[0]
        if shape_key.startswith("file."):
            version, billing = "2.0", "duration"   # 文件识别无 duration/concurrent 之分
            errors.append("resource_id=%s 属于录音文件识别（异步），不能用于实时 WebSocket 链路" % rid)
        else:
            version, billing = shape_key.rsplit(".", 1)
        dv, db = str(cfg.get("api_version", version)), str(cfg.get("billing", billing))
        if dv != version:
            errors.append("api_version=%s 与 resource_id 推断的 %s 不一致" % (dv, version))
        if db != billing:
            errors.append("billing=%s 与 resource_id 推断的 %s 不一致" % (db, billing))

    auth = str(cfg.get("auth_mode", "new"))
    cred_issues = []
    if auth not in ("new", "legacy"):
        cred_issues.append("auth_mode=%r 非法，只能是 new 或 legacy" % auth)
    elif auth == "new":
        if not cfg.get("api_key"):
            cred_issues.append("auth_mode=new 但 api_key 为空（新版控制台 → API Key 管理）")
        if cfg.get("app_key") or cfg.get("access_key"):
            warnings.append("检测到旧版 app_key/access_key；新版只用 X-Api-Key，两套 Header 不要混用")
    else:
        if not cfg.get("app_key") or not cfg.get("access_key"):
            cred_issues.append("auth_mode=legacy 需要 app_key + access_key 两个值")
        warnings.append("当前用旧版控制台鉴权，官方建议改用新版 X-Api-Key")
    (errors if strict else warnings).extend(cred_issues)

    for key, want in (("endpoint_stream", "bigmodel_async"),
                      ("endpoint_segment", "bigmodel_nostream")):
        ep = str(cfg.get(key) or "")
        if want not in ep:
            warnings.append("%s 未使用 %s，当前=%s（模式与能力会改变）" % (key, want, ep or "<空>"))

    try:
        sm = int(cfg.get("stream_mode", 0) or 0)
    except (TypeError, ValueError):
        sm = -1
    if sm not in (0, 1, 2):
        errors.append("stream_mode 只能是 0（不发送）/ 1 / 2，当前=%r" % cfg.get("stream_mode"))
    elif sm:
        warnings.append(
            "StreamMode=%d 属于火山 RTC/对话式 AI 的 ASRConfig.ProviderParams，"
            "不是原生 sauc WebSocket full client request 字段；将按 request.StreamMode 透传" % sm)

    if not cfg.get("project"):
        warnings.append("未填 project：请确认【模型开通项目】与【API Key 所属项目】为同一个，否则 403")
    if len(cfg.get("hotwords", [])) > 20:
        warnings.append("热词 %d 个，超过双向流式 corpus.context 的 100 tokens 建议上限" %
                        len(cfg.get("hotwords", [])))

    return {
        "ok": not errors, "errors": errors, "warnings": warnings,
        "shape": {
            "version": version, "billing": billing, "resource_id": rid,
            "auth_mode": auth, "stream_mode": sm,
            "project": cfg.get("project", ""),
            "endpoint_stream": cfg.get("endpoint_stream", ""),
            "endpoint_segment": cfg.get("endpoint_segment", ""),
        },
    }


def diagnose_handshake(err, cfg: dict) -> str:
    """把 401/403 翻译成可执行的排查动作。

    关键：只看状态码时「密钥有效但资源未开通」与「密钥本身无效」长得一模一样，
    必须用响应体里的 error / code 分流（45000030 = requested resource not granted）。
    """
    status = getattr(err, "status", 0) or 0
    logid = getattr(err, "logid", "")
    code = getattr(err, "code", None)
    body = (getattr(err, "message", "") or "").strip()
    rid = str(cfg.get("resource_id") or "<空>")
    head = "豆包 WSS 握手失败 HTTP %s%s · resource_id=%s · auth=%s" % (
        status or "?", (" · X-Tt-Logid=%s" % logid) if logid else "",
        rid, cfg.get("auth_mode", "new"))
    if body:
        head += "\n      服务端返回：%s" % body[:240]

    low = body.lower()
    # 45000030 / not granted：密钥被识别，但项目下没有这个资源
    if "not granted" in low or str(code) == "45000030":
        ver = "2.0" if "seedasr" in rid else ("1.0" if "bigasr" in rid else "?")
        return head + (
            "\n      结论：API Key 有效，但【项目未开通该资源】——不是密钥写错，也不是代码问题。"
            "\n      开通路径：豆包语音控制台 → 左上角切到该 Key 所属项目 → 左侧「开通管理」"
            " → 找到「流式语音识别大模型 %s」→ 点「开通」→ 勾选协议确认。"
            "\n      两个必查点：① 开通的项目必须与 API Key 所在项目一致（不同项目资源隔离、不可共享）；"
            "② %s 需要按量或资源包有额度（*.duration 为小时版，*.concurrent 为并发版）。"
            "\n      开通后无需改代码，重新开始录音即可。" % (ver, rid))
    # app key not found：头没带上或字段名错
    if "app key not found" in low:
        return head + (
            "\n      结论：请求头里没有可识别的凭据。检查 X-Api-Key 是否为空、"
            "auth_mode 是否为 new（旧版用 X-Api-App-Key + X-Api-Access-Key）。")
    if status == 401 or "invalid" in low or "unauthorized" in low:
        return head + (
            "\n      结论：凭据无效或已失效。到控制台 API Key 管理确认该 Key 未禁用、未删除；"
            "若刚创建，等 1–2 分钟生效再试。")
    if status in (401, 403):
        return head + (
            " → 依次检查：① 模型开通项目 与 API Key 所属项目是否【同一个项目】；"
            "② Resource ID 拼写（2.0=volc.seedasr.sauc.*，1.0=volc.bigasr.sauc.*）；"
            "③ 该版本/计费形态是否已开通且额度充足；"
            "④ 是否把旧版 App-Key/Access-Key 与新版 X-Api-Key 混用；"
            "⑤ endpoint 是否指向 bigmodel_async / bigmodel_nostream。"
            + (" 保留 X-Tt-Logid 给火山工单。" if logid else ""))
    if status == 404:
        return head + " → endpoint 路径不存在，检查是否误写成 /bigmodel 之外的路径或混入了版本后缀"
    if not status:
        return "%s → %s（网络/TLS/DNS 层失败，先用 curl 或浏览器测连通性）" % (head, err)
    return "%s → %s" % (head, err)


async def probe_handshake(cfg: dict, timeout: float = 10.0) -> dict:
    """真实握手探测：不发音频、不建会话，只回答「这个 Key + 这个 Resource ID 能不能用」。

    返回结构化结果，供前端「检测密钥」按钮与运维脚本直接消费。
    """
    rid = str(cfg.get("resource_id") or "")
    url = cfg.get("endpoint_stream")
    h = {
        "X-Api-Resource-Id": rid,
        "X-Api-Request-Id": str(uuid.uuid4()),
        "X-Api-Connect-Id": str(uuid.uuid4()),
    }
    if cfg.get("auth_mode", "new") == "legacy":
        h["X-Api-App-Key"] = cfg.get("app_key", "")
        h["X-Api-Access-Key"] = cfg.get("access_key", "")
    else:
        h["X-Api-Key"] = cfg.get("api_key", "")
    out = {"ok": False, "status": 0, "code": None, "message": "", "logid": "",
           "resource_id": rid, "endpoint": url, "hint": ""}
    try:
        reader, writer, resp = await ws_client_connect(url, h, timeout=timeout)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        out.update({"ok": True, "status": 101,
                    "logid": (resp or {}).get("x-tt-logid", ""),
                    "message": "握手成功，鉴权与资源均可用"})
        return out
    except WsHandshakeError as e:
        out.update({"status": e.status, "code": e.code,
                    "message": e.message or e.status_line, "logid": e.logid})
        out["hint"] = diagnose_handshake(e, cfg)
        return out
    except Exception as e:
        out.update({"message": "%s: %s" % (type(e).__name__, str(e)[:160]),
                    "hint": "网络层失败：检查出口网络、DNS、TLS 与代理设置"})
        return out


# 豆包二进制协议常量
CLIENT_FULL_REQUEST = 0b0001
CLIENT_AUDIO_ONLY = 0b0010
SERVER_FULL_RESPONSE = 0b1001
SERVER_ACK = 0b1011
SERVER_ERROR = 0b1111
FLAG_NONE = 0b0000
FLAG_POS_SEQ = 0b0001
FLAG_LAST = 0b0010
FLAG_NEG_SEQ = 0b0011
SERIAL_NONE, SERIAL_JSON = 0b0000, 0b0001
COMPRESS_NONE, COMPRESS_GZIP = 0b0000, 0b0001


def _header(msg_type: int, flags: int, serial: int, compress: int) -> bytes:
    return bytes([0x11, (msg_type << 4) | flags, (serial << 4) | compress, 0x00])


def full_request_frame(config: dict) -> bytes:
    payload = gzip.compress(json.dumps(config, ensure_ascii=False).encode("utf-8"))
    return (_header(CLIENT_FULL_REQUEST, FLAG_NONE, SERIAL_JSON, COMPRESS_GZIP)
            + struct.pack(">I", len(payload)) + payload)


def audio_frame(pcm: bytes, last: bool = False) -> bytes:
    """普通音频包 flags=0，最后一包（负包）flags=0b0010。"""
    payload = gzip.compress(pcm)
    flags = FLAG_LAST if last else FLAG_NONE
    return (_header(CLIENT_AUDIO_ONLY, flags, SERIAL_NONE, COMPRESS_GZIP)
            + struct.pack(">I", len(payload)) + payload)


def parse_server_frame(data: bytes):
    """把一个 WebSocket 二进制帧解成 dict；错误帧返回 {'__error__': (code, msg)}。"""
    if len(data) < 8:
        return None
    h = data[:4]
    msg_type, flags = h[1] >> 4, h[1] & 0x0F
    compress = h[2] & 0x0F
    if msg_type == SERVER_ERROR:
        code = struct.unpack(">I", data[4:8])[0]
        size = struct.unpack(">I", data[8:12])[0] if len(data) >= 12 else 0
        msg = data[12:12 + size].decode("utf-8", "replace")
        return {"__error__": (code, msg)}
    if msg_type not in (SERVER_FULL_RESPONSE, SERVER_ACK):
        return None
    pos = 4
    # 官方示例里 full server response 在 header 后带 4B sequence
    if msg_type == SERVER_FULL_RESPONSE and flags in (FLAG_POS_SEQ, FLAG_NEG_SEQ):
        pos += 4
    if len(data) < pos + 4:
        return None
    size = struct.unpack(">I", data[pos:pos + 4])[0]
    payload = data[pos + 4:pos + 4 + size]
    if not payload:
        return None
    if compress == COMPRESS_GZIP:
        try:
            payload = gzip.decompress(payload)
        except OSError:
            return None
    try:
        return json.loads(payload.decode("utf-8"))
    except Exception:
        return None


def _rms(pcm: bytes) -> float:
    if len(pcm) < 2:
        return 0.0
    n = len(pcm) // 2
    vals = struct.unpack("<%dh" % n, pcm[:n * 2])
    return math.sqrt(sum(v * v for v in vals) / n) if n else 0.0


class BaseASR:
    mode = "base"

    def __init__(self, cfg: dict, callbacks: dict, session_id: str = ""):
        self.cfg = cfg
        self.cb = callbacks
        self.session_id = session_id
        self.lang = cfg.get("default_lang", "zh-CN")
        self.hotwords = list(cfg.get("hotwords", []))
        self.started_at = time.time()
        self.frames_in = 0
        self.bytes_in = 0

    async def _emit(self, name, *args):
        fn = self.cb.get(name)
        if fn:
            try:
                r = fn(*args)
                # 回调允许是同步函数：直接 await 会抛
                # "object NoneType can't be used in 'await' expression"，
                # 而且异常被吞掉后表现为「识别成功但前端一片空白」，极难排查。
                if inspect.isawaitable(r):
                    await r
            except Exception as e:
                print("[asr] callback %s error: %s" % (name, e))

    async def start(self, lang: str, hotwords: list | None = None):
        self.lang = lang or self.lang
        if hotwords:
            self.hotwords = hotwords

    async def feed(self, pcm: bytes):
        self.frames_in += 1
        self.bytes_in += len(pcm)

    async def inject_text(self, text: str, speaker: str = "客户", meta: dict | None = None):
        raise NotImplementedError

    async def stop(self):
        pass

    def info(self):
        d = {"mode": self.mode, "lang": self.lang, "frames": self.frames_in,
             "bytes": self.bytes_in, "hotwords": len(self.hotwords)}
        d.update(validate_asr_config(self.cfg, strict=False)["shape"])
        return d


class MockASR(BaseASR):
    """零密钥可跑：接收真实 PCM（做能量统计），文本由 inject_text / mock_say 注入。

    这样「麦克风 → 音频帧 → 字幕 → 主题 → 提示卡」的链路形状与真实模式一致，
    只是把声学模型替换成一个可预测的文本源，便于无凭据联调与演示。
    """
    mode = "mock"

    def __init__(self, cfg, callbacks, session_id=""):
        super().__init__(cfg, callbacks, session_id)
        self.last_voice = time.time()
        self.voice_frames = 0

    async def feed(self, pcm: bytes):
        await super().feed(pcm)
        if _rms(pcm) > 180:
            self.last_voice = time.time()
            self.voice_frames += 1

    async def inject_text(self, text: str, speaker="客户", meta=None):
        text = (text or "").strip()
        if not text:
            return
        # 模拟流式 ASR 的中间结果：先出半句，再出全句
        cut = max(2, int(len(text) * 0.55))
        await self._emit("on_partial", text[:cut], self.lang,
                         {"speaker": speaker, "source": "mock"})
        await asyncio.sleep(0.12)
        await self._emit("on_partial", text, self.lang,
                         {"speaker": speaker, "source": "mock"})
        await asyncio.sleep(0.08)
        await self._emit("on_final", text, self.lang,
                         {"speaker": speaker, "source": "mock", "definite": True})
        self.last_voice = time.time()

    async def stop(self):
        await self._emit("on_status", "info", "mock ASR 已停止，未产生任何伪造识别结果")


class DoubaoASR(BaseASR):
    """真实豆包流式 ASR。按语种自动选择轨迹：

      stream  : bigmodel_async（中英/方言/auto）→ 实时 partial + 二遍 definite
      segment : bigmodel_nostream（其余 23 语种）→ 分段 + 负包，300–400ms 返回
    """
    mode = "doubao"

    def __init__(self, cfg, callbacks, session_id=""):
        super().__init__(cfg, callbacks, session_id)
        self.validation = validate_asr_config(cfg)
        self.track = "stream"
        self.reader = self.writer = None
        self._recv_task = None
        self._closed = False
        self._buf = bytearray()
        self._segment_buf = bytearray()
        self._chunk_bytes = int(cfg.get("sample_rate", 16000) *
                                cfg.get("chunk_ms", 200) / 1000) * 2
        self._segment_bytes = int(cfg.get("sample_rate", 16000) *
                                  cfg.get("segment_ms", 4000) / 1000) * 2
        self._seg_tasks = set()
        self._last_partial = ""
        self._last_final = ""
        self._final_count = 0
        # 分句身份去重：end_time(ms) 集合 + 插入顺序（超长会话只保留最近 1024 个）
        self._finalized = set()
        self._finalized_order = deque()

    # ---------------- 连接与配置 ----------------
    def _auth_headers(self):
        """只发一套鉴权头：新版 X-Api-Key 或旧版 App-Key+Access-Key，绝不混用。

        Resource ID 与 API Key 必须来自【同一个控制台项目】，这点代码无法代查，
        只能在握手失败时通过 diagnose_handshake 提示。
        """
        cfg = self.cfg
        rid = str(uuid.uuid4())
        h = {
            "X-Api-Resource-Id": cfg.get("resource_id", "volc.seedasr.sauc.duration"),
            "X-Api-Request-Id": rid,
            "X-Api-Sequence": "-1",
            "X-Api-Connect-Id": str(uuid.uuid4()),
        }
        if cfg.get("auth_mode", "new") == "legacy":
            h["X-Api-App-Key"] = cfg.get("app_key", "")
            h["X-Api-Access-Key"] = cfg.get("access_key", "")
        else:
            h["X-Api-Key"] = cfg.get("api_key", "")
        return h

    def _build_config(self, lang: str, with_language: bool):
        req = {
            "model_name": "bigmodel",
            "enable_itn": True,
            "enable_punc": True,
            "enable_ddc": False,
            "show_utterances": True,
            "result_type": "full",
        }
        audio = {"format": "pcm", "codec": "raw", "rate": 16000, "bits": 16, "channel": 1}
        if with_language and lang not in ("auto", ""):
            audio["language"] = lang
        if self.track == "stream":
            req["enable_nonstream"] = bool(self.cfg.get("enable_nonstream", True))
            req["enable_lid"] = True
            req["end_window_size"] = int(self.cfg.get("end_window_size", 800))
            # 自动语种模式不支持 corpus 热词（官方约束）
            if lang not in ("auto", "") and self.hotwords:
                req["corpus"] = {"context": json.dumps(
                    {"hotwords": [{"word": w} for w in self.hotwords[:20]]},
                    ensure_ascii=False)}
        # 可选透传：StreamMode 属于 RTC / 对话式 AI 的 ASRConfig，不属于原生 sauc 协议。
        # 默认 stream_mode=0 时完全不发送，避免往 full client request 塞未知字段。
        sm = int(self.cfg.get("stream_mode", 0) or 0)
        if sm in (1, 2):
            req["StreamMode"] = sm
        return {"user": {"uid": self.session_id or "qieshen-live"},
                "audio": audio, "request": req}

    async def _connect(self, lang: str, with_language: bool):
        url = (self.cfg.get("endpoint_segment") if self.track == "segment"
               else self.cfg.get("endpoint_stream"))
        try:
            self.reader, self.writer, resp = await ws_client_connect(
                url, self._auth_headers(), timeout=10.0)
        except WsHandshakeError as e:
            raise RuntimeError(diagnose_handshake(e, self.cfg)) from e
        logid = (resp or {}).get("x-tt-logid", "")
        if logid:
            await self._emit("on_status", "info",
                             "豆包 X-Tt-Logid=%s（排错请保留给火山工单）" % logid)
        await send_frame(self.writer, 0x2, full_request_frame(
            self._build_config(lang, with_language)), mask=True)

    def choose_track(self, lang: str) -> str:
        """中英/方言/auto 走低延迟双向流式；其余 23 语种走指定 language 的分段轨。"""
        stream_langs = set(self.cfg.get("stream_langs",
                                        ["zh-CN", "en-US", "yue-CN", "auto"]))
        return "stream" if lang in stream_langs else "segment"

    async def start(self, lang: str, hotwords=None):
        v = validate_asr_config(self.cfg)
        if not v["ok"]:
            raise RuntimeError("豆包 ASR 配置自检未通过：" + "；".join(v["errors"]))
        await super().start(lang, hotwords)
        self.track = self.choose_track(self.lang)
        if self.track == "stream":
            await self._connect(self.lang, False)
            self._recv_task = asyncio.create_task(self._recv_loop())
        await self._emit("on_status", "info",
                         "豆包 ASR 已连接 · %s 轨 · lang=%s" % (self.track, self.lang))

    # ---------------- 发送音频 ----------------
    async def feed(self, pcm: bytes):
        await super().feed(pcm)
        # 已 stop / 上游连接已断：丢掉这一帧，不要往 None 的 writer 上写。
        # 旧实现直接 send_frame(self.writer=...) → AttributeError，上层若没兜住就会
        # 连 WS 一起死；现象是「停止录音后再开始，一个字都识别不出来」。
        if self._closed or self.writer is None:
            self._buf.clear()
            return
        self._buf += pcm
        while len(self._buf) >= self._chunk_bytes:
            chunk = bytes(self._buf[:self._chunk_bytes])
            del self._buf[:self._chunk_bytes]
            if self.track == "stream":
                await send_frame(self.writer, 0x2, audio_frame(chunk), mask=True)
            else:
                self._segment_buf += chunk
                if len(self._segment_buf) >= self._segment_bytes:
                    seg = bytes(self._segment_buf)
                    self._segment_buf.clear()
                    t = asyncio.create_task(self._run_segment(seg))
                    self._seg_tasks.add(t)
                    t.add_done_callback(self._seg_tasks.discard)

    async def _run_segment(self, pcm: bytes):
        """nostream 轨：每段时间片独立会话，发完负包取确定性结果。"""
        try:
            await self._connect(self.lang, True)
            step = self._chunk_bytes
            for i in range(0, len(pcm), step):
                part = pcm[i:i + step]
                await send_frame(self.writer, 0x2,
                                 audio_frame(part, last=(i + step >= len(pcm))), mask=True)
            await self._segment_recv()
        except Exception as e:
            await self._emit("on_status", "warn", "分段识别失败：%s" % e)
        finally:
            await self._close_conn()

    async def _segment_recv(self):
        deadline = time.time() + 6.0
        while time.time() < deadline:
            try:
                op, payload, _ = await asyncio.wait_for(read_frame(self.reader), 3.0)
            except asyncio.TimeoutError:
                break
            except Exception:
                break
            if op == 0x8:
                break
            if op != 0x2:
                continue
            parsed = parse_server_frame(payload)
            if not parsed:
                continue
            if "__error__" in parsed:
                await self._emit("on_status", "warn", "豆包错误 %s" % (parsed["__error__"],))
                break
            if self._handle_result(parsed, definite_override=True):
                break

    async def _recv_loop(self):
        while not self._closed:
            try:
                op, payload, fin = await read_frame(self.reader)
            except asyncio.CancelledError:
                raise
            except (asyncio.IncompleteReadError, ConnectionResetError,
                    BrokenPipeError, OSError):
                break          # 对端正常关闭 / 网络断
            except Exception as e:
                # 编程错误（字段名写错之类）绝不能静默吞掉：一旦吞掉，现象是
                # 「握手成功、音频在传，但前端一个字都不出」，几乎无法定位。
                await self._emit("on_status", "warn",
                                 "ASR 接收循环异常终止：%s: %s" % (type(e).__name__, e))
                break
            if op == 0x8:
                break
            if op == 0x9:  # ping -> pong
                await send_frame(self.writer, 0xA, payload, mask=True)
                continue
            if op != 0x2:
                continue
            parsed = parse_server_frame(payload)
            if not parsed:
                continue
            if "__error__" in parsed:
                await self._emit("on_status", "warn", "豆包错误 %s" % (parsed["__error__"],))
                continue
            # 真正的「负包」标志在二进制头里（flags=0x3），不在 WebSocket FIN 位
            proto_flags = (payload[1] & 0x0F) if len(payload) > 1 else 0
            self._handle_result(parsed, flags=proto_flags)

    def _handle_result(self, parsed: dict, definite_override=False, flags: int = 0) -> bool:
        """返回 True 表示本轮已产出一个确定分句（segment 轨据此结束会话）。

        实测（ASR 2.0 · bigmodel_async · enable_nonstream=true）同一个分句会被发两次：
          flags=0x1 正常下发 → flags=0x3（负包）把同一分句再回放一遍，
        两者 utterances[i].end_time 完全相同、additions.source 都是 two_pass。
        不去重就会重复推送同一句定稿：前端看到两遍，主题提取白跑一次，还吃掉推送额度。

        因此用 end_time 作为分句身份去重——不靠文本比对，二遍识别改写了措辞也认得是同一句；
        一包内若有多个新分句则逐个发 on_final，保持「一句话轮」的下游语义。
        """
        result = parsed.get("result") or {}
        text = (result.get("text") or "").strip()
        utts = result.get("utterances") or []

        emitted = False
        for u in utts:
            if not u.get("definite"):
                continue
            ut = (u.get("text") or "").strip()
            if not ut:
                continue
            end_ms = u.get("end_time")
            if isinstance(end_ms, int) and end_ms >= 0:
                if end_ms in self._finalized:
                    continue                     # 同一分句重复下发（含负包回放）
                self._finalized.add(end_ms)
                self._finalized_order.append(end_ms)
                while len(self._finalized_order) > 1024:   # 防长会话无限增长
                    self._finalized.discard(self._finalized_order.popleft())
            elif self._is_dup_final(ut):
                continue
            self._last_final = ut
            self._final_count += 1
            asyncio.create_task(self._emit(
                "on_final", ut, self.lang,
                {"source": "doubao", "definite": True, "track": self.track,
                 "end_time": end_ms, "last_packet": bool(flags & FLAG_NEG_SEQ)}))
            emitted = True

        if emitted:
            self._last_partial = ""
            return True

        # segment 轨（bigmodel_nostream）不一定带 utterances，退回整段文本判断
        if definite_override and text and not self._is_dup_final(text):
            self._last_final = text
            self._final_count += 1
            asyncio.create_task(self._emit(
                "on_final", text, self.lang,
                {"source": "doubao", "definite": True, "track": self.track}))
            return True

        if text and text != self._last_partial:
            self._last_partial = text
            asyncio.create_task(self._emit(
                "on_partial", text, self.lang,
                {"source": "doubao", "track": self.track}))
        return False

    def _is_dup_final(self, text: str) -> bool:
        """文本级兜底：完全重复，或只差末尾标点/空白 → 视为同一句。"""
        last = self._last_final
        if not last:
            return False
        strip = lambda s: re.sub(r"[\s，。、！？,.!?；;：:]+", "", s or "")
        return text == last or strip(text) == strip(last)

    async def inject_text(self, text: str, speaker="客户", meta=None):
        """真实模式下也允许文本注入（联调用），产出与 ASR 同构的定稿事件。"""
        if (text or "").strip():
            await self._emit("on_final", text.strip(), self.lang,
                             {"speaker": speaker, "source": "inject", "definite": True})

    async def _close_conn(self):
        if self.writer:
            try:
                self.writer.close()
            except Exception:
                pass
        self.reader = self.writer = None

    async def stop(self):
        self._closed = True
        if self.track == "stream" and self.writer:
            try:
                await send_frame(self.writer, 0x2, audio_frame(b"", last=True), mask=True)
                await asyncio.sleep(0.25)
            except Exception:
                pass
        if self._recv_task:
            self._recv_task.cancel()
        await self._close_conn()
        await self._emit("on_status", "info", "豆包 ASR 会话已结束")


def build_asr(cfg: dict, callbacks: dict, session_id: str = "") -> BaseASR:
    provider = cfg.get("provider", "mock")
    if provider == "doubao":
        key_ok = bool(cfg.get("api_key")) or (
            cfg.get("auth_mode") == "legacy" and cfg.get("app_key"))
        if not key_ok:
            print("[asr] 配置为 doubao 但缺少密钥，自动降级为 mock")
            return MockASR(cfg, callbacks, session_id)
        return DoubaoASR(cfg, callbacks, session_id)
    return MockASR(cfg, callbacks, session_id)
