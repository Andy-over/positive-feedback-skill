"""Evidence-gated external task-action controller with directional feedback."""
import argparse
import copy
import contextlib
import hashlib
import json
import math
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from feedback import LABELS_BY_KIND, signed_number, validate

TASK_KINDS = ["writing", "code", "data", "general"]
ACTIONS = [
    "inspect_target", "edit_content", "add_evidence", "verify_citations",
    "recalculate", "restructure", "generate_revision", "compare_versions",
    "validate_requirements", "report_gaps", "preserve_correct", "deliver_artifact",
]
ACTION_HINTS = {
    "inspect_target": "定位需要处理的内容和版本",
    "edit_content": "对目标内容执行实际编辑",
    "add_evidence": "补充可核查证据",
    "verify_citations": "核查引用与来源对应关系",
    "recalculate": "重新计算并核对结果",
    "restructure": "按目标重组内容结构",
    "generate_revision": "生成并保存修订稿",
    "compare_versions": "比较修改前后差异",
    "validate_requirements": "逐项核验明确要求",
    "report_gaps": "说明尚缺的数据或证据",
    "preserve_correct": "保留未要求修改且已正确的内容",
    "deliver_artifact": "交付可打开的实际成果",
}
INPUT_SIZE, HIDDEN, OUTPUT_SIZE = 8, 16, len(ACTIONS)
PARAMETER_COUNT = HIDDEN*INPUT_SIZE + HIDDEN + HIDDEN*HIDDEN + HIDDEN + OUTPUT_SIZE*HIDDEN + OUTPUT_SIZE
LR, CLIP_NORM = 0.08, 1.0
SKILL_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = ".positive-feedback"
STATE_VERSION = 5
PROFILE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
EVALUATION_MODES = ["selection", "quality"]
PREFERENCE_STATUSES = ["active", "disabled"]
MAX_PREFERENCE_TEXT = 400
MAX_CONTEXT_PREFERENCES = 6
MAX_CONTEXT_QUALITY_REQUIREMENTS = 4
MAX_CONTEXT_TEXT = 1600
CONTEXT_CACHE_VERSION = 1
CONTEXT_SNAPSHOT_VERSION = 1
SESSION_VERSION = 1
THREAD_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def validate_profile(profile):
    if not profile or not PROFILE_PATTERN.fullmatch(profile):
        raise ValueError("profile must be 1-64 characters using letters, digits, dot, underscore, or hyphen")
    return profile


def default_state_path(profile, cwd=None):
    profile = validate_profile(profile)
    root = Path.cwd() if cwd is None else Path(cwd)
    path = root.resolve() / RUNTIME_DIR / profile / "action-controller.json"
    ensure_external_path(path)
    return path


def session_path_for(thread_id):
    if not thread_id or not THREAD_PATTERN.fullmatch(thread_id):
        raise ValueError("a stable host thread ID is required for session activation")
    return ensure_external_path(SKILL_ROOT.parents[1] / "positive-feedback" /
                                "sessions" / f"{thread_id}.json")


def load_session(path, thread_id):
    path = ensure_external_path(path)
    if not path.exists():
        return {"version": SESSION_VERSION, "thread_id": thread_id,
                "status": "inactive", "source_excerpt": "", "changed_at": None}
    record = json.loads(path.read_text(encoding="utf-8"))
    if (record.get("version") != SESSION_VERSION or record.get("thread_id") != thread_id
            or record.get("status") not in ("active", "disabled")):
        raise ValueError("invalid or mismatched session activation record")
    return record


