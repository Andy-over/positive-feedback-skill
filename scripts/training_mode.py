"""Opt-in, per-thread, minimized training-data journal for positive-feedback."""

import argparse
import hashlib
import json
import os
import re
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from policy import ACTIONS, SKILL_ROOT, TASK_KINDS, THREAD_PATTERN, session_command

DATA_ROOT = SKILL_ROOT / "training-data"
TURN_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
PROFILE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SECRET_PATTERN = re.compile(
    r"(?:-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
    r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,}|"
    r"AKIA[0-9A-Z]{16})\b|"
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b)"
)
FIELDS = {"turn_id", "task_kind", "task_summary", "response_summary", "actions",
          "outcome", "verification", "feedback", "model_profile", "origin"}
REQUIRED = {"turn_id", "task_kind", "task_summary", "response_summary", "actions",
            "outcome", "verification"}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def text_field(value, name, limit, required=True):
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    value = re.sub(r"\s+", " ", value).strip()
    if (required and not value) or len(value) > limit:
        raise ValueError(f"{name} must contain 1-{limit} characters" if required
                         else f"{name} must contain at most {limit} characters")
    if SECRET_PATTERN.search(value):
        raise ValueError(f"{name} appears to contain a secret or contact detail")
    return value


def validate_entry(entry):
    if not isinstance(entry, dict) or not REQUIRED <= entry.keys() or entry.keys() - FIELDS:
        raise ValueError("entry has missing or unsupported fields")
    turn_id = text_field(entry["turn_id"], "turn_id", 128)
    if not TURN_PATTERN.fullmatch(turn_id):
        raise ValueError("invalid turn_id")
    if entry["task_kind"] not in TASK_KINDS:
        raise ValueError("invalid task_kind")
    if entry["outcome"] not in ("verified", "partial", "failed", "unknown"):
        raise ValueError("invalid outcome")
    actions = entry["actions"]
    if (not isinstance(actions, list) or len(actions) > len(ACTIONS)
            or any(not isinstance(action, str) for action in actions)
            or len(actions) != len(set(actions))):
        raise ValueError("actions must be a unique list")
    if any(action not in ACTIONS for action in actions):
        raise ValueError("unknown action")
    feedback = entry.get("feedback", {"kind": "none", "summary": ""})
    if (not isinstance(feedback, dict) or set(feedback) != {"kind", "summary"}
            or feedback["kind"] not in ("positive", "evaluation", "request", "none")):
        raise ValueError("invalid feedback")
    summary = text_field(feedback["summary"], "feedback.summary", 300, required=False)
    if feedback["kind"] == "none" and summary:
        raise ValueError("feedback summary must be empty when kind is none")
    profile = entry.get("model_profile", "")
    if not isinstance(profile, str) or (profile and not PROFILE_PATTERN.fullmatch(profile)):
        raise ValueError("invalid model_profile")
    origin = entry.get("origin", "manual")
    if origin not in ("manual", "policy.verify", "policy.feedback"):
        raise ValueError("invalid origin")
    return {
        "turn_id": turn_id,
        "task_kind": entry["task_kind"],
        "task_summary": text_field(entry["task_summary"], "task_summary", 800),
        "response_summary": text_field(entry["response_summary"], "response_summary", 800),
        "actions": actions,
        "outcome": entry["outcome"],
        "verification": text_field(entry["verification"], "verification", 500),
        "feedback": {"kind": feedback["kind"], "summary": summary},
        "model_profile": profile,
        "origin": origin,
    }


def paths(thread_id, data_dir=None):
    if not thread_id or not THREAD_PATTERN.fullmatch(thread_id):
        raise ValueError("stable host thread ID is required")
    root = Path(data_dir) if data_dir is not None else DATA_ROOT
    folder = root / thread_id
    if (root.is_symlink() or folder.is_symlink() or
            (folder / "mode.json").is_symlink() or (folder / "turns.jsonl").is_symlink()):
        raise ValueError("training-data path must not be a symlink")
    return folder, folder / "mode.json", folder / "turns.jsonl"


def mode_record(path, thread_id):
    if not path.exists():
        return {"version": 1, "thread_id": thread_id, "enabled": False, "enabled_at": ""}
    record = json.loads(path.read_text(encoding="utf-8"))
    if (record.get("version") != 1 or record.get("thread_id") != thread_id
            or not isinstance(record.get("enabled"), bool)):
        raise ValueError("invalid training mode record")
    return record


@contextmanager
def lock(folder):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / ".lock"
    deadline = time.monotonic() + 5
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise TimeoutError("training-data lock timed out")
            time.sleep(.02)
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


