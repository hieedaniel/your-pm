"""perf MCP 语义适配层：把「客户一句话」变成 需求 / 问题单 / 项目 的检索，再折算成提示卡。

为什么要这一层
--------------
perf 系统的 25 个工具是**业务 CRUD**（按标题模糊查需求、按状态查问题单…），
而实时链路需要的是 `docs.search(query, top_k)` 这种**语义检索**。中间必须有一层
把「客户问了什么」翻译成「查哪张表、拿什么关键词、命中怎么算分、给销售看什么」。

三条设计约束（沿用报告 v1.2 的立场）
--------------------------------
1. 不让大模型决定调哪个工具：关键词用本地规则抽，工具固定三个，参数固定，
   全程确定性 —— 这是「MCP without an agent」的落地。
2. 内部研发信息默认 internal（勿对客户表述），不因为「查到了」就对外说。
   只有**已交付的项目**才升 public；restricted 整卡不推（编排器负责拦）。
3. 检索失败一律返回空 hits，绝不伪造命中：宁可这一轮不推卡，也不给销售假依据。

延迟：3 类 × 2 关键词 = 最多 6 个并发 tools/call，同城 HTTPS 约 300–700ms。
"""
from __future__ import annotations

import asyncio
import re
import time

# 中文高频虚词/疑问词：参与检索只会让 SQL LIKE 命中一堆噪音
STOP_CHARS = set("的了吗呢吧啊呀嘛么是在和与跟或也都就很更最太真还挺把被给从向往为这那个"
                 "些什么怎如何哪谁几多少能不能可可以要想说讲问题请问一下我们你们他们")
# 注：故意不把「对」列进来 —— 它是虚词，但也是「方案对比」「对比功能」的核心词，
# 一刀切会把术语打碎成「方案」，反而搜出一堆不相关的问题单。
STOP_BIGRAMS = {"能不", "不能", "可以", "可可", "多少", "什么", "怎么", "这个", "那个", "我们",
                "你们", "他们", "一下", "请问", "问题", "支持", "有没", "没有", "是不", "不是",
                "现在", "目前", "时候", "时间", "客户", "客户问", "报价", "价格", "折扣", "多少价",
                "太贵", "贵了", "打折", "优惠", "怎么卖", "卖多少", "要多久", "多久"}
# 英文泛词：在标题里到处都是，当主关键词等于没检索
EN_STOP = {"bug", "ok", "app", "api", "the", "and", "yes", "no", "hi", "hello", "demo",
           "test", "new", "old", "fix", "issue", "ui", "id", "sp", "pc"}

TYPE_LABEL = {"requirement": "需求", "issue": "问题单", "project": "项目"}
STATUS_CN = {
    "pending": "待处理", "assigned": "已分配", "fixing": "修复中", "testing": "测试中",
    "completed": "已完成", "closed": "已关闭", "executing": "进行中", "planning": "规划中",
    "released": "已发布", "rejected": "已驳回", "cancelled": "已取消", "done": "已完成",
    "in_progress": "进行中", "open": "待处理", "resolved": "已解决", "verified": "已验证",
}
SEVERITY_CN = {"fatal": "致命", "critical": "严重", "major": "主要", "minor": "次要",
               "trivial": "轻微", "blocker": "阻塞", "normal": "一般", "high": "高",
               "medium": "中", "low": "低"}
PRIORITY_CN = {"high": "高", "medium": "中", "low": "低", "urgent": "紧急"}
DIFFICULTY_CN = {"simple": "简单", "medium": "中等", "complex": "复杂"}


def _cn(v: str) -> str:
    v = (v or "").strip()
    return STATUS_CN.get(v.lower(), v) or "—"


