"""Keep chat records separate by default and share only by explicit link."""
import argparse
import json
import os
from pathlib import Path
import re
import sys

SKILL_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = ".positive-feedback"
PROFILE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
THREAD_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
NAME_PATTERN = re.compile(r"^[\w][\w .-]{0,63}$", re.UNICODE)
WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{x}" for x in range(1, 10)),
                    *(f"LPT{x}" for x in range(1, 10))}


def validate_profile(profile):
    if not profile or not PROFILE_PATTERN.fullmatch(profile):
        raise ValueError("profile must be a stable 1-64 character model/profile ID")
    return profile


def validate_thread(thread_id):
    if not thread_id or not THREAD_PATTERN.fullmatch(thread_id):
        raise ValueError("a stable host chat ID is required for profile records")
    return thread_id


def validate_name(name):
    if not isinstance(name, str) or not NAME_PATTERN.fullmatch(name):
        raise ValueError("record name must be 1-64 letters, digits, spaces, dots, underscores or hyphens")
    if name.endswith((".", " ")) or name.upper().split(".")[0] in WINDOWS_RESERVED:
        raise ValueError("record name is not a portable directory name")
    return name


def profile_directory(profile, skill_root=None):
    root = Path(skill_root).resolve() if skill_root is not None else SKILL_ROOT
    runtime = root / RUNTIME_DIR
    directory = runtime / validate_profile(profile)
    if runtime.is_symlink() or directory.is_symlink() or (directory / "chats").is_symlink():
        raise ValueError("profile record directory must not be a symlink")
    return directory


def _names_path(directory):
    path = directory / "record-names.json"
    if path.is_symlink():
        raise ValueError("record name registry must not be a symlink")
    return path


def _load_names(directory):
    path = _names_path(directory)
    if not path.exists():
        return {}
    record = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(record, dict) or record.get("version") != 1 or not isinstance(record.get("names"), dict):
        raise ValueError("invalid record name registry")
    for thread, name in record["names"].items():
        validate_thread(thread)
        validate_name(name)
    return record["names"]


def _links_path(directory):
    path = directory / "record-links.json"
    if path.is_symlink():
        raise ValueError("record link registry must not be a symlink")
    return path


def _load_links(directory):
    path = _links_path(directory)
    if not path.exists():
        return {}
    record = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(record, dict) or record.get("version") != 1 or not isinstance(record.get("links"), dict):
        raise ValueError("invalid record link registry")
    for source, target in record["links"].items():
        validate_thread(source)
        validate_thread(target)
    return record["links"]


def _owner(thread, links):
    seen = set()
    while thread in links:
        if thread in seen:
            raise ValueError("cyclic chat record links")
        seen.add(thread)
        thread = links[thread]
    return thread


