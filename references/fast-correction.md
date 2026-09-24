# 精简事实纠错流程

仅用于边界明确、能直接核实的单项事实纠错。先独立核实并形成修正答案；用户要求的答案不能被记录流程延后或替代。多项争议、需要复杂来源判断或质量评价归因时改读 [内容核验](content-correction.md) 与 [事件与状态机](event-schema.md)。

1. 若用户在当前任务明确启用本 skill，用宿主提供的 `CODEX_THREAD_ID` 执行一次 `session enable --source-excerpt`，不要同时传 `--profile` 或 `--state`。测试隔离状态只用 `--session-state`；真实运行用默认会话位置。
2. 准备稳定任务 ID、当前请求确实要求的动作、目标及来源 ID。`factual_correction:true` 自动加入 `inspect_target,edit_content,add_evidence,validate_requirements` 并要求证据，不需猜测额外动作。实际模型配置档用 `--profile`，隔离测试用 `--state`；两者不要同时传。
3. 在用户项目目录创建两个小 JSON 文件：`evidence.json` 为 `{"revised_answer":"修正后的答案","claims":[{"disputed":"原断言","finding":"contradicted","sources":["实际核过的来源或可复算过程"],"corrected_text":"修正后的答案中的原样片段"}]}`；`checks.json` 为 `{"source_checks_passed":true,"claims_reviewed":1,"checked_sources":["与证据完全一致的来源或过程"]}`。只有亲自核验才能写 `true`。不确定结论改用 `finding:"uncertain"`、`uncertainty`，并在答案中披露。无输出文件要求时，证据 JSON 即可，不要虚构成果文件。
4. 实际修正完成后，创建 UTF-8 `plan.json`，从 `prepare` 起用一次 `batch --plan-json plan.json` 原子提交。下面的 5 步示意包含精确字段；替换占位符为真实 ID、路径、用户原文和实际动作：

```json
{"batch_id":"BATCH","steps":[
  {"op":"prepare","task_id":"TASK","task_kind":"general","required_actions":"inspect_target,edit_content,add_evidence,validate_requirements","target_spec":"明确的纠错目标","target_source_id":"TARGET_SOURCE","source_message_id":"MESSAGE","factual_correction":true},
  {"op":"complete","task_id":"TASK","output_id":"OUTPUT","actions":"inspect_target,edit_content,add_evidence,validate_requirements","evidence_json":"evidence.json"},
  {"op":"verify","task_id":"TASK","passed":true,"checks_json":"checks.json"},
  {"op":"feedback","event_id":"EVENT","kind":"request","task_id":"TASK","source_id":"SIGNAL","source_message_id":"MESSAGE","label":"请","quote":"用户原文中的请","target_spec":"与 prepare 完全一致的目标","target_source_id":"与 prepare 完全一致的来源 ID","target_actions":"inspect_target,edit_content,add_evidence,validate_requirements"},
  {"op":"learn","update_id":"UPDATE"}
]}
```

会话命令示例：`python -X utf8 POLICY.py --thread-id THREAD --session-state TEST_SESSION.json session enable --source-excerpt '当前用户原文'`；真实运行省略 `--thread-id` 和 `--session-state`，直接用宿主 ID。事务命令示例：`python -X utf8 POLICY.py --profile REAL_PROFILE batch --plan-json plan.json --compact`；隔离测试把 `--profile REAL_PROFILE` 换成 `--state TEST_STATE.json`。`--compact` 只返回每步状态，不打印整份任务与训练参数。

`request` 只能有目标动作，**不能**传 `target_task_id` 或 `failed_actions`；它并不代表先前答案被验证为错误。仅当同条消息有可归因的实际委托词时保留后两步；否则只提交 `prepare`、`complete`、`verify`。`quote` 必须是当前用户原文，`label` 必须在请求词表中，不能照搬示例；其他分值或混合信号才按需读 [分值设计](design.md)。未核实的失败步骤不得提交或宣称学习。

不要在成功的常规路径额外运行 `report`、整份 `--help` 或加载无关参考文件；需要排错时才按错误定位查看具体命令或模式。最终答复以修正结论和验算为主，除用户要求机制说明外不输出训练回执。
