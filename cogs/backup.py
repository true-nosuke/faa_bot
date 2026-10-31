from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

from .operations import create_backup

LOGGER = logging.getLogger(__name__)
DEFAULT_LIMIT = 10 * 1024 * 1024


class Backup(commands.Cog):
    """データ（DB）のバックアップを作り、実行した本人にだけ見える形でDiscordに添付する。"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        config = getattr(bot, "config", {}) or {}
        self.config = config
        self.admin_ids = {int(x) for x in (config.get("DEPLOY_ADMIN_IDS") or "").replace(" ", "").split(",") if x.isdigit()}
        self._lock = asyncio.Lock()

    async def _is_admin(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id in self.admin_ids or await self.bot.is_owner(interaction.user)

    @app_commands.command(name="backup", description="データのバックアップ(zip)を作成し、自分にだけ見える形で送る（Bot所有者のみ）")
    @app_commands.guild_only()
    async def backup(self, interaction: discord.Interaction):
        if not await self._is_admin(interaction):
            return await interaction.response.send_message("この操作はBotの所有者だけが実行できます。", ephemeral=True)
        if self._lock.locked():
            return await interaction.response.send_message("バックアップを作成中です。少し待ってからもう一度実行してください。", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self._lock:
            try:
                path = await asyncio.to_thread(create_backup, self.config)
            except Exception:
                LOGGER.exception("手動バックアップに失敗しました")
                return await interaction.followup.send("❌ バックアップを作成できませんでした。ログを確認してください。", ephemeral=True)
            if path is None:
                return await interaction.followup.send("❌ バックアップするDBファイルが見つかりません。", ephemeral=True)
            size = path.stat().st_size
            limit = getattr(interaction.guild, "filesize_limit", DEFAULT_LIMIT)
            mb = size / 1024 / 1024
            if size > limit:
                return await interaction.followup.send(
                    f"⚠️ バックアップは作成しましたが、{mb:.1f}MBでDiscordの添付上限（{limit // 1024 // 1024}MB）を超えるため送れません。"
                    f"サーバー内の `backups/{path.name}` に保存されています。", ephemeral=True)
            try:
                await interaction.followup.send(
                    f"💾 バックアップを作成しました（{mb:.2f}MB）。\n"
                    "含まれるもの：`bot.db`（タスク・好感度など）・`messages.json`・`emoji_map.json`（絵文字キーワード）。\n"
                    "-# このメッセージとファイルは、あなたにだけ表示されています。個人情報を含むため、公開の場所には置かないでください。",
                    file=discord.File(path, filename=path.name), ephemeral=True)
            except discord.HTTPException as error:
                LOGGER.warning("バックアップを添付できませんでした: HTTP %s", error.status)
                await interaction.followup.send("⚠️ ファイルを送れませんでした。サイズや権限を確認してください。", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Backup(bot))
