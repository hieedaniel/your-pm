"""内置最小 MCP 文档服务（前期用你提供的文档）。

实现 MCP 的两个核心方法：tools/list 与 tools/call，工具：
    docs.search(query, top_k, lang?)  → 命中片段
    docs.get(doc_id)                 → 完整正文
    docs.list()                      → 文档清单（控制面：建索引 / 生成热词）

文档放在 docs/*.md，front-matter 支持：
    ---
    title: ...
    visibility: public | internal | restricted
    as_of: 2026-11-07
    tags: 逗号分隔
    ---
字段缺失一律按 internal 兜底（沿用 v1.2 的信息分级策略）。
检索用轻量 BM25：中文按 2-gram、英文按词，零第三方依赖。
"""
from __future__ import annotations

import math
import os
import re
from collections import Counter

VISIBILITY_LEVEL = {"public": 0, "internal": 1, "restricted": 2}

# 查询侧的虚词/套话 2-gram。中文按 bigram 切会产生跨词碎片（「能不」「能分」「限能」
# 「算有」），这些碎片在库里只出现一两次，idf 反而很高，会把「预算有限能不能分期」
# 这种没有答案的话轮顶到 0.7 分并推到一张无关的卡上。
# 只作用于查询侧，索引照旧保留，不影响 BM25 统计。
STOP_BIGRAMS = {
    "能不", "不能", "可以", "可可", "能分", "限能", "算有", "能打", "间能", "统之",
    "你们", "我们", "他们", "们公", "司在", "这些", "那些", "些系", "这个", "那个",
    "什么", "怎么", "如何", "多少", "哪个", "哪儿", "在哪", "是不是", "是不", "有没",
    "没有", "还要", "来了", "就是", "然后", "现在", "目前", "时候", "一下", "请问",
    # 企业名与工商套话：几乎每篇案例都有，属于零信息词
    "有限", "公司", "股份", "集团", "总部",
}


def tokenize(text: str):
    out = []
    for seg in re.findall(r"[\u4e00-\u9fff]+|[a-zA-Z0-9]+", (text or "").lower()):
        if "\u4e00" <= seg[0] <= "\u9fff":
            if len(seg) == 1:
                out.append(seg)
            else:
                out.extend(seg[i:i + 2] for i in range(len(seg) - 1))
        else:
            out.append(seg)
    return out


def parse_front_matter(raw: str):
    meta, body = {}, raw
    if raw.startswith("---"):
        end = raw.find("\n---", 3)
        if end > 0:
            block = raw[3:end].strip()
            for line in block.splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip().lower()] = v.strip().strip('"').strip("'")
            body = raw[end + 4:].lstrip("\n")
    return meta, body