def save_session(path, record):
    path = ensure_external_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(record, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def session_command(action, thread_id, path=None, source_excerpt=""):
    if not thread_id:
        if action == "status":
            return {"status": "unavailable", "active": False,
                    "reason": "stable host thread ID unavailable"}
        raise ValueError("enable/disable requires a stable host thread ID")
    if not THREAD_PATTERN.fullmatch(thread_id):
        raise ValueError("invalid stable host thread ID")
    path = ensure_external_path(path) if path is not None else session_path_for(thread_id)
    if action == "status":
        record = load_session(path, thread_id)
        return {"status": record["status"], "active": record["status"] == "active",
                "thread_id": thread_id, "source_excerpt": record.get("source_excerpt", ""),
                "changed_at": record.get("changed_at")}
    excerpt = normalized_text(source_excerpt, "source excerpt", limit=160)
    desired = "active" if action == "enable" else "disabled"
    with state_lock(path):
        record = load_session(path, thread_id)
        if record["status"] == desired:
            return {"status": "duplicate", "active": desired == "active",
                    "thread_id": thread_id}
        record.update(status=desired, source_excerpt=excerpt, changed_at=now())
        save_session(path, record)
    return {"status": desired, "active": desired == "active", "thread_id": thread_id}


def preferences_path_for(state_path):
    return ensure_external_path(Path(state_path).with_name("preferences.json"))


def ensure_external_path(path):
    resolved = Path(path).resolve()
    try:
        resolved.relative_to(SKILL_ROOT)
    except ValueError:
        return resolved
    raise ValueError("preference state must not be stored inside the skill directory")


def now():
    return datetime.now(timezone.utc).isoformat()


def normalized_text(value, field, limit=MAX_PREFERENCE_TEXT):
    value = re.sub(r"\s+", " ", value or "").strip()
    if not value:
        raise ValueError(f"{field} is required")
    if len(value) > limit:
        raise ValueError(f"{field} must be at most {limit} characters")
    return value


def parse_task_kinds(value):
    if not value:
        return []
    kinds = value if isinstance(value, list) else [x.strip() for x in value.split(",") if x.strip()]
    if len(set(kinds)) != len(kinds) or any(k not in TASK_KINDS for k in kinds):
        raise ValueError("task kinds must be unique values from writing, code, data, general")
    return kinds


def parse_utc_timestamp(value, field="expires_at"):
    if not value:
        return ""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def initial():
    rng = random.Random(31)
    return {
        "w1": [[rng.uniform(-.08, .08) for _ in range(INPUT_SIZE)] for _ in range(HIDDEN)],
        "b1": [0.0]*HIDDEN,
        "w2": [[rng.uniform(-.08, .08) for _ in range(HIDDEN)] for _ in range(HIDDEN)],
        "b2": [0.0]*HIDDEN,
        "w3": [[rng.uniform(-.08, .08) for _ in range(HIDDEN)] for _ in range(OUTPUT_SIZE)],
        "b3": [0.0]*OUTPUT_SIZE,
    }


def flatten(p):
    return [v for key in sorted(p) for row in p[key]
            for v in (row if isinstance(row, list) else [row])]


def dense(w, b, x):
    return [sum(a*c for a, c in zip(row, x))+bias for row, bias in zip(w, b)]


def sigmoid(x):
    if x >= 0:
        z = math.exp(-x)
        return 1/(1+z)
    z = math.exp(x)
    return z/(1+z)


def softplus(x):
    return max(x, 0) + math.log1p(math.exp(-abs(x)))


def task_features(task):
    if task["task_kind"] not in TASK_KINDS:
        raise ValueError("invalid task kind")
    flags = task["flags"]
    return [float(task["task_kind"] == k) for k in TASK_KINDS] + [
        float(bool(flags.get("artifact_present"))),
        float(bool(flags.get("evidence_required"))),
        float(bool(flags.get("revision_requested"))),
        float(bool(flags.get("output_required"))),
    ]


def forward(p, x):
    if len(x) != INPUT_SIZE:
        raise ValueError("invalid feature vector")
    h1 = [math.tanh(v) for v in dense(p["w1"], p["b1"], x)]
    h2 = [math.tanh(v) for v in dense(p["w2"], p["b2"], h1)]
    logits = dense(p["w3"], p["b3"], h2)
    return x, h1, h2, logits, [sigmoid(v) for v in logits]


def action_indices(actions):
    if not actions:
        raise ValueError("at least one action is required")
    if len(set(actions)) != len(actions):
        raise ValueError("duplicate action")
    try:
        return [ACTIONS.index(a) for a in actions]
    except ValueError as exc:
        raise ValueError(f"unknown action: {exc}") from exc


def backprop(p, x, dz):
    _, h1, h2, _, _ = forward(p, x)
    dh2 = [(1-h2[j]**2)*sum(p["w3"][i][j]*dz[i] for i in range(OUTPUT_SIZE))
           for j in range(HIDDEN)]
    dh1 = [(1-h1[j]**2)*sum(p["w2"][i][j]*dh2[i] for i in range(HIDDEN))
           for j in range(HIDDEN)]
    return {
        "w3": [[d*h for h in h2] for d in dz], "b3": dz,
        "w2": [[d*h for h in h1] for d in dh2], "b2": dh2,
        "w1": [[d*v for v in x] for d in dh1], "b1": dh1,
    }


def attraction_gradient(p, x, actions, weight):
    weight = abs(signed_number(weight))
    ids = action_indices(actions)
    _, _, _, _, probs = forward(p, x)
    dz = [0.0]*OUTPUT_SIZE
    loss = 0.0
    for i in ids:
        loss -= weight*math.log(max(probs[i], 1e-12))
        dz[i] += weight*(probs[i]-1)
    return loss, backprop(p, x, dz)


def pair_gradient(p, x, failed_actions, target_actions, weight):
    weight = abs(signed_number(weight))
    neg, pos = action_indices(failed_actions), action_indices(target_actions)
    if set(neg) & set(pos):
        raise ValueError("failed and target actions must not overlap")
    _, _, _, logits, _ = forward(p, x)
    pairs = [(n, q) for n in neg for q in pos]
    scale = weight/len(pairs)
    dz, loss = [0.0]*OUTPUT_SIZE, 0.0
    for n, q in pairs:
        diff = logits[n]-logits[q]
        loss += scale*softplus(diff)
        d = scale*sigmoid(diff)
        dz[n] += d
        dz[q] -= d
    return loss, backprop(p, x, dz)


def apply_step(p, loss_grad):
    loss, g = loss_grad
    norm = math.sqrt(sum(v*v for v in flatten(g)))
    scale = min(1.0, CLIP_NORM/norm) if norm else 1.0
    q = copy.deepcopy(p)
    for key in q:
        for i in range(len(q[key])):
            if isinstance(q[key][i], list):
                for j in range(len(q[key][i])):
                    q[key][i][j] -= LR*scale*g[key][i][j]
            else:
                q[key][i] -= LR*scale*g[key][i]
    return q, {"loss_before": loss, "gradient_norm": norm, "clip_scale": scale,
               "parameter_delta_l2": math.sqrt(sum((a-b)**2 for a, b in zip(flatten(p), flatten(q))))}


def fresh_state():
    return {"version": STATE_VERSION, "params": initial(), "tasks": [], "events": [],
            "preferences": [], "updates": [], "context_cache": {}, "batch_history": [],
            "identity": {"profile_id": None, "controller_version": STATE_VERSION,
                         "created_by": None, "created_at": None,
                         "last_executor": None, "last_seen_at": None}}


def load(path):
    path = ensure_external_path(path)
    if not path.exists():
        return fresh_state()
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("version") == 4:
        state["version"] = STATE_VERSION
        for event in state.get("events", []):
            event.setdefault("evaluation_mode", "selection" if event.get("kind") == "evaluation" else "")
            event.setdefault("quality_requirement", "")
    if state.get("version") != STATE_VERSION:
        raise ValueError(f"controller state is not v{STATE_VERSION}; use a new state path and keep the old file as archive")
    state.setdefault("preferences", [])
    state.setdefault("context_cache", {})
    state.setdefault("batch_history", [])
    state.setdefault("identity", {"profile_id": None, "controller_version": STATE_VERSION,
                                  "created_by": None, "created_at": None,
                                  "last_executor": None, "last_seen_at": None})
    if (not isinstance(state.get("tasks"), list) or not isinstance(state.get("events"), list)
            or not isinstance(state.get("preferences"), list) or not isinstance(state.get("updates"), list)
            or not isinstance(state.get("batch_history"), list)):
        raise ValueError("invalid controller collections")
    if len({t.get("task_id") for t in state["tasks"]}) != len(state["tasks"]):
        raise ValueError("duplicate or missing task_id in controller state")
    if len({e.get("event_id") for e in state["events"]}) != len(state["events"]):
        raise ValueError("duplicate or missing event_id in controller state")
    if len({p.get("preference_id") for p in state["preferences"]}) != len(state["preferences"]):
        raise ValueError("duplicate or missing preference_id in controller state")
    if len(flatten(state["params"])) != PARAMETER_COUNT:
        raise ValueError("invalid parameter count")
    if not all(math.isfinite(v) for v in flatten(state["params"])):
        raise ValueError("non-finite controller state")
    return state


def register_identity(state, profile="", executor_id=""):
    """Persist only host-provided IDs; never infer a model or executor name."""
    identity = state.setdefault("identity", {})
    identity.setdefault("profile_id", None)
    identity.setdefault("controller_version", STATE_VERSION)
    identity.setdefault("created_by", None)
    identity.setdefault("created_at", None)
    identity.setdefault("last_executor", None)
    identity.setdefault("last_seen_at", None)
    if profile:
        validate_profile(profile)
        if identity["profile_id"] not in (None, profile):
            raise ValueError("controller state belongs to a different profile")
        identity["profile_id"] = profile
    if executor_id:
        validate_profile(executor_id)
        if identity["created_by"] is None:
            identity["created_by"] = executor_id
            identity["created_at"] = now()
        identity["last_executor"] = executor_id
        identity["last_seen_at"] = now()
    identity["controller_version"] = STATE_VERSION
    return identity


def save(path, state):
    path = ensure_external_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    runtime_root = next((p for p in path.parents if p.name == RUNTIME_DIR), None)
    if runtime_root is not None:
        ignore = runtime_root / ".gitignore"
        if not ignore.exists():
            ignore.write_text("*\n", encoding="utf-8")
    temp = path.with_suffix(path.suffix+f".tmp.{os.getpid()}")
    persisted = {key: value for key, value in state.items() if key != "preferences"}
    encoded = json.dumps(persisted, ensure_ascii=False, separators=(",", ":"),
                         allow_nan=False).encode("utf-8")
    with temp.open("wb") as f:
        f.write(encoded)
        f.flush(); os.fsync(f.fileno())
    os.replace(temp, path)
    try:
        write_context_snapshot(path, state, hashlib.sha256(encoded).hexdigest())
    except (ValueError, KeyError, TypeError, IndexError, OSError):
        # Auxiliary context data must never block the authoritative state write.
        pass


def snapshot_path_for(path):
    return path.with_suffix(".snapshot.json")


def file_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_context_snapshot(path, state, state_sha256=None):
    """Derived, atomic cache; a stale or damaged copy is never authoritative."""
    kinds = {}
    for kind in TASK_KINDS:
        params, count, _ = context_params(state, kind)
        kinds[kind] = {"params": params, "event_count": count,
                       "quality_requirements": quality_requirements(state, kind)}
    payload = {"version": CONTEXT_SNAPSHOT_VERSION,
               "state_sha256": state_sha256 or file_sha256(path), "kinds": kinds}
    target = snapshot_path_for(path)
    temp = target.with_suffix(target.suffix + f".tmp.{os.getpid()}")
    with temp.open("w", encoding="utf-8") as output:
        output.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False))
        output.flush(); os.fsync(output.fileno())
    os.replace(temp, target)


def read_context_snapshot(path, kind):
    target = snapshot_path_for(path)
    if not path.exists() or not target.exists():
        return None
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
        entry = payload["kinds"][kind]
        params = entry["params"]
        if (payload["version"] != CONTEXT_SNAPSHOT_VERSION
                or payload["state_sha256"] != file_sha256(path)
                or not isinstance(entry["event_count"], int)
                or entry["event_count"] < 0
                or not isinstance(entry["quality_requirements"], list)
                or len(flatten(params)) != PARAMETER_COUNT
                or not all(math.isfinite(value) for value in flatten(params))):
            return None
        return entry
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def load_preferences(path):
    path = ensure_external_path(path)
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("version") != 1 or not isinstance(payload.get("preferences"), list):
        raise ValueError("invalid preferences file")
    rows = payload["preferences"]
    if len({p.get("preference_id") for p in rows}) != len(rows):
        raise ValueError("duplicate or missing preference_id in preferences file")
    return rows


def save_preferences(path, preferences):
    path = ensure_external_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    runtime_root = next((p for p in path.parents if p.name == RUNTIME_DIR), None)
    if runtime_root is not None:
        ignore = runtime_root / ".gitignore"
        if not ignore.exists():
            ignore.write_text("*\n", encoding="utf-8")
    temp = path.with_suffix(path.suffix+f".tmp.{os.getpid()}")
    payload = {"version": 1, "preferences": preferences}
    with temp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, allow_nan=False)
        f.flush(); os.fsync(f.fileno())
    os.replace(temp, path)


@contextlib.contextmanager
def state_lock(path, timeout=10.0):
    """Serialize one load-modify-save transaction across local processes."""
    path = ensure_external_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    handle = lock_path.open("a+b")
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
    deadline = time.monotonic() + timeout
    locked = False
    try:
        while not locked:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except (OSError, BlockingIOError):
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"timed out waiting for controller lock: {lock_path}")
                time.sleep(0.05)
        yield
    finally:
        if locked:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def get_task(state, task_id):
    try:
        return next(t for t in state["tasks"] if t["task_id"] == task_id)
    except StopIteration as exc:
        raise ValueError("unknown task_id") from exc