def keywords(text: str, limit: int = 2):
    """抽检索关键词：型号/英文优先，其次整段实词，最后 2-gram。零依赖。

    过滤原则：**只要有一个字是虚词就丢弃这个 2-gram**。
    不这么做会抽出「户问」「垫的」「个报」这类碎片，SQL LIKE 一查就是噪音命中，
    再配上计分权重就会推出不相关的卡 —— 在销售场景里，推错比不推更糟。
    """
    text = (text or "").strip()
    if not text:
        return []
    out, seen = [], set()

    def push(w, keeps=True):
        w = w.strip()
        if len(w) < 2 or w.lower() in seen or w.lower() in EN_STOP:
            return
        for s in seen:
            if w.lower() in s:      # 已被更长的候选包含（「面弓」已收，「子面」不要）
                return
        seen.add(w.lower())
        out.append(w)

    # 1) 型号 / 英文 / 版本号：「Nexjaw」「A200」「V3.0.1」命中率最高，排最前
    for seg in re.findall(r"[A-Za-z][A-Za-z0-9\.\-_]{1,20}", text):
        if not re.fullmatch(r"[0-9\.]+", seg):
            push(seg)

    # 2) 中文候选：整段 2–4 字实词优先，长句切 2-gram 兜底（任一虚字即弃）。
    #    排序用「长词优先」：客户说「方案对比」时，别让「对比」这种泛词抢了主词位。
    cjk, pos = [], 0
    for seg in re.findall(r"[\u4e00-\u9fff]+", text):
        if 2 <= len(seg) <= 4 and not any(c in STOP_CHARS for c in seg):
            cjk.append((pos, seg))
        elif len(seg) > 4:
            # 长句开头 4 字：「方案对比只对下颌对比」→「方案对比」。
            # 中文复合术语多在前缀，且 4 字比 2 字更能压住泛词的噪音。
            head = seg[:4]
            if not any(c in STOP_CHARS for c in head):
                cjk.append((pos, head))
            for i in range(len(seg) - 1):
                g = seg[i:i + 2]
                if g in STOP_BIGRAMS or any(c in STOP_CHARS for c in g):
                    continue
                cjk.append((pos + i, g))
        pos += len(seg)
    for _, w in sorted(cjk, key=lambda x: (-len(x[1]), x[0])):
        push(w)
    return out[:limit]


