# 回答前上下文与宿主集成

## 能力边界

显式调用 skill 时，Codex 宿主可在模型生成前把完整 `SKILL.md` 作为 skill 输入加入本轮上下文，因此这里的静态工作流约束能从回答开始生效。

本地脚本保存的动态偏好不会自行进入已经发出的网络请求。skill 不能拦截、改写或重发用户原始消息，也不能改变宿主的系统或开发者指令。要在 `turn/start` 前加入动态偏好，必须由控制 Codex App Server 的客户端先运行本地 `context` 命令，再把输出作为额外输入项提交。

## 原生 Codex 回答流程

当机制处于 `active`，或用户明确要求本轮应用历史偏好时：

1. 读取当前消息，确定 `task_kind`、明确要求的动作和四个任务标志。
2. 在撰写实质性答案前运行：

```powershell
$PositiveFeedbackPolicy = 'C:\Users\LENOVO\.codex\skills\positive-feedback\scripts\policy.py'
python $PositiveFeedbackPolicy --profile MODEL_PROFILE context --for-model --task-kind TASK_KIND --required-actions ACTIONS [FLAGS]
```

如本轮尚未单独查询会话状态，且已知实际 `MODEL_PROFILE`，可将命令换为 `--profile MODEL_PROFILE session-context --for-model --task-kind TASK_KIND --required-actions ACTIONS [FLAGS]`。未启用时只返回 `active: false`，不读取控制器；启用时返回状态、skill SHA-256 与一份完整指导。程序需要结构化字段时沿用 `--compact` JSON；旧消费方需要 ASCII 转义可在子命令前加 `--ascii-output`。

3. 模型阅读模式使用 `guidance`；结构化 JSON 模式使用 `instruction_text` 与 `preferred_actions`。
4. 当前用户要求、事实、安全规则和验收标准始终优先。
5. 继续正常执行任务；需要记录时再进入 `prepare → complete → verify → feedback → learn` 状态机。

保持项目目录为当前工作目录；不要切换到 skill 目录。`context` 是完全只读操作，不获取写锁，不创建 `.positive-feedback` 目录或锁文件。它使用相同任务类型的已学习历史派生参数，与固定初始化控制器比较动作分数，并输出正向动作差值；没有同类型已学习事件时 `preferred_actions` 为空。

`--compact` 只返回生成所需的 `instruction_text`、必需和可选动作、适用偏好与质量要求；不带标志时保留完整分数、差值和诊断元数据，默认行为不变。写操作原子生成分类参数快照，读操作用主状态文件 SHA-256 校验后使用；快照缺失、损坏或过期时回退完整状态，并以事件内容摘要验证分类缓存，必要时只在内存重放。偏好始终从独立文件读取。只读命令不写目录、锁或快照；事件修订后旧快照与旧缓存均失效。

当当前消息指出事实错误时，增加 `--factual-correction`；返回的 `content_checks` 明确要求追踪争议断言、核对来源、修正或限定结论，不把语气变化当作处理结果。详情见 [内容核验](content-correction.md)。

输出还可包含：

- `explicit_preferences`：作用范围与当前任务匹配、仍处于启用和有效期内的明确诉求，最多6条。
- `quality_requirements`：同任务类型下经纠正任务验证的质量要求，最多4条。

两者合计最多1600字符。输出不包含反馈原文、隐藏推理、凭据或完整本地状态。事件被修订后，旧参数影响立即从有效重放中移除。

## 自定义 App Server 客户端

若客户端需要在模型开始本轮生成之前加入动态偏好：

1. 在本地运行 `context` 并解析JSON。
2. 构造 `turn/start` 时同时提交用户文本、明确的 `skill` 输入项，以及标记为本地偏好上下文的额外文本输入。
3. 额外文本只使用 `instruction_text`、`required_actions` 和 `preferred_actions`，不要上传完整本地状态、事件、文件路径或反馈原文。
4. 若本地命令失败，继续使用原始用户请求，不得声称偏好已经应用。

客户端集成属于宿主功能，不由本 skill 自动安装或修改。
