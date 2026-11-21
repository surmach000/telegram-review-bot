"""Versioned, explicit working context. Never crawls chats, credentials or arbitrary folders."""
import hashlib
import json
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .core import BotError


SECRET = re.compile(r"(?:glpat-|glft-)[A-Za-z0-9_-]{12,}|\b[0-9]{7,12}:[A-Za-z0-9_-]{30,}\b")


def verify_snapshot(folder):
    if folder.is_symlink():
        raise BotError("REVIEW_CONTEXT_UNSAFE_PATH")
    try:
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        files = manifest["files"]
        if not files:
            raise BotError("REVIEW_CONTEXT_MANIFEST_EMPTY")
        digest = hashlib.sha256()
        for entry in files:
            name = entry["file"]
            if Path(name).name != name or "/" in name or "\\" in name:
                raise BotError("REVIEW_CONTEXT_UNSAFE_PATH")
            path = folder / name
            if path.is_symlink():
                raise BotError("REVIEW_CONTEXT_UNSAFE_PATH")
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != entry["sha256"]:
                raise BotError("REVIEW_CONTEXT_SNAPSHOT_CHANGED")
            digest.update(name.encode("utf-8") + b"\0" + raw)
        if manifest["digest"] != digest.hexdigest():
            raise BotError("REVIEW_CONTEXT_SNAPSHOT_CHANGED")
        return manifest
    except (OSError, ValueError, KeyError, TypeError):
        raise BotError("REVIEW_CONTEXT_SNAPSHOT_INVALID") from None


def snapshot_context(cfg, folder):
    folder = Path(folder)
    if folder.is_symlink():
        raise BotError("REVIEW_CONTEXT_UNSAFE_PATH")
    if folder.exists():
        return verify_snapshot(folder)
    sources = cfg.get("review_context_sources", [])
    if not sources:
        if cfg.get("review_context_required", False):
            raise BotError("REVIEW_CONTEXT_REQUIRED")
        return None
    staging = Path(tempfile.mkdtemp(prefix="review-context-", dir=str(folder.parent)))
    try:
        manifest = {"schema_version": 1, "captured_at": datetime.now(timezone.utc).isoformat(), "files": []}
        digest = hashlib.sha256()
        for index, source in enumerate(sources):
            path = Path(source)
            if path.suffix.lower() not in (".md", ".txt") or not path.is_file():
                raise BotError("REVIEW_CONTEXT_SOURCE_UNAVAILABLE")
            if path.stat().st_size > 256 * 1024:
                raise BotError("REVIEW_CONTEXT_SOURCE_TOO_LARGE")
            raw = path.read_bytes()
            text = raw.decode("utf-8-sig")
            if SECRET.search(text):
                raise BotError("REVIEW_CONTEXT_POSSIBLE_CREDENTIAL")
            name = "{:02d}-{}.txt".format(index + 1, path.stem)
            (staging / name).write_bytes(raw)
            manifest["files"].append({"file": name, "source": str(path.resolve()),
                                      "sha256": hashlib.sha256(raw).hexdigest()})
            digest.update(name.encode("utf-8") + b"\0" + raw)
        manifest["digest"] = digest.hexdigest()
        (staging / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        staging.replace(folder)
        return manifest
    except (OSError, UnicodeError):
        raise BotError("REVIEW_CONTEXT_SOURCE_READ_FAILED") from None
    finally:
        if staging.exists():
            if staging.is_symlink() or staging.resolve().parent != folder.parent.resolve():
                raise BotError("REVIEW_CONTEXT_UNSAFE_PATH")
            shutil.rmtree(str(staging))


def copy_previous_context(source, destination):
    source, destination = Path(source), Path(destination)
    if not source.exists():
        return
    manifest = verify_snapshot(source)
    if destination.exists():
        verify_snapshot(destination)
        return
    staging = Path(tempfile.mkdtemp(prefix="previous-review-context-", dir=str(destination.parent)))
    try:
        for entry in manifest["files"]:
            (staging / entry["file"]).write_bytes((source / entry["file"]).read_bytes())
        (staging / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        staging.replace(destination)
    finally:
        if staging.exists():
            if staging.is_symlink() or staging.resolve().parent != destination.parent.resolve():
                raise BotError("REVIEW_CONTEXT_UNSAFE_PATH")
            shutil.rmtree(str(staging))
