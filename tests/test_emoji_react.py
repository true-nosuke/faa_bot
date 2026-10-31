import asyncio
import io
import json
import random
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
from PIL import Image

from cogs import emoji_react as er
from cogs.emoji_react import EmojiError, EmojiReact, RateLimiter, check_keyword, normalize, parse_emoji_ref, process_image


def png(size=(64, 64), noise=False, mode="RGBA"):
    im = Image.new(mode, size, (255, 0, 0, 128) if mode == "RGBA" else (255, 0, 0))
    if noise:
        im = Image.frombytes(mode, size, random.Random(1).randbytes(size[0] * size[1] * len(mode)))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def gif(frames=10, size=(200, 200), noise=False):
    ims = []
    for i in range(frames):
        if noise:
            ims.append(Image.frombytes("RGB", size, random.Random(i).randbytes(size[0] * size[1] * 3)))
        else:
            ims.append(Image.new("RGB", size, (i * 20 % 255, 0, 100)))
    buf = io.BytesIO()
    ims[0].save(buf, "GIF", save_all=True, append_images=ims[1:], duration=50, loop=0)
    return buf.getvalue()


class ImageTests(unittest.TestCase):
    def test_small_passthrough(self):
        raw = png()
        self.assertEqual(process_image(raw), (raw, False))

    def test_large_static_shrinks(self):
        raw = png((1500, 1500), noise=True)
        self.assertGreater(len(raw), er.MAX_EMOJI_BYTES)
        data, animated = process_image(raw)
        self.assertLessEqual(len(data), er.MAX_EMOJI_BYTES)
        self.assertFalse(animated)
        self.assertEqual(Image.open(io.BytesIO(data)).format, "PNG")

    def test_jpeg_and_webp(self):
        for fmt in ("JPEG", "WEBP"):
            buf = io.BytesIO()
            Image.new("RGB", (100, 100), (1, 2, 3)).save(buf, fmt)
            data, animated = process_image(buf.getvalue())
            self.assertLessEqual(len(data), er.MAX_EMOJI_BYTES)

    def test_small_gif_passthrough(self):
        raw = gif(5, (32, 32))
        self.assertEqual(process_image(raw), (raw, True))

    def test_large_gif_shrinks_and_stays_animated(self):
        raw = gif(30, (400, 400), noise=True)
        self.assertGreater(len(raw), er.MAX_EMOJI_BYTES)
        data, animated = process_image(raw)
        self.assertTrue(animated)
        self.assertLessEqual(len(data), er.MAX_EMOJI_BYTES)
        self.assertGreater(Image.open(io.BytesIO(data)).n_frames, 1)

    def test_animated_webp_to_gif(self):
        ims = [Image.new("RGB", (64, 64), (i * 30, 0, 0)) for i in range(5)]
        buf = io.BytesIO()
        ims[0].save(buf, "WEBP", save_all=True, append_images=ims[1:], duration=80)
        data, animated = process_image(buf.getvalue())
        self.assertTrue(animated)
        self.assertEqual(Image.open(io.BytesIO(data)).format, "GIF")

    def test_bad_inputs(self):
        with self.assertRaises(EmojiError):
            process_image(b"not an image")
        buf = io.BytesIO()
        Image.new("RGB", (10, 10)).save(buf, "BMP")
        with self.assertRaises(EmojiError):
            process_image(buf.getvalue())
        with self.assertRaises(EmojiError):
            process_image(b"0" * (er.MAX_INPUT_BYTES + 1))


