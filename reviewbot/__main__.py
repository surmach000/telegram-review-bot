import argparse
import json
import os
import threading
import time
from pathlib import Path

from .core import BotError, GitLabTemporarilyUnavailable, Store, load_config
from .telegram import Telegram
from .worker import Worker, prepare_documentation


class SingleInstance:
    def __init__(self, path):
        self.file = open(str(path), "a+b")
        self.file.seek(0)
        self.file.write(b"0")
        self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise BotError("BOT_ALREADY_RUNNING") from None

    def close(self):
        self.file.close()


def wait_for_telegram(telegram, stop_path, sleep=None):
    sleep = time.sleep if sleep is None else sleep
    attempts = 0
    while True:
        if stop_path.exists():
            stop_path.unlink()
            return None
        try:
            # Never silently remove a webhook owned by a different deployment.
            if telegram.call("getWebhookInfo", {}).get("url"):
                raise BotError("WEBHOOK_EXISTS_REMOVE_IT_EXPLICITLY")
            return telegram.call("getMe", {})
        except BotError as failure:
            retryable = str(failure) in (
                "HTTP_DNS_FAILED", "HTTP_CONNECT_FAILED", "HTTP_CONNECTION_FAILED", "HTTP_TIMEOUT", "HTTP_REQUEST_FAILED",
                "HTTP_STATUS_408", "HTTP_STATUS_429", "HTTP_STATUS_500", "HTTP_STATUS_502",
                "HTTP_STATUS_503", "HTTP_STATUS_504")
            if not retryable:
                raise
            delay = (5, 10, 30, 60)[min(attempts, 3)]
            attempts += 1
            print("Waiting for Telegram; code={}; retry in {}s".format(failure, delay), flush=True)
            for _ in range(delay):
                if stop_path.exists():
                    stop_path.unlink()
                    return None
                sleep(1)


def review_loop(cfg, database, stop):
    store = Store(database)
    worker = Worker(cfg)
    try:
        while not stop.is_set():
            # Deliver the preceding report before beginning the next job.
            blocked = store.reports_pending()
            job = None if blocked else store.claim()
            if not job:
                stop.wait(1)
                continue
            print("job={} started kind={}".format(job["id"], job["kind"]), flush=True)
            try:
                report = worker.execute(job, store)
                store.complete(job, report)
                print("job={} complete".format(job["id"]), flush=True)
            except GitLabTemporarilyUnavailable as error:
                store.wait_for_gitlab(job, str(error))
                print("job={} waiting_for_gitlab code={}".format(job["id"], error), flush=True)
            except Exception as error:
                code = str(error) if isinstance(error, BotError) else "INTERNAL_WORKER_FAILURE"
                store.complete(job, "Ревью не выполнено. Код: {}. Снижения модели не было.".format(code), failed=True)
                print("job={} failed code={}".format(job["id"], code), flush=True)
    finally:
        store.db.close()


def serve(cfg, store, database):
    token = os.environ.get("REVIEW_TELEGRAM_TOKEN", "")
    if not token:
        raise BotError("BOT_TOKEN_REQUIRED")
    telegram = Telegram(token)
    identity = wait_for_telegram(telegram, Path(cfg["data_directory"]) / "stop.request")
    if identity is None:
        return
    saved = store.db.execute("SELECT value FROM meta WHERE key='bot_id'").fetchone()
    if saved and saved[0] != str(identity["id"]):
        raise BotError("BOT_ID_CHANGED_USE_SEPARATE_DATA_DIRECTORY")
    with store.db:
        store.db.execute("INSERT OR IGNORE INTO meta VALUES('bot_id',?)", (str(identity["id"]),))
        store.db.execute("INSERT OR IGNORE INTO meta VALUES('accept_after',?)", (str(cfg["accept_after"]),))
    cfg["accept_after"] = int(store.db.execute("SELECT value FROM meta WHERE key='accept_after'").fetchone()[0])
    store.recover()
    stop = threading.Event()
    worker = threading.Thread(target=review_loop, args=(cfg, database, stop), daemon=True)
    worker.start()
    print("Bot running. FIFO, 6.1 Sol high/medium. Ctrl+C to stop.", flush=True)
    try:
        while True:
            stop_path = Path(cfg["data_directory"]) / "stop.request"
            if stop_path.exists():
                stop_path.unlink()
                print("Stop requested. Waiting for the active review to finish.", flush=True)
                break
            for _ in range(10):
                if not telegram.deliver_one(store):
                    break
                time.sleep(1.1)
            try:
                for update in telegram.updates(store.next_offset()):
                    store.ingest(update, cfg)
            except BotError as error:
                print("poll failed code={}".format(error), flush=True)
                stop.wait(5)
            if not worker.is_alive():
                raise BotError("WORKER_STOPPED")
    except KeyboardInterrupt:
        print("Stopping after the current review. Do not start another instance yet.", flush=True)
    finally:
        stop.set()
        worker.join()


