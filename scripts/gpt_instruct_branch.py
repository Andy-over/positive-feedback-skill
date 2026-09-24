#!/usr/bin/env python3
"""Verify and stage the bundled gpt-instruct project without touching live state."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
PROJECT = SKILL_ROOT / "branches" / "gpt-instruct"
MANIFEST = SKILL_ROOT / "branches" / "gpt-instruct.manifest.json"
# Original archives remain untouched. Missing plaintext sources are reconstructed
# only in prepared workspaces so the upstream archive-sync workflow can run.
PROMPT_SOURCES = (
    ("gpt-6-astra-v1.zip", "gpt-6-astra-v1.md", "gpt-6-astra-v1.md"),
    ("historical-versions/gpt-6-astra-v1-rc1.zip", "historical-versions/gpt-6-astra-v1-rc1.md", "gpt-6-astra-v1-rc1.md"),
    ("gpt-5.6-sol-v45.zip", "gpt-5.6-sol-v45.md", "gpt-5.6-sol-unrestricted-v45.md"),
    ("historical-versions/gpt-5.6-sol-unrestricted-v42.zip", "historical-versions/gpt-5.6-sol-unrestricted-v42.md", "gpt-5.6-sol-unrestricted-v42.md"),
    ("historical-versions/gpt-5.6-sol-unrestricted-v41.zip", "historical-versions/gpt-5.6-sol-unrestricted-v41.md", "gpt-5.6-sol-unrestricted-v41.md"),
    ("historical-versions/gpt-5.6-sol-unrestricted-v41-skills.zip", "historical-versions/gpt-5.6-sol-unrestricted-v41-skills.md", "gpt-5.6-sol-unrestricted-v41-skills.md"),
    ("historical-versions/gpt-5.6-sol-unrestricted-v24.zip", "reports/prompt_candidates/gpt-5.6-sol-unrestricted-v24.md", "gpt-5.6-sol-unrestricted-v24.md"),
    ("historical-versions/gpt-5.6-sol-unrestricted-v35.zip", "reports/prompt_candidates/gpt-5.6-sol-unrestricted-v35.md", "gpt-5.6-sol-unrestricted-v35.md"),
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify() -> dict[str, object]:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    expected = data["files"]
    if not isinstance(expected, dict) or len(expected) != data["file_count"]:
        raise ValueError("invalid branch manifest")
    actual = {
        path.relative_to(PROJECT).as_posix()
        for path in PROJECT.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }
    if actual != set(expected):
        raise ValueError(f"branch file set differs: missing={sorted(set(expected)-actual)}, extra={sorted(actual-set(expected))}")
    archive_count = 0
    for name, digest in expected.items():
        path = PROJECT / name
        if path.is_symlink() or sha256(path) != digest:
            raise ValueError(f"branch hash differs: {name}")
        if path.suffix.lower() == ".zip":
            with zipfile.ZipFile(path) as archive:
                if archive.testzip() is not None:
                    raise ValueError(f"corrupt archive: {name}")
                members = [member for member in archive.namelist() if not member.endswith("/")]
                if len(members) != 1 or Path(members[0]).name != members[0] or "\\" in members[0]:
                    raise ValueError(f"unexpected archive members: {name}")
            archive_count += 1
    return {"status": "ok", "files": len(expected), "archives": archive_count, "commit": data["commit"]}


def safe_script_member(name: str) -> bool:
    path = Path(name)
    return bool(name) and path.name == name and name.endswith(".py") and not path.is_absolute() and "\\" not in name


def prepare(output: Path) -> dict[str, object]:
    verified = verify()
    output = output.expanduser().resolve()
    if output.exists():
        raise FileExistsError(f"output already exists: {output}")
    if output == SKILL_ROOT or SKILL_ROOT in output.parents:
        raise ValueError("output must be outside the installed skill")
    # Check every archive before writing the first output file.
    archives = sorted((PROJECT / "scripts").glob("*.zip"))
    members: list[tuple[Path, str, bytes]] = []
    for archive in archives:
        with zipfile.ZipFile(archive) as z:
            names = [n for n in z.namelist() if not n.endswith("/")]
            if len(names) != 1 or not safe_script_member(names[0]) or z.testzip() is not None:
                raise ValueError(f"unsafe script archive: {archive.name}")
            members.append((archive, names[0], z.read(names[0])))
    prompt_members: list[tuple[str, bytes]] = []
    for archive_name, source_name, member_name in PROMPT_SOURCES:
        with zipfile.ZipFile(PROJECT / archive_name) as z:
            names = [n for n in z.namelist() if not n.endswith("/")]
            if names != [member_name] or z.testzip() is not None:
                raise ValueError(f"invalid prompt archive: {archive_name}")
            prompt_members.append((source_name, z.read(member_name)))
    installer_source = (PROJECT / "codex-instruct.py").read_bytes()
    old = b'with os.fdopen(descriptor, "w", encoding="utf-8") as handle:'
    new = b'with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:'
    if installer_source.count(old) != 1:
        raise ValueError("installer compatibility patch site changed")
    shutil.copytree(PROJECT, output)
    for _archive, name, data in members:
        dest = output / "scripts" / name
        if dest.exists() and dest.read_bytes() != data:
            raise ValueError(f"script conflict: {name}")
        dest.write_bytes(data)
    restored = 0
    for source_name, data in prompt_members:
        dest = output / source_name
        if dest.exists() and dest.read_bytes() != data:
            raise ValueError(f"prompt source conflict: {source_name}")
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            restored += 1
    # Windows text mode otherwise changes packaged prompt LF bytes to CRLF.
    installer = output / "codex-instruct.py"
    installer.write_bytes(installer_source.replace(old, new))
    return {**verified, "output": str(output), "unpacked_scripts": len(members), "restored_prompt_sources": restored, "compatibility_patch": "preserve prompt bytes on Windows"}


def test() -> int:
    with tempfile.TemporaryDirectory(prefix="gpt-instruct-branch-test-") as temporary:
        root = Path(temporary)
        staged = root / "project"
        prepare(staged)
        home = root / "home"
        codex = root / "codex"
        for path in (home, codex, root / "config", root / "cache", root / "data", root / "tmp"):
            path.mkdir()
        env = os.environ.copy()
        env.update({
            "HOME": str(home), "USERPROFILE": str(home), "CODEX_HOME": str(codex),
            "XDG_CONFIG_HOME": str(root / "config"), "XDG_CACHE_HOME": str(root / "cache"),
            "XDG_DATA_HOME": str(root / "data"), "TMPDIR": str(root / "tmp"),
            "TEMP": str(root / "tmp"), "TMP": str(root / "tmp"),
        })
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        compile_check = subprocess.run([sys.executable, "-m", "compileall", "-q", str(staged)], cwd=staged, env=env, check=False)
        if compile_check.returncode:
            return compile_check.returncode
        sync = subprocess.run([sys.executable, "sync-archives.py", "--check"], cwd=staged, env=env, check=False)
        if sync.returncode:
            return sync.returncode
        probe_target = root / "symlink-probe-target"
        probe_link = root / "symlink-probe-link"
        probe_target.write_text("probe", encoding="utf-8")
        try:
            probe_link.symlink_to(probe_target)
            can_symlink = True
            probe_link.unlink()
        except OSError:
            can_symlink = False
        runner = (
            "import sys, unittest; "
            "suite=unittest.defaultTestLoader.discover('unit-tests'); "
            "cases=[]; "
            "walk=lambda node: [walk(item) for item in node] if isinstance(node,unittest.TestSuite) else cases.append(node); "
            "walk(suite); "
            "[(setattr(type(case),case._testMethodName,unittest.skip('symlink privilege unavailable')(getattr(type(case),case._testMethodName)))) "
            "for case in cases if not bool(int(sys.argv[1])) and case._testMethodName=='test_atomic_config_update_preserves_symlink']; "
            "result=unittest.TextTestRunner(verbosity=1).run(suite); "
            "sys.exit(0 if result.wasSuccessful() else 1)"
        )
        completed = subprocess.run([sys.executable, "-c", runner, "1" if can_symlink else "0"], cwd=staged, env=env, check=False)
        return completed.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("verify", help="verify every original project file")
    prepared = commands.add_parser("prepare", help="make a working copy and unpack script archives")
    prepared.add_argument("--output", required=True, type=Path)
    commands.add_parser("test", help="run bundled project unit tests in isolation")
    args = parser.parse_args()
    try:
        if args.command == "test":
            return test()
        result = verify() if args.command == "verify" else prepare(args.output)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, ValueError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
        print(f"branch error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
