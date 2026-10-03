#!/usr/bin/env python3
"""数字员工仓库 · 自动化测试运行器（自带依赖，零第三方包，只读标准库 + pyyaml）。

这个文件会被 tools/build_repos.py 复制到每个仓库的 tests/run.py，
让每个仓库都能独立跑测试（本地与 GitHub Actions 都直接可用）。

六个套件：
  structure  结构完备性：必需文件、目录名==frontmatter name、相对链接可达、YAML 可解析
  contract   契约闭环：清单与磁盘一致、工作流调用链可解析、依赖版本可解析、无重复物理存放
  scenario   多场景用例：把 tests/cases.yaml 的每条期望，对照产出正文做"可执行性"断言
  script     共享脚本回归：对每个自带 --selftest 的 .py 跑内嵌断言（确定性，不需要 LLM）
  doc        文档命令可执行性：文档里写出来的命令，照抄必须跑得通（路径可达 + cwd 明确）
  compliance 纪律与合规：红线声明齐备、无密钥、无真实 PII、许可齐备、无本机绝对路径

为什么 scenario 能不开 LLM 就跑：
  本仓库的技能是**提示词即执行体**。用例的每条 expect 都对应产出正文里
  必须存在的可执行约束（例如「缺输入要停下来问」必须能在正文字面找到）。
  于是"这条用例能不能被满足"退化为"正文里有没有承载它的那条约束"——
  这是确定性的、可 CI 的，而且真能抓出退化的提示词。

为什么 script 套件是「自测随脚本分发」：
  共享原子脚本（合规扫描 / 标题生成 / 平台适配 / 简报渲染 / 异常检测 …）会
  物理复制到多个技能目录。若把自测单独放一份，它和脚本本身就会各走各的。
  因此每个脚本内嵌 `--selftest`，测试与实现同一份文件，副本再漂移也会被
  本套件当场发现。

用法：
  python3 tests/run.py                            # 全量，只在终端输出
  python3 tests/run.py --suite scenario           # 单套件
  python3 tests/run.py --suite script             # 只跑共享脚本自测
  python3 tests/run.py --suite doc                # 只跑文档命令可执行性
  python3 tests/run.py --filter wf-ecom-scout     # 按 slug 过滤
  python3 tests/run.py --json reports/last.json   # 落盘报告（不传则不写文件）
退出码：0 全通过；1 有失败。

关于落盘：默认**不写文件**。仓里提交的 `reports/last-run.json` 是全量快照，
只有 tools/run_all_tests.py 才会写它。若默认就往那儿写，随手跑一个单套件
就会把已提交的全量报告覆盖成单套件结果，工作区变脏且真假难辨。
报告里的 `suites_run` / `full` 两个字段用来标明这份报告覆盖了几个套件。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    import yaml
except ImportError:                                     # pragma: no cover
    print("需要 pyyaml：pip install pyyaml", file=sys.stderr)
    sys.exit(2)

ROOT = Path(__file__).resolve().parents[1]
CST = timezone(timedelta(hours=8))

REQUIRED_COMMON = ["README.md", "LICENSE", "CHANGELOG.md", "CONTRIBUTING.md",
                   ".gitignore", ".github/workflows/ci.yml", "tests/run.py"]

# 期望正文里出现的「可执行约束」→ 用于 scenario 套件做确定性断言
BEHAVIOR_MARKERS = {
    "missing-input": ["缺", "不要猜"],
    "numeric-discipline": ["待填", "数值与事实纪律"],
    "boundary": ["业务红线", "不做"],
    "happy-path": ["输出契约", "执行步骤"],
    "gate": ["人工确认", "待确认"],
    "degrade": ["降级", "模拟"],
    "end-to-end": ["调用", "步骤"],
}

SECRET_PATTERNS = [
    (r"sk-[A-Za-z0-9]{20,}", "疑似 OpenAI 风格密钥"),
    (r"gho_[A-Za-z0-9]{20,}", "疑似 GitHub OAuth token"),
    (r"AKIA[0-9A-Z]{16}", "疑似 AWS Access Key"),
    (r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*[\"'][^\"'<$\s]{16,}[\"']",
     "疑似硬编码凭据"),
]
PII_PATTERNS = [
    (r"(?<!\d)1[3-9]\d{9}(?!\d)", "疑似真实手机号"),
    (r"(?<!\d)\d{17}[\dXx](?!\d)", "疑似身份证号"),
    (r"(?<!\d)\d{16,19}(?!\d)", "疑似银行卡号"),
]
# 本机专属绝对路径：/Users/<用户名>/ 或 /home/<用户名>/。
# 不包含 /tmp/xxx —— 那是跨机器一致的中立占位（读者自己会替换）。
ABS_PATH_RE = re.compile(r"/(?:Users|home)/[A-Za-z0-9_.-]+/")

# 文档里的可执行命令：`python3 <某路径>.py`（路径可含 ../../）
DOC_CMD_RE = re.compile(r"python3\s+((?:\.\./|\./)?[A-Za-z0-9_./-]+\.py)")


class Result:
    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.skipped: list[dict] = []

    def add(self, suite: str, case_id: str, ok: bool, detail: str = "") -> None:
        self.rows.append({"suite": suite, "id": case_id, "ok": bool(ok),
                          "detail": detail})

    def skip(self, suite: str, case_id: str, reason: str) -> None:
        """记一条「不适用」，既不进通过数也不进用例数。

        为什么需要这个语义：入口汇总仓（digital-employees）只做索引与图表，
        本就不含 skills/ workflows/ 副本。对它断言「存在自带自测的共享脚本」
        与「文档中包含可执行命令」，是拿一把量错地方的尺子——失败的不是仓库，
        是这条断言。跳过必须区别于通过：通过=尺子量过且合格，跳过=尺子不适用。
        """
        self.skipped.append({"suite": suite, "id": case_id, "reason": reason})

    @property
    def failed(self) -> list[dict]:
        return [r for r in self.rows if not r["ok"]]


def ships_skill_artifacts() -> bool:
    """本仓是否随技能一起发布 skills/ 或 workflows/ 内容。

    只有入口汇总仓（hub）为假。若某个员工仓把这两个目录都弄丢了，那是结构
    退化——由 suite_structure 的「技能层非空」当场抓出，因此这里可以安全地
    把「无目录」当作「不适用」处理。
    """
    return any((ROOT / d).is_dir() for d in ("skills", "workflows"))


# ------------------------------------------------------------------ 工具


def read_yaml(p: Path):
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def front_matter(p: Path) -> dict:
    t = p.read_text(encoding="utf-8")
    if not t.startswith("---"):
        return {}
    end = t.find("\n---", 3)
    if end < 0:
        return {}
    try:
        return yaml.safe_load(t[3:end]) or {}
    except yaml.YAMLError:
        return {}


def rel_links(md: Path) -> list[str]:
    t = md.read_text(encoding="utf-8")
    out = []
    for m in re.finditer(r"\[[^\]]*\]\(([^)\s]+)\)", t):
        u = m.group(1)
        if u.startswith(("http://", "https://", "#", "mailto:")):
            continue
        out.append(u.split("#")[0])
    return [x for x in out if x]


def repo_kind() -> str:
    if (ROOT / "employee.yaml").exists():
        return "employee"
    if (ROOT / "core.yaml").exists():
        return "core"
    if (ROOT / "catalog").is_dir():
        return "hub"
    # 创作源库（agent-skills-library）：有 skills/ 与 content/ 但没有
    # 三层仓库的锚文件。它同样需要跑 doc / compliance 这类与仓库形态无关的套件。
    if (ROOT / "content").is_dir() and (ROOT / "skills").is_dir():
        return "library"
    return "hub"


def _skill_layer_count() -> int:
    """技能层规模 = 私有技能目录 + spec 绑定 + core 依赖条目。

    为什么要三项相加而不是只看 skills/：有的员工能力全部沉淀到公共底座
    （例如脚本写手用的钩子文案、口语化改写、脚本结构生成都是公共技能），
    它的技能层是"引用型"的，skills/ 为空并不等于仓库是空的。
    """
    n = 0
    if (ROOT / "skills").is_dir():
        n += sum(1 for p in (ROOT / "skills").iterdir() if p.is_dir())
    if (ROOT / "spec-bindings").is_dir():
        n += sum(1 for _ in (ROOT / "spec-bindings").glob("*.md"))
    d = ROOT / "dependencies.yaml"
    if d.exists():
        try:
            n += len((read_yaml(d) or {}).get("dependencies") or [])
        except Exception:                                # noqa: BLE001
            pass
    return n


# ------------------------------------------------------------------ 套件


def suite_structure(res: Result, flt: str | None) -> None:
    kind = repo_kind()
    for f in REQUIRED_COMMON:
        res.add("structure", f"必需文件存在：{f}", (ROOT / f).exists())

    extra = {
        "employee": ["employee.yaml", "dependencies.yaml", "persona/system-prompt.md",
                     "persona/boundary.md", "persona/kpi.md", "kb/README.md",
                     "kb/schema.yaml", "connectors/bindings.yaml",
                     "docs/01-安装与接入.md", "docs/02-使用教程.md",
                     "docs/03-场景与价值.md", "docs/04-架构与流程图.md",
                     "docs/05-验收与测试.md", "docs/images/architecture.svg"],
        "core": ["core.yaml", "connectors/definitions.yaml", "schemas/skill.schema.json",
                 "schemas/employee.schema.json", "tools/README.md"],
        "hub": ["catalog/employees.md", "catalog/skills-index.md",
                "catalog/workflow-index.md", "catalog/dependency-graph.md",
                "catalog/employees.csv", "architecture/system.svg"],
    }.get(kind, [])          # library：创作源库没有三层仓库的锚文件，跳过
    for f in extra:
        res.add("structure", f"必需文件存在：{f}", (ROOT / f).exists())

    # 技能：目录名必须与 frontmatter name 逐字相同（跨平台挂载的硬要求）
    sk = ROOT / "skills"
    n_dir = 0
    if sk.is_dir():
        for d in sorted(p for p in sk.iterdir() if p.is_dir()):
            n_dir += 1
            if flt and flt not in d.name:
                continue
            f = d / "SKILL.md"
            if not f.exists():
                res.add("structure", f"{d.name}: SKILL.md 存在", False)
                continue
            fm = front_matter(f)
            res.add("structure", f"{d.name}: SKILL.md frontmatter 可解析", bool(fm))
            res.add("structure", f"{d.name}: 目录名 == frontmatter name",
                    fm.get("name") == d.name,
                    f"目录 {d.name} / name {fm.get('name')}")
            res.add("structure", f"{d.name}: name 符合 kebab-case",
                    bool(re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", d.name or "")))
            res.add("structure", f"{d.name}: license 已声明", bool(fm.get("license")))
            md = fm.get("metadata") or {}
            res.add("structure", f"{d.name}: metadata.layer 合法",
                    md.get("layer") in {"L1-skill", "L2-workflow"})
            res.add("structure", f"{d.name}: tests/cases.yaml 存在",
                    (d / "tests" / "cases.yaml").exists())
    if kind != "hub":
        res.add("structure", "技能层非空（私有技能 ∪ spec 绑定 ∪ core 依赖）",
                _skill_layer_count() > 0, f"实际 {_skill_layer_count()} 项")

    # 工作流
    wfd = ROOT / "workflows"
    n_wf = 0
    if wfd.is_dir():
        for d in sorted(p for p in wfd.iterdir() if p.is_dir()):
            n_wf += 1
            if flt and flt not in d.name:
                continue
            f = d / "WORKFLOW.md"
            res.add("structure", f"{d.name}: WORKFLOW.md 存在", f.exists())
            if f.exists():
                fm = front_matter(f)
                res.add("structure", f"{d.name}: 目录名 == frontmatter name",
                        fm.get("name") == d.name)
                res.add("structure", f"{d.name}: 声明了 layer=L2-workflow",
                        (fm.get("metadata") or {}).get("layer") == "L2-workflow")
            res.add("structure", f"{d.name}: tests/cases.yaml 存在",
                    (d / "tests" / "cases.yaml").exists())

    # 相对链接可达
    bad_links = []
    for md in sorted(ROOT.rglob("*.md")):
        if any(x in md.parts for x in (".git", "node_modules", "reports")):
            continue
        for u in rel_links(md):
            if u.startswith("/") or "://" in u:
                continue
            if not (md.parent / u).resolve().exists():
                bad_links.append(f"{md.relative_to(ROOT)} → {u}")
    res.add("structure", "全部相对链接可达", not bad_links,
            f"{len(bad_links)} 个断链：" + "; ".join(bad_links[:5]))

    # 全部 YAML 可解析
    bad_yaml = []
    for y in sorted(ROOT.rglob("*.yaml")):
        if any(x in y.parts for x in (".git", "node_modules")):
            continue
        try:
            read_yaml(y)
        except Exception as e:                          # noqa: BLE001
            bad_yaml.append(f"{y.relative_to(ROOT)}: {e}")
    res.add("structure", "全部 YAML 可解析", not bad_yaml, "; ".join(bad_yaml[:5]))


def suite_contract(res: Result, flt: str | None) -> None:
    kind = repo_kind()
    local = {d.name for d in (ROOT / "skills").iterdir() if d.is_dir()} \
        if (ROOT / "skills").is_dir() else set()
    specs = {p.stem for p in (ROOT / "spec-bindings").glob("*.md")} \
        if (ROOT / "spec-bindings").is_dir() else set()

    if kind == "employee":
        emp = read_yaml(ROOT / "employee.yaml")
        meta = emp.get("metadata") or {}
        for k in ("id", "name", "repo", "group", "phase", "license", "version"):
            res.add("contract", f"employee.yaml.metadata.{k} 已声明", bool(meta.get(k)))
        res.add("contract", "仓库名 == employee.yaml.repo", meta.get("repo") == ROOT.name,
                f"{meta.get('repo')} vs {ROOT.name}")

        deps = read_yaml(ROOT / "dependencies.yaml")
        core = (deps or {}).get("core") or {}
        res.add("contract", "dependencies.yaml 声明了 core 版本",
                bool(re.fullmatch(r"\d+\.\d+\.\d+", str(core.get("version", "")))))
        dep_skills = {d["skill"] for d in (deps.get("dependencies") or [])}
        res.add("contract", "core 依赖非空（或显式声明无需 core）",
                bool(dep_skills) or not (emp.get("external_dependencies") or []))

        # 清单 vs 磁盘
        declared = {s["slug"] for s in (emp.get("deliverables") or {}).get("skills", [])}
        res.add("contract", "employee.yaml 声明的技能都在磁盘上",
                declared <= local, f"缺：{declared - local}")
        res.add("contract", "skills/ 目录都在清单里", local <= declared,
                f"多出：{local - declared}")
        wf_declared = {w["slug"] for w in
                       (emp.get("deliverables") or {}).get("workflows", [])}
        wf_disk = {d.name for d in (ROOT / "workflows").iterdir() if d.is_dir()} \
            if (ROOT / "workflows").is_dir() else set()
        res.add("contract", "员工工作流清单 == 磁盘目录", wf_declared == wf_disk,
                f"清单-磁盘 {wf_declared - wf_disk} / 磁盘-清单 {wf_disk - wf_declared}")

        # 工作流调用链可解析
        resolvable = local | specs | dep_skills
        unresolved = []
        for w in (emp.get("deliverables") or {}).get("workflows", []):
            if flt and flt not in w["slug"]:
                continue
            for c in w.get("calls") or []:
                if c not in resolvable:
                    unresolved.append(f"{w['slug']} → {c}")
            res.add("contract", f"{w['slug']}: 至少调用 1 个技能",
                    bool(w.get("calls")))
        res.add("contract", "全部工作流调用链可解析", not unresolved,
                "; ".join(unresolved[:5]))

        # 重复物理存放：私有技能不得与 core 同名
        clash = local & dep_skills
        res.add("contract", "无技能同时存在于本仓与 core（禁止内联复制）", not clash,
                f"冲突：{clash}")

    if kind == "core":
        core = read_yaml(ROOT / "core.yaml")
        sk = {s["slug"] for s in core.get("skills", [])}
        res.add("contract", "core.yaml 技能清单 == 磁盘", sk == local,
                f"清单-磁盘 {sk - local} / 磁盘-清单 {local - sk}")
        res.add("contract", "core.yaml 声明了版本与 tag",
                bool(core.get("metadata", {}).get("version")))
        for s in core.get("skills", []):
            res.add("contract", f"{s['slug']}: 复用员工数 ≥ 2",
                    s.get("mount_count", 0) >= 2,
                    f"实际 {s.get('mount_count')}")
        # 有意不公共化的能力必须有 spec
        spec_files = {p.stem for p in (ROOT / "specs").glob("*.md")} \
            if (ROOT / "specs").is_dir() else set()
        res.add("contract", "specs/ 覆盖全部 spec 条目",
                {s["slug"] for s in core.get("specs", [])} == spec_files)

    if kind == "hub" and (ROOT / "catalog" / "employees.yaml").exists():
        cat = read_yaml(ROOT / "catalog" / "employees.yaml")
        res.add("contract", "入口清单非空", bool(cat.get("employees")))
        # 32 个仓**各自独立发布**，入口仓里不可能存在员工仓目录。原先断言
        # `(ROOT.parent / repo).is_dir()` 只在"本地 32 仓并排于 repos/"这一
        # 构建布局下成立，一上 CI / GitHub 就必然全红（实测 30 条失败）——
        # 那是把构建布局误当成契约。正确判据是：入口仓对每个员工都建立了
        # **绝对地址**的可达引用。账号从索引自身的链接里解析，不写死。
        idx = []
        for rel in ("README.md", "catalog/employees.md", "catalog/by-group.md",
                    "catalog/by-phase.md", "catalog/by-channel.md"):
            f = ROOT / rel
            if f.exists():
                idx.append(f.read_text(encoding="utf-8", errors="ignore"))
        blob = "\n".join(idx)
        m = re.search(r"https://github\.com/([A-Za-z0-9._-]+)/", blob)
        owner = m.group(1) if m else ""
        res.add("contract", "索引以绝对地址引用他仓（不得用 ../ 相对路径）",
                bool(owner) and not re.search(r"\]\(\.\./", blob))
        for e in cat.get("employees", []):
            if flt and flt not in e["repo"]:
                continue
            res.add("contract", f"{e['repo']}: 已有绝对地址可达引用",
                    bool(owner)
                    and f"https://github.com/{owner}/{e['repo']}" in blob)
        res.add("contract", "分类维度齐备",
                all((ROOT / "catalog" / f).exists() for f in
                    ["by-group.md", "by-phase.md", "by-channel.md"]))

    # 版本字段
    for f in sorted(ROOT.rglob("*.md")):
        if any(x in f.parts for x in (".git", "reports", "node_modules")):
            continue
        if "skills/" in str(f) or "workflows/" in str(f):
            fm = front_matter(f)
            if fm:
                res.add("contract", f"{f.parent.name}: version 已声明",
                        bool((fm.get("metadata") or {}).get("version")))


def suite_scenario(res: Result, flt: str | None) -> None:
    case_files = sorted(ROOT.rglob("cases.yaml"))
    res.add("scenario", "存在测试用例文件", bool(case_files), f"{len(case_files)} 个")
    for cf in case_files:
        slug = cf.parent.parent.name
        if flt and flt not in slug:
            continue
        data = read_yaml(cf)
        cases = data.get("cases") or []
        res.add("scenario", f"{slug}: 用例数 ≥ 3", len(cases) >= 3, f"{len(cases)} 条")
        # 找到对应的产出正文
        doc = None
        for cand in (cf.parent.parent / "SKILL.md", cf.parent.parent / "WORKFLOW.md",
                     ROOT / "README.md"):        # 入口仓的用例锚在根 README
            if cand.exists():
                doc = cand.read_text(encoding="utf-8")
                break
        if doc is None:
            res.add("scenario", f"{slug}: 找到产出正文", False)
            continue
        for c in cases:
            cid = c.get("id", "?")
            res.add("scenario", f"{slug}/{cid}: 用例字段齐备",
                    all(c.get(k) for k in ("id", "title", "given", "expect", "forbid")))
            expect = c.get("expect") or []
            forbid = c.get("forbid") or []
            res.add("scenario", f"{slug}/{cid}: expect/forbid 非空",
                    len(expect) >= 1 and len(forbid) >= 1)

            # 方式 A：用例自带确定性断言（索引类仓库用这套）
            if c.get("asserts"):
                for i, a in enumerate(c["asserts"]):
                    ok, det = _run_assert(a)
                    res.add("scenario", f"{slug}/{cid}: 断言{i + 1} {a.get('type')}", ok, det)
                continue

            # 方式 B：把"期望的行为"落到产出正文里必须存在的可执行约束上
            key = "boundary" if cid == "boundary" else \
                "numeric-discipline" if cid == "numeric-discipline" else \
                "missing-input" if cid == "missing-input" else \
                "gate" if "gate" in cid else \
                "degrade" if "degrade" in cid else \
                "end-to-end" if "end-to-end" in cid else "happy-path"
            markers = c.get("markers") or BEHAVIOR_MARKERS[key]
            hit = [m for m in markers if m in doc]
            res.add("scenario", f"{slug}/{cid}: 正文承载了该用例的约束",
                    len(hit) >= 1, f"命中 {hit} / 需要 {markers}")
            # must_call 可解析
            mc = c.get("must_call") or []
            for m in mc:
                res.add("scenario", f"{slug}/{cid}: 调用 {m} 可解析",
                        (ROOT / "skills" / m).is_dir() or
                        (ROOT / "spec-bindings" / f"{m}.md").exists() or
                        _in_core_deps(m))


def _run_assert(a: dict) -> tuple[bool, str]:
    """执行一条确定性断言。支持 file_exists / file_contains / no_file / count_lines。"""
    t = a.get("type")
    p = ROOT / str(a.get("path", ""))
    if t == "file_exists":
        return p.exists(), str(a.get("path"))
    if t == "file_contains":
        if not p.exists():
            return False, f"文件不存在：{a.get('path')}"
        txt = p.read_text(encoding="utf-8")
        miss = [v for v in (a.get("values") or [v]) if v not in txt]
        return not miss, f"缺失 {miss}" if miss else ""
    if t == "no_file":
        hits = sorted(str(q.relative_to(ROOT)) for q in ROOT.rglob(str(a.get("name", ""))))
        return not hits, f"意外存在：{hits[:3]}"
    if t == "count_lines":
        if not p.exists():
            return False, f"文件不存在：{a.get('path')}"
        n = len([x for x in p.read_text(encoding="utf-8").splitlines() if x.strip()])
        return n == a.get("expected"), f"实际 {n} 行，期望 {a.get('expected')}"
    return False, f"未知断言类型 {t}"


def _in_core_deps(slug: str) -> bool:
    f = ROOT / "dependencies.yaml"
    if not f.exists():
        return False
    d = read_yaml(f) or {}
    return slug in {x["skill"] for x in (d.get("dependencies") or [])}


def suite_compliance(res: Result, flt: str | None) -> None:
    text_files = [p for p in ROOT.rglob("*")
                  if p.is_file() and p.suffix in {".md", ".yaml", ".yml", ".json",
                                                  ".py", ".txt", ".csv"}
                  and not any(x in p.parts for x in (".git", "reports", "node_modules"))]

    secrets, pii, abs_paths = [], [], []
    for f in text_files:
        try:
            t = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for pat, why in SECRET_PATTERNS:
            if re.search(pat, t):
                secrets.append(f"{f.relative_to(ROOT)}: {why}")
        for pat, why in PII_PATTERNS:
            if re.search(pat, t):
                pii.append(f"{f.relative_to(ROOT)}: {why}")
        # 注意：这里**不能**像 v1 那样用「文件里含 example 一词就豁免」。
        # v1 的写法让这条检查在几乎每个文件上被静默跳过——一个永不失败的测试
        # 等于没有测试，实测因此漏掉了 642 处 /Users/<作者>/ 泄漏。
        m = ABS_PATH_RE.search(t)
        if m:
            abs_paths.append(f"{f.relative_to(ROOT)}: {m.group(0)}")
    res.add("compliance", "无硬编码凭据", not secrets, "; ".join(secrets[:5]))
    res.add("compliance", "无真实 PII（手机号/身份证/银行卡）", not pii, "; ".join(pii[:5]))
    res.add("compliance", "无本机绝对路径泄漏", not abs_paths, "; ".join(abs_paths[:5]))

    lic = (ROOT / "LICENSE")
    res.add("compliance", "LICENSE 存在且非空", lic.exists() and lic.stat().st_size > 200)

    kind = repo_kind()
    n_redline = 0
    docs = []
    for d in (ROOT / "skills").glob("*/SKILL.md") if (ROOT / "skills").is_dir() else []:
        docs.append(d)
    for d in (ROOT / "workflows").glob("*/WORKFLOW.md") if (ROOT / "workflows").is_dir() else []:
        docs.append(d)
    for d in docs:
        if flt and flt not in d.parent.name:
            continue
        t = d.read_text(encoding="utf-8")
        if "业务红线" in t:
            n_redline += 1
        res.add("compliance", f"{d.parent.name}: 声明了「不做」边界",
                "不做" in t or "不" in t)
    res.add("compliance", "全部技能/工作流都写了业务红线", n_redline == len(docs),
            f"{n_redline}/{len(docs)}")

    if kind == "employee":
        b = ROOT / "persona" / "boundary.md"
        if b.exists():
            t = b.read_text(encoding="utf-8")
            res.add("compliance", "persona/boundary.md 含 10 条红线",
                    t.count("|") >= 30)
        c = ROOT / "connectors" / "bindings.yaml"
        if c.exists():
            d = read_yaml(c) or {}
            res.add("compliance", "连接器默认只读 + 写操作需人工确认",
                    (d.get("policy") or {}).get("default_access") == "read-only"
                    and (d.get("policy") or {}).get("write_requires_human") is True)
            res.add("compliance", "连接器凭证不落仓",
                    "环境变量" in str((d.get("policy") or {}).get("credential_location", "")))

    # 文档完备率
    if kind == "employee":
        need = ["docs/01-安装与接入.md", "docs/02-使用教程.md", "docs/03-场景与价值.md",
                "docs/04-架构与流程图.md", "docs/05-验收与测试.md"]
        res.add("compliance", "五类发布文档齐备",
                all((ROOT / f).exists() for f in need))


def suite_script(res: Result, flt: str | None) -> None:
    """共享脚本回归：对每个支持 `--selftest` 的脚本跑它内嵌的确定性断言。

    为什么这么做：共享原子脚本会被复制到多个技能目录（同一份文件存在多处）。
    把自测写在脚本内部（而不是单独一份测试文件），测试与实现就永远同源——
    副本一旦漂移，`compliance` 套件抓不到，但这里能抓（自测会随旧副本一起失败）。
    """
    scripts = sorted(
        p for p in ROOT.rglob("*.py")
        if p.name != "run.py"
        and not any(x in p.parts for x in (".git", "reports", "node_modules", "__pycache__"))
    )
    with_selftest = []
    for p in scripts:
        try:
            head = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "--selftest" in head:
            with_selftest.append(p)

    if not ships_skill_artifacts() and not scripts:
        res.skip("script", "存在自带自测的共享脚本",
                 "本仓不随技能发布脚本副本（入口汇总仓，只有索引与图表）")
        return

    res.add("script", "存在自带自测的共享脚本", bool(with_selftest), f"{len(with_selftest)} 个")
    for p in with_selftest:
        slug = p.parent.parent.name if p.parent.name == "scripts" else p.stem
        if flt and flt not in slug:
            continue
        try:
            proc = subprocess.run(
                [sys.executable, str(p), "--selftest"],
                capture_output=True, text=True, timeout=120, cwd=str(p.parent),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            res.add("script", f"{p.name} 自测（{slug}）", False, f"无法执行：{exc}")
            continue
        tail = [l for l in (proc.stdout or "").strip().splitlines()
                if "自测：" in l]
        detail = tail[-1].strip() if tail else (proc.stderr or "").strip()[:160]
        res.add("script", f"{p.name} 自测（{slug}）", proc.returncode == 0, detail)


def suite_doc(res: Result, flt: str | None) -> None:
    """文档命令可执行性：文档里写出来的每条命令，照抄必须跑得通。

    为什么需要这一套：文档里的命令是**生成器拼字符串**拼出来的，拼接错了
    语法上依然"看着没问题"，只有真去解析路径才暴露。实测抓出两类：
      1. 工作流的「嵌进自动化链路」示例把原子脚本的相对路径当参数又拼了一次 →
         `python3 scripts/a.py ../../skills/x/scripts/a.py --args`（路径重复，必失败）；
      2. 校验命令写成 `python3 tools/validate.py …` 却没写 cwd，而 `tools/`
         只存在于库根，读者在技能目录里照抄必然 No such file。
    两类都是"读者第一次照抄就踩"的缺陷，且都能被这一套确定性抓出。

    判据：命令里的 `*.py` 路径，在「命令所在目录」或「仓库根」下至少存在一个；
    并且校验类命令必须带 cwd 说明。

    **作用域只取 `skills/` 与 `workflows/` 子树**（这些是随技能一起发布的文档）。
    库根 `docs/`、`examples/`、`demo/` 下的叙述文会引用"在某技能目录内执行"的
    相对命令，那是行文口径问题而非断链，不在此套件判定范围内。
    """
    roots = [r for r in (ROOT / "skills", ROOT / "workflows") if r.is_dir()]
    if not roots:
        res.skip("doc", "文档中包含可执行命令",
                 "本仓不含 skills/ 与 workflows/ 文档（入口汇总仓）")
        res.skip("doc", "文档命令引用的脚本路径都可达",
                 "本仓不含 skills/ 与 workflows/ 文档（入口汇总仓）")
        res.skip("doc", "校验命令写明了执行目录（cwd）",
                 "本仓不含 skills/ 与 workflows/ 文档（入口汇总仓）")
        return
    text_files = [p for r in roots for p in r.rglob("*")
                  if p.is_file() and p.suffix in {".md", ".html", ".txt"}
                  and not any(x in p.parts for x in (".git", "reports", "node_modules"))]

    broken: list[str] = []
    total_cmds = 0
    n_checked = 0
    for f in text_files:
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for m in DOC_CMD_RE.finditer(text):
            raw = m.group(1)
            total_cmds += 1
            # 命令可能以「技能根 / 工作流根 / 库根 / 文档所在目录」为 cwd 书写。
            # 这里对候选基准取并集：只有在**所有**合理基准下都找不到，
            # 才算真正的断链——宁可漏报也不制造假失败。
            bases = [f.parent, f.parent.parent, f.parent.parent.parent, ROOT]
            cand = Path(raw)
            if any((b / cand).resolve().exists() for b in bases):
                continue
            n_checked += 1
            if flt and flt not in str(f):
                continue
            broken.append(f"{f.relative_to(ROOT)} → {raw}")

    res.add("doc", "文档中包含可执行命令", total_cmds > 0, f"{total_cmds} 条")
    res.add("doc", "文档命令引用的脚本路径都可达", not broken,
            f"{len(broken)} 条断链：" + "; ".join(broken[:5]))

    # 校验类命令必须写明 cwd（`tools/` 只存在于库根）
    missing_cwd = []
    for f in text_files:
        try:
            lines = f.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines):
            if "tools/validate.py" not in line:
                continue
            # 同一行或紧邻上一行必须提到 cwd（库根 / 根目录）。
            # 用**行号**取上一行，不能用 str.find 找首次出现——同一命令在文档里
            # 往往出现多次，find 会把第一处的位置当成本次的上下文，判定就跑偏。
            if "库根" in line or "根目录" in line:
                continue
            prev = lines[i - 1] if i else ""
            if "库根" in prev or "根目录" in prev:
                continue
            missing_cwd.append(f"{f.relative_to(ROOT)}:{i + 1}: {line.strip()[:70]}")
    res.add("doc", "校验命令写明了执行目录（cwd）", not missing_cwd,
            f"{len(missing_cwd)} 处未标注：" + "; ".join(missing_cwd[:5]))


SUITES = {"structure": suite_structure, "contract": suite_contract,
          "scenario": suite_scenario, "script": suite_script,
          "doc": suite_doc, "compliance": suite_compliance}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--suite", choices=list(SUITES), help="只跑指定套件")
    ap.add_argument("--filter", default=None, help="按 slug 过滤")
    # 默认不落盘。原先默认写 reports/last-run.json，导致"只想看一眼单套件"
    # 也会把仓里已提交的**全量**报告覆盖成单套件结果——工作区因此变脏，
    # 且肉眼分不出"这 3 条通过"是单套件还是全量。要落盘就显式给路径。
    ap.add_argument("--json", default=None,
                    help="把结果写入该路径；不传则只在终端输出、不写文件")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    res = Result()
    for name, fn in SUITES.items():
        if args.suite and name != args.suite:
            continue
        fn(res, args.filter)

    total = len(res.rows)
    failed = res.failed
    now = datetime.now(CST).isoformat(timespec="seconds")

    print(f"仓库：{ROOT.name}（{repo_kind()}）")
    print(f"用例：{total}　通过：{total - len(failed)}　失败：{len(failed)}"
          + (f"　跳过（不适用）：{len(res.skipped)}" if res.skipped else ""))
    if res.skipped and not args.quiet:
        print("\n跳过（本仓形态下该断言不适用）：")
        for s in res.skipped:
            print(f"  – [{s['suite']}] {s['id']}　— {s['reason']}")
    if failed and not args.quiet:
        print("\n失败明细：")
        for r in failed[:40]:
            print(f"  ✗ [{r['suite']}] {r['id']}"
                  + (f"　— {r['detail']}" if r["detail"] else ""))
        if len(failed) > 40:
            print(f"  … 还有 {len(failed) - 40} 条")
    by_suite: dict[str, list[int]] = {}
    for r in res.rows:
        b = by_suite.setdefault(r["suite"], [0, 0])
        b[0] += 1
        if not r["ok"]:
            b[1] += 1
    print("\n分套件：")
    for s, (t, f) in by_suite.items():
        print(f"  {s:11s} {t - f}/{t}")

    if args.json:
        out = Path(args.json)
        if not out.is_absolute():
            out = ROOT / out
        out.parent.mkdir(parents=True, exist_ok=True)
        suites_run = [s for s in SUITES if not args.suite or s == args.suite]
        out.write_text(json.dumps({
            "repo": ROOT.name, "kind": repo_kind(), "run_at": now,
            "cases": total, "passed": total - len(failed), "failed": len(failed),
            "by_suite": {k: {"total": v[0], "failed": v[1]} for k, v in by_suite.items()},
            "skipped": res.skipped,
            "failures": failed,
            # 显式记录本次跑了哪些套件。单套件报告与全量报告字段完全一样，
            # 只靠数字区分不了；标了 full 才能一眼看出这份报告能不能代表全仓。
            "suites_run": suites_run,
            "full": len(suites_run) == len(SUITES),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            shown = out.relative_to(ROOT)
        except ValueError:
            shown = out
        print(f"\n报告：{shown}")
    elif not args.quiet:
        print("\n（未指定 --json，本次未落盘报告；全量报告请走 tools/run_all_tests.py）")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