class HelperTests(unittest.TestCase):
    def test_keyword(self):
        self.assertEqual(check_keyword(" ナイス "), "ナイス")
        for bad in ("", "a b", "x" * 21, "<a>", "@all", "#x", "a\u3000b", "a\nb"):
            with self.assertRaises(EmojiError, msg=bad):
                check_keyword(bad)
        self.assertEqual(normalize("ＮＩＣＥ"), normalize("nice"))
        self.assertEqual(normalize("ﾌｧﾐﾁｷ"), "ファミチキ")

    def test_parse_ref(self):
        self.assertEqual(parse_emoji_ref("<a:x_y:1558708828831223878>"), (1558708828831223878, True))
        self.assertEqual(parse_emoji_ref("<:x:1558708828831223878>"), (1558708828831223878, False))
        self.assertEqual(parse_emoji_ref(" 1558708828831223878 "), (1558708828831223878, None))
        for bad in ("abc", "123", "<:x:12>", "https://x"):
            with self.assertRaises(EmojiError):
                parse_emoji_ref(bad)

    def test_rate_limiter(self):
        r = RateLimiter(((60, 3), (3600, 5)))
        self.assertTrue(all(r.allow(1, t) for t in (0, 1, 2)))
        self.assertFalse(r.allow(1, 3))
        self.assertTrue(r.allow(2, 3))
        self.assertTrue(r.allow(1, 70))
        self.assertTrue(r.allow(1, 71))
        self.assertFalse(r.allow(1, 72))     # 1時間に5回まで
        self.assertTrue(r.allow(1, 4000))


def make_cog(tmp, **kw):
    bot = MagicMock()
    bot.create_application_emoji = AsyncMock()
    bot.fetch_application_emoji = AsyncMock()
    bot.fetch_application_emojis = AsyncMock(return_value=[])
    return EmojiReact(bot, Path(tmp) / "emoji_map.json"), bot


_next_user = iter(range(1000, 100000))


def inter(user_id=None):
    i = MagicMock()
    i.user = SimpleNamespace(id=user_id or next(_next_user))
    i.response.send_message = AsyncMock()
    i.response.defer = AsyncMock()
    i.followup.send = AsyncMock()
    return i


def sent(i):
    calls = i.followup.send.call_args_list + i.response.send_message.call_args_list
    return "\n".join(str(c.args[0]) for c in calls)


def attachment(raw, content_type="image/png"):
    a = MagicMock(spec=discord.Attachment)
    a.size = len(raw)
    a.content_type = content_type
    a.read = AsyncMock(return_value=raw)
    return a


def fake_emoji(eid=111111111111111111, name="e_ab", animated=False):
    e = MagicMock()
    e.id, e.name, e.animated = eid, name, animated
    e.__str__ = lambda self: f"<{'a' if animated else ''}:{name}:{eid}>"
    e.delete = AsyncMock()
    return e