def _save_links(directory, links):
    directory.mkdir(parents=True, exist_ok=True)
    runtime = directory.parent
    ignore = runtime / ".gitignore"
    if not ignore.exists():
        ignore.write_text("*\n", encoding="utf-8")
    path = _links_path(directory)
    temporary = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump({"version": 1, "links": links}, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _folder(thread_id, name):
    return f"chat-{thread_id}" + (f"--{name}" if name else "")


def chat_directory(profile, thread_id=None, skill_root=None):
    thread = validate_thread(thread_id if thread_id is not None else os.environ.get("CODEX_THREAD_ID", ""))
    directory = profile_directory(profile, skill_root)
    owner = _owner(thread, _load_links(directory))
    name = _load_names(directory).get(owner)
    path = directory / "chats" / _folder(owner, name)
    if path.is_symlink():
        raise ValueError("chat record directory must not be a symlink")
    return path


def set_record_name(profile, thread_id, name, skill_root=None):
    thread = validate_thread(thread_id)
    display = validate_name(name)
    directory = profile_directory(profile, skill_root)
    if thread in _load_links(directory):
        raise ValueError("a linked chat cannot rename its owner's record")
    names = _load_names(directory)
    if any(other != thread and value.casefold() == display.casefold()
           for other, value in names.items()):
        raise ValueError("record name is already used by another chat")
    old = directory / "chats" / _folder(thread, names.get(thread))
    new = directory / "chats" / _folder(thread, display)
    if old.is_symlink() or new.is_symlink():
        raise ValueError("chat record directory must not be a symlink")
    if old == new:
        return {"status": "unchanged", "name": display, "record_dir": str(new)}
    if new.exists():
        raise ValueError("target record directory already exists")
    new.parent.mkdir(parents=True, exist_ok=True)
    if old.exists():
        old.rename(new)
    else:
        new.mkdir()
    names[thread] = display
    runtime = directory.parent
    ignore = runtime / ".gitignore"
    if not ignore.exists():
        ignore.write_text("*\n", encoding="utf-8")
    path = _names_path(directory)
    temporary = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump({"version": 1, "names": names}, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        new.rename(old) if old != new and not old.exists() else None
        raise
    return {"status": "named", "name": display, "record_dir": str(new)}


def link_record(profile, thread_id, *, to_thread_id=None, to_name=None, skill_root=None):
    thread = validate_thread(thread_id)
    if (to_thread_id is None) == (to_name is None):
        raise ValueError("provide exactly one target chat ID or record name")
    directory = profile_directory(profile, skill_root)
    names = _load_names(directory)
    links = _load_links(directory)
    if to_name is not None:
        display = validate_name(to_name)
        matches = [key for key, value in names.items() if value.casefold() == display.casefold()]
        if len(matches) != 1:
            raise ValueError("record name must identify exactly one chat")
        target = matches[0]
    else:
        target = validate_thread(to_thread_id)
    owner = _owner(target, links)
    if owner == thread:
        raise ValueError("cannot link a chat to its own record")
    record = directory / "chats" / _folder(owner, names.get(owner))
    if record.is_symlink() or not record.is_dir():
        raise ValueError("target chat record does not exist")
    if links.get(thread) == owner:
        return {"status": "unchanged", "owner_thread_id": owner, "record_dir": str(record)}
    links[thread] = owner
    _save_links(directory, links)
    return {"status": "linked", "owner_thread_id": owner, "record_dir": str(record)}


def unlink_record(profile, thread_id, skill_root=None):
    thread = validate_thread(thread_id)
    directory = profile_directory(profile, skill_root)
    links = _load_links(directory)
    if thread not in links:
        return {"status": "unchanged", "record_dir": str(chat_directory(profile, thread, skill_root))}
    del links[thread]
    _save_links(directory, links)
    return {"status": "unlinked", "record_dir": str(chat_directory(profile, thread, skill_root))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--thread-id", default=os.environ.get("CODEX_THREAD_ID", ""))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("set-name").add_argument("--name", required=True)
    link = sub.add_parser("link")
    target = link.add_mutually_exclusive_group(required=True)
    target.add_argument("--to-thread-id")
    target.add_argument("--to-name")
    sub.add_parser("unlink")
    args = parser.parse_args()
    try:
        if args.command == "status":
            path = chat_directory(args.profile, args.thread_id)
            directory = profile_directory(args.profile)
            owner = _owner(validate_thread(args.thread_id), _load_links(directory))
            result = {"status": "ok", "name": _load_names(directory).get(owner, ""),
                      "owner_thread_id": owner, "linked": owner != args.thread_id,
                      "record_dir": str(path)}
        elif args.command == "set-name":
            result = set_record_name(args.profile, args.thread_id, args.name)
        elif args.command == "link":
            result = link_record(args.profile, args.thread_id,
                                 to_thread_id=args.to_thread_id, to_name=args.to_name)
        else:
            result = unlink_record(args.profile, args.thread_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, OSError, TypeError, json.JSONDecodeError) as exc:
        parser.exit(1, f"Error: {exc}\n")


if __name__ == "__main__":
    main()
