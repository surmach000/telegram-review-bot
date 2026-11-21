import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from reviewbot.core import BotError, GitLabTemporarilyUnavailable, Store, WAITING_MESSAGE
from reviewbot.worker import Worker, git_transport
from reviewbot.__main__ import wait_for_telegram, review_loop


URL = "https://gitlab.5-element.tech/5element/edo-experimental/gateway/-/merge_requests/13"


class WaitingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "queue.db"
        self.store = Store(self.path)
        self.addCleanup(self.store.db.close)
        self.cfg = {"allowed_user_id": 17, "gitlab_host": "gitlab.5-element.tech", "accept_after": 0,
                    "projects": ["5element/edo-experimental/gateway"], "max_pending_jobs": 2,
                    "data_directory": str(self.root), "base_branch": "dev", "run_tests": False}

    def ingest(self, number):
        self.store.ingest({"update_id": number, "message": {"text": URL, "message_id": number * 10,
            "date": 200, "from": {"id": 17}, "chat": {"id": 17, "type": "private"}}}, self.cfg)

    def job(self, number=1):
        return self.store.db.execute("SELECT * FROM jobs WHERE id=?", (number,)).fetchone()

    def test_wait_is_persistent_blocks_following_jobs_and_resumes_same_job(self):
        self.ingest(1)
        self.ingest(2)
        job = self.store.claim(now=100)
        self.store.wait_for_gitlab(job, "GITLAB_CONNECTION_UNAVAILABLE", now=100)
        self.assertIsNone(self.store.claim(now=129))
        reopened = Store(self.path)
        self.addCleanup(reopened.db.close)
        reopened.recover()
        self.assertEqual(reopened.claim(now=130)["id"], 1)
        self.assertIsNone(reopened.claim(now=131))
        reopened.complete(job, "Review complete")
        self.assertEqual(reopened.claim(now=132)["id"], 2)

    def test_wait_message_is_exact_and_sent_only_once_with_capped_delays(self):
        self.ingest(1)
        for number, delay in enumerate((30, 60, 120, 300, 300, 300)):
            now = 1000 + number * 1000
            job = self.store.claim(now=now)
            self.store.wait_for_gitlab(job, "GITLAB_TIMEOUT", now=now)
            self.assertEqual(self.job()["next_attempt"], now + delay)
        messages = [r[0] for r in self.store.db.execute("SELECT body FROM outbox")]
        self.assertEqual(messages.count("Задание принято. Ожидаю доступности GitLab и включения корпоративного VPN"), 1)
        self.assertEqual(self.job()["retry_count"], 6)
        self.assertIsNone(self.job()["report"])

    def test_waiting_jobs_count_towards_capacity(self):
        self.cfg["max_pending_jobs"] = 1
        self.ingest(1)
        self.store.wait_for_gitlab(self.store.claim(), "GITLAB_TIMEOUT")
        self.ingest(2)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)

    def test_status_explains_waiting_without_running_model(self):
        self.ingest(1)
        self.store.wait_for_gitlab(self.store.claim(), "GITLAB_TIMEOUT")
        self.store.ingest({"update_id": 2, "message": {"text": "/status", "message_id": 20, "date": 200,
            "from": {"id": 17}, "chat": {"id": 17, "type": "private"}}}, self.cfg)
        body = self.store.db.execute("SELECT body FROM outbox ORDER BY id DESC LIMIT 1").fetchone()[0]
        self.assertIn("ожидает GitLab/VPN", body)
        self.assertIn("следующая попытка", body)

    def test_existing_database_is_upgraded_without_losing_job(self):
        path = self.root / "old.db"
        old = sqlite3.connect(str(path))
        old.execute("""CREATE TABLE jobs(id INTEGER PRIMARY KEY,chat_id INTEGER,source_id INTEGER,kind TEXT,
            url TEXT,question TEXT,parent_id INTEGER,status TEXT,report TEXT,error TEXT,created REAL)""")
        old.execute("INSERT INTO jobs(id,status) VALUES(1,'done')")
        old.commit()
        old.close()
        upgraded = Store(path)
        self.addCleanup(upgraded.db.close)
        row = upgraded.db.execute("SELECT * FROM jobs WHERE id=1").fetchone()
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["next_attempt"], 0)

    def test_interrupted_fetch_is_retried_without_failure_report(self):
        self.ingest(1)
        with self.store.db:
            self.store.db.execute("UPDATE outbox SET status='sent'")
        stop = Mock()
        stop.is_set.side_effect = [False, True]
        with patch("reviewbot.__main__.Worker.execute", side_effect=GitLabTemporarilyUnavailable("GITLAB_TIMEOUT")):
            review_loop(self.cfg, self.path, stop)
        self.assertEqual(self.job()["status"], "waiting_for_gitlab")
        self.assertIsNone(self.job()["report"])

    def test_authorization_error_is_not_retried_as_vpn_outage(self):
        self.ingest(1)
        with self.store.db:
            self.store.db.execute("UPDATE outbox SET status='sent'")
        stop = Mock()
        stop.is_set.side_effect = [False, True]
        with patch("reviewbot.__main__.Worker.execute", side_effect=BotError("GITLAB_AUTH_PERMISSION_REF_OR_CERTIFICATE_ERROR")):
            review_loop(self.cfg, self.path, stop)
        self.assertEqual(self.job()["status"], "failed")
        self.assertEqual(self.job()["retry_count"], 0)

    def test_completed_snapshot_and_tests_are_reused_without_git_or_test_rerun(self):
        self.ingest(1)
        job = self.store.claim()
        worker = Worker(self.cfg)
        folder = worker.root / "1"
        folder.mkdir()
        (folder / "mr.json").write_text(json.dumps({"url": URL, "head_sha": "a", "base_sha": "b"}))
        (folder / "test-results").mkdir()
        (folder / "test-results" / "summary.json").write_text(json.dumps({"status": "PASSED", "unit": {}, "integration": {}}))
        with patch.object(worker, "prepare") as prepare, patch("reviewbot.worker.TestRunner.run") as tests, \
                patch.object(worker, "codex", return_value="Review"):
            result = worker.execute(job, self.store)
        self.assertIn("PASSED", result)
        prepare.assert_not_called()
        tests.assert_not_called()


