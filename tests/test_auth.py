from __future__ import annotations

import inspect
import unittest

from app.telegram.auth_flow import describe_auth_error, normalize_login_code, normalize_phone
from app.telegram import user_client
from telethon.errors import FloodWaitError, PhoneCodeInvalidError, PhoneNumberInvalidError


class PhoneNormalizeTests(unittest.TestCase):
    def test_plus_format(self):
        self.assertEqual(normalize_phone("+7 900 123-45-67"), "+79001234567")

    def test_eight_local(self):
        self.assertEqual(normalize_phone("89001234567"), "+79001234567")

    def test_invalid_short(self):
        self.assertIsNone(normalize_phone("123"))

    def test_code_digits(self):
        self.assertEqual(normalize_login_code("12-345"), "12345")
        self.assertIsNone(normalize_login_code("ab"))


class AuthErrorTests(unittest.TestCase):
    def test_floodwait_message(self):
        text = describe_auth_error(FloodWaitError(None, 12))
        self.assertIn("12", text)
        self.assertNotIn("token", text.lower())

    def test_invalid_phone_message(self):
        self.assertIn("Номер", describe_auth_error(PhoneNumberInvalidError(None)))

    def test_invalid_code_message(self):
        self.assertIn("Код", describe_auth_error(PhoneCodeInvalidError(None)))


class NoTerminalLoginTests(unittest.TestCase):
    def test_connect_helper_exists(self):
        self.assertTrue(hasattr(user_client, "connect_user_client"))
        self.assertFalse(hasattr(user_client, "start_user_client"))
        source = inspect.getsource(user_client)
        self.assertNotIn("input(", source)
        self.assertNotIn("client.start()", source)


if __name__ == "__main__":
    unittest.main()
