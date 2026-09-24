import argparse
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from policy import fresh_state, save, sha256
from train_local_model import train, validate_paths, verified_examples


class LocalTrainingTests(unittest.TestCase):
    def test_identity_and_path_guards(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            model = root / "candidate"
            model.mkdir()
            (model / "config.json").write_text("{}", encoding="utf-8")
            (model / "model.safetensors").write_bytes(b"synthetic")
            with self.assertRaises(ValueError):
                validate_paths(model, root / "output", "active", "active")
            with self.assertRaises(ValueError):
                validate_paths(model, root / "output", "active", "candidate", model)
            with self.assertRaises(ValueError):
                validate_paths(model, model / "nested", "active", "candidate")

    def test_verified_example_gate_and_full_parameter_training(self):
        import torch
        from tokenizers import Tokenizer, models, pre_tokenizers
        from transformers import GPT2Config, GPT2LMHeadModel, PreTrainedTokenizerFast

        torch.set_num_threads(1)

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            checkpoint = root / "candidate"
            checkpoint.mkdir()
            vocab = {"<unk>": 0, "<eos>": 1, "<pad>": 2,
                     "Question": 3, "wrong": 4, "correct": 5, "evidence": 6}
            word_tokenizer = Tokenizer(models.WordLevel(vocab, unk_token="<unk>"))
            word_tokenizer.pre_tokenizer = pre_tokenizers.WhitespaceSplit()
            tokenizer = PreTrainedTokenizerFast(tokenizer_object=word_tokenizer,
                                                 unk_token="<unk>", eos_token="<eos>",
                                                 pad_token="<pad>")
            tokenizer.save_pretrained(checkpoint)
            model = GPT2LMHeadModel(GPT2Config(vocab_size=len(vocab), n_positions=32,
                                                n_ctx=32, n_embd=16, n_layer=1, n_head=2))
            model.save_pretrained(checkpoint, safe_serialization=True)
            original_weight = model.transformer.wte.weight.detach().clone()

            artifact = root / "correction.txt"
            artifact.write_text("correct evidence", encoding="utf-8")
            prompt = "Question wrong"
            state = fresh_state()
            state["tasks"].append({"task_id": "fixed", "status": "verified",
                                   "output_id": "fixed-output",
                                   "verification_checks": {
                                       "training_example_approved": True,
                                       "training_prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest()},
                                   "artifacts": [{"path": str(artifact.resolve()),
                                                  "sha256": sha256(artifact),
                                                  "size": artifact.stat().st_size}]})
            state["events"].append({"event_id": "quality", "kind": "evaluation",
                                    "evaluation_mode": "quality", "target_task_id": "fixed",
                                    "status": "learned", "update_id": "update",
                                    "action_evidence": {"task_id": "fixed",
                                                        "output_id": "fixed-output"}})
            state["updates"].append({"update_id": "update", "new_events": ["quality"]})
            validation_artifact = root / "validation-correction.txt"
            validation_artifact.write_text("correct evidence", encoding="utf-8")
            validation_prompt = "Question correct"
            state["tasks"].append({"task_id": "validation-fixed", "status": "verified",
                                   "output_id": "validation-output",
                                   "verification_checks": {
                                       "training_example_approved": True,
                                       "training_prompt_sha256": hashlib.sha256(
                                           validation_prompt.encode()).hexdigest()},
                                   "artifacts": [{"path": str(validation_artifact.resolve()),
                                                  "sha256": sha256(validation_artifact),
                                                  "size": validation_artifact.stat().st_size}]})
            state["events"].append({"event_id": "validation-quality", "kind": "evaluation",
                                    "evaluation_mode": "quality", "target_task_id": "validation-fixed",
                                    "status": "learned", "update_id": "validation-update",
                                    "action_evidence": {"task_id": "validation-fixed",
                                                        "output_id": "validation-output"}})
            state["updates"].append({"update_id": "validation-update",
                                     "new_events": ["validation-quality"]})
            state_path = root / "state.json"
            save(state_path, state)
            manifest = root / "manifest.jsonl"
            manifest.write_text(json.dumps({"event_id": "quality", "task_id": "fixed",
                                            "artifact_path": str(artifact), "prompt": prompt}),
                                encoding="utf-8")
            validation_manifest = root / "validation.jsonl"
            validation_manifest.write_text(json.dumps({"event_id": "validation-quality",
                                                       "task_id": "validation-fixed",
                                                       "artifact_path": str(validation_artifact),
                                                       "prompt": validation_prompt}), encoding="utf-8")
            self.assertEqual(len(verified_examples(state, manifest)), 1)
            tampered = root / "tampered.jsonl"
            tampered.write_text(json.dumps({"event_id": "quality", "task_id": "fixed",
                                            "artifact_path": str(artifact),
                                            "prompt": "Question changed"}), encoding="utf-8")
            with self.assertRaises(ValueError):
                verified_examples(state, tampered)
            artifact.write_text("tampered text", encoding="utf-8")
            with self.assertRaises(ValueError):
                verified_examples(state, manifest)
            artifact.write_text("correct evidence", encoding="utf-8")
            state["updates"][0]["superseded_events"] = [{"event_id": "quality"}]
            with self.assertRaises(ValueError):
                verified_examples(state, manifest)
            state["updates"][0].pop("superseded_events")
            args = argparse.Namespace(state=state_path, manifest=manifest,
                                      validation_manifest=validation_manifest, checkpoint=checkpoint,
                                      output=root / "trained", active_model_id="codex-active",
                                      candidate_model_id="tiny-local", active_checkpoint=None,
                                      steps=1, max_tokens=16, learning_rate=1e-3,
                                      max_validation_regression=0.0,
                                      max_checkpoint_bytes=536870912, device="cpu")
            overlap_args = argparse.Namespace(**{**vars(args),
                                                 "validation_manifest": manifest})
            with self.assertRaises(ValueError):
                train(overlap_args)
            limited_args = argparse.Namespace(**{**vars(args), "max_checkpoint_bytes": 1})
            with self.assertRaises(ValueError):
                train(limited_args)
            result = train(args)
            self.assertGreater(result["gradient_bearing_tensors"], 0)
            self.assertLessEqual(result["validation_loss_after"],
                                 result["validation_loss_before"])
            self.assertTrue((args.output / "model.safetensors").is_file())
            trained = GPT2LMHeadModel.from_pretrained(args.output, local_files_only=True,
                                                       use_safetensors=True)
            self.assertFalse(torch.equal(original_weight, trained.transformer.wte.weight))
            self.assertTrue((checkpoint / "model.safetensors").is_file())


if __name__ == "__main__":
    unittest.main()
