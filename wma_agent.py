"""主题提取层：WMA（WorkBuddy Managed Agents）为主，本地规则为兜底。

WMA 事实（见架构设计 01 章）：它是云端托管的任务执行型 Agent，数据面走 ACP
—— GET {link} 建 SSE 接收通道拿 Acp-Connection-Id，POST {link} 发 JSON-RPC
initialize → session/load → session/prompt，回复从 SSE 异步推回，以
session/prompt 的 JSON-RPC response 作为本轮结束标志。因此这里维持一个长会话
并复用，而不是每话轮新建会话。

三种 provider：
    mock   —— 本地规则主题（零配置，输出与 WMA 完全同构）
    acp    —— 真实 WMA / ACP 通道
    openai —— OpenAI 兼容接口（便于先用现成模型验证提示词）

任何真实 provider 超时 / 解析失败，都会被 ResilientExtractor 降级为本地规则，
并置 degraded=True，保证链路永不空转。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time

from wsproto import http_request, open_sse

# 多语种关键词映射：ASR 可能输出外文，但文档是中文，需映射回中文再检索
MULTILINGUAL = {
    "edge gateway": "边缘网关", "fanless": "无风扇", "wide temperature": "宽温",
    "on-premise": "私有化", "protocol": "协议", "private cloud": "私有化",
    "price": "报价", "discount": "折扣", "delivery": "交付", "roadmap": "版本路线",
    "competitor": "竞品", "5g": "内置5G", "modbus": "Modbus",
    "エッジゲートウェイ": "边缘网关", "ファンレス": "无风扇", "広温度": "宽温",
    "オンプレミス": "私有化", "プロトコル": "协议", "価格": "报价",
}

INTENT_RULES = [
    ("price_objection", ["太贵", "报价", "多少钱", "预算", "折扣", "便宜", "价格", "price", "discount"]),
    ("competitor_compare", ["竞品", "别家", "友商", "对比", "差在哪", "competitor", "他家"]),
    ("roadmap_question", ["什么时候", "下一版", "规划", "下一代", "路线", "roadmap", "未来版本"]),
    ("delivery_question", ["交期", "多久", "部署", "交付", "实施", "delivery", "信创"]),
    ("tech_why", ["为什么", "凭什么", "怎么做到", "原理", "依据", "实测", "口径",
                  "单机", "集群", "压测", "标称", "测试条件", "why", "benchmark"]),
    ("capability_question", ["能不能", "支持吗", "是否支持", "有没有", "可不可以",
                             "能扛", "内置", "can it", "support"]),
]

CHITCHAT = ["您好", "你好", "路上", "喝水", "天气", "吃了吗", "辛苦了", "hello", "hi there"]

STOPWORDS = set("的 了 吗 呢 吧 啊 我们 你们 这个 那个 一下 可以 请问 现在 就是 "
                "a an the is are to of for we you it this that can do".split())


def _parse_json_loose(raw: str):
    if not raw:
        return None
    s = raw.strip()
    if "```" in s:
        m = re.search(r"```(?:json)?\s*([\s\S]*?)```", s)
        if m:
            s = m.group(1).strip()
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        return json.loads(s[i:j + 1])
    except Exception:
        return None


def _brief(text: str, n: int = 180) -> str:
    """把响应体压成一行短摘要，用于错误信息（不吞掉真相，也不刷屏）。"""
    if not text:
        return "(空响应)"
    s = " ".join(str(text).split())
    return s if len(s) <= n else s[:n] + "…"


def _ctrl_error_hint(status: int, text: str) -> tuple[str, str]:
    """把控制面失败分流成「病因 + 该做什么」。只看状态码会误判，必须看响应体。"""
    low = (text or "").lower()
    if "invalid_token" in low or "expired token" in low:
        return ("凭据无效或已过期",
                "确认填的是 pt_ 应用 Key（要放 Authorization: Bearer，放 X-Api-Key 会按 ck_ 校验而失败）"
                "或 <keyID>.<secret> 个人 Key；到开放平台应用管理 / 个人设置-API Keys 重新生成")
    if "not_found" in low:
        return ("凭据不存在（keyID 在库里查不到）",
                "ck_ 个人 Key 的 keyID 或 secret 写错 / 已被删除；到个人设置-API Keys 重建后整串复制"
                "（格式 <keyID>.<secret>，含点号）")
    if status == 401:
        return ("请求没带上有效凭据",
                "检查 acp.app_key / acp.user_key 是否为空；401 且返回 HTML 说明根本没到业务层就被网关拦下")
    if status == 403:
        return ("网关判定这把 Key 与路径里的 enterpriseId 不是同一个企业",
                "两种可能，请求本身分不出来：① enterpriseId 不是企业控制台「企业设置-企业信息」里的那串"
                "（个人中心显示的「1000…@企业名」是部门名，不一定等于企业 ID）；② Key 没有企业归属"
                "（建在个人空间，或企业未完成实名认证——官方文档：未认证企业部分能力受限）。"
                "先取控制台里的企业 ID 再试，仍 403 就用企业管理员生成 pt_ 应用 Key")
    if status == 429:
        return ("网关限频（单 IP 默认 600 次/秒）", "退避重试；本服务已做节流，通常是别处并发在打")
    if "unprovisionedtenant" in low:
        return ("租户未开通，或企业不存在",
                "去腾讯云 WorkBuddy Managed Agents 控制台点「立即开通」并关联该企业")
    if "entitlementnotfound" in low:
        return ("未查询到服务开通记录", "先开通 WMA 服务（需绑定 WorkBuddy 企业版席位）")
    if "resourcenotfound.agent" in low:
        return ("Agent 不存在", "核对 acp.agent_id；或先在控制台创建 Agent 并发布版本")
    if "permissiondenied" in low or "unauthorizedoperation" in low:
        return ("凭据权限不足", "该 Key 的角色没有 Agent 管理权限；换管理员凭据或补授权")
    if status >= 500:
        return ("服务端错误", "稍后重试；带上 requestId 报障")
    return ("未归类的失败", "把完整响应体与 requestId 留档再排查")


async def _sanity_403(base: str, key: str, ent: str):
    """403 对照实验：换一个不可能存在的 enterpriseId 再打一次。

    网关把「企业不匹配」统一回成同一句话，所以单看 403 无法区分
    「eid 写错了」与「这把 key 根本没绑企业」。加一次哨兵请求就能分清：
      · 哨兵也是 403 且响应一致 → 与 eid 无关，是凭据没企业归属
      · 哨兵不是 403 → 说明 eid 才是变量，换对 eid 就能通
    """
    marker = "/enterprises/%s/" % ent
    if marker not in base:
        return None
    sentinel = base.replace(marker, "/enterprises/__sentinel_no_such_ent__/")
    try:
        st, _, raw = await http_request(
            sentinel + "/agents?page=1&pageSize=1", "GET",
            {"Authorization": "Bearer " + key, "Accept": "application/json"}, None, 8.0)
        return st, _brief(raw.decode("utf-8", "replace"), 120)
    except Exception:
        return None


async def probe_wma(acp: dict) -> dict:
    """只发一次控制面 GET /agents，回答「企业 ID + 凭据 + 服务是否开通」。

    不建会话、不建 Agent、不产生会话时长计费 —— 与豆包的 /api/probe 同一设计意图：
    申请完先确认能不能用，再谈接不接。
    """
    acp = acp or {}
    ent = str(acp.get("enterprise_id") or "").strip()
    raw_base = str(acp.get("base_url") or "")
    base = (raw_base.replace("{enterprise_id}", ent) if ent else raw_base).rstrip("/")
    key = str(acp.get("app_key") or acp.get("user_key") or "").strip()
    out = {"ok": False, "base_url": base, "enterprise_id": ent,
           "auth": "app_key(pt_)" if acp.get("app_key") else
                   ("user_key(ck_)" if acp.get("user_key") else "none"),
           "agent_id_configured": bool(acp.get("agent_id"))}
    try:
        if not ent:
            out["message"] = "缺 acp.enterprise_id（WorkBuddy 企业 ID）"
            out["hint"] = "在腾讯云 WorkBuddy Managed Agents 控制台开通后即可看到企业；也可从企业控制台地址取"
            return out
        if not key:
            out["message"] = "缺控制面凭据：acp.app_key（pt_…）或 acp.user_key（ck_…）"
            out["hint"] = "pt_ 在开放平台应用管理生成；ck_ 在个人设置-API Keys 创建"
            return out

        status, _, raw = await http_request(
            base + "/agents?page=1&pageSize=1", "GET",
            {"Authorization": "Bearer " + key, "Accept": "application/json"}, None, 8.0)
        text = raw.decode("utf-8", "replace")
        out["status"] = status
        if status >= 400:
            out["message"], out["hint"] = _ctrl_error_hint(status, text)
            out["raw"] = _brief(text)
            if status == 403:
                chk = await _sanity_403(base, key, ent)
                if chk:
                    out["sentinel_status"], out["sentinel_raw"] = chk
                    # 注意这只能证明「换 eid 不会有任何变化」，不能推出 key 没企业归属：
                    # 等值判断下，任何不匹配的 eid 和「无归属」返回的是同一句话。
                    if chk[0] == 403:
                        out["message"] = "403：网关认为这把 Key 与 eid=%s 不属于同一企业" % ent
                        out["hint"] = (
                            "换任何 enterpriseId 都是同一句 403，所以请求层面到此为止，只有两种可能："
                            "① enterpriseId 不是这个值——去企业控制台「企业设置-企业信息」取企业 ID"
                            "（个人中心里「1000…@企业名」那串是部门名，不一定等于企业 ID）；"
                            "② Key 没有企业归属——可能建在个人空间，或企业尚未完成实名认证"
                            "（官方文档：未认证企业部分能力受限）。"
                            "仍不通就让企业管理员生成 pt_ 应用 Key 填 acp.app_key")
                    else:
                        out["message"] = "凭据有效，但与路径里的 enterpriseId 不匹配"
                        out["hint"] = "换成凭据所属企业的 enterpriseId（企业 ID 只能用路径参数声明）"
            return out
        try:
            d = json.loads(text)
        except Exception:
            out["message"] = "控制面返回非 JSON（可能打到了错误的 host 或路径）"
            out["hint"] = "检查 acp.base_url 应为 …/v2/enterprises/{enterpriseId}/managedagent（注意：没有 /openapi 前缀）"
            out["raw"] = _brief(text)
            return out
        if d.get("code") not in (0, "0", None):
            out["message"], out["hint"] = _ctrl_error_hint(200, text)
            out["code"] = str(d.get("code"))
            out["raw"] = _brief(text)
            return out

        data = d.get("data") or {}
        total = data.get("totalCount")
        out["agents_total"] = total
        out["ok"] = True
        out["message"] = "控制面连通，凭据有效 · 可见 Agent %s 个" % (
            total if total is not None else "?")
        if not acp.get("agent_id"):
            out["hint"] = "还差 acp.agent_id：在控制台 Agents 页创建/选定一个 Agent 再填进来"
        return out
    except Exception as e:
        out["message"] = "%s: %s" % (type(e).__name__, str(e)[:160])
        out["hint"] = "网络层失败：确认能访问 www.workbuddy.cn、无代理拦截 HTTPS"
        return out


# ---------------------------------------------------------------------------
# 材料主题表：模型只做「归类」，label 必须落在表内
# ---------------------------------------------------------------------------
# 让模型自由命名主题是这条链路最容易出错的地方——它会把「访客登记」说成
# 「访客管理」「门禁访客」「人员出入」，而资料里叫「人员与车辆管理」。
# 标签一对不上，检索再准也是白做。所以：表由资料生成（_wma_taxonomy.py），
# 模型只能选，不能造；选偏了在下面这一层被拦掉，绝不外泄给前端。
_TAX = {"by_label": {}, "by_id": {}, "by_doc": {}, "topics": [],
        "aliases": {}, "loaded": False}


def set_taxonomy_rows(rows) -> dict:
    """用显式主题行重置校验表（不读文件）。

    为什么需要它：校验表（_TAX）决定 normalize 认哪些 label。如果校验表来自
    仓库自带的 prompts/taxonomy.json，而实际生效的是按知识库重建出来的表，
    两张表的 label 不一致 → 模型/规则给的主题全被当「自创主题」丢掉，
    线上表现就是「主题一直空、但卡片有」。两者必须同源。
    """
    by_label, by_id, by_doc, clean = {}, {}, {}, []
    aliases = {}          # 主题 label -> [客户实际会说的词]，本地规则匹配用
    for t in rows or []:
        if not isinstance(t, dict):
            continue
        label = str(t.get("label") or "").strip()
        if not label:
            continue
        by_label[label] = t
        by_label[_squash(label)] = t
        if t.get("id"):
            by_id[str(t["id"]).upper()] = t
        if t.get("doc_id"):
            by_doc[label] = t["doc_id"]
        say = [str(x).strip() for x in (t.get("say") or []) if str(x).strip()]
        aliases[label] = say
        clean.append({"id": t.get("id", ""), "label": label,
                      "kind": t.get("kind") or "general",
                      "visibility": t.get("visibility", "public"),
                      "say": say,
                      "tags": [str(x).strip() for x in (t.get("tags") or [])
                               if str(x).strip()]})
    _TAX.update(by_label=by_label, by_id=by_id, by_doc=by_doc,
                topics=clean, aliases=aliases, loaded=bool(clean))
    return _TAX


def load_taxonomy(path: str | None = None) -> dict:
    root = os.path.dirname(os.path.abspath(__file__))
    p = path or os.path.join(root, "prompts", "taxonomy.json")
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        _TAX.update(loaded=False)
        return _TAX
    return set_taxonomy_rows(data.get("topics") or [])


def _squash(s: str) -> str:
    """归一化：去掉一切分隔与标点，只留字，用来容忍模型的写法差异。"""
    return re.sub(r"[\s\-_·・、,，.。()（）:：/|]+", "", (s or "")).lower()


def _canon_label(label: str, tid: str = "") -> str | None:
    """把模型给的主题对回材料表里的合法 label；对不上返回 None。

    顺序：topic_id 精确 → label 精确 → 去标点后精确 → 双向包含（≥3 字）。
    对不上就是「材料里没有这个主题」，返回 None，由调用方丢弃。
    """
    if not _TAX["loaded"]:
        return label or None
    if tid:
        t = _TAX["by_id"].get(str(tid).upper())
        if t:
            return t["label"]
    if not label:
        return None
    t = _TAX["by_label"].get(label) or _TAX["by_label"].get(_squash(label))
    if t:
        return t["label"]
    key = _squash(label)
    if len(key) < 2:
        return None
    hits = {}
    for k, v in _TAX["by_label"].items():
        if len(k) < 2:
            continue
        if key in k or k in key:
            hits[v["label"]] = min(len(k), hits.get(v["label"], 99))
    if len(hits) != 1:
        # 一个短词同时沾上好几行，说明模型没真看表，只是猜了个大概。
        # 这种宁可整条丢掉，也不要推一张让销售当场说错的卡。
        return None
    return next(iter(hits))


def normalize(obj: dict | None, provider: str, degraded: bool, text: str) -> dict:
    """WMA 输出做 schema 校验；解析失败一律降级，绝不把脏数据推给前端。"""
    if not isinstance(obj, dict):
        obj = {}
    topics, dropped = [], []
    for t in obj.get("topics") or []:
        if not isinstance(t, dict):
            continue
        raw = str(t.get("label") or "").strip()
        tid = str(t.get("topic_id") or t.get("id") or "").strip()
        if not raw and not tid:
            continue
        try:
            conf = float(t.get("confidence", 0.6))
        except (TypeError, ValueError):
            conf = 0.6
        if conf < 0.5:
            continue
        label = _canon_label(raw, tid)
        if not label:
            # 模型自创了主题 / 把材料外的东西硬套进来 → 丢掉，不推错卡
            dropped.append(raw or tid)
            continue
        topics.append({"label": label,
                       "topic_id": _TAX["by_id"].get(tid.upper(), {}).get("id", ""),
                       "kind": str(t.get("kind") or "general"),
                       "confidence": round(min(1.0, conf), 2),
                       "evidence": str(t.get("evidence") or "")})
    # 同一句话重复给出同一个主题时去重，保留置信度高的那条
    if len(topics) > 1:
        best = {}
        for t in topics:
            old = best.get(t["label"])
            if not old or t["confidence"] > old["confidence"]:
                best[t["label"]] = t
        topics = sorted(best.values(), key=lambda x: -x["confidence"])
    raw_query = str(obj.get("query") or "").strip()
    query = raw_query
    if not query:
        query = " ".join(t["label"] for t in topics) or text[:40]
    # 主题全被拦不代表这句话没用：只要还有可检索的串，卡片仍然值得推——
    # 卡片自己有标题和结论，主题只是标签。旧逻辑在这里要求 topics 非空，
    # 一旦主题表没对上就整条静默，现场看到的就是「什么都不出」。
    push = bool(obj.get("should_push", True)) and (bool(topics) or bool(raw_query))
    if not push:
        query = ""
    return {
        "lang": str(obj.get("lang") or "zh-CN"),
        "query": query[:160], "topics": topics[:5],
        "intent": str(obj.get("intent") or "general"),
        "should_push": push,
        "push_reason": str(obj.get("push_reason") or ""),
        "degrade_reason": str(obj.get("degrade_reason") or ""),
        "dropped_topics": dropped,
        "provider": provider, "degraded": degraded,
    }


class RuleExtractor:
    """本地规则主题：与 WMA 同构输出，零配置可跑。

    词全部来自主题表（prompts/taxonomy.json）：label 本身、say（客户实际说法）、
    tags（资料关键词）都映射回**表内 label**。产出的主题天然能过 normalize 的
    强校验——否则会出现「规则给了主题、校验全丢掉」的死局：主题空 → 不推卡，
    前端看起来就是「什么都不工作」。
    """
    provider = "rule"

    def __init__(self, vocab: dict):
        self.vocab = vocab or {}
        self.rows = self.vocab.get("topics") or []
        self._terms = []          # (归一化词, 主题 label, 权重)
        for r in self.rows:
            label = str(r.get("label") or "").strip()
            if not label:
                continue
            self._terms.append((_squash(label), label, 1.0))
            for s in r.get("say") or []:
                s = str(s).strip()
                if len(s) >= 2:
                    self._terms.append((_squash(s), label, 0.92))
            for tg in r.get("tags") or []:
                tg = str(tg).strip()
                if len(tg) >= 2:
                    self._terms.append((_squash(tg), label, 0.78))
        if not self._terms:       # 兼容只给了 aliases 的旧词表
            for label, al in (self.vocab.get("aliases") or {}).items():
                for a in al:
                    self._terms.append((_squash(a), label, 0.9))

    def info(self) -> dict:
        """自检用：一眼看出规则到底有几个词可用。

        「主题全空」线上最常见的两种成因——主题表没加载（rows=0）、
        词典为空（terms=0）——在这里是能直接看见的数字，不用去猜。
        """
        say = sum(len([x for x in (r.get("say") or []) if len(str(x).strip()) >= 2])
                  for r in self.rows)
        tag = sum(len([x for x in (r.get("tags") or []) if len(str(x).strip()) >= 2])
                  for r in self.rows)
        return {"rows": len(self.rows), "terms": len(self._terms),
                "from_label": len([r for r in self.rows if r.get("label")]),
                "from_say": say, "from_tags": tag,
                "aliases": len(self.vocab.get("aliases") or {})}

    async def extract(self, text: str, context: dict | None = None):
        raw = (text or "").strip()
        low = raw.lower()
        sq = _squash(raw)
        lang = (context or {}).get("lang") or "zh-CN"
        # 多语种关键词回映射
        mapped = [v for k, v in MULTILINGUAL.items() if k in low and v not in raw]
        hay = raw + " " + " ".join(mapped)

        if any(c in low for c in CHITCHAT) and len(raw) < 18:
            return normalize({"lang": lang, "query": "", "topics": [],
                              "intent": "chitchat", "should_push": False},
                             self.provider, False, raw)

        # 词典为空（主题表没加载成功）时绝不能整轮静默：没有主题照样能按原话检索，
        # 卡片自己带标题和结论，主题只是标签。静默会把「主题表坏了」伪装成
        # 「服务全停了」，现场只能靠猜。
        if not self._terms:
            return normalize({"lang": lang, "query": hay.strip() or raw,
                              "topics": [], "intent": "general",
                              "should_push": bool(raw),
                              "degrade_reason": "主题表为空（词典 0 条），仅按原话检索"},
                             self.provider, True, raw)

        # 命中即累加到对应主题，同一主题取权重最高的那个词当证据
        best = {}
        for term, label, w in self._terms:
            if not term:
                continue
            if term in sq or term in low:
                old = best.get(label)
                if not old or w > old[0]:
                    best[label] = (w, term)
        # 命中的主题越多，说明这句话越可能只是闲聊，降一点置信度
        bump = 0.0 if len(best) <= 1 else -0.04 * (len(best) - 1)
        topics = []
        for label, (w, ev) in sorted(best.items(), key=lambda kv: -kv[1][0])[:3]:
            conf = min(0.98, max(0.5, 0.5 + 0.48 * w + bump))
            topics.append({"label": label, "kind": "general",
                           "confidence": round(conf, 2), "evidence": ev})

        intent = "general"
        for name, kws in INTENT_RULES:
            if any(k in low for k in kws):
                intent = name
                break

        zh_words = [w for w in re.findall(r"[\u4e00-\u9fff]{2,}", hay)
                    if w not in STOPWORDS]
        en_words = [w for w in re.findall(r"[a-zA-Z0-9]{2,}", low)
                    if w not in STOPWORDS and w not in MULTILINGUAL]
        # 文档是中文：外文词已映射成中文术语后就不必再塞英文原词，避免稀释检索串
        words = zh_words[:8] + ([] if mapped else en_words[:4])
        ev_words = [v[1] for _, v in sorted(best.items(), key=lambda kv: -kv[1][0])[:2]]
        query = " ".join([t["label"] for t in topics[:2]] + mapped + ev_words + words)
        return normalize({"lang": lang, "query": query, "topics": topics,
                          "intent": intent,
                          "should_push": bool(topics) or len(raw) > 8},
                         self.provider, False, raw)



class OpenAIExtractor:
    """OpenAI 兼容接口，用于先验证提示词，再迁到 WMA。"""
    provider = "openai"

    def __init__(self, cfg: dict, prompt: str, timeout_ms: int):
        self.cfg = cfg
        self.prompt = prompt
        self.timeout = timeout_ms / 1000.0

    async def extract(self, text: str, context: dict | None = None):
        o = self.cfg.get("openai", {})
        url = o.get("base_url", "").rstrip("/") + "/chat/completions"
        body = json.dumps({
            "model": o.get("model", "gpt-4o-mini"),
            "temperature": 0,
            "messages": [{"role": "system", "content": self.prompt},
                         {"role": "user", "content": text}],
        }, ensure_ascii=False).encode("utf-8")
        status, _, raw = await asyncio.wait_for(http_request(
            url, "POST", {"Content-Type": "application/json",
                          "Authorization": "Bearer " + o.get("api_key", "")},
            body, timeout=self.timeout), self.timeout + 1)
        if status >= 400:
            raise RuntimeError("OpenAI 兼容接口 HTTP %s" % status)
        obj = json.loads(raw.decode("utf-8", "replace"))
        content = obj["choices"][0]["message"]["content"]
        parsed = _parse_json_loose(content)
        if not parsed:
            raise RuntimeError("模型返回非 JSON")
        parsed.setdefault("lang", (context or {}).get("lang", "zh-CN"))
        return normalize(parsed, self.provider, False, text)


class AcpExtractor:
    """WMA / ACP：SSE 接收 + JSON-RPC 发送，长会话复用。

    控制面（REST，Bearer pt_/ck_）：建会话 → 查用户级 access_token
    数据面（ACP）：GET {link} 建 SSE → POST {link} 发 initialize / session/load / session/prompt

    两者凭据不可混用：pt_/ck_ 只用于控制面；user-access-token 下发的 accessToken
    只用于数据面。把 accessToken 当控制面凭据用会 401。
    """
    provider = "wma"

    def __init__(self, cfg: dict, prompt: str, timeout_ms: int):
        c = cfg.get("acp", {}) or {}
        self.cfg = c
        self.prompt = prompt
        self.timeout = timeout_ms / 1000.0
        # 提示词放哪边：
        #   agent  —— 已在 Agent 的 Manifest/控制台里配好，数据面每轮只发一句话（默认，最快）
        #   client —— Agent 侧没配，客户端每轮把提示词拼在输入前面（兼容老配置）
        # 5888 字的提示词 ≈ 3500 token，每轮重发一次就是白等一次预填充。
        self.prompt_mode = str(cfg.get("prompt_mode") or "agent").strip().lower()

        # 控制面
        self.enterprise_id = (c.get("enterprise_id") or "").strip()
        self.base = (c.get("base_url") or "").replace(
            "{enterprise_id}", self.enterprise_id).rstrip("/")
        self.ctrl_key = (c.get("app_key") or c.get("user_key") or "").strip()
        self.agent_id = (c.get("agent_id") or "").strip()
        self.version_id = (c.get("version_id") or "").strip()

        # 数据面（可由控制面填充，也可直连）
        self.link = (c.get("link") or "").strip()
        self.token = (c.get("token") or "").strip()
        self.session_id = (c.get("session_id") or "").strip()

        self.conn_id = ""
        self.reader = self.writer = None
        self._sse_task = None
        self._pending = {}
        self._chunks = []
        self._id = 0
        self._ready = False

    def _next_id(self):
        self._id += 1
        return self._id

    # ---------------- 控制面 ----------------
    async def _ctrl(self, method: str, path: str, payload: dict | None = None):
        if not self.base:
            raise RuntimeError("WMA 控制面 base_url 无法确定：请填 acp.enterprise_id，"
                               "或直连填 acp.link + acp.token + acp.session_id")
        if not self.ctrl_key:
            raise RuntimeError("WMA 控制面缺凭据：请填 acp.app_key（pt_…）或 acp.user_key（ck_…）")
        url = self.base + path
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        status, _, raw = await http_request(
            url, method,
            {"Authorization": "Bearer " + self.ctrl_key,
             "Content-Type": "application/json"}, body, 8.0)
        text = raw.decode("utf-8", "replace")
        if status >= 400:
            raise RuntimeError("WMA 控制面 %s %s → HTTP %s %s"
                               % (method, path, status, _brief(text)))
        try:
            d = json.loads(text)
        except Exception:
            raise RuntimeError("WMA 控制面返回非 JSON：%s" % _brief(text))
        code = d.get("code")
        if code not in (0, "0", None):
            raise RuntimeError("WMA %s · %s" % (code, d.get("msg") or _brief(text)))
        return d.get("data") or {}

    async def _create_session(self):
        """POST /agents/{agentId}/sessions → sessionId + endpointSet。"""
        if not self.agent_id:
            raise RuntimeError("WMA 控制面缺 acp.agent_id：请先在控制台创建 Agent")
        payload = {"AgentId": self.agent_id}
        if self.version_id:
            payload["VersionId"] = self.version_id
        data = await self._ctrl("POST", "/agents/%s/sessions" % self.agent_id, payload)
        self.session_id = str(data.get("sessionId") or "")
        if not self.session_id:
            raise RuntimeError("建会话成功但未返回 sessionId")
        for ep in (data.get("endpointSet") or []):
            if isinstance(ep, dict) and str(ep.get("url") or "").startswith("http"):
                self.link = ep["url"]
                break

    async def _user_token(self):
        """POST /user-access-token → 数据面 accessToken（与控制面凭据是两回事）。"""
        data = await self._ctrl("POST", "/user-access-token")
        tok = str(data.get("accessToken") or "")
        if not tok:
            raise RuntimeError("user-access-token 未返回 accessToken")
        self.token = tok

    async def _open_channel(self):
        self.reader, self.writer, headers = await open_sse(
            self.link, {"Authorization": "Bearer " + self.token}, 10.0)
        self.conn_id = headers.get("acp-connection-id", "")
        self._sse_task = asyncio.create_task(self._sse_loop())

    async def _sse_loop(self):
        while True:
            try:
                line = await self.reader.readline()
            except Exception:
                break
            if not line:
                break
            s = line.decode("utf-8", "replace").strip()
            if not s.startswith("data:"):
                continue
            payload = s[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                msg = json.loads(payload)
            except Exception:
                continue
            rid = msg.get("id")
            if rid in self._pending and ("result" in msg or "error" in msg):
                fut = self._pending.pop(rid)
                if not fut.done():
                    fut.set_result(msg)
                continue
            if msg.get("method") == "session/update":
                self._collect_text(msg.get("params") or {})

    def _collect_text(self, node):
        """从 ACP session/update 里抽取 agent_message_chunk 的文本增量。"""
        if isinstance(node, dict):
            u = node.get("update")
            if isinstance(u, dict) and "content" in u:
                kind = str(u.get("sessionUpdate", ""))
                if not kind or "agent" in kind:
                    c = u["content"]
                    if isinstance(c, dict) and isinstance(c.get("text"), str):
                        self._chunks.append(c["text"])
                    elif isinstance(c, list):
                        for it in c:
                            if isinstance(it, dict) and isinstance(it.get("text"), str):
                                self._chunks.append(it["text"])
            for k, v in node.items():
                if k != "update" and isinstance(v, (dict, list)):
                    self._collect_text(v)
        elif isinstance(node, list):
            for v in node:
                self._collect_text(v)

    async def _post_rpc(self, method: str, params: dict):
        rid = self._next_id()
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        payload = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        headers = {"Authorization": "Bearer " + self.token,
                   "Content-Type": "application/json"}
        if self.conn_id:
            headers["Acp-Connection-Id"] = self.conn_id
        status, _, _ = await http_request(
            self.link, "POST", headers,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"), 8.0)
        if status >= 400:
            self._pending.pop(rid, None)
            raise RuntimeError("ACP POST HTTP %s" % status)
        return await asyncio.wait_for(fut, self.timeout)

    async def _ensure_ready(self):
        if self._ready:
            return
        if not (self.link and self.token):
            await self._create_session()
            await self._user_token()
        if not self.link:
            raise RuntimeError("会话已建但 endpointSet 无公网接入点")
        if not self.session_id:
            raise RuntimeError("直连模式还需 acp.session_id；"
                               "或改用控制面模式（enterprise_id + agent_id + app_key）")
        await self._open_channel()
        await self._post_rpc("initialize", {"protocolVersion": 1,
                                            "clientCapabilities": {
                                                "fs": {"readTextFile": False,
                                                       "writeTextFile": False}}})
        await self._post_rpc("session/load", {"sessionId": self.session_id,
                                              "cwd": "/workspace", "mcpServers": []})
        self._ready = True

    async def extract(self, text: str, context: dict | None = None):
        await self._ensure_ready()
        self._chunks = []
        if self.prompt_mode == "client":
            full = self.prompt + "\n\n# 本轮输入\n" + text
        else:
            # Agent 侧已带人设，这里只发本轮那句话——省一次全文预填充
            full = text
        resp = await self._post_rpc("session/prompt", {
            "sessionId": self.session_id,
            "prompt": [{"type": "text", "text": full}],
        })
        if "error" in resp:
            raise RuntimeError("ACP 返回错误: %s" % resp["error"])
        raw = "".join(self._chunks).strip()
        if not raw:
            self._collect_text(resp.get("result") or {})
            raw = "".join(self._chunks).strip()
        parsed = _parse_json_loose(raw)
        if not parsed:
            raise RuntimeError("WMA 输出无法解析为 JSON")
        parsed.setdefault("lang", (context or {}).get("lang", "zh-CN"))
        return normalize(parsed, self.provider, False, text)

    async def close(self):
        if self._sse_task:
            self._sse_task.cancel()
        if self.writer:
            self.writer.close()


class ResilientExtractor:
    """真实 provider 失败 → 本地规则降级，并明确标注 degraded。"""

    def __init__(self, primary, fallback: RuleExtractor):
        self.primary = primary
        self.fallback = fallback
        self.stats = {"primary_ok": 0, "degraded": 0, "last_error": ""}

    async def extract(self, text: str, context=None):
        try:
            out = await asyncio.wait_for(
                self.primary.extract(text, context),
                getattr(self.primary, "timeout", 2.5) + 0.5)
            self.stats["primary_ok"] += 1
            return out
        except Exception as e:
            self.stats["degraded"] += 1
            self.stats["last_error"] = "%s: %s" % (type(e).__name__, str(e)[:120])
            out = await self.fallback.extract(text, context)
            out["degraded"] = True
            out["provider"] = self.fallback.provider
            out["degrade_reason"] = self.stats["last_error"]
            return out


def build_extractor(cfg: dict, vocab: dict):
    root = os.path.dirname(os.path.abspath(__file__))
    p = cfg.get("system_prompt_file", "prompts/topic_extract.md")
    path = p if os.path.isabs(p) else os.path.join(root, p)
    try:
        with open(path, "r", encoding="utf-8") as f:
            prompt = f.read()
    except OSError:
        prompt = "只输出 JSON：{query, topics[], intent, should_push}。不要输出解释。"
    fallback = RuleExtractor(vocab)
    # 校验表必须跟「实际生效的那张表」同源：知识库重建过的表放在 data/kb，
    # 这里如果还去读仓库自带的 prompts/taxonomy.json，两边的 label 对不上，
    # 规则给的主题会被 normalize 当成自创主题整批丢掉——主题空、卡片有。
    if vocab.get("topics"):
        tax = set_taxonomy_rows(vocab["topics"])
        src = "生效主题表（vocab）"
    else:
        tax = load_taxonomy()
        src = "prompts/taxonomy.json"
    if tax.get("loaded"):
        print(" 校验主题表  : %d 个主题 · 来自%s（label 强校验已启用）" % (len(tax["by_id"]), src))
    else:
        print(" 校验主题表  : 未找到，label 不做校验")
    provider = cfg.get("provider", "mock")
    timeout = int(cfg.get("timeout_ms", 2500))
    if provider == "auto":
        # 先给本地规则兜底，服务启动时探一次控制面：通了就热切到 acp。
        # 好处是拿到凭据只需重启，不用改配置；不通也不影响演示。
        fallback.auto_pending = True
        return fallback
    if provider == "acp":
        return ResilientExtractor(AcpExtractor(cfg, prompt, timeout), fallback)
    if provider == "openai":
        return ResilientExtractor(OpenAIExtractor(cfg, prompt, timeout), fallback)
    return fallback


def extractor_info(ex):
    if isinstance(ex, ResilientExtractor):
        return {"provider": ex.primary.provider, "fallback": "rule",
                "degraded_count": ex.stats["degraded"],
                "primary_ok": ex.stats["primary_ok"],
                "last_error": ex.stats["last_error"]}
    return {"provider": getattr(ex, "provider", type(ex).__name__),
            "fallback": "none", "degraded_count": 0}
