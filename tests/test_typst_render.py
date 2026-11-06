import unittest
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import AsyncMock

from cogs import typst_render as tr
from cogs.typst_render import TypstRender, extract_manual_source, extract_source


class ManualSourceTests(unittest.TestCase):
    def test_detected_math_unchanged(self):
        content = "$x^2 + y^2$"
        self.assertEqual(extract_manual_source(content), extract_source(content))

    def test_plain_math_without_dollars(self):
        source = extract_manual_source("x^2 + y^2 = z^2")
        self.assertIsNotNone(source)
        self.assertTrue(source.endswith("$ x^2 + y^2 = z^2 $\n"))

    def test_generic_code_block_as_code(self):
        source = extract_manual_source("```\n#let x = 1\nx + 1\n```")
        self.assertIsNotNone(source)
        self.assertIn("#let x = 1", source)
        self.assertNotIn("$ #let", source)

    def test_raw_typst_commands_as_code(self):
        source = extract_manual_source("#set text(size: 12pt)\n本文です")
        self.assertIsNotNone(source)
        self.assertIn("#set text(size: 12pt)", source)

    def test_too_many_spans_rescued(self):
        content = " ".join(f"$a{i}$" for i in range(11))
        self.assertIsNone(extract_source(content))  # 上限超過で自動検知されない
        self.assertIsNotNone(extract_manual_source(content))

    def test_multiline_math_rescued(self):
        content = "$\nx^2 + y^2\n$"
        self.assertIsNone(extract_source(content))
        source = extract_manual_source(content)
        self.assertIsNotNone(source)
        self.assertIn("$ x^2 + y^2 $", source)

    def test_forbidden_include_rejected(self):
        self.assertIsNone(extract_manual_source('#include "secret.typ"'))
        self.assertIsNone(extract_manual_source('```\n#include "secret.typ"\n```'))

    def test_empty_and_too_long(self):
        self.assertIsNone(extract_manual_source("   "))
        self.assertIsNone(extract_manual_source("x" * (tr.MAX_CODE_CHARS + 1)))


class Typing:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *args):
        return False


def make_handler_cog():
    cog = TypstRender.__new__(TypstRender)
    cog.done = OrderedDict()
    cog.last_used = {}
    cog.manual = 0
    cog.render = AsyncMock(return_value=([b"PNGDATA"], None))
    return cog


class ReactionHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_manual_trigger_on_undetected_message(self):
        cog = make_handler_cog()
        message = SimpleNamespace(
            id=10,
            content="x^2 + y^2 = z^2",  # $ がないので自動検知されない
            reactions=[],  # Botのリアクションはない
            reply=AsyncMock(),
        )
        channel = SimpleNamespace(
            permissions_for=lambda me: SimpleNamespace(
                send_messages=True, attach_files=True, read_message_history=True),
            fetch_message=AsyncMock(return_value=message),
            typing=lambda: Typing(),
        )
        cog.bot = SimpleNamespace(
            user=SimpleNamespace(id=999),
            get_guild=lambda guild_id: SimpleNamespace(me=SimpleNamespace(id=999)),
            get_channel=lambda channel_id: channel,
        )
        payload = SimpleNamespace(
            emoji=tr.TRIGGER_EMOJI, guild_id=1, user_id=42, channel_id=5, message_id=10)
        await cog.on_raw_reaction_add(payload)
        message.reply.assert_awaited_once()
        files = message.reply.await_args.kwargs["files"]
        self.assertEqual(len(files), 1)
        self.assertEqual(cog.manual, 1)
        self.assertIn(10, cog.done)

    async def test_bot_own_reaction_ignored(self):
        cog = make_handler_cog()
        fetch = AsyncMock()
        channel = SimpleNamespace(fetch_message=fetch)
        cog.bot = SimpleNamespace(
            user=SimpleNamespace(id=999),
            get_guild=lambda guild_id: SimpleNamespace(me=SimpleNamespace(id=999)),
            get_channel=lambda channel_id: channel,
        )
        payload = SimpleNamespace(
            emoji=tr.TRIGGER_EMOJI, guild_id=1, user_id=999, channel_id=5, message_id=10)
        await cog.on_raw_reaction_add(payload)
        fetch.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()

class ManualSourceJapaneseTests(unittest.TestCase):
    def test_japanese_prose_as_document(self):
        source = extract_manual_source("ただの文章です")
        self.assertIsNotNone(source)
        self.assertTrue(source.endswith("ただの文章です"))
        self.assertNotIn("$ ただの文章です $", source)

    def test_japanese_with_multiline_math_as_document(self):
        source = extract_manual_source("答えは $\nx^2\n$ です")
        self.assertIsNotNone(source)
        self.assertIn("答えは", source)

class ManualEmojiTests(unittest.IsolatedAsyncioTestCase):
    def build(self, message):
        cog = make_handler_cog()
        channel = SimpleNamespace(
            permissions_for=lambda me: SimpleNamespace(
                send_messages=True, attach_files=True, read_message_history=True),
            fetch_message=AsyncMock(return_value=message),
            typing=lambda: Typing(),
        )
        cog.bot = SimpleNamespace(
            user=SimpleNamespace(id=999),
            get_guild=lambda guild_id: SimpleNamespace(me=SimpleNamespace(id=999)),
            get_channel=lambda channel_id: channel,
        )
        return cog, channel

    async def test_regional_indicator_t_triggers(self):
        message = SimpleNamespace(
            id=11, content="x^2 + y^2 = z^2", reactions=[], reply=AsyncMock())
        cog, _ = self.build(message)
        payload = SimpleNamespace(
            emoji="🇹", guild_id=1, user_id=42, channel_id=5, message_id=11)
        await cog.on_raw_reaction_add(payload)
        message.reply.assert_awaited_once()
        self.assertEqual(cog.manual, 1)

    async def test_unrelated_emoji_ignored(self):
        message = SimpleNamespace(
            id=12, content="x^2", reactions=[], reply=AsyncMock())
        cog, channel = self.build(message)
        payload = SimpleNamespace(
            emoji="👍", guild_id=1, user_id=42, channel_id=5, message_id=12)
        await cog.on_raw_reaction_add(payload)
        channel.fetch_message.assert_not_awaited()
        message.reply.assert_not_awaited()