class PerfSearchProxy:
    """对编排器暴露 docs.search / docs.get 契约，内部翻译成 perf 的 tools/call。"""

    name = "perf"

    def __init__(self, client, cfg: dict | None = None, vocab: dict | None = None):
        self.client = client
        self.cfg = cfg or {}
        self.types = self.cfg.get("types") or ["requirement", "issue", "project"]
        self.max_kw = int(self.cfg.get("max_keywords", 2))
        self.top_k = int(self.cfg.get("top_k", 5))
        self.include_completed = bool(self.cfg.get("include_completed", True))
        self.promote_completed = bool(self.cfg.get("promote_completed", False))
        self.cache_ttl = float(self.cfg.get("cache_seconds", 120))
        self.timeout = float(self.cfg.get("timeout_ms", 2500)) / 1000.0
        # 实测：单项 311ms，3 类并发 592ms，6 并发 996ms（服务端会排队）。
        # 给整轮检索一个硬预算，超时就用已回来的结果，不为了完整性把体感拖到 1s 以上。
        self.deadline = float(self.cfg.get("deadline_ms", 900)) / 1000.0
        self.require_primary = bool(self.cfg.get("require_primary", True))
        self.generic_total = int(self.cfg.get("generic_total", 30))
        self.partial_count = 0
        self.vocab = vocab or {}
        self._cache = {}
        self.calls = 0
        self.errors = 0
        self.err_log = []      # 最近几条失败，排错时不用翻服务端日志
        self.lat = []

    def _note_err(self, msg):
        self.err_log.append("%s %s" % (time.strftime("%H:%M:%S"), msg))
        del self.err_log[:-8]

    # ---------------- 对外契约 ----------------
    async def call(self, name: str, args: dict) -> dict:
        args = args or {}
        if "search" in name:
            return await self.search(args.get("query", ""), int(args.get("top_k") or self.top_k))
        if "get" in name:
            return await self.get(args.get("doc_id", ""))
        if "list" in name:
            return {"docs": await self.listing()}
        return {"error": "perf 适配层不支持的工具：%s" % name}

    async def search(self, query: str, top_k: int = 5) -> dict:
        t0 = time.perf_counter()
        kws = keywords(query, self.max_kw)
        if not kws:
            self.lat.append(0.0)
            return {"hits": [], "keywords": [], "note": "无可检索关键词（疑问词/虚词话轮不查）"}

        # 同一关键词 2 分钟内复用，连续追问不重复打网络
        now = time.time()
        tasks, pending = [], []
        for kw in kws:
            for typ in self.types:
                ck = (typ, kw.lower())
                hit = self._cache.get(ck)
                if hit and now - hit[0] < self.cache_ttl:
                    tasks.append(asyncio.sleep(0, result=hit[1]))
                else:
                    tasks.append(asyncio.sleep(0, result=None))
                    pending.append((ck, kw, typ))
        results = await asyncio.gather(*tasks)     # 缓存命中项：瞬时返回
        partial = False
        if pending:
            futs = [asyncio.ensure_future(self._fetch(kw, typ)) for _, kw, typ in pending]
            done, todo = await asyncio.wait(futs, timeout=self.deadline)
            for f in todo:
                f.cancel()
            if todo:
                # 超预算的不再等：宁可用已回来的部分结果，也不把粗卡拖到 1s 以上
                partial = True
                self.partial_count += 1
            for f, (ck, kw, typ) in zip(futs, pending):
                if f.done() and not f.cancelled() and f.exception() is None:
                    got = f.result() or ([], 0)
                    self._cache[ck] = (now, got)
                else:
                    got = ([], 0)    # 超时不写缓存，避免把「没查完」当成「没命中」
                results.append(got)

        self.calls += 1
        hits = self._score(results, kws, query, top_k)
        self.lat.append((time.perf_counter() - t0) * 1000)
        return {"hits": hits, "keywords": kws, "source": "perf", "partial": partial,
                "latency_ms": round(self.lat[-1], 1)}

    async def get(self, doc_id: str) -> dict:
        """perf:req:31 / perf:issue:699 / perf:proj:3 → 详情。"""
        parts = str(doc_id).split(":")
        if len(parts) != 3 or parts[0] != "perf":
            return {"error": "doc_id 形如 perf:req:31"}
        kind, rid = parts[1], parts[2]
        tool = {"req": ("get_requirement", "requirement_id"),
                "issue": ("get_issue", "issue_id"),
                "proj": ("get_project", "project_id")}.get(kind)
        if not tool:
            return {"error": "未知类型 %s" % kind}
        try:
            data = await asyncio.wait_for(
                self.client.call(tool[0], {tool[1]: int(rid)}), self.timeout)
        except Exception as e:
            return {"error": "%s: %s" % (type(e).__name__, e)}
        return (data or {}).get("data") or data

    async def listing(self):
        try:
            tools = await asyncio.wait_for(self.client.list_tools(), self.timeout)
        except Exception:
            return []
        return [{"doc_id": "tool:" + t.get("name", ""), "title": t.get("name", ""),
                 "visibility": "internal", "as_of": "实时",
                 "source": "perf MCP", "snippet": (t.get("description") or "")[:80]}
                for t in tools]

    def stats(self):
        out = {"adapter": "perf", "calls": self.calls, "errors": self.errors,
               "avg_ms": round(sum(self.lat) / len(self.lat), 1) if self.lat else 0.0,
               "p95_ms": round(sorted(self.lat)[min(len(self.lat) - 1, int(len(self.lat) * 0.95))], 1)
               if self.lat else 0.0,
               "partial": self.partial_count,
               "cache_entries": len(self._cache)}
        if self.err_log:
            out["last_errors"] = self.err_log[-3:]
        return out

    # ---------------- 内部 ----------------
    async def _fetch(self, kw: str, typ: str):
        """返回 (items, total)。total 是服务端命中总数，用来识别「泛词」。"""
        tool, params = {
            "requirement": ("search_requirements", {"title": kw, "page": 1, "page_size": 5}),
            "issue": ("search_issues", {"title": kw, "page": 1, "page_size": 5}),
            "project": ("search_projects", {"keyword": kw, "page": 1, "page_size": 5}),
        }[typ]
        try:
            data = await asyncio.wait_for(self.client.call(tool, params), self.timeout)
        except Exception as e:
            self.errors += 1
            self._note_err("%s:%s %s" % (tool, kw, type(e).__name__))
            return ([], 0)
        if not isinstance(data, dict) or data.get("error"):
            self.errors += 1
            self._note_err("%s:%s %s" % (tool, kw, str((data or {}).get("error"))[:80]))
            return ([], 0)
        box = data.get("data") or {}
        items = box.get("items") or box.get("list") or []
        return ([dict(it, _type=typ, _kw=kw) for it in items if isinstance(it, dict)],
                int(box.get("total") or len(items)))

    def _score(self, groups, kws, query, top_k):
        n = max(1, len(kws))
        # 主关键词权重最高：它是客户话里最具体的那个词。
        # 只命中次要词（往往是长句切出来的碎片）不算强证据，默认直接淘汰。
        wts = [1.0] + [0.55] * (n - 1)
        wsum = sum(wts[:n]) or 1.0
        merged = {}
        for group in groups:
            items, total = group if isinstance(group, tuple) else (group, 0)
            # 命中总数过大 = 这个词太泛（「版本」「功能」），同分不同质，压低权重
            generic = total > self.generic_total
            for it in items or []:
                typ = it.get("_type") or "requirement"
                rid = it.get("id")
                title = str(it.get("title") or it.get("name") or "").strip()
                desc = str(it.get("description") or "")
                status = str(it.get("status") or "").lower()
                if not title:
                    continue
                if not self.include_completed and status in ("completed", "closed", "released"):
                    continue
                # 命中质量：标题命中 > 描述命中
                matched = {}
                field_w = 0.0
                low_title, low_desc = title.lower(), desc.lower()
                for i, kw in enumerate(kws):
                    k = kw.lower()
                    if k in low_title:
                        matched[i] = True
                        field_w = max(field_w, 1.0)
                    elif k in low_desc:
                        matched[i] = True
                        field_w = max(field_w, 0.6)
                if not matched:
                    continue
                if self.require_primary and 0 not in matched:
                    continue
                key = "perf:%s:%s" % ({"requirement": "req", "issue": "issue",
                                       "project": "proj"}[typ], rid)
                rec = self._recency(it)
                cov = sum(wts[i] for i in matched) / wsum
                # 关键词越长越具体：「2 字泛词」和「型号」不该同分
                spec = min(1.0, 0.55 + 0.11 * max(len(kws[i]) for i in matched))
                score = spec * (0.58 * cov + 0.24 * field_w) + 0.18 * rec
                if generic:
                    score *= 0.78
                # 整句出现在标题里（客户几乎是原话）再加一点，避免被泛词拉平
                if query and query.strip().lower() in low_title:
                    score += 0.08
                prev = merged.get(key)
                if prev and prev["score"] >= score:
                    continue
                merged[key] = {
                    "doc_id": key, "chunk_id": key + "#" + typ,
                    "title": self._title(typ, it, title),
                    "snippet": self._snippet(typ, it),
                    "score": round(min(1.0, score), 3),
                    "visibility": self._visibility(typ, status),
                    "as_of": self._as_of(it),
                    "source": "绩效系统 · " + TYPE_LABEL[typ],
                    "type": typ, "status": status, "match_total": total,
                    "keywords": [kws[i] for i in sorted(matched)],
                }
        hits = sorted(merged.values(), key=lambda x: -x["score"])
        return hits[:top_k]

    def _title(self, typ, it, title):
        if typ == "requirement":
            return "%s %s" % (it.get("requirement_no") or "REQ", title)
        if typ == "issue":
            return "%s %s" % (it.get("issue_no") or "BUG", title) \
                if it.get("issue_no") else title
        return "%s %s" % (it.get("project_no") or "P", title) \
            if it.get("project_no") else title

    def _snippet(self, typ, it):
        if typ == "requirement":
            bits = ["需求", "状态 " + _cn(it.get("status")),
                    "进度 %s%%" % (it.get("progress_percent") or 0)]
            if it.get("difficulty"):
                bits.append("难度 " + DIFFICULTY_CN.get(str(it["difficulty"]).lower(),
                                                       str(it["difficulty"])))
            if it.get("priority"):
                bits.append("优先级 " + PRIORITY_CN.get(str(it["priority"]).lower(),
                                                       str(it["priority"])))
            if it.get("owner_name"):
                bits.append("责任人 " + str(it["owner_name"]))
            if it.get("planned_completion_at"):
                bits.append("计划 " + str(it["planned_completion_at"])[:10])
            body = _clean(it.get("description"))
            return " · ".join(bits) + (" ｜ " + body if body else "")
        if typ == "issue":
            bits = ["问题单", "状态 " + _cn(it.get("status"))]
            if it.get("severity"):
                bits.append("严重度 " + SEVERITY_CN.get(str(it["severity"]).lower(),
                                                       str(it["severity"])))
            if it.get("issue_version"):
                bits.append("发现于 " + str(it["issue_version"]))
            if it.get("assignee_name"):
                bits.append("修复人 " + str(it["assignee_name"]))
            elif it.get("pl_name"):
                bits.append("PL " + str(it["pl_name"]))
            return " · ".join(bits)
        bits = ["项目", "状态 " + _cn(it.get("status")),
                "进度 %s%%" % (it.get("progress") or 0)]
        if it.get("plan_start_date") and it.get("plan_end_date"):
            bits.append("计划 %s→%s" % (str(it["plan_start_date"])[:10],
                                       str(it["plan_end_date"])[:10]))
        if (it.get("owner") or {}).get("real_name"):
            bits.append("负责人 " + it["owner"]["real_name"])
        if it.get("requirement_count") is not None:
            bits.append("需求 %s / 问题 %s" % (it.get("requirement_count"), it.get("issue_count")))
        return " · ".join(bits)

    def _visibility(self, typ, status):
        """只有「已交付」的事实可以对外说；研发过程一律 internal。"""
        if typ == "project" and status in ("completed", "released", "done"):
            return "public"
        if typ == "requirement" and self.promote_completed and status in ("completed", "released", "done"):
            return "public"
        return "internal"

    @staticmethod
    def _as_of(it):
        for k in ("updated_at", "created_at", "completed_at", "plan_end_date", "actual_end_date"):
            v = it.get(k)
            if v:
                return str(v)[:10]
        return "未标注"

    @staticmethod
    def _recency(it):
        """半年内线性衰减，老需求仍然可查但不抢新需求的位置。"""
        raw = str(it.get("updated_at") or it.get("created_at") or "")
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", raw)
        if not m:
            return 0.5
        try:
            import datetime
            d = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            days = max(0, (datetime.date.today() - d).days)
        except Exception:
            return 0.5
        return max(0.2, 1.0 - days / 180.0)


def _clean(s, limit=110):
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return (s[:limit] + "…") if len(s) > limit else s
