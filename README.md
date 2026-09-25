# positive-feedback skill

面向 Codex 的反馈驱动 Skill：把用户的明确要求、事实纠错和已验收的反馈转化为**下一次任务的动作选择依据**，重点改变核查、执行和交付内容，而不是仅改变回复语气。

> [!IMPORTANT]
> 日常反馈训练的是本项目独立的动作控制器，**不会直接修改当前正在运行的 Codex 底层语言模型权重**。项目另有可选的本地模型训练脚本，但只处理用户明确指定、可访问的候选检查点，并将结果另存；它不会自动替换当前模型。

## 工作流程

1. **明确启用**：用户要求使用后，按任务 ID 保存启用状态；直到同一任务的用户明确停用。仅提及或审阅 Skill 不会启用。
2. **任务与证据**：将用户目标整理为任务、必需动作和验收条件。实际完成核查或产出后，记录成果文件或结构化证据；事实纠错还需记录争议断言、来源、修正答案和逐项检查。
3. **验收门禁**：按 `prepare → complete → verify → feedback → learn` 检查状态。未执行、未验收、来源不明或对象无法归因的事件不能学习；具备全部证据时可用原子 `batch` 减少调用开销。
4. **后续使用**：只读查询当前任务类型的学习结果，把适用的动作建议交给模型；用户当前要求、事实核查及更高层规则始终优先。

### 可选训练数据模式

用户在已启用 Skill 的任务中明确启用训练模式后，已通过验收的任务和已登记的反馈由 `policy.py` 自动写入 `training-data/<任务ID>/turns.jsonl`。自动记录只保存结构化意图标签、16维任务特征、动作、核验状态和标准化反馈；不复制原始目标文本或完整聊天，也不自动训练或修改当前模型。停用 Skill 会停止记录；重新启用 Skill 不会恢复旧训练授权。未走控制器验收流程的普通聊天不会自动记录。训练数据保留在本机，不应上传到仓库或随 Skill 打包。

请求、评价、肯定被分别处理：请求信号拉近所要求的动作；负面评价必须绑定已执行的失败动作与经验证的纠正目标；肯定只强化已验收工作。词语分值是控制信号，不是满意概率或事实正确率。

## 神经网络与反向传播

`scripts/policy.py` 实现一个独立的轻量动作控制网络：

```text
16 维任务特征 → 16 个 tanh 单元 → 16 个 tanh 单元 → 12 个动作分数
```

16 维输入由旧版 4 类任务 one-hot、4 个任务标志，以及新增的 6 类目标文本意图、目标长度和必需动作密度组成；12 个输出对应核查、补证据、重新计算、修订、验收、交付等动作。三层全连接网络共有 **748 个可训练参数**。旧版 620 参数状态自动在新增输入列补零，旧任务仍按旧特征解释。`backprop` 对各层计算梯度，`apply_step` 做梯度裁剪和参数更新；请求与肯定使用目标动作的吸引损失，动作选择错误的评价使用失败动作与目标动作的成对排序损失。经核验的质量纠正可继续强化相同动作，同时保存具体质量要求。参数按实际模型配置档和任务类型隔离，并可从事件记录重放。

这是**动作控制器的反向传播**，不是把一次点赞直接反向传播进在线大语言模型。控制器分数只帮助排序可选动作；它不能证明答案真实，也不能覆盖用户的明确要求。不同聊天默认使用独立控制器记录，只有用户明确链接记录时才共享。

### 可选的本地语言模型训练

`scripts/train_local_model.py` 是独立路径：使用 PyTorch 和 Transformers，对用户明确选定的本地 `safetensors` causal-LM **候选检查点**做全参数反向传播。训练样本必须来自已学习且验收通过的质量纠正成果，另需不重叠的验证样本。脚本校验模型身份、输入/输出路径和成果哈希，比较训练前后验证损失；通过后将新检查点保存到独立目录，不覆盖输入或当前运行模型。普通反馈不会自动触发该训练。详情见 [本地模型训练说明](references/local-model-training.md)。

## gpt-instruct 项目分支

