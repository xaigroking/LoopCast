"""Small streaming same-origin fixture proxy for the combined-service browser test."""
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import sys
from urllib.parse import urlsplit

listen_host, listen_port, api_host, api_port, web_host, web_port = sys.argv[1:]
api_paths = ("/v1", "/healthz", "/openapi.json")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self): self.forward()
    def do_HEAD(self): self.forward()
    def do_POST(self): self.forward()
    def do_PUT(self): self.forward()
    def do_PATCH(self): self.forward()

    def forward(self):
        path = urlsplit(self.path).path
        upstream_host, upstream_port = (api_host, int(api_port)) if any(path == prefix or path.startswith(prefix + "/") for prefix in api_paths) else (web_host, int(web_port))
        connection = HTTPConnection(upstream_host, upstream_port, timeout=30)
        headers = {key: value for key, value in self.headers.items() if key.lower() not in {"connection", "keep-alive", "proxy-connection", "transfer-encoding", "upgrade", "host"}}
        headers["Host"] = self.headers.get("Host", f"{listen_host}:{listen_port}")
        headers["X-Forwarded-Host"] = headers["Host"]
        headers["X-Forwarded-Proto"] = "http"
        headers["X-Forwarded-For"] = self.client_address[0]
        body = self.rfile.read(int(self.headers.get("Content-Length", "0"))) if self.command in {"POST", "PUT", "PATCH"} else None
        try:
            connection.request(self.command, self.path, body=body, headers=headers)
            response = connection.getresponse()
            self.send_response(response.status, response.reason)
            for key, value in response.getheaders():
                if key.lower() not in {"connection", "keep-alive", "transfer-encoding", "upgrade", "date", "server"}:
                    self.send_header(key, value)
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                while chunk := response.read1(65536):
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            connection.close()

    def log_message(self, *_args): pass


ThreadingHTTPServer((listen_host, int(listen_port)), Handler).serve_forever()
