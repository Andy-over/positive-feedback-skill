# 事件、动作证据与状态机

## 1. 任务记录

任务不是一段话的风格标签，而是一次可核验工作单元：

```text
task_id, task_kind, target_spec, target_source_id, source_message_id,
upstream_task_ref, created_by, completed_by, verified_by,
required_actions, flags, status, attempts,
output_id, actual_actions, artifacts, action_evidence,
verification_checks, verified_at
```

`task_kind` 为 `writing | code | data | general`。`required_actions` 来自用户明确要求，不受控制器概率削弱。`flags` 包括是否存在原成果、是否需要证据、是否要求修订、是否必须交付成果，以及是否为事实纠错。事实纠错的结构化证据和验收字段见 [内容核验](content-correction.md)。

任务正式登记前可以运行只读 `context`。它使用相同的任务类型与标志计算本轮动作分数，但不创建任务、事件或更新记录；明确要求的动作直接进入预生成约束。

动作词表固定为：

```text
inspect_target, edit_content, add_evidence, verify_citations,
recalculate, restructure, generate_revision, compare_versions,
validate_requirements, report_gaps, preserve_correct, deliver_artifact
```

`actual_actions` 必须覆盖 `required_actions`，且只记录真实完成的动作。成果文件记录绝对路径、大小、SHA-256；验收时重新计算并拒绝丢失或发生变化的文件。`action_evidence` 是非空JSON对象或数组。至少提供一种成果或证据。验收检查也必须是非空JSON。

若 `evidence_required=true`，必须提供非空 `action_evidence`；若 `output_required=true`，必须至少提供一个成果文件。这两个条件分别验证，不能互相替代。

任务状态：

```text
awaiting_execution -> awaiting_verification -> verified
                           | fail
                           v
                    awaiting_execution
```

失败验收写入 `attempts`，清除当前完成态后允许重做；不得覆盖已经验证的成果。

## 2. 反馈事件

统一字段：

```text
event_id, kind, score, label, quote,
task_id, source_id, source_message_id, previous_output_id, aspect,
failed_actions, target_spec, target_source_id,
target_actions, target_task_id, evaluation_mode,
quality_requirement, status, update_id, revisions
```

三类事件的绑定规则：

| kind | 参照对象 | 必填动作 | 训练前提 |
|---|---|---|---|
| positive | 已完成任务 | 使用该任务的 `actual_actions` | 原任务已验证 |
| request | 本条目标任务 | `target_actions` | 目标动作已执行且任务已验证 |
| evaluation | 上一结果与纠正任务 | `failed_actions`、`target_actions` | 纠正任务已验证 |

`request` 必须有 `target_spec` 和 `target_source_id`，禁止 `failed_actions`。`evaluation` 必须有 `failed_actions` 和模式；目标尚未出现时允许缺少目标字段，并停在 `awaiting_target`。一旦同条消息已给出修正目标，目标任务ID、规范、来源ID和目标动作必须成套记录。

`quote` 只能是用户消息中真实存在的片段；`label` 是单独的标准化映射词，绝不充当原文。CLI 新记录必须提供 `--quote`。`source_message_id` 标识原消息，多个子任务或混合信号可以共享它；`source_id` 标识该消息中的唯一信号片段，不能重复奖励。多目标句应拆成独立任务，并逐项核对片段的对象；引用、否定、普通许可、评价和明确命令不可仅凭关键词自动归类。没有映射词的明确要求照常执行，不伪造请求事件。

- `selection`：失败动作与目标动作不得重叠，表示需要改变动作选择。
- `quality`：必须填写最多400字符的 `quality_requirement`，失败动作与目标动作允许重叠，表示继续执行该动作但提高质量与验收要求。

`positive` 与 `evaluation` 的来源任务必须已经验收；`evaluation.failed_actions` 必须是来源任务 `actual_actions` 的子集，并绑定来源任务 `output_id`。只有 `selection` 模式禁止失败动作与目标动作重叠。请求和纠正目标的 `target_source_id` 必须匹配其已登记任务。

## 3. 事件状态

```text
awaiting_target      负面评价尚无相关纠正目标
awaiting_execution   目标任务尚未执行
awaiting_verification 已执行但未验收
ready                已满足训练条件
learned              已进入一次可审计重放更新
```

`learn` 每次重新检查状态。只有 `ready` 事件进入训练；状态已经是 `learned` 的事件直接跳过，不能被新的 `update_id` 重复登记。`update_id` 重复提交为幂等；事件内容纠正必须显式 `--replace`，保存旧版本到 `revisions`，撤销旧 `update_id`，并立即从有效 `learned` 历史重放以移除旧参数影响。纠正后的事件重新达到 `ready` 后才能学习新版本。

报告时重新计算未学习事件的派生状态，不能直接展示可能陈旧的缓存状态。历史更新被事件修订取代时，在旧更新中记录 `superseded_events`。

## 4. 存储隔离

真实数据默认写入 skill 主目录的 `.positive-feedback/MODEL_PROFILE/chats/chat-<CODEX_THREAD_ID>/`，不再依赖调用时工作目录。每个聊天有独立子目录：控制器任务、事件和参数写入该目录的 `action-controller.json`；具体诉求写入 `preferences.json`，轻量反馈账本写入 `events/`。`MODEL_PROFILE` 使用宿主公开的稳定模型或控制器标识，不同模型不得共用，身份未知时不得猜测；缺少稳定聊天 ID 时拒绝真实记录。当前聊天跨项目继续使用同一目录，不同聊天不共享控制器权重或历史。用户指定名称时，运行 `python scripts/chat_record.py --profile MODEL_PROFILE set-name --name '名称'`；目录改为 `chat-<CODEX_THREAD_ID>--<名称>`，已有文件原样迁移，名称映射保存在配置档的 `record-names.json`。未指定时默认使用 ID 名称。CLI 未提供 `--profile` 时拒绝使用隐式默认值；隔离测试可显式传入临时 `--state` 或 `--data-dir`。skill 目录中只有 `.positive-feedback/MODEL_PROFILE/` 可保存这些状态，其他路径仍拒绝。旧项目目录及此前配置档共享文件中的历史状态不自动搬迁或合并，需单独核对后迁移。

