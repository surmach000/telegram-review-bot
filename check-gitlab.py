"""Read-only capability probe. Credentials never enter arguments or diagnostics."""
import json
import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote

from reviewbot.core import BotError
from reviewbot.telegram import request_json
from reviewbot.worker import child_environment, run_command


def check(config, token, api_headers=None):
    if not token:
        raise BotError("GITLAB_TOKEN_REQUIRED")
    if not token.isascii() or any(char.isspace() for char in token):
        raise BotError("TOKEN_INVALID_FORMAT_USE_TOKEN_VALUE_WITHOUT_SPACES")
    for project in config["projects"]:
        base = "https://{}/api/v4/projects/{}".format(config["gitlab_host"], quote(project, safe=""))
        headers = api_headers or {"PRIVATE-TOKEN": token}
        metadata = request_json(base, headers=headers)
        if metadata.get("path_with_namespace") != project:
            raise BotError("GITLAB_UNEXPECTED_PROJECT")
        requests = request_json(base + "/merge_requests?per_page=1", headers=headers)
        if not isinstance(requests, list):
            raise BotError("GITLAB_MR_API_UNAVAILABLE")
        print("OK: {} - project and MR API".format(project), flush=True)
        with tempfile.TemporaryDirectory(prefix="review-gitlab-check-") as temporary:
            helper = Path(temporary) / "askpass.py"
            helper.write_text("import os,sys\nprint('oauth2' if 'Username' in sys.argv[1] else os.environ['REVIEW_GITLAB_TOKEN'])\n", encoding="utf-8")
            env = child_environment()
            env.update({"REVIEW_GITLAB_TOKEN": token, "GIT_TERMINAL_PROMPT": "0",
                        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                        "GIT_ASKPASS": '"{}" "{}"'.format(sys.executable.replace("\\", "/"), str(helper).replace("\\", "/"))})
            run_command(["git", "-c", "credential.helper=", "-c", "http.followRedirects=false",
                         "ls-remote", "--exit-code", "https://{}/{}.git".format(config["gitlab_host"], project),
                         "HEAD"], temporary, env=env)
        print("OK: {} - Git repository read".format(project), flush=True)


if __name__ == "__main__":
    try:
        config = json.loads(Path(__file__).with_name("config.example.json").read_text(encoding="utf-8"))
        check(config, os.environ.get("REVIEW_GITLAB_TOKEN", ""))
        print("PASS: GitLab API and Git access verified for all projects.")
    except Exception as failure:
        code = str(failure) if isinstance(failure, BotError) else "GITLAB_CHECK_FAILED"
        print("ERROR: " + code)
        raise SystemExit(1)
