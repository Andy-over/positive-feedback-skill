# 可选训练数据模式

训练模式与 skill 启用状态是两道独立开关。必须由同一任务的用户明确要求启用；不追溯之前的聊天。只读 `status` 不建目录或文件。停用 skill 后即使模式文件仍标为 enabled，也不记录；再次启用 skill 时必须重新取得训练模式授权。请勿把「训练模式」误说成正在微调当前 Codex 模型。

## 命令

保持用户项目为当前工作目录，使用脚本绝对路径：

```powershell
$Training = 'C:\Users\LENOVO\.codex\skills\positive-feedback\scripts\training_mode.py'
python -X utf8 $Training mode status
python -X utf8 $Training mode enable --source-excerpt '用户明确要求启用训练模式的原文短句'
python -X utf8 $Training mode disable --source-excerpt '用户明确要求停用训练模式的原文短句'
```

`CODEX_THREAD_ID` 必须来自宿主。启用后，`policy.py verify` 验收通过或 `feedback` 登记成功时自动写摘要；原子 `batch` 中的相同步骤亦然。普通聊天、只读查询、准备中或验收失败的任务不会被自动捕获；确需补记时可用 `record --entry-json`，但只能填实际已知信息。测试时用 `--thread-id`、`--session-state`、`--data-dir`（`policy.py` 使用 `--training-data-dir`）指向临时隔离位置。真实数据不伪造任务 ID，也不把测试内容写进真实训练目录。默认目录 `training-data/<thread-id>/` 中，`mode.json` 保存开关和授权时间，`turns.jsonl` 逐行保存数据。

忽略规则只能防止常规 Git 添加；打包或发布前必须运行 `scripts/check_release.py` 审计，并确保排除训练数据目录。不要手动复制训练数据到公开产物。

## 每轮记录

`--entry-json` 是 UTF-8 JSON 对象，字段如下：

```json
{
  "turn_id": "宿主提供或本任务内稳定的唯一轮次 ID",
  "task_kind": "writing|code|data|general",
  "task_summary": "不含敏感信息的用户目标摘要",
  "response_summary": "实际交付结果摘要",
  "actions": ["inspect_target", "deliver_artifact"],
  "outcome": "verified|partial|failed|unknown",
  "verification": "可复查的简短检查结果；未检查写明未检查",
  "feedback": {"kind": "positive|evaluation|request|none", "summary": "有则填写已理解的反馈，没有则留空"},
  "model_profile": "仅在宿主明确提供时填写真实配置档",
  "origin": "manual|policy.verify|policy.feedback"
}
```

必填：`turn_id`、`task_kind`、`task_summary`、`response_summary`、`actions`、`outcome`、`verification`。`feedback`、`model_profile`、`origin` 可省略。自动记录按已验收任务或反馈事件生成稳定 ID，重复 ID 不新增；同一聊天轮次有多个已验收任务时可以产生多条记录。只记录实际发生的动作与核验，不把计划写成结果，不用推测代替缺失的宿主信息。用户内容只作最小化摘要，不复制完整消息；绝不填入隐藏推理、密码、令牌、私钥、个人联系方式或未必要的文件内容。脚本会拒绝不支持的字段和明显的秘密模式，但人工摘要仍须谨慎。

训练数据是待审阅样本，不直接作为模型正确性真值。后续优化前要先去重、审阅隐私与证据、筛选质量并做隔离评测。用户要求删除时可按指定任务目录删除；绝不以关闭模式冒充已删除数据。
