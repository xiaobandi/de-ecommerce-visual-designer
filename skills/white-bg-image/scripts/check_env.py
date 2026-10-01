#!/usr/bin/env python3
"""环境能力探测（技能通用探针）。

技能激活时若不确定当前环境具备哪些能力，可运行本脚本获取一份能力清单，
据此在「首选路径」与「降级路径」之间选择。

设计约束（跨平台技能脚本通用）：
  - 只用 Python 标准库，零第三方依赖
  - 结果输出到 stdout，人类可读 + 末行为 JSON
  - 任何探测失败只降级为 False，绝不让整个脚本崩掉（脚本崩溃会被上层误判为环境不可用）
  - **绝不输出本机绝对路径**：本脚本的输出会被逐字录进
    `examples/output/session.txt`、`demo/walkthrough.html` 与 `docs/*`，
    随技能一起发布到公网。v1 直接打印 `str(Path.cwd())`，把作者家目录
    （`/Users/<用户名>/…`）写进了 448 个文件——既泄漏本机信息，又让示例
    在别人机器上显得不可复现。现改为「技能根相对路径」（见 `display_cwd`）。

用法:
    python3 scripts/check_env.py
    python3 scripts/check_env.py --selftest
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import sys
import tempfile
from pathlib import Path


def can_write_workspace() -> bool:
    """能否在工作区写入文件。

    判据只取「写成功」。v1 把 `unlink()` 也算进判定：只要环境禁止删除
    （沙箱、回收站策略、只读挂载的临时清理钩子），一个**完全可写**的目录
    就被误报成不可写——实测出现过 `can_write_temp=True` 却
    `can_write_workspace=False` 的自相矛盾输出，而技能据此会错误地
    走「降级为对话内联输出」路径。清理失败不影响结论，探针文件已在
    `.gitignore` 内。
    """
    probe = Path.cwd() / ".skill-env-probe"
    try:
        probe.write_text("ok", encoding="utf-8")
    except Exception:
        return False
    try:
        probe.unlink()
    except Exception:
        pass  # 删不掉不代表不可写
    return True


def can_write_temp() -> bool:
    try:
        with tempfile.NamedTemporaryFile(delete=True):
            return True
    except Exception:
        return False


def can_read_outside_workspace() -> bool:
    probe = Path.home() / ".skill-env-probe-absent"
    try:
        probe.exists()
        return True
    except Exception:
        return False


def has_network() -> bool:
    try:
        with socket.create_connection(("1.1.1.1", 53), timeout=1.5):
            return True
    except Exception:
        return False


def display_cwd() -> str:
    """当前工作目录的**可公开**表示（不含本机绝对路径）。

    规则：向上找到最近的含 `SKILL.md` 的祖先目录作为「技能根」，返回
    `<容器名>/<技能名>[/<子路径>]`；容器名取 `skills` / `workflows`
    （技能库与三层员工仓库都是这个结构），因此同一技能在任何机器、
    任何层级仓库里跑出来的这一行**完全一致**——示例才是可复现的。
    找不到技能根时只返回最后一级目录名。
    """
    cwd = Path.cwd().resolve()
    for p in [cwd, *cwd.parents]:
        if (p / "SKILL.md").is_file():
            rel = cwd.relative_to(p)
            base = (f"{p.parent.name}/{p.name}" if p.parent.name in ("skills", "workflows")
                    else p.name)
            return base if str(rel) == "." else f"{base}/{rel}"
    return cwd.name


PROBE_KEYS = (
    "python_version", "os", "cwd", "can_read_workspace", "can_write_workspace",
    "can_write_temp", "can_read_home", "has_network", "has_git", "has_unzip",
    "is_sandboxed",
)


def _stub(value):
    """返回一个恒返回 `value` 的替身函数（供自测替换真实探测）。"""

    def _f(*_a, **_k):
        return value

    return _f


def _raise_permission(*_a, **_k):
    """替身：任何调用都抛 PermissionError。"""
    raise PermissionError("selftest: permission denied")


def _selftest() -> int:
    """内嵌回归自测。

    核心断言有两条：① 输出里不得出现本机绝对路径；② 可写判定只取「写成功」，
    不得把「删除失败」算成「不可写」。

    **零副作用约束**：自测全程用替身替换真实探测，绝不写 `.skill-env-probe`
    等探针文件。原因——本脚本随 300+ 处副本分发，每份副本跑一次自测就会在
    磁盘上撒一个探针文件，随后任何批量清理都会撞上删除护栏（实测 50 文件
    阈值，`SAFE_DELETE_BULK_CONFIRM_REQUIRED`），于是「自测」自己成了污染
    源、成片失败。自测的职责是校验**判定逻辑**，不是复测宿主 OS 权限。
    """
    checks: list[tuple[str, bool, str]] = []

    def ck(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))

    shown = display_cwd()
    ck("cwd 表示不含前导斜杠", not shown.startswith("/"), shown)
    ck("cwd 表示不含家目录用户段", "/Users/" not in shown and str(Path.home()) not in shown,
       shown)
    ck("cwd 表示非空", bool(shown), shown)
    ck("cwd 表示不含工程根绝对前缀", str(Path(__file__).resolve().parents[1]) not in shown, shown)

    saved = {n: globals()[n] for n in (
        "can_write_workspace", "can_write_temp", "can_read_outside_workspace",
        "has_network", "display_cwd")}
    try:
        globals().update({
            "can_write_workspace": _stub(True),
            "can_write_temp": _stub(True),
            "can_read_outside_workspace": _stub(True),
            "has_network": _stub(True),
            "display_cwd": _stub("skills/demo-skill"),
        })
        caps = probe()

        # JSON 契约：键齐全且无多余键
        for k in PROBE_KEYS:
            ck(f"JSON 含键 {k}", k in caps)
        ck("JSON 无多余键", not (set(caps) - set(PROBE_KEYS)),
           f"多出 {sorted(set(caps) - set(PROBE_KEYS))}")
        ck("JSON.cwd 也不含绝对路径", not str(caps.get("cwd", "")).startswith("/"),
           str(caps.get("cwd")))
        ck("可读工作区恒为真（结构性事实，不探测）", caps["can_read_workspace"] is True,
           str(caps.get("can_read_workspace")))
        ck("全能力可用时 is_sandboxed 为假", caps["is_sandboxed"] is False,
           f"is_sandboxed={caps.get('is_sandboxed')}")

        # 沙箱判定：读不到家目录 → 认为在沙箱内
        globals().update({"can_read_outside_workspace": _stub(False)})
        ck("读不到家目录时 is_sandboxed 为真", probe()["is_sandboxed"] is True, "")
    finally:
        globals().update(saved)

    # 可写判定只取「写成功」：禁删环境不得把可写目录误报为不可写
    real_write, real_unlink = Path.write_text, Path.unlink
    try:
        Path.write_text = lambda self, *a, **k: len(str(a[0]) if a else "")
        Path.unlink = _raise_permission
        ck("删除失败不影响「可写」判定", can_write_workspace() is True,
           "沙箱/回收站策略禁删时，可写目录被误报为不可写")
        Path.write_text = _raise_permission
        Path.unlink = real_unlink
        ck("写入失败时判定为不可写", can_write_workspace() is False, "")
    finally:
        Path.write_text, Path.unlink = real_write, real_unlink

    fails = [(n, d) for n, ok, d in checks if not ok]
    for n, ok, d in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f" —— {d}" if not ok and d else ""))
    print(f"check_env 自测：{len(checks) - len(fails)}/{len(checks)} 通过")
    return 1 if fails else 0


def probe() -> dict:
    """收集环境能力（供 main 与自测共用）。"""
    caps = {
        "python_version": platform.python_version(),
        "os": platform.system(),
        "cwd": display_cwd(),
        "can_read_workspace": True,
        "can_write_workspace": can_write_workspace(),
        "can_write_temp": can_write_temp(),
        "can_read_home": can_read_outside_workspace(),
        "has_network": has_network(),
        "has_git": shutil.which("git") is not None,
        "has_unzip": shutil.which("unzip") is not None,
    }
    caps["is_sandboxed"] = not caps["can_read_home"] or not caps["can_write_temp"]
    return caps


def main() -> int:
    if "--selftest" in sys.argv:
        return _selftest()

    caps = probe()

    print("环境能力探测结果")
    print("-" * 34)
    for k, v in caps.items():
        print(f"  {k:22s} {v}")

    print("\n建议：")
    if caps["can_write_workspace"]:
        print("  - 可直接在工作区生成交付文件")
    else:
        print("  - 无法写入工作区：降级为在对话中内联输出，并说明原因")
    if caps["has_network"]:
        print("  - 联网可用：可按需检索外部信息")
    else:
        print("  - 无网络：只能依赖用户提供的材料，不要尝试联网")
    if caps["is_sandboxed"]:
        print("  - 检测到沙箱环境：避免依赖跨目录读写")

    print("\n" + json.dumps(caps, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # 探测脚本永不因异常中断调用方流程
        print(f"探测过程出现异常，按保守策略处理: {exc}", file=sys.stderr)
        print(json.dumps({"probe_failed": True, "error": str(exc)}, ensure_ascii=False))
        sys.exit(0)
