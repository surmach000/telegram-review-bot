"""Credential-free, fixed-origin Maven Central GET/HEAD proxy; runs in its own container."""
import http.server
import re
import urllib.error
import urllib.request
from urllib.parse import urlsplit


def allowed_path(path):
    return (path.startswith("/maven2/") and not urlsplit(path).query
            and re.fullmatch(r"/[A-Za-z0-9_./+\-]+", path) is not None
            and ".." not in path and "//" not in path
            and path.endswith((".pom", ".jar", ".xml", ".sha1", ".sha256", ".sha512", ".md5", ".asc")))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.forward(False)

    def do_HEAD(self):
        self.forward(True)

    def log_message(self, *args):
        pass

    def forward(self, head):
        if not allowed_path(self.path):
            self.send_error(403)
            return
        request = urllib.request.Request("https://repo.maven.apache.org" + self.path,
                                         method="HEAD" if head else "GET")
        try:
            opener = urllib.request.build_opener(NoRedirect(), urllib.request.ProxyHandler({}))
            with opener.open(request, timeout=30) as response:
                self.send_response(200)
                if response.headers.get("Content-Length"):
                    self.send_header("Content-Length", response.headers["Content-Length"])
                self.end_headers()
                if not head:
                    while True:
                        block = response.read(64 * 1024)
                        if not block:
                            break
                        self.wfile.write(block)
        except urllib.error.HTTPError as failure:
            self.send_error(failure.code)
        except (OSError, ValueError):
            self.send_error(502)


if __name__ == "__main__":
    http.server.ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
