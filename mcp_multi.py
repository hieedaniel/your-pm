"""多源检索：把 N 个 MCP 数据源合成一个 docs.search 入口。

为什么需要
----------
「资料中心」和「绩效系统」是两类完全不同的知识：
  · 资料中心 = 产品资料、方案、参数（**对客户能讲的事实**，多为 public）
  · 绩效系统 = 需求、问题单、项目进度（**内部研发状态**，多为 internal）
销售问一句「这个能不能做」，两边的答案都要给：资料中心回答「我们宣称支持什么」，
绩效系统回答「实际上做到哪一步、有没有已知问题」。合成一张卡会让依据互相污染，
所以按 doc_id 命名空间隔离（perf:req:31 / docs:xxx），只在排序层合并。

失败隔离：任一源超时/报错只影响它自己，另一个源的结果照常推 —— 不能因为
资料中心挂了，连已知问题单都不给销售看了。
"""
from __future__ import annotations

import asyncio
import time


class MultiSourceSearch:
    def __init__(self, sources):
        # sources: [{"name","proxy","weight","enabled"}]
        self.sources = [s for s in sources if s.get("enabled", True) and s.get("proxy")]
        self.calls = 0
        self.timeout_ms = 0
        self.lat = []
        self.last_errors = []

    @property
    def primary(self):
        return self.sources[0]["proxy"] if self.sources else None

    async def call(self, name: str, args: dict) -> dict:
        args = args or {}
        if "list" in name:
            return {"docs": await self.listing()}
        if "get" in name:
            doc_id = str(args.get("doc_id") or "")
            for s in self.sources:
                if doc_id.startswith(s.get("prefix") or ""):
                    return await s["proxy"].call(name, args)
            return {"error": "无匹配数据源：%s" % doc_id}
        if not self.sources:
            return {"hits": [], "note": "没有启用的数据源"}

        t0 = time.perf_counter()
        jobs = [self._one(s, name, args) for s in self.sources]
        got = await asyncio.gather(*jobs, return_exceptions=True)
        self.calls += 1

        merged, src_stats = {}, {}
        for s, res in zip(self.sources, got):
            tag = s.get("name") or "?"
            if isinstance(res, Exception):
                self._note("%s: %s: %s" % (tag, type(res).__name__, str(res)[:80]))
                src_stats[tag] = {"ok": False, "error": str(res)[:120], "hits": 0}
                continue
            hits = (res or {}).get("hits") or []
            if (res or {}).get("error"):
                # 「连上了但报错」和「真的没命中」必须分开：前者要暴露，后者是正常空
                src_stats[tag] = {"ok": False, "hits": 0,
                                  "error": str(res.get("error"))[:160]}
                self._note("%s: %s" % (tag, str(res.get("error"))[:120]))
                continue
            src_stats[tag] = {"ok": True, "hits": len(hits),
                              "ms": (res or {}).get("latency_ms"),
                              "partial": (res or {}).get("partial", False),
                              "keywords": (res or {}).get("keywords")}
            w = float(s.get("weight", 1.0))
            for h in hits:
                d = dict(h)
                d["score"] = round(min(1.0, float(d.get("score", 0)) * w), 3)
                d["origin"] = tag
                key = d.get("doc_id") or d.get("chunk_id") or d.get("title")
                prev = merged.get(key)
                # 同一份材料被两个源都命中时留高分那条，但记下来源，便于排查
                if prev is None or d["score"] > prev["score"]:
                    if prev:
                        d["also_in"] = sorted(set(prev.get("also_in") or []) | {prev.get("origin")})
                    merged[key] = d
                else:
                    prev["also_in"] = sorted(set(prev.get("also_in") or []) | {tag})

        out = sorted(merged.values(), key=lambda x: -float(x.get("score", 0)))
        self.lat.append((time.perf_counter() - t0) * 1000)
        return {"hits": out, "sources": src_stats, "source": "multi",
                "latency_ms": round(self.lat[-1], 1)}

    async def _one(self, s, name, args):
        try:
            return await asyncio.wait_for(s["proxy"].call(name, args),
                                          float(s.get("timeout_ms", 2500)) / 1000.0)
        except asyncio.TimeoutError:
            raise RuntimeError("超时 %sms" % s.get("timeout_ms", 2500))

    async def listing(self):
        out = []
        for s in self.sources:
            try:
                docs = await asyncio.wait_for(s["proxy"].listing(), 4.0)
            except Exception:
                continue
            for d in docs or []:
                d = dict(d)
                d["source_name"] = s.get("name")
                out.append(d)
        return out

    def _note(self, msg):
        self.last_errors.append("%s %s" % (time.strftime("%H:%M:%S"), msg))
        del self.last_errors[:-6]

    def stats(self):
        out = {"sources": [{"name": s.get("name"), "adapter": s.get("adapter", "none"),
                            "weight": s.get("weight", 1.0)} for s in self.sources],
               "calls": self.calls,
               "avg_ms": round(sum(self.lat) / len(self.lat), 1) if self.lat else 0.0}
        out["details"] = {s.get("name"): (s["proxy"].stats()
                                          if hasattr(s["proxy"], "stats") else {})
                          for s in self.sources}
        if self.last_errors:
            out["last_errors"] = self.last_errors[-3:]
        return out
