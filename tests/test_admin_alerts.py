from unittest.mock import patch
import json
import unittest

import admin_alerts


class AdminAlertTests(unittest.TestCase):
    def test_sends_each_admin_a_message_and_hides_token_from_text(self):
        with patch.object(admin_alerts, "dotenv_values", return_value={}), patch.dict(
            admin_alerts.os.environ, {"BOT_TOKEN": "123:test-token", "ADMIN_IDS": "1,2"}
        ), patch.object(admin_alerts.http.client, "HTTPSConnection") as connection_class:
            connection = connection_class.return_value
            connection.getresponse.return_value.status = 200
            delivered = admin_alerts.send_admin_message("Bot is offline")

        self.assertTrue(delivered)
        self.assertEqual(connection.request.call_count, 2)
        sent = [json.loads(call.args[2]) for call in connection.request.call_args_list]
        self.assertEqual([item["chat_id"] for item in sent], [1, 2])
        self.assertTrue(all("123:test-token" not in item["text"] for item in sent))

    def test_failed_delivery_does_not_log_bot_token(self):
        with patch.object(admin_alerts, "dotenv_values", return_value={}), patch.dict(
            admin_alerts.os.environ, {"BOT_TOKEN": "123:test-token", "ADMIN_IDS": "1"}
        ), patch.object(admin_alerts.http.client, "HTTPSConnection") as connection_class:
            connection_class.return_value.request.side_effect = RuntimeError("123:test-token")
            with self.assertLogs("admin_alerts", level="WARNING") as captured:
                delivered = admin_alerts.send_admin_message("Bot is offline")
        self.assertFalse(delivered)
        self.assertNotIn("123:test-token", "\n".join(captured.output))


if __name__ == "__main__":
    unittest.main()
