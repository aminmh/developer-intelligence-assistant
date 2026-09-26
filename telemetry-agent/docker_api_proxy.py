"""Tiny read-only Docker API proxy with exact endpoint allowlisting."""
from __future__ import annotations

from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
from urllib.parse import urlsplit


def allowed(method: str, target: str) -> bool:
    if method != "GET":
        return False
    path = urlsplit(target).path
    if path in {"/_ping", "/version"}:
        return True
    parts = path.strip("/").split("/")
    if len(parts) == 2 and parts[0].startswith("v"):
        return parts[1] == "events"
    if len(parts) == 3 and parts[0].startswith("v"):
        return parts[1:] == ["containers", "json"]
    return False


class UnixHTTPConnection(HTTPConnection):
    def __init__(self, socket_path: str):
        super().__init__("localhost")
        self.socket_path = socket_path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self.socket_path)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "DockerReadProxy/1"

    def do_GET(self):
        self._proxy()

    def do_POST(self):
        self.send_error(403)

    def do_PUT(self):
        self.send_error(403)

    def do_DELETE(self):
        self.send_error(403)

    def do_PATCH(self):
        self.send_error(403)

    def _proxy(self):
        if not allowed(self.command, self.path):
            self.send_error(403)
            return
        upstream = UnixHTTPConnection("/var/run/docker.sock")
        try:
            upstream.request("GET", self.path, headers={"Host": "docker"})
            response = upstream.getresponse()
            self.send_response(response.status, response.reason)
            for name in ("Content-Type", "Content-Length", "Docker-Experimental", "Api-Version"):
                value = response.getheader(name)
                if value:
                    self.send_header(name, value)
            self.send_header("Connection", "close")
            self.end_headers()
            while True:
                block = response.read(65536)
                if not block:
                    break
                self.wfile.write(block)
                self.wfile.flush()
        except (OSError, TimeoutError):
            if not self.wfile.closed:
                self.send_error(502)
        finally:
            upstream.close()

    def log_message(self, fmt, *args):
        # Docker paths can contain filters; keep proxy logs free of request data.
        return


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 2375), Handler).serve_forever()
