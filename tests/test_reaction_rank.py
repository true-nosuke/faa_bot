"""reaction_rank（リアクションランキング）の単体テスト。"""
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cogs.reaction_rank import (JST, PAGE_SIZE, RankingView, aggregate_emojis,
                                aggregate_users, cache_covers, emoji_key,
                                emoji_matches, filter_records, format_emoji_pages,
                                format_pages, format_user_pages, load_cache,
                                make_emoji_records, match_count, merge_records,
                                parse_emoji, parse_emojis, past_year_start,
                                period_start, rank_records, reaction_key,
                                save_cache, scan_channel, scan_channel_emojis,
                                scan_channel_multi, year_start)


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


class CacheHelperTests(unittest.TestCase):
    def rec(self, mid, count, day):
        return {"message_id": mid, "count": count,
                "created_at": datetime(2026, 3, day, tzinfo=timezone.utc)}

    def test_emoji_key(self):
        self.assertEqual(emoji_key(parse_emoji("⭐")), "unicode:⭐")
        self.assertEqual(emoji_key(parse_emoji("<:typst:1558680485427679422>")),
                         "custom:1558680485427679422")

    def test_parse_emojis_multi_dedupe_invalid(self):
        valid, invalid = parse_emojis("⭐ 🔥,⭐ abc")
        self.assertEqual([str(e) for e in valid], ["⭐", "🔥"])
        self.assertEqual(invalid, ["abc"])

    def test_merge_window_replace_and_drop(self):
        cached = [self.rec(1, 5, 1), self.rec(2, 3, 10), self.rec(3, 9, 20)]
        fresh = [self.rec(2, 8, 10), self.rec(4, 1, 25)]
        window = datetime(2026, 3, 5, tzinfo=timezone.utc)
        merged = merge_records(cached, fresh, window)
        by_id = {r["message_id"]: r["count"] for r in merged}
        # 1: 窓より前なので保持 / 2: fresh で置換 / 3: 窓内だが fresh に無いので消去 / 4: 新規
        self.assertEqual(by_id, {1: 5, 2: 8, 4: 1})

    def test_merge_full_replace(self):
        merged = merge_records([self.rec(1, 5, 1)], [self.rec(2, 3, 2)], None)
        self.assertEqual([r["message_id"] for r in merged], [2])

    def test_cache_covers(self):
        jan1 = datetime(2026, 1, 1, tzinfo=JST)
        entry_all = {"coverage_start": None}
        entry_year = {"coverage_start": jan1}
        self.assertTrue(cache_covers(entry_all, None))
        self.assertTrue(cache_covers(entry_all, jan1))
        self.assertTrue(cache_covers(entry_year, jan1))
        self.assertFalse(cache_covers(entry_year, None))
        self.assertFalse(cache_covers(entry_year, datetime(2025, 6, 1, tzinfo=JST)))
        self.assertFalse(cache_covers(None, jan1))

    def test_filter_records(self):
        records = [self.rec(1, 5, 1), self.rec(2, 3, 10)]
        start = datetime(2026, 3, 5, tzinfo=timezone.utc)
        self.assertEqual([r["message_id"] for r in filter_records(records, start)], [2])
        self.assertEqual(len(filter_records(records, None)), 2)

    def test_save_load_roundtrip(self):
        import tempfile
        from pathlib import Path as P
        record = user_record("alice", 1, 5, 10)
        record["message_id"] = 111
        record["channel_id"] = 222
        cache = {42: {"unicode:⭐": {
            "emoji": "⭐",
            "cached_until": datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc),
            "coverage_start": None,
            "records": [record]}}}
        with tempfile.TemporaryDirectory() as d:
            path = P(d) / "cache.json"
            save_cache(path, cache)
            loaded = load_cache(path)
        entry = loaded[42]["unicode:⭐"]
        self.assertIsNone(entry["coverage_start"])
        self.assertEqual(entry["records"][0]["count"], 5)
        self.assertEqual(entry["records"][0]["created_at"], record["created_at"])
        self.assertEqual(entry["records"][0]["message_id"], 111)

    def test_load_cache_missing_file(self):
        self.assertEqual(load_cache("/nonexistent-dir/cache.json"), {})

    def test_format_note(self):
        pages = format_pages("⭐", [], "過去1年", 10, 2, note="キャッシュ併用：テスト")
        self.assertIn("キャッシュ併用：テスト", pages[0])


