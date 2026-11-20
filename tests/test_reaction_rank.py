"""reaction_rank（リアクションランキング）の単体テスト。"""
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cogs.reaction_rank import (JST, PAGE_SIZE, RankingView, aggregate_users,
                                emoji_matches, format_pages, format_user_pages,
                                match_count, parse_emoji, past_year_start,
                                period_start, rank_records, scan_channel,
                                year_start)


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

    def test_past_year_start(self):
        start = past_year_start(datetime(2026, 11, 20, 17, 30, tzinfo=JST))
        self.assertEqual(start, datetime(2025, 11, 20, 17, 30, tzinfo=JST))

    def test_past_year_start_leap_day(self):
        start = past_year_start(datetime(2028, 2, 29, tzinfo=JST))
        self.assertEqual(start, datetime(2027, 2, 28, tzinfo=JST))

    def test_period_start(self):
        now = datetime(2026, 11, 20, tzinfo=JST)
        self.assertEqual(period_start("year", now), datetime(2026, 1, 1, tzinfo=JST))
        self.assertEqual(period_start("past_year", now), datetime(2025, 11, 20, tzinfo=JST))
        self.assertIsNone(period_start("all", now))


def user_record(author, author_id, count, day, bot=False, snippet="本文"):
    return {"count": count,
            "created_at": datetime(2026, 3, day, tzinfo=timezone.utc),
            "author": author, "author_id": author_id, "author_bot": bot,
            "channel": "general", "snippet": snippet,
            "url": f"https://example.com/{author_id}-{day}"}


class AggregateUsersTests(unittest.TestCase):
    def test_totals_best_and_bot_exclusion(self):
        records = [
            user_record("alice", 1, 5, 10),
            user_record("alice", 1, 7, 12),
            user_record("alice", 1, 3, 20),
            user_record("bob", 2, 10, 5),
            user_record("botty", 3, 100, 1, bot=True),
        ]
        users = aggregate_users(records, 10)
        self.assertEqual([u["author"] for u in users], ["alice", "bob"])
        self.assertEqual(users[0]["total"], 15)
        self.assertEqual(users[0]["messages"], 3)
        self.assertEqual(users[0]["best"]["count"], 7)
        self.assertEqual(users[1]["total"], 10)

    def test_tie_breaks_by_first_hit(self):
        records = [
            user_record("late", 1, 10, 20),
            user_record("early", 2, 10, 3),
        ]
        users = aggregate_users(records, 10)
        self.assertEqual(users[0]["author"], "early")

    def test_limit(self):
        records = [user_record(f"u{i}", i, 10 - i, 1) for i in range(5)]
        self.assertEqual(len(aggregate_users(records, 2)), 2)


class UserFormatTests(unittest.TestCase):
    def test_user_pages_shape(self):
        records = [
            user_record("alice", 1, 5, 10, snippet="一番の投稿"),
            user_record("alice", 1, 7, 12),
            user_record("bob", 2, 10, 5),
        ]
        users = aggregate_users(records, 10)
        pages = format_user_pages("⭐", users, "過去1年", 500, 4)
        self.assertEqual(len(pages), 1)
        self.assertIn("## ⭐ リアクション ユーザーランキング（過去1年）", pages[0])
        self.assertIn("### 1位　⭐×12", pages[0])
        self.assertIn("alice ・ 対象メッセージ2件", pages[0])
        self.assertIn("最多は ⭐×7", pages[0])

    def test_user_pages_empty(self):
        pages = format_user_pages("⭐", [], "過去1年", 10, 2)
        self.assertEqual(len(pages), 1)
        self.assertIn("いなかった", pages[0])


class FormatTests(unittest.TestCase):
    def build_records(self, n):
        from cogs.reaction_rank import make_record
        records = []
        for i in range(n):
            m = fake_message(content="あ" * 80, jump_url=f"https://example.com/{i}")
            records.append(make_record(m, n - i))
        return records

    def test_markdown_shape(self):
        pages = format_pages("⭐", self.build_records(3), "2026年", 100, 4)
        self.assertEqual(len(pages), 1)
        self.assertIn("## ⭐ リアクションランキング（2026年）", pages[0])
        self.assertIn("### 1位　⭐×3", pages[0])
        self.assertIn("> " + "あ" * 80, pages[0])
        self.assertIn("[メッセージへ](https://example.com/0)", pages[0])
        self.assertNotIn("ページ 1 /", pages[0])  # 1ページだけならフッターなし

    def test_pagination(self):
        pages = format_pages("⭐", self.build_records(25), "2026年", 12345, 6)
        self.assertEqual(len(pages), 25 // PAGE_SIZE)
        self.assertTrue(all(len(p) <= 2000 for p in pages))
        self.assertIn("### 1位　⭐×25", pages[0])
        self.assertIn(f"### {PAGE_SIZE + 1}位", pages[1])  # 順位はページをまたいで連続
        self.assertIn(f"ページ 2 / {len(pages)}", pages[1])

    def test_empty(self):
        pages = format_pages("⭐", [], "2026年", 10, 2)
        self.assertEqual(len(pages), 1)
        self.assertIn("見つからなかった", pages[0])


class ViewTests(unittest.TestCase):
    def test_button_states_follow_index(self):
        view = RankingView(["p1", "p2", "p3"])
        self.assertTrue(view.prev_page.disabled)
        self.assertFalse(view.next_page.disabled)
        self.assertEqual(view.page_label.label, "1 / 3")
        view.index = 1
        view._sync_buttons()
        self.assertFalse(view.prev_page.disabled)
        self.assertFalse(view.next_page.disabled)
        self.assertEqual(view.page_label.label, "2 / 3")
        view.index = 2
        view._sync_buttons()
        self.assertTrue(view.next_page.disabled)
        self.assertEqual(view.page_label.label, "3 / 3")


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

    async def test_view_timeout_disables_buttons(self):
        view = RankingView(["p1", "p2"])
        await view.on_timeout()
        self.assertTrue(all(c.disabled for c in view.children))


if __name__ == "__main__":
    unittest.main()
