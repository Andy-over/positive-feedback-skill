# 具体偏好记录

动作控制器只表达“编辑、核验、交付”等动作倾向。用户明确提出的可复用诉求使用独立的 `preferences.json` 保存，例如“先给结论，再说明依据”或“论文引用必须逐条核对原文”。不要从沉默、示例、第三方文字或一次性任务细节中推断长期偏好。

每条偏好包含：

```text
preference_id, text, source_id, task_kinds, exclude_task_kinds,
conflict_group,
priority, expires_at, status, created_at, updated_at, revisions
```

- `text` 去除多余空白，最多400字符。
- `task_kinds` 为空表示当前工作目录内全部任务；非空时只用于列出的任务类型。
- `exclude_task_kinds` 明确排除任务类型，不能和包含范围重叠。
- `priority` 范围0–100，较高者先进入上下文。
- `expires_at` 使用带时区的ISO 8601时间；到期后自动停止应用，但不删除记录。
- `status` 为 `active | disabled`。停用是可恢复操作。
- `conflict_group` 可选；同组同时适用时只输出一条，先看较高 `priority`，同优先级取最近更新的一条。只用于确知互斥的偏好，不能自动判定语义冲突。当前消息的明确要求高于任何已存偏好。

管理命令保持项目目录为当前工作目录，并使用 `policy.py` 的绝对路径：

```powershell
python $PositiveFeedbackPolicy --profile MODEL_PROFILE preference add --preference-id PREF --text "先给结论" --source-id MESSAGE --task-kinds writing --priority 80 --conflict-group report-order
python $PositiveFeedbackPolicy --profile MODEL_PROFILE preference disable --preference-id PREF
python $PositiveFeedbackPolicy --profile MODEL_PROFILE preference enable --preference-id PREF
python $PositiveFeedbackPolicy --profile MODEL_PROFILE preference list --task-kind writing
```

纠正已有偏好使用 `preference add ... --replace`，旧内容写入 `revisions`。`context` 最多选择6条适用偏好，具体偏好和质量要求合计最多1600字符；不会读取或上传完整控制器状态。

偏好按配置档隔离，不随模型切换自动复制。长期偏好需要用户明确提出可复用诉求；一次性任务要求直接执行，不需要为触发学习将其包装成偏好。评估实用性时，用独立任务案例记录启用前后的要求覆盖、验收、返工和耗时；参数变化不是质量提升证据，无改善也照实记录。