def main():
    parser = argparse.ArgumentParser(description="Private MR review bot")
    parser.add_argument("command", choices=("run", "stop", "prepare-docs", "discover", "queue", "deliveries", "retry-delivery", "ack-delivery"))
    parser.add_argument("--config", default="config.local.json")
    parser.add_argument("--id", type=int)
    parser.add_argument("--message-id", type=int)
    args = parser.parse_args()
    if args.command == "discover":
        token = os.environ.get("REVIEW_TELEGRAM_TOKEN", "")
        if not token:
            raise BotError("BOT_TOKEN_REQUIRED")
        # No offset: this inspection does not acknowledge or discard the /start update.
        updates = Telegram(token).call("getUpdates", {"timeout": 0, "limit": 100, "allowed_updates": ["message"]})
        ids = sorted({u["message"]["from"]["id"] for u in updates if u.get("message", {}).get("chat", {}).get("type") == "private"})
        print(json.dumps({"private_user_ids": ids}))
        return
    cfg = load_config(args.config)
    if args.command == "prepare-docs":
        prepare_documentation(cfg)
        print("Documentation prepared.")
        return
    data = Path(cfg["data_directory"])
    data.mkdir(parents=True, exist_ok=True)
    if args.command == "stop":
        (data / "stop.request").write_text("stop", encoding="utf-8")
        print("Stop requested; active review will finish before exit.")
        return
    lock = SingleInstance(data / "bot.lock")
    database = data / "queue.sqlite3"
    store = Store(database)
    try:
        if args.command == "run":
            serve(cfg, store, database)
        elif args.command == "queue":
            for row in store.db.execute("SELECT id,kind,status,url FROM jobs ORDER BY id"):
                print(json.dumps(dict(row), ensure_ascii=False))
        elif args.command == "deliveries":
            for row in store.db.execute("SELECT id,job_id,status,telegram_id,retry_count,next_attempt,last_error FROM outbox ORDER BY id"):
                print(json.dumps(dict(row)))
        elif args.command == "retry-delivery":
            if not args.id:
                raise BotError("DELIVERY_ID_REQUIRED")
            with store.db:
                cursor = store.db.execute("UPDATE outbox SET status='pending',next_attempt=0,retry_count=0,last_error=NULL WHERE id=? AND status IN ('unknown','failed')", (args.id,))
                if not cursor.rowcount:
                    raise BotError("UNKNOWN_DELIVERY_NOT_FOUND")
            print("Delivery queued again. This explicit retry may duplicate a previously delivered message.")
        elif args.command == "ack-delivery":
            if not args.id or not args.message_id or args.message_id <= 0:
                raise BotError("DELIVERY_AND_ACTUAL_TELEGRAM_MESSAGE_ID_REQUIRED")
            with store.db:
                cursor = store.db.execute("""UPDATE outbox SET status='sent',telegram_id=?
                    WHERE id=? AND status='unknown'""", (args.message_id, args.id))
                if not cursor.rowcount:
                    raise BotError("UNKNOWN_DELIVERY_NOT_FOUND")
            print("Existing Telegram delivery acknowledged. No message sent.")
    finally:
        store.db.close()
        lock.close()


if __name__ == "__main__":
    try:
        main()
    except BotError as failure:
        print("ERROR: " + str(failure))
        raise SystemExit(1)
