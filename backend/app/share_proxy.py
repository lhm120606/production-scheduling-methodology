from __future__ import annotations

import argparse
import http.client
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


UPSTREAM_HOST = "127.0.0.1"
UPSTREAM_PORT = 8000
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}


class ReadOnlyShareHandler(BaseHTTPRequestHandler):
    server_version = "GuogangReadOnlyShare/1.0"

    def do_GET(self) -> None:  # noqa: N802
        if urlsplit(self.path).path == "/api/ai/config":
            self._send_json(
                200,
                {"enabled": False, "apiKey": "", "modelName": "", "baseUrl": ""},
            )
            return
        self._proxy("GET")

    def do_HEAD(self) -> None:  # noqa: N802
        if urlsplit(self.path).path == "/api/ai/config":
            self._send_json(200, {}, include_body=False)
            return
        self._proxy("HEAD")

    def do_POST(self) -> None:  # noqa: N802
        self._reject_write()

    def do_PUT(self) -> None:  # noqa: N802
        self._reject_write()

    def do_PATCH(self) -> None:  # noqa: N802
        self._reject_write()

    def do_DELETE(self) -> None:  # noqa: N802
        self._reject_write()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._reject_write()

    def log_message(self, message: str, *args: object) -> None:
        print(f"{self.address_string()} - {message % args}", flush=True)

    def _reject_write(self) -> None:
        self._send_json(
            403,
            {"detail": "外网分享网址仅供查看，修改操作请在厂内网址完成。"},
        )

    def _send_json(
        self,
        status: int,
        payload: dict[str, object],
        *,
        include_body: bool = True,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Guogang-Share-Mode", "read-only")
        self.end_headers()
        if include_body:
            self.wfile.write(body)

    def _proxy(self, method: str) -> None:
        request_headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in HOP_BY_HOP_HEADERS and key.lower() != "host"
        }
        connection = http.client.HTTPConnection(
            UPSTREAM_HOST,
            UPSTREAM_PORT,
            timeout=120,
        )
        try:
            connection.request(method, self.path, headers=request_headers)
            response = connection.getresponse()
            body = response.read()
            self.send_response(response.status, response.reason)
            for key, value in response.getheaders():
                if key.lower() not in HOP_BY_HOP_HEADERS and key.lower() != "content-length":
                    self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Guogang-Share-Mode", "read-only")
            self.end_headers()
            if method != "HEAD":
                self.wfile.write(body)
        except (ConnectionError, TimeoutError, http.client.HTTPException):
            self._send_json(502, {"detail": "排产系统暂时不可用，请稍后刷新。"})
        finally:
            connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="国刚排产系统外网只读分享代理")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8010)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), ReadOnlyShareHandler)
    print(f"read-only share proxy listening on http://{args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
