#!/usr/bin/env python3
"""合规词与 AI 味基线扫描（零依赖）。

对一段文案做三类检查：
  1. 绝对化用语 / 极限词（广告法第九条禁用词族）
  2. 平台违禁 / 高风险表述：医疗功效、特殊人群安全承诺、无依据销量、
     绝对承诺、金融诱导、诱导互动、绝对化购买引导、平台引流
  3. AI 味特征打分（套话密度、句长方差）—— 只提示，不参与阻断判定

判定口径（v2，2026-09 修订）
------------------------------
v1 把「极限词」排除在阻断之外，只给「警告」并返回退出码 0。真实实测中
被判为「漏放行」：文案「全网最好闻的雪松调，没有之一。100% 纯天然，
孕妇婴儿都能用。」只命中 3 处极限词 → 判「警告 / 退出码 0」，而其中
「孕妇婴儿都能用」实为高危安全承诺（见 reports/real-run/
de-ecommerce-visual-designer、de-creator-multichannel-rewriter）。

v2 起：
  - 极限词命中即「阻断」（绝对化用语本身违法，不再是「仅警告」）；
  - 新增 特殊人群承诺 / 无依据销量 / 绝对化引导 三类，v1 词表完全漏检；
  - 医疗功效补入「治失眠 / 抗癌 / 消炎」等口语化变体；
  - 保留 SAFE_CONTEXT 白名单，避免「最后 / 最近 / 第一步 / 第一人称」
    这类常用词被单字「最」「第一」误伤；
  - 重叠命中按「最长优先」去重（「最好」不会同时再报一个「最」）；
  - 需要「只提示不阻断」的草稿模式时加 --soft。

用法:
    python3 compliance_scan.py --file draft.md --platform 小红书
    echo "全网最低价" | python3 compliance_scan.py --platform 淘宝
    python3 compliance_scan.py --file draft.md --json --soft
    python3 compliance_scan.py --show-lexicon

退出码: 0 = 放行 / 仅提示, 1 = 阻断（命中高危）, 2 = 用法错误。

词表是基线而非全集：真实上线前请把平台最新规则补充进 --lexicon 指定的
JSON（{"极限词": [...], "违禁词": [...]}），脚本会自动合并。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# 词表版本：修改词表时同步更新，便于产出里标注「用哪版词表扫的」
LEXICON_VERSION = "2026.09-baseline"

# --- 基线词表（可被外部 lexicon 覆盖/追加） -------------------------------

ABSOLUTE_WORDS = [
    # 单字「最」+ 常见「最」词组
    "最", "最佳", "最好", "最优", "最强", "最低", "最高", "最便宜",
    "最先进", "最流行", "最火", "最划算", "最有效",
    # 排名类
    "第一", "No.1", "TOP1", "排名第一", "首屈一指", "数一数二", "名列前茅",
    # 独占类
    "唯一", "独家", "首创", "首个", "首款", "绝无仅有", "仅此一家", "史无前例",
    # 权威 / 级别类
    "国家级", "世界级", "全球领先", "行业领先", "国家级认证", "国家免检",
    "顶级", "顶尖", "王牌",
    # 极端承诺类
    "万能", "100%", "百分百", "绝对", "彻底", "根治", "永久", "终身", "包治",
    # 价格类
    "秒杀全网", "史上最低", "全网最低", "全网最低价", "惊爆价", "跳楼价", "史上最强",
]

# 白名单：命中区间落在这里面就不算违规（避免单字「最 / 第一」误伤常用词）
SAFE_CONTEXT = [
    r"最后", r"最近", r"最初", r"最终", r"最新", r"最晚", r"最少",
    r"第一步", r"第一版", r"第一次", r"第一批", r"第一轮", r"第一层",
    r"第一点", r"第一节", r"第一周", r"第一天", r"第一阶", r"第一人称",
    r"第一时间", r"第一位顾客", r"第一笔",
]

BANNED_PATTERNS = {
    "医疗功效": [
        r"治疗", r"治愈", r"疗效", r"药效", r"根除", r"抗癌", r"降血压", r"降血糖",
        r"治[^\n，。；;！!？?]{0,3}(失眠|咳嗽|感冒|头痛|疼痛|痘|斑|炎|病|癌)",
        r"(防|抗)(癌|病|脱发|衰老|过敏)",
        r"(消炎|杀菌|抗病毒)",
    ],
    # v1 完全漏检：特殊人群安全承诺是广告法高危项
    "特殊人群承诺": [
        r"(孕妇|孕妈|婴儿|婴幼儿|新生儿|儿童|小孩|宝宝|敏感肌|过敏体质)"
        r"[^\n，。；;！!？?]{0,8}(都|也|均|同样)?"
        r"[^\n，。；;！!？?]{0,6}(能|可|可以|放心|安全|适用)"
        r"[^\n，。；;！!？?]{0,6}(用|使用|吃|喝|涂|喷|接触|佩戴)?",
        r"(孕妇|婴儿|婴幼儿|新生儿|儿童|宝宝|敏感肌)"
        r"[^\n，。；;！!？?]{0,10}(绝对|完全|百分百|100%)?"
        r"[^\n，。；;！!？?]{0,4}(安全|无害|无刺激|零刺激|零添加|无添加)",
    ],
    # v1 完全漏检：无依据销量属虚假宣传
    "无依据销量": [
        r"(卖|售出|已售|销量|成交|发货|下单|回购)[^\n，。；;！!？?]{0,6}\d[\d,，\.]*\s*(万|w|W)?\s*\+?",
        r"\d[\d,，\.]{2,}\s*(万|w|W)?\s*\+?\s*单(?:量)?",
        r"\d[\d,，\.]*\s*(万|w|W)\s*\+?\s*单(?:量)?",
        r"\d[\d,，\.]*\s*\+\s*单(?:量)?",
        r"\d+\s*(万|w|W)\s*\+?\s*(人|位|个)?\s*(都在|已经|已|都)?\s*(用|买|选|抢|复购)",
    ],
    "绝对承诺": [
        r"包过", r"包退", r"无效退款", r"稳赚", r"保本", r"零风险", r"必瘦", r"必赚",
        r"保证(有效|见效|赚钱|成功)",
    ],
    "金融诱导": [r"稳赚不赔", r"日入过万", r"躺赚", r"内幕消息", r"保收益", r"月入十万"],
    "诱导互动": [r"不转不是", r"转发抽奖", r"点赞领", r"私信领现金", r"关注领红包"],
    # v1 只有「秒杀全网」；「没有之一 / 闭眼冲 / 吊打 / 用过都说好」等实测高频项漏检
    "绝对化引导": [
        r"闭眼冲", r"闭眼入", r"无脑冲", r"盲入", r"不接受反驳",
        r"吊打", r"碾压", r"没有之一", r"秒杀同行",
        r"用过?都(说|夸)好", r"好评如潮", r"口碑爆棚", r"谁用谁知道",
    ],
    "平台引流": [
        r"微信\s*[:：]?\s*[a-zA-Z0-9_\-]+", r"加\s*V", r"vx\s*[:：]", r"扣1",
        r"私聊我", r"加我(好友|微信)",
    ],
}

AI_TRACE_MARKERS = [
    "首先", "其次", "再次", "最后", "综上所述", "总而言之", "值得注意的是",
    "不仅", "而且", "更是", "在当今", "随着", "在这个", "意义上", "层面上",
    "赋能", "抓手", "闭环", "沉淀", "助力", "打造", "构建", "深度融合",
]

_SAFE_RES = [re.compile(p) for p in SAFE_CONTEXT]


def load_lexicon(path: str | None) -> dict[str, list[str]]:
    lex = {"极限词": list(ABSOLUTE_WORDS)}
    if path:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        for k, v in data.items():
            lex.setdefault(k, []).extend(v)
    return lex


def _in_safe_context(text: str, start: int, end: int) -> bool:
    win = text[max(0, start - 1): min(len(text), end + 2)]
    return any(r.search(win) for r in _SAFE_RES)


def _dedupe(hits: list[dict]) -> list[dict]:
    """重叠命中按最长优先去重：命中「最好」就不再单报一个「最」。"""
    hits.sort(key=lambda h: (h["start"], -(h["end"] - h["start"])))
    out: list[dict] = []
    last_end = -1
    for h in hits:
        if h["start"] >= last_end:
            out.append(h)
            last_end = h["end"]
    return out


def scan(text: str, lexicon: dict[str, list[str]]) -> dict:
    hits: dict[str, list[dict]] = {}

    # 1) 极限词：先收集原始命中 → 过滤白名单 → 最长优先去重
    raw: list[dict] = []
    for w in lexicon.get("极限词", []):
        for m in re.finditer(re.escape(w), text):
            if _in_safe_context(text, m.start(), m.end()):
                continue
            raw.append({"word": w, "start": m.start(), "end": m.end()})
    for h in _dedupe(raw):
        hits.setdefault("极限词", []).append({
            "word": h["word"],
            "pos": h["start"],
            "ctx": text[max(0, h["start"] - 8): h["end"] + 8],
        })

    # 2) 违禁 / 高风险表述
    for cat, pats in BANNED_PATTERNS.items():
        for p in pats:
            for m in re.finditer(p, text):
                hits.setdefault(cat, []).append(
                    {"word": m.group(0), "pos": m.start(),
                     "ctx": text[max(0, m.start() - 8): m.end() + 8]}
                )

    # 3) AI 味：套话密度（每千字命中数）+ 句长方差
    n_chars = max(len(text), 1)
    marker_hits = sum(text.count(m) for m in AI_TRACE_MARKERS)
    density = marker_hits / n_chars * 1000
    sents = [s for s in re.split(r"[。！？!?；;\n]", text) if s.strip()]
    lens = [len(s) for s in sents] or [0]
    avg = sum(lens) / len(lens)
    var = sum((l - avg) ** 2 for l in lens) / len(lens)
    ai_score = min(100, round(density * 8 + (0 if var > 60 else 20)))

    return {
        "lexicon_version": LEXICON_VERSION,
        "hits": hits,
        "ai_trace": {
            "score": ai_score,
            "marker_density": round(density, 2),
            "sentence_len_var": round(var, 1),
        },
    }


def verdict(result: dict, soft: bool = False) -> tuple[str, list[str]]:
    """所有命中类别都参与阻断判定（v1 曾把「极限词」排除在外，已修）。"""
    hits = result["hits"]
    reasons: list[str] = []
    for c in sorted(hits):
        reasons.append(f"{c} 命中 {len(hits[c])} 处")
    if result["ai_trace"]["score"] >= 60:
        reasons.append(f"AI 味评分 {result['ai_trace']['score']}，建议改写")

    if soft:
        # 草稿模式：只提示，不改判
        status = "提示" if reasons else "放行"
        return status, reasons

    status = "阻断" if hits else ("警告" if reasons else "放行")
    return status, reasons


def _print_lexicon() -> None:
    print(f"词表版本：{LEXICON_VERSION}")
    print(f"极限词 {len(ABSOLUTE_WORDS)} 条：{', '.join(ABSOLUTE_WORDS)}")
    for cat, pats in BANNED_PATTERNS.items():
        print(f"{cat} {len(pats)} 条：{' | '.join(pats)}")
    print(f"白名单 {len(SAFE_CONTEXT)} 条：{', '.join(SAFE_CONTEXT)}")


def _selftest() -> int:
    """内嵌回归自测：把「实测抓到的真实缺陷」固化成断言，随脚本一起分发，永不漂移。

    运行: python3 compliance_scan.py --selftest
    """
    checks: list[tuple[str, bool, str]] = []

    def ck(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))

    lex = load_lexicon(None)

    # 1) 实测原文案 1：必须是「阻断」，且要抓到 v1 完全漏检的「特殊人群承诺 / 没有之一」
    t1 = "全网最好闻的雪松调，没有之一。100% 纯天然，孕妇婴儿都能用。"
    r1 = scan(t1, lex)
    s1, _ = verdict(r1)
    ck("原文案1 判阻断", s1 == "阻断", f"实际 {s1}")
    ck("原文案1 命中特殊人群承诺", bool(r1["hits"].get("特殊人群承诺")))
    ck("原文案1 命中绝对化引导（没有之一）",
       any("没有之一" in h["word"] for h in r1["hits"].get("绝对化引导", [])))
    ck("原文案1 极限词去重后只报最长词",
       "最好" in [h["word"] for h in r1["hits"].get("极限词", [])]
       and "最" not in [h["word"] for h in r1["hits"].get("极限词", [])])

    # 2) 实测原文案 2：必须抓到「无依据销量」与「闭眼冲 / 用过都说好」
    t2 = "这个是我们家卖得最火的一款，卖了 10000+ 单，效果绝对是最好的，用过都说好，闭眼冲就完了。"
    r2 = scan(t2, lex)
    s2, _ = verdict(r2)
    ck("原文案2 判阻断", s2 == "阻断", f"实际 {s2}")
    ck("原文案2 命中无依据销量", bool(r2["hits"].get("无依据销量")))
    ck("原文案2 命中闭眼冲", any("闭眼冲" in h["word"] for h in r2["hits"].get("绝对化引导", [])))
    ck("原文案2 命中用过都说好", any("用过都说好" in h["word"] for h in r2["hits"].get("绝对化引导", [])))

    # 3) 白名单：常用词不得被单字「最 / 第一」误伤
    t3 = "最后，我们最近做了第一步尝试，第一次发笔记，第一人称视角，最终上线。"
    r3 = scan(t3, lex)
    ck("白名单：极限词 0 命中", not r3["hits"].get("极限词"),
       f"误报 {[h['word'] for h in r3['hits'].get('极限词', [])]}")

    # 4) 干净文案：必须是「放行」
    t4 = "这盏蜡烛用天然大豆蜡手工灌注，香调是雪松与琥珀，燃烧约 45 小时。"
    r4 = scan(t4, lex)
    s4, _ = verdict(r4)
    ck("干净文案 判放行", s4 == "放行", f"实际 {s4}")

    # 5) 平台引流（上游示例级）
    r5 = scan("国家级认证，绝对安全，加V：cup123", lex)
    ck("平台引流命中", bool(r5["hits"].get("平台引流")))
    ck("国家免检/国家级认证计入极限词", bool(r5["hits"].get("极限词")))

    # 6) soft 模式：不改判，不阻断
    s6, _ = verdict(r1, soft=True)
    ck("--soft 不改判为阻断", s6 != "阻断", f"实际 {s6}")

    fails = [(n, d) for n, ok, d in checks if not ok]
    for n, ok, d in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f" —— {d}" if not ok and d else ""))
    print(f"compliance_scan 自测：{len(checks) - len(fails)}/{len(checks)} 通过")
    return 1 if fails else 0


def main() -> int:
    # 自测模式要在 argparse 之前拦掉（避免被 required 参数拦住）
    if "--selftest" in sys.argv:
        return _selftest()

    ap = argparse.ArgumentParser(description="合规词与 AI 味基线扫描")
    ap.add_argument("--file", help="待检文件；缺省从 stdin 读取")
    ap.add_argument("--platform", default="通用", help="目标平台（用于提示规则差异）")
    ap.add_argument("--lexicon", help="补充词表 JSON")
    ap.add_argument("--soft", action="store_true", help="草稿模式：只提示，不阻断")
    ap.add_argument("--show-lexicon", action="store_true", help="打印当前词表后退出")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    if args.show_lexicon:
        _print_lexicon()
        return 0

    text = Path(args.file).read_text(encoding="utf-8") if args.file else sys.stdin.read()
    if not text.strip():
        print("错误：输入为空，无可检内容", file=sys.stderr)
        return 2

    lexicon = load_lexicon(args.lexicon)
    result = scan(text, lexicon)
    status, reasons = verdict(result, soft=args.soft)

    if args.json:
        print(json.dumps({"platform": args.platform, "status": status, "soft": args.soft,
                          "reasons": reasons, **result},
                         ensure_ascii=False, indent=2))
    else:
        print(f"目标平台：{args.platform}　词表版本：{result['lexicon_version']}")
        print(f"结论：{status}")
        for r in reasons:
            print(f"  - {r}")
        print(f"AI 味评分：{result['ai_trace']['score']}/100")
        total = sum(len(v) for v in result["hits"].values())
        print(f"命中总数：{total}")
        for cat, items in result["hits"].items():
            seen: list[str] = []
            for it in items:
                if it["word"] not in seen:
                    seen.append(it["word"])
            print(f"  [{cat}] {', '.join(seen)}")
        if status == "阻断":
            print("→ 命中高危，不得直接对外发布；逐条替换后重扫。")

    return 1 if status == "阻断" else 0


if __name__ == "__main__":
    sys.exit(main())