def get_event(state, event_id):
    try:
        return next(e for e in state["events"] if e["event_id"] == event_id)
    except StopIteration as exc:
        raise ValueError("unknown event_id") from exc


def task_index_for(state):
    index = {}
    for task in state["tasks"]:
        task_id = task["task_id"]
        if task_id in index:
            raise ValueError(f"duplicate task_id: {task_id}")
        index[task_id] = task
    return index


def parse_actions(value):
    if isinstance(value, list):
        actions = value
    else:
        actions = [x.strip() for x in value.split(",") if x.strip()]
    action_indices(actions)
    return actions


def public_task(task, params):
    scores = forward(params, task_features(task))[4]
    ranked = [ACTIONS[i] for i in sorted(range(OUTPUT_SIZE), key=lambda i: scores[i], reverse=True)]
    recommended = list(task["required_actions"])
    for action in ranked:
        if action not in recommended and len(recommended) < max(4, len(task["required_actions"])):
            recommended.append(action)
    return {"task_id": task["task_id"], "status": task["status"],
            "source_message_id": task.get("source_message_id"),
            "upstream_task_ref": task.get("upstream_task_ref"),
            "created_by": task.get("created_by"),
            "required_actions": task["required_actions"],
            "recommended_actions": recommended,
            "action_scores": {a: scores[i] for i, a in enumerate(ACTIONS)},
            "guidance": [ACTION_HINTS[a] for a in recommended],
            "rule": "execute explicit requirements regardless of controller score; record only actions actually performed"}


def add_preference(state, preference_id, text, source_id, task_kinds="",
                   exclude_task_kinds="", priority=50, expires_at="",
                   conflict_group="", replace=False):
    if not preference_id.strip() or not source_id.strip():
        raise ValueError("preference ID and source ID are required")
    text = normalized_text(text, "preference text")
    included = parse_task_kinds(task_kinds)
    excluded = parse_task_kinds(exclude_task_kinds)
    if set(included) & set(excluded):
        raise ValueError("included and excluded task kinds must not overlap")
    if not 0 <= priority <= 100:
        raise ValueError("priority must be in [0,100]")
    expires_at = parse_utc_timestamp(expires_at)
    if conflict_group:
        conflict_group = validate_profile(conflict_group)
    payload = {"preference_id": preference_id, "text": text, "source_id": source_id,
               "task_kinds": included, "exclude_task_kinds": excluded,
               "priority": priority, "expires_at": expires_at,
               "conflict_group": conflict_group, "status": "active"}
    for other in state["preferences"]:
        if other["source_id"] == source_id and other["preference_id"] != preference_id:
            raise ValueError("source_id already belongs to another preference")
    existing = next((p for p in state["preferences"] if p["preference_id"] == preference_id), None)
    if existing:
        comparable = {k: existing.get(k) for k in payload}
        if comparable == payload:
            return {"status": "duplicate", "preference_id": preference_id}
        if not replace:
            raise ValueError("preference_id exists; use --replace to correct it")
        revision = {k: existing.get(k) for k in payload}
        revision["replaced_at"] = now()
        existing.setdefault("revisions", []).append(revision)
        existing.update(payload)
        existing["updated_at"] = now()
    else:
        if replace:
            raise ValueError("new preference cannot use --replace")
        payload.update({"created_at": now(), "updated_at": now(), "revisions": []})
        state["preferences"].append(payload)
    return {"status": "active", "preference_id": preference_id,
            "scope": "task_kind" if included or excluded else "workspace"}


def set_preference_status(state, preference_id, status):
    if status not in PREFERENCE_STATUSES:
        raise ValueError("invalid preference status")
    try:
        preference = next(p for p in state["preferences"] if p["preference_id"] == preference_id)
    except StopIteration as exc:
        raise ValueError("unknown preference_id") from exc
    if preference["status"] == status:
        return {"status": "duplicate", "preference_id": preference_id,
                "preference_status": status}
    preference["status"] = status
    preference["updated_at"] = now()
    return {"status": "updated", "preference_id": preference_id,
            "preference_status": status}


def list_preferences(state, task_kind=""):
    if task_kind and task_kind not in TASK_KINDS:
        raise ValueError("invalid task kind")
    rows = copy.deepcopy(state["preferences"])
    if task_kind:
        for row in rows:
            row["applicable"] = preference_is_applicable(row, task_kind)
    return {"preference_count": len(rows), "task_kind": task_kind or None,
            "preferences": rows}


def preference_is_applicable(preference, task_kind, at=None):
    if preference.get("status") != "active":
        return False
    if preference.get("task_kinds") and task_kind not in preference["task_kinds"]:
        return False
    if task_kind in preference.get("exclude_task_kinds", []):
        return False
    if preference.get("expires_at"):
        at = at or datetime.now(timezone.utc)
        if datetime.fromisoformat(preference["expires_at"]) <= at:
            return False
    return True


def applicable_preferences(state, task_kind):
    rows = [p for p in state["preferences"] if preference_is_applicable(p, task_kind)]
    rows.sort(key=lambda p: (p.get("updated_at", ""), p["preference_id"]), reverse=True)
    rows.sort(key=lambda p: p["priority"], reverse=True)
    selected, used, selected_groups = [], 0, set()
    for preference in rows:
        if len(selected) >= MAX_CONTEXT_PREFERENCES:
            break
        group = preference.get("conflict_group", "")
        if group and group in selected_groups:
            continue
        text = preference["text"]
        if used + len(text) > MAX_CONTEXT_TEXT:
            remaining = MAX_CONTEXT_TEXT - used
            if remaining < 20:
                break
            text = text[:remaining-1].rstrip() + "…"
        selected.append({"preference_id": preference["preference_id"], "text": text,
                         "priority": preference["priority"],
                         "conflict_group": group or None,
                         "scope": "task_kind" if preference.get("task_kinds") or preference.get("exclude_task_kinds") else "workspace"})
        if group:
            selected_groups.add(group)
        used += len(text)
    return selected


def event_training_task(state, event, task_index=None):
    task_id = event.get("target_task_id") if event["kind"] == "evaluation" else event["task_id"]
    if task_index is not None:
        try:
            return task_index[task_id]
        except KeyError as exc:
            raise ValueError("unknown task_id") from exc
    return get_task(state, task_id)


def quality_requirements(state, task_kind, max_chars=MAX_CONTEXT_TEXT):
    rows = []
    task_index = task_index_for(state)
    for event in state["events"]:
        if (event.get("status") == "learned" and event.get("kind") == "evaluation"
                and event.get("evaluation_mode") == "quality"
                and event_training_task(state, event, task_index)["task_kind"] == task_kind):
            rows.append({"event_id": event["event_id"], "text": event["quality_requirement"]})
    selected, used = [], 0
    for row in rows[-MAX_CONTEXT_QUALITY_REQUIREMENTS:]:
        if used + len(row["text"]) > max_chars:
            remaining = max_chars - used
            if remaining < 20:
                break
            row = {**row, "text": row["text"][:remaining-1].rstrip() + "…"}
        selected.append(row); used += len(row["text"])
    return selected


def learned_events_for_kind(state, task_kind, task_index=None):
    task_index = task_index if task_index is not None else task_index_for(state)
    return [event for event in state["events"]
            if event.get("status") == "learned"
            and event_training_task(state, event, task_index)["task_kind"] == task_kind]


