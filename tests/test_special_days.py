import asyncio
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
from discord import app_commands

from cogs.special_days import ImportView, PersonalView, PublicView, SpecialDays
from cogs.special_days_store import JST, SLOTS, Store, parse_csv, parse_date, scheduled

DAY = "2026-11-10"
NOW = datetime(2026, 11, 9, 16, 0, tzinfo=JST)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "bot.db"
        self.s = Store(self.path)
        self.s.upsert([(DAY, "午前", "")], 123, "手動", NOW)

    def tearDown(self):
        self.tmp.cleanup()

    def test_slots_and_midnight(self):
        self.assertEqual(len(SLOTS), 10)
        times = [scheduled(DAY, s) for s in range(10)]
        self.assertEqual(len(set(times)), 10)
        self.assertEqual([t.hour for t in times], [18, 19, 20, 21, 22, 23, 0, 6, 7, 8])
        self.assertEqual(times[6].date(), date(2026, 11, 10))
        self.assertEqual(scheduled(DAY, -1), NOW)
        with self.assertRaises(ValueError):
            scheduled(DAY, 10)

    def test_csv_mapping_errors_and_duplicate(self):
        data = b"Subject,Start Date\nA\xe7\x89\xb9,11/10/2026\nP\xe7\x89\xb9,2026/11/10\nother,2026/11/11\nA\xe7\x89\xb9,not-a-date\nA\xe7\x89\xb9,11/10/2026\nA\xe7\x89\xb9,2025/01/01\n"
        records, errors = parse_csv(data, NOW.date())
        self.assertEqual({r[1] for r in records}, {"午前", "午後"})
        self.assertEqual(len(errors), 4)
        self.assertTrue(any(e.startswith("4行目") for e in errors))

    def test_bom_cp932_and_dates(self):
        text = "Subject,Start Date\nA特,2026/11/10\n"
        for encoding in ["utf-8-sig", "cp932"]:
            self.assertEqual(len(parse_csv(text.encode(encoding), NOW.date())[0]), 1)
        self.assertEqual(parse_date("2026年11月10日").isoformat(), DAY)
        with self.assertRaises(ValueError):
            parse_date("2026/02/30")

    def test_csv_bounds_and_header(self):
        for data in [
            b"x" * (1024 * 1024 + 1),
            b"foo,bar\n",
            b"Subject,Start Date\n" + b"foo,bar\n" * 1001,
            b'Subject,Start Date\n"unterminated',
        ]:
            with self.assertRaises(ValueError):
                parse_csv(data, NOW.date())

    def test_upsert_no_duplicate_no_delete(self):
        self.s.upsert([(DAY, "午後", "a")], 1, "CSV", NOW)
        self.s.upsert([(DAY, "午後", "b")], 1, "CSV", NOW)
        self.assertEqual(len(self.s.events(DAY)), 2)
        self.assertEqual(self.s.events(DAY)[1]["note"], "b")

    def test_import_atomic_and_past_invalid(self):
        with self.assertRaises(ValueError):
            self.s.upsert(
                [(DAY, "午後", ""), ("2020-01-01", "午前", "")], 1, "CSV", NOW
            )
        self.assertEqual(len(self.s.events(DAY)), 1)

    def test_multiple_independent_user_guild_date_settings(self):
        for slot in [0, 6, 9]:
            self.s.toggle(1, DAY, 10, slot, NOW)
        self.assertEqual(self.s.selections(1, DAY, 10), {0, 6, 9})
        self.s.toggle(1, DAY, 10, 0, NOW)
        self.assertEqual(self.s.selections(1, DAY, 10), {6, 9})
        self.assertEqual(self.s.selections(2, DAY, 10), set())
        self.assertEqual(self.s.selections(1, DAY, 11), set())
        self.assertEqual(self.s.selections(1, "2026-11-11", 10), set())

    def test_past_slot_rejected(self):
        with self.assertRaises(ValueError):
            self.s.toggle(1, DAY, 10, 0, scheduled(DAY, 0))

    def test_cancel_partial_then_all(self):
        self.s.upsert([(DAY, "午後", "")], 1, "CSV", NOW)
        self.s.toggle(1, DAY, 10, 0, NOW)
        rows = self.s.events(DAY)
        self.s.cancel(rows[0]["id"], NOW)
        self.assertEqual(self.s.selections(1, DAY, 10), {0})
        self.s.cancel(rows[1]["id"], NOW)
        self.assertEqual(self.s.selections(1, DAY, 10), set())
        self.assertFalse(self.s.claim(1, DAY, 0, -1, NOW))
        with self.assertRaises(ValueError):
            self.s.toggle(1, DAY, 10, 1, NOW)

    def test_persistence_and_claim_once_even_after_crash(self):
        self.assertTrue(self.s.claim(1, DAY, 0, -1, NOW))
        restarted = Store(self.path)
        self.assertFalse(restarted.claim(1, DAY, 0, -1, NOW))
        self.assertEqual(
            restarted.rows("SELECT status FROM special_day_notifications")[0]["status"],
            "sending",
        )

    def test_no_claim_for_disabled_sub(self):
        self.s.toggle(1, DAY, 10, 0, NOW)
        self.s.toggle(1, DAY, 10, 0, NOW)
        self.assertFalse(self.s.claim(1, DAY, 10, 0, NOW))

    def test_processed_dm_cannot_reenable(self):
        self.s.toggle(1, DAY, 10, 0, NOW)
        self.assertTrue(self.s.claim(1, DAY, 10, 0, NOW))
        self.s.toggle(1, DAY, 10, 0, NOW)
        with self.assertRaises(ValueError):
            self.s.toggle(1, DAY, 10, 0, NOW)

    def test_existing_task_tables_untouched(self):
        with sqlite3.connect(self.path) as c:
            c.execute("CREATE TABLE tasks(id INTEGER,name TEXT)")
            c.execute("INSERT INTO tasks VALUES(1,'existing')")
        Store(self.path)
        self.s.cancel(self.s.events(DAY)[0]["id"], NOW)
        self.assertEqual(
            self.s.rows("SELECT * FROM tasks"), [{"id": 1, "name": "existing"}]
        )


class DiscordTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "bot.db")
        self.store.upsert([(DAY, "午前", ""), (DAY, "午後", "")], 1, "CSV", NOW)
        self.store.configure(100, 200, None, True)
        self.message = SimpleNamespace(
            id=300, channel=SimpleNamespace(id=200), edit=AsyncMock()
        )
        self.member = SimpleNamespace(send=AsyncMock(return_value=self.message))
        self.guild = SimpleNamespace(
            get_member=Mock(return_value=self.member),
            fetch_member=AsyncMock(),
            get_role=Mock(return_value=None),
        )
        self.channel = SimpleNamespace(
            id=200,
            guild=SimpleNamespace(id=100),
            send=AsyncMock(return_value=self.message),
            fetch_message=AsyncMock(return_value=self.message),
        )
        self.bot = SimpleNamespace(
            get_guild=Mock(return_value=self.guild),
            get_channel=Mock(return_value=self.channel),
            fetch_channel=AsyncMock(),
            add_view=Mock(),
        )
        # Don't construct a production Store or start background tasks in unit tests.
        self.cog = object.__new__(SpecialDays)
        self.cog.bot, self.cog.store, self.cog.lock = (
            self.bot,
            self.store,
            asyncio.Lock(),
        )
        self.setting = self.store.settings(100)[0]

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def interaction(self, user_id=10, guild_id=100, admin=False):
        return SimpleNamespace(
            user=SimpleNamespace(id=user_id),
            guild_id=guild_id,
            guild=SimpleNamespace(id=guild_id),
            permissions=SimpleNamespace(administrator=admin),
            response=SimpleNamespace(
                defer=AsyncMock(), send_message=AsyncMock(), edit_message=AsyncMock()
            ),
            followup=SimpleNamespace(send=AsyncMock()),
            edit_original_response=AsyncMock(),
        )

    async def test_public_grouped_once_no_everyone(self):
        await self.cog.tick(NOW)
        await self.cog.tick(NOW + timedelta(minutes=1))
        self.channel.send.assert_awaited_once()
        args = self.channel.send.call_args
        self.assertIn("特曜午前", args.args[0])
        self.assertIn("特曜午後", args.args[0])
        self.assertFalse(args.kwargs["allowed_mentions"].everyone)
        self.assertTrue(args.kwargs["view"].is_persistent())

    async def test_dm_multiple_slots_no_public_fallback(self):
        for slot in [0, 1, 6]:
            self.store.toggle(100, DAY, 10, slot, NOW)
        for slot in [0, 1, 6]:
            await self.cog.deliver(self.setting, DAY, 10, slot, scheduled(DAY, slot))
        self.assertEqual(self.member.send.await_count, 3)
        self.channel.send.assert_not_awaited()
        await self.cog.deliver(self.setting, DAY, 10, 0, scheduled(DAY, 0))
        self.assertEqual(self.member.send.await_count, 3)

    async def test_dm_failure_not_public_not_retried(self):
        self.store.toggle(100, DAY, 10, 0, NOW)
        self.member.send.side_effect = discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"),
            {"message": "closed", "code": 50007},
        )
        await self.cog.deliver(self.setting, DAY, 10, 0, scheduled(DAY, 0))
        await self.cog.deliver(self.setting, DAY, 10, 0, scheduled(DAY, 0))
        self.member.send.assert_awaited_once()
        self.channel.send.assert_not_awaited()
        self.assertEqual(
            self.store.rows("SELECT status FROM special_day_notifications")[0][
                "status"
            ],
            "failed",
        )

    async def test_late_missed_and_10min_grace(self):
        await self.cog.deliver(self.setting, DAY, 0, -1, NOW + timedelta(minutes=11))
        self.channel.send.assert_not_awaited()
        self.assertEqual(
            self.store.rows("SELECT status FROM special_day_notifications")[0][
                "status"
            ],
            "missed",
        )
        self.store.configure(101, 200, None, True)
        await self.cog.deliver(
            self.store.settings(101)[0], DAY, 0, -1, NOW + timedelta(minutes=10)
        )
        # Target channel guild mismatch blocks delivery.
        self.assertEqual(
            self.store.rows(
                "SELECT status FROM special_day_notifications WHERE guild_id=101"
            )[0]["status"],
            "failed",
        )

    async def test_future_not_sent(self):
        await self.cog.tick(NOW - timedelta(minutes=1))
        self.channel.send.assert_not_awaited()
        self.assertEqual(self.store.rows("SELECT * FROM special_day_notifications"), [])

    async def test_settings_off_stops_public_and_dm(self):
        self.store.toggle(100, DAY, 10, 0, NOW)
        self.store.configure(100, 200, None, False)
        await self.cog.tick(scheduled(DAY, 0))
        self.member.send.assert_not_awaited()
        self.channel.send.assert_not_awaited()

    async def test_departed_member_no_dm(self):
        self.store.toggle(100, DAY, 10, 0, NOW)
        self.guild.get_member.return_value = None
        self.guild.fetch_member.side_effect = discord.NotFound(
            SimpleNamespace(status=404, reason="Not Found"), {"message": "missing"}
        )
        await self.cog.deliver(self.setting, DAY, 10, 0, scheduled(DAY, 0))
        self.member.send.assert_not_awaited()
        self.channel.send.assert_not_awaited()

    async def test_public_cancellation_edited_without_mention(self):
        await self.cog.tick(NOW)
        for r in self.store.events(DAY):
            self.store.cancel(r["id"], NOW)
        await self.cog.tick(NOW + timedelta(minutes=1))
        self.message.edit.assert_awaited_once()
        self.assertIn("取り消されました", self.message.edit.call_args.kwargs["content"])
        self.assertIsNone(self.message.edit.call_args.kwargs["view"])
        self.assertFalse(
            self.message.edit.call_args.kwargs["allowed_mentions"].everyone
        )
        self.channel.send.assert_awaited_once()

    async def test_partial_cancellation_updates_retained_period(self):
        await self.cog.tick(NOW)
        self.store.cancel(self.store.events(DAY)[0]["id"], NOW)
        await self.cog.tick(NOW + timedelta(minutes=1))
        text = self.message.edit.call_args.kwargs["content"]
        self.assertNotIn("特曜午前", text)
        self.assertIn("特曜午後", text)

    async def test_personal_view_owner_check(self):
        panel = PersonalView(self.cog, 100, DAY, 10)
        wrong = self.interaction(user_id=11)
        self.assertFalse(await panel.interaction_check(wrong))
        self.assertTrue(await panel.interaction_check(self.interaction()))
        self.assertEqual(len(panel.children), 10)

    async def test_import_owner_admin_and_one_shot(self):
        panel = ImportView(self.cog, 10, [(DAY, "午後", "updated")])
        self.assertFalse(await panel.interaction_check(self.interaction(admin=False)))
        self.assertFalse(
            await panel.interaction_check(self.interaction(user_id=11, admin=True))
        )
        interaction = self.interaction(admin=True)
        self.assertTrue(await panel.interaction_check(interaction))
        await panel.confirm.callback(interaction)
        await panel.confirm.callback(interaction)
        self.assertEqual(self.store.events(DAY)[1]["note"], "updated")
        interaction.edit_original_response.assert_awaited_once()

    async def test_admin_commands_have_runtime_checks(self):
        for command in SpecialDays.__cog_app_commands__:
            if command.name in ("list",):
                continue
            self.assertTrue(command.checks, command.name)
            with self.assertRaises(app_commands.MissingPermissions):
                await command.checks[0](self.interaction(admin=False))

    async def test_grace_boundary_sends(self):
        await self.cog.deliver(self.setting, DAY, 0, -1, NOW + timedelta(minutes=10))
        self.channel.send.assert_awaited_once()

    async def test_correction_failure_bounded(self):
        await self.cog.tick(NOW)
        self.store.cancel(self.store.events(DAY)[0]["id"], NOW)
        self.message.edit.side_effect = discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"), {"message": "denied"}
        )
        await self.cog.tick(NOW + timedelta(minutes=1))
        await self.cog.tick(NOW + timedelta(minutes=2))
        self.message.edit.assert_awaited_once()
        self.assertEqual(
            self.store.rows("SELECT status FROM special_day_notifications")[0][
                "status"
            ],
            "correction_failed",
        )

    async def test_cog_registration_and_dm_restriction(self):
        from discord.ext import commands

        bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())
        with patch("cogs.special_days.Store", return_value=self.store):
            cog = SpecialDays(bot)
        async with bot:
            await bot.add_cog(cog)
            group = bot.tree.get_command("tokuyo")
            self.assertIsNotNone(group)
            data = group.to_dict(bot.tree)
            self.assertFalse(data["dm_permission"])
            self.assertEqual(
                {c["name"] for c in data["options"]},
                {"add", "import", "list", "cancel", "settings", "status"},
            )
            await bot.remove_cog(cog.qualified_name)

    async def test_message_view_ids_stable(self):
        a = PublicView(self.cog, 100, DAY)
        b = PublicView(self.cog, 100, DAY)
        self.assertEqual(a.children[0].custom_id, b.children[0].custom_id)
        self.assertNotEqual(
            a.children[0].custom_id,
            PublicView(self.cog, 101, DAY).children[0].custom_id,
        )


if __name__ == "__main__":
    unittest.main()