def save_mode(path, record):
    temp = path.with_name(path.name + f".tmp.{os.getpid()}")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(record, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def mode_status(thread_id, session_state=None, data_dir=None):
    if not thread_id:
        return {"status": "unavailable", "recording": False,
                "reason": "stable host thread ID unavailable"}
    folder, mode_path, _ = paths(thread_id, data_dir)
    session = session_command("status", thread_id, session_state)
    mode = mode_record(mode_path, thread_id)
    recording = bool(session["active"] and mode["enabled"]
                     and mode.get("enabled_at", "") >= (session.get("changed_at") or ""))
    return {"status": "enabled" if recording else "disabled", "recording": recording,
            "skill_active": session["active"], "directory": str(folder)}


def set_mode(action, thread_id, source_excerpt, session_state=None, data_dir=None):
    folder, mode_path, _ = paths(thread_id, data_dir)
    excerpt = text_field(source_excerpt, "source_excerpt", 160)
    if action == "enable" and not session_command("status", thread_id, session_state)["active"]:
        raise ValueError("skill must be active before enabling training mode")
    if action == "disable" and not mode_path.exists():
        return {"status": "disabled", "recording": False, "directory": str(folder)}
    with lock(folder):
        record = mode_record(mode_path, thread_id)
        record.update(enabled=action == "enable", enabled_at=utc_now() if action == "enable" else "",
                      changed_at=utc_now(), source_excerpt=excerpt)
        save_mode(mode_path, record)
    return mode_status(thread_id, session_state, data_dir)


def record_turn(thread_id, entry, session_state=None, data_dir=None):
    entry = validate_entry(entry)
    folder, _, journal = paths(thread_id, data_dir)
    if not mode_status(thread_id, session_state, data_dir)["recording"]:
        raise ValueError("training mode is not recording")
    with lock(folder):
        if not mode_status(thread_id, session_state, data_dir)["recording"]:
            raise ValueError("training mode stopped before write")
        if journal.exists():
            with journal.open(encoding="utf-8") as handle:
                for line in handle:
                    if json.loads(line).get("turn_id") == entry["turn_id"]:
                        return {"status": "duplicate", "turn_id": entry["turn_id"], "path": str(journal)}
        row = {"version": 1, "thread_id": thread_id, "recorded_at": utc_now(), **entry}
        with journal.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    return {"status": "recorded", "turn_id": entry["turn_id"], "path": str(journal)}


def capture_policy_event(command, result, state, thread_id, session_state=None,
                         data_dir=None, profile=""):
    """Automatically journal verified policy results; never scrape chat transcripts."""
    if not thread_id or not mode_status(thread_id, session_state, data_dir)["recording"]:
        return None
    completed = []
    steps = (result.get("steps", []) if command == "batch" else
             [{"op": command, "result": result}])
    for step in steps:
        op, item = step.get("op"), step.get("result", {})
        if op == "verify" and item.get("passed") is True:
            task = next((row for row in state["tasks"] if row["task_id"] == item["task_id"]), None)
            if task is None or task["status"] != "verified":
                continue
            stable = hashlib.sha256(task["task_id"].encode("utf-8")).hexdigest()[:24]
            entry = {
                "turn_id": "task-" + stable, "origin": "policy.verify",
                "task_kind": task["task_kind"],
                "task_summary": task["target_spec"][:800],
                "response_summary": f"已核验任务；成果文件 {len(task.get('artifacts', []))} 个。",
                "actions": task.get("actual_actions", []), "outcome": "verified",
                "verification": "policy verify pass；核验记录已保存。",
                "feedback": {"kind": "none", "summary": ""}, "model_profile": profile,
            }
        elif op == "feedback" and item.get("event_id"):
            event = next((row for row in state["events"] if row["event_id"] == item["event_id"]), None)
            if event is None:
                continue
            task = next((row for row in state["tasks"] if row["task_id"] == event["task_id"]), None)
            if task is None:
                continue
            revision = len(event.get("revisions", []))
            stable = hashlib.sha256(f"{event['event_id']}:{revision}".encode("utf-8")).hexdigest()[:24]
            entry = {
                "turn_id": "feedback-" + stable, "origin": "policy.feedback",
                "task_kind": task["task_kind"], "task_summary": task["target_spec"][:800],
                "response_summary": "已登记用户反馈；不代表已学习或答案正确。",
                "actions": task.get("actual_actions", []),
                "outcome": "verified" if task["status"] == "verified" else "unknown",
                "verification": f"反馈状态 {event.get('status', 'unknown')}；修订序号 {revision}。",
                "feedback": {"kind": event["kind"],
                             "summary": str(event.get("label") or event.get("quality_requirement")
                                            or "已记录反馈信号")[:300]},
                "model_profile": profile,
            }
        else:
            continue
        try:
            completed.append(record_turn(thread_id, entry, session_state, data_dir)["status"])
        except ValueError as exc:
            completed.append("skipped: " + str(exc))
    return {"status": "captured" if completed else "no_eligible_event", "entries": completed}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--thread-id", default=os.environ.get("CODEX_THREAD_ID", ""))
    parser.add_argument("--session-state", type=Path, help="isolated test session path")
    parser.add_argument("--data-dir", type=Path, help="isolated test data directory")
    sub = parser.add_subparsers(dest="command", required=True)
    mode = sub.add_parser("mode")
    modes = mode.add_subparsers(dest="action", required=True)
    modes.add_parser("status")
    for action in ("enable", "disable"):
        modes.add_parser(action).add_argument("--source-excerpt", required=True)
    sub.add_parser("record").add_argument("--entry-json", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "mode":
            result = (mode_status(args.thread_id, args.session_state, args.data_dir)
                      if args.action == "status" else
                      set_mode(args.action, args.thread_id, args.source_excerpt,
                               args.session_state, args.data_dir))
        else:
            entry = json.loads(args.entry_json.read_text(encoding="utf-8"))
            result = record_turn(args.thread_id, entry, args.session_state, args.data_dir)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ValueError, OSError, json.JSONDecodeError, TimeoutError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
