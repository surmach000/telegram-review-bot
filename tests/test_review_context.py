import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from reviewbot.core import BotError, Store
from reviewbot.review_context import snapshot_context, verify_snapshot, copy_previous_context
from reviewbot.worker import Worker


class ReviewContextTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "decisions.md"
        self.source.write_text("Закреплять получателя А при первом обнаружении", encoding="utf-8")
        self.cfg = {"review_context_required": True, "review_context_sources": [str(self.source)],
                    "data_directory": str(self.root), "run_tests": False, "base_branch": "dev"}

    def test_source_update_reaches_new_jobs_but_existing_job_remains_pinned(self):
        first = snapshot_context(self.cfg, self.root / "first")
        self.source.write_text("Новое согласованное решение", encoding="utf-8")
        self.assertEqual(snapshot_context(self.cfg, self.root / "first")["digest"], first["digest"])
        second = snapshot_context(self.cfg, self.root / "second")
        self.assertNotEqual(first["digest"], second["digest"])
        self.assertEqual(len(first["files"][0]["sha256"]), 64)
        self.assertEqual(first["files"][0]["source"], str(self.source.resolve()))

    def test_required_context_does_not_disappear_silently(self):
        self.source.unlink()
        with self.assertRaisesRegex(BotError, "SOURCE_UNAVAILABLE"):
            snapshot_context(self.cfg, self.root / "snapshot")
        self.assertFalse((self.root / "snapshot").exists())
        self.assertEqual(list(self.root.glob("review-context-*")), [])

    def test_empty_required_context_is_an_error(self):
        self.cfg["review_context_sources"] = []
        with self.assertRaisesRegex(BotError, "CONTEXT_REQUIRED"):
            snapshot_context(self.cfg, self.root / "snapshot")

    def test_modified_snapshot_is_rejected_instead_of_reinterpreting_review(self):
        folder = self.root / "snapshot"
        manifest = snapshot_context(self.cfg, folder)
        (folder / manifest["files"][0]["file"]).write_text("Changed", encoding="utf-8")
        with self.assertRaisesRegex(BotError, "SNAPSHOT_CHANGED"):
            snapshot_context(self.cfg, folder)

    def test_known_credential_format_is_not_copied_to_review(self):
        self.source.write_text("glpat-" + "a" * 25, encoding="utf-8")
        with self.assertRaisesRegex(BotError, "POSSIBLE_CREDENTIAL"):
            snapshot_context(self.cfg, self.root / "snapshot")
        self.assertFalse((self.root / "snapshot").exists())

    def test_previous_review_context_is_preserved_for_questions(self):
        first = snapshot_context(self.cfg, self.root / "original")
        self.source.write_text("New decision", encoding="utf-8")
        current = snapshot_context(self.cfg, self.root / "current")
        copy_previous_context(self.root / "original", self.root / "previous")
        self.assertEqual(verify_snapshot(self.root / "previous")["digest"], first["digest"])
        self.assertNotEqual(current["digest"], first["digest"])

    def test_full_review_receives_working_context_and_reports_its_version(self):
        worker = Worker(self.cfg)
        store = Store(self.root / "queue.db")
        self.addCleanup(store.db.close)
        job = {"id": 1, "kind": "review", "url": "https://example.invalid/group/project/-/merge_requests/1"}
        with patch.object(worker, "prepare", return_value={"head_sha": "a", "base_sha": "b"}), \
                patch.object(worker, "codex", return_value="Review") as model:
            answer = worker.execute(job, store)
        self.assertIn("review-context/", model.call_args[0][1])
        self.assertIn("не проверяй оформление кода и тестов", model.call_args[0][1])
        self.assertEqual(model.call_args[0][2], "high")
        manifest = verify_snapshot(worker.root / "1" / "review-context")
        self.assertIn(manifest["digest"][:12], answer)

    def test_followup_receives_current_and_original_context_without_test_rerun(self):
        worker = Worker(self.cfg)
        original = worker.root / "1"
        original.mkdir()
        first = snapshot_context(self.cfg, original / "review-context")
        self.source.write_text("Updated decision", encoding="utf-8")
        store = Store(self.root / "queue.db")
        self.addCleanup(store.db.close)
        with store.db:
            store.db.execute("""INSERT INTO jobs(id,chat_id,source_id,kind,url,question,status,report,created)
                VALUES(1,17,10,'review','https://example.invalid/mr','q','done','Original report',0)""")
        job = {"id": 2, "kind": "question", "parent_id": 1, "url": "https://example.invalid/mr", "question": "Explain"}
        with patch.object(worker, "codex", return_value="Explanation") as model, patch.object(worker, "prepare") as prepare:
            worker.execute(job, store)
        self.assertIn("previous-review-context", model.call_args[0][1])
        self.assertEqual(model.call_args[0][2], "medium")
        self.assertEqual(verify_snapshot(worker.root / "2" / "previous-review-context")["digest"], first["digest"])
        self.assertNotEqual(verify_snapshot(worker.root / "2" / "review-context")["digest"], first["digest"])
        prepare.assert_not_called()
