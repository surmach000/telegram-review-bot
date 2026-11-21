import json
import re
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlsplit


MODEL = "gpt-6.1-sol"
WAITING_MESSAGE = "Задание принято. Ожидаю доступности GitLab и включения корпоративного VPN"
MR_LINK = re.compile(r"https://[^\s<>\[\]()]+/-/merge_requests/\d+")
HELP = ("Отправьте ссылку на MR — начну ревью. Несколько ссылок проверю по очереди.\n"
        "Уточнение отправьте ответом на отчёт. Повторное ревью: /review ответом на отчёт.\n"
        "/status — очередь. Ревью: 6.1 Sol high; уточнения: 6.1 Sol medium.")


class BotError(Exception):
    """A safe diagnostic code, never a raw HTTP/subprocess exception."""


class GitLabTemporarilyUnavailable(BotError):
    """A safe, retryable Git transport failure; never credentials or stderr."""


class RateLimited(BotError):
    def __init__(self, seconds):
        super().__init__("HTTP_STATUS_429")
        self.seconds = max(1, int(seconds))


def load_config(path):
    path = Path(path).resolve()
    cfg = json.loads(path.read_text(encoding="utf-8-sig"))
    if cfg.get("model") != MODEL:
        raise BotError("CONFIG_MODEL_MUST_BE_GPT_6_1_SOL")
    if type(cfg.get("allowed_user_id")) is not int or cfg["allowed_user_id"] <= 0:
        raise BotError("CONFIG_ALLOWED_USER_ID_REQUIRED")
    if not cfg.get("projects"):
        raise BotError("CONFIG_PROJECT_ALLOWLIST_REQUIRED")
    branch = cfg.get("base_branch", "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_/-]*", branch):
        raise BotError("CONFIG_EXPLICIT_BASE_BRANCH_REQUIRED")
    for project in cfg["projects"]:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+", project):
            raise BotError("CONFIG_INVALID_PROJECT")
    host = cfg.get("gitlab_host", "")
    if not re.fullmatch(r"[A-Za-z0-9.-]+", host):
        raise BotError("CONFIG_INVALID_GITLAB_HOST")
    sources = cfg.get("review_context_sources", [])
    if (not isinstance(sources, list) or len(sources) > 16 or any(not isinstance(x, str) for x in sources)
            or type(cfg.get("review_context_required", False)) is not bool):
        raise BotError("CONFIG_INVALID_REVIEW_CONTEXT")
    cfg["review_context_sources"] = [str((path.parent / Path(x)).resolve()) for x in sources]
    if cfg.get("review_context_required") and not sources:
        raise BotError("REVIEW_CONTEXT_REQUIRED")
    for field in ("data_directory", "documentation_directory", "test_artifact_cache"):
        if field == "test_artifact_cache" and field not in cfg:
            cfg[field] = "runtime/maven-artifacts"
        value = Path(cfg[field])
        cfg[field] = str((path.parent / value).resolve() if not value.is_absolute() else value)
    if not Path(cfg["documentation_directory"]).is_dir():
        raise BotError("CONFIG_DOCUMENTATION_DIRECTORY_MISSING")
    cfg.setdefault("max_pending_jobs", 30)
    cfg.setdefault("codex_timeout_seconds", 1800)
    cfg.setdefault("run_tests", True)
    cfg.setdefault("test_timeout_seconds", 1800)
    if type(cfg["run_tests"]) is not bool or type(cfg["test_timeout_seconds"]) is not int or not 30 <= cfg["test_timeout_seconds"] <= 7200:
        raise BotError("CONFIG_INVALID_TEST_SETTINGS")
    if (type(cfg["max_pending_jobs"]) is not int or not 1 <= cfg["max_pending_jobs"] <= 100
            or type(cfg["codex_timeout_seconds"]) is not int or not 30 <= cfg["codex_timeout_seconds"] <= 7200):
        raise BotError("CONFIG_INVALID_LIMITS")
    cfg.setdefault("accept_after", int(time.time()))
    return cfg


