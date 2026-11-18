from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from urllib.request import Request
import io
import json
import unittest

from supervise_shop import (
    ALERT_REPEAT_SECONDS, ALERT_RETRY_SECONDS, CHECK_SECONDS, OUTAGE_SECONDS,
    PUBLIC_CHECK_SECONDS, Service, TUNNEL_URL, TunnelProbe,
)


class ServiceTests(unittest.TestCase):
    def test_public_tunnel_health_is_checked_with_ngrok_header_and_cached(self):
        now = [0]
        public_requests = []
        public_healthy = [True]

        def response(payload):
            stream = io.BytesIO(json.dumps(payload).encode())
            stream.status = 200
            return stream

        def open_url(request, timeout):
            if isinstance(request, Request):
                public_requests.append(request)
                return response({"ok": public_healthy[0]})
            return response({"tunnels": [{"public_url": TUNNEL_URL}]})

        probe = TunnelProbe(opener=open_url, clock=lambda: now[0])
        self.assertEqual(probe(), (True, True))
        self.assertEqual(public_requests[0].get_header("Ngrok-skip-browser-warning"), "true")
        now[0] = PUBLIC_CHECK_SECONDS - 1
        self.assertEqual(probe(), (True, True))
        self.assertEqual(len(public_requests), 1)
        now[0] = PUBLIC_CHECK_SECONDS
        public_healthy[0] = False
        self.assertEqual(probe(), (False, True))
        self.assertEqual(len(public_requests), 2)

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.log_path = Path(self.temp.name) / "service.log"

    def test_healthy_existing_service_is_not_started_again(self):
        existing = Mock()
        service = Service("Bot", ["python", "bot.py"], self.log_path,
                          lambda: (True, False), existing)
        with patch("supervise_shop.subprocess.Popen") as launch:
            service.check(0)
        launch.assert_not_called()
        existing.assert_not_called()

    def test_different_ngrok_tunnel_does_not_start_a_duplicate(self):
        existing = Mock()
        service = Service("ngrok", ["ngrok", "http", "8080"], self.log_path,
                          lambda: (False, True), existing)
        with patch("supervise_shop.subprocess.Popen") as launch:
            service.check(0)
        launch.assert_not_called()
        existing.assert_not_called()

    def test_external_process_is_adopted_until_it_exits(self):
        running = Mock(side_effect=[True, False])
        service = Service("Bot", ["python", "bot.py"], self.log_path,
                          lambda: (False, False), running)
        child = Mock(pid=123)
        with patch("supervise_shop.subprocess.Popen", return_value=child) as launch:
            service.check(0)
            launch.assert_not_called()
            service.check(CHECK_SECONDS)
            launch.assert_called_once()
        service.log_file.close()

    def test_exited_child_restarts_after_backoff(self):
        first = Mock(pid=123, returncode=1)
        first.poll.return_value = None
        second = Mock(pid=456)
        service = Service("Bot", ["python", "bot.py"], self.log_path,
                          lambda: (False, False), lambda: False)
        with patch("supervise_shop.subprocess.Popen", side_effect=[first, second]) as launch:
            service.check(0)
            service.check(1)
            self.assertEqual(launch.call_count, 1)
            first.poll.return_value = 1
            service.check(2)
            service.check(2 + CHECK_SECONDS - 1)
            self.assertEqual(launch.call_count, 1)
            service.check(2 + CHECK_SECONDS)
            self.assertEqual(launch.call_count, 2)
        service.log_file.close()

    def test_long_outage_alerts_then_reports_recovery(self):
        status = {"healthy": False}
        send = Mock(return_value=True)
        service = Service("ngrok", ["ngrok"], self.log_path,
                          lambda: (status["healthy"], False), lambda: True, send)

        service.check(0)
        service.check(OUTAGE_SECONDS - 1)
        send.assert_not_called()
        service.check(OUTAGE_SECONDS)
        self.assertEqual(send.call_count, 1)
        self.assertIn("mat ket noi", send.call_args.args[0])
        service.check(OUTAGE_SECONDS + ALERT_REPEAT_SECONDS - 1)
        self.assertEqual(send.call_count, 1)
        service.check(OUTAGE_SECONDS + ALERT_REPEAT_SECONDS)
        self.assertEqual(send.call_count, 2)

        status["healthy"] = True
        service.check(OUTAGE_SECONDS + ALERT_REPEAT_SECONDS + 1)
        self.assertEqual(send.call_count, 3)
        self.assertIn("hoat dong lai", send.call_args.args[0])
        service.check(OUTAGE_SECONDS + ALERT_REPEAT_SECONDS + 2)
        self.assertEqual(send.call_count, 3)

    def test_failed_alert_retries_after_delay_and_resets_for_new_outage(self):
        status = {"healthy": False}
        send = Mock(side_effect=[False, True, True, True])
        service = Service("Bot", ["python"], self.log_path,
                          lambda: (status["healthy"], False), lambda: True, send)

        service.check(0)
        service.check(OUTAGE_SECONDS)
        service.check(OUTAGE_SECONDS + ALERT_RETRY_SECONDS - 1)
        self.assertEqual(send.call_count, 1)
        service.check(OUTAGE_SECONDS + ALERT_RETRY_SECONDS)
        self.assertEqual(send.call_count, 2)
        status["healthy"] = True
        service.check(OUTAGE_SECONDS + ALERT_RETRY_SECONDS + 1)
        self.assertEqual(send.call_count, 3)
        status["healthy"] = False
        service.check(OUTAGE_SECONDS + ALERT_RETRY_SECONDS + 2)
        service.check(2 * OUTAGE_SECONDS + ALERT_RETRY_SECONDS + 2)
        self.assertEqual(send.call_count, 4)


if __name__ == "__main__":
    unittest.main()
