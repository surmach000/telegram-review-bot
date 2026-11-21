"""Deterministic OS access probes on disposable files, never real personal file contents."""
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

from reviewbot.permissions import permission_arguments, PROFILE


with tempfile.TemporaryDirectory(prefix="review-access-") as temporary:
    root = Path(temporary)
    job = root / "job"
    job.mkdir()
    inside, outside = job / "inside.txt", root / "outside.txt"
    inside.write_text("INSIDE_CANARY", encoding="utf-8")
    outside.write_text("OUTSIDE_CANARY", encoding="utf-8")
    codex = shutil.which("codex")
    failures = []
    downloads = Path.home() / "Downloads"
    private_canary = downloads / (".review-bot-access-" + uuid.uuid4().hex + ".txt")
    private_canary.write_text("PUBLIC_DISPOSABLE_CANARY", encoding="utf-8")
    cases = [("inside", ["cmd.exe", "/d", "/c", "type", str(inside)] if os.name == "nt" else ["cat", str(inside)], True),
             ("outside", ["cmd.exe", "/d", "/c", "type", str(outside)] if os.name == "nt" else ["cat", str(outside)], False),
             ("downloads_read", ["cmd.exe", "/d", "/c", "type", str(private_canary)] if os.name == "nt" else ["cat", str(private_canary)], False),
             ("downloads_list", ["cmd.exe", "/d", "/c", "dir", "/b", str(downloads)] if os.name == "nt" else ["ls", str(downloads)], False)]
    try:
      for label, command, allowed in cases:
        args = [codex, "sandbox", "-C", str(job), "-P", PROFILE] + permission_arguments() + ["--"] + command
        result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        passed = (result.returncode == 0) == allowed
        print(label + ": " + ("PASS" if passed else "FAIL"), flush=True)
        if not passed:
            failures.append(label)
            print(result.stderr.decode("utf-8", errors="replace")[-2200:], flush=True)
    finally:
        private_canary.unlink()
    if failures:
        raise SystemExit(1)
