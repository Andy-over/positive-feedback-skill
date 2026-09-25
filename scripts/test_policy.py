import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from policy import (
    ACTIONS, FEATURE_SCHEMA_VERSION, INPUT_SIZE, MAX_CONTEXT_PREFERENCES,
    MAX_CONTEXT_TEXT, PARAMETER_COUNT,
    add_feedback, add_preference, apply_step, attraction_gradient,
    complete, default_state_path, ensure_external_path, flatten, forward, fresh_state, initial, learn,
    link_target, pair_gradient, pre_generation_context, prepare, report,
    refresh_context_cache, register_identity, save, set_preference_status,
    session_command, state_lock, task_features, validate_fact_correction_evidence, verify,
    load,
    replay, read_context_snapshot, snapshot_path_for,
    run_batch,
)


def each_parameter(params):
    for key in params:
        for i in range(len(params[key])):
            if isinstance(params[key][i], list):
                for j in range(len(params[key][i])):
                    yield key, i, j
            else:
                yield key, i, None


def value_at(params, key, i, j):
    return params[key][i] if j is None else params[key][i][j]


def perturb(params, key, i, j, amount):
    result = copy.deepcopy(params)
    if j is None:
        result[key][i] += amount
    else:
        result[key][i][j] += amount
    return result


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.task = {
            "task_kind": "writing",
            "flags": {"artifact_present": True, "evidence_required": True,
                      "revision_requested": True, "output_required": True},
        }
        self.x = task_features(self.task)

    def test_session_activation_is_read_only_until_explicit_change(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "session.json"
            thread = "thread-1"
            self.assertEqual(session_command("status", thread, path)["status"], "inactive")
            self.assertFalse(path.exists())
            self.assertFalse(path.parent.joinpath("session.json.lock").exists())
            enabled = session_command("enable", thread, path, "请使用 positive-feedback skill")
            self.assertTrue(enabled["active"])
            self.assertEqual(session_command("status", thread, path)["status"], "active")
            self.assertEqual(session_command("enable", thread, path, "请使用 positive-feedback skill")["status"],
                             "duplicate")
            self.assertEqual(session_command("disable", thread, path, "停用 positive-feedback skill")["status"],
                             "disabled")
            self.assertFalse(session_command("status", thread, path)["active"])

    def test_session_activation_is_isolated_by_thread_and_survives_process(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            first = root / "first.json"
            second = root / "second.json"
            script = Path(__file__).with_name("policy.py")
            def run(thread, path, *command):
                return subprocess.run(
                    [sys.executable, "-X", "utf8", str(script), "--thread-id", thread,
                     "--session-state", str(path), "session", *command],
                    capture_output=True, text=True, check=True)
            run("thread-first", first, "enable", "--source-excerpt", "请使用该 skill")
            self.assertTrue(json.loads(run("thread-first", first, "status").stdout)["active"])
            self.assertEqual(json.loads(run("thread-second", second, "status").stdout)["status"],
                             "inactive")
            self.assertFalse(second.exists())
            with self.assertRaises(ValueError):
                session_command("status", "thread-second", first)
            run("thread-first", first, "disable", "--source-excerpt", "停止使用该 skill")
            self.assertFalse(json.loads(run("thread-first", first, "status").stdout)["active"])

    def test_session_activation_rejects_missing_identity_and_source(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "session.json"
            self.assertEqual(session_command("status", "", path)["status"], "unavailable")
            with self.assertRaises(ValueError):
                session_command("enable", "", path, "use skill")
            with self.assertRaises(ValueError):
                session_command("enable", "thread-1", path, "")
            with self.assertRaises(ValueError):
                session_command("status", "../thread-1", path)
            self.assertFalse(path.exists())

    def test_session_context_skips_inactive_state_and_matches_active_context(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            session_path = directory / "session.json"
            state_path = directory / "controller.json"
            script = Path(__file__).with_name("policy.py")
            command = [sys.executable, "-X", "utf8", str(script), "--thread-id", "test-thread",
                       "--session-state", str(session_path), "--state", str(state_path)]
            def call(*suffix):
                result = subprocess.run(command + list(suffix), capture_output=True,
                                        text=True, check=True)
                return json.loads(result.stdout)
            inactive = call("session-context", "--task-kind", "general", "--compact")
            self.assertFalse(inactive["active"])
            self.assertFalse(state_path.exists())
            self.assertFalse(session_path.exists())
            session_command("enable", "test-thread", session_path, "请使用这个 skill")
            active = call("session-context", "--task-kind", "general", "--compact")
            ordinary = call("context", "--task-kind", "general", "--compact")
            self.assertTrue(active["active"])
            self.assertEqual(active["instruction_text"], ordinary["instruction_text"])
            self.assertEqual(active["preferred_actions"], ordinary["preferred_actions"])
            self.assertFalse(state_path.exists())

    def test_model_output_keeps_guidance_once_and_compact_status(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            script = Path(__file__).with_name("policy.py")
            session_path = directory / "session.json"
            state_path = directory / "controller.json"
            session_command("enable", "model-thread", session_path, "使用 skill")
            base = [sys.executable, "-X", "utf8", str(script), "--thread-id", "model-thread",
                    "--session-state", str(session_path), "--state", str(state_path)]
            subprocess.run(base + ["preference", "add", "--preference-id", "chinese-guidance",
                                   "--text", "核查来源", "--source-id", "user-preference"],
                           capture_output=True, text=True, check=True)
            utf8_json = subprocess.run(base + ["session-context", "--task-kind", "general",
                                               "--compact", "--factual-correction"],
                                       capture_output=True, text=True, check=True).stdout
            ordinary = json.loads(utf8_json)
            self.assertIn("核查来源", utf8_json)
            model = subprocess.run(base + ["session-context", "--task-kind", "general",
                                           "--for-model", "--factual-correction"],
                                   capture_output=True, text=True, check=True).stdout
            self.assertIn("guidance: " + ordinary["instruction_text"], model)
            self.assertEqual(model.count(ordinary["instruction_text"]), 1)
            self.assertIn("核查来源", model)
            self.assertNotIn("\\u6838", model)
            self.assertLess(len(model), len(json.dumps(ordinary, ensure_ascii=False)))
            status = subprocess.run(base[:8] + ["session", "status", "--compact"],
                                    capture_output=True, text=True, check=True).stdout
            status_result = json.loads(status)
            self.assertEqual(status_result["status"], "active")
            self.assertTrue(status_result["active"])
            self.assertEqual(len(status_result["skill_sha256"]), 64)

    def test_batch_applies_verified_steps_atomically(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            state = fresh_state()
            prepare(state, "batched", "writing", "edit_content,deliver_artifact",
                    "revise content", "source-batched", output_required=True)
            artifact, evidence, checks = self._files(directory, "batched")
            plan = directory / "plan.json"
            steps = [
                {"op": "complete", "task_id": "batched", "output_id": "output-batched",
                 "actions": "edit_content,deliver_artifact", "artifacts": [str(artifact)],
                 "evidence_json": str(evidence)},
                {"op": "verify", "task_id": "batched", "passed": True,
                 "checks_json": str(checks)},
                {"op": "feedback", "event_id": "batched-positive", "kind": "positive",
                 "task_id": "batched", "source_id": "feedback-batched", "label": "很好",
                 "quote": "很好"},
                {"op": "learn", "update_id": "batched-update"},
            ]
            plan.write_text(json.dumps({"batch_id": "batch-one", "steps": steps},
                                       ensure_ascii=False), encoding="utf-8")
            working = copy.deepcopy(state)
            result = run_batch(working, plan)
            self.assertEqual(result["step_count"], 4)
            self.assertEqual(working["tasks"][0]["status"], "verified")
            self.assertEqual(working["events"][0]["status"], "learned")
            self.assertEqual(run_batch(working, plan)["status"], "duplicate")
            self.assertEqual(len(working["updates"]), 1)
            bad = copy.deepcopy(state)
            steps[1]["checks_json"] = str(directory / "missing-checks.json")
            plan.write_text(json.dumps({"batch_id": "batch-one", "steps": steps},
                                       ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(OSError):
                run_batch(bad, plan)
            self.assertEqual(bad["tasks"][0]["status"], "awaiting_execution")

    def test_batch_prepares_factual_correction_atomically(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            evidence = directory / "evidence.json"
            checks = directory / "checks.json"
            plan = directory / "plan.json"
            evidence.write_text(json.dumps({
                "revised_answer": "17×19=323",
                "claims": [{"disputed": "17×19=306", "finding": "contradicted",
                            "sources": ["17×(20−1)=323"],
                            "corrected_text": "17×19=323"}]}, ensure_ascii=False), encoding="utf-8")
            checks.write_text(json.dumps({"source_checks_passed": True,
                                          "claims_reviewed": 1,
                                          "checked_sources": ["17×(20−1)=323"]},
                                         ensure_ascii=False), encoding="utf-8")
            steps = [
                {"op": "prepare", "task_id": "math", "task_kind": "general",
                 "required_actions": "recalculate", "target_spec": "核实17×19",
                 "target_source_id": "user-math", "source_message_id": "user-math",
                 "factual_correction": True},
                {"op": "complete", "task_id": "math", "output_id": "answer-323",
                 "actions": "recalculate,inspect_target,edit_content,add_evidence,validate_requirements",
                 "evidence_json": str(evidence)},
                {"op": "verify", "task_id": "math", "passed": True,
                 "checks_json": str(checks)},
                {"op": "feedback", "event_id": "request-math", "kind": "request",
                 "task_id": "math", "source_id": "request-math-source",
                 "source_message_id": "user-math", "label": "请", "quote": "请核实17×19",
                 "target_spec": "核实17×19", "target_source_id": "user-math",
                 "target_actions": "recalculate,validate_requirements"},
                {"op": "learn", "update_id": "math-update"},
            ]
            plan.write_text(json.dumps({"batch_id": "math-batch", "steps": steps},
                                       ensure_ascii=False), encoding="utf-8")
            state = fresh_state()
            result = run_batch(state, plan)
            self.assertEqual(result["step_count"], 5)
            self.assertEqual(state["tasks"][0]["status"], "verified")
            self.assertEqual(state["events"][0]["status"], "learned")
            self.assertEqual(run_batch(state, plan)["status"], "duplicate")
            bad = fresh_state()
            steps[2]["checks_json"] = str(directory / "absent.json")
            plan.write_text(json.dumps({"batch_id": "math-batch", "steps": steps},
                                       ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(OSError):
                run_batch(bad, plan)
            self.assertEqual(bad["tasks"], [])
            self.assertEqual(bad["events"], [])

    def test_batch_compact_cli_omits_full_task_payload(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            plan = directory / "plan.json"
            plan.write_text(json.dumps({"batch_id": "compact-one", "steps": [{
                "op": "prepare", "task_id": "t", "task_kind": "general",
                "required_actions": "inspect_target", "target_spec": "inspect",
                "target_source_id": "source"}]}), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "-X", "utf8", str(Path(__file__).with_name("policy.py")),
                 "--state", str(directory / "state.json"), "batch", "--plan-json",
                 str(plan), "--compact"], capture_output=True, text=True, check=True)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "applied")
            self.assertEqual(payload["steps"], [{"op": "prepare", "status": "awaiting_execution"}])
            self.assertNotIn("action_scores", result.stdout)

    def assert_gradient(self, loss_function):
        params = initial()
        loss, gradient = loss_function(params)
        self.assertTrue(loss >= 0)
        self.assertEqual(len(flatten(params)), PARAMETER_COUNT)
        epsilon = 1e-5
        for key, i, j in each_parameter(params):
            plus = perturb(params, key, i, j, epsilon)
            minus = perturb(params, key, i, j, -epsilon)
            numerical = (loss_function(plus)[0] - loss_function(minus)[0]) / (2*epsilon)
            self.assertAlmostEqual(value_at(gradient, key, i, j), numerical, places=7)

    def test_all_controller_attraction_gradients(self):
        self.assert_gradient(lambda p: attraction_gradient(
            p, self.x, ["edit_content", "deliver_artifact"], .8))

    def test_all_controller_pairwise_gradients(self):
        self.assert_gradient(lambda p: pair_gradient(
            p, self.x, ["restructure"], ["edit_content", "validate_requirements"], .7))

    def test_zero_is_not_punishment_and_small_positive_updates(self):
        params = initial()
        zero, metrics = apply_step(params, attraction_gradient(params, self.x, ["edit_content"], 0))
        self.assertEqual(params, zero)
        self.assertEqual(metrics["parameter_delta_l2"], 0)
        small, metrics = apply_step(params, attraction_gradient(params, self.x, ["edit_content"], .1))
        self.assertNotEqual(params, small)
        self.assertGreater(metrics["parameter_delta_l2"], 0)
        action = ACTIONS.index("edit_content")
        self.assertGreater(forward(small, self.x)[4][action], forward(params, self.x)[4][action])

    def test_semantic_features_separate_same_kind_tasks(self):
        left = {"task_kind": "general", "feature_schema_version": FEATURE_SCHEMA_VERSION,
                "target_spec": "核实引用来源和事实准确性", "required_actions": ["verify_citations"],
                "flags": {}}
        right = {**left, "target_spec": "重新计算预算和公式"}
        x_left, x_right = task_features(left), task_features(right)
        self.assertEqual(len(x_left), INPUT_SIZE)
        self.assertNotEqual(x_left, x_right)
        trained, _ = apply_step(initial(), attraction_gradient(initial(), x_left,
                                                               ["verify_citations"], .8))
        self.assertNotEqual(forward(trained, x_left)[4], forward(trained, x_right)[4])

    def test_legacy_620_parameter_state_migrates_without_changing_old_features(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "legacy.json"
            state = fresh_state()
            state.pop("feature_schema_version")
            state["params"]["w1"] = [row[:8] for row in state["params"]["w1"]]
            path.write_text(json.dumps(state), encoding="utf-8")
            restored = load(path)
            self.assertEqual(len(restored["params"]["w1"][0]), INPUT_SIZE)
            self.assertEqual(restored["params"], initial())
            old_task = {"task_kind": "general", "flags": {},
                        "target_spec": "核实引用来源和事实准确性"}
            self.assertEqual(task_features(old_task)[8:], [0.0]*8)

    def _files(self, directory, stem):
        artifact = directory / f"{stem}.txt"
        evidence = directory / f"{stem}-evidence.json"
        checks = directory / f"{stem}-checks.json"
        artifact.write_text("verified synthetic result", encoding="utf-8")
        evidence.write_text(json.dumps({"operation": "edited", "target": stem}), encoding="utf-8")
        checks.write_text(json.dumps({"requirements": True, "artifact_opened": True}), encoding="utf-8")
        return artifact, evidence, checks

    def _verified_task(self, state, directory, task_id, actions, task_kind="writing"):
        prepare(state, task_id, task_kind, actions, "synthetic target", f"source-{task_id}",
                artifact_present=True, evidence_required=True, revision_requested=True,
                output_required=True)
        artifact, evidence, checks = self._files(directory, task_id)
        complete(state, task_id, f"output-{task_id}", actions, [artifact], evidence)
        verify(state, task_id, True, checks)

    def test_request_is_target_attraction_not_rejection(self):
        with tempfile.TemporaryDirectory() as raw:
            state, directory = fresh_state(), Path(raw)
            prepare(state, "request-task", "writing", "edit_content,deliver_artifact",
                    "revise the paper", "message-2", revision_requested=True, output_required=True)
            event = add_feedback(
                state, "request-1", "request", "request-task", "message-2",
                label="需要你", target_spec="revise the paper", target_source_id="message-2",
                target_actions="edit_content,deliver_artifact")
            self.assertEqual(event["status"], "awaiting_execution")
            with self.assertRaises(ValueError):
                add_feedback(state, "bad-request", "request", "request-task", "message-3",
                             label="要求你", failed_actions="restructure",
                             target_spec="edit", target_source_id="message-3",
                             target_actions="edit_content")
            artifact, evidence, checks = self._files(directory, "request")
            complete(state, "request-task", "output-request", "edit_content,deliver_artifact",
                     [artifact], evidence)
            self.assertEqual(learn(state, "early")["status"], "no_ready_events")
            verify(state, "request-task", True, checks)
            before = forward(state["params"], task_features(state["tasks"][0]))[4]
            result = learn(state, "request-update")
            after = forward(state["params"], task_features(state["tasks"][0]))[4]
            self.assertEqual(result["new_events"], ["request-1"])
            self.assertGreater(after[ACTIONS.index("edit_content")], before[ACTIONS.index("edit_content")])

    def test_evaluation_waits_for_verified_correction_then_improves_margin(self):
        with tempfile.TemporaryDirectory() as raw:
            state, directory = fresh_state(), Path(raw)
            self._verified_task(state, directory, "old-task", "restructure,deliver_artifact")
            event = add_feedback(state, "eval-1", "evaluation", "old-task", "message-eval",
                                 label="很差", aspect="content", failed_actions="restructure")
            self.assertEqual(event["status"], "awaiting_target")
            self.assertEqual(learn(state, "no-target")["status"], "no_ready_events")
            self._verified_task(state, directory, "fix-task", "edit_content,validate_requirements,deliver_artifact")
            linked = link_target(state, "eval-1", "fix-task", "make concrete edits",
                                 "source-fix-task", "edit_content,validate_requirements")
            self.assertEqual(linked["status"], "ready")
            target_task = next(t for t in state["tasks"] if t["task_id"] == "fix-task")
            logits_before = forward(state["params"], task_features(target_task))[3]
            result = learn(state, "eval-update")
            logits_after = forward(state["params"], task_features(target_task))[3]
            failed = ACTIONS.index("restructure")
            target = ACTIONS.index("edit_content")
            self.assertEqual(result["new_events"], ["eval-1"])
            self.assertGreater(logits_after[target] - logits_after[failed],
                               logits_before[target] - logits_before[failed])

    def test_quality_evaluation_reinforces_same_action_and_surfaces_requirement(self):
        with tempfile.TemporaryDirectory() as raw:
            state, directory = fresh_state(), Path(raw)
            self._verified_task(state, directory, "old-quality", "verify_citations,deliver_artifact")
            event = add_feedback(
                state, "quality", "evaluation", "old-quality", "quality-feedback",
                label="不满意", failed_actions="verify_citations", evaluation_mode="quality",
                quality_requirement="逐条核对引用与原文是否一致")
            self.assertEqual(event["status"], "awaiting_target")
            self._verified_task(state, directory, "fixed-quality", "verify_citations,deliver_artifact")
            linked = link_target(
                state, "quality", "fixed-quality", "verify citations more carefully",
                "source-fixed-quality", "verify_citations")
            self.assertEqual(linked["status"], "ready")
            learn(state, "quality-update")
            context = pre_generation_context(
                state, "writing", artifact_present=True, evidence_required=True,
                revision_requested=True, output_required=True)
            self.assertIn("verify_citations", context["preferred_actions"])
            self.assertEqual(context["quality_requirements"][0]["text"], "逐条核对引用与原文是否一致")

    def test_factual_correction_requires_content_evidence_and_source_review(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            state = fresh_state()
            prepared = prepare(state, "fact", "general", "edit_content",
                               "纠正事实错误", "message", factual_correction=True)
            self.assertIn("add_evidence", prepared["required_actions"])
            context = pre_generation_context(state, "general", "edit_content",
                                             factual_correction=True, compact=True)
            self.assertEqual(context["content_checks"], [
                "trace_disputed_claims", "check_sources", "correct_or_qualify_claims"])
            self.assertIn("traceable sources", context["instruction_text"])
            weak = directory / "weak.json"
            weak.write_text(json.dumps({"claims": [{"disputed": "old fact", "finding": "contradicted",
                                                       "sources": ["source"]}]}), encoding="utf-8")
            with self.assertRaises(ValueError):
                complete(state, "fact", "output", "edit_content,inspect_target,add_evidence,validate_requirements",
                         evidence_json=weak)
            strong = directory / "strong.json"
            strong.write_text(json.dumps({"revised_answer": "new fact", "claims": [
                {"disputed": "old fact", "finding": "contradicted",
                 "sources": ["source"], "corrected_text": "new fact"}]}),
                              encoding="utf-8")
            apology_only = directory / "apology.json"
            apology_only.write_text(json.dumps({"revised_answer": "I am very sorry", "claims": [
                {"disputed": "old fact", "finding": "contradicted",
                 "sources": ["source"], "corrected_text": "new fact"}]}), encoding="utf-8")
            with self.assertRaises(ValueError):
                complete(state, "fact", "output", "edit_content,inspect_target,add_evidence,validate_requirements",
                         evidence_json=apology_only)
            complete(state, "fact", "output", "edit_content,inspect_target,add_evidence,validate_requirements",
                     evidence_json=strong)
            checks = directory / "checks.json"
            checks.write_text(json.dumps({"source_checks_passed": False, "claims_reviewed": 1}), encoding="utf-8")
            with self.assertRaises(ValueError):
                verify(state, "fact", True, checks)
            checks.write_text(json.dumps({"source_checks_passed": True, "claims_reviewed": 1}), encoding="utf-8")
            with self.assertRaises(ValueError):
                verify(state, "fact", True, checks)
            checks.write_text(json.dumps({"source_checks_passed": True, "claims_reviewed": 1,
                                          "checked_sources": ["source"]}), encoding="utf-8")
            verify(state, "fact", True, checks)
            self.assertEqual(state["tasks"][0]["status"], "verified")

    def test_uncertain_fact_cannot_be_omitted_from_revised_answer(self):
        claim = {"disputed": "unsettled claim", "finding": "uncertain",
                 "sources": ["source"], "uncertainty": "evidence remains inconclusive"}
        with self.assertRaises(ValueError):
            validate_fact_correction_evidence({"revised_answer": "This is the answer.",
                                               "claims": [claim]})
        validate_fact_correction_evidence({"revised_answer": "evidence remains inconclusive",
                                           "claims": [claim]})

    def test_selection_evaluation_still_rejects_same_failed_and_target_action(self):
        with tempfile.TemporaryDirectory() as raw:
            state, directory = fresh_state(), Path(raw)
            self._verified_task(state, directory, "old-selection", "verify_citations,deliver_artifact")
            add_feedback(state, "selection", "evaluation", "old-selection", "selection-feedback",
                         label="不满意", failed_actions="verify_citations",
                         evaluation_mode="selection")
            self._verified_task(state, directory, "fixed-selection", "verify_citations,deliver_artifact")
            with self.assertRaises(ValueError):
                link_target(state, "selection", "fixed-selection", "retry",
                            "source-fixed-selection", "verify_citations")

    def test_positive_learns_only_verified_actual_actions(self):
        with tempfile.TemporaryDirectory() as raw:
            state, directory = fresh_state(), Path(raw)
            prepare(state, "p", "code", "validate_requirements,deliver_artifact",
                    "finish tested patch", "message-p", output_required=True)
            with self.assertRaises(ValueError):
                add_feedback(state, "positive-early", "positive", "p", "message-early", label="很好")
            artifact, evidence, checks = self._files(directory, "positive")
            complete(state, "p", "output-p", "validate_requirements,deliver_artifact",
                     [artifact], evidence)
            verify(state, "p", True, checks)
            event = add_feedback(state, "positive-1", "positive", "p", "message-positive", label="很好")
            self.assertEqual(event["status"], "ready")
            self.assertEqual(learn(state, "positive-update")["new_events"], ["positive-1"])
            self.assertEqual(state["events"][0]["status"], "learned")
            self.assertEqual(state["events"][0]["action_evidence"]["output_id"], "output-p")
            self.assertEqual(state["events"][0]["previous_output_id"], "output-p")

    def test_completion_requires_evidence_and_failed_verification_allows_retry(self):
        with tempfile.TemporaryDirectory() as raw:
            state, directory = fresh_state(), Path(raw)
            prepare(state, "retry", "data", "recalculate,deliver_artifact",
                    "correct table", "message-retry", evidence_required=True, output_required=True)
            with self.assertRaises(ValueError):
                complete(state, "retry", "out", "recalculate,deliver_artifact")
            artifact, evidence, checks = self._files(directory, "retry")
            complete(state, "retry", "out-1", "recalculate,deliver_artifact", [artifact], evidence)
            verify(state, "retry", False, checks)
            self.assertEqual(state["tasks"][0]["status"], "awaiting_execution")
            complete(state, "retry", "out-2", "recalculate,deliver_artifact", [artifact], evidence)
            verify(state, "retry", True, checks)
            self.assertEqual(state["tasks"][0]["status"], "verified")
            self.assertEqual(len(state["tasks"][0]["attempts"]), 2)

    def test_verification_rejects_artifact_changed_after_completion(self):
        with tempfile.TemporaryDirectory() as raw:
            state, directory = fresh_state(), Path(raw)
            prepare(state, "integrity", "code", "deliver_artifact", "deliver", "source",
                    output_required=True)
            artifact, evidence, checks = self._files(directory, "integrity")
            complete(state, "integrity", "out", "deliver_artifact", [artifact], evidence)
            artifact.write_text("changed after completion", encoding="utf-8")
            with self.assertRaises(ValueError):
                verify(state, "integrity", True, checks)

    def test_no_ready_learn_does_not_consume_update_id(self):
        state = fresh_state()
        with patch("policy.replay", side_effect=AssertionError("empty learn must not replay")):
            result = learn(state, "reusable")
        self.assertEqual(result["status"], "no_ready_events")
        self.assertEqual(state["updates"], [])
        self.assertEqual(learn(state, "reusable")["status"], "no_ready_events")

    def test_no_ready_cli_does_not_rewrite_unchanged_state(self):
        with tempfile.TemporaryDirectory() as raw:
            state_path = Path(raw) / "controller.json"
            save(state_path, fresh_state())
            before = state_path.stat().st_mtime_ns
            script = Path(__file__).with_name("policy.py")
            result = subprocess.run(
                [sys.executable, "-X", "utf8", str(script), "--state", str(state_path),
                 "learn", "--update-id", "empty"],
                capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(result.stdout)["status"], "no_ready_events")
            self.assertEqual(state_path.stat().st_mtime_ns, before)

    def test_learned_event_is_not_registered_again_under_new_update_id(self):
        with tempfile.TemporaryDirectory() as raw:
            state, directory = fresh_state(), Path(raw)
            self._verified_task(state, directory, "p", "edit_content,deliver_artifact")
            add_feedback(state, "positive", "positive", "p", "feedback", label="很好")
            learn(state, "first")
            before = copy.deepcopy(state["params"])
            with patch("policy.replay", side_effect=AssertionError("no new events must not replay")):
                result = learn(state, "second")
            self.assertEqual(result["status"], "no_ready_events")
            self.assertEqual(state["params"], before)
            self.assertEqual(len(state["updates"]), 1)
            self.assertEqual(state["events"][0]["update_id"], "first")

    def test_no_ready_learn_rebuilds_when_training_state_is_stale(self):
        with tempfile.TemporaryDirectory() as raw:
            state, directory = fresh_state(), Path(raw)
            self._verified_task(state, directory, "p", "edit_content,deliver_artifact")
            add_feedback(state, "positive", "positive", "p", "feedback", label="很好")
            learn(state, "first")
            expected = copy.deepcopy(state["params"])
            state["params"]["b3"][0] += 0.5
            result = learn(state, "repair")
            self.assertEqual(result["status"], "no_ready_events")
            self.assertGreater(result["parameter_delta_l2"], 0)
            self.assertEqual(state["params"], expected)
            self.assertEqual(len(state["updates"]), 1)

    def test_append_only_learning_matches_full_replay_and_repairs_tampered_trace(self):
        with tempfile.TemporaryDirectory() as raw:
            state, directory = fresh_state(), Path(raw)
            self._verified_task(state, directory, "first", "edit_content,deliver_artifact")
            add_feedback(state, "first-event", "positive", "first", "first-feedback", label="很好")
            self.assertTrue(learn(state, "first-update")["incremental"])
            self._verified_task(state, directory, "second", "validate_requirements,deliver_artifact")
            add_feedback(state, "second-event", "positive", "second", "second-feedback", label="很好")
            with patch("policy.replay", side_effect=AssertionError("append-only path replayed")):
                result = learn(state, "second-update")
            self.assertTrue(result["incremental"])
            expected_params, expected_trace = replay(state)
            self.assertEqual(state["params"], expected_params)
            self.assertEqual(state["training_trace"], expected_trace)
            state["training_trace"][0]["score"] = 0.1
            self._verified_task(state, directory, "third", "inspect_target,deliver_artifact")
            add_feedback(state, "third-event", "positive", "third", "third-feedback", label="很好")
            self.assertFalse(learn(state, "third-update")["incremental"])
            self.assertEqual(state["training_trace"], replay(state)[1])

    def test_snapshot_matches_full_context_and_falls_back_when_stale_or_damaged(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "controller.json"
            state = fresh_state()
            self._verified_task(state, Path(raw), "first", "edit_content,deliver_artifact")
            add_feedback(state, "first-event", "positive", "first", "first-feedback", label="很好")
            learn(state, "first-update")
            save(path, state)
            snapshot = read_context_snapshot(path, "writing")
            self.assertIsNotNone(snapshot)
            from_snapshot = pre_generation_context({"preferences": []}, "writing", snapshot_entry=snapshot)
            full = pre_generation_context(state, "writing")
            for key in ("preferred_actions", "quality_requirements", "action_scores", "instruction_text"):
                self.assertEqual(from_snapshot[key], full[key])
            path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")
            self.assertIsNone(read_context_snapshot(path, "writing"))
            save(path, state)
            snapshot_path_for(path).write_text("{damaged", encoding="utf-8")
            self.assertIsNone(read_context_snapshot(path, "writing"))

    def test_replacing_learned_event_removes_stale_context_until_relearned(self):
        with tempfile.TemporaryDirectory() as raw:
            state, directory = fresh_state(), Path(raw)
            self._verified_task(state, directory, "p", "edit_content,deliver_artifact")
            add_feedback(state, "positive", "positive", "p", "feedback", label="很好")
            learn(state, "first")
            before = pre_generation_context(
                state, "writing", artifact_present=True, evidence_required=True,
                revision_requested=True, output_required=True)
            self.assertIn("edit_content", before["preferred_actions"])
            add_feedback(state, "positive", "positive", "p", "feedback",
                         score=0, replace=True)
            after = pre_generation_context(
                state, "writing", artifact_present=True, evidence_required=True,
                revision_requested=True, output_required=True)
            self.assertEqual(after["preferred_actions"], [])
            self.assertEqual(state["params"], initial())

    def test_learning_and_replacing_only_refresh_affected_task_kind(self):
        with tempfile.TemporaryDirectory() as raw:
            state, directory = fresh_state(), Path(raw)
            self._verified_task(state, directory, "write", "edit_content,deliver_artifact", "writing")
            self._verified_task(state, directory, "code", "edit_content,deliver_artifact", "code")
            add_feedback(state, "write-event", "positive", "write", "write-feedback",
                         label="很好", quote="很好")
            learn(state, "write-update")
            writing_cache = copy.deepcopy(state["context_cache"]["writing"])
            add_feedback(state, "code-event", "positive", "code", "code-feedback",
                         label="很好", quote="很好")
            learn(state, "code-update")
            self.assertEqual(state["context_cache"]["writing"], writing_cache)
            code_cache = copy.deepcopy(state["context_cache"]["code"])
            add_feedback(state, "write-event", "positive", "write", "write-feedback",
                         score=0, quote="改为零分", replace=True)
            self.assertEqual(state["context_cache"]["code"], code_cache)
            self.assertEqual(pre_generation_context(state, "writing")["preferred_actions"], [])
            self.assertIn("edit_content", pre_generation_context(state, "code")["preferred_actions"])

    def test_pre_generation_context_uses_only_learned_score_deltas(self):
        with tempfile.TemporaryDirectory() as raw:
            state, directory = fresh_state(), Path(raw)
            empty = pre_generation_context(state, "writing", "inspect_target")
            self.assertEqual(empty["preferred_actions"], [])
            self._verified_task(state, directory, "p", "edit_content,deliver_artifact")
            add_feedback(state, "positive", "positive", "p", "feedback", label="很好")
            learn(state, "update")
            context = pre_generation_context(
                state, "writing", "inspect_target", artifact_present=True,
                evidence_required=True, revision_requested=True, output_required=True)
            self.assertIn("edit_content", context["preferred_actions"])
            self.assertIn("inspect_target", context["required_actions"])
            self.assertIn("Current request requires", context["instruction_text"])
            self.assertNotIn("inspect_target", context["preferred_actions"])

    def test_context_variants_remove_stale_sentence_and_preserve_guidance(self):
        with tempfile.TemporaryDirectory() as raw:
            state = fresh_state()
            empty = pre_generation_context(state, "writing", "inspect_target")
            self.assertNotIn("base-model weights", empty["instruction_text"])
            self.assertIn("Current request requires", empty["instruction_text"])
            add_preference(state, "p", "先给结论", "preference-source", task_kinds="writing")
            scoped = pre_generation_context(state, "writing", compact=True)
            self.assertEqual(scoped["explicit_preferences"][0]["text"], "先给结论")
            self.assertNotIn("action_scores", scoped)
            self.assertNotIn("base-model weights", scoped["instruction_text"])
            self._verified_task(state, Path(raw), "done", "edit_content,deliver_artifact")
            add_feedback(state, "event", "positive", "done", "feedback-source",
                         label="很好", quote="做得很好")
            learn(state, "update")
            learned = pre_generation_context(state, "writing")
            self.assertTrue(learned["context_cache_hit"])
            self.assertIn("edit_content", learned["preferred_actions"])
            self.assertNotIn("base-model weights", learned["instruction_text"])

    def test_cached_context_matches_replay_and_correction_invalidates_it(self):
        with tempfile.TemporaryDirectory() as raw:
            state = fresh_state()
            self._verified_task(state, Path(raw), "done", "edit_content,deliver_artifact")
            add_feedback(state, "event", "positive", "done", "feedback-source",
                         label="很好", quote="很好")
            learn(state, "update")
            cached = pre_generation_context(state, "writing")
            uncached_state = copy.deepcopy(state)
            uncached_state["context_cache"] = {}
            uncached = pre_generation_context(uncached_state, "writing")
            self.assertEqual(cached["action_scores"], uncached["action_scores"])
            self.assertEqual(cached["preferred_actions"], uncached["preferred_actions"])
            add_feedback(state, "event", "positive", "done", "feedback-source",
                         score=0, quote="这次只能算零分", replace=True)
            corrected = pre_generation_context(state, "writing")
            self.assertEqual(corrected["preferred_actions"], [])
            self.assertTrue(corrected["context_cache_hit"])
            self.assertEqual(state["events"][0]["revisions"][0]["quote"], "很好")

    def test_quote_is_not_filled_with_label_and_multi_target_source_is_checked(self):
        state = fresh_state()
        prepare(state, "project", "code", "edit_content", "继续优化项目",
                "message-project", source_message_id="message-both")
        prepare(state, "skill", "code", "edit_content", "修改 skill",
                "message-skill", source_message_id="message-both")
        with self.assertRaises(ValueError):
            add_feedback(state, "wrong", "request", "project", "message-project",
                         label="需要你", quote="需要把 skill 修改", source_message_id="other-message",
                         target_spec="继续优化项目", target_source_id="message-project",
                         target_actions="edit_content")
        event = add_feedback(state, "skill-request", "request", "skill", "message-skill",
                             label="需要你", quote="需要把 skill 修改",
                             source_message_id="message-both", target_spec="修改 skill",
                             target_source_id="message-skill", target_actions="edit_content")
        self.assertEqual(event["status"], "awaiting_execution")
        self.assertEqual(state["events"][0]["quote"], "需要把 skill 修改")
        self.assertNotEqual(state["events"][0]["quote"], state["events"][0]["label"])

    def test_conflict_group_prefers_priority_then_latest_revision(self):
        state = fresh_state()
        add_preference(state, "older", "先给过程", "source-older", priority=50,
                       conflict_group="report-order")
        add_preference(state, "newer", "先给结论", "source-newer", priority=60,
                       conflict_group="report-order")
        self.assertEqual(pre_generation_context(state, "writing")["explicit_preferences"][0]["text"], "先给结论")
        set_preference_status(state, "newer", "disabled")
        self.assertEqual(pre_generation_context(state, "writing")["explicit_preferences"][0]["text"], "先给过程")

    def test_report_compatibility_and_identity_on_legacy_state(self):
        state = fresh_state()
        state.pop("identity")
        register_identity(state, "controller-a", "host-executor")
        result = report(state, current_executor="host-executor", current_profile="controller-a")
        self.assertEqual(result["controller_parameter_count"], result["external_parameter_count"])
        self.assertEqual(result["controller_identity"]["created_by"], "host-executor")
        self.assertEqual(result["controller_identity"]["current_profile"], "controller-a")

    def test_context_does_not_generalize_learned_actions_across_task_kinds(self):
        with tempfile.TemporaryDirectory() as raw:
            state, directory = fresh_state(), Path(raw)
            self._verified_task(state, directory, "writing", "edit_content,deliver_artifact", "writing")
            add_feedback(state, "positive", "positive", "writing", "feedback", label="很好")
            learn(state, "update")
            code_context = pre_generation_context(
                state, "code", artifact_present=True, evidence_required=True,
                revision_requested=True, output_required=True)
            self.assertEqual(code_context["learned_event_count"], 0)
            self.assertEqual(code_context["preferred_actions"], [])
            prepared = prepare(
                state, "code-task", "code", "inspect_target", "inspect code", "code-source",
                artifact_present=True, evidence_required=True,
                revision_requested=True, output_required=True)
            baseline = forward(initial(), task_features(next(t for t in state["tasks"] if t["task_id"] == "code-task")))[4]
            self.assertEqual(prepared["action_scores"], {a: baseline[i] for i, a in enumerate(ACTIONS)})

    def test_explicit_preferences_are_scoped_disableable_and_bounded(self):
        state = fresh_state()
        add_preference(state, "writing-pref", "先给结论，再说明依据", "pref-source",
                       task_kinds="writing", priority=90)
        writing = pre_generation_context(state, "writing")
        code = pre_generation_context(state, "code")
        self.assertEqual(writing["explicit_preferences"][0]["text"], "先给结论，再说明依据")
        self.assertEqual(code["explicit_preferences"], [])
        set_preference_status(state, "writing-pref", "disabled")
        self.assertEqual(pre_generation_context(state, "writing")["explicit_preferences"], [])
        for index in range(8):
            add_preference(state, f"p-{index}", (f"要求{index}" * 70), f"source-{index}", priority=index)
        bounded = pre_generation_context(state, "general")["explicit_preferences"]
        self.assertLessEqual(len(bounded), MAX_CONTEXT_PREFERENCES)
        self.assertLessEqual(sum(len(p["text"]) for p in bounded), MAX_CONTEXT_TEXT)

    def test_expired_preference_is_not_applied(self):
        state = fresh_state()
        add_preference(state, "expired", "这条不应出现", "expired-source",
                       expires_at="2000-01-01T00:00:00+00:00")
        self.assertEqual(pre_generation_context(state, "general")["explicit_preferences"], [])

    def test_required_evidence_and_output_flags_are_enforced_separately(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            artifact, evidence, _ = self._files(directory, "flags")
            state = fresh_state()
            prepare(state, "e", "writing", "edit_content", "edit", "source-e",
                    evidence_required=True)
            with self.assertRaises(ValueError):
                complete(state, "e", "out-e", "edit_content", [artifact])
            state = fresh_state()
            prepare(state, "o", "writing", "deliver_artifact", "deliver", "source-o",
                    output_required=True)
            with self.assertRaises(ValueError):
                complete(state, "o", "out-o", "deliver_artifact", evidence_json=evidence)

    def test_evaluation_must_name_an_action_executed_by_verified_source(self):
        with tempfile.TemporaryDirectory() as raw:
            state, directory = fresh_state(), Path(raw)
            prepare(state, "old", "writing", "edit_content", "edit", "source-old")
            with self.assertRaises(ValueError):
                add_feedback(state, "early", "evaluation", "old", "feedback-early",
                             label="不满意", failed_actions="edit_content")
            self._verified_task(state, directory, "verified", "edit_content,deliver_artifact")
            with self.assertRaises(ValueError):
                add_feedback(state, "wrong-action", "evaluation", "verified", "feedback-wrong",
                             label="不满意", failed_actions="recalculate")

    def test_report_recomputes_unlearned_event_status(self):
        with tempfile.TemporaryDirectory() as raw:
            state, directory = fresh_state(), Path(raw)
            self._verified_task(state, directory, "p", "edit_content,deliver_artifact")
            add_feedback(state, "positive", "positive", "p", "feedback", label="很好")
            state["events"][0]["status"] = "awaiting_execution"
            self.assertEqual(report(state)["events"][0]["status"], "ready")

    def _isolated_skill_cli(self, directory):
        skill = directory / "isolated-skill"
        scripts = skill / "scripts"
        scripts.mkdir(parents=True)
        for name in ("policy.py", "feedback.py", "training_mode.py"):
            shutil.copyfile(Path(__file__).with_name(name), scripts / name)
        return skill, scripts / "policy.py"

    def test_default_state_path_is_skill_directory_and_profile_scoped(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "isolated-skill"
            root.mkdir()
            with patch("policy.SKILL_ROOT", root):
                first = default_state_path("model-a")
                second = default_state_path("model-b")
                self.assertEqual(first, root / ".positive-feedback" / "model-a" / "action-controller.json")
                self.assertNotEqual(first, second)
                self.assertFalse(first.exists())
                with self.assertRaises(ValueError):
                    ensure_external_path(root / "SKILL.md")
                for bad in ("", "../escape", "has space"):
                    with self.assertRaises(ValueError):
                        default_state_path(bad)

    def test_cli_uses_profile_scoped_skill_directory_storage(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            artifact, evidence, checks = self._files(directory, "cli")
            skill, script = self._isolated_skill_cli(directory)

            def run(*args):
                result = subprocess.run(
                    [sys.executable, "-X", "utf8", str(script), "--profile", "model-a", *args],
                    cwd=directory, env={**os.environ, "CODEX_THREAD_ID": ""},
                    text=True, capture_output=True, check=True)
                return json.loads(result.stdout)

            run("prepare", "--task-id", "t", "--task-kind", "writing",
                "--required-actions", "edit_content,deliver_artifact",
                "--target-spec", "edit", "--target-source-id", "message",
                "--evidence-required", "--output-required")
            run("complete", "--task-id", "t", "--output-id", "o",
                "--actions", "edit_content,deliver_artifact",
                "--artifact", str(artifact), "--evidence-json", str(evidence))
            run("verify", "--task-id", "t", "--result", "pass",
                "--checks-json", str(checks))
            state_path = skill / ".positive-feedback" / "model-a" / "action-controller.json"
            self.assertTrue(state_path.is_file())
            run("feedback", "--event-id", "positive", "--kind", "positive",
                "--task-id", "t", "--source-id", "feedback", "--label", "很好",
                "--quote", "这次修改很好")
            run("learn", "--update-id", "update")
            run("preference", "add", "--preference-id", "concise",
                "--text", "先给结论", "--source-id", "preference-source",
                "--task-kinds", "writing")
            preferences_path = skill / ".positive-feedback" / "model-a" / "preferences.json"
            self.assertTrue(preferences_path.is_file())
            self.assertNotIn("preferences", json.loads(state_path.read_text(encoding="utf-8")))
            before = state_path.read_bytes()
            context = run("context", "--task-kind", "writing",
                          "--required-actions", "inspect_target",
                          "--evidence-required", "--output-required")
            self.assertEqual(context["mode"], "pre_generation")
            self.assertIn("edit_content", context["preferred_actions"])
            self.assertEqual(context["explicit_preferences"][0]["text"], "先给结论")
            self.assertEqual(before, state_path.read_bytes())
            self.assertTrue((skill / ".positive-feedback" / ".gitignore").is_file())
            self.assertFalse((directory / ".positive-feedback").exists())
            self.assertFalse((skill / ".positive-feedback" / "model-b" / "action-controller.json").exists())

    def test_cli_context_without_state_creates_no_runtime_directory(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            skill, script = self._isolated_skill_cli(directory)
            result = subprocess.run(
                [sys.executable, "-X", "utf8", str(script), "--profile", "model-empty",
                 "context", "--task-kind", "general"],
                cwd=directory, env={**os.environ, "CODEX_THREAD_ID": ""},
                text=True, capture_output=True, check=True)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["preferred_actions"], [])
            self.assertFalse((skill / ".positive-feedback").exists())
            self.assertFalse((directory / ".positive-feedback").exists())

    def test_cli_preference_file_is_independent_from_controller_state(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            skill, script = self._isolated_skill_cli(directory)
            subprocess.run(
                [sys.executable, "-X", "utf8", str(script), "--profile", "model-pref",
                 "preference", "add", "--preference-id", "p", "--text", "先给结论",
                 "--source-id", "message", "--task-kinds", "writing"],
                cwd=directory, env={**os.environ, "CODEX_THREAD_ID": ""},
                text=True, capture_output=True, check=True)
            runtime = skill / ".positive-feedback" / "model-pref"
            self.assertTrue((runtime / "preferences.json").is_file())
            self.assertFalse((runtime / "action-controller.json").exists())
            self.assertFalse((directory / ".positive-feedback").exists())

    def test_state_lock_rejects_concurrent_writer(self):
        with tempfile.TemporaryDirectory() as raw:
            state_path = Path(raw) / "state.json"
            with state_lock(state_path):
                with self.assertRaises(TimeoutError):
                    with state_lock(state_path, timeout=.05):
                        self.fail("second writer unexpectedly acquired the lock")


if __name__ == "__main__":
    unittest.main()
