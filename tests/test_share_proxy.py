from __future__ import annotations

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from backend.app.share_proxy import ReadOnlyShareHandler


class ShareProxyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), ReadOnlyShareHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def test_ai_config_never_exposes_saved_key(self) -> None:
        with urlopen(f"{self.base_url}/api/ai/config") as response:
            payload = json.load(response)
        self.assertEqual("", payload["apiKey"])
        self.assertFalse(payload["enabled"])

    def test_write_requests_are_blocked(self) -> None:
        request = Request(
            f"{self.base_url}/api/schedule/run",
            data=b"{}",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(HTTPError) as caught:
            urlopen(request)
        self.assertEqual(403, caught.exception.code)
        payload = json.loads(caught.exception.read().decode("utf-8"))
        self.assertIn("仅供查看", payload["detail"])


if __name__ == "__main__":
    unittest.main()
