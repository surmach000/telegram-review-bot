import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
import socket
import ssl
import urllib.error
from pathlib import Path
from unittest.mock import patch

from reviewbot.core import BotError, MODEL, Store, links_in, load_config
from reviewbot.telegram import Telegram, request_json
from reviewbot.worker import Worker, child_environment, extract_snapshot, prepare_documentation, snapshot_documentation
from reviewbot.__main__ import SingleInstance


URL = "https://gitlab.5-element.tech/5element/edo-experimental/gateway/-/merge_requests/13"


class BotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cfg = {"gitlab_host": "gitlab.5-element.tech", "model": MODEL,
                    "run_tests": False,
                    "base_branch": "dev",
                    "projects": ["5element/edo-experimental/gateway"], "allowed_user_id": 17,
                    "accept_after": 100, "max_pending_jobs": 30, "data_directory": str(self.root),
                    "documentation_directory": str(self.root / "docs"), "codex_timeout_seconds": 30}
        (self.root / "docs").mkdir()
        self.store = Store(self.root / "queue.db")
        self.addCleanup(self.store.db.close)

    def update(self, text, update_id=1, user=17, **extra):
        message = {"text": text, "from": {"id": user}, "chat": {"id": user, "type": "private"},
                   "message_id": update_id * 10, "date": 200}
        message.update(extra)
        return {"update_id": update_id, "message": message}

    def jobs(self):
        return self.store.db.execute("SELECT * FROM jobs ORDER BY id").fetchall()

    def finish_report(self):
        self.store.ingest(self.update(URL), self.cfg)
        job = self.store.claim()
        self.store.complete(job, "Original report")
        with self.store.db:
            self.store.db.execute("UPDATE outbox SET telegram_id=900,status='sent' WHERE job_id=?", (job["id"],))
        return job

    def test_multiple_mrs_are_fifo_and_not_deduplicated_across_messages(self):
        self.store.ingest(self.update(URL + "\n" + URL.replace("/13", "/14")), self.cfg)
        self.store.ingest(self.update(URL, 2), self.cfg)
        self.assertEqual([r["url"] for r in self.jobs()], [URL, URL.replace("/13", "/14"), URL])
        first = self.store.claim()
        self.assertEqual(first["id"], 1)
        self.store.complete(first, "First review done")
        self.assertEqual(self.store.claim()["id"], 2)

    def test_same_update_replay_is_idempotent_and_offset_is_durable(self):
        self.store.ingest(self.update(URL, 11), self.cfg)
        self.store.ingest(self.update(URL, 11), self.cfg)
        self.assertEqual(len(self.jobs()), 1)
        other = Store(self.root / "queue.db")
        self.addCleanup(other.db.close)
        self.assertEqual(other.next_offset(), 12)

    def test_unauthorized_group_bot_and_old_messages_are_ignored(self):
        updates = [self.update(URL, 1, user=18),
                   self.update(URL, 2, chat={"id": 17, "type": "group"}),
                   self.update(URL, 3, **{"from": {"id": 17, "is_bot": True}}),
                   self.update(URL, 4, date=99)]
        for update in updates:
            self.store.ingest(update, self.cfg)
        self.assertEqual(len(self.jobs()), 0)
        self.assertEqual(self.store.next_offset(), 5)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 0)

    def test_replied_to_link_is_not_a_new_review(self):
        self.finish_report()
        self.store.ingest(self.update("Объясни замечание", 2,
            reply_to_message={"message_id": 900, "text": URL}), self.cfg)
        self.assertEqual(self.jobs()[-1]["kind"], "question")
        self.assertEqual(self.jobs()[-1]["parent_id"], 1)

    def test_repeat_request_uses_high_review_kind(self):
        self.finish_report()
        for index, text in enumerate(("/review", "Повторное ревью", "проверь снова"), 2):
            self.store.ingest(self.update(text, index, reply_to_message={"message_id": 900}), self.cfg)
        self.assertTrue(all(row["kind"] == "review" for row in self.jobs()[1:]))

    def test_unrelated_reply_does_not_attach_to_last_mr(self):
        self.finish_report()
        self.store.ingest(self.update("Объясни", 2, reply_to_message={"message_id": 901}), self.cfg)
        self.assertEqual(len(self.jobs()), 1)

    def test_only_exact_https_host_and_allowlisted_project_are_accepted(self):
        for text in (URL.replace("https:", "http:"), URL.replace("gitlab.5-element.tech", "evil.example"),
                     URL.replace("gateway/-/", "unknown/-/"), URL.replace("/13", "/0"),
                     URL.replace("gitlab.5-element.tech", "gitlab.5-element.tech@evil.example")):
            self.assertEqual(links_in({"text": text}, self.cfg), [])
        self.assertEqual(links_in({"text": URL + "?x=1#abc"}, self.cfg), [URL])

    def test_hidden_telegram_link_is_supported(self):
        msg = {"text": "Посмотри MR", "entities": [{"type": "text_link", "url": URL}]}
        self.assertEqual(links_in(msg, self.cfg), [URL])

    def test_capacity_rejects_whole_batch(self):
        self.cfg["max_pending_jobs"] = 1
        self.store.ingest(self.update(URL + " " + URL.replace("/13", "/14")), self.cfg)
        self.assertEqual(len(self.jobs()), 0)

    def test_status_does_not_enqueue_model_job(self):
        self.store.ingest(self.update("/status"), self.cfg)
        self.assertEqual(len(self.jobs()), 0)
        self.assertIn("Очередь", self.store.db.execute("SELECT body FROM outbox").fetchone()[0])

    def test_ingest_and_offset_roll_back_together_on_failure(self):
        with patch.object(self.store, "enqueue_text", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self.store.ingest(self.update(URL), self.cfg)
        self.assertEqual(self.store.next_offset(), 0)
        self.assertEqual(len(self.jobs()), 0)

    def test_long_report_is_split_with_utf16_limit_and_shared_job_id(self):
        job = self.finish_report()
        self.store.complete(job, "😀" * 9000)
        rows = self.store.db.execute("SELECT body,job_id FROM outbox WHERE status='pending' AND job_id IS NOT NULL").fetchall()
        self.assertGreater(len(rows), 3)
        for row in rows:
            self.assertLessEqual(len(row["body"].encode("utf-16-le")) // 2, 4096)
            self.assertEqual(row["job_id"], job["id"])

    def test_delivery_unknown_is_never_automatically_retried(self):
        with self.store.db:
            self.store.enqueue_text(17, 10, "report")
        telegram = Telegram("not-a-real-token")
        with patch.object(telegram, "call", side_effect=BotError("HTTP_REQUEST_FAILED")) as send:
            self.assertFalse(telegram.deliver_one(self.store))
            self.assertFalse(telegram.deliver_one(self.store))
            self.assertEqual(send.call_count, 1)
        self.assertEqual(self.store.db.execute("SELECT status FROM outbox").fetchone()[0], "unknown")

    def test_successful_delivery_preserves_reply_binding(self):
        self.finish_report()
        with self.store.db:
            self.store.db.execute("UPDATE outbox SET status='sent'")
            self.store.enqueue_text(17, 10, "report", 1)
        telegram = Telegram("not-a-real-token")
        with patch.object(telegram, "call", return_value={"message_id": 500}):
            self.assertTrue(telegram.deliver_one(self.store))
        self.assertEqual(self.store.parent_for_reply(17, 500)["id"], 1)
        self.assertIsNone(self.store.parent_for_reply(18, 500))

    def test_recovery_requeues_only_unfinished_reviews_not_completed_reports(self):
        self.store.ingest(self.update(URL), self.cfg)
        self.store.claim()
        with self.store.db:
            self.store.db.execute("UPDATE outbox SET status='sending'")
        self.store.recover()
        self.assertEqual(self.jobs()[0]["status"], "queued")
        self.assertEqual(self.store.db.execute("SELECT status FROM outbox").fetchone()[0], "unknown")

    def test_worker_uses_medium_only_for_explanation_and_escalates_new_analysis(self):
        self.finish_report()
        self.store.ingest(self.update("Нужна ли новая проверка?", 2,
                                     reply_to_message={"message_id": 900}), self.cfg)
        job = dict(self.jobs()[-1])
        worker = Worker(self.cfg)
        with patch.object(worker, "codex", return_value="Разъяснение") as model:
            self.assertIn("medium", worker.execute(job, self.store))
            self.assertEqual(model.call_args[0][2], "medium")
        with patch.object(worker, "codex", side_effect=["NEEDS_REVIEW", "Новый отчёт"]) as model, \
                patch.object(worker, "prepare", return_value={"head_sha": "a" * 40, "base_sha": "b" * 40}):
            self.assertIn("high", worker.execute(job, self.store))
            self.assertEqual([c[0][2] for c in model.call_args_list], ["medium", "high"])

    def test_review_includes_actual_test_failure_in_model_context_and_final_report(self):
        self.cfg["run_tests"] = True
        self.store.ingest(self.update(URL), self.cfg)
        job = self.store.claim()
        worker = Worker(self.cfg)
        result = {"status": "FAILED", "unit": {"tests": 2, "failures": 1, "errors": 0, "skipped": 0}, "integration": {}}
        with patch.object(worker, "prepare", return_value={"head_sha": "a" * 40, "base_sha": "b" * 40}), \
                patch("reviewbot.worker.TestRunner.run", return_value=result) as tests, \
                patch.object(worker, "codex", return_value="Review") as model:
            report = worker.execute(job, self.store)
        self.assertIn("FAILED", report)
        self.assertIn("failures=1", report)
        self.assertIn("test-summary.json", model.call_args[0][1])
        self.assertEqual(model.call_args[0][2], "high")
        tests.assert_called_once()

    def test_codex_arguments_pin_model_reasoning_and_read_only_without_fallback(self):
        worker = Worker(self.cfg)
        def fake_run(args, **kwargs):
            proof = (self.root / "read-proof.txt").read_text(encoding="utf-8")
            Path(args[args.index("--output-last-message") + 1]).write_text(proof + "\nReport", encoding="utf-8")
            return subprocess.CompletedProcess(args, 0)
        with patch("reviewbot.worker.subprocess.run", side_effect=fake_run) as run:
            self.assertEqual(worker.codex(self.root, "prompt", "high"), "Report")
        args = run.call_args[0][0]
        self.assertEqual(args[args.index("--model") + 1], "gpt-6.1-sol")
        self.assertIn('model_reasoning_effort="high"', args)
        self.assertEqual(args[args.index("--sandbox") + 1], "read-only")
        self.assertIn("--ignore-user-config", args)
        with patch("reviewbot.worker.subprocess.run", return_value=subprocess.CompletedProcess([], 1)) as run:
            with self.assertRaisesRegex(BotError, "NO_MODEL_FALLBACK"):
                worker.codex(self.root, "prompt", "medium")
            self.assertEqual(run.call_count, 1)

    def test_codex_cannot_report_success_if_file_read_was_blocked(self):
        worker = Worker(self.cfg)
        def fake_run(args, **kwargs):
            Path(args[args.index("--output-last-message") + 1]).write_text("Cannot read: blocked by policy", encoding="utf-8")
            return subprocess.CompletedProcess(args, 0)
        with patch("reviewbot.worker.subprocess.run", side_effect=fake_run):
            with self.assertRaisesRegex(BotError, "FILE_READ_NOT_CONFIRMED"):
                worker.codex(self.root, "prompt", "high")

    def test_child_environment_does_not_inherit_service_credentials(self):
        with patch.dict(os.environ, {"REVIEW_TELEGRAM_TOKEN": "secret", "REVIEW_GITLAB_TOKEN": "secret",
                                     "OPENAI_API_KEY": "secret", "AWS_SECRET_ACCESS_KEY": "secret"}):
            child = child_environment()
            self.assertNotIn("REVIEW_TELEGRAM_TOKEN", child)
            self.assertNotIn("REVIEW_GITLAB_TOKEN", child)
            self.assertNotIn("OPENAI_API_KEY", child)
            self.assertNotIn("AWS_SECRET_ACCESS_KEY", child)

    def test_config_rejects_weaker_model_and_unconfigured_recipient(self):
        cfg = self.cfg.copy()
        config = self.root / "config.json"
        for key, value in (("model", "gpt-6.1-terra"), ("allowed_user_id", 0)):
            cfg = self.cfg.copy()
            cfg[key] = value
            config.write_text(json.dumps(cfg), encoding="utf-8")
            with self.assertRaises(BotError):
                load_config(config)

    def test_git_only_mode_requires_an_explicit_base_branch_without_gitlab_token(self):
        config = self.root / "config.json"
        config.write_text(json.dumps(self.cfg), encoding="utf-8")
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(load_config(config)["base_branch"], "dev")
        cfg = self.cfg.copy()
        del cfg["base_branch"]
        config.write_text(json.dumps(cfg), encoding="utf-8")
        with self.assertRaisesRegex(BotError, "EXPLICIT_BASE_BRANCH"):
            load_config(config)

    def test_documentation_snapshot_keeps_source_hash(self):
        source = self.root / "requirements.md"
        source.write_text("Required behaviour", encoding="utf-8")
        self.cfg["documentation_sources"] = [str(source)]
        prepare_documentation(self.cfg)
        manifest = json.loads((self.root / "docs" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest[0]["sha256"]), 64)
        self.assertEqual(manifest[0]["source"], str(source))

    def test_documentation_snapshot_ignores_obsolete_files_and_rejects_tampering(self):
        source = self.root / "requirements.md"
        source.write_text("Line one\nLine two", encoding="utf-8")
        self.cfg["documentation_sources"] = [str(source)]
        prepare_documentation(self.cfg)
        docs = self.root / "docs"
        (docs / "obsolete.txt").write_text("Old requirements", encoding="utf-8")
        snapshot_documentation(docs, self.root / "frozen")
        self.assertFalse((self.root / "frozen" / "obsolete.txt").exists())
        self.assertEqual((self.root / "frozen" / "01-requirements.txt").read_text(), "Line one\nLine two")
        (docs / "01-requirements.txt").write_text("Changed", encoding="utf-8")
        with self.assertRaisesRegex(BotError, "DOCUMENTATION_CHANGED"):
            snapshot_documentation(docs, self.root / "tampered")

    def test_unknown_delivery_blocks_later_report_fragments(self):
        with self.store.db:
            self.store.enqueue_text(17, 10, "first")
            self.store.enqueue_text(17, 10, "second")
            self.store.db.execute("UPDATE outbox SET status='unknown' WHERE id=1")
        telegram = Telegram("not-a-real-token")
        with patch.object(telegram, "call") as send:
            self.assertFalse(telegram.deliver_one(self.store))
            send.assert_not_called()

    def test_second_process_cannot_lock_the_same_bot(self):
        lock = SingleInstance(self.root / "bot.lock")
        self.addCleanup(lock.close)
        with self.assertRaisesRegex(BotError, "ALREADY_RUNNING"):
            SingleInstance(self.root / "bot.lock")


class GitSnapshotTest(unittest.TestCase):
    def test_real_git_snapshot_is_pinned_and_excludes_agent_configuration(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "source"
            repo.mkdir()
            def git(*args):
                return subprocess.check_output(["git", "-C", str(repo)] + list(args), stderr=subprocess.DEVNULL).decode().strip()
            git("init")
            git("config", "user.email", "test@example.invalid")
            git("config", "user.name", "Test")
            (repo / "app.txt").write_text("version one", encoding="utf-8")
            (repo / ".codex").mkdir()
            (repo / ".codex" / "config.toml").write_text("untrusted", encoding="utf-8")
            (repo / "AGENTS.md").write_text("untrusted", encoding="utf-8")
            git("add", ".")
            git("commit", "-m", "fixture")
            sha = git("rev-parse", "HEAD")
            (repo / "app.txt").write_text("uncommitted change", encoding="utf-8")
            extract_snapshot(repo / ".git", sha, root / "snapshot")
            self.assertEqual((root / "snapshot" / "app.txt").read_text(), "version one")
            self.assertFalse((root / "snapshot" / ".codex").exists())
            self.assertFalse((root / "snapshot" / "AGENTS.md").exists())


class HttpDiagnosticsTest(unittest.TestCase):
    def test_html_response_is_distinguished_from_authentication_rejection(self):
        with patch("reviewbot.telegram.urllib.request.build_opener") as opener:
            response = opener.return_value.open.return_value.__enter__.return_value
            response.read.return_value = b"<html>PRIVATE_SECRET</html>"
            response.status = 200
            response.headers.get_content_type.return_value = "text/html"
            with self.assertRaisesRegex(BotError, "^HTTP_RESPONSE_NOT_JSON_STATUS_200_HTML$"):
                request_json("https://example.invalid")

    def test_errors_expose_only_safe_category_without_token_url_or_response_body(self):
        url = "https://example.invalid/botPRIVATE_SECRET/getMe"
        cases = [
            (urllib.error.HTTPError(url, 401, "PRIVATE_SECRET", {}, None), "HTTP_STATUS_401"),
            (urllib.error.URLError(socket.gaierror("PRIVATE_SECRET")), "HTTP_DNS_FAILED"),
            (urllib.error.URLError(ssl.SSLCertVerificationError("PRIVATE_SECRET")), "HTTP_TLS_CERTIFICATE_UNTRUSTED"),
            (urllib.error.URLError(socket.timeout("PRIVATE_SECRET")), "HTTP_TIMEOUT")]
        for failure, expected in cases:
            with self.subTest(expected=expected), patch("reviewbot.telegram.urllib.request.build_opener") as opener:
                opener.return_value.open.side_effect = failure
                with self.assertRaises(BotError) as caught:
                    request_json(url)
                self.assertEqual(str(caught.exception), expected)
                self.assertNotIn("PRIVATE_SECRET", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
