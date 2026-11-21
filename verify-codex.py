"""An explicit small live check: no MR, Telegram or GitLab requests."""
import shutil
import tempfile
from pathlib import Path

from reviewbot.core import BotError
from reviewbot.worker import Worker


with tempfile.TemporaryDirectory(prefix="review-bot-probe-") as temporary:
    cfg = {"data_directory": temporary, "codex_command": shutil.which("codex"),
           "codex_timeout_seconds": 120}
    if not cfg["codex_command"]:
        raise SystemExit("Codex CLI not found")
    try:
        answer = Worker(cfg).codex(Path(temporary), "After the required file-read proof, reply exactly READY.", "high")
        print("Live read-only 6.1 Sol/high: " + ("PASS" if answer.strip() == "READY" else "UNEXPECTED_RESPONSE"))
    except BotError as failure:
        print("Live 6.1 Sol/high: " + str(failure))
        raise SystemExit(1)
