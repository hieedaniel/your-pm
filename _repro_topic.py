import asyncio, json, os, sys
import wma_agent as wa
from mcp_client import McpClient
from mcp_docs import LocalMcpServer
from mcp_multi import MultiSourceSearch
from orchestrator import Orchestrator

async def main():
    local = LocalMcpServer(["docs-kaiyi", "data/kb"])
    print("文档:", len(local.store.listing()), "块:", len(local.store.chunks))
    vocab = {"tags": local.store.hotwords(60), "aliases": {}}
    tax = wa.load_taxonomy("data/kb/taxonomy.json")
    vocab["topics"] = tax["topics"]
    ex = wa.RuleExtractor(vocab)
    events = []
    async def emit(ev, data):
        events.append((ev, data))
    # 模拟：配了一个连不通的远程 MCP 源 + 本地基线
    remote = McpClient({"transport": "http", "timeout_ms": 800,
                        "http": {"url": "http://127.0.0.1:9/none", "token": ""}})
    entries = [{"name": "远程MCP", "proxy": remote, "adapter": "none", "weight": 1.0,
                "prefix": "远程MCP:", "timeout_ms": 800, "enabled": True},
               {"name": "本地知识库", "proxy": McpClient({"transport": "local"}, local),
                "adapter": "local", "weight": 1.0, "prefix": "", "timeout_ms": 3000, "enabled": True}]
    search = MultiSourceSearch(entries)
    orch = Orchestrator({"policy": {"progressive_push": True, "max_hints_per_turn": 2}}, search, ex, emit)
    for u in ["有没有智慧宿舍的解决方案？如果有智慧宿舍的话，应该可以满足我的要求。",
              "我发现已经对接了 NCP 之后呢，那就没法自动的完成提示。"]:
        events.clear()
        await orch.on_final(u, "zh-CN")
        print("\n>>", u)
        for ev, d in events:
            if ev == "topic":
                print("  topic:", [(t["label"], t["confidence"]) for t in d["topics"]],
                      "| intent", d["intent"], "| push", d["should_push"],
                      "| prov", d["provider"], "| degraded", d.get("degraded"),
                      "| %sms" % d["latency_ms"], "| query:", d["query"][:50])
            elif ev == "hint":
                print("  hint:", d["title"], "|", d["answer"][:40], "| prov", d.get("provisional"))
            elif ev == "hint.update":
                print("  update:", d["id"], "|", d.get("title"))
            elif ev == "silenced":
                print("  SILENCED:", d["reason"])
            else:
                print("  ", ev, str(d)[:100])
    print("\nstats:", json.dumps(orch.stats(), ensure_ascii=False)[:300])
asyncio.run(main())