任务从一个配置档转到另一档时，只在新任务的 `upstream_task_ref` 中记录可核对的旧任务引用，不复制旧事件、更新或参数。提供宿主执行者标识时可用全局 `--executor-id`；报告区分原始创建者、最近执行者和当前查询者。旧 v5 状态可读，缺失身份字段显示为 `null`；下一次写入时增量补充，不猜测历史身份。跨档偏好默认不共享，只有用户明确要求迁移且逐条核对来源和适用范围时，才在新档单独添加。

CLI 最小示例（保持项目目录为当前工作目录，使用脚本绝对路径）：

```powershell
$PositiveFeedbackPolicy = 'C:\Users\LENOVO\.codex\skills\positive-feedback\scripts\policy.py'
python $PositiveFeedbackPolicy --profile MODEL_PROFILE context --compact --task-kind writing --required-actions inspect_target,edit_content
python $PositiveFeedbackPolicy --profile MODEL_PROFILE prepare --task-id TASK --task-kind writing --required-actions edit_content,deliver_artifact --target-spec '明确目标' --target-source-id MESSAGE-PART --source-message-id MESSAGE
python $PositiveFeedbackPolicy --profile MODEL_PROFILE complete --task-id TASK --output-id OUTPUT --actions edit_content,deliver_artifact --artifact PATH --evidence-json EVIDENCE.json
python $PositiveFeedbackPolicy --profile MODEL_PROFILE verify --task-id TASK --result pass --checks-json CHECKS.json
python $PositiveFeedbackPolicy --profile MODEL_PROFILE feedback --event-id EVENT --kind positive --task-id TASK --source-id FEEDBACK-PART --source-message-id FEEDBACK --label 很好 --quote '这次修改很好'
python $PositiveFeedbackPolicy --profile MODEL_PROFILE learn --update-id UPDATE
python $PositiveFeedbackPolicy --profile MODEL_PROFILE report
```

成果、证据和检查文件已经实际存在后，可把步骤写入独立的 UTF-8 JSON 文件，并调用 `batch --plan-json PLAN.json`。该命令支持最多 8 个步骤，按数组顺序执行 `prepare`、`complete`、`verify`、`feedback`、`target`、`learn`，每步字段名与对应 Python 函数相同；`verify.passed` 必须为布尔值。失败时整批状态不提交，文件成果本身由原任务管理。它不能替代实际编辑和检查；已登记任务可从 `complete` 开始，新任务可从 `prepare` 开始。例如：

```json
{"batch_id":"STABLE-BATCH-ID","steps":[
  {"op":"complete","task_id":"TASK","output_id":"OUTPUT","actions":"edit_content,deliver_artifact","artifacts":["ABSOLUTE_PATH"],"evidence_json":"EVIDENCE.json"},
  {"op":"verify","task_id":"TASK","passed":true,"checks_json":"CHECKS.json"},
  {"op":"feedback","event_id":"EVENT","kind":"positive","task_id":"TASK","source_id":"FEEDBACK-PART","label":"很好","quote":"这次修改很好"},
  {"op":"learn","update_id":"UPDATE"}
]}
```

示例值不能直接作为真实记录使用；真实 `quote` 必须是用户原文，成果和证据必须可核查。CLI 命令为 `python $PositiveFeedbackPolicy --profile MODEL_PROFILE batch --plan-json PLAN.json`。成功后整批保存一次；使用相同 `batch_id` 和相同内容重试返回 `duplicate`，同一 ID 的不同内容被拒绝。

模型只需确认每步状态时，可加 `--compact`，避免打印完整任务记录；失败仍返回非零状态与原始错误，不会隐藏拒绝原因。

只有确认同一任务链的纠正目标后，才用 `target --event-id EVENT --target-task-id FIXED_TASK --target-spec '明确修正内容' --target-source-id TARGET_MESSAGE --target-actions edit_content,validate_requirements` 关联。历史事件纠正使用 `feedback ... --replace`，保留 `revisions`；若原文或对象无法核实，不凭摘要猜测修订。

## 5. 同条与下一条归因

- 正反馈默认指向最近一个对象明确、已完成的回答或成果；对象不明确则不记录。
- 负面评价只把明确失败方面绑定到上一结果，不能自动否定所有动作。
- 修正目标可以出现在同条消息；若出现在下一条或更后消息，必须确认它与该失败项属于同一任务链后使用 `target` 链接。
- 无关的新任务不能被当成旧负反馈的纠正目标。
- “希望你/需要你/我想/想/要求你/命令你”中的负数只表示请求力度，目标是同条请求，不回看上一结果做排斥更新；“我想”和“想”须表达实际任务诉求，重叠命中只记一次。
- 一条消息同时包含评价和请求时创建两个事件；不得求和或互相覆盖。

## 6. 操作优先级

控制器仅对明确要求之外的动作排序提供建议。执行顺序为：用户明确目标与约束 → 必需实际操作 → 成果证据 → 验收 → 可选控制器建议 → 简短说明。语言风格变化只能是完成这些动作的副产品，不能代替实际修改。
