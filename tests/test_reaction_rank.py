"""reaction_rank（リアクションランキング）の単体テスト。"""
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cogs.reaction_rank import (JST, emoji_matches, format_ranking, match_count,
                                parse_emoji, rank_records, scan_channel, year_start)


def fake_message(content="本文", reactions=(), created_at=None, jump_url="https://example.com/m"):
    return SimpleNamespace(
        content=content, reactions=list(reactions),
        created_at=created_at or datetime(2026, 3, 1, tzinfo=timezone.utc),
        author="user#1", channel=SimpleNamespace(name="general", id=1),
        jump_url=jump_url)


class ParseEmojiTests(unittest.TestCase):
    def test_unicode(self):
        e = parse_emoji("⭐")
        self.assertIsNotNone(e)
        self.assertIsNone(e.id)
        self.assertEqual(e.name, "⭐")

    def test_custom(self):
        e = parse_emoji("<:typst:1558680485427679422>")
        self.assertEqual(e.id, 1558680485427679422)
        self.assertEqual(e.name, "typst")
        self.assertFalse(e.animated)

    def test_animated(self):
        e = parse_emoji("<a:party:123456789012345678>")
        self.assertTrue(e.animated)

    def test_invalid(self):
        for bad in ["", "abc", ":typst:", "<:typst:123>", "hello world"]:
            self.assertIsNone(parse_emoji(bad), bad)


class MatchTests(unittest.TestCase):
    def test_unicode_str_emoji(self):
        self.assertTrue(emoji_matches("⭐", parse_emoji("⭐")))
        self.assertFalse(emoji_matches("🔥", parse_emoji("⭐")))

    def test_custom_by_id(self):
        target = parse_emoji("<:typst:1558680485427679422>")
        self.assertTrue(emoji_matches(SimpleNamespace(id=1558680485427679422, name="other"), target))
        self.assertFalse(emoji_matches(SimpleNamespace(id=1, name="typst"), target))

    def test_match_count(self):
        target = parse_emoji("⭐")
        msg = fake_message(reactions=[
            SimpleNamespace(emoji="🔥", count=3),
            SimpleNamespace(emoji="⭐", count=7),
        ])
        self.assertEqual(match_count(msg, target), 7)
        self.assertEqual(match_count(fake_message(), target), 0)


class RankTests(unittest.TestCase):
    def rec(self, count, day):
        return {"count": count, "created_at": datetime(2026, 1, day, tzinfo=timezone.utc)}

    def test_count_desc_then_oldest_first(self):
        records = [self.rec(5, 10), self.rec(9, 20), self.rec(9, 3), self.rec(1, 1)]
        ranked = rank_records(records, 3)
        self.assertEqual([r["count"] for r in ranked], [9, 9, 5])
        self.assertEqual(ranked[0]["created_at"].day, 3)  # 同数は古い順

    def test_year_start(self):
        start = year_start(datetime(2026, 11, 11, tzinfo=JST))
        self.assertEqual((start.year, start.month, start.day), (2026, 1, 1))
        self.assertEqual(start.tzinfo, JST)


class FormatTests(unittest.TestCase):
    def test_chunks_within_limit(self):
        records = []
        for i in range(25):
            m = fake_message(content="あ" * 80, jump_url=f"https://example.com/{i}")
            from cogs.reaction_rank import make_record
            records.append(make_record(m, 25 - i))
        chunks = format_ranking("⭐", records, "2026年", 12345, 6)
        self.assertTrue(all(len(c) <= 1900 for c in chunks))
        self.assertIn("⭐×25", chunks[0])

    def test_empty(self):
        chunks = format_ranking("⭐", [], "2026年", 10, 2)
        self.assertEqual(len(chunks), 1)
        self.assertIn("見つからなかった", chunks[0])


class ScanTests(unittest.IsolatedAsyncioTestCase):
    async def test_scan_collects_hits(self):
        target = parse_emoji("⭐")
        hit = fake_message(reactions=[SimpleNamespace(emoji="⭐", count=4)])
        miss = fake_message(reactions=[SimpleNamespace(emoji="🔥", count=9)])
        none = fake_message()

        async def history(limit=None, after=None):
            for m in (hit, miss, none):
                yield m

        channel = SimpleNamespace(history=history)
        records = []
        scanned = await scan_channel(channel, target, None, records)
        self.assertEqual(scanned, 3)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["count"], 4)


if __name__ == "__main__":
    unittest.main()
