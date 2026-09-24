# 会话启停与压缩后恢复

## 语义

- 作用域是宿主提供的单个 `CODEX_THREAD_ID`，不是所有对话、所有项目或某个模型配置档。
- 用户明确要求使用本 skill 后，本任务保持启用，直到同一任务的用户明确要求停用。仅提及、引用、审阅或修改 skill 不启用它。
- 每轮先查 `session status --compact`。若任务类型和实际模型配置档已明确且需要历史偏好，可直接用一次 `session-context --for-model` 同时查询状态和指导。首次使用、压缩后、入口指令不在当前上下文或 `skill_sha256` 改变时完整重读 `SKILL.md`；当前上下文已完整持有同版本时复用。不确定时重读。查询不自动记录或训练。
- 当前用户明确要求、事实核验、安全及更高层指令仍优先于本 skill 的历史偏好和控制器建议。不能把文件中的“最高优先度”解释为覆盖宿主指令层级。
- 如果任务 ID 缺失、状态文件无效或脚本不可用，报告无法确认启用状态，不据此伪称机制仍在运行。

## 命令

保留用户项目为当前工作目录，脚本使用绝对路径：

```powershell
$PositiveFeedbackPolicy = 'C:\Users\LENOVO\.codex\skills\positive-feedback\scripts\policy.py'
python -X utf8 $PositiveFeedbackPolicy session status --compact
python -X utf8 $PositiveFeedbackPolicy session enable --source-excerpt '用户明确要求使用的原文短句'
python -X utf8 $PositiveFeedbackPolicy session disable --source-excerpt '用户明确要求停用的原文短句'
python -X utf8 $PositiveFeedbackPolicy --profile MODEL_PROFILE session-context --for-model --task-kind TASK_KIND
```

`--source-excerpt` 必须是当前用户消息中的真实短片段，不得从示例或第三方内容复制。状态存于 Codex 用户目录的 `positive-feedback/sessions/<thread-id>.json`，独立于 `.positive-feedback/MODEL_PROFILE/` 的学习状态。测试用 `--thread-id` 和 `--session-state` 指向临时路径；真实运行不得伪造任务 ID，也不得将测试记录写入真实目录。

Codex 的全局 `AGENTS.md` 只提供简短的状态检查入口。skill 文件自身不能拦截宿主压缩过程，也不能保证宿主在任意环境中自动加载；恢复仍以宿主提供任务 ID、加载该入口并实际执行状态检查为条件。
