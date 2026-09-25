#!/usr/bin/env python3
"""Verify and stage the bundled gpt-instruct project without touching live state."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
PROJECT = SKILL_ROOT / "branches" / "gpt-instruct"
MANIFEST = SKILL_ROOT / "branches" / "gpt-instruct.manifest.json"
EVIDENCE = SKILL_ROOT / "references" / "gpt-instruct-evidence.json"
WINDOWS_EVAL_PATCH = SKILL_ROOT / "assets" / "gpt-instruct-windows-eval.patch"
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


def prepare(output: Path, windows_eval_compat: bool = False) -> dict[str, object]:
    if windows_eval_compat and os.name != "nt":
        raise ValueError("--windows-eval-compat is only for Windows evaluation")
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
    eval_compat = "not_requested"
    if windows_eval_compat:
        if not WINDOWS_EVAL_PATCH.is_file():
            raise FileNotFoundError(WINDOWS_EVAL_PATCH)
        # Git checkouts with core.autocrlf=true can turn .patch files into CRLF;
        # git apply requires LF against the byte-preserved archive sources.
        normalized_patch = output / ".gpt-instruct-eval-compat.patch"
        normalized_patch.write_bytes(WINDOWS_EVAL_PATCH.read_bytes().replace(b"\r\n", b"\n"))
        try:
            applied = subprocess.run(
                ["git", "-c", "core.autocrlf=false", "apply", str(normalized_patch)],
                cwd=output, capture_output=True, text=True, encoding="utf-8", check=False,
            )
        finally:
            normalized_patch.unlink()
        if applied.returncode:
            raise ValueError(f"Windows evaluator compatibility patch failed: {applied.stderr}")
        eval_compat = "applied; runner/scorer method identity differs from upstream"
    return {**verified, "output": str(output), "unpacked_scripts": len(members),
            "restored_prompt_sources": restored,
            "compatibility_patch": "preserve prompt bytes on Windows",
            "windows_eval_compat": eval_compat}


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


def evidence() -> dict[str, object]:
    """Audit published gates and model-free runner readiness; never infer a new score."""
    verified = verify()
    data = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    if data.get("version") != 1 or data.get("source_commit") != verified["commit"]:
        raise ValueError("evaluation evidence does not match the bundled source")
    source = (PROJECT / data["source_document"]).read_text(encoding="utf-8")
    if not all(marker in source for marker in ("52/66", "60/74", "15/16", "C remains unrun")):
        raise ValueError("published evaluation summary changed")
    published = data["published"]
    families = published["B"]["families"]
    for metric in ("cases", "turns"):
        totals = [sum(row[metric][index] for row in families.values()) for index in (0, 1)]
        if totals != published["B"][metric]:
            raise ValueError(f"published B {metric} totals do not match families")
    artifacts = [sum(row.get("artifacts", [0, 0])[index] for row in families.values())
                 for index in (0, 1)]
    if artifacts != published["B"]["artifacts"]:
        raise ValueError("published B artifact totals do not match families")
    if published["C"]["status"] != "not_run":
        raise ValueError("C status cannot be inferred from the B results")
    with tempfile.TemporaryDirectory(prefix="gpt-instruct-evidence-") as temporary:
        staged = Path(temporary) / "project"
        prepare(staged)
        for script in ("generate_gpt56_sol_issue_regression_bank.py",
                       "generate_gpt56_sol_prompt_bank.py"):
            completed = subprocess.run([sys.executable, str(staged / "scripts" / script)],
                                       cwd=staged, capture_output=True, text=True,
                                       encoding="utf-8", check=False)
            if completed.returncode:
                raise ValueError(f"bank generation failed: {script}: {completed.stderr}")
        issue = staged / "tests" / "gpt56_sol_issue_regression_bank.jsonl"
        prompt = staged / "tests" / "gpt56_sol_prompt_bank.jsonl"
        counts = {"issue_bank": len(issue.read_text(encoding="utf-8").splitlines()),
                  "prompt_bank": len(prompt.read_text(encoding="utf-8").splitlines())}
        if counts != {"issue_bank": 66, "prompt_bank": 360}:
            raise ValueError("generated bank sizes changed")
        source_context_required = [
            row["case_id"]
            for row in (json.loads(line) for line in issue.read_text(encoding="utf-8").splitlines())
            if not row.get("workspace_fixture") and not row.get("initial_transcript")
            and any(
                turn.get("forbid_fixture")
                and re.search(r"\b(?:repository|source tree)\b", turn.get("user", ""), re.I)
                and re.search(r"\b(?:patch|diff)\b", turn.get("user", ""), re.I)
                for turn in row["turns"]
            )
        ]
        instruction_file = staged / "gpt-6-astra-v1.md"
        dry_runs = {}
        for script in ("run_gpt56_sol_issue_regression.py", "run_gpt56_sol_prompt_bank.py"):
            completed = subprocess.run([
                sys.executable, str(staged / "scripts" / script), "--dry-run",
                "--model", data["method"]["model"], "--reasoning", data["method"]["reasoning"],
                "--instructions-file", str(instruction_file)], cwd=staged,
                capture_output=True, text=True, encoding="utf-8", check=False)
            dry_runs[script] = completed.returncode
            if completed.returncode:
                raise ValueError(f"runner dry-run failed: {script}: {completed.stderr}")
        bank_hashes = {"issue_bank": sha256(issue), "prompt_bank": sha256(prompt)}
    deficits = [{"family": name, "missing_cases": row["cases"][1] - row["cases"][0]}
                for name, row in families.items() if row["cases"][0] < row["cases"][1]]
    deficits.sort(key=lambda row: (-row["missing_cases"], row["family"]))
    return {"status": "published_B_gate_not_met", "source_commit": verified["commit"],
            "published": published, "B_case_deficits": deficits,
            "offline_checks": {"generated_rows": counts, "bank_sha256": bank_hashes,
                               "source_context_required": source_context_required,
                               "runner_dry_run_exit": dry_runs},
            "new_model_evaluation": "not_run", "model_invocations": 0}


def installer_action(action: str, codex_dir: Path, version: str = "",
                     confirm_live_config: bool = False) -> dict[str, object]:
    target = codex_dir.expanduser().resolve()
    if not codex_dir.is_dir() or codex_dir.is_symlink() or target == SKILL_ROOT or SKILL_ROOT in target.parents:
        raise ValueError("--codex-dir must be an existing real directory outside the skill")
    if action in ("deploy", "reset") and not confirm_live_config:
        raise ValueError("live config change requires --confirm-live-config")
    config = target / "config.toml"
    if config.is_symlink():
        raise ValueError("config.toml must not be a symlink for bridge operations")
    before = sha256(config) if config.is_file() else None
    with tempfile.TemporaryDirectory(prefix="gpt-instruct-operation-") as temporary:
        staged = Path(temporary) / "project"
        prepare(staged)
        command = [sys.executable, str(staged / "codex-instruct.py")]
        command += (["--reset"] if action == "reset" else ["--apply", "--version", version])
        command += ["--codex-dir", str(target)]
        if action == "preview":
            command.append("--dry-run")
        env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
        completed = subprocess.run(command, cwd=staged, env=env, capture_output=True,
                                   text=True, encoding="utf-8", check=False,
                                   input="yes\n" if action == "reset" else None)
    after = sha256(config) if config.is_file() else None
    if action == "preview" and before != after:
        raise ValueError("preview unexpectedly changed config.toml")
    return {"status": "ok" if completed.returncode == 0 else "error",
            "action": action, "codex_dir": str(target), "version": version,
            "installer_exit_status": completed.returncode,
            "installer_stdout": completed.stdout, "installer_stderr": completed.stderr,
            "config_sha256_before": before, "config_sha256_after": after}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("verify", help="verify every original project file")
    prepared = commands.add_parser("prepare", help="make a working copy and unpack script archives")
    prepared.add_argument("--output", required=True, type=Path)
    prepared.add_argument("--windows-eval-compat", action="store_true",
                          help="opt in to Windows native-sandbox evaluator compatibility in the disposable copy")
    commands.add_parser("test", help="run bundled project unit tests in isolation")
    commands.add_parser("evidence", help="audit published A/B/C gates and model-free runner readiness")
    preview = commands.add_parser("preview", help="dry-run instruction deployment without writing config")
    preview.add_argument("--version", choices=("gpt-5.6-v45", "gpt-6-v1"), required=True)
    preview.add_argument("--codex-dir", type=Path, required=True)
    deploy = commands.add_parser("deploy", help="explicitly deploy instructions to an existing Codex directory")
    deploy.add_argument("--version", choices=("gpt-5.6-v45", "gpt-6-v1"), required=True)
    deploy.add_argument("--codex-dir", type=Path, required=True)
    deploy.add_argument("--confirm-live-config", action="store_true")
    reset = commands.add_parser("reset", help="explicitly remove only the managed instruction setting")
    reset.add_argument("--codex-dir", type=Path, required=True)
    reset.add_argument("--confirm-live-config", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "test":
            return test()
        if args.command == "verify":
            result = verify()
        elif args.command == "prepare":
            result = prepare(args.output, args.windows_eval_compat)
        elif args.command == "evidence":
            result = evidence()
        else:
            result = installer_action(args.command, args.codex_dir,
                                      getattr(args, "version", ""),
                                      getattr(args, "confirm_live_config", False))
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return result.get("installer_exit_status", 0)
    except (OSError, ValueError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
        print(f"branch error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
