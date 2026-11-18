from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import hmac
import json
import logging
import socket
import threading
import time

from config import Config
from payments import process_sepay_payload
from store import Store


log = logging.getLogger(__name__)


class PaymentHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32
    request_timeout = 5
    max_active_requests = 32

    def __init__(self, config: Config, store: Store, notify, on_error=None):
        super().__init__((config.webhook_host, config.webhook_port), PaymentHandler)
        self.config = config
        self.store = store
        self.notify = notify
        self.on_error = on_error
        self.active_requests = threading.BoundedSemaphore(self.max_active_requests)

    def report_error(self, category: str):
        if self.on_error is None:
            return
        try:
            self.on_error(category)
        except Exception:
            log.exception("Could not schedule webhook error alert")

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(self.request_timeout)
        return request, address

    def verify_request(self, request, client_address):
        if self.active_requests.acquire(blocking=False):
            return True
        try:
            request.settimeout(0.1)
            request.sendall(
                b"HTTP/1.1 503 Service Unavailable\r\n"
                b"Retry-After: 5\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            )
        except OSError:
            pass
        self.report_error("overload")
        return False

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.active_requests.release()


class PaymentHandler(BaseHTTPRequestHandler):
    server: PaymentHTTPServer

    def _respond(self, status: int, body: dict):
        if self.path == "/webhooks/sepay" and status >= 400:
            self.server.report_error("processing" if status >= 500 else "rejected")
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/healthz":
            self._respond(200, {"ok": True})
        else:
            self._respond(404, {"success": False})

    def do_POST(self):
        if self.path != "/webhooks/sepay":
            self._respond(404, {"success": False})
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            size = 0
        if not 0 < size <= 16_384:
            self._respond(413, {"success": False})
            return
        timestamp = self.headers.get("X-SePay-Timestamp", "")
        signature = self.headers.get("X-SePay-Signature", "")
        if not (9 <= len(timestamp) <= 11 and timestamp.isascii() and timestamp.isdigit()):
            self._respond(401, {"success": False})
            return
        if abs(time.time() - int(timestamp)) > 300:
            self._respond(401, {"success": False})
            return
        if len(signature) != 71 or not signature.startswith("sha256="):
            self._respond(401, {"success": False})
            return
        try:
            raw_body = self.rfile.read(size)
        except (socket.timeout, OSError):
            try:
                self._respond(408, {"success": False})
            except OSError:
                pass
            return
        if len(raw_body) != size:
            self._respond(400, {"success": False})
            return
        signing_input = timestamp.encode("ascii") + b"." + raw_body
        expected_signature = "sha256=" + hmac.new(
            self.server.config.sepay_webhook_secret.encode("utf-8"),
            signing_input, hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(signature, expected_signature):
            self._respond(401, {"success": False})
            return
        try:
            payload = json.loads(raw_body)
            result = process_sepay_payload(
                self.server.store, payload, self.server.config.bank_account_number,
                via_webhook=True,
                notify_admin_ids=getattr(self.server.config, "admin_ids", frozenset()),
            )
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            self._respond(400, {"success": False})
            return
        except Exception:
            log.exception("Could not process SePay webhook")
            self._respond(500, {"success": False})
            return

        if result["status"] in {"credited", "unmatched"}:
            try:
                self.server.notify(result)
            except Exception:
                log.exception("Could not schedule payment notification")
                self.server.report_error("notification")
        self._respond(200, {"success": True})

    def log_message(self, format_string, *args):
        log.debug("Webhook HTTP: " + format_string, *args)


def start_webhook_server(config: Config, store: Store, notify, on_error=None):
    server = PaymentHTTPServer(config, store, notify, on_error)
    thread = threading.Thread(target=server.serve_forever, name="payment-webhook", daemon=True)
    thread.start()
    return server, thread
