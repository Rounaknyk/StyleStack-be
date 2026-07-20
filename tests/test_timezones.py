import unittest

from app.services.timezones import normalize_timezone_name, resolve_timezone


class TimezoneTests(unittest.TestCase):
    def test_device_ist_maps_to_india_iana_timezone(self) -> None:
        self.assertEqual(normalize_timezone_name("IST"), "Asia/Kolkata")
        self.assertEqual(resolve_timezone("IST").key, "Asia/Kolkata")

    def test_valid_iana_timezone_is_preserved(self) -> None:
        self.assertEqual(normalize_timezone_name("Asia/Kolkata"), "Asia/Kolkata")

    def test_invalid_timezone_falls_back_to_utc(self) -> None:
        self.assertEqual(normalize_timezone_name("not/a-zone"), "UTC")


if __name__ == "__main__":
    unittest.main()
