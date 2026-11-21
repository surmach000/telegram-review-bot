"""Verify existing Git authentication and download one pinned MR without any API token."""
import json
import tempfile
from pathlib import Path

from reviewbot.core import BotError
from reviewbot.worker import Worker, child_environment, run_command


if __name__ == "__main__":
    try:
        root = Path(__file__).resolve().parent
        cfg = json.loads((root / "config.example.json").read_text(encoding="utf-8"))
        cfg["documentation_directory"] = str(root / cfg["documentation_directory"])
        env = child_environment()
        env.update({"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never"})
        for project in cfg["projects"]:
            remote = "https://{}/{}.git".format(cfg["gitlab_host"], project)
            run_command(["git", "-c", "http.followRedirects=false", "ls-remote", "--exit-code",
                         remote, "refs/heads/" + cfg["base_branch"]], root, env)
            print("OK: {} - Git read and base branch {}".format(project, cfg["base_branch"]), flush=True)
        with tempfile.TemporaryDirectory(prefix="review-git-only-") as temporary:
            cfg["data_directory"] = temporary
            folder = Path(temporary) / "verification"
            folder.mkdir()
            result = Worker(cfg).prepare({"url": "https://{}/{}/-/merge_requests/13".format(
                cfg["gitlab_host"], cfg["projects"][0]), "question": "Git-only verification"}, folder)
            print("OK: MR fetched; base/head snapshots, diff and documentation prepared.")
            print("HEAD: " + result["head_sha"])
            print("BASE: " + result["base_sha"])
        print("PASS: Git-only mode, no API or additional GitLab token.")
    except Exception as failure:
        print("ERROR: " + (str(failure) if isinstance(failure, BotError) else "GIT_ONLY_CHECK_FAILED"))
        raise SystemExit(1)
