"""Prepare a credential-free copy of Maven artifacts, not Maven configuration."""
import json
from pathlib import Path

from reviewbot.test_runner import prepare_cache


root = Path(__file__).resolve().parent
path = root / "config.local.json"
cfg = json.loads((path if path.exists() else root / "config.example.json").read_text(encoding="utf-8-sig"))
destination = Path(cfg.get("test_artifact_cache", "runtime/maven-artifacts"))
if not destination.is_absolute():
    destination = root / destination
count = prepare_cache(Path.home() / ".m2" / "repository", destination)
print("Prepared artifact-only Maven cache: {} files".format(count))