class GitFailureTests(unittest.TestCase):
    def test_transient_errors_are_classified_without_exposing_stderr(self):
        for message in ("Could not resolve host: secret", "Failed to connect to secret", "early EOF secret",
                        "fatal: unable to access secret: The requested URL returned error: 503"):
            with self.subTest(message=message), patch("reviewbot.worker.subprocess.run",
                return_value=subprocess.CompletedProcess([], 128, b"", message.encode())):
                with self.assertRaises(GitLabTemporarilyUnavailable) as caught:
                    git_transport(["git"], ".", {})
                self.assertNotIn("secret", str(caught.exception))

    def test_permission_reference_and_certificate_errors_are_permanent(self):
        for message in ("fatal: unable to access secret: The requested URL returned error: 403",
                        "Authentication failed secret", "Couldn't find remote ref secret", "SSL certificate problem secret"):
            with self.subTest(message=message), patch("reviewbot.worker.subprocess.run",
                return_value=subprocess.CompletedProcess([], 128, b"", message.encode())):
                with self.assertRaises(BotError) as caught:
                    git_transport(["git"], ".", {})
                self.assertNotIsInstance(caught.exception, GitLabTemporarilyUnavailable)
                self.assertNotIn("secret", str(caught.exception))

    def test_timeout_is_transient_but_missing_git_is_not(self):
        with patch("reviewbot.worker.subprocess.run", side_effect=subprocess.TimeoutExpired("git", 20)):
            with self.assertRaises(GitLabTemporarilyUnavailable):
                git_transport(["git"], ".", {})
        with patch("reviewbot.worker.subprocess.run", side_effect=FileNotFoundError()):
            with self.assertRaisesRegex(BotError, "GIT_EXECUTABLE_UNAVAILABLE"):
                git_transport(["git"], ".", {})


class TelegramStartupTests(unittest.TestCase):
    def test_network_outage_at_startup_is_retried_until_online(self):
        with tempfile.TemporaryDirectory() as temporary:
            telegram = Mock()
            telegram.call.side_effect = [BotError("HTTP_DNS_FAILED"), {}, {"id": 1}]
            sleep = Mock()
            self.assertEqual(wait_for_telegram(telegram, Path(temporary) / "stop", sleep), {"id": 1})
            self.assertEqual(sleep.call_count, 5)

    def test_invalid_token_and_existing_webhook_do_not_loop_forever(self):
        with tempfile.TemporaryDirectory() as temporary:
            for response in (BotError("HTTP_STATUS_401"), {"url": "https://example.invalid/hook"}):
                telegram = Mock()
                telegram.call.side_effect = response if isinstance(response, Exception) else None
                telegram.call.return_value = response
                sleep = Mock()
                with self.assertRaises(BotError):
                    wait_for_telegram(telegram, Path(temporary) / "stop", sleep)
                sleep.assert_not_called()

    def test_waiting_at_startup_can_be_stopped(self):
        with tempfile.TemporaryDirectory() as temporary:
            stop = Path(temporary) / "stop"
            telegram = Mock()
            telegram.call.side_effect = BotError("HTTP_TIMEOUT")
            def sleep(seconds):
                stop.write_text("stop")
            self.assertIsNone(wait_for_telegram(telegram, stop, sleep))