def run(coro):
    return asyncio.run(coro)


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cog, self.bot = make_cog(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def stored(self):
        return json.loads((Path(self.tmp.name) / "emoji_map.json").read_text(encoding="utf-8"))

    def add(self, keyword="ナイス", **kw):
        i = inter()
        run(self.cog.emoji_add.callback(self.cog, i, keyword, **kw))
        return i

    def test_seed_famichiki_once(self):
        self.assertEqual(self.stored()["ファミチキ"]["emoji_id"], 1558708828831223878)
        self.assertFalse(self.stored()["ファミチキ"]["owned"])
        self.cog.data.clear(); self.cog._save()
        again, _ = make_cog(self.tmp.name)        # ファイルがあれば再投入しない
        self.assertEqual(again.data, {})

    def test_add_image(self):
        self.bot.create_application_emoji.return_value = fake_emoji()
        i = self.add(image=attachment(png()))
        self.assertIn("登録しました", sent(i))
        self.bot.create_application_emoji.assert_awaited_once()
        self.assertEqual(self.stored()["ナイス"]["emoji_id"], 111111111111111111)
        self.assertGreaterEqual(self.stored()["ナイス"]["added_by"], 1000)

    def test_add_large_image_is_shrunk(self):
        self.bot.create_application_emoji.return_value = fake_emoji()
        self.add(image=attachment(png((1500, 1500), noise=True)))
        self.assertLessEqual(len(self.bot.create_application_emoji.call_args.kwargs["image"]), er.MAX_EMOJI_BYTES)

    def test_add_validation(self):
        i = self.add(keyword="a b", image=attachment(png()))
        self.assertIn("空白", sent(i))
        i = self.add()
        self.assertIn("どちらか一方", sent(i))
        i = self.add(image=attachment(png()), emoji_id="123456789012345678")
        self.assertIn("どちらか一方", sent(i))
        i = self.add(image=attachment(png()), name="日本語")
        self.assertIn("name", sent(i))
        i = self.add(image=attachment(b"x", "text/plain"))
        self.assertIn("画像", sent(i))
        self.bot.create_application_emoji.assert_not_awaited()
        self.assertNotIn("ナイス", self.stored())

    def test_add_duplicate_and_fullwidth(self):
        self.bot.create_application_emoji.return_value = fake_emoji()
        self.add("NICE", image=attachment(png()))
        i = self.add("ＮＩＣＥ", image=attachment(png()))
        self.assertIn("すでに登録", sent(i))
        i = self.add("ファミチキ", image=attachment(png()))
        self.assertIn("すでに登録", sent(i))

    def test_add_failure_does_not_save(self):
        self.bot.create_application_emoji.side_effect = discord.HTTPException(
            MagicMock(status=400, reason="x"), {"message": "Maximum number of emojis reached", "code": 30008})
        i = self.add(image=attachment(png()))
        self.assertIn("枠がいっぱい", sent(i))
        self.assertNotIn("ナイス", self.stored())

    def test_add_from_emoji_id(self):
        self.bot.create_application_emoji.return_value = fake_emoji(animated=True)
        urls = []

        async def dl(url):
            urls.append(url)
            return gif(3, (32, 32))
        with patch.object(self.cog, "_download", dl):
            i = self.add("ぱっと", emoji_id="<a:wow:222222222222222222>")
        self.assertIn("cdn.discordapp.com/emojis/222222222222222222.gif", urls[0])
        self.assertIn("登録しました", sent(i))
        i = self.add("だめ", emoji_id="abc")
        self.assertIn("emoji_id", sent(i))

    def test_emoji_id_only_digits_static(self):
        self.bot.create_application_emoji.return_value = fake_emoji()
        urls = []

        async def dl(url):
            urls.append(url)
            return None if url.split("?")[0].endswith(".gif") and False else (gif(1, (16, 16)) if ".gif" in url else png())
        with patch.object(self.cog, "_download", dl):
            self.add("せいし", emoji_id="222222222222222222")
        self.assertTrue(urls[0].split("?")[0].endswith(".gif"))
        self.assertTrue(urls[1].split("?")[0].endswith(".png"))

    def test_emoji_id_not_found(self):
        async def dl(url):
            return None
        with patch.object(self.cog, "_download", dl):
            i = self.add("なし", emoji_id="222222222222222222")
        self.assertIn("見つかりません", sent(i))
        self.assertNotIn("なし", self.stored())

    def test_alias_edit_remove_flow(self):
        self.bot.create_application_emoji.return_value = fake_emoji()
        self.add(image=attachment(png()))
        i = inter(); run(self.cog.emoji_alias.callback(self.cog, i, "ナイス", "nice")); self.assertIn("追加しました", sent(i))
        i = inter(); run(self.cog.emoji_alias.callback(self.cog, i, "ナイス", "NICE")); self.assertIn("すでに", sent(i))
        i = inter(); run(self.cog.emoji_alias.callback(self.cog, i, "なにか", "x")); self.assertIn("登録されていません", sent(i))
        self.assertEqual(self.stored()["nice"]["emoji_id"], 111111111111111111)
        # 画像差し替え → alias も一緒に新しい絵文字へ、古い絵文字は削除
        old = fake_emoji(); self.bot.fetch_application_emoji.return_value = old
        self.bot.create_application_emoji.return_value = fake_emoji(eid=333333333333333333, name="e_new")
        i = inter(); run(self.cog.emoji_edit.callback(self.cog, i, "ナイス", image=attachment(png()))); self.assertIn("変更しました", sent(i))
        self.assertEqual(self.stored()["nice"]["emoji_id"], 333333333333333333)
        self.assertEqual(self.stored()["ナイス"]["emoji_id"], 333333333333333333)
        old.delete.assert_awaited_once()
        # キーワード変更
        i = inter(); run(self.cog.emoji_edit.callback(self.cog, i, "nice", new_keyword="いいね")); self.assertIn("変更しました", sent(i))
        self.assertNotIn("nice", self.stored()); self.assertIn("いいね", self.stored())
        i = inter(); run(self.cog.emoji_edit.callback(self.cog, i, "いいね", new_keyword="ナイス")); self.assertIn("すでに", sent(i))
        i = inter(); run(self.cog.emoji_edit.callback(self.cog, i, "いいね")); self.assertIn("変更する項目", sent(i))
        i = inter(); run(self.cog.emoji_edit.callback(self.cog, i, "いいね", image=attachment(png()), emoji_id="123456789012345678")); self.assertIn("どちらか", sent(i))
        # remove: 片方消してもまだ共有されているので絵文字は残る
        cur = fake_emoji(eid=333333333333333333); self.bot.fetch_application_emoji.return_value = cur
        i = inter(); run(self.cog.emoji_remove.callback(self.cog, i, "いいね")); self.assertIn("削除しました", sent(i)); self.assertIn("残しています", sent(i))
        cur.delete.assert_not_awaited()
        i = inter(); run(self.cog.emoji_remove.callback(self.cog, i, "ナイス")); self.assertNotIn("残しています", sent(i))
        cur.delete.assert_awaited_once()
        i = inter(); run(self.cog.emoji_remove.callback(self.cog, i, "ナイス")); self.assertIn("登録されていません", sent(i))

    def test_remove_seed_does_not_delete_server_emoji(self):
        i = inter(); run(self.cog.emoji_remove.callback(self.cog, i, "ファミチキ"))
        self.assertIn("削除しました", sent(i))
        self.bot.fetch_application_emoji.assert_not_awaited()
        self.assertNotIn("ファミチキ", self.stored())

    def test_edit_failure_keeps_old(self):
        self.bot.create_application_emoji.return_value = fake_emoji()
        self.add(image=attachment(png()))
        i = inter(); run(self.cog.emoji_edit.callback(self.cog, i, "ナイス", image=attachment(b"zzz")))
        self.assertIn("⚠️", sent(i))
        self.assertEqual(self.stored()["ナイス"]["emoji_id"], 111111111111111111)

    def test_list_slots(self):
        i = inter(); run(self.cog.emoji_list.callback(self.cog, i, 1)); self.assertIn("ファミチキ", sent(i))
        self.bot.fetch_application_emojis.return_value = [fake_emoji(animated=True), fake_emoji()]
        i = inter(); run(self.cog.emoji_slots.callback(self.cog, i)); self.assertIn("2 /", sent(i)); self.assertIn("アニメ 1個", sent(i))
        i = inter(); run(self.cog.emoji_list.callback(self.cog, i, 99)); self.assertIn("1/1ページ", sent(i))

    def test_throttle(self):
        self.bot.create_application_emoji.return_value = fake_emoji()
        for n in range(5):
            i = inter(7); run(self.cog.emoji_remove.callback(self.cog, i, f"x{n}"))
            self.assertNotIn("少し待って", sent(i))
        i = inter(7); run(self.cog.emoji_remove.callback(self.cog, i, "x"))
        self.assertIn("少し待って", sent(i))
        i = inter(user_id=8); run(self.cog.emoji_remove.callback(self.cog, i, "x"))
        self.assertNotIn("少し待って", sent(i))

    def test_no_permission_checks(self):
        for cmd in (self.cog.emoji_add, self.cog.emoji_alias, self.cog.emoji_edit, self.cog.emoji_remove, self.cog.emoji_list, self.cog.emoji_slots):
            self.assertFalse(getattr(cmd, "checks", []))
            self.assertIsNone(cmd.default_permissions)


def message(content, reply_to=5, add_reactions=True, manage=True, history=True, bot=False, reactions=()):
    m = MagicMock(spec=discord.Message)
    m.author = SimpleNamespace(id=7, bot=bot)
    m.guild = SimpleNamespace(me=object())
    m.content = content
    m.channel = MagicMock()
    m.channel.permissions_for.return_value = SimpleNamespace(add_reactions=add_reactions, manage_messages=manage, read_message_history=history)
    m.reference = None if reply_to is None else SimpleNamespace(message_id=reply_to, cached_message=None)
    target = MagicMock()
    target.id = reply_to
    target.reactions = [SimpleNamespace(emoji=e, me=True) for e in reactions]
    target.add_reaction = AsyncMock()
    m.channel.fetch_message = AsyncMock(return_value=target)
    m.delete = AsyncMock()
    m._target = target
    return m


class TriggerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cog, self.bot = make_cog(self.tmp.name)
        self.cog.data["nice"] = {"keyword": "nice", "emoji_id": 444444444444444444, "name": "nice", "animated": False, "owned": True, "added_by": 1}

    def tearDown(self):
        self.tmp.cleanup()

    def go(self, m):
        run(self.cog.on_message(m))

    def test_famichiki_migrated(self):
        m = message("ファミチキ")
        self.go(m)
        m._target.add_reaction.assert_awaited_once()
        self.assertEqual(str(m._target.add_reaction.call_args.args[0]), "<a:famichiki:1558708828831223878>")
        m.delete.assert_awaited_once()

    def test_not_triggered(self):
        for m in (message("ファミチキ", reply_to=None), message("ファミチキ です"), message("こんにちは"), message("ファミチキ", bot=True),
                  message("ファミチキ nice 1 2"), message("")):
            self.go(m)
            m._target.add_reaction.assert_not_awaited(); m.delete.assert_not_awaited()

    def test_multiple_and_dedup(self):
        self.cog.data["nice2"] = dict(self.cog.data["nice"], keyword="nice2")
        m = message("ＮＩＣＥ  nice2 ファミチキ")
        self.go(m)
        self.assertEqual(m._target.add_reaction.await_count, 2)
        m.delete.assert_awaited_once()

    def test_no_manage_messages_still_reacts(self):
        m = message("nice", manage=False)
        self.go(m)
        m._target.add_reaction.assert_awaited_once(); m.delete.assert_not_awaited()

    def test_no_add_reactions(self):
        m = message("nice", add_reactions=False)
        self.go(m)
        m.channel.fetch_message.assert_not_awaited(); m.delete.assert_not_awaited()

    def test_already_reacted(self):
        m = message("nice", reactions=["<:nice:444444444444444444>"])
        self.go(m)
        m._target.add_reaction.assert_not_awaited(); m.delete.assert_not_awaited()

    def test_missing_emoji_keeps_message(self):
        m = message("nice")
        m._target.add_reaction.side_effect = discord.NotFound(MagicMock(status=404, reason="x"), {"message": "Unknown Emoji", "code": 10014})
        self.go(m)
        m.delete.assert_not_awaited()

    def test_errors_are_swallowed(self):
        m = message("nice")
        m.channel.fetch_message.side_effect = discord.NotFound(MagicMock(status=404, reason="x"), {"message": "Unknown Message", "code": 10008})
        self.go(m)
        m = message("nice")
        m._target.add_reaction.side_effect = discord.Forbidden(MagicMock(status=403, reason="x"), {"message": "Missing", "code": 50013})
        self.go(m)
        m.delete.assert_not_awaited()

    def test_react_rate_limit(self):
        for n in range(10):
            m = message("nice"); self.go(m); m._target.add_reaction.assert_awaited_once()
        m = message("nice"); self.go(m)
        m._target.add_reaction.assert_not_awaited(); m.delete.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