def context_cache_key(state, task_kind, task_index=None):
    task_index = task_index if task_index is not None else task_index_for(state)
    rows = []
    for event in learned_events_for_kind(state, task_kind, task_index):
        task = event_training_task(state, event, task_index)
        rows.append({
            "event_id": event["event_id"], "kind": event["kind"], "score": event["score"],
            "evaluation_mode": event.get("evaluation_mode", ""),
            "failed_actions": event.get("failed_actions", []),
            "target_actions": event.get("target_actions", []),
            "task_features": task_features(task),
            "actual_actions": task.get("actual_actions", []),
        })
    encoded = json.dumps(rows, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True, allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest(), len(rows)


def training_integrity(state):
    task_index = task_index_for(state)
    rows = []
    for event in state["events"]:
        if event.get("status") != "learned":
            continue
        task = event_training_task(state, event, task_index)
        rows.append((event["event_id"], event["kind"], event["score"],
                     event.get("evaluation_mode", ""), event.get("failed_actions", []),
                     event.get("target_actions", []), task["task_kind"],
                     task_features(task), task.get("actual_actions", [])))
    payload = json.dumps(rows, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True, allow_nan=False).encode("utf-8")
    params = json.dumps(state["params"], sort_keys=True, separators=(",", ":"),
                        allow_nan=False).encode("utf-8")
    trace = json.dumps(state.get("training_trace", []), sort_keys=True,
                       separators=(",", ":"), allow_nan=False).encode("utf-8")
    return {"events_sha256": hashlib.sha256(payload).hexdigest(),
            "params_sha256": hashlib.sha256(params).hexdigest(),
            "trace_sha256": hashlib.sha256(trace).hexdigest()}


def context_params(state, task_kind):
    digest, event_count = context_cache_key(state, task_kind)
    entry = state.get("context_cache", {}).get(task_kind, {})
    params = entry.get("params")
    if (entry.get("version") == CONTEXT_CACHE_VERSION
            and entry.get("event_digest") == digest
            and entry.get("event_count") == event_count
            and isinstance(params, dict)):
        try:
            if len(flatten(params)) == PARAMETER_COUNT and all(math.isfinite(v) for v in flatten(params)):
                return copy.deepcopy(params), event_count, True
        except (KeyError, TypeError):
            pass
    params, _ = replay(state, task_kind=task_kind)
    return params, event_count, False


def refresh_context_cache(state, task_kinds=None):
    kinds = TASK_KINDS if task_kinds is None else list(dict.fromkeys(task_kinds))
    if any(kind not in TASK_KINDS for kind in kinds):
        raise ValueError("invalid task kind for cache refresh")
    cache = {} if task_kinds is None else dict(state.get("context_cache", {}))
    task_index = task_index_for(state)
    for task_kind in kinds:
        digest, event_count = context_cache_key(state, task_kind, task_index)
        params, _ = replay(state, task_kind=task_kind, task_index=task_index)
        cache[task_kind] = {"version": CONTEXT_CACHE_VERSION,
                            "event_digest": digest, "event_count": event_count,
                            "params": params}
    state["context_cache"] = cache
    return cache


def pre_generation_context(state, task_kind, required_actions="", artifact_present=False,
                           evidence_required=False, revision_requested=False,
                           output_required=False, compact=False, factual_correction=False,
                           snapshot_entry=None):
    required = parse_actions(required_actions) if required_actions else []
    if factual_correction:
        required = list(dict.fromkeys(required + ["inspect_target", "edit_content",
                                                  "add_evidence", "validate_requirements"]))
    task = {
        "task_kind": task_kind,
        "required_actions": required,
        "flags": {"artifact_present": artifact_present,
                  "evidence_required": evidence_required,
                  "revision_requested": revision_requested,
                  "output_required": output_required},
    }
    features = task_features(task)
    if snapshot_entry is None:
        effective_params, learned_count, cache_hit = context_params(state, task_kind)
    else:
        effective_params = snapshot_entry["params"]
        learned_count = snapshot_entry["event_count"]
        cache_hit = True
    current = forward(effective_params, features)[4]
    baseline = forward(initial(), features)[4]
    candidates = [i for i in range(OUTPUT_SIZE)
                  if ACTIONS[i] not in required and current[i] - baseline[i] > 1e-9]
    candidates.sort(key=lambda i: current[i] - baseline[i], reverse=True)
    preferred = [ACTIONS[i] for i in candidates[:4]] if learned_count else []
    concrete = applicable_preferences(state, task_kind)
    remaining_context = max(0, MAX_CONTEXT_TEXT - sum(len(p["text"]) for p in concrete))
    if snapshot_entry is None:
        quality = quality_requirements(state, task_kind, remaining_context)
    else:
        quality, used = [], 0
        for row in snapshot_entry["quality_requirements"]:
            if used + len(row["text"]) > remaining_context:
                remaining = remaining_context - used
                if remaining < 20:
                    break
                row = {**row, "text": row["text"][:remaining-1].rstrip() + "…"}
            quality.append(row); used += len(row["text"])
    clauses = []
    if required:
        clauses.append("Current request requires: " + ", ".join(required) + ".")
    if preferred:
        clauses.append("Learned optional action preferences: " + ", ".join(preferred) + ".")
    if concrete:
        clauses.append("Applicable explicit preferences: " + " | ".join(p["text"] for p in concrete) + ".")
    if quality:
        clauses.append("Verified quality requirements: " + " | ".join(q["text"] for q in quality) + ".")
    if factual_correction:
        clauses.append("Recheck disputed factual claims against traceable sources; correct contradicted claims, identify unresolved uncertainty, and present the evidence and revised answer.")
    clauses.append("Apply current explicit instructions, facts, safety rules, and acceptance criteria before controller preferences.")
    result = {
        "mode": "pre_generation",
        "task_kind": task_kind,
        "required_actions": required,
        "preferred_actions": preferred,
        "explicit_preferences": concrete,
        "quality_requirements": quality,
        "content_checks": (["trace_disputed_claims", "check_sources", "correct_or_qualify_claims"]
                           if factual_correction else []),
        "learned_event_count": learned_count,
        "context_cache_hit": cache_hit,
        "action_scores": {ACTIONS[i]: current[i] for i in range(OUTPUT_SIZE)},
        "score_deltas_from_initial": {ACTIONS[i]: current[i] - baseline[i] for i in range(OUTPUT_SIZE)},
        "instruction_text": " ".join(clauses),
        "boundary": "This context can guide generation after it is loaded; only a host/client can inject it before turn/start.",
    }
    if compact:
        return {key: result[key] for key in (
            "mode", "task_kind", "required_actions", "preferred_actions",
            "explicit_preferences", "quality_requirements", "content_checks",
            "instruction_text")}
    return result


def prepare(state, task_id, task_kind, required_actions, target_spec, target_source_id,
            artifact_present=False, evidence_required=False, revision_requested=False,
            output_required=False, source_message_id="", upstream_task_ref="",
            executor_id="", factual_correction=False):
    if not all(x.strip() for x in (task_id, target_spec, target_source_id)):
        raise ValueError("task ID, target specification, and target source ID are required")
    required_actions = parse_actions(required_actions)
    if factual_correction:
        required_actions = list(dict.fromkeys(required_actions + [
            "inspect_target", "edit_content", "add_evidence", "validate_requirements"]))
        evidence_required = True
    source_message_id = source_message_id.strip() or target_source_id
    upstream_task_ref = upstream_task_ref.strip()
    scoped_params, _, _ = context_params(state, task_kind)
    for old in state["tasks"]:
        if old["task_id"] == task_id:
            expected = (task_kind, required_actions, target_spec, target_source_id,
                        source_message_id, upstream_task_ref, artifact_present,
                        evidence_required, revision_requested, output_required, factual_correction)
            actual = (old["task_kind"], old["required_actions"], old["target_spec"], old["target_source_id"],
                      old.get("source_message_id", old["target_source_id"]), old.get("upstream_task_ref", ""),
                      *[old["flags"][k] for k in ("artifact_present", "evidence_required", "revision_requested", "output_required")],
                      old["flags"].get("factual_correction", False))
            if expected != actual:
                raise ValueError("task_id reused with different content")
            return public_task(old, scoped_params)
    task = {"task_id": task_id, "task_kind": task_kind,
            "required_actions": required_actions, "target_spec": target_spec,
            "target_source_id": target_source_id,
            "source_message_id": source_message_id, "upstream_task_ref": upstream_task_ref,
            "created_by": executor_id or None,
            "flags": {"artifact_present": artifact_present, "evidence_required": evidence_required,
                      "revision_requested": revision_requested, "output_required": output_required,
                      "factual_correction": factual_correction},
            "status": "awaiting_execution", "created_at": now(), "attempts": []}
    state["tasks"].append(task)
    return public_task(task, scoped_params)


def sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024*1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_json(path):
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, (dict, list)) or not value:
        raise ValueError("JSON evidence/checks must be a non-empty object or list")
    return value


