"""HTTP plumbing: static files from ./static and JSON routing to the Api.

Standard library only (http.server). Logs never include client addresses or
query strings, because the query string carries plates and share codes.
"""

import json
import mimetypes
import os
import posixpath
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .api import Request

MAX_BODY = 64 * 1024
STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")

EXTRA_TYPES = {
    ".webmanifest": "application/manifest+json",
    ".js": "text/javascript",
    ".css": "text/css",
    ".svg": "image/svg+xml",
    ".html": "text/html",
    ".png": "image/png",
    ".json": "application/json",
}

SECURITY_HEADERS = (
    ("Content-Security-Policy",
     "default-src 'self'; frame-src https://www.openstreetmap.org; img-src 'self' data:; "
     "connect-src 'self'; style-src 'self'; script-src 'self'; base-uri 'self'; "
     "form-action 'self'; frame-ancestors 'none'"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("Permissions-Policy", "geolocation=(self), camera=(), microphone=()"),
    ("Cache-Control", "no-cache"),
)


class _TooLarge(Exception):
    pass


def _reject_constant(name):
    raise ValueError("%s is not allowed in JSON" % name)


def make_handler(api, static_dir=STATIC_DIR):
    static_root = os.path.normpath(static_dir)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "AsaSakyan"
        sys_version = ""

        # -- entry points ----------------------------------------------------

        def do_GET(self):
            self._dispatch("GET")

        def do_HEAD(self):
            self._dispatch("HEAD")

        def do_POST(self):
            self._dispatch("POST")

        def do_PATCH(self):
            self._dispatch("PATCH")

        def do_DELETE(self):
            self._dispatch("DELETE")

        def do_PUT(self):
            self._dispatch("PUT")

        def do_OPTIONS(self):
            self._dispatch("OPTIONS")

        def _dispatch(self, method):
            url = urlsplit(self.path)
            if url.path == "/api" or url.path.startswith("/api/"):
                self._api(method, url)
            elif method in ("GET", "HEAD"):
                self._static(url.path, head=(method == "HEAD"))
            else:
                self._json(405, {"error": "method_not_allowed", "message": "Only GET is allowed here."})

        # -- API -------------------------------------------------------------

        def _api(self, method, url):
            try:
                body = self._body()
            except _TooLarge:
                self.close_connection = True
                return self._json(413, {"error": "too_large", "message": "The request body is too large."})
            except (ValueError, UnicodeDecodeError):
                self.close_connection = True
                return self._json(400, {"error": "bad_json", "message": "The request body must be a JSON object."})
            query = {k: v[-1] for k, v in parse_qs(url.query, keep_blank_values=True).items()}
            auth = self.headers.get("Authorization", "")
            token = auth[7:].strip() if auth.startswith("Bearer ") else None
            request = Request(method, url.path, query, body, token, self._client())
            status, payload = api.handle(request)
            self._json(status, payload, head=(method == "HEAD"))

        def _client(self):
            forwarded = self.headers.get("X-Forwarded-For")
            if forwarded:
                return forwarded.split(",")[0].strip()
            return self.client_address[0]

        def _body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length < 0:
                raise ValueError("negative length")
            if length > MAX_BODY:
                raise _TooLarge()
            raw = self.rfile.read(length) if length else b""
            if not raw.strip():
                return {}
            data = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
            if not isinstance(data, dict):
                raise ValueError("not an object")
            return data

        # -- static files ----------------------------------------------------

        def _static(self, path, head=False):
            if path == "/":
                path = "/index.html"
            rel = posixpath.normpath(posixpath.join("/", path)).lstrip("/")
            full = os.path.normpath(os.path.join(static_root, *rel.split("/"))) if rel else ""
            inside = full.startswith(static_root + os.sep)
            if not rel or not inside or not os.path.isfile(full):
                return self._json(404, {"error": "not_found", "message": "Nothing here."})
            ext = os.path.splitext(full)[1].lower()
            ctype = EXTRA_TYPES.get(ext) or mimetypes.guess_type(full)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/manifest+json", "image/svg+xml", "application/json"):
                ctype += "; charset=utf-8"
            with open(full, "rb") as handle:
                data = handle.read()
            self._send(200, ctype, data, head)

        # -- responses -------------------------------------------------------

        def _json(self, status, payload, head=False):
            data = json.dumps(payload).encode("utf-8")
            self._send(status, "application/json; charset=utf-8", data, head)

        def _send(self, status, ctype, data, head=False):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            for name, value in SECURITY_HEADERS:
                self.send_header(name, value)
            self.end_headers()
            if not head:
                self.wfile.write(data)

        # -- logging (no addresses, no query strings) ------------------------

        def log_request(self, code="-", size="-"):
            path = urlsplit(self.path).path
            sys.stderr.write("[%s] %s %s %s\n" % (self.log_date_time_string(), self.command, path, code))

        def log_message(self, fmt, *args):
            sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    return Handler


def create_server(host, port, api, static_dir=STATIC_DIR):
    return ThreadingHTTPServer((host, port), make_handler(api, static_dir))
