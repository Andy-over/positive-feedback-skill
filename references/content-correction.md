# 事实纠错的内容门控

用户指出某项信息可能有误时，先识别具体断言和适用任务，不把负面评价泛化为对全部内容的否定。可在 `context` 与 `prepare` 使用 `--factual-correction`；它将 `inspect_target`、`add_evidence`、`validate_requirements` 加入必需动作，并使任务需要证据JSON。对当前任务的明确纠错要求直接执行，不等控制器动作分数或反馈词映射。

实际核验需要可追溯的来源。具体来源选择依任务而定：有原始文件时核对文件和位置；有权威公开资料时核对原文及发布日期；信息不充分时明确哪些结论仍不确定。不要把找到一个URL当成已经核实该断言，不能把未经检查的来源写成支持证据。

`complete --evidence-json` 必须包含非空 `revised_answer`。对每项争议断言保存：`disputed`、`finding`（`supported | contradicted | uncertain`）、非空 `sources`；`contradicted` 还需 `corrected_text` 且修正文字必须出现在 `revised_answer`，`uncertain` 还需 `uncertainty` 且不确定性必须在回答中披露。`verify --result pass --checks-json` 必须有 `source_checks_passed: true`、与断言条数一致的 `claims_reviewed`，以及覆盖所有引用来源的 `checked_sources`。这些字段提供可审计的内容验收门槛；它们不是自动真实性判定，执行者仍需逐项核对来源及最终回答。未核实的内容不能标记为通过。

最终回答以纠正后的结论、关键证据及剩余不确定性为主体。若发生错误，可以简短承认，但不把礼貌措辞计作修复动作、证据或验收结果。对于值得复用的质量要求，使用 `evaluation_mode=quality` 并将已验证的具体要求存入 `quality_requirement`；仅当纠正任务验收通过后才参与学习。
