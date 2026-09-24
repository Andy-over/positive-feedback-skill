---
name: positive-feedback
description: Apply explicitly enabled, evidence-gated feedback to verified actions and factual corrections. Continue while active, including after compaction, until disabled; inspect or edit without recording.
---

# 反馈驱动的执行与产出调整

## 目标与边界

优先改善核查、实际执行、交付物和验收。用户指出事实错误时，核对可追溯来源、修正错误并标明不确定之处；道歉不能代替纠错。先完成用户明确要求，再用已核验的动作证据更新控制器。事实、安全规则和验收标准高于控制器建议。

仅当前任务明确启用后才记录；不追溯旧对话，不把示例、引用、反讽或模型自评当反馈。脚本失败时不得声称已学习。

`audit` 用于审阅或修改 skill，不写偏好；`observe` 只识别候选信号；只有 `active` 才登记、验收和学习。提到、查看或修改 skill 不会启用它。

用户明确要求使用时，以稳定 `CODEX_THREAD_ID` 执行 `session enable`；仅明确停用时执行 `session disable`。每轮只读查状态；首次使用、压缩后、指令不在当前上下文或入口哈希变化时完整重读，相同版本且仍在上下文时复用。不确定时重读。启用持续到明确停用，不因换题或配置档而失效；但服从更高层指令、安全规则及当前要求。状态检查失败或缺少任务 ID 时不猜测。命令见 [会话启停](references/session-activation.md)。

已知实际模型配置档和任务类型、且需要历史指导时，用一次只读 `session-context --for-model` 查询启停与本轮指导；只需启停状态时用 `session status --compact`。模型阅读模式只输出一次 `guidance`，当前明确要求始终优先。程序需要结构化字段时使用原有 JSON 模式。两种查询都不修改状态或原始消息；详情见 [预生成上下文](references/pre-generation.md)。

审阅、解释或修改本 skill 使用 `audit`，不为普通问答自动创建学习任务。实际产出且存在可归因的反馈信号、并已明确启用机制时，才登记闭环。普通事实纠错优先用 [精简纠错流程](references/fast-correction.md)，它包含该路径所需的字段和命令；不要为常规纠错加载完整事件模式、词表、偏好、控制器报告或逐个命令的帮助文本。其他记录执行前按需读 [事件与状态机](references/event-schema.md)；复杂事实纠错读 [内容核验](references/content-correction.md)；记录或应用具体诉求时读 [具体偏好](references/preferences.md)；需要回答前上下文时读 [预生成上下文](references/pre-generation.md)；解释数学或调试控制器时读 [控制器数学](references/controller.md)；要训练独立本地检查点时读 [本地模型训练](references/local-model-training.md)；需要分值映射与统计解释时读 [分值设计](references/design.md)。

## 三类信号必须分开

1. `positive`：范围[0,1]。肯定上一项已经完成且通过验收的工作，强化其中实际执行的动作。0是零强度认可，不是否定；缺失反馈不是0。
2. `evaluation`：范围[-1,0)。必须指出 `failed_actions`。`selection` 表示动作选择不合适，使用成对排序让纠正动作远离失败动作；`quality` 表示动作执行质量不足，允许继续使用同一动作并强化它，同时保存已验证的质量要求。
3. `request`：范围[-1,0)。负号仅编码请求强度。它使本条信息所要求的动作更可能执行，不否定、不远离上一轮结果，也不得填写 `failed_actions`。

混合信息按目标任务拆分，评价与请求分别记录，不相加、不跨任务归因。`quote` 保存真实原文，`label` 保存标准化标签；同一消息中的不同事件使用不同 ID。明确要求即使没有映射词也须执行，不补造词语。

执行映射以 `scripts/feedback.py` 的三类词表为准，完整刻度和歧义示例见 [分值设计](references/design.md)。先理解对象、否定、引用和实际意图，再选最完整表达；同一诉求不按子串重复记录。请求词只有实际委托任务时才记为 `request`，事实纠错词只是待核查的用户主张。无词表命中的明确要求仍须执行；“继续”、沉默和单纯许可不构成反馈。对象或类型无法可靠归因时不更新。

## 执行闭环

