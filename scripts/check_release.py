"""Audit tracked Skill files and build a data-free release archive."""

import argparse
import hashlib
import json
import subprocess
import sys
import zipfile
from pathlib import Path, PurePosixPath

PRIVATE_SUFFIXES = {".safetensors", ".pt", ".pth", ".ckpt", ".bin"}
ROOT_ONLY = {"README.md", ".gitignore"}


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tracked_paths(repo):
    result = subprocess.run(["git", "-C", str(repo), "ls-files", "-z", "--cached"],
                            capture_output=True, check=True)
    return [item.decode("utf-8") for item in result.stdout.split(b"\0") if item]


def private_path(name):
    rel = PurePosixPath(name)
    parts = rel.parts
    if not parts or rel.is_absolute() or ".." in parts:
        return True
    if parts[0] == ".positive-feedback":
        return True
    if parts[0] == "training-data" and name != "training-data/.gitignore":
        return True
    if rel.name == ".env" or rel.name.startswith(".env."):
        return True
    return rel.suffix.lower() in PRIVATE_SUFFIXES


def audit(repo, installed=None):
    repo = Path(repo).resolve()
    installed = Path(installed).resolve() if installed else None
    issues, checked = [], []
    for name in tracked_paths(repo):
        source = repo / name
        if private_path(name):
            issues.append(f"tracked private path: {name}")
            continue
        if not source.is_file() or source.is_symlink():
            issues.append(f"missing or linked tracked file: {name}")
            continue
        if installed and name not in ROOT_ONLY:
            target = installed / name
            if not target.is_file() or target.is_symlink() or sha256(target) != sha256(source):
                issues.append(f"installed/repository mismatch: {name}")
        checked.append(name)
    return {"ok": not issues, "tracked_file_count": len(checked),
            "issues": issues, "files": checked}


def package(repo, output, files):
    output = Path(output).resolve()
    if output.exists():
        raise ValueError("release archive already exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in files:
            if private_path(name):
                raise ValueError("private path in package set")
            archive.write(Path(repo) / name, name)
    with zipfile.ZipFile(output) as archive:
        if any(private_path(name) for name in archive.namelist()):
            raise ValueError("private path entered release archive")
    return {"path": str(output), "sha256": sha256(output)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--installed", type=Path)
    parser.add_argument("--package", type=Path)
    args = parser.parse_args(argv)
    try:
        result = audit(args.repo, args.installed)
        if not result["ok"]:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 1
        if args.package:
            result["package"] = package(args.repo, args.package, result["files"])
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError, zipfile.BadZipFile) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
