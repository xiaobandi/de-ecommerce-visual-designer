# 平台规格适配 · 测试说明

用例定义见 `cases.yaml`。执行：

```bash
python3 tests/run.py --suite scenario --filter platform-spec-adapter
```

| 用例 | 覆盖点 |
|---|---|
| happy-path | 正向链路与输出契约 |
| missing-input | 缺输入时不得编造 |
| boundary | 业务红线拦截 |
| numeric-discipline | 数值纪律（不得无据估算） |
