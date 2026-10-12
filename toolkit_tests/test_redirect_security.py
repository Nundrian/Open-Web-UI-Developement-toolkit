"""Security regression checks for authenticated Open Terminal HTTP requests."""

import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from development_toolkit import Tools


class _SilentHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass


class RedirectSecurityTests(unittest.TestCase):
    def test_redirect_must_not_forward_api_key_to_different_host(self):
        received_auth = []

        class Recipient(_SilentHandler):
            def do_GET(self):
                received_auth.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok":true}')

        class Redirector(_SilentHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", recipient_url + "/receive")
                self.end_headers()

        receiver = ThreadingHTTPServer(("127.0.0.1", 0), Recipient)
        recipient_url = f"http://127.0.0.1:{receiver.server_port}"
        redirector = ThreadingHTTPServer(("127.0.0.1", 0), Redirector)
        threads = [
            threading.Thread(target=s.serve_forever, daemon=True)
            for s in (receiver, redirector)
        ]
        for thread in threads:
            thread.start()

        try:
            tool = Tools()
            tool.valves.BASE_URL = f"http://127.0.0.1:{redirector.server_port}"
            tool.valves.API_KEY = "test-only-secret"

            with self.assertRaisesRegex(RuntimeError, r"HTTP 302"):
                tool._request_sync("GET", "/api/config")

            self.assertEqual(
                received_auth, [],
                "An authenticated API request followed a redirect to a new origin.",
            )
        finally:
            for server in (redirector, receiver):
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
