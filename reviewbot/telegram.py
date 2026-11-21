import json
import socket
import ssl
import urllib.error
import urllib.request

from .core import BotError


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise BotError("HTTP_REDIRECT_REFUSED")


def request_json(url, data=None, headers=None, timeout=45):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.build_opener(SafeRedirect()).open(req, timeout=timeout) as response:
            payload = response.read(2 * 1024 * 1024 + 1)
            if len(payload) > 2 * 1024 * 1024:
                raise BotError("HTTP_RESPONSE_TOO_LARGE")
            try:
                return json.loads(payload)
            except (ValueError, UnicodeError):
                content_type = "HTML" if response.headers.get_content_type() == "text/html" else "OTHER"
                raise BotError("HTTP_RESPONSE_NOT_JSON_STATUS_{}_{}".format(response.status, content_type)) from None
    except urllib.error.HTTPError as failure:
        raise BotError("HTTP_STATUS_{}".format(failure.code)) from None
    except urllib.error.URLError as failure:
        reason = failure.reason
        if isinstance(reason, socket.gaierror):
            code = "HTTP_DNS_FAILED"
        elif isinstance(reason, ssl.SSLCertVerificationError):
            code = "HTTP_TLS_CERTIFICATE_UNTRUSTED"
        elif isinstance(reason, (TimeoutError, socket.timeout)):
            code = "HTTP_TIMEOUT"
        else:
            code = "HTTP_CONNECTION_FAILED"
        raise BotError(code) from None
    except ValueError:
        raise BotError("HTTP_REQUEST_INVALID_VALUE_OR_HEADER") from None
    except OSError:
        # urllib exception messages may contain token-bearing request URLs.
        raise BotError("HTTP_REQUEST_FAILED") from None


class Telegram:
    def __init__(self, token):
        self.token = token

    def call(self, method, payload):
        result = request_json("https://api.telegram.org/bot" + self.token + "/" + method,
                              json.dumps(payload).encode("utf-8"), {"Content-Type": "application/json"})
        if not result.get("ok"):
            raise BotError("TELEGRAM_REQUEST_REJECTED")
        return result["result"]

    def updates(self, offset):
        return self.call("getUpdates", {"offset": offset, "limit": 100, "timeout": 25,
                                        "allowed_updates": ["message"]})

    def deliver_one(self, store):
        row = store.db.execute("SELECT * FROM outbox WHERE status!='sent' ORDER BY id LIMIT 1").fetchone()
        if not row or row["status"] != "pending":
            return False
        with store.db:
            store.db.execute("UPDATE outbox SET status='sending' WHERE id=?", (row["id"],))
        try:
            sent = self.call("sendMessage", {"chat_id": row["chat_id"], "text": row["body"],
                "reply_parameters": {"message_id": row["reply_id"], "allow_sending_without_reply": True},
                "link_preview_options": {"is_disabled": True}})
        except BotError:
            with store.db:
                store.db.execute("UPDATE outbox SET status='unknown' WHERE id=?", (row["id"],))
            return False
        with store.db:
            store.db.execute("UPDATE outbox SET status='sent',telegram_id=? WHERE id=?",
                             (sent["message_id"], row["id"]))
        return True
