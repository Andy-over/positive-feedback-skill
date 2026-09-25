"""Auditable directional feedback and request-signal ledger; standard library only."""
import argparse
import hashlib
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path

POSITIVE_LABELS = {
    "很好": 1.0, "十分感谢": 1.0, "特别感谢": 0.9,
    "不错": 0.8, "非常感谢": 0.8, "多谢": 0.7,
    "可以": 0.6, "还可以": 0.6, "谢谢": 0.6,
    "基本满意": 0.6, "还不错": 0.7, "有帮助": 0.7, "有用": 0.7,
    "满意": 0.8, "做得不错": 0.8, "很满意": 0.9, "很棒": 0.9,
    "非常满意": 1.0, "太棒了": 1.0, "完全符合要求": 1.0,
}
EVALUATION_LABELS = {"不太满意": -0.2, "不满意": -0.4, "很差": -0.7,
                     "太差劲了": -1.0, "垃圾": -1.0,
                     "有待改进": -0.2, "不够好": -0.2,
                     "不够准确": -0.4, "不够完整": -0.4,
                     "有误": -0.5, "不准确": -0.5, "有遗漏": -0.5,
                     "答非所问": -0.6, "偏离要求": -0.6, "很不满意": -0.7,
                     "完全错误": -0.9, "完全不符合要求": -0.9}
REQUEST_LABELS = {"希望你": -0.15, "需要你": -0.25, "我想": -0.25, "想": -0.25,
                  "要求你": -0.4, "命令你": -1.0,
                  "希望": -0.05, "我希望": -0.05, "能否": -0.05, "可否": -0.05,
                  "请": -0.15, "请你": -0.15, "帮我": -0.15, "麻烦你": -0.15,
                  "我需要": -0.15, "我想要": -0.15, "想要": -0.15,
                  "务必": -0.3, "必须": -0.3, "请务必": -0.3}
LABELS_BY_KIND = {"positive": POSITIVE_LABELS, "evaluation": EVALUATION_LABELS,
                  "request": REQUEST_LABELS}
LABELS = POSITIVE_LABELS  # compatibility for callers that only need positive labels
SKILL_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = ".positive-feedback"
PROFILE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def external_directory(path):
    resolved = Path(path).resolve()
    try:
        relative = resolved.relative_to(SKILL_ROOT)
    except ValueError:
        return resolved
    if relative.parts and relative.parts[0] == RUNTIME_DIR and len(relative.parts) >= 3:
        return resolved
    raise ValueError("feedback data inside the skill directory is allowed only under .positive-feedback/MODEL_PROFILE")


def default_directory(profile, cwd=None):
    if not profile or not PROFILE_PATTERN.fullmatch(profile):
        raise ValueError("profile must be 1-64 characters using letters, digits, dot, underscore, or hyphen")
    root = SKILL_ROOT if cwd is None else Path(cwd).resolve()
    runtime = root / RUNTIME_DIR
    if runtime.is_symlink() or (runtime / profile).is_symlink():
        raise ValueError("profile data directory must not be a symlink")
    return external_directory(runtime / profile / "events")


def signed_number(value):
    value = float(value)
    if not math.isfinite(value) or not -1 <= value <= 1:
        raise ValueError("score must be finite and in [-1,1]")
    return value


def number(value):
    """Compatibility validator for the original nonnegative feedback API."""
    value = signed_number(value)
    if value < 0:
        raise ValueError("positive score must be in [0,1]")
    return value


def smoothing(value):
    value = number(value)
    if value == 0:
        raise ValueError("alpha must be in (0,1]")
    return value


def validate(kind, score):
    score = signed_number(score)
    if kind == "positive" and score < 0:
        raise ValueError("positive score must be in [0,1]")
    if kind in ("evaluation", "request") and not -1 <= score < 0:
        raise ValueError(f"{kind} score must be in [-1,0)")
    if kind not in LABELS_BY_KIND:
        raise ValueError("invalid signal kind")
    return score


def read_events(directory):
    events = []
    for path in sorted(Path(directory).glob("*.json")):
        e = json.loads(path.read_text(encoding="utf-8"))
        validate(e["kind"], e["score"])
        if e["version"] != 2 or not e["response_id"] or not e["source_id"]:
            raise ValueError(f"invalid event: {path}")
        datetime.fromisoformat(e["created_at"])
        if not isinstance(e["replace"], bool):
            raise ValueError("replace must be boolean")
        events.append(e)
    return sorted(events, key=lambda e: (e["created_at"], e["source_id"]))


