# Week 4：Markdown 评估报告

本次增量改进为现有评估器增加 `--format markdown`，复用 `EvaluationReport.to_dict()` 的结构化结果，便于在代码审查或 CI 中阅读和保存。

```bash
kite-x402-evaluate --format markdown > evaluation-report.md
```

报告包含网络、资产、价格、场景 ID 与名称、通过/失败状态和总体汇总；失败场景还显示检查的预期值、实际值或异常。输出保留场景顺序，不添加时间戳，并对检查值的 JSON 键排序，因此相同结果生成相同 Markdown。Python 调用方可使用 `EvaluationReport.to_markdown()`。

GitHub Actions 在现有检查之外生成报告，并上传为 `x402-evaluation-report` artifact；评估失败时也会上传生成的报告，同时保留失败退出码。

文本和 JSON 输出、全部十个评估场景及退出码保持原有行为：全部通过为 `0`，任何失败为 `1`。facilitator 和上游仍使用 HTTP mock，不提交真实付款或连接真实服务。本次没有新增依赖或调整版本号。

验证包括 Markdown 成功/失败输出、异常详情、特殊字符、确定性及 CLI 格式和退出码测试，以及现有 pytest、包构建、元数据和安装检查。
