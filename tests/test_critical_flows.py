import hashlib
import hmac
import json
import os
import unittest
from unittest.mock import patch


# Prevent import-time startup notification side effects in tests.
os.environ["YOUR_PERSONAL_WHATSAPP"] = ""
os.environ["META_APP_SECRET"] = "test_secret"

import app as app_module


class CriticalFlowTests(unittest.TestCase):
    def setUp(self):
        with app_module._state_lock:
            app_module.live_sessions.clear()
            app_module.pending_live_offers.clear()
            app_module.escalation_message_map.clear()
            app_module.pending_approvals.clear()
            app_module.pending_selections.clear()
            app_module.approval_message_map.clear()
            app_module.processing_messages.clear()
            app_module._startup_notified = False

    def test_detect_needs_human_escalates_immediately(self):
        number = "+50711111111"
        message = "ok entiendo"
        called = []

        def _fake_escalate(n, m, reason=""):
            called.append((n, m, reason))

        with patch.object(app_module, "parse_request_multi", return_value=[]), \
             patch.object(app_module, "detect_needs_human", return_value=True), \
             patch.object(app_module, "_handle_human_escalation", side_effect=_fake_escalate), \
             patch.object(app_module, "send_whatsapp", return_value="sid_test"):
            app_module.process_customer_request(number, message)

        self.assertEqual(len(called), 1)
        self.assertEqual(called[0][0], number)
        self.assertEqual(called[0][1], message)

    def test_customer_image_does_not_reannounce_live_mode_if_already_live(self):
        owner_number = "50764794106"
        customer_number = "+50763622248"
        sent_texts = []

        def _fake_send_whatsapp(to, msg):
            sent_texts.append((to, msg))
            return "sid_text"

        message = {
            "from": customer_number.replace("+", ""),
            "type": "image",
            "image": {"id": "media_123", "mime_type": "image/jpeg", "caption": "esto"},
        }

        with app_module._state_lock:
            app_module.live_sessions[customer_number] = True

        with patch.dict(os.environ, {"YOUR_PERSONAL_WHATSAPP": owner_number}, clear=False), \
             patch.object(app_module, "get_store_numbers", return_value=[]), \
             patch("utils.media.download_meta_media", return_value=(b"img", "image/jpeg")), \
             patch("utils.media.upload_meta_media", return_value="new_media"), \
             patch.object(app_module, "send_whatsapp_image", return_value="sid_image"), \
             patch.object(app_module, "send_whatsapp", side_effect=_fake_send_whatsapp):
            app_module._handle_image_relay(message)

        # The image should still be relayed, but no duplicate live-mode texts should be sent.
        self.assertFalse(any("Cliente en modo en vivo" in m for _, m in sent_texts))
        self.assertFalse(any("ya te contacta alguien del equipo" in m for _, m in sent_texts))

    def test_terminar_dead_session_is_silent_noop(self):
        owner_number = "50764794106"
        sent_texts = []

        def _fake_send_whatsapp(to, msg):
            sent_texts.append((to, msg))
            return "sid_text"

        payload = {
            "entry": [{
                "changes": [{
                    "value": {
                        "messages": [{
                            "id": "wamid.test.terminar",
                            "from": owner_number,
                            "type": "text",
                            "text": {"body": "terminar +50799988877"},
                        }]
                    }
                }]
            }]
        }
        raw = json.dumps(payload, separators=(",", ":")).encode()
        sig = "sha256=" + hmac.new(
            os.environ["META_APP_SECRET"].encode(), raw, hashlib.sha256
        ).hexdigest()

        with patch.dict(os.environ, {"YOUR_PERSONAL_WHATSAPP": owner_number}, clear=False), \
             patch.object(app_module, "send_whatsapp", side_effect=_fake_send_whatsapp):
            client = app_module.app.test_client()
            resp = client.post(
                "/webhook",
                data=raw,
                headers={
                    "Content-Type": "application/json",
                    "X-Hub-Signature-256": sig,
                },
            )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(sent_texts, [])

    def test_startup_notification_sends_once(self):
        sent = []

        def _fake_send_whatsapp(to, msg):
            sent.append((to, msg))
            return "sid_startup"

        with patch.dict(os.environ, {"YOUR_PERSONAL_WHATSAPP": "50764794106"}, clear=False), \
             patch.object(app_module, "send_whatsapp", side_effect=_fake_send_whatsapp):
            app_module._send_startup_notification_once()
            app_module._send_startup_notification_once()

        self.assertEqual(len(sent), 1)
        self.assertIn("Zeli Bot Online", sent[0][1])


if __name__ == "__main__":
    unittest.main()