def validate_fact_correction_evidence(evidence):
    if not isinstance(evidence, dict) or not isinstance(evidence.get("claims"), list) or not evidence["claims"]:
        raise ValueError("factual correction requires a nonempty claims evidence list")
    revised_answer = evidence.get("revised_answer")
    if not isinstance(revised_answer, str) or not revised_answer.strip():
        raise ValueError("factual correction requires a revised_answer")
    for claim in evidence["claims"]:
        if not isinstance(claim, dict):
            raise ValueError("each fact-check claim must be an object")
        if claim.get("finding") not in ("supported", "contradicted", "uncertain"):
            raise ValueError("fact-check finding must be supported, contradicted, or uncertain")
        if not isinstance(claim.get("disputed"), str) or not claim["disputed"].strip():
            raise ValueError("fact-check claim requires the disputed statement")
        sources = claim.get("sources")
        if not isinstance(sources, list) or not sources or any(
                not isinstance(source, str) or not source.strip() for source in sources):
            raise ValueError("fact-check claim requires nonempty source references")
        if claim["finding"] == "contradicted" and not str(claim.get("corrected_text", "")).strip():
            raise ValueError("contradicted claim requires corrected_text")
        if claim["finding"] == "uncertain" and not str(claim.get("uncertainty", "")).strip():
            raise ValueError("uncertain claim requires an uncertainty statement")
        if claim["finding"] == "contradicted" and claim["corrected_text"].strip() not in revised_answer:
            raise ValueError("revised_answer must include each corrected statement")
        if claim["finding"] == "uncertain" and claim["uncertainty"].strip() not in revised_answer:
            raise ValueError("revised_answer must disclose each unresolved uncertainty")


def complete(state, task_id, output_id, actions, artifacts=None, evidence_json=None,
             executor_id=""):
    task = get_task(state, task_id)
    if not output_id.strip():
        raise ValueError("output ID is required")
    actions = parse_actions(actions)
    if not set(task["required_actions"]).issubset(actions):
        raise ValueError("actual actions do not cover explicit required actions")
    artifact_rows = []
    for raw in artifacts or []:
        path = Path(raw)
        if not path.is_file():
            raise ValueError(f"artifact not found: {path}")
        artifact_rows.append({"path": str(path.resolve()), "sha256": sha256(path), "size": path.stat().st_size})
    evidence = read_json(evidence_json) if evidence_json else None
    if task["flags"].get("factual_correction"):
        validate_fact_correction_evidence(evidence)
    if not artifact_rows and evidence is None:
        raise ValueError("completion requires an artifact or evidence JSON")
    if task["flags"]["evidence_required"] and evidence is None:
        raise ValueError("task requires a non-empty evidence JSON")
    if task["flags"]["output_required"] and not artifact_rows:
        raise ValueError("task requires at least one output artifact")
    payload = {"output_id": output_id, "actual_actions": actions,
               "artifacts": artifact_rows, "action_evidence": evidence}
    if task["status"] in ("awaiting_verification", "verified"):
        if all(task.get(k) == v for k, v in payload.items()):
            return {"status": "duplicate", "task_id": task_id}
        raise ValueError("completed task cannot be overwritten; record a failed verification before retry")
    if task["status"] != "awaiting_execution":
        raise ValueError("task is not awaiting execution")
    task.update(payload); task["completed_at"] = now(); task["completed_by"] = executor_id or None
    task["status"] = "awaiting_verification"
    return {"status": task["status"], "task_id": task_id, "output_id": output_id,
            "actual_actions": actions, "artifacts": artifact_rows}


def verify(state, task_id, passed, checks_json, executor_id=""):
    task = get_task(state, task_id)
    if task["status"] != "awaiting_verification":
        raise ValueError("task is not awaiting verification")
    checks = read_json(checks_json)
    if task["flags"].get("factual_correction") and passed:
        declared_sources = {source for claim in task["action_evidence"]["claims"]
                            for source in claim["sources"]}
        checked_sources = checks.get("checked_sources") if isinstance(checks, dict) else None
        if (not isinstance(checks, dict) or checks.get("source_checks_passed") is not True
                or checks.get("claims_reviewed") != len(task["action_evidence"]["claims"])
                or not isinstance(checked_sources, list)
                or any(not isinstance(source, str) or not source.strip() for source in checked_sources)
                or not declared_sources.issubset(set(checked_sources))):
            raise ValueError("factual correction verification requires checked sources and all claims reviewed")
    integrity = []
    for artifact in task["artifacts"]:
        path = Path(artifact["path"])
        if not path.is_file():
            raise ValueError(f"artifact disappeared before verification: {path}")
        current = {"path": str(path.resolve()), "sha256": sha256(path), "size": path.stat().st_size}
        if current["sha256"] != artifact["sha256"] or current["size"] != artifact["size"]:
            raise ValueError(f"artifact changed after completion: {path}")
        integrity.append(current)
    attempt = {"output_id": task["output_id"], "actual_actions": task["actual_actions"],
               "artifacts": task["artifacts"], "action_evidence": task["action_evidence"],
               "artifact_integrity": integrity, "checks": checks,
               "passed": passed, "verified_at": now()}
    task["attempts"].append(attempt)
    if passed:
        task["verification_checks"] = checks; task["verified_at"] = now()
        task["verified_by"] = executor_id or None; task["status"] = "verified"
    else:
        for key in ("output_id", "actual_actions", "artifacts", "action_evidence", "completed_at", "completed_by"):
            task.pop(key, None)
        task["status"] = "awaiting_execution"
    return {"task_id": task_id, "status": task["status"], "passed": passed}


def event_status(state, event, task_index=None):
    if event["kind"] == "evaluation":
        target_id = event.get("target_task_id")
        if not target_id or not event.get("target_spec") or not event.get("target_actions"):
            return "awaiting_target"
    task = event_training_task(state, event, task_index)
    if task["status"] == "awaiting_execution": return "awaiting_execution"
    if task["status"] == "awaiting_verification": return "awaiting_verification"
    if task["status"] != "verified": return task["status"]
    actions = event.get("target_actions") if event["kind"] != "positive" else task["actual_actions"]
    if not set(actions).issubset(task["actual_actions"]):
        raise ValueError("feedback target actions were not actually executed")
    return "ready"


def action_evidence(state, event, task_index=None):
    task_id = event.get("target_task_id") if event["kind"] == "evaluation" else event["task_id"]
    task = event_training_task(state, event, task_index)
    return {"task_id": task_id, "output_id": task["output_id"],
            "actual_actions": task["actual_actions"], "artifacts": task["artifacts"],
            "action_evidence": task["action_evidence"],
            "verification_checks": task["verification_checks"],
            "verified_at": task["verified_at"]}


def add_feedback(state, event_id, kind, task_id, source_id, label=None, score=None,
                  quote="", aspect="", previous_output_id="", failed_actions=None,
                  target_spec="", target_source_id="", target_actions=None,
                  target_task_id="", evaluation_mode="", quality_requirement="",
                  source_message_id="", replace=False):
    if not all(x.strip() for x in (event_id, task_id, source_id)):
        raise ValueError("event, task, and source IDs are required")
    task = get_task(state, task_id)
    if (label is None) == (score is None):
        raise ValueError("provide exactly one of label or score")
    if label is not None:
        try: score = LABELS_BY_KIND[kind][label]
        except KeyError as exc: raise ValueError("label does not belong to signal kind") from exc
    score = validate(kind, score)
    quote = normalized_text(quote, "source quote", limit=800) if quote else ""
    source_message_id = source_message_id.strip() or source_id
    failed = parse_actions(failed_actions) if failed_actions else []
    targets = parse_actions(target_actions) if target_actions else []
    if kind == "evaluation":
        evaluation_mode = evaluation_mode or "selection"
        if evaluation_mode not in EVALUATION_MODES:
            raise ValueError("evaluation mode must be selection or quality")
        if evaluation_mode == "quality":
            quality_requirement = normalized_text(quality_requirement, "quality requirement")
        elif quality_requirement:
            raise ValueError("quality_requirement is only valid for quality evaluation")
    elif evaluation_mode or quality_requirement:
        raise ValueError("evaluation_mode and quality_requirement are only valid for evaluation feedback")
    if kind in ("positive", "evaluation") and task["status"] != "verified":
        raise ValueError(f"{kind} feedback must reference a verified source task")
    if kind == "positive" and (failed or targets or target_task_id):
        raise ValueError("positive feedback learns verified actual actions; do not supply target/failed actions")
    if kind == "evaluation" and not failed:
        raise ValueError("evaluation requires specific failed actions")
    if kind == "evaluation" and not set(failed).issubset(task["actual_actions"]):
        raise ValueError("failed actions must have been executed by the verified source task")
    if kind == "request" and (failed or not targets or target_task_id):
        raise ValueError("request requires target actions and never takes failed actions or target_task_id")
    if kind == "request" and (not target_spec.strip() or not target_source_id.strip()):
        raise ValueError("request requires target specification and target source ID")
    if kind == "request" and target_source_id != task["target_source_id"]:
        raise ValueError("request target source ID must match the prepared task")
    if kind == "request" and source_message_id != task.get("source_message_id", task["target_source_id"]):
        raise ValueError("request source message ID must match the prepared task")
    if kind == "evaluation" and target_task_id:
        if not target_spec.strip() or not target_source_id.strip() or not targets:
            raise ValueError("an inline correction target requires task ID, specification, source ID, and actions")
        target_task = get_task(state, target_task_id)
        if target_source_id != target_task["target_source_id"]:
            raise ValueError("correction target source ID must match the prepared target task")
        if evaluation_mode == "selection" and set(failed) & set(targets):
            raise ValueError("failed and target actions must not overlap")
    if previous_output_id and task.get("output_id") and previous_output_id != task["output_id"]:
        raise ValueError("previous_output_id does not match task output")
    payload = {"event_id": event_id, "kind": kind, "score": score, "label": label,
               "task_id": task_id, "source_id": source_id, "previous_output_id": previous_output_id or task.get("output_id", ""),
               "source_message_id": source_message_id,
               "aspect": aspect, "quote": quote, "failed_actions": failed,
               "target_spec": target_spec, "target_source_id": target_source_id,
               "target_actions": targets, "target_task_id": target_task_id,
               "evaluation_mode": evaluation_mode, "quality_requirement": quality_requirement}
    for other in state["events"]:
        if other["source_id"] == source_id and other["event_id"] != event_id:
            raise ValueError("source_id already belongs to another event")
    existing = next((e for e in state["events"] if e["event_id"] == event_id), None)
    if existing:
        comparable = {k: existing.get(k) for k in payload}
        if comparable == payload: return {"status": "duplicate", "event_id": event_id}
        if not replace: raise ValueError("event_id exists; use --replace to correct it")
        old_learned_kind = (event_training_task(state, existing)["task_kind"]
                            if existing.get("status") == "learned" else None)
        revision = {k: existing.get(k) for k in payload}; revision["replaced_at"] = now()
        existing.setdefault("revisions", []).append(revision)
        for update in state["updates"]:
            if event_id in update.get("new_events", []):
                update.setdefault("superseded_events", []).append(
                    {"event_id": event_id, "superseded_at": revision["replaced_at"]})
        existing.update(payload); existing.pop("update_id", None); event = existing
    else:
        old_learned_kind = None
        if replace:
            raise ValueError("new event cannot use --replace")
        payload.update({"created_at": now(), "revisions": [], "action_evidence": {}})
        state["events"].append(payload); event = payload
    if replace:
        event["action_evidence"] = {}
    event["status"] = event_status(state, event)
    if old_learned_kind is not None:
        state["params"], state["training_trace"] = replay(state)
        refresh_context_cache(state, task_kinds=[old_learned_kind])
        state["training_integrity"] = training_integrity(state)
    return {"status": event["status"], "event_id": event_id, "kind": kind, "score": score}