class DocStore:
    def __init__(self, docs_dir):
        # 支持多个目录：内置 docs-kaiyi（只读）+ data/kb（管理页上传的）。
        # 两者格式完全一致（front-matter + markdown），所以不需要第二套索引。
        if isinstance(docs_dir, (list, tuple)):
            self.docs_dirs = [d for d in docs_dir if d]
        else:
            self.docs_dirs = [docs_dir]
        self.docs_dir = self.docs_dirs[0] if self.docs_dirs else ""
        self.docs = {}
        self.chunks = []
        self.df = Counter()
        self.avgdl = 1.0
        self.load()

    def load(self):
        self.docs.clear()
        self.chunks.clear()
        self.df.clear()
        for d in self.docs_dirs:
            if not os.path.isdir(d):
                os.makedirs(d, exist_ok=True)
        for d in self.docs_dirs:
            for name in sorted(os.listdir(d)):
                if not name.lower().endswith((".md", ".markdown", ".txt")):
                    continue
                if name in self.docs:      # 同名文档：先加载的目录优先，避免后一份覆盖内置
                    print("[docs] 跳过同名文档：%s（%s）" % (name, d))
                    continue
                path = os.path.join(d, name)
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        raw = f.read()
                except OSError:
                    continue
                self._add_doc(name, raw)
        self._index()

    def _add_doc(self, name: str, raw: str):
        meta, body = parse_front_matter(raw)
        vis = meta.get("visibility", "internal").lower()
        if vis not in VISIBILITY_LEVEL:
            vis = "internal"
        doc = {
            "doc_id": name,
            "title": meta.get("title") or os.path.splitext(name)[0],
            "visibility": vis,
            "as_of": meta.get("as_of", "未标注"),
            "tags": [t.strip() for t in meta.get("tags", "").split(",") if t.strip()],
            # 客户实际会怎么说。只参与检索加权，不进 ASR 热词（「订了不用」当热词是噪音）
            "say": [t.strip() for t in meta.get("say", "").split(",") if t.strip()],
            # 卡片上的「出处」优先用 front-matter 声明（如 PPT 页码），否则退回文件名
            "source": meta.get("source") or ("docs/" + name),
            # 一句话结论：卡片主行。用户要的是「怎么答」，不是「原文在第几段」
            "answer": (meta.get("answer") or "").strip(),
            "body": body,
        }
        self.docs[name] = doc
        # 按标题切块，段落过长再切
        sections = re.split(r"(?m)^#{1,3}\s+", body)
        for idx, sec in enumerate(sections):
            sec = sec.strip()
            if not sec:
                continue
            for j in range(0, len(sec), 420):
                piece = sec[j:j + 420]
                if len(piece) < 12:
                    continue
                label = "%s · 段%d" % (doc["title"], idx + 1)
                toks = tokenize(piece + " " + doc["title"] + " "
                                + " ".join(doc["tags"]) + " " + " ".join(doc["say"]))
                self.chunks.append({
                    "chunk_id": "%s#%d.%d" % (name, idx, j),
                    "doc_id": name, "title": label, "text": piece,
                    "tokens": toks, "length": max(1, len(toks)),
                    "visibility": vis, "as_of": doc["as_of"], "source": doc["source"],
                    "tags": doc["tags"], "say": doc["say"], "answer": doc["answer"],
                })

    def _index(self):
        """DF 与平均长度必须在所有文档加载完之后算一次，不能边加边算。"""
        self.df.clear()
        for c in self.chunks:
            self.df.update(set(c["tokens"]))
        self.avgdl = (sum(c["length"] for c in self.chunks) / len(self.chunks)
                      if self.chunks else 1.0)

    def search(self, query: str, top_k: int = 4, prefer=None):
        q = [t for t in tokenize(query) if t not in STOP_BIGRAMS]
        if not q:
            return []
        # 主题定向：模型已判定这句话落在「人员与车辆管理」，那这一篇的片段
        # 就该比同分片段靠前。只加偏置不改排序成本——主题判错时最坏退化成
        # 「和原来一样」，不会把无关文档硬顶上来（因为仍要求真命中库内词）。
        prefer_keys = [p for p in (prefer or []) if p]
        qc = Counter(q)
        qterms = set(qc)
        n = max(1, len(self.chunks))

        def idf(term):
            return math.log(1 + (n - self.df.get(term, 0) + 0.5) /
                           (self.df.get(term, 0) + 0.5))

        # idf 加权后的查询总量。分母只算「库里真有的词」——客户口语（「抄表」）
        # 与方案用词（「采集监测」）天然不同，把不在库里的词计入分母会把真命中一起压死。
        # 高频虚词（「能不」「这个」）idf 本来就低，也撑不起覆盖率。
        known = [t for t in qterms if self.df.get(t, 0) > 0]
        qdenom = sum(idf(t) for t in known) or 1.0
        scored = []
        for c in self.chunks:
            tf = Counter(c["tokens"])
            score = 0.0
            for term, qn in qc.items():
                f = tf.get(term, 0)
                if not f:
                    continue
                score += idf(term) * (f * 2.2) / (f + 1.2 * (0.25 + 0.75 * c["length"] / self.avgdl)) * min(qn, 2)
            # 领域词命中：tags/say 是人工维护的概念词表，命中一个才算「这话在问我们的业务」。
            # 纯 BM25 命中（「对比」「一块」这种）不足以出卡——宁可沉默，也别推一张假卡。
            raw = query.lower()
            strong = False
            for t in list(c["tags"]) + list(c.get("say") or []):
                if t and len(t) >= 2 and t.lower() in raw:
                    score += 2.0
                    strong = True
            if score > 0:
                present = qterms & set(tf)
                cov = sum(idf(t) for t in present) / qdenom
                via = bool(prefer_keys) and any(
                    k in c["title"] or c["title"] in k for k in prefer_keys)
                scored.append((score, cov, c, strong, via))
        if not scored:
            return []
        scored.sort(key=lambda x: -x[0])
        top = scored[:max(1, min(int(top_k or 4), 8))]
        mx = top[0][0] or 1.0
        out = []
        for score, cov, c, strong, via in top:
            # 只用「相对分」会让 top1 永远等于 1.0 —— 虚词命中一句无关话也被判满分。
            # 掺入 idf 加权的查询覆盖率后，分数才有绝对含义，min_confidence 才能真正拦住跑题。
            final = 0.45 * (score / mx) + 0.55 * cov
            # 没命中任何领域词 → 判定为「不是业务问题」（寒暄、销售自己的口头语），
            # 直接压到门槛以下。代价是词表外的问法会漏，但这比推错卡便宜。
            if not strong:
                final *= 0.45
            if via:
                # 主题已由模型定向到这一篇，给一个小偏置让它排到前面。
                # 幅度刻意不大：主题判错时最多把同分区里的顺序搅乱，不会造出假命中。
                final = min(1.0, final + 0.10)
            snippet = re.sub(r"\s+", " ", c["text"]).strip()[:110]
            out.append({
                "doc_id": c["doc_id"], "chunk_id": c["chunk_id"],
                "title": c["title"], "snippet": snippet,
                "answer": c.get("answer") or "",
                "score": round(min(1.0, final), 3),
                "coverage": round(cov, 3),
                "strong": bool(strong),
                "via_topic": bool(via),
                "visibility": c["visibility"], "as_of": c["as_of"],
                "source": c["source"], "tags": c["tags"],
            })
        out.sort(key=lambda x: -x["score"])
        return out

    def get(self, doc_id: str):
        d = self.docs.get(doc_id)
        if not d:
            return {"error": "doc not found: %s" % doc_id}
        return {k: v for k, v in d.items() if k != "body"} | {"content": d["body"]}

    def listing(self):
        return [{"doc_id": d["doc_id"], "title": d["title"],
                 "visibility": d["visibility"], "as_of": d["as_of"],
                 "tags": d["tags"], "source": d["source"]} for d in self.docs.values()]

    def hotwords(self, limit=20):
        words = []
        for d in self.docs.values():
            words.extend(d["tags"])
            words.append(re.sub(r"\s*[·(（].*$", "", d["title"]).strip())
        seen, out = set(), []
        for w in words:
            if w and w not in seen and 2 <= len(w) <= 14:
                seen.add(w)
                out.append(w)
            if len(out) >= limit:
                break
        return out


