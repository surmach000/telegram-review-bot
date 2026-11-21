"""Local diagnostic on a public disposable canary; never sends Telegram messages."""
import json
import subprocess
from pathlib import Path
from unittest.mock import patch
import uuid

from reviewbot.core import BotError, load_config
from reviewbot.worker import Worker


cfg = load_config("config.local.json")
folder = Path("runtime/isolation-probes").resolve() / uuid.uuid4().hex
folder.mkdir(parents=True)
original = subprocess.run
with (folder / "events.jsonl").open("wb") as events, (folder / "stderr.log").open("wb") as stderr:
    def invoke(args, **kwargs):
        args = args[:2] + ["--json"] + args[2:]
        kwargs.update(stdout=events, stderr=stderr)
        return original(args, **kwargs)
    try:
        with patch("reviewbot.worker.subprocess.run", side_effect=invoke):
            Worker(cfg).codex(folder, "After reading the proof, reply READY. Use cmd.exe /d /c type to read the proof if PowerShell fails.", "high")
        print("PASS")
    except BotError as failure:
        print(str(failure))
        answer = folder / "answer.txt"
        if answer.exists():
            print(answer.read_text(encoding="utf-8")[:1000])
print("Probe directory: " + str(folder))
