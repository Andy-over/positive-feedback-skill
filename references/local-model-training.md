# 独立本地检查点训练

`scripts/train_local_model.py` 使用本地 PyTorch 和 Transformers，对用户明确选定的本地 causal-LM 检查点做全参数反向传播。它只接受本地 `safetensors` 检查点和 tokenizer，禁用远程加载及远程自定义代码；训练结果保存为新的目录，不覆盖输入。该路径与 `policy.py learn` 的620参数动作控制器分开。

训练清单和独立验证清单均为JSONL，每行包含 `event_id`、`task_id`、`artifact_path`、`prompt`。事件须为未被修订取代的已学习 `quality` 评价，有对应更新记录，并指向已验收的纠正任务；`artifact_path` 必须属于该任务记录的成果，当前SHA-256与验收时一致，事件动作证据必须匹配该任务输出。`prompt` 的SHA-256必须等于任务验收检查中的 `training_prompt_sha256`，且 `training_example_approved` 为 `true`。成果文件的UTF-8文本作为目标 completion。两份清单不可重复事件或完全相同的 prompt-completion 对。这样训练目标来自实际验收成果，而不是评价词、道歉文本或未经核对的草稿。使用者应先确认成果确实适合语言模型训练，并处理敏感数据与许可问题。

示例（只说明接口，不会自动启动）：

```powershell
python C:\Users\LENOVO\.codex\skills\positive-feedback\scripts\train_local_model.py --state STATE.json --manifest TRAIN.jsonl --validation-manifest HOLDOUT.jsonl --checkpoint LOCAL-CANDIDATE --output NEW-CHECKPOINT --active-model-id HOST-MODEL-ID --candidate-model-id CANDIDATE-ID --steps 1
```

调用方必须从宿主获取当前模型身份；未知时不要猜测并启动。脚本拒绝相同模型身份、相同当前检查点路径、输出路径覆盖或嵌套输入/当前检查点、缺失本地权重、未学习事件、未验收任务和成果哈希变化。若宿主能提供当前检查点路径，再传 `--active-checkpoint` 执行路径级检查。默认权重文件大小上限为512 MiB；处理更大检查点须显式提高 `--max-checkpoint-bytes`，并先确认内存、优化器状态与保存空间足够。输出先写入独立暂存目录，训练成功后才改名为目标目录；失败时原检查点不变，暂存目录留待检查。

训练前后对独立验证清单计算目标文本损失；默认验证损失上升就拒绝保存检查点，可用 `--max-validation-regression` 明确设置容差。训练时不再复制整套参数用于比较，避免额外一份权重常驻内存。验证损失只是代理指标，不等于事实准确率。仅在用户明确要求针对某个可访问的候选模型训练，且模型身份、资源、数据和输出位置齐备时执行。此技能不会下载权重、调用付费微调API、自动训练每次反馈，也不会把新检查点自动切换为 Codex 当前模型。真实效果仍需比较训练前后在独立任务集上的事实准确率、要求覆盖与返工。本机无候选检查点时只能验证极小合成模型上的训练路径，不能声称已训练用户模型。
