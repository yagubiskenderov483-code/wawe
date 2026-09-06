from __future__ import annotations

import unittest
from types import SimpleNamespace

from aiogram.enums import ChatType
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.methods import GetMe

from app.config import bot_id_from_token
from app.telegram.bot import BOT_TOKEN_HELP, BotUnauthorizedError, verify_target_channels
from tests.helpers import settings


class FakeBot:
    def __init__(self, me=None, error=None, chats=None, members=None):
        self._me = me
        self._error = error
        self._chats = chats or {}
        self._members = members or {}
        self.me_calls = 0
        self.get_chat_calls = []

    async def me(self):
        self.me_calls += 1
        if self._error is not None:
            raise self._error
        return self._me

    async def get_chat(self, chat_id):
        self.get_chat_calls.append(chat_id)
        if chat_id not in self._chats:
            raise RuntimeError(f"missing chat {chat_id}")
        return self._chats[chat_id]

    async def get_chat_member(self, chat_id, user_id):
        return self._members.get((chat_id, user_id), SimpleNamespace(status="administrator", can_post_messages=True))


class BotIdFromTokenTests(unittest.TestCase):
    def test_parses_numeric_prefix(self):
        self.assertEqual(bot_id_from_token("8825465611:AAHexampletokenvalue"), 8825465611)

    def test_rejects_empty_and_invalid(self):
        self.assertIsNone(bot_id_from_token(""))
        self.assertIsNone(bot_id_from_token(None))
        self.assertIsNone(bot_id_from_token("not-a-token"))
        self.assertIsNone(bot_id_from_token("abc:secret"))


class VerifyTargetChannelsTests(unittest.IsolatedAsyncioTestCase):
    async def test_unauthorized_token_is_configuration_error(self):
        bot = FakeBot(
            error=TelegramUnauthorizedError(method=GetMe(), message="Unauthorized"),
            chats={-1003784435307: SimpleNamespace(type=ChatType.CHANNEL)},
        )
        cfg = settings(bot_token="8825465611:AAHrevokedtokenvalue000", target_channel_id=-1003784435307)
        with self.assertRaises(BotUnauthorizedError) as ctx:
            await verify_target_channels(bot, cfg)
        self.assertEqual(str(ctx.exception), BOT_TOKEN_HELP)
        self.assertIn("BotFather", str(ctx.exception))
        self.assertNotIn("AAHrevokedtokenvalue000", str(ctx.exception))
        self.assertEqual(bot.me_calls, 1)

    async def test_bot_id_is_not_a_publish_target(self):
        bot = FakeBot(me=SimpleNamespace(id=8825465611))
        cfg = settings(bot_token="8825465611:AAHexampletokenvalue", target_channel_id=8825465611)
        usable = await verify_target_channels(bot, cfg)
        self.assertEqual(usable, ())
        self.assertEqual(bot.me_calls, 0)
        self.assertEqual(bot.get_chat_calls, [])

    async def test_missing_targets_return_empty(self):
        bot = FakeBot(me=SimpleNamespace(id=1))
        cfg = settings(target_channel_id=None, target_channels=())
        usable = await verify_target_channels(bot, cfg)
        self.assertEqual(usable, ())
        self.assertEqual(bot.me_calls, 0)


if __name__ == "__main__":
    unittest.main()
