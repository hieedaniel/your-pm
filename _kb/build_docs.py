"""把凯翼智慧园区方案 PPT 的 41 页内容，按主题重组成知识库文档。

为什么要重组而不是「一页一篇」：
- 41 篇里有 4 页是目录、1 页封面、1 页法律声明，一页一篇会产生大量空壳，
  BM25 的 df 统计会被稀释，检索质量反而下降。
- 同一主题分散在多页（如安消 19/20/21），合并后单篇信息完整，
  卡片能一次给出完整依据，而不是碎片。

输出到 docs-kaiyi/*.md，front-matter 带 visibility / as_of / tags，
DocStore 直接可加载；正文用「## 场景（第 N 页）」切块，卡片 snippet 里自带页码。
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from parse import parse

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "kaiyi.md")
OUT = os.path.join(os.path.dirname(HERE), "docs-kaiyi")

AS_OF = "2022-08-26"

# 每页都带一条左侧导航，是模板元素不是内容，不剔掉会污染每一块检索语料。
# 注意：它在导出结果里是「一行一个」而不是一行，所以必须按行过滤。
NAV_ITEMS = {"数据运营中心", "生产制造管理", "物流仓储管理", "园区安消管理",
             "园区能耗管理", "行政办公管理", "园区设施管理"}

# 页码 → 文档。顺序即正文顺序
PLAN = [
    # (文件名, 可见级别, tags=检索/热词用的规范词, say=客户嘴里的说法, 页码, 用法备注)
    ("01-方案总览与覆盖范围", "public",
     "智慧园区,解决方案,覆盖范围,慧拼,海康威视,凯翼汽车,全场景,建设方案",
     "园区智能化,整体方案,你们能做什么,整套,覆盖范围",
     [1, 2, 8],
     "方案卖什么、覆盖哪些场景，一句话定位时用"),
    ("02-行业背景与政策驱动", "public",
     "政策,数字经济,十四五,智能制造,制造业转型,园区提升工程,信息化",
     "为什么现在做,有没有政策,政府有没有补贴,同行都在做",
     [3, 4],
     "客户问「为什么要现在做」时，给政策与市场规模依据"),
    ("03-凯翼园区一期现状与需求分析", "internal",
     "一期,现状,问题,需求分析,痛点,信息孤岛,巡更,配电运维,能耗监管,卡口,一卡通",
     "我们一期建了什么,老系统,以前上的,用得不好,没人用",
     [5, 6],
     "对客户现有系统的判断，属内部口径：可以说问题，不要说「你们很落后」"),
    ("04-总体架构与分层设计", "public",
     "总体架构,智能应用层,平台层,网络层,感知层,一张图,综合管理,集成,对接",
     "架构怎么搭,能不能打通,跟现有系统对接,要不要换掉原来的,换掉原来的,原来的平台,保留现有,平台化",
     [9, 10],
     "讲清「平台+应用+硬件」的分层，回答「是不是又要买一堆系统」"),
    ("05-数据运营中心与一张图", "public",
     "数据运营中心,一张图,AR实景,三维视频融合,可视化,数字孪生,总览,大屏,驾驶舱",
     "汇总到一个屏,领导要看全局,数据看不到,驾驶舱,一屏统览",
     [12, 13, 14],
     "客户问「能不能汇总到一个屏上」「领导要看全局」时的核心答案"),
    ("06-园区设施管理-配电室与电梯", "public",
     "配电室,配电,智能巡检,挂轨机器人,热成像,电梯,困人,远程监测,用电安全",
     "配电房,抄表,跳闸,停电,没人值班,电梯困人,电梯故障",
     [15, 16],
     "回答「配电房要人天天抄表」「电梯困人怎么第一时间知道」"),
    ("07-园区数字化展厅", "public",
     "展厅,数字化展厅,全息投影,数字沙盘,商业显示,参观接待",
     "展厅怎么弄,接待客户参观,参观动线,品牌展示",
     [17],
     "接待、参观、品牌展示类需求"),
    ("08-物流仓储与数字化月台", "public",
     "月台,数字化月台,车辆调度,预约,排队,扫码签到,装卸,物流,单号",
     "月台排队,司机等半天,装卸慢,车辆调度,月台不够用",
     [18],
     "回答「月台排队、司机等半天、月台利用率低」"),
    ("09-园区安消管理-周界与隐患检测", "public",
     "周界,入侵,翻越,人员离岗,抽烟,睡岗,穿戴,安全帽,雷视一体机,隐患,烟火,警戒,摄像头",
     "摄像头,录像,事后查,翻录像,有人翻墙,夜里看不清,安全靠人盯",
     [19, 20],
     "回答「摄像头只能事后查」「安全隐患靠人盯」"),
    ("10-消防管理", "public",
     "消防,消控主机,烟感,报警,联动,应急,电瓶车,充电,消防救援,门禁联动,消控室",
     "消防主机,报警确认,跑过去看,电瓶车充电,消防联动",
     [21],
     "回答「消防报警要人跑过去确认」「电瓶车充电安全」"),
    ("11-能耗管理", "public",
     "能耗,节能,用电,电费,抄表,分项计量,用能,空调,照明,节费",
     "电费高,浪费,不知道哪儿费电,空调用电,省电,分项",
     [22],
     "回答「电费高但说不清哪里浪费」"),
    ("12-人员与车辆管理", "public",
     "人员定位,人脸,门禁,闸机,访客,一脸通,车辆,停车,车牌,车位,测速,ESD,洁净区,登记",
     "访客登记,前台打电话,没人接,外来人员,乱停,车位找不到,找人",
     [23, 24],
     "回答「访客登记麻烦」「园区车乱停」「外来人员管不住」"),
    ("13-智慧巡检", "public",
     "巡检,巡逻,安保,垃圾桶满溢,垃圾,满溢,消防通道占用,机房温度,消控室,整改闭环,打点",
     "保安巡逻,走过场,巡逻不到位,问题没人管,整改,垃圾没人清",
     [25],
     "回答「保安巡逻走过场、问题没人闭环」"),
    ("14-考勤与会议管理", "public",
     "考勤,打卡,刷脸,移动考勤,外勤,会议,会议室,预约,视频会议,无纸化",
     "代打卡,打卡机,会议室被占,订了不用,开会,会议室不够",
     [26, 27],
     "回答「考勤代打卡」「会议室被占着没人用」"),
    ("15-信息发布与智慧食堂", "public",
     "信息发布,大屏,宣导,智慧食堂,刷脸消费,餐补,排队,食安,超市",
     "通知发不出去,食堂排队,刷脸吃饭,饭卡,餐补怎么算",
     [28, 29],
     "回答「通知发不到位」「食堂排队、餐补不好算」"),
    ("16-宿舍-移动端-共享柜-智慧厕所", "public",
     "宿舍,归寝,住宿,离调退宿,智能锁,APP,小程序,移动端,共享柜,智慧厕所,工位",
     "宿管,宿舍管理,晚上不回来,手机端能不能办,办公用品领用,厕所",
     [30, 31, 32, 33],
     "回答「宿舍管理靠宿管阿姨」「手机端能不能办」这类行政细节"),
    ("17-智慧大屏管理系统showOS", "public",
     "showOS,大屏,拼控,坐席,信号预监,多屏互动,中控,信息发布盒子,指挥中心",
     "拼接屏,指挥中心,大屏怎么控,坐席,多个屏一起显示",
     [34, 35],
     "客户已有大屏或在建指挥中心时讲"),
    ("18-典型案例", "internal",
     "案例,海康威视,桐庐,富士康,立讯精密,老板电器,AR车间,标杆",
     "同行案例,别人家怎么做的,有没有做过,标杆客户,参观",
     [37, 38, 39, 40],
     "同行标杆可增强可信度，但别把其他客户的项目细节当承诺讲给凯翼"),
    ("19-法律声明与使用限制", "restricted",
     "法律声明,版权,使用限制,免责",
     "",
     [41],
     "仅供合规备查，任何情况下都不推给销售当话术"),
]

# 页内残留的导出噪音：「- • 」「- n 」「- l 」「- Ø 」等
NOISE = re.compile(r"^[-–]\s*(?:[•·nØl]\s*)?")
TITLE_PREFIX = re.compile(r"^[^｜|]{0,20}[｜|]\s*")


def tidy(text: str) -> str:
    s = text.replace("\\&", "&").replace("&amp;", "&")
    s = s.replace("\\~", "~").replace("\\_", "_").replace("\\*", "*")
    s = re.sub(r"[ \t]{2,}", " ", s).strip()
    return s


def page_body(p: dict, doc_title: str) -> str:
    head = p["title"].strip().replace("|", "｜")
    prose, labels = [], []
    for t in p["text"]:
        t = tidy(t)
        if not t or t == p["title"]:
            continue
        t = NOISE.sub("", t).lstrip("•· ").strip()
        if len(t) < 2 or t in NAV_ITEMS:
            continue
        # 截图界面上的示意标注（「车牌：浙AXXXXX」「车速：25km/h」）是配图元素，
        # 不是方案内容，留着会让卡片看起来像乱码
        if re.match(r"^[^：:]{1,4}[：:]\S{0,12}$", t):
            continue
        # 长句 / 带标点 = 正文；短标签 = 架构图上的功能点
        (prose if len(t) >= 18 or re.search(r"[，。；：、]", t) else labels).append(t)

    out = []
    # 页标题通常就是下面那行加粗的结论句，标题已在「## …（第 N 页）」里出现过就不再重复
    if head and head != "- 目录 -" and head not in p["title"].replace("|", "｜"):
        # 章节前缀（「行政办公管理｜」）在这里是冗余的，主题已经写在文档标题里
        if "｜" in head:
            head = head.split("｜", 1)[1].strip()
        out.append("**%s**" % head)
    seen = set()
    for t in prose:
        k = re.sub(r"\s+", "", t)
        if k in seen:
            continue
        seen.add(k)
        out.append(t)
    # 架构页的短标签串成一行，检索命中率和可读性都比竖排好
    uniq = []
    for t in labels:
        if t not in uniq:
            uniq.append(t)
    if uniq:
        out.append("图示要素：" + "、".join(uniq) + "。")
    return "\n".join(out)


def main():
    pages = {p["no"]: p for p in parse(SRC)}
    os.makedirs(OUT, exist_ok=True)
    # 清掉旧的，避免改名后残留
    for name in os.listdir(OUT):
        if name.lower().endswith(".md"):
            os.remove(os.path.join(OUT, name))

    made, total_chars = [], 0
    for fname, vis, tags, say, nos, note in PLAN:
        title = re.sub(r"^\d+-", "", fname)
        chunks = []
        for no in nos:
            p = pages.get(no)
            if not p:
                continue
            body = page_body(p, title)
            if not body:
                continue
            ptitle = p["title"].replace("|", "｜").strip()
            if re.match(r"^- 目录 -$", ptitle):
                ptitle = "目录与章节划分"
            chunks.append("## %s（第 %d 页）\n%s" % (ptitle, no, body))
        if not chunks:
            continue
        body = "\n\n".join(chunks)
        total_chars += len(body)
        # 来源与「什么时候用」都放 front-matter：正文一旦出现「用法：回答『访客登记麻烦』」，
        # 演示问句就会命中自己这段话，卡片永久 1.00 分且内容全是元信息 —— 等于自我泄题。
        head = ("---\ntitle: %s\nvisibility: %s\nas_of: %s\ntags: %s\n"
                "%ssource: 凯翼智慧园区方案 · 海康威视 %s · P%s\n"
                "use_when: %s\n---\n\n" % (
                    title, vis, AS_OF, tags,
                    ("say: %s\n" % say) if say else "",
                    AS_OF, "、P".join(str(n) for n in nos), note))
        path = os.path.join(OUT, fname + ".md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(head + body + "\n")
        made.append((fname, vis, nos, len(body)))

    for m in made:
        print("%-42s %-10s 页%-12s %5d 字" %
              (m[0], m[1], ",".join(map(str, m[2])), m[3]))
    print("\n共 %d 篇 / %d 字 → %s" % (len(made), total_chars, OUT))


if __name__ == "__main__":
    main()
