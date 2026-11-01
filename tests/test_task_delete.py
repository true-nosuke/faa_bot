import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from cogs import chat_ops
from cogs.task_perms import can_delete_task, deletable_task_autocomplete, deletable_tasks
from cogs.tasks import TaskStore, Tasks

GUILD = 1


def member(uid, admin=False, manage=False, name=None):
    m = MagicMock(spec=discord.Member)
    m.id = uid
    m.display_name = name or f"user{uid}"
    m.guild_permissions = discord.Permissions(administrator=admin, manage_guild=manage)
    return m


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = TaskStore(Path(self.tmp.name) / "bot.db")
        base = dict(guild_id=GUILD, channel_id=9, name="x", due="2030-01-01", interval_days=3, ask_time="09:00",
                    next_ask_at="2030-01-01T00:00:00", created_at="2024-01-01T00:00:00")
        self.mine = self.store.add(**base | {"owner_id": 10, "name": "自分の"})                       # 担当=10 作成=10
        self.assigned = self.store.add(**base | {"owner_id": 20, "creator_id": 10, "name": "依頼した"})  # 担当=20 作成=10
        self.other = self.store.add(**base | {"owner_id": 30, "name": "他人の"})                       # 担当=30 作成=30
        self.elsewhere = self.store.add(**base | {"owner_id": 30, "guild_id": 2, "name": "別サーバー"})

    def tearDown(self):
        self.store.conn.close()
        self.tmp.cleanup()


class PermissionTests(Base):
    def test_rules(self):
        row = self.store.get(self.assigned)
        self.assertTrue(can_delete_task(member(20), row))                 # 担当者
        self.assertTrue(can_delete_task(member(10), row))                 # 作成者
        self.assertFalse(can_delete_task(member(99), row))
        self.assertTrue(can_delete_task(member(99, admin=True), row))     # 管理者権限
        self.assertTrue(can_delete_task(member(99, manage=True), row))    # サーバー管理

    def test_deletable_lists(self):
        ids = lambda u: [r["id"] for r in deletable_tasks(self.store, u, GUILD)]
        self.assertEqual(sorted(ids(member(10))), sorted([self.mine, self.assigned]))
        self.assertEqual(ids(member(20)), [self.assigned])
        self.assertEqual(ids(member(99)), [])
        admin = ids(member(30, admin=True))
        self.assertEqual(sorted(admin), sorted([self.mine, self.assigned, self.other]))
        self.assertEqual(admin[0], self.other)           # 自分のタスクが先
        self.assertNotIn(self.elsewhere, admin)          # 別サーバーは対象外


def fake_cog(store):
    cog = object.__new__(Tasks)
    cog.store = store
    cog._disable_last_message = AsyncMock()
    return cog


def interaction(user):
    i = MagicMock()
    i.user, i.guild_id = user, GUILD
    i.guild = MagicMock()
    i.guild.get_member = lambda uid: member(uid, name=f"担当{uid}")
    i.response.send_message = AsyncMock()
    return i


class SlashTests(Base):
    def run_delete(self, user, task_id):
        i = interaction(user)
        asyncio.run(Tasks.delete.callback(fake_cog(self.store), i, task_id))
        return i.response.send_message.call_args.args[0]

    def test_admin_can_delete_others(self):
        text = self.run_delete(member(99, admin=True), self.other)
        self.assertIn("削除しました", text)
        self.assertIn("担当: 担当30", text)
        self.assertIsNone(self.store.get(self.other))

    def test_creator_can_delete_assigned(self):
        self.assertIn("削除しました", self.run_delete(member(10), self.assigned))
        self.assertIsNone(self.store.get(self.assigned))

    def test_stranger_denied(self):
        self.assertIn("削除できるのは", self.run_delete(member(99), self.other))
        self.assertIsNotNone(self.store.get(self.other))

    def test_other_guild_not_found(self):
        self.assertIn("見つかりません", self.run_delete(member(99, admin=True), self.elsewhere))

    def test_autocomplete(self):
        i = interaction(member(99, admin=True))
        i.client.get_cog = lambda name: fake_cog(self.store)
        choices = asyncio.run(deletable_task_autocomplete(i, ""))
        self.assertEqual(len(choices), 3)
        self.assertTrue(any("担当:担当30" in c.name for c in choices))
        j = interaction(member(99))
        j.client.get_cog = lambda name: fake_cog(self.store)
        self.assertEqual(asyncio.run(deletable_task_autocomplete(j, "")), [])


class ChatTests(Base):
    def run_chat(self, user, specs):
        chat_ops.DELETE_CONFIRM = False
        ops = object.__new__(chat_ops.ChatOps)
        cog = fake_cog(self.store)
        ops._need = AsyncMock(return_value=cog)
        said = []
        ops._say = AsyncMock(side_effect=lambda m, t: said.append(t))
        message = MagicMock()
        message.author = user
        message.guild = MagicMock(id=GUILD)
        message.guild.get_member = lambda uid: member(uid, name=f"担当{uid}")
        message.reply = AsyncMock()
        message.channel.send = AsyncMock()
        asyncio.run(ops._do_task_delete(message, {"tasks": specs}, set()))
        return said, message

    def test_admin_deletes_others_task(self):
        said, message = self.run_chat(member(99, admin=True), [f"#{self.other}"])
        self.assertIsNone(self.store.get(self.other))
        self.assertIn("担当: 担当30", message.reply.call_args.args[0])

    def test_by_name_for_admin(self):
        self.run_chat(member(99, manage=True), ["他人の"])
        self.assertIsNone(self.store.get(self.other))

    def test_normal_user_limited(self):
        said, _ = self.run_chat(member(20), [f"{self.other}"])
        self.assertIn("削除できるタスクにありません", said[0])
        self.assertIsNotNone(self.store.get(self.other))
        said, _ = self.run_chat(member(99), ["1"])
        self.assertIn("削除できる進行中のタスクはありません", said[0])

    def test_creator_via_chat(self):
        self.run_chat(member(10), [f"{self.assigned}"])
        self.assertIsNone(self.store.get(self.assigned))

    def test_confirm_listing_shows_owner(self):
        chat_ops.DELETE_CONFIRM = True
        ops = object.__new__(chat_ops.ChatOps)
        ops._need = AsyncMock(return_value=fake_cog(self.store))
        ops._say = AsyncMock()
        message = MagicMock()
        message.author = member(99, admin=True)
        message.guild = MagicMock(id=GUILD)
        message.guild.get_member = lambda uid: member(uid, name=f"担当{uid}")
        message.reply = AsyncMock()
        asyncio.run(ops._do_task_delete(message, {"tasks": [str(self.other)]}, set()))
        self.assertIn("担当:担当30", message.reply.call_args.args[0])
        self.assertIsNotNone(self.store.get(self.other))      # 確認前は消えない


if __name__ == "__main__":
    unittest.main()