def effective(events):
    answers, sources = {}, set()
    for e in events:
        if e["source_id"] in sources:
            raise ValueError("duplicate source_id in ledger")
        sources.add(e["source_id"])
        key = (e["response_id"], e["kind"])
        exists = key in answers
        if exists != e["replace"]:
            raise ValueError("replacement flag does not match response/kind history")
        answers[key] = e
    return list(answers.values())


def add(directory, response_id, source_id, kind="positive", label=None, score=None,
        quote="", replace=False):
    directory = external_directory(directory)
    if not response_id.strip() or not source_id.strip():
        raise ValueError("IDs must not be empty")
    if (label is None) == (score is None):
        raise ValueError("provide exactly one of label or score")
    if label is not None:
        try:
            score = LABELS_BY_KIND[kind][label]
        except KeyError as exc:
            raise ValueError("label does not belong to signal kind") from exc
    score = validate(kind, score)
    quote = re.sub(r"\s+", " ", quote or "").strip()
    if len(quote) > 800:
        raise ValueError("quote must be at most 800 characters")
    payload = dict(version=2, response_id=response_id, source_id=source_id,
                   kind=kind, label=label, score=score,
                   quote=quote, replace=replace)
    events = read_events(directory)
    current = effective(events)
    for old in events:
        if old["source_id"] == source_id:
            if any(old[k] != v for k, v in payload.items()):
                raise ValueError("source_id reused with different content")
            return {"status": "duplicate", "event": old}
    key_exists = any(e["response_id"] == response_id and e["kind"] == kind for e in current)
    if key_exists != replace:
        raise ValueError("existing response/kind needs --replace; new entry cannot use it")
    payload["created_at"] = datetime.now(timezone.utc).isoformat()
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    runtime_root = next((p for p in directory.parents if p.name == RUNTIME_DIR), None)
    if runtime_root is not None:
        ignore = runtime_root / ".gitignore"
        if not ignore.exists():
            ignore.write_text("*\n", encoding="utf-8")
    path = directory / (hashlib.sha256(source_id.encode()).hexdigest() + ".json")
    with path.open("x", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, allow_nan=False)
    return {"status": "recorded", "path": str(path), "event": payload}


def report(directory, alpha=0.2):
    alpha = smoothing(alpha)
    events, rows = read_events(directory), None
    rows = effective(events)
    groups = {}
    for kind in LABELS_BY_KIND:
        values = [e["score"] for e in rows if e["kind"] == kind]
        ewma = None
        for value in values:
            ewma = value if ewma is None else (1-alpha)*ewma + alpha*value
        groups[kind] = {"count": len(values),
                        "sum": math.fsum(values) if values else None,
                        "mean": math.fsum(values)/len(values) if values else None,
                        "ewma": ewma}
    return {"version": 2, "event_count": len(events), "effective_events": len(rows),
            "alpha": alpha, "by_kind": groups, "events": rows,
            "interpretation": "evaluation is corrective; request is target attraction and does not reject the prior result; missing is not zero"}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    location = p.add_mutually_exclusive_group()
    location.add_argument("--data-dir", type=Path,
                          help="explicit external ledger path, primarily for isolated tests")
    location.add_argument("--profile",
                          help="stable model/profile ID; stores data under SKILL_ROOT/.positive-feedback/PROFILE")
    sub = p.add_subparsers(dest="command", required=True)
    a = sub.add_parser("add")
    a.add_argument("--response-id", required=True)
    a.add_argument("--source-id", required=True)
    a.add_argument("--kind", choices=LABELS_BY_KIND, required=True)
    g = a.add_mutually_exclusive_group(required=True)
    g.add_argument("--label")
    g.add_argument("--score", type=signed_number)
    a.add_argument("--quote", required=True)
    a.add_argument("--replace", action="store_true")
    r = sub.add_parser("report")
    r.add_argument("--alpha", type=smoothing, default=0.2)
    args = vars(p.parse_args())
    command, directory, profile = args.pop("command"), args.pop("data_dir"), args.pop("profile")
    try:
        if command == "add" and not args["quote"].strip():
            raise ValueError("add --quote must contain a verified source excerpt")
        if directory is None:
            if not profile:
                raise ValueError("provide --profile for skill-root model storage, or --data-dir for an isolated test")
            directory = default_directory(profile)
        else:
            directory = external_directory(directory)
        result = add(directory, **args) if command == "add" else report(directory, **args)
        result["data_dir"] = str(directory)
        print(json.dumps(result, ensure_ascii=True, indent=2, allow_nan=False))
    except (ValueError, KeyError, OSError, TypeError, json.JSONDecodeError) as e:
        p.exit(1, f"Error: {e}\n")


if __name__ == "__main__":
    main()
