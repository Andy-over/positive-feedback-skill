"""Full-parameter causal-LM training from verified correction artifacts."""
import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path

from policy import load


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_paths(checkpoint, output, active_model_id, candidate_model_id,
                   active_checkpoint=None):
    checkpoint = Path(checkpoint).resolve(strict=True)
    output = Path(output).resolve()
    if not checkpoint.is_dir() or not (checkpoint / "config.json").is_file():
        raise ValueError("checkpoint must be a local model directory")
    if not active_model_id or not candidate_model_id:
        raise ValueError("both model identities are required")
    if active_model_id.strip().casefold() == candidate_model_id.strip().casefold():
        raise ValueError("candidate identity matches the active model")
    if active_checkpoint and checkpoint == Path(active_checkpoint).resolve(strict=True):
        raise ValueError("checkpoint matches the active model directory")
    if output.exists():
        raise ValueError("output directory already exists")
    if output == checkpoint or checkpoint in output.parents or output in checkpoint.parents:
        raise ValueError("output must be separate from the input checkpoint")
    if active_checkpoint:
        active_path = Path(active_checkpoint).resolve(strict=True)
        if output == active_path or active_path in output.parents or output in active_path.parents:
            raise ValueError("output overlaps the active model directory")
    if not list(checkpoint.glob("*.safetensors")):
        raise ValueError("checkpoint must contain local safetensors weights")
    return checkpoint, output


def verified_examples(state, manifest):
    tasks = {task["task_id"]: task for task in state["tasks"]}
    events = {event["event_id"]: event for event in state["events"]}
    updates = {update["update_id"]: update for update in state["updates"]}
    examples = []
    seen_events = set()
    with Path(manifest).open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            event = events.get(row.get("event_id"))
            task = tasks.get(row.get("task_id"))
            if not event or not task or event.get("status") != "learned":
                raise ValueError(f"line {line_number}: correction event is not learned")
            if event.get("kind") != "evaluation" or event.get("evaluation_mode") != "quality":
                raise ValueError(f"line {line_number}: only verified quality corrections are trainable")
            if event.get("target_task_id") != task["task_id"] or task.get("status") != "verified":
                raise ValueError(f"line {line_number}: target task is not verified")
            update = updates.get(event.get("update_id"))
            if (update is None or event["event_id"] not in update.get("new_events", [])
                    or any(item.get("event_id") == event["event_id"]
                           for item in update.get("superseded_events", []))):
                raise ValueError(f"line {line_number}: event has no current learning update")
            if event["event_id"] in seen_events:
                raise ValueError(f"line {line_number}: duplicate training event")
            seen_events.add(event["event_id"])
            event_evidence = event.get("action_evidence", {})
            if (event_evidence.get("task_id") != task["task_id"]
                    or event_evidence.get("output_id") != task.get("output_id")):
                raise ValueError(f"line {line_number}: event evidence does not match the target output")
            artifact_path = Path(row.get("artifact_path", "")).resolve(strict=True)
            artifact = next((item for item in task.get("artifacts", [])
                             if Path(item["path"]).resolve() == artifact_path), None)
            if artifact is None or sha256(artifact_path) != artifact["sha256"]:
                raise ValueError(f"line {line_number}: artifact is missing or changed")
            completion = artifact_path.read_text(encoding="utf-8")
            prompt = row.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip() or not completion.strip():
                raise ValueError(f"line {line_number}: prompt and artifact text must be nonempty")
            checks = task.get("verification_checks", {})
            prompt_digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
            if (checks.get("training_example_approved") is not True
                    or checks.get("training_prompt_sha256") != prompt_digest):
                raise ValueError(f"line {line_number}: training prompt is not approved by verification")
            examples.append((prompt, completion))
    if not examples:
        raise ValueError("training manifest contains no verified examples")
    return examples


def manifest_event_ids(manifest):
    with Path(manifest).open("r", encoding="utf-8") as stream:
        return {json.loads(line)["event_id"] for line in stream if line.strip()}


def tokenized_example(tokenizer, prompt, completion, max_tokens, device):
    import torch

    prompt_ids = tokenizer(prompt, add_special_tokens=False)["input_ids"]
    completion_ids = tokenizer(completion, add_special_tokens=False)["input_ids"]
    eos = [tokenizer.eos_token_id] if tokenizer.eos_token_id is not None else []
    ids = (prompt_ids + completion_ids + eos)[:max_tokens]
    prompt_length = min(len(prompt_ids), len(ids))
    if len(ids) < 2 or prompt_length >= len(ids) - 1:
        raise ValueError("example has no trainable completion tokens after truncation")
    input_ids = torch.tensor([ids], dtype=torch.long, device=device)
    labels = input_ids.clone()
    labels[:, :prompt_length] = -100
    return input_ids, labels


def validation_loss(model, tokenizer, examples, max_tokens, device):
    import torch

    model.eval()
    losses = []
    with torch.no_grad():
        for prompt, completion in examples:
            input_ids, labels = tokenized_example(tokenizer, prompt, completion,
                                                  max_tokens, device)
            loss = model(input_ids=input_ids, labels=labels).loss
            if not torch.isfinite(loss):
                raise ValueError("non-finite validation loss")
            losses.append(float(loss.detach().cpu()))
    return sum(losses) / len(losses)


