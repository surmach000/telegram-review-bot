"""Real Git outage/recovery probe using a local disposable queue. Sends no Telegram messages."""
import tempfile
from pathlib import Path
from unittest.mock import patch

from reviewbot.core import Store, GitLabTemporarilyUnavailable, load_config, WAITING_MESSAGE
from reviewbot.worker import Worker, git_transport


root = Path(__file__).resolve().parent
cfg = load_config(root / "config.local.json")
with tempfile.TemporaryDirectory(prefix="review-network-wait-") as temporary:
    cfg["data_directory"] = temporary
    database = Path(temporary) / "queue.db"
    store = Store(database)
    url = "https://{}/{}/-/merge_requests/13".format(cfg["gitlab_host"], cfg["projects"][0])
    # Purely local synthetic queue record, no Telegram transport is instantiated.
    with store.db:
        store.db.execute("""INSERT INTO jobs(id,chat_id,source_id,kind,url,question,created)
            VALUES(1,17,1,'review',?,'network probe',0)""", (url,))
    job = store.claim(now=100)
    worker = Worker(cfg)
    folder = worker.root / "1"
    folder.mkdir()
    def offline(args, cwd, env, timeout=30):
        return git_transport(args[:1] + ["-c", "http.proxy=http://127.0.0.1:9"] + args[1:], cwd, env, timeout)
    try:
        with patch("reviewbot.worker.git_transport", side_effect=offline):
            worker.prepare(job, folder)
    except GitLabTemporarilyUnavailable as failure:
        store.wait_for_gitlab(job, str(failure), now=100)
    else:
        raise RuntimeError("Expected local-proxy failure was not observed")
    assert store.claim(now=129) is None
    assert store.db.execute("SELECT body FROM outbox").fetchone()[0] == WAITING_MESSAGE
    store.db.close()
    store = Store(database)
    store.recover()
    restored = store.claim(now=130)
    assert restored["id"] == 1 and restored["retry_count"] == 1
    pinned = worker.prepare(restored, folder)
    assert len(pinned["head_sha"]) == 40
    assert store.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1
    store.db.close()
    print("PASS: real Git outage -> persistent wait -> reopen queue -> real Git recovery.")
    print("PASS: exact waiting text once; no Telegram messages, model calls or test runs.")