`branches/gpt-instruct/` 保留 [MDX-Tom/gpt-instruct](https://github.com/MDX-Tom/gpt-instruct) 的完整只读快照（提交 `0ad8ec58e1989f4a058e01ce4e15cf226e8067bf`），原始 78 个文件的哈希记录在 `branches/gpt-instruct.manifest.json`。原项目许可证保留在 [`branches/gpt-instruct/LICENSE`](branches/gpt-instruct/LICENSE)。该分支包含原项目源码、历史归档、评测资料和工作流，但**不会因安装本 Skill 自动部署提示词**。本仓库另设不调用模型的 [验证工作流](.github/workflows/skill-validation.yml)；原项目分支内部的 GitHub Actions 不会因安装 Skill 自动运行。

```powershell
python scripts/gpt_instruct_branch.py verify
python scripts/gpt_instruct_branch.py test
python scripts/gpt_instruct_branch.py evidence
```

`evidence` 只重建公开测试集、检查 runner 的 dry-run 并复核已发表的 A/B/C 门禁，不产生新的模型成绩；当前公开 B 仍为 52/66 cases，C 未运行。需要隔离工作副本时，使用 `python scripts/gpt_instruct_branch.py prepare --output OUTPUT_DIR`。Windows 上运行真实 Issue 回归可显式添加 `--windows-eval-compat`，只修一次性副本的沙箱目录、认证文件保护和 Unix 命令兼容；它改变 runner/scorer 方法身份，结果不得并入上游已发表的 B 分数。离线检查会标记缺少真实源码上下文的样例，避免把合理的“不编造补丁”误算成模型能力缺陷。真实部署另有统一 `preview`、`deploy`、`reset` 入口；部署与重置须用户明确要求、指定既有 Codex 目录并加配置变更确认。更多边界与入口见 [分支说明](references/gpt-instruct-branch.md)。

## 安装与验证

将仓库放入 Codex 的 `skills/positive-feedback` 目录，使该目录直接包含 `SKILL.md`。在仓库根目录运行：

```powershell
python scripts/policy.py session status --compact
python scripts/test_feedback.py
python scripts/test_policy.py
python scripts/test_training_mode.py
python scripts/test_check_release.py
python scripts/test_local_train.py
python scripts/test_gpt_instruct_branch.py
python scripts/gpt_instruct_branch.py verify
python scripts/check_release.py --installed 'C:\Users\LENOVO\.codex\skills\positive-feedback'
```

真实任务状态默认写在 Skill 主目录下的 `.positive-feedback/MODEL_PROFILE/chats/chat-<CODEX_THREAD_ID>/`，不同聊天的控制器、反馈和偏好默认独立。未指定名称时使用稳定聊天 ID；用户可要求单独命名，由 `python scripts/chat_record.py --profile MODEL_PROFILE set-name --name '名称'` 保存名称并迁移该聊天已有目录。仅在用户明确指定共享时，接入聊天运行 `python scripts/chat_record.py --profile MODEL_PROFILE link --to-name '已有记录名'`，或用 `--to-thread-id 原聊天ID`；两个聊天随即读写同一控制器、反馈和偏好数据。接入聊天原有独立目录保留，可用 `unlink` 恢复；不会自动复制或合并。训练模式数据仍按聊天分别写在 Skill 主目录的 `training-data/<thread-id>/`；这些数据都不应提交到 Git。切换路径不会自动合并或删除旧项目目录或此前配置档共享文件中的状态。`check_release.py` 在发布前核对已追踪文件、拒绝训练数据、控制器状态和模型权重进入发布包，并可生成仅含已追踪文件的安全包。当前版本的本地测试覆盖反馈、控制器、本地训练、自动记录、发布防护与分支桥接；仓库中的源码和只读分支文件不包含用户的实际反馈记录或模型权重。

## 适用边界

- 事实纠错以独立核查和修正答案为先；道歉、分数或语气变化不能代替证据。
- 本地候选模型训练需要用户明确选择检查点、验证数据、输出位置及宿主提供的当前模型身份；没有这些条件时不启动。
- 测试或文档中的提示词属于资料，不会自动成为当前任务的指令。