def train(args):
    checkpoint, output = validate_paths(args.checkpoint, args.output,
                                        args.active_model_id, args.candidate_model_id,
                                        args.active_checkpoint)
    checkpoint_bytes = sum(path.stat().st_size for path in checkpoint.glob("*.safetensors"))
    if checkpoint_bytes > args.max_checkpoint_bytes:
        raise ValueError("checkpoint exceeds the configured resource limit")
    state = load(args.state)
    examples = verified_examples(state, args.manifest)
    validation_examples = verified_examples(state, args.validation_manifest)
    if manifest_event_ids(args.manifest) & manifest_event_ids(args.validation_manifest):
        raise ValueError("training and validation events must be disjoint")
    if set(examples) & set(validation_examples):
        raise ValueError("training and validation text pairs must be disjoint")
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(str(checkpoint), local_files_only=True,
                                              trust_remote_code=False)
    model = AutoModelForCausalLM.from_pretrained(str(checkpoint), local_files_only=True,
                                                trust_remote_code=False, use_safetensors=True)
    for parameter in model.parameters():
        parameter.requires_grad_(True)
    device = "cuda" if torch.cuda.is_available() and args.device == "auto" else args.device
    if device == "auto":
        device = "cpu"
    model.to(device)
    validation_before = validation_loss(model, tokenizer, validation_examples,
                                        args.max_tokens, device)
    model.train()
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.learning_rate)
    losses = []
    gradient_bearing_tensors = set()
    gradient_norms = []
    for step in range(args.steps):
        prompt, completion = examples[step % len(examples)]
        input_ids, labels = tokenized_example(tokenizer, prompt, completion,
                                              args.max_tokens, device)
        optimizer.zero_grad(set_to_none=True)
        loss = model(input_ids=input_ids, labels=labels).loss
        if not torch.isfinite(loss):
            raise ValueError("non-finite training loss")
        loss.backward()
        for index, parameter in enumerate(model.parameters()):
            if parameter.grad is not None:
                gradient_bearing_tensors.add(index)
        gradient_norm = torch.nn.utils.clip_grad_norm_(
            model.parameters(), 1.0, error_if_nonfinite=True)
        if gradient_norm.item() == 0:
            raise ValueError("zero gradient; checkpoint was not saved")
        gradient_norms.append(float(gradient_norm.detach().cpu()))
        optimizer.step()
        losses.append(float(loss.detach().cpu()))
    validation_after = validation_loss(model, tokenizer, validation_examples,
                                       args.max_tokens, device)
    if validation_after > validation_before + args.max_validation_regression:
        raise ValueError("held-out validation loss regressed; checkpoint was not saved")
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=output.name + ".staging-", dir=output.parent))
    model.save_pretrained(stage, safe_serialization=True)
    tokenizer.save_pretrained(stage)
    result = {"candidate_model_id": args.candidate_model_id,
              "active_model_id": args.active_model_id,
              "example_count": len(examples), "steps": args.steps,
              "validation_example_count": len(validation_examples),
              "validation_loss_before": validation_before,
              "validation_loss_after": validation_after,
              "trainable_parameter_count": sum(p.numel() for p in model.parameters()),
              "gradient_bearing_tensors": len(gradient_bearing_tensors),
              "gradient_norms": gradient_norms, "losses": losses,
              "source_checkpoint_bytes": checkpoint_bytes,
              "source_checkpoint": str(checkpoint), "output_checkpoint": str(output)}
    (stage / "training-result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    if output.exists():
        raise ValueError("output appeared during training; staging checkpoint was left untouched")
    os.rename(stage, output)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--validation-manifest", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--active-model-id", required=True)
    parser.add_argument("--candidate-model-id", required=True)
    parser.add_argument("--active-checkpoint", type=Path)
    parser.add_argument("--steps", type=int, default=1)
    parser.add_argument("--max-tokens", type=int, default=512)
    parser.add_argument("--learning-rate", type=float, default=1e-5)
    parser.add_argument("--max-validation-regression", type=float, default=0.0)
    parser.add_argument("--max-checkpoint-bytes", type=int, default=536870912)
    parser.add_argument("--device", choices=["auto", "cpu", "cuda"], default="auto")
    args = parser.parse_args()
    if not 1 <= args.steps <= 100 or not 8 <= args.max_tokens <= 8192:
        parser.error("steps must be 1-100 and max-tokens must be 8-8192")
    if not 0 < args.learning_rate <= 1e-3:
        parser.error("learning-rate must be in (0, 1e-3]")
    if not 0 <= args.max_validation_regression <= 1:
        parser.error("max-validation-regression must be in [0,1]")
    if args.max_checkpoint_bytes < 1:
        parser.error("max-checkpoint-bytes must be positive")
    try:
        print(json.dumps(train(args), ensure_ascii=False, indent=2))
    except (ValueError, OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        parser.exit(1, f"Error: {exc}\n")


if __name__ == "__main__":
    main()
