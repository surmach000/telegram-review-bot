"""A local maintenance guard; prints counts only, never messages or credentials."""
import sqlite3
from pathlib import Path


root = Path(__file__).resolve().parent
database = sqlite3.connect(str(root / "runtime" / "queue.sqlite3"))
running = database.execute("SELECT COUNT(*) FROM jobs WHERE status='running'").fetchone()[0]
sending = database.execute("SELECT COUNT(*) FROM outbox WHERE status='sending'").fetchone()[0]
database.close()
print("Active reviews: {}; active deliveries: {}".format(running, sending))
raise SystemExit(10 if running or sending else 0)
