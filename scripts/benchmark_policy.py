"""Repeatable synthetic benchmark for positive-feedback policy operations."""
import argparse
import json
import statistics
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch
from pathlib import Path

from policy import (fresh_state, initial, learn, load, pre_generation_context,
                    read_context_snapshot, refresh_context_cache, replay, save,
                    session_command, training_integrity)


def percentile95(values):
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, int(.95 * len(ordered) + .999999) - 1))]


def measure(operation, repeats, warmups=1):
    for _ in range(warmups):
        operation()
    samples = []
    for _ in range(repeats):
        started = time.perf_counter()
        operation()
        samples.append((time.perf_counter() - started) * 1000)
    return {"median_ms": statistics.median(samples), "p95_ms": percentile95(samples)}


def save_without_snapshot(path, state):
    with patch("policy.write_context_snapshot"):
        save(path, state)


def synthetic_state(event_count, task_count=1):
    state = fresh_state()
    task = {
        "task_id": "synthetic-writing-0",
        "task_kind": "writing",
        "required_actions": ["edit_content"],
        "target_spec": "isolated synthetic benchmark",
        "target_source_id": "synthetic-source",
        "flags": {"artifact_present": True, "evidence_required": True,
                  "revision_requested": True, "output_required": True},
        "status": "verified",
        "actual_actions": ["edit_content", "validate_requirements", "deliver_artifact"],
        "output_id": "synthetic-output",
        "artifacts": [],
        "action_evidence": {"synthetic": True},
        "verification_checks": {"synthetic": True},
        "verified_at": "2000-01-01T00:00:00+00:00",
        "attempts": [],
    }
    for index in range(task_count):
        state["tasks"].append({**task, "task_id": f"synthetic-writing-{index}"})
    for index in range(event_count):
        task_id = state["tasks"][index % task_count]["task_id"]
        state["events"].append({
            "event_id": f"synthetic-{index}", "kind": "positive", "score": .8,
            "label": "synthetic", "quote": "synthetic benchmark event",
            "task_id": task_id, "source_id": f"synthetic-source-{index}",
            "previous_output_id": task["output_id"], "aspect": "", "failed_actions": [],
            "target_spec": "", "target_source_id": "", "target_actions": [],
            "target_task_id": "", "evaluation_mode": "", "quality_requirement": "",
            "status": "learned", "update_id": "synthetic-update", "revisions": [],
            "action_evidence": {"synthetic": True},
        })
    state["params"] = initial()
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", default="0,100,1000,10000")
    parser.add_argument("--repeats", type=int, default=7)
    parser.add_argument("--tasks", type=int, default=1,
                        help="number of distinct synthetic tasks sharing events")
    args = parser.parse_args()
    sizes = [int(value) for value in args.sizes.split(",")]
    if args.repeats < 2 or args.tasks < 1 or any(size < 0 for size in sizes):
        parser.error("use at least two repeats and non-negative sizes")

    script = Path(__file__).with_name("policy.py")
    startup = measure(
        lambda: subprocess.run([sys.executable, "-X", "utf8", str(script), "--help"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True),
        args.repeats)
    rows = []
    with tempfile.TemporaryDirectory(prefix="positive-feedback-benchmark-") as raw:
        directory = Path(raw)
        session_path = directory / "benchmark-session.json"
        empty_state_path = directory / "benchmark-empty-state.json"
        session_command("enable", "benchmark-thread", session_path, "isolated benchmark")
        cli_prefix = [sys.executable, "-X", "utf8", str(script), "--thread-id",
                      "benchmark-thread", "--session-state", str(session_path)]
        context_prefix = cli_prefix + ["--state", str(empty_state_path)]
        def run_cli(command):
            subprocess.run(command, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, check=True)
        combined_cli = measure(lambda: run_cli(context_prefix + [
            "session-context", "--compact", "--task-kind", "general"]), args.repeats)
        model_command = context_prefix + ["session-context", "--for-model", "--task-kind", "general"]
        model_cli = measure(lambda: run_cli(model_command), args.repeats)
        compact_output = subprocess.run(context_prefix + ["session-context", "--compact",
                                                            "--task-kind", "general"],
                                        capture_output=True, check=True).stdout
        model_output = subprocess.run(model_command, capture_output=True, check=True).stdout
        separate_cli = measure(lambda: (
            run_cli(cli_prefix + ["session", "status"]),
            run_cli(context_prefix + ["context", "--compact", "--task-kind", "general"])),
            args.repeats)
        for size in sizes:
            state = synthetic_state(size, args.tasks)
            state_path = directory / f"state-{size}.json"
            state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            loaded = load(state_path)
            context = pre_generation_context(
                loaded, "writing", "inspect_target", artifact_present=True,
                evidence_required=True, revision_requested=True, output_required=True)
            compact = pre_generation_context(
                loaded, "writing", "inspect_target", artifact_present=True,
                evidence_required=True, revision_requested=True, output_required=True,
                compact=True)
            refresh_context_cache(loaded)
            loaded["params"], _ = replay(loaded)
            loaded["training_integrity"] = training_integrity(loaded)
            save(state_path, loaded)
            cached_context = pre_generation_context(
                loaded, "writing", "inspect_target", artifact_present=True,
                evidence_required=True, revision_requested=True, output_required=True)
            rows.append({
                "event_count": size,
                "state_bytes": state_path.stat().st_size,
                "startup": startup,
                "read": measure(lambda: load(state_path), args.repeats),
                "replay": measure(lambda: replay(loaded, task_kind="writing"), args.repeats),
                "empty_learn": measure(lambda: learn(loaded, "no-ready-events"), args.repeats),
                "context_cached": measure(
                    lambda: pre_generation_context(loaded, "writing", "inspect_target",
                                                   artifact_present=True, evidence_required=True,
                                                   revision_requested=True, output_required=True),
                    args.repeats),
                "snapshot_read": measure(
                    lambda: read_context_snapshot(state_path, "writing"), args.repeats),
                "save_with_snapshot": measure(
                    lambda: save(state_path, loaded), args.repeats),
                "save_without_snapshot": measure(
                    lambda: save_without_snapshot(state_path, loaded), args.repeats),
                "context_serialization": measure(
                    lambda: json.dumps(context, ensure_ascii=False, allow_nan=False), args.repeats),
                "context_chars": len(json.dumps(context, ensure_ascii=False, allow_nan=False)),
                "compact_chars": len(json.dumps(compact, ensure_ascii=False, allow_nan=False)),
                "cache_hit": cached_context["context_cache_hit"],
            })
    print(json.dumps({
        "benchmark_version": 2,
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "repeats": args.repeats,
        "task_count": args.tasks,
        "cli_active_combined": combined_cli,
        "cli_active_model": model_cli,
        "cli_active_separate": separate_cli,
        "empty_context_output_bytes": {"compact_json": len(compact_output),
                                       "for_model": len(model_output)},
        "synthetic_only": True,
        "tool_roundtrip_ms": None,
        "model_prompt_processing_ms": None,
        "rows": rows,
    }, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