TOOLS = [
    {
        "name": "docs.search",
        "description": "按主题关键词检索产品/方案/技术依据文档片段，返回带 visibility 与 as_of 的结构化命中",
        "inputSchema": {"type": "object", "properties": {
            "query": {"type": "string", "description": "检索串，空格分隔关键词"},
            "top_k": {"type": "integer", "description": "返回条数，默认 4"},
            "prefer": {"type": "array", "items": {"type": "string"},
                       "description": "已判定的主题名（材料主题表里的 label），命中该主题的文档排前"},
            "lang": {"type": "string", "description": "原始话轮语种，仅作记录"}},
            "required": ["query"]},
    },
    {
        "name": "docs.get",
        "description": "按 doc_id 取回文档全文",
        "inputSchema": {"type": "object", "properties": {
            "doc_id": {"type": "string"}}, "required": ["doc_id"]},
    },
    {
        "name": "docs.list",
        "description": "列出全部文档及可见级别、更新时间、标签（控制面用）",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


class LocalMcpServer:
    """进程内 MCP server：tools/list + tools/call，与真实 MCP 返回结构同构。"""

    def __init__(self, docs_dir: str):
        self.store = DocStore(docs_dir)

    def handle(self, req: dict):
        method, params = req.get("method"), req.get("params") or {}
        rid = req.get("id")
        if method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            name, args = params.get("name"), params.get("arguments") or {}
            result = self.call(name, args)
        elif method == "initialize":
            result = {"protocolVersion": "2024-11-05",
                      "capabilities": {"tools": {}},
                      "serverInfo": {"name": "qieshen-docs", "version": "1.0"}}
        else:
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32601, "message": "method not found: %s" % method}}
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    def call(self, name: str, args: dict) -> dict:
        if name == "docs.search":
            hits = self.store.search(args.get("query", ""), args.get("top_k", 4),
                                     args.get("prefer"))
            return {"content": [{"type": "text", "text": __import__("json").dumps(
                {"hits": hits}, ensure_ascii=False)}], "structuredContent": {"hits": hits}}
        if name == "docs.get":
            return {"content": [{"type": "text", "text": __import__("json").dumps(
                self.store.get(args.get("doc_id", "")), ensure_ascii=False)}],
                "structuredContent": self.store.get(args.get("doc_id", ""))}
        if name == "docs.list":
            docs = self.store.listing()
            return {"content": [{"type": "text", "text": __import__("json").dumps(
                {"docs": docs}, ensure_ascii=False)}], "structuredContent": {"docs": docs}}
        return {"content": [{"type": "text", "text": "unknown tool"}], "isError": True}
