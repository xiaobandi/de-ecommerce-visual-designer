# 随仓分发的共享脚本

本仓工作流的确定性脚本由 `agent-skills-core@1.0.0` 提供
（见 `dependencies.yaml`；本仓**不复制技能正文**）。为了让文档与示例在
**只克隆本仓**的前提下也能复现，这里随仓分发被引用到的那几个脚本。

| 脚本 | 说明 |
|---|---|
| `compliance_scan.py` | 副本，真相源在 agent-skills-library 共享脚本库 |

## 约束

- **单一真相源**在 `agent-skills-library/content/scripts/`；本目录是副本，
  由构建期漂移守卫（`digital-employee-repos/tools/sync_scripts.py --check`）
  保证与真相源逐字节一致——改了真相源却忘了同步副本，构建会直接中止
- 零第三方依赖，仅标准库
- 文件/stdin 进、stdout 出，退出码表达判定：0 通过 / 1 警告 / 2 阻断
- 每个脚本都内嵌 `--selftest`，`python3 tests/run.py --suite script` 会逐个跑
