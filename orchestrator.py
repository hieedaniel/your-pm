"""编排中心：定稿话轮 → 预取先推粗卡 → 主题并行提取 → 原地升级 → 抑制。

两段式（渐进式推送）：
  definite 到达 → 并行：MCP 原话轮预取  ∥  WMA/规则 主题提取
  预取先回来（50–200ms）→ 立刻推 top1 粗卡（provisional=true），销售马上有东西看
  主题回来（1–3s）→ 用主题 query 精检，与预取合并重排
      → 仍是它：发 hint.update 原地升级（补主题标、换更贴的片段）
      → 换了别篇：粗卡降级（demote，变灰加注），推新卡占剩余额度
  弱信号话轮（top1 分数不够）不推粗卡，宁可不推也不误导
  WMA 失败 → 本地规则主题（degraded 标注），缓存结果照常推送
"""
from __future__ import annotations

import asyncio
import re
import time
from collections import Counter, deque

from doubao_asr import BaseASR  # noqa: F401  (类型说明用)


def _pct(values, q):
    if not values:
        return 0.0
    s = sorted(values)
    return round(s[min(len(s) - 1, int(len(s) * q))], 1)


class Orchestrator:
    def __init__(self, cfg: dict, mcp, extractor, emit):
        self.cfg = cfg
        self.policy = cfg.get("policy", {})
        self.mcp = mcp
        self.extractor = extractor
        self.emit = emit
        self.tool_search = cfg.get("tool_search", "docs.search")
        self.turn_no = 0
        self.topic_count = 0
        self.card_count = 0
        self.silenced = Counter()
        self._silence_sent = {}       # turn_id -> 已发过的原因，避免一轮刷屏
        self.pushed_docs = {}          # doc_id -> last push ts
        self.core_times = deque()
        self.assist_times = deque()
        self.topic_lat = []
        self.card_lat = []        # 首卡延迟（体感延迟）
        self.card_final_lat = []  # 终卡延迟（主题精修后）
        # 渐进式推送：预取一到先推 top1 粗卡，主题回来后原地升级
        self.progressive = bool(self.policy.get("progressive_push", True))
        self.progressive_min = float(self.policy.get("progressive_min_score", 0.5))
        self.last_topic = None
        self.log = []

    # ---------------- ASR 回调 ----------------
    async def on_partial(self, text, lang, meta=None):
        await self.emit("asr.partial", {"text": text, "lang": lang,
                                        "t": round(time.time(), 3),
                                        "meta": meta or {}})

    async def on_status(self, level, msg):
        await self.emit("status", {"level": level, "message": msg,
                                   "t": round(time.time(), 3)})

    async def on_final(self, text, lang, meta=None):
        t0 = time.perf_counter()
        self.turn_no += 1
        turn_id = "turn-%d" % self.turn_no
        await self.emit("asr.final", {"id": turn_id, "text": text, "lang": lang,
                                      "speaker": (meta or {}).get("speaker", "客户"),
                                      "source": (meta or {}).get("source", "asr"),
                                      "t": round(time.time(), 3)})

        # 两阶段：预取一到就走快通道推粗卡，主题回来后原地升级。
        # st 跨阶段共享：prefetch 结果、已推卡片、本轮已用文档、首卡时间。
        st = {"prefetch": {}, "pushed": {}, "docs": set(), "first_at": None}
        lock = asyncio.Lock()
        prefetch_ready = asyncio.Event()

        async def fast_lane():
            pf = {}
            try:
                pf = await self.mcp.call(self.tool_search,
                                         {"query": text, "top_k": 5, "lang": lang})
            except Exception as e:
                await self.on_status("warn", "预取失败，改走主题后检索：%s" % str(e)[:80])
            try:
                async with lock:
                    st["prefetch"] = pf if isinstance(pf, dict) else {}
                    hits = st["prefetch"].get("hits") or []
                    # 只推 top1 且分数要够高：弱信号话轮不推粗卡，宁可不推也不误导
                    if (self.progressive and hits and
                            float(hits[0].get("score", 0)) >= self.progressive_min):
                        await self._deliver(turn_id, t0, hits[:1], None, True, st)
            finally:
                prefetch_ready.set()

        lane = asyncio.create_task(fast_lane())

        try:
            topic = await self.extractor.extract(text, {"lang": lang})
        except Exception:
            topic = await self.extractor.extract(text, {"lang": lang})

        self.topic_lat.append((time.perf_counter() - t0) * 1000)
        self.last_topic = topic
        await self.emit("topic", {
            "turn_id": turn_id, "lang": topic.get("lang", lang),
            "query": topic.get("query", ""),
            "topics": topic.get("topics", []),
            "intent": topic.get("intent", "general"),
            "provider": topic.get("provider", "rule"),
            "degraded": topic.get("degraded", False),
            "degrade_reason": topic.get("degrade_reason", ""),
            "should_push": topic.get("should_push", True),
            "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
        })
        self.topic_count += 1

        if not topic.get("should_push", True):
            await prefetch_ready.wait()
            await self._silence(turn_id, "寒暄 / 无有效主题，抑制推送")
            await self._emit_stats()
            return

        # 用主题 query 做精检，并与预取结果重排合并。
        # 主题不只是给人看的标签：把它作为 prefer 传给检索，让「模型判定落在哪一篇」
        # 真的影响排序。否则主题识别就只是个装饰。
        query = topic.get("query") or text
        labels = [t.get("label") for t in (topic.get("topics") or []) if t.get("label")]
        refined = {}
        if query.strip() and (labels or query.strip() != text.strip()):
            args = {"query": query, "top_k": 5, "lang": lang}
            if labels:
                args["prefer"] = labels[:3]
            try:
                refined = await asyncio.wait_for(
                    self.mcp.call(self.tool_search, args), 3.0)
            except Exception:
                refined = {}

        await prefetch_ready.wait()
        await lane
        async with lock:
            hits = self._merge(st["prefetch"].get("hits") or [],
                               (refined or {}).get("hits") or [])
            if not hits:
                # 粗卡已出但精检无命中：降级而非删除，不打断销售正在看的内容
                await self._demote_all(turn_id, t0, st, "精检无命中，仅按原文匹配")
                await self._silence(turn_id, "MCP 文档无命中")
                await self._emit_stats()
                return
            await self._deliver(turn_id, t0, hits, topic, False, st)
            await self._emit_stats()

    # ---------------- 渐进式推送 ----------------
    @staticmethod
    def _key(hit: dict) -> str:
        return (hit.get("chunk_id") or hit.get("doc_id")
                or hit.get("title") or "")

    def _make_card(self, turn_id, idx, hit, vis, topic, t0, provisional):
        # 标题只留文档名：「人员与车辆管理 · 段3」对现场没意义，段号进 meta
        raw_title = hit.get("title") or ""
        title = re.split(r"\s*[·・]\s*段\s*\d+\s*$", raw_title)[0].strip() or "相关文档"
        card = {
            "id": "card-%d-%d" % (self.turn_no, idx),
            "turn_id": turn_id, "kind": "hint",
            "title": title,
            # 卡片主行：一句话结论。拿不到就把原文摘录顶上，绝不空着
            "answer": hit.get("answer") or "",
            "snippet": hit.get("snippet") or "",
            "badge": "内部依据" if vis == "internal" else "产品提示",
            "visibility": vis,
            "as_of": hit.get("as_of", "未标注"),
            "source": hit.get("source", ""),
            "score": hit.get("score", 0),
            "topics": [t["label"] for t in (topic or {}).get("topics", [])[:3]],
            "intent": (topic or {}).get("intent", "general"),
            "degraded": bool((topic or {}).get("degraded", False)),
            "ttl": int(self.policy.get("card_ttl_seconds", 45)),
            "provisional": bool(provisional),
            "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
            "t": round(time.time(), 3),
        }
        if vis == "internal":
            card["note"] = "勿对客户表述 · 仅用于你理解判断依据"
        return card

    async def _deliver(self, turn_id, t0, hits, topic, provisional, st):
        """推卡统一出口。provisional=True 推粗卡；False 升级已推的、补新卡、降级落选的。"""
        pushed = st["pushed"]
        max_per_turn = int(self.policy.get("max_hints_per_turn", 2))
        budget = float(self.policy.get("min_confidence", 0.55))
        intent = (topic or {}).get("intent", "general")
        keep = set()

        for hit in hits:
            if len(keep) >= max_per_turn:
                break
            vis = hit.get("visibility", "internal")
            if vis == "restricted":
                await self._silence(turn_id, "restricted 内容不推送：%s" % hit.get("title", ""))
                continue
            if vis not in ("public", "internal"):
                vis = "internal"

            key = self._key(hit)
            if key and key in pushed:
                keep.add(key)
                if not provisional:
                    pushed[key] = await self._replace(
                        turn_id, t0, pushed[key], hit=hit, topic=topic)
                continue

            ok, reason = self._gate(hit, vis, budget, intent, st["docs"])
            if not ok:
                await self._silence(turn_id, reason)
                continue

            card = self._make_card(turn_id, len(pushed) + 1, hit, vis, topic,
                                   t0, provisional)
            await self.emit("hint", card)
            self.card_count += 1
            pushed[key] = card
            keep.add(key)
            if st["first_at"] is None:
                st["first_at"] = round((time.perf_counter() - t0) * 1000, 1)
                self.card_lat.append(st["first_at"])

        if not provisional:
            for key, card in list(pushed.items()):
                if key not in keep and card.get("provisional"):
                    pushed[key] = await self._replace(
                        turn_id, t0, card, demote=True,
                        note="已被更贴主题的依据取代，仅作参考")

    async def _replace(self, turn_id, t0, card, hit=None, topic=None,
                       demote=False, note=""):
        """原地替换一张已推卡片（升级或降级），前端按 id 覆盖，不新增卡。"""
        new = dict(card)
        new["kind"] = "hint.update"     # payload 自描述，别让消费方靠猜字段
        new["provisional"] = False
        new["update"] = True
        new["demote"] = bool(demote)
        if hit is not None:
            new["score"] = round(max(float(card.get("score", 0)),
                                     float(hit.get("score", 0))), 3)
            if hit.get("snippet"):
                new["snippet"] = hit["snippet"]
            if hit.get("answer"):
                new["answer"] = hit["answer"]
        if topic:
            new["topics"] = [t["label"] for t in topic.get("topics", [])[:3]]
            new["intent"] = topic.get("intent", card.get("intent", "general"))
            new["degraded"] = bool(topic.get("degraded", False))
        if note:
            new["note"] = note
        new["refine_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        new["t"] = round(time.time(), 3)
        await self.emit("hint.update", new)
        if not demote:
            self.card_final_lat.append(new["refine_ms"])
        return new

    async def _demote_all(self, turn_id, t0, st, note):
        for key, card in list(st["pushed"].items()):
            if card.get("provisional"):
                st["pushed"][key] = await self._replace(
                    turn_id, t0, card, demote=True, note=note)

    # ---------------- 合并 / 抑制 ----------------
    def _merge(self, prefetch, refined):
        merged = {}
        for rank, hit in enumerate(prefetch):
            key = hit.get("chunk_id") or hit.get("doc_id") or hit.get("title")
            if key not in merged:
                merged[key] = dict(hit)
                merged[key]["_score"] = float(hit.get("score", 0)) * 1.0
                merged[key]["_both"] = False
        for hit in refined:
            key = hit.get("chunk_id") or hit.get("doc_id") or hit.get("title")
            if key in merged:
                merged[key]["_score"] += float(hit.get("score", 0)) * 1.5
                merged[key]["_both"] = True
                # 精检片段更贴主题时替换 snippet
                if float(hit.get("score", 0)) > float(merged[key].get("score", 0)) + 0.25:
                    merged[key]["snippet"] = hit.get("snippet", merged[key]["snippet"])
            else:
                d = dict(hit)
                d["_score"] = float(hit.get("score", 0)) * 1.2
                d["_both"] = True
                merged[key] = d
        out = []
        for d in merged.values():
            d["score"] = round(min(1.0, (d.pop("_score") / 2.5) + (0.12 if d.pop("_both") else 0)), 3)
            out.append(d)
        out.sort(key=lambda x: -float(x.get("score", 0)))
        return out

    def _gate(self, hit, vis, budget, intent="general", this_turn=None):
        """去重按「片段 + 意图」：同一段落换了提问角度仍可再推，
        避免用 doc_id 粗粒度把客户主动追问的判断依据卡全挤掉。"""
        now = time.time()
        this_turn = this_turn if this_turn is not None else set()
        doc_id = hit.get("doc_id") or ""
        chunk_id = hit.get("chunk_id") or hit.get("doc_id") or hit.get("title", "")
        if doc_id and doc_id in this_turn:
            return False, "同一文档本轮已推送（合并展示）"
        key = "%s|%s" % (chunk_id, intent)
        ttl = float(self.policy.get("dedupe_seconds", 90))
        last = self.pushed_docs.get(key, 0)
        if now - last < ttl:
            return False, "同一片段同意图 %ds 内已推送（去重）" % int(ttl)
        # 没踩到领域词（词表里没有这个概念）→ 判为闲聊或词表外问法，不推。
        # 分数可以靠覆盖率凑上去，但「这句话在问我们的业务吗」只有词表能回答。
        if hit.get("strong") is False:
            return False, "无对应知识（未命中领域词）"
        score = float(hit.get("score", 0))
        is_core = vis == "public" and score >= budget
        # 辅助卡也要有底线：低于这条线的命中基本是词面上蹭到的（0.2 分那种），
        # 推出去就是在赌销售不会看内容只看标题
        floor = float(self.policy.get("assist_min_score", budget * 0.65))
        if score < floor:
            return False, "匹配度过低（%.2f < %.2f），不推送" % (score, floor)
        q = self.core_times if is_core else self.assist_times
        limit = int(self.policy.get("max_cards_per_10min", 6) if is_core
                    else self.policy.get("max_assist_per_10min", 3))
        while q and now - q[0] > 600:
            q.popleft()
        if len(q) >= limit:
            return False, ("主卡" if is_core else "辅助卡") + " 10 分钟额度用尽（节流）"
        q.append(now)
        self.pushed_docs[key] = now
        if doc_id:
            this_turn.add(doc_id)
        return True, "通过"

    async def _silence(self, turn_id, reason):
        key = reason.split("：")[0]
        sent = self._silence_sent.setdefault(turn_id, set())
        # 一个话轮会拿多条候选依次判，同一类原因会重复出现；
        # 不在这一层去重，前端就会连着弹好几条一模一样的提示。
        if key in sent:
            return
        sent.add(key)
        if len(self._silence_sent) > 64:      # 只留最近若干轮，别让字典长胖
            for k in list(self._silence_sent)[:-32]:
                self._silence_sent.pop(k, None)
        self.silenced[key] += 1
        await self.emit("silenced", {"turn_id": turn_id, "reason": reason})

    async def _emit_stats(self):
        await self.emit("stats", self.stats())

    def stats(self):
        return {
            "turns": self.turn_no, "topics": self.topic_count,
            "cards": self.card_count, "silenced": sum(self.silenced.values()),
            "silence_reasons": dict(self.silenced),
            "topic_p50_ms": _pct(self.topic_lat, 0.5),
            "topic_p95_ms": _pct(self.topic_lat, 0.95),
            "card_p50_ms": _pct(self.card_lat, 0.5),
            "card_p95_ms": _pct(self.card_lat, 0.95),
            "card_final_p95_ms": _pct(self.card_final_lat, 0.95),
            "progressive": self.progressive,
            "core": len(self.core_times), "assist": len(self.assist_times),
            "extractor": __import__("wma_agent").extractor_info(self.extractor),
            "last_topic": (self.last_topic or {}).get("query", ""),
        }

    def reset(self):
        self.turn_no = self.topic_count = self.card_count = 0
        self.silenced.clear()
        self._silence_sent.clear()
        self.pushed_docs.clear()
        self.core_times.clear()
        self.assist_times.clear()
        self.topic_lat.clear()
        self.card_lat.clear()
        self.card_final_lat.clear()
        self.last_topic = None