def link_target(state, event_id, target_task_id, target_spec, target_source_id, target_actions):
    event = get_event(state, event_id)
    if event["kind"] != "evaluation":
        raise ValueError("only evaluation feedback can link a later correction target")
    target_task = get_task(state, target_task_id)
    if not all(x.strip() for x in (target_spec, target_source_id)):
        raise ValueError("target specification and source ID are required")
    actions = parse_actions(target_actions)
    if target_source_id != target_task["target_source_id"]:
        raise ValueError("correction target source ID must match the prepared target task")
    if event.get("evaluation_mode", "selection") == "selection" and set(event["failed_actions"]) & set(actions):
        raise ValueError("failed and target actions must not overlap")
    values = {"target_task_id": target_task_id, "target_spec": target_spec,
              "target_source_id": target_source_id, "target_actions": actions}
    if event.get("target_task_id"):
        if all(event.get(k) == v for k, v in values.items()):
            return {"status": "duplicate", "event_id": event_id}
        raise ValueError("target already linked; correct the event with --replace")
    event.update(values); event["status"] = event_status(state, event)
    return {"event_id": event_id, "status": event["status"], **values}


def train_event_step(params, event, task):
    if event["kind"] == "positive":
        grad = attraction_gradient(params, task_features(task), task["actual_actions"], event["score"])
    elif event["kind"] == "request":
        grad = attraction_gradient(params, task_features(task), event["target_actions"], abs(event["score"]))
    elif event.get("evaluation_mode", "selection") == "quality":
        grad = attraction_gradient(params, task_features(task), event["target_actions"], abs(event["score"]))
    else:
        grad = pair_gradient(params, task_features(task), event["failed_actions"], event["target_actions"], abs(event["score"]))
    updated, metrics = apply_step(params, grad)
    return updated, {"event_id": event["event_id"], "kind": event["kind"],
                     "score": event["score"], **metrics}


def replay(state, task_kind=None, task_index=None):
    p, trace = initial(), []
    task_index = task_index if task_index is not None else task_index_for(state)
    for event in state["events"]:
        if event.get("status") != "learned":
            continue
        task = event_training_task(state, event, task_index)
        if task_kind is not None and task["task_kind"] != task_kind:
            continue
        p, row = train_event_step(p, event, task)
        trace.append(row)
    return p, trace


