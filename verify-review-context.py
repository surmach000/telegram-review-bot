"""Check actual model access to approved working context; no Git, tests or Telegram."""
import json
import tempfile
from pathlib import Path

from reviewbot.core import load_config
from reviewbot.review_context import snapshot_context
from reviewbot.worker import Worker


root = Path(__file__).resolve().parent
cfg = load_config(root / "config.local.json")
with tempfile.TemporaryDirectory(prefix="review-context-probe-") as temporary:
    folder = Path(temporary)
    snapshot = snapshot_context(cfg, folder / "review-context")
    print("Working-context sources verified: " + str(len(snapshot["files"])), flush=True)
    answer = Worker(cfg).codex(folder,
        "Read the approved documents in review-context/. After the required read-proof line return only JSON: "
        "routeMoment (short Russian text: when the recipient is pinned for an already discovered file), "
        "keyStatus (check-config status when private access remains unconfirmed), "
        "testBlocks (three ordered English Given/When/Then block names required by the personal methodology). "
        "Read only, no network, no code execution, no other folders.", "high")
    result = json.loads(answer)
    blocks = [str(x).lower() for x in result.get("testBlocks", [])]
    passed = ("обнаруж" in result.get("routeMoment", "").lower()
              and result.get("keyStatus", "").upper() == "PARTIAL"
              and blocks == ["given", "when", "then"])
    print("Live 6.1 Sol/high applied chat decisions and test methodology: " + ("PASS" if passed else "UNEXPECTED_RESULT"))
    if not passed:
        raise SystemExit(1)