def links_in(message, cfg):
    # Only the new message body/entities, never replied-to or quoted content.
    content = message.get("text", "") + " " + message.get("caption", "")
    candidates = list(MR_LINK.findall(content))
    for entity in message.get("entities", []) + message.get("caption_entities", []):
        if entity.get("type") == "text_link":
            candidates.extend(MR_LINK.findall(entity.get("url", "")))
    found = []
    for candidate in candidates:
        parsed = urlsplit(candidate)
        match = re.fullmatch(r"/(.+)/-/merge_requests/([1-9][0-9]*)", parsed.path)
        if (parsed.scheme != "https" or parsed.netloc != cfg["gitlab_host"]
                or not match or match[1] not in cfg["projects"]):
            continue
        canonical = "https://" + parsed.netloc + parsed.path
        if canonical not in found:
            found.append(canonical)
    return found


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(str(path), timeout=20)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS updates(id INTEGER PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS jobs(
                id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL, source_id INTEGER NOT NULL,
                kind TEXT NOT NULL, url TEXT NOT NULL, question TEXT NOT NULL,
                parent_id INTEGER, status TEXT NOT NULL DEFAULT 'queued', report TEXT,
                error TEXT, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS outbox(
                id INTEGER PRIMARY KEY, job_id INTEGER, chat_id INTEGER NOT NULL,
                reply_id INTEGER NOT NULL, body TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', telegram_id INTEGER);
        """)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(jobs)")}
        with self.db:
            for name, definition in (("retry_count", "INTEGER NOT NULL DEFAULT 0"),
                                     ("next_attempt", "REAL NOT NULL DEFAULT 0"),
                                     ("waiting_notified", "INTEGER NOT NULL DEFAULT 0"),
                                     ("retry_reason", "TEXT")):
                if name not in columns:
                    self.db.execute("ALTER TABLE jobs ADD COLUMN " + name + " " + definition)
            outbox_columns = {row[1] for row in self.db.execute("PRAGMA table_info(outbox)")}
            for name, definition in (("retry_count", "INTEGER NOT NULL DEFAULT 0"),
                                     ("next_attempt", "REAL NOT NULL DEFAULT 0"), ("last_error", "TEXT")):
                if name not in outbox_columns:
                    self.db.execute("ALTER TABLE outbox ADD COLUMN " + name + " " + definition)

    def recover(self):
        with self.db:
            self.db.execute("UPDATE jobs SET status='queued' WHERE status='running'")
            # The API may have accepted a send before the process died. Never resend blindly.
            self.db.execute("UPDATE outbox SET status='unknown' WHERE status='sending'")

    def next_offset(self):
        row = self.db.execute("SELECT MAX(id) FROM updates").fetchone()
        return (row[0] + 1) if row[0] is not None else 0

    def next_delivery(self, now=None):
        now = time.time() if now is None else now
        return self.db.execute("""SELECT o.* FROM outbox o WHERE o.status='pending' AND o.next_attempt<=?
            AND (o.job_id IS NULL OR NOT EXISTS(SELECT 1 FROM outbox earlier
                WHERE earlier.job_id=o.job_id AND earlier.id<o.id AND earlier.status!='sent'))
            ORDER BY o.id LIMIT 1""", (now,)).fetchone()

    def reports_pending(self):
        return self.db.execute("""SELECT 1 FROM outbox o WHERE o.job_id IS NOT NULL
            AND o.status IN ('pending','sending') AND NOT EXISTS(SELECT 1 FROM outbox earlier
                WHERE earlier.job_id=o.job_id AND earlier.id<o.id AND earlier.status IN ('unknown','failed'))
            LIMIT 1""").fetchone() is not None

    def enqueue_text(self, chat_id, source_id, body, job_id=None):
        # UTF-16 safe even with astral characters; prefixes fit comfortably in 4096.
        chunks, chunk, size = [], [], 0
        for char in body:
            width = 2 if ord(char) > 0xFFFF else 1
            if size + width > 3500:
                chunks.append("".join(chunk))
                chunk, size = [], 0
            chunk.append(char)
            size += width
        if chunk:
            chunks.append("".join(chunk))
        for index, text in enumerate(chunks):
            prefix = "[{}/{}] ".format(index + 1, len(chunks)) if len(chunks) > 1 else ""
            self.db.execute("INSERT INTO outbox(job_id,chat_id,reply_id,body) VALUES(?,?,?,?)",
                            (job_id, chat_id, source_id, prefix + text))

    def parent_for_reply(self, chat_id, reply_id):
        return self.db.execute("""SELECT j.* FROM outbox o JOIN jobs j ON j.id=o.job_id
            WHERE o.chat_id=? AND o.telegram_id=? AND j.report IS NOT NULL""",
                               (chat_id, reply_id)).fetchone()

    def ingest(self, update, cfg):
        with self.db:
            cursor = self.db.execute("INSERT OR IGNORE INTO updates VALUES(?)", (update["update_id"],))
            if not cursor.rowcount:
                return
            msg = update.get("message", {})
            sender, chat = msg.get("from", {}), msg.get("chat", {})
            if (sender.get("id") != cfg["allowed_user_id"] or sender.get("is_bot")
                    or chat.get("type") != "private" or chat.get("id") != sender.get("id")
                    or msg.get("date", 0) < cfg["accept_after"]):
                return
            chat_id, source = chat["id"], msg["message_id"]
            text = msg.get("text", msg.get("caption", "")).strip()
            command = text.split(maxsplit=1)[0].split("@")[0].lower() if text else ""
            if command in ("/start", "/help"):
                self.enqueue_text(chat_id, source, HELP)
                return
            if command == "/status":
                rows = self.db.execute("SELECT status,COUNT(*) n FROM jobs GROUP BY status").fetchall()
                unknown = self.db.execute("SELECT COUNT(*) FROM outbox WHERE status='unknown'").fetchone()[0]
                failed_deliveries = self.db.execute("SELECT COUNT(*) FROM outbox WHERE status='failed'").fetchone()[0]
                waiting = self.db.execute("SELECT id,next_attempt,retry_reason FROM jobs WHERE status='waiting_for_gitlab' ORDER BY id LIMIT 1").fetchone()
                detail = ("; задание #{} ожидает GitLab/VPN; следующая попытка через {} с; причина={}".format(
                    waiting["id"], max(0, int(waiting["next_attempt"] - time.time())), waiting["retry_reason"]) if waiting else "")
                self.enqueue_text(chat_id, source, "Очередь: " + ", ".join(
                    "{}={}".format(row["status"], row["n"]) for row in rows)
                    + "; доставка с неизвестным результатом={}; отклонённых отправок={}".format(unknown, failed_deliveries) + detail)
                return
            parent = self.parent_for_reply(chat_id, msg.get("reply_to_message", {}).get("message_id", -1))
            urls = links_in(msg, cfg)
            repeat = command == "/review" or bool(re.search(
                r"повтор\w*\s+(?:код[- ]?)?ревью|перепроверь|проверь\s+(?:ещё|еще|снова)|re-?review",
                text, re.IGNORECASE))
            if urls:
                specs = [("review", url, None) for url in urls]
            elif parent and repeat:
                specs = [("review", parent["url"], parent["id"])]
            elif parent and text:
                specs = [("question", parent["url"], parent["id"])]
            else:
                self.enqueue_text(chat_id, source, "Нужна ссылка на разрешённый MR или ответ на мой отчёт.\n" + HELP)
                return
            count = self.db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running','waiting_for_gitlab')").fetchone()[0]
            if count + len(specs) > cfg["max_pending_jobs"]:
                self.enqueue_text(chat_id, source, "Очередь заполнена. Отправьте запрос позже.")
                return
            ids = []
            for kind, url, parent_id in specs:
                cursor = self.db.execute("""INSERT INTO jobs(chat_id,source_id,kind,url,question,parent_id,created)
                    VALUES(?,?,?,?,?,?,?)""", (chat_id, source, kind, url, text, parent_id, time.time()))
                ids.append(str(cursor.lastrowid))
            self.enqueue_text(chat_id, source, "Принято. Номера заданий: " + ", ".join(ids))

    def claim(self, now=None):
        now = time.time() if now is None else now
        with self.db:
            row = self.db.execute("SELECT * FROM jobs WHERE status IN ('queued','running','waiting_for_gitlab') ORDER BY id LIMIT 1").fetchone()
            if row and row["status"] != "running" and row["next_attempt"] <= now:
                self.db.execute("UPDATE jobs SET status='running' WHERE id=?", (row["id"],))
                return dict(row)

    def wait_for_gitlab(self, job, reason, now=None):
        now = time.time() if now is None else now
        with self.db:
            row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone()
            attempts = row["retry_count"] + 1
            delay = (30, 60, 120, 300)[min(attempts - 1, 3)]
            self.db.execute("""UPDATE jobs SET status='waiting_for_gitlab',retry_count=?,next_attempt=?,retry_reason=? WHERE id=?""",
                            (attempts, now + delay, reason, job["id"]))
            if not row["waiting_notified"]:
                self.enqueue_text(job["chat_id"], job["source_id"], WAITING_MESSAGE)
                self.db.execute("UPDATE jobs SET waiting_notified=1 WHERE id=?", (job["id"],))

    def complete(self, job, report, failed=False):
        with self.db:
            self.db.execute("UPDATE jobs SET status=?,report=? WHERE id=?",
                            ("failed" if failed else "done", report, job["id"]))
            self.enqueue_text(job["chat_id"], job["source_id"],
                              "Задание #{}\n{}\n\n{}".format(job["id"], job["url"], report), job["id"])
