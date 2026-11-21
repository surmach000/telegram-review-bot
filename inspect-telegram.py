"""Read-only bot identity/update diagnostics; never print token or message text."""
import json
import os

from reviewbot.core import BotError
from reviewbot.telegram import Telegram


try:
    telegram = Telegram(os.environ.get("REVIEW_TELEGRAM_TOKEN", ""))
    if not telegram.token:
        raise BotError("BOT_TOKEN_REQUIRED")
    me = telegram.call("getMe", {})
    hook = telegram.call("getWebhookInfo", {})
    print(json.dumps({"bot_username": me.get("username"), "webhook_configured": bool(hook.get("url")),
                      "pending_update_count": hook.get("pending_update_count")}, ensure_ascii=False), flush=True)
    if not hook.get("url"):
        updates = telegram.call("getUpdates", {"timeout": 5, "limit": 100, "allowed_updates": ["message"]})
        ids = sorted({u["message"]["from"]["id"] for u in updates
                      if u.get("message", {}).get("chat", {}).get("type") == "private"})
        print(json.dumps({"private_user_ids": ids, "update_count": len(updates)}), flush=True)
except BotError as failure:
    print("ERROR: " + str(failure))
    raise SystemExit(1)
