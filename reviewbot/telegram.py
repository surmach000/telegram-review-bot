import json
import socket
import ssl
import time
import urllib.error
import urllib.request

from .core import BotError, RateLimited


def retry_seconds(payload):
    parameters = payload.get("parameters", {}) if isinstance(payload, dict) else {}
    seconds = parameters.get("retry_after", 300) if isinstance(parameters, dict) else 300
    return seconds if type(seconds) is int and seconds > 0 else 300


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
        if failure.code == 429:
            try:
                payload = json.loads(failure.read(4096))
                raise RateLimited(retry_seconds(payload)) from None
            except (ValueError, OSError):
                pass
        raise BotError("HTTP_STATUS_{}".format(failure.code)) from None
    except urllib.error.URLError as failure:
        reason = failure.reason
        if isinstance(reason, socket.gaierror):
            code = "HTTP_DNS_FAILED"
        elif isinstance(reason, ssl.SSLCertVerificationError):
            code = "HTTP_TLS_CERTIFICATE_UNTRUSTED"
        elif isinstance(reason, (TimeoutError, socket.timeout)):
            code = "HTTP_TIMEOUT"
        elif isinstance(reason, ConnectionRefusedError):
            code = "HTTP_CONNECT_FAILED"
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
            if result.get("error_code") == 429:
                raise RateLimited(retry_seconds(result))
            if type(result.get("error_code")) is int:
                raise BotError("HTTP_STATUS_{}".format(result["error_code"]))
            raise BotError("TELEGRAM_REQUEST_REJECTED")
        return result["result"]

    def updates(self, offset):
        return self.call("getUpdates", {"offset": offset, "limit": 100, "timeout": 25,
                                        "allowed_updates": ["message"]})

    def deliver_one(self, store):
        row = store.next_delivery()
        if not row:
            return False
        with store.db:
            store.db.execute("UPDATE outbox SET status='sending' WHERE id=?", (row["id"],))
        try:
            sent = self.call("sendMessage", {"chat_id": row["chat_id"], "text": row["body"],
                "reply_parameters": {"message_id": row["reply_id"], "allow_sending_without_reply": True},
                "link_preview_options": {"is_disabled": True}})
        except BotError as failure:
            with store.db:
                code = str(failure)
                if code in ("HTTP_DNS_FAILED", "HTTP_CONNECT_FAILED", "HTTP_STATUS_429"):
                    attempt = row["retry_count"] + 1
                    delay = failure.seconds if isinstance(failure, RateLimited) else (30, 60, 120, 300)[min(attempt - 1, 3)]
                    store.db.execute("""UPDATE outbox SET status='pending',retry_count=?,next_attempt=?,last_error=? WHERE id=?""",
                                     (attempt, time.time() + delay, code, row["id"]))
                else:
                    status = "failed" if code in ("HTTP_STATUS_400", "HTTP_STATUS_401", "HTTP_STATUS_403", "HTTP_TLS_CERTIFICATE_UNTRUSTED") else "unknown"
                    store.db.execute("UPDATE outbox SET status=?,last_error=? WHERE id=?", (status, code, row["id"]))
            return False
        with store.db:
            store.db.execute("UPDATE outbox SET status='sent',telegram_id=?,next_attempt=0,last_error=NULL WHERE id=?",
                             (sent["message_id"], row["id"]))
        return True