def learn(state, update_id):
    if not update_id.strip(): raise ValueError("update ID is required")
    if any(u["update_id"] == update_id for u in state["updates"]):
        return {"status": "duplicate", "update_id": update_id}
    pending = []
    status_changed = False
    task_index = task_index_for(state)
    for event in state["events"]:
        if event.get("status") == "learned":
            continue
        status = event_status(state, event, task_index)
        if status == "ready":
            pending.append(event)
        elif event.get("status") != "learned":
            status_changed = status_changed or event.get("status") != status
            event["status"] = status
    integrity_valid = state.get("training_integrity") == training_integrity(state)
    if not pending:
        learned_count = sum(e.get("status") == "learned" for e in state["events"])
        if ((not learned_count and state["params"] == initial())
                or integrity_valid):
            return {"status": "no_ready_events", "update_id": update_id,
                    "parameter_delta_l2": 0.0, "replay_event_count": learned_count,
                    "state_changed": status_changed}
        before = state["params"]
        state["params"], trace = replay(state)
        state["training_trace"] = trace
        refresh_context_cache(state)
        state["training_integrity"] = training_integrity(state)
        delta = math.sqrt(sum((a-b)**2 for a, b in zip(flatten(before), flatten(state["params"]))))
        return {"status": "no_ready_events", "update_id": update_id,
                "parameter_delta_l2": delta, "replay_event_count": len(trace),
                "state_changed": True}
    old_learned = [event["event_id"] for event in state["events"]
                   if event.get("status") == "learned"]
    old_trace = state.get("training_trace")
    checkpoint_valid = (
        (not old_learned and state["params"] == initial() and old_trace in (None, []))
        or (integrity_valid and isinstance(old_trace, list)
            and [row.get("event_id") for row in old_trace] == old_learned))
    changed_kinds = {event_training_task(state, event, task_index)["task_kind"]
                     for event in pending}
    old_cache = {}
    if checkpoint_valid:
        for kind in changed_kinds:
            entry = state.get("context_cache", {}).get(kind)
            if not isinstance(entry, dict):
                continue
            digest, count = context_cache_key(state, kind, task_index)
            if (entry.get("version") == CONTEXT_CACHE_VERSION
                    and entry.get("event_digest") == digest
                    and entry.get("event_count") == count):
                old_cache[kind] = entry
    newly = []
    for event in pending:
        event["action_evidence"] = action_evidence(state, event, task_index)
        if event["kind"] == "positive" and not event.get("previous_output_id"):
            event["previous_output_id"] = event["action_evidence"]["output_id"]
        event["status"] = "learned"; event["update_id"] = update_id; newly.append(event["event_id"])
    before = state["params"]
    learned_order = [event["event_id"] for event in state["events"]
                     if event.get("status") == "learned"]
    append_only = checkpoint_valid and learned_order == old_learned + newly
    if append_only:
        params, trace = state["params"], list(old_trace or [])
        for event in state["events"]:
            if event["event_id"] in newly:
                params, row = train_event_step(params, event, event_training_task(state, event, task_index))
                trace.append(row)
        state["params"] = params
    else:
        state["params"], trace = replay(state)
    state["training_trace"] = trace
    newly_set = set(newly)
    if append_only:
        uncached = []
        for kind in changed_kinds:
            entry = old_cache.get(kind)
            if entry is None:
                uncached.append(kind)
                continue
            params = entry["params"]
            try:
                if len(flatten(params)) != PARAMETER_COUNT or not all(math.isfinite(v) for v in flatten(params)):
                    uncached.append(kind)
                    continue
            except (KeyError, TypeError):
                uncached.append(kind)
                continue
            for event in state["events"]:
                if event["event_id"] in newly_set:
                    task = event_training_task(state, event, task_index)
                    if task["task_kind"] == kind:
                        params, _ = train_event_step(params, event, task)
            digest, count = context_cache_key(state, kind, task_index)
            state["context_cache"][kind] = {"version": CONTEXT_CACHE_VERSION,
                                            "event_digest": digest,
                                            "event_count": count, "params": params}
        if uncached:
            refresh_context_cache(state, task_kinds=uncached)
    else:
        refresh_context_cache(state, task_kinds=changed_kinds)
    state["training_integrity"] = training_integrity(state)
    delta = math.sqrt(sum((a-b)**2 for a, b in zip(flatten(before), flatten(state["params"]))))
    trace_bytes = json.dumps(trace, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    update = {"update_id": update_id, "created_at": now(), "new_events": newly,
              "parameter_delta_l2": delta, "replay_event_count": len(trace),
              "incremental": append_only,
              "replay_trace_sha256": hashlib.sha256(trace_bytes).hexdigest(),
              "new_event_trace": [row for row in trace if row["event_id"] in newly]}
    state["updates"].append(update)
    return {"status": "updated", **update}


def report(state, current_executor="", current_profile=""):
    statuses = {}
    events = []
    task_index = task_index_for(state)
    for stored in state["events"]:
        event = copy.deepcopy(stored)
        if event.get("status") != "learned":
            event["status"] = event_status(state, event, task_index)
        statuses[event["status"]] = statuses.get(event["status"], 0)+1
        events.append(event)
    identity = copy.deepcopy(state.get("identity", {}))
    identity["current_executor"] = current_executor or None
    identity["current_profile"] = current_profile or None
    return {"version": STATE_VERSION, "report_schema_version": 2,
            "controller_parameter_count": PARAMETER_COUNT,
            "external_parameter_count": PARAMETER_COUNT,
            "deprecated_fields": {"external_parameter_count":
                                  "compatibility alias; use controller_parameter_count; removal not scheduled"},
            "controller_identity": identity,
            "task_count": len(state["tasks"]), "event_count": len(state["events"]),
            "preference_count": len(state["preferences"]),
            "event_statuses": statuses, "update_count": len(state["updates"]),
            "tasks": [{"task_id": t["task_id"], "status": t["status"],
                       "source_message_id": t.get("source_message_id"),
                       "upstream_task_ref": t.get("upstream_task_ref"),
                       "created_by": t.get("created_by"),
                       "completed_by": t.get("completed_by"),
                       "verified_by": t.get("verified_by"),
                       "required_actions": t["required_actions"],
                       "actual_actions": t.get("actual_actions", [])} for t in state["tasks"]],
            "preferences": state["preferences"], "events": events, "updates": state["updates"]}


def run_batch(state, plan_path, executor_id=""):
    """Apply already-evidenced operations to one in-memory state transaction."""
    plan = read_json(plan_path)
    if not isinstance(plan, dict) or not isinstance(plan.get("steps"), list):
        raise ValueError("batch plan requires a steps list")
    batch_id = plan.get("batch_id")
    if not isinstance(batch_id, str) or not batch_id.strip() or len(batch_id) > 128:
        raise ValueError("batch plan requires a stable batch_id")
    digest = hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":"),
                                       ensure_ascii=True, allow_nan=False).encode("utf-8")).hexdigest()
    for previous in state.get("batch_history", []):
        if previous["batch_id"] == batch_id:
            if previous["sha256"] != digest:
                raise ValueError("batch_id already belongs to different plan contents")
            return {"status": "duplicate", "batch_id": batch_id, "step_count": len(plan["steps"])}
    steps = plan["steps"]
    if not 1 <= len(steps) <= 8:
        raise ValueError("batch plan requires 1-8 steps")
    working = copy.deepcopy(state)
    allowed = {
        "prepare": {"task_id", "task_kind", "required_actions", "target_spec",
                    "target_source_id", "artifact_present", "evidence_required",
                    "revision_requested", "output_required", "source_message_id",
                    "upstream_task_ref", "factual_correction"},
        "complete": {"task_id", "output_id", "actions", "artifacts", "evidence_json"},
        "verify": {"task_id", "passed", "checks_json"},
        "feedback": {"event_id", "kind", "task_id", "source_id", "label", "score",
                     "quote", "aspect", "previous_output_id", "failed_actions",
                     "target_spec", "target_source_id", "target_actions", "target_task_id",
                     "evaluation_mode", "quality_requirement", "source_message_id", "replace"},
        "target": {"event_id", "target_task_id", "target_spec", "target_source_id",
                   "target_actions"},
        "learn": {"update_id"},
    }
    results = []
    for index, step in enumerate(steps):
        if not isinstance(step, dict) or step.get("op") not in allowed:
            raise ValueError(f"batch step {index+1} has invalid operation")
        op = step["op"]
        kwargs = {key: value for key, value in step.items() if key != "op"}
        if set(kwargs) - allowed[op]:
            raise ValueError(f"batch step {index+1} has unsupported fields")
        for path_field in ("evidence_json", "checks_json"):
            if kwargs.get(path_field) is not None:
                kwargs[path_field] = Path(kwargs[path_field])
        if op == "prepare":
            result = prepare(working, **kwargs, executor_id=executor_id)
        elif op == "complete":
            result = complete(working, **kwargs, executor_id=executor_id)
        elif op == "verify":
            if not isinstance(kwargs.get("passed"), bool):
                raise ValueError("batch verify requires boolean passed")
            result = verify(working, **kwargs, executor_id=executor_id)
        elif op == "feedback":
            if not isinstance(kwargs.get("quote"), str) or not kwargs["quote"].strip():
                raise ValueError("batch feedback requires a verified source quote")
            result = add_feedback(working, **kwargs)
        elif op == "target":
            result = link_target(working, **kwargs)
        else:
            result = learn(working, **kwargs)
        results.append({"op": op, "result": result})
    working.setdefault("batch_history", []).append({"batch_id": batch_id, "sha256": digest})
    state.clear(); state.update(working)
    return {"status": "applied", "batch_id": batch_id, "step_count": len(results), "steps": results}


def add_common_task_flags(parser):
    parser.add_argument("--artifact-present", action="store_true")
    parser.add_argument("--evidence-required", action="store_true")
    parser.add_argument("--revision-requested", action="store_true")
    parser.add_argument("--output-required", action="store_true")


