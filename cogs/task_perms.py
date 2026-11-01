from __future__ import annotations

import discord
from discord import app_commands


def is_task_admin(user) -> bool:
    """サーバー管理者（管理者権限、またはサーバー管理）か。"""
    perms = getattr(user, "guild_permissions", None)
    return bool(perms and (perms.administrator or perms.manage_guild))


def can_delete_task(user, row) -> bool:
    """担当者・作成者・サーバー管理者のどれかなら削除できる。"""
    return is_task_admin(user) or user.id in (row["owner_id"], row["creator_id"])


def deletable_tasks(store, user, guild_id: int):
    """削除できる進行中タスク。管理者はサーバー内の全タスク（自分のタスクが先）、それ以外は自分が担当・作成したもの。"""
    rows = store.query(guild_id=guild_id)
    if is_task_admin(user):
        return sorted(rows, key=lambda r: r["owner_id"] != user.id)
    return [r for r in rows if can_delete_task(user, r)]


def owner_name(guild, owner_id: int) -> str:
    member = guild.get_member(owner_id) if guild else None
    return discord.utils.escape_markdown(member.display_name) if member else str(owner_id)


async def deletable_task_autocomplete(interaction: discord.Interaction, current: str):
    """/task delete の task_id の入力候補。他の人のタスクには担当者名を付ける。"""
    store = interaction.client.get_cog("Tasks").store
    choices = []
    for r in deletable_tasks(store, interaction.user, interaction.guild_id):
        label = f"#{r['id']} {r['name']}（{r['percent']}%）"
        if r["owner_id"] != interaction.user.id:
            label += f" 担当:{owner_name(interaction.guild, r['owner_id'])}"
        if current and current not in label:
            continue
        choices.append(app_commands.Choice(name=label[:100], value=r["id"]))
    return choices[:25]
