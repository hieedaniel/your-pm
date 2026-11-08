"""把 slidep 产出的 slides.md 解析成「页 → 文本」，供建立知识库。"""
import html
import re
import sys


def clean(line: str) -> str:
    s = line.strip()
    if not s:
        return ""
    # 去掉坐标标注 **cN**（左上 ……；右下 ……）
    s = re.sub(r"^\*\*c\d+\*\*\s*(（[^）]*）)?\s*", "", s)
    s = html.unescape(s)
    s = s.replace("\\|", "|").replace("\\-", "-")
    s = s.replace("&#32;", " ").replace("\u00a0", " ")
    s = re.sub(r"[ \t]{2,}", " ", s)
    return s.strip()


def parse(path: str):
    raw = open(path, encoding="utf-8").read()
    pages = []
    cur = None
    for line in raw.splitlines():
        # 冒号中英混用（"第 3 页：" / "第 3 页: "）都要认，否则整页会被吞进上一页
        m = re.match(r"^## 第\s*(\d+)\s*页\s*[:：]?\s*(.*)$", line)
        if m:
            cur = {"no": int(m.group(1)), "head": (m.group(2) or "").strip(), "text": []}
            pages.append(cur)
            continue
        if cur is None:
            continue
        if line.startswith("### "):
            continue
        c = clean(line)
        if c:
            cur["text"].append(c)
    for p in pages:
        # 去重相邻重复行
        out, prev = [], None
        for t in p["text"]:
            if t == prev:
                continue
            out.append(t)
            prev = t
        p["text"] = out
        p["title"] = p["head"] or (out[0][:40] if out else "第%d页" % p["no"])
    return pages


if __name__ == "__main__":
    pages = parse(sys.argv[1])
    for p in pages:
        body = " ".join(p["text"])
        print("[%02d] %-38s %d字" % (p["no"], p["title"][:38], len(body)))
        print("     " + body[:200])
