"""Restricted read scope for model tools; client authentication stays outside tool permissions."""
import json
import os
from pathlib import Path


PROFILE = "review-job"


def permission_arguments():
    denied = [Path.home() / "Downloads", Path.home() / ".ssh", Path.home() / ".aws",
              Path.home() / ".git-credentials", Path.home() / ".gitconfig",
              Path.home() / ".codex" / "auth.json"]
    filesystem = ['":root"="deny"', '":minimal"="read"', '":tmpdir"="deny"', '":slash_tmp"="deny"',
                  '":workspace_roots"={"."="read"}']
    filesystem += [json.dumps(p.as_posix()) + '="deny"' for p in denied]
    args = ["-c", 'default_permissions="' + PROFILE + '"',
            "-c", "permissions." + PROFILE + ".filesystem={" + ",".join(filesystem) + "}",
            "-c", "permissions." + PROFILE + ".network.enabled=false",
            "-c", "project_doc_max_bytes=0", "-c", 'web_search="disabled"']
    if os.name == "nt":
        args += ["-c", 'windows.sandbox="mxc"']
    return args
