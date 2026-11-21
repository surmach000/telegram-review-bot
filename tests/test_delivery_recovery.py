import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from reviewbot.core import BotError, Store, RateLimited
from reviewbot.telegram import Telegram, request_json
from reviewbot.permissions import permission_arguments


class DeliveryRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "queue.db")
        self.addCleanup(self.store.db.close)
        self.telegram = Telegram("fake-token")

    def enqueue(self, body="message", job=None):
        with self.store.db:
            self.store.enqueue_text(17, 10, body, job_id=job)

    def test_unknown_service_delivery_does_not_block_new_service_or_report(self):
        self.enqueue("old")
        self.enqueue("new")
        self.enqueue("report", job=1)
        with self.store.db:
            self.store.db.execute("UPDATE outbox SET status='unknown' WHERE id=1")
        with patch.object(self.telegram, "call", side_effect=[{"message_id": 20}, {"message_id": 21}]) as send:
            self.assertTrue(self.telegram.deliver_one(self.store))
            self.assertTrue(self.telegram.deliver_one(self.store))
        self.assertEqual([c.args[1]["text"] for c in send.call_args_list], ["new", "report"])
        self.assertEqual(self.store.db.execute("SELECT status FROM outbox WHERE id=1").fetchone()[0], "unknown")

    def test_unknown_report_preserves_fragment_order_without_stopping_other_jobs(self):
        self.enqueue("part one", job=1)
        self.enqueue("part two", job=1)
        self.enqueue("status")
        self.enqueue("next report", job=2)
        with self.store.db:
            self.store.db.execute("UPDATE outbox SET status='unknown' WHERE id=1")
        with patch.object(self.telegram, "call", side_effect=[{"message_id": 20}, {"message_id": 21}]) as send:
            self.telegram.deliver_one(self.store)
            self.telegram.deliver_one(self.store)
        self.assertEqual([c.args[1]["text"] for c in send.call_args_list], ["status", "next report"])
        self.assertFalse(self.store.reports_pending())

    def test_dns_failure_is_retried_after_backoff_without_blocking_other_replies(self):
        self.enqueue("first")
        self.enqueue("second")
        with patch.object(self.telegram, "call", side_effect=BotError("HTTP_DNS_FAILED")), patch("reviewbot.telegram.time.time", return_value=100):
            self.assertFalse(self.telegram.deliver_one(self.store))
        row = self.store.db.execute("SELECT * FROM outbox WHERE id=1").fetchone()
        self.assertEqual((row["status"], row["next_attempt"], row["retry_count"]), ("pending", 130, 1))
        self.assertEqual(self.store.next_delivery(now=129)["id"], 2)
        self.assertEqual(self.store.next_delivery(now=130)["id"], 1)

    def test_timeout_remains_ambiguous_and_is_not_blindly_retried(self):
        self.enqueue()
        with patch.object(self.telegram, "call", side_effect=BotError("HTTP_TIMEOUT")) as send:
            self.telegram.deliver_one(self.store)
            self.telegram.deliver_one(self.store)
            send.assert_called_once()
        self.assertEqual(self.store.db.execute("SELECT status FROM outbox").fetchone()[0], "unknown")

    def test_rate_limit_uses_server_retry_after(self):
        self.enqueue()
        with patch.object(self.telegram, "call", side_effect=RateLimited(900)), patch("reviewbot.telegram.time.time", return_value=100):
            self.telegram.deliver_one(self.store)
        self.assertEqual(self.store.db.execute("SELECT next_attempt FROM outbox").fetchone()[0], 1000)

    def test_permanent_rejection_does_not_freeze_later_messages(self):
        self.enqueue("rejected")
        self.enqueue("next")
        with patch.object(self.telegram, "call", side_effect=[BotError("HTTP_STATUS_400"), {"message_id": 20}]):
            self.telegram.deliver_one(self.store)
            self.assertTrue(self.telegram.deliver_one(self.store))
        self.assertEqual(self.store.db.execute("SELECT status FROM outbox WHERE id=1").fetchone()[0], "failed")

    def test_http_429_retains_retry_delay_without_exposing_body(self):
        failure = urllib.error.HTTPError("https://example.invalid", 429, "ignored", {},
            io.BytesIO(json.dumps({"parameters": {"retry_after": 901}, "description": "secret"}).encode()))
        with patch("reviewbot.telegram.urllib.request.build_opener") as opener:
            opener.return_value.open.side_effect = failure
            with self.assertRaises(RateLimited) as caught:
                request_json("https://example.invalid")
        self.assertEqual(caught.exception.seconds, 901)
        self.assertNotIn("secret", str(caught.exception))

    def test_permission_profile_has_no_broad_read_or_legacy_fallback(self):
        args = permission_arguments()
        text = " ".join(args)
        self.assertIn('":root"="deny"', text)
        self.assertIn('":workspace_roots"={"."="read"}', text)
        self.assertIn("network.enabled=false", text)
        self.assertNotIn("elevated", text)
        self.assertNotIn("danger-full-access", text)