def add_context_arguments(parser):
    parser.add_argument("--task-kind", choices=TASK_KINDS, required=True)
    parser.add_argument("--required-actions", default="")
    parser.add_argument("--compact", action="store_true")
    parser.add_argument("--for-model", action="store_true",
                        help="emit one concise UTF-8 guidance string without duplicate JSON fields")
    parser.add_argument("--factual-correction", action="store_true")
    add_common_task_flags(parser)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    location = p.add_mutually_exclusive_group()
    location.add_argument("--state", type=Path,
                          help="explicit external state path, primarily for isolated tests")
    location.add_argument("--profile",
                          help="stable model/profile ID; stores state under CWD/.positive-feedback/PROFILE")
    p.add_argument("--executor-id", default="",
                   help="host-provided stable executor ID; omit when unavailable rather than guessing")
    p.add_argument("--thread-id", default=os.environ.get("CODEX_THREAD_ID", ""),
                   help="host-provided stable task ID for session activation")
    p.add_argument("--session-state", type=Path,
                   help="explicit external session path, only for isolated tests")
    p.add_argument("--ascii-output", action="store_true",
                   help="use ASCII-escaped JSON for legacy consumers")
    sub = p.add_subparsers(dest="cmd", required=True)
    session = sub.add_parser("session", help="manage per-task skill activation independently of model profile")
    session_sub = session.add_subparsers(dest="session_cmd", required=True)
    session_sub.add_parser("status", help="read activation without creating files").add_argument(
        "--compact", action="store_true")
    for action in ("enable", "disable"):
        session_sub.add_parser(action).add_argument("--source-excerpt", required=True)
    x = sub.add_parser("context", help="emit read-only learned guidance before composing an answer")
    add_context_arguments(x)
    x = sub.add_parser("session-context", help="check activation and emit context in one read-only call")
    add_context_arguments(x)
    pref = sub.add_parser("preference", help="manage explicit scoped user preferences")
    pref_sub = pref.add_subparsers(dest="preference_cmd", required=True)
    pref_add = pref_sub.add_parser("add")
    pref_add.add_argument("--preference-id", required=True); pref_add.add_argument("--text", required=True)
    pref_add.add_argument("--source-id", required=True); pref_add.add_argument("--task-kinds", default="")
    pref_add.add_argument("--exclude-task-kinds", default=""); pref_add.add_argument("--priority", type=int, default=50)
    pref_add.add_argument("--expires-at", default=""); pref_add.add_argument("--conflict-group", default="")
    pref_add.add_argument("--replace", action="store_true")
    pref_enable = pref_sub.add_parser("enable"); pref_enable.add_argument("--preference-id", required=True)
    pref_disable = pref_sub.add_parser("disable"); pref_disable.add_argument("--preference-id", required=True)
    pref_list = pref_sub.add_parser("list"); pref_list.add_argument("--task-kind", choices=TASK_KINDS, default="")
    a = sub.add_parser("prepare")
    a.add_argument("--task-id", required=True); a.add_argument("--task-kind", choices=TASK_KINDS, required=True)
    a.add_argument("--required-actions", required=True); a.add_argument("--target-spec", required=True)
    a.add_argument("--target-source-id", required=True); a.add_argument("--source-message-id", default="")
    a.add_argument("--upstream-task-ref", default=""); a.add_argument("--factual-correction", action="store_true")
    add_common_task_flags(a)
    c = sub.add_parser("complete")
    c.add_argument("--task-id", required=True); c.add_argument("--output-id", required=True)
    c.add_argument("--actions", required=True); c.add_argument("--artifact", dest="artifacts", action="append", default=[])
    c.add_argument("--evidence-json", type=Path)
    v = sub.add_parser("verify")
    v.add_argument("--task-id", required=True); v.add_argument("--result", choices=["pass", "fail"], required=True)
    v.add_argument("--checks-json", type=Path, required=True)
    f = sub.add_parser("feedback")
    f.add_argument("--event-id", required=True); f.add_argument("--kind", choices=LABELS_BY_KIND, required=True)
    f.add_argument("--task-id", required=True); f.add_argument("--source-id", required=True)
    g = f.add_mutually_exclusive_group(required=True); g.add_argument("--label"); g.add_argument("--score", type=signed_number)
    f.add_argument("--quote", required=True); f.add_argument("--source-message-id", default="")
    f.add_argument("--aspect", default="")
    f.add_argument("--previous-output-id", default=""); f.add_argument("--failed-actions", default="")
    f.add_argument("--target-spec", default=""); f.add_argument("--target-source-id", default="")
    f.add_argument("--target-actions", default=""); f.add_argument("--target-task-id", default="")
    f.add_argument("--evaluation-mode", choices=EVALUATION_MODES, default="")
    f.add_argument("--quality-requirement", default="")
    f.add_argument("--replace", action="store_true")
    t = sub.add_parser("target")
    t.add_argument("--event-id", required=True); t.add_argument("--target-task-id", required=True)
    t.add_argument("--target-spec", required=True); t.add_argument("--target-source-id", required=True)
    t.add_argument("--target-actions", required=True)
    l = sub.add_parser("learn"); l.add_argument("--update-id", required=True)
    b = sub.add_parser("batch", help="atomically prepare and apply 1-8 already-evidenced steps")
    b.add_argument("--plan-json", type=Path, required=True)
    b.add_argument("--compact", action="store_true", help="return step statuses without full task records")
    sub.add_parser("report")
    args = vars(p.parse_args())
    ascii_output = args.pop("ascii_output")
    for_model = args.pop("for_model", False)
    cmd, state_path, profile = args.pop("cmd"), args.pop("state"), args.pop("profile")
    executor_id = args.pop("executor_id")
    thread_id, session_state = args.pop("thread_id"), args.pop("session_state")
    session_cmd = args.pop("session_cmd", None)
    preference_cmd = args.pop("preference_cmd", None)
    try:
        if not ascii_output and hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        def emit(result, model_text=False):
            if model_text:
                if result.get("active") is False:
                    print("active: false")
                else:
                    if result.get("active"):
                        print("active: true")
                    if result.get("skill_sha256"):
                        print("skill_sha256: " + result["skill_sha256"])
                    print("task_kind: " + result["task_kind"])
                    print("guidance: " + result["instruction_text"])
            else:
                print(json.dumps(result, ensure_ascii=ascii_output,
                                 indent=None if result.get("mode") == "pre_generation"
                                 and args.get("compact") else 2, allow_nan=False))
        if cmd == "session":
            if state_path is not None or profile or executor_id:
                raise ValueError("session activation is independent of controller profile and state")
            result = session_command(session_cmd, thread_id, session_state,
                                     args.get("source_excerpt", ""))
            if session_cmd == "status" and args.get("compact"):
                result = {"status": result["status"], "active": result["active"]}
                if result["active"]:
                    result["skill_sha256"] = file_sha256(SKILL_ROOT / "SKILL.md")
            emit(result)
            return
        if cmd == "session-context":
            activation = session_command("status", thread_id, session_state)
            if not activation["active"]:
                result = {"activation": activation["status"], "active": False,
                          "thread_id": activation.get("thread_id")}
            else:
                if state_path is None:
                    if not profile:
                        raise ValueError("active session-context requires --profile or isolated --state")
                    state_path = default_state_path(profile)
                else:
                    state_path = ensure_external_path(state_path)
                snapshot = read_context_snapshot(state_path, args["task_kind"])
                state = ({"preferences": load_preferences(preferences_path_for(state_path))}
                         if snapshot is not None else load(state_path))
                if snapshot is None:
                    state["preferences"] = load_preferences(preferences_path_for(state_path))
                result = pre_generation_context(state, **args, snapshot_entry=snapshot)
                result["activation"] = "active"
                result["active"] = True
                result["skill_sha256"] = file_sha256(SKILL_ROOT / "SKILL.md")
                if not args.get("compact"):
                    result["state_path"] = str(state_path)
            emit(result, model_text=for_model)
            return
        if cmd == "feedback" and not args["quote"].strip():
            raise ValueError("feedback --quote must contain a verified source excerpt")
        if state_path is None:
            if not profile:
                raise ValueError("provide --profile for model-isolated working-directory storage, or --state for an isolated test")
            state_path = default_state_path(profile)
        else:
            state_path = ensure_external_path(state_path)
        preferences_path = preferences_path_for(state_path)
        read_only = cmd in ("context", "report") or (cmd == "preference" and preference_cmd == "list")

        def execute(state):
            if cmd == "context": return pre_generation_context(state, **args)
            if cmd == "preference":
                if preference_cmd == "add": return add_preference(state, **args)
                if preference_cmd == "enable": return set_preference_status(state, args["preference_id"], "active")
                if preference_cmd == "disable": return set_preference_status(state, args["preference_id"], "disabled")
                return list_preferences(state, **args)
            if cmd == "prepare": return prepare(state, **args, executor_id=executor_id)
            if cmd == "complete": return complete(state, **args, executor_id=executor_id)
            if cmd == "verify": return verify(state, args["task_id"], args["result"] == "pass",
                                                args["checks_json"], executor_id=executor_id)
            if cmd == "feedback": return add_feedback(state, **args)
            if cmd == "target": return link_target(state, **args)
            if cmd == "learn": return learn(state, **args)
            if cmd == "batch": return run_batch(state, args["plan_json"], executor_id=executor_id)
            return report(state, current_executor=executor_id, current_profile=profile or "")

        if read_only:
            snapshot = (read_context_snapshot(state_path, args["task_kind"])
                        if cmd == "context" else None)
            state = ({"preferences": load_preferences(preferences_path)}
                     if snapshot is not None else load(state_path))
            if snapshot is None:
                state["preferences"] = load_preferences(preferences_path)
            result = (pre_generation_context(state, **args, snapshot_entry=snapshot)
                      if cmd == "context" and snapshot is not None else execute(state))
        else:
            lock_target = preferences_path if cmd == "preference" else state_path
            with state_lock(lock_target):
                state = load(state_path)
                state["preferences"] = load_preferences(preferences_path)
                identity_before = copy.deepcopy(state.get("identity", {}))
                register_identity(state, profile=profile or "", executor_id=executor_id)
                result = execute(state)
                if cmd == "preference":
                    save_preferences(preferences_path, state["preferences"])
                elif not (cmd == "learn" and result.get("status") == "no_ready_events"
                          and not result.get("state_changed")
                          and state.get("identity", {}) == identity_before):
                    save(state_path, state)
        if cmd == "batch" and args.get("compact"):
            full_result = result
            result = {key: result[key] for key in ("status", "batch_id", "step_count")}
            if result["status"] == "applied":
                result["steps"] = [{"op": step["op"],
                                    "status": step["result"].get("status")}
                                   for step in full_result["steps"]]
        if not (cmd == "context" and args.get("compact")):
            result["state_path"] = str(state_path)
            result["preferences_path"] = str(preferences_path)
        emit(result, model_text=cmd == "context" and for_model)
    except (ValueError, KeyError, OSError, StopIteration, TypeError, json.JSONDecodeError) as e:
        p.exit(1, f"Error: {e}\n")


if __name__ == "__main__":
    main()
