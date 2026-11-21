"""Probe existing Git credentials in memory; never display or persist the secret."""
import json
import os
import runpy
import subprocess
from pathlib import Path
from urllib.parse import quote

from reviewbot.core import BotError
from reviewbot.telegram import request_json


def main():
    root = Path(__file__).resolve().parent
    cfg = json.loads((root / "config.example.json").read_text(encoding="utf-8"))
    host = cfg["gitlab_host"]
    env = os.environ.copy()
    env.update({"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never"})
    try:
        result = subprocess.run(["git", "credential", "fill"],
            input=("protocol=https\nhost=" + host + "\n\n").encode("utf-8"),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        raise BotError("EXISTING_GIT_CREDENTIAL_UNAVAILABLE") from None
    values = dict(line.split("=", 1) for line in result.stdout.decode("utf-8").splitlines() if "=" in line)
    secret = values.get("password", "")
    if result.returncode or not secret:
        raise BotError("NO_EXISTING_GIT_CREDENTIAL")
    print("OK: existing Git credential obtained in memory; value hidden.", flush=True)
    remote = "https://{}/{}.git".format(host, cfg["projects"][0])
    try:
        git_result = subprocess.run(["git", "ls-remote", "--exit-code", remote, "HEAD"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        raise BotError("EXISTING_GIT_READ_UNAVAILABLE") from None
    if git_result.returncode:
        raise BotError("EXISTING_GIT_READ_FAILED")
    print("OK: Git read through existing credential helper.", flush=True)
    probe = "https://{}/api/v4/projects/{}".format(host, quote(cfg["projects"][0], safe=""))
    headers = None
    for candidate in ({"PRIVATE-TOKEN": secret}, {"Authorization": "Bearer " + secret}):
        try:
            request_json(probe, headers=candidate)
            headers = candidate
            break
        except BotError as failure:
            if str(failure) != "HTTP_STATUS_401":
                raise
    if headers is None:
        raise BotError("GIT_WORKS_BUT_API_REJECTS_EXISTING_CREDENTIAL_401")
    check = runpy.run_path(str(root / "check-gitlab.py"))["check"]
    check(cfg, secret, api_headers=headers)
    print("PASS: existing Git credential supports API and Git read. Not copied or saved.")


if __name__ == "__main__":
    try:
        main()
    except Exception as failure:
        code = str(failure) if isinstance(failure, BotError) else "EXISTING_CREDENTIAL_CHECK_FAILED"
        print("ERROR: " + code)
        raise SystemExit(1)
