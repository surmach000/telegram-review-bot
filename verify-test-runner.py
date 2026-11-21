"""Run one real MR test recipe without invoking the model or sending Telegram messages."""
import argparse
import json
from pathlib import Path

from reviewbot.core import load_config
from reviewbot.worker import Worker
from reviewbot.test_runner import TestRunner


parser = argparse.ArgumentParser()
parser.add_argument("project", choices=("gateway", "server", "auth-service"))
args = parser.parse_args()
root = Path(__file__).resolve().parent
cfg = load_config(root / "config.local.json")
project = "5element/edo-experimental/" + args.project
iid = {"gateway": 13, "server": 8, "auth-service": 1}[args.project]
folder = root / "runtime" / "runner-verification" / args.project
attempt = 0
while folder.exists():
    attempt += 1
    folder = root / "runtime" / "runner-verification" / (args.project + "-" + str(attempt))
folder.mkdir(parents=True)
print("Preparing pinned MR snapshot: " + args.project, flush=True)
Worker(cfg).prepare({"url": "https://{}/{}/-/merge_requests/{}".format(cfg["gitlab_host"], project, iid),
                     "question": "Isolated test runner verification"}, folder)
print("Running isolated Maven tests...", flush=True)
result = TestRunner(cfg).run(folder, args.project)
print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
print("Results: " + str(folder / "test-results"), flush=True)
