import unittest

from app.services.push_notifications import build_multicast_message


class PushNotificationMessageTests(unittest.TestCase):
    def test_platform_branding_and_rich_media_are_applied(self):
        message = build_multicast_message(
            title="A fresh look",
            body="Open StyleStack",
            data={"destination": "today"},
            tokens=["device-token"],
            image_url="https://example.com/look.jpg",
        )

        self.assertEqual(message.android.notification.icon, "ic_stat_stylestack")
        self.assertEqual(message.android.notification.color, "#006B6B")
        self.assertEqual(
            message.android.notification.image, "https://example.com/look.jpg"
        )
        self.assertTrue(message.apns.payload.aps.mutable_content)
        self.assertEqual(
            message.apns.fcm_options.image, "https://example.com/look.jpg"
        )

    def test_text_only_message_does_not_request_ios_media_processing(self):
        message = build_multicast_message(
            title="Morning edit",
            body="Your look is ready",
            data={"destination": "today"},
            tokens=["device-token"],
        )

        self.assertFalse(message.apns.payload.aps.mutable_content)
        self.assertIsNone(message.apns.fcm_options)


if __name__ == "__main__":
    unittest.main()