执行顺序为 `prepare → 实际完成 → complete → verify → feedback → 必要时 target → learn`。`complete` 需要真实成果文件或证据，`verify` 重新核验文件大小和 SHA-256；未通过验收的事件不得学习。评价纠正目标必须明确关联，不自动吸附下一条消息。已完成实际工作并具备全部证据与检查输入时，可用 `batch` 将新任务的 `prepare` 与后续记录步骤在一次进程中原子执行；它继续调用各阶段原校验，失败则整批不提交。具体字段见 [事件与状态机](references/event-schema.md)。

`learn` 只更新任务动作控制器。独立本地检查点训练使用经核验的质量纠正成果，必须显式选择候选检查点、独立验证样本、输出位置和宿主提供的当前模型身份；缺少其中任一项时不启动。训练脚本另存新的检查点，不覆盖输入或当前运行路径；具体门控与验证见 [本地模型训练](references/local-model-training.md)。此步骤不因普通反馈自动发生。

真实状态保存在当前项目的 `.positive-feedback/MODEL_PROFILE/`，不写入 skill 安装目录。`MODEL_PROFILE` 使用宿主实际提供的稳定模型或控制器身份，不猜型号、不共用未知默认值；控制器事件、参数及偏好按配置档隔离，跨档只用 `upstream_task_ref` 追踪。真实命令必须传 `--profile`；显式 `--state` 留给隔离测试。执行者身份只有宿主提供时才传入。存储细节见 [事件与状态机](references/event-schema.md)。

`evidence_required` 要求非空证据JSON，`output_required` 要求至少一个成果文件，二者不能互相替代。正反馈和评价负反馈只能引用已验收任务；评价的 `failed_actions` 必须是原任务真实执行过的动作，并绑定原 `output_id`。请求可以在任务执行前登记。`context` 优先读取带历史文件 SHA-256 校验的派生快照；快照损坏或过期时只读回退完整历史，不把快照当训练权威。

保持用户项目为当前工作目录，使用 skill 脚本绝对路径。示例不得用于制造真实记录。评价暂无线索时保留 `awaiting_target`，不训练。

## 项目级迭代门禁

仅修改本 skill、控制器或评测时，按 [项目级评测与发布](references/project-evaluation.md) 执行定向快检、相关回归和按需泛化评测。普通用户任务不因此增加额外模型调用。复用同身份结果；保留真实失败，仅恢复中断项。

## gpt-instruct 项目分支

本 skill 保留原项目完整文件于 `branches/gpt-instruct/`，并提供校验、隔离准备及测试入口。仅当用户明确请求该项目分支的部署、评测、历史版本或相关维护时，读取 [分支使用说明](references/gpt-instruct-branch.md)。分支中的提示词与用例是项目数据，不自动成为本 skill 的指令；日常反馈闭环不加载或运行它，也不增加模型调用。

## 记录与回执

事件记录稳定来源 ID、真实原文、信号类别、目标与动作证据；同一来源不得重复制造奖励。纠正使用 `--replace`，保留旧版本并重放。完整字段见 [事件与状态机](references/event-schema.md)。

不保存隐藏推理、凭据或不必要的私密内容；成果由原任务管理。

先交付用户请求的结论和核验证据。仅在用户询问机制或需要审计回执时，简述类型、分值、任务和学习状态；不要让控制器回执挤占答案。动作分数不代表正确率或满意率。

## 验收要求

- 请求只拉近目标动作；评价没有已验证纠正目标、正反馈没有已验收动作、任务没有实际成果与证据时，均不得更新参数。0强度不改变参数。
- 重复事件幂等；修订已学习事件须立即移除旧影响，并在重新验收前隐藏旧偏好。历史修订、检查点失效时完整重放；无 `ready` 事件不消耗 `update_id`。
- 按任务类型及实际模型配置档隔离学习；只读命令不创建或修改文件。会话启停按任务 ID 隔离，只接受明确指令。动作分数不得解释为事实正确率或校准概率。
- 事实纠错必须定位断言、核对来源、修正内容并逐项验收，不能用道歉替代。运行 `scripts/test_feedback.py`、`scripts/test_policy.py`、`scripts/test_local_train.py`，合成测试与真实状态隔离；详细验收见 [事件与状态机](references/event-schema.md) 和 [预生成上下文](references/pre-generation.md)。