class ScanMultiTests(unittest.IsolatedAsyncioTestCase):
    async def test_multi_targets_and_window(self):
        star = parse_emoji("⭐")
        fire = parse_emoji("🔥")
        old = fake_message(
            reactions=[SimpleNamespace(emoji="⭐", count=2),
                       SimpleNamespace(emoji="🔥", count=3)],
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
        new = fake_message(
            reactions=[SimpleNamespace(emoji="⭐", count=5)],
            created_at=datetime(2026, 3, 1, tzinfo=timezone.utc))

        async def history(limit=None, after=None):
            for m in (old, new):
                yield m

        channel = SimpleNamespace(history=history)
        star_sink, fire_sink = [], []
        jobs = [(star, None, star_sink),
                (fire, datetime(2026, 2, 1, tzinfo=timezone.utc), fire_sink)]
        scanned = await scan_channel_multi(channel, jobs, None)
        self.assertEqual(scanned, 2)
        self.assertEqual([r["count"] for r in star_sink], [2, 5])
        self.assertEqual(fire_sink, [])  # 🔥 のヒットは窓（2/1）より前なので対象外


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


def emoji_record(emoji, key, count, day, author="alice", snippet="投稿", bot=False):
    return {"count": count,
            "created_at": datetime(2026, 1, day, tzinfo=timezone.utc),
            "author": author, "author_id": 1, "author_bot": bot,
            "channel": "general", "snippet": snippet,
            "url": "https://example.com/m",
            "emoji": emoji, "emoji_key": key}


class EmojiModeTests(unittest.TestCase):
    def test_reaction_key(self):
        self.assertEqual(reaction_key("🔥"), "unicode:🔥")
        custom = parse_emoji("<:party:123456789012345678>")
        self.assertEqual(reaction_key(custom), "custom:123456789012345678")

    def test_make_emoji_records(self):
        msg = fake_message(reactions=[
            SimpleNamespace(emoji="🔥", count=3),
            SimpleNamespace(emoji="⭐", count=7),
            SimpleNamespace(emoji=parse_emoji("<:party:123456789012345678>"), count=2),
        ])
        records = make_emoji_records(msg)
        self.assertEqual(len(records), 3)
        by_key = {r["emoji_key"]: r for r in records}
        self.assertEqual(by_key["unicode:🔥"]["count"], 3)
        self.assertEqual(by_key["unicode:⭐"]["count"], 7)
        custom = by_key["custom:123456789012345678"]
        self.assertEqual(custom["count"], 2)
        self.assertEqual(custom["emoji"], "<:party:123456789012345678>")
        self.assertEqual(make_emoji_records(fake_message()), [])

    def test_aggregate_totals_best_and_order(self):
        records = [
            emoji_record("🔥", "unicode:🔥", 3, 10),
            emoji_record("🔥", "unicode:🔥", 9, 12),
            emoji_record("⭐", "unicode:⭐", 20, 5),
            emoji_record("🎉", "unicode:🎉", 50, 1, bot=True),
        ]
        items = aggregate_emojis(records, 10)
        # Bot の投稿に付いた分も含むので 🎉 が1位
        self.assertEqual([i["emoji"] for i in items], ["🎉", "⭐", "🔥"])
        fire = items[2]
        self.assertEqual(fire["total"], 12)
        self.assertEqual(fire["messages"], 2)
        self.assertEqual(fire["best"]["count"], 9)

    def test_aggregate_tie_breaks_by_first_hit(self):
        records = [
            emoji_record("🔥", "unicode:🔥", 10, 20),
            emoji_record("⭐", "unicode:⭐", 10, 3),
        ]
        items = aggregate_emojis(records, 10)
        self.assertEqual(items[0]["emoji"], "⭐")

    def test_aggregate_limit(self):
        records = [emoji_record(f"e{i}", f"unicode:e{i}", 10 - i, 1)
                   for i in range(5)]
        self.assertEqual(len(aggregate_emojis(records, 2)), 2)


class EmojiFormatTests(unittest.TestCase):
    def test_emoji_pages_shape(self):
        records = [
            emoji_record("⭐", "unicode:⭐", 5, 10, snippet="一番の投稿"),
            emoji_record("⭐", "unicode:⭐", 7, 12),
            emoji_record("🔥", "unicode:🔥", 3, 5),
        ]
        items = aggregate_emojis(records, 10)
        pages = format_emoji_pages(items, "過去1年", 500, 4)
        self.assertEqual(len(pages), 1)
        self.assertIn("## リアクション 絵文字ランキング（過去1年）", pages[0])
        self.assertIn("### 1位　⭐×12", pages[0])
        self.assertIn("対象メッセージ2件", pages[0])
        self.assertIn("最多のメッセージ：⭐×7", pages[0])

    def test_emoji_pages_empty(self):
        pages = format_emoji_pages([], "過去1年", 10, 2)
        self.assertEqual(len(pages), 1)
        self.assertIn("見つからなかった", pages[0])


class ScanEmojisTests(unittest.IsolatedAsyncioTestCase):
    async def test_scan_collects_all_emojis(self):
        multi = fake_message(reactions=[
            SimpleNamespace(emoji="⭐", count=4),
            SimpleNamespace(emoji="🔥", count=2),
        ])
        none = fake_message()

        async def history(limit=None, after=None):
            for m in (multi, none):
                yield m

        channel = SimpleNamespace(history=history)
        records = []
        scanned = await scan_channel_emojis(channel, None, records)
        self.assertEqual(scanned, 2)
        self.assertEqual(len(records), 2)
        self.assertEqual({r["emoji_key"] for r in records},
                         {"unicode:⭐", "unicode:🔥"})


if __name__ == "__main__":
    unittest.main()
