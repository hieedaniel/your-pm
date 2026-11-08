"""答案合成：把「客户这句话 + 文档原文片段」压成一句能照着说的话。

为什么要独立一层：检索只回答「哪份文档相关」，销售要的是「我该怎么接」。
资料中心返回的是 pptx / pdf 正文，动辄几千字，直接贴到卡片上等于没给。

两种模式，按配置自动选：
  llm     —— 有模型凭据时，把片段交给模型生成口语答案（严格禁止编造）
  extract —— 没模型时，从片段里抽最贴题的一句原文，前面标「原文」
两者都遵守同一条底线：**片段里没有的信息不许出现在答案里**。
llm 模式判断片段答不了，返回 NOT_COVERED，由调用方降级到 extract / 沉默，
而不是让模型硬编一句 —— 销售照着念错，比不给提示更糟。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import urllib.request

MAX_ANSWER_CHARS = 90

SYSTEM = """你在给一线销售做实时提词。用户会给你「客户刚说的一句话」和一份内部资料的原文片段。
请只用片段里的信息，写一句销售可以直接对客户说出口的话。

铁律：
1. 只输出那一句话，不要解释、不要前缀、不要引号、不要 Markdown。
2. 不超过 60 字，口语，像人说话。
3. 不要出现「根据资料」「文档中提到」「以上信息显示」这类词——销售不会这么跟客户说。
4. 可以有型号、数字、参数，但必须在片段里逐字出现过；不许换算、不许推测、不许补全。
5. 片段里没有能回答的信息时，只输出 NOT_COVERED，不要试图用常识补。"""


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------
def llm_cfg(cfg: dict | None) -> dict:
    """答案模型配置：answer 段优先，回落到 wma.openai（已有的兼容接口段）。"""
    cfg = cfg or {}
    a = cfg.get("answer") or {}
    o = (cfg.get("wma") or {}).get("openai") or {}
    return {
        "base_url": (a.get("base_url") or o.get("base_url") or "").rstrip("/"),
        "api_key": a.get("api_key") or o.get("api_key") or "",
        "model": a.get("model") or o.get("model") or "gpt-4o-mini",
        "timeout": float(a.get("timeout_ms") or 6000) / 1000.0,
        "enabled": str(a.get("enabled", "auto")).lower() not in ("false", "0", "off"),
        "mode": str(a.get("mode") or "auto").lower(),   # auto | llm | extract
    }


def available(cfg: dict | None) -> tuple[bool, str]:
    c = llm_cfg(cfg)
    if not c["enabled"] or c["mode"] == "extract":
        return False, "配置指定只用抽取模式"
    if not c["api_key"]:
        return False, "没有 model api_key（config.answer.api_key 或 wma.openai.api_key）"
    if not c["base_url"]:
        return False, "没有 base_url"
    return True, ""


# --------------------------------------------------------------------------
# LLM
# --------------------------------------------------------------------------
def _chat(c: dict, question: str, title: str, passages: list) -> tuple[str, str]:
    """同步调用 OpenAI 兼容接口。返回 (answer, error)。"""
    body = "\n\n".join("【片段%d】%s" % (i + 1, p) for i, p in enumerate(passages))
    user = ("客户刚说：%s\n\n资料标题：%s\n\n原文片段：\n%s" %
            (question, title or "（未命名）", body))
    payload = {
        "model": c["model"],
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": user}],
        "temperature": 0.2,
        "max_tokens": 160,
    }
    req = urllib.request.Request(
        c["base_url"] + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + c["api_key"]})
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with op.open(req, timeout=c["timeout"]) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        return "", "%s: %s" % (type(e).__name__, str(e)[:120])
    try:
        text = (d.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    except (AttributeError, IndexError):
        return "", "返回结构不认识：%s" % json.dumps(d, ensure_ascii=False)[:120]
    return clean(text), ""


def clean(text: str) -> str:
    """模型很爱加前缀和引号，卡片上这些都得去掉。"""
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?|```$", "", t).strip()
    t = re.sub(r'^["“”\']+|["“”\']+$', "", t).strip()
    t = re.sub(r"^(答|答案|回答|建议回答|可以这样说)[:：]\s*", "", t)
    t = re.sub(r"\s*\n+\s*", " ", t)
    # 「根据资料，X」这类前缀去掉，保留 X
    t = re.sub(r"^(根据|依据|按照)(该|这份|上述|以上)?(资料|文档|材料|文件)[，,、:：]\s*", "", t)
    return t.strip()


# --------------------------------------------------------------------------
# 抽取兜底
# --------------------------------------------------------------------------
def _bigrams(s: str) -> set:
    s = re.sub(r"[^\w\u4e00-\u9fff]+", "", s or "")
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) > 1 else set()


def extract_answer(question: str, query: str, passages: list) -> str:
    """挑一句最贴题的原文。没有模型时也得让人看见东西，且必须是原话。"""
    sents = []
    for p in passages:
        for s in re.split(r"(?<=[。！？；!?;])\s*|\n", p):
            s = s.strip(" 　·-—")
            # pptx 分页标记、md 标题符号都不能出现在销售要念出口的答案里
            s = re.sub(r"^\[第\d+页\]\s*", "", s)
            s = re.sub(r"^#{1,6}\s*", "", s)
            if 8 <= len(s) <= 120:
                sents.append(s)
    if not sents:
        fallback = re.sub(r"^\[第\d+页\]\s*", "", (passages[0] if passages else ""))
        return fallback[:MAX_ANSWER_CHARS]

    q = _bigrams(query) | _bigrams(question)
    words = [w for w in re.split(r"[\s,，、/]+", (query or "") + " " + (question or ""))
             if len(w) >= 2]
    best, best_score = "", -1
    for i, s in enumerate(sents):
        score = len(q & _bigrams(s))
        score += sum(2 for w in words if w in s)
        score -= i * 0.01                      # 同分取靠前的（文档开头通常是结论）
        if score > best_score:
            best, best_score = s, score
    return best[:MAX_ANSWER_CHARS]


# --------------------------------------------------------------------------
# 对外
# --------------------------------------------------------------------------
async def compose(question: str, query: str, title: str, passages: list,
                  cfg: dict | None = None) -> dict:
    """返回 {answer, mode, quoted, error, ms}。

    mode: llm（模型生成）/ extract（原文摘句）/ none（无正文，无从作答）
    quoted: 答案是否来自原文摘句（前端据此决定要不要标「原文」）
    """
    t0 = time.perf_counter()
    if not passages:
        return {"answer": "", "mode": "none", "quoted": False,
                "error": "没有正文片段", "ms": 0.0}

    ok, why = available(cfg)
    if ok:
        c = llm_cfg(cfg)
        ans, err = await asyncio.to_thread(_chat, c, question, title, passages)
        ms = round((time.perf_counter() - t0) * 1000, 1)
        if ans and "NOT_COVERED" not in ans.upper():
            return {"answer": ans[:MAX_ANSWER_CHARS], "mode": "llm",
                    "quoted": False, "error": "", "ms": ms}
        if ans and "NOT_COVERED" in ans.upper():
            # 模型说片段答不了：退到原文摘句，但把判断记下来
            return {"answer": extract_answer(question, query, passages),
                    "mode": "extract", "quoted": True,
                    "error": "模型判断原文未覆盖该问题，已退为原文摘句", "ms": ms}
        # 调用失败也必须给内容，否则卡片又是空的
        return {"answer": extract_answer(question, query, passages),
                "mode": "extract", "quoted": True,
                "error": "模型调用失败：" + (err or "空返回"), 
                "ms": round((time.perf_counter() - t0) * 1000, 1)}

    ans = extract_answer(question, query, passages)
    return {"answer": ans, "mode": "extract", "quoted": True,
            "error": why, "ms": round((time.perf_counter() - t0) * 1000, 1)}
