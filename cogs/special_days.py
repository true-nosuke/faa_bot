"""特曜日 notifications. No external calendar fetch and no interaction with affinity/tasks."""

from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

from .special_days_store import (
    GRACE,
    MAX_CSV_BYTES,
    SLOTS,
    Store,
    now_jst,
    parse_csv,
    parse_date,
    scheduled,
)

LOGGER = logging.getLogger(__name__)
NO_MENTIONS = discord.AllowedMentions.none()
# User-provided initial destination. Existing /tokuyo settings always take priority.
DEFAULT_PUBLIC_CHANNEL_ID = 1510542025110458428


def display_events(day, events):
    if not events:
        return f"📅 {day} の特曜日予定は取り消されました。"
    periods = "・".join(f"特曜{e['period']}" for e in events)
    notes = "\n".join(
        f"{e['period']}: {discord.utils.escape_markdown(e['note'])}"
        for e in events
        if e["note"]
    )
    return (
        f"📅 特曜日のお知らせ\n対象日：{day}\n{periods}\n時間割・持ち物は学校の案内を確認してください。"
        + (f"\n{notes}" if notes else "")
    )


async def require_member(bot, guild_id, user_id):
    guild = bot.get_guild(guild_id)
    if guild is None:
        raise ValueError("サーバーが利用できません。")
    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except discord.NotFound as exc:
            raise ValueError("このサーバーのメンバーのみ利用できます。") from exc
    return member


class PublicView(discord.ui.View):
    def __init__(self, cog, guild_id, day):
        super().__init__(timeout=None)
        self.cog, self.guild_id, self.day = cog, guild_id, day
        button = discord.ui.Button(
            label="個別DMを設定",
            style=discord.ButtonStyle.primary,
            custom_id=f"tokuyo:open:{guild_id}:{day}",
        )
        button.callback = self.open_panel
        self.add_item(button)

    async def open_panel(self, interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            if interaction.guild_id != self.guild_id:
                raise ValueError("対象のサーバーが違います。")
            await require_member(self.cog.bot, self.guild_id, interaction.user.id)
            events = self.cog.store.events(self.day)
            if not events or scheduled(self.day, 9) <= now_jst():
                raise ValueError("この日のDM設定受付は終了しています。")
            view = PersonalView(self.cog, self.guild_id, self.day, interaction.user.id)
            await interaction.followup.send(
                view.text(), view=view, ephemeral=True, allowed_mentions=NO_MENTIONS
            )
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        except discord.HTTPException:
            await interaction.followup.send(
                "メンバー情報を確認できません。後でもう一度お試しください。",
                ephemeral=True,
            )


class PersonalView(discord.ui.View):
    def __init__(self, cog, guild_id, day, user_id):
        super().__init__(timeout=900)
        self.cog, self.guild_id, self.day, self.user_id = cog, guild_id, day, user_id
        self.lock = asyncio.Lock()
        for slot, label in enumerate(SLOTS):
            button = discord.ui.Button(
                label=label, custom_id=f"tokuyo:slot:{slot}", row=slot // 5
            )

            async def callback(interaction, slot=slot):
                await self.toggle(interaction, slot)

            button.callback = callback
            self.add_item(button)
        self.refresh()

    def refresh(self):
        selected = self.cog.store.selections(self.guild_id, self.day, self.user_id)
        now = now_jst()
        for slot, button in enumerate(self.children):
            button.style = (
                discord.ButtonStyle.success
                if slot in selected
                else discord.ButtonStyle.secondary
            )
            button.label = f"{'ON' if slot in selected else 'OFF'} {SLOTS[slot]}"
            button.disabled = scheduled(self.day, slot) <= now

    def text(self):
        return (
            f"**{self.day} の個別DM設定**（日本時間）\n"
            "各ボタンでON/OFF。複数選択できます。変更は押すたびに保存されます。\n"
            "前日24:00＝当日0:00。他の日には引き継ぎません。すべてOFFならDMなし。\n"
            "DM拒否などで届かない場合があります。失敗しても公開チャンネルには転送しません。\n"
            "パネルは15分で終了します。全体通知のボタンから開き直せます。"
        )

    async def interaction_check(self, interaction):
        if interaction.user.id != self.user_id or interaction.guild_id != self.guild_id:
            await interaction.response.send_message(
                "本人専用の設定パネルです。", ephemeral=True
            )
            return False
        return True

    async def toggle(self, interaction, slot):
        await interaction.response.defer()
        async with self.lock:
            try:
                await require_member(self.cog.bot, self.guild_id, self.user_id)
                # Serialize event mutations and sends within this process.
                async with self.cog.lock:
                    self.cog.store.toggle(
                        self.guild_id, self.day, self.user_id, slot, now_jst()
                    )
                self.refresh()
                await interaction.edit_original_response(content=self.text(), view=self)
            except ValueError as exc:
                await interaction.followup.send(str(exc), ephemeral=True)
            except discord.HTTPException:
                await interaction.followup.send(
                    "設定画面を更新できません。開き直して保存状態を確認してください。",
                    ephemeral=True,
                )


class ImportView(discord.ui.View):
    def __init__(self, cog, user_id, records):
        super().__init__(timeout=300)
        self.cog, self.user_id, self.records = cog, user_id, records
        self.used = False
        self.lock = asyncio.Lock()

    async def interaction_check(self, interaction):
        if (
            interaction.user.id != self.user_id
            or not interaction.guild
            or not interaction.permissions.administrator
        ):
            await interaction.response.send_message(
                "取込を開始した管理者のみ確定できます。", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(
        label="有効行を全サーバー共通の日程へ反映", style=discord.ButtonStyle.danger
    )
    async def confirm(self, interaction, button):
        await interaction.response.defer()
        async with self.lock:
            if self.used:
                return
            try:
                async with self.cog.lock:
                    self.cog.store.upsert(
                        self.records, interaction.user.id, "CSV", now_jst()
                    )
                self.used = True
                self.stop()
                await interaction.edit_original_response(
                    content=f"{len(self.records)}件を反映しました。CSVにない既存予定は削除していません。",
                    view=None,
                )
            except ValueError as exc:
                await interaction.followup.send(str(exc), ephemeral=True)

    @discord.ui.button(label="中止", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        async with self.lock:
            self.used = True
            self.stop()
            await interaction.response.edit_message(
                content="取込を中止しました。日程は変更していません。", view=None
            )


@app_commands.guild_only()
class SpecialDays(
    commands.GroupCog,
    group_name="tokuyo",
    group_description="深志高校の特曜日・DMリマインダー",
):
    def __init__(self, bot):
        self.bot = bot
        self.store = Store()
        self.lock = asyncio.Lock()

    async def cog_load(self):
        for row in self.store.public_messages():
            if (
                self.store.events(row["target_date"])
                and scheduled(row["target_date"], 9) > now_jst()
            ):
                self.bot.add_view(
                    PublicView(self, row["guild_id"], row["target_date"]),
                    message_id=row["message_id"],
                )
        self.dispatch.start()

    def cog_unload(self):
        self.dispatch.cancel()

    @app_commands.command(
        name="add", description="全サーバー共通の日程を登録・更新（管理者）"
    )
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.default_permissions(administrator=True)
    @app_commands.choices(
        period=[
            app_commands.Choice(name="午前（A特）", value="午前"),
            app_commands.Choice(name="午後（P特）", value="午後"),
        ]
    )
    async def add(
        self,
        interaction: discord.Interaction,
        day: str,
        period: app_commands.Choice[str],
        note: app_commands.Range[str, 0, 100] = "",
    ):
        await interaction.response.defer(ephemeral=True)
        try:
            day = parse_date(day).isoformat()
            async with self.lock:
                self.store.upsert(
                    [(day, period.value, note)], interaction.user.id, "手動", now_jst()
                )
            await interaction.followup.send(
                f"全サーバー共通：{day} 特曜{period.value}を登録・更新しました。\n前日16:00の通知は10分以内の遅延のみ送信。過ぎて追加した場合は自動送信しません。",
                ephemeral=True,
            )
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)

    @app_commands.command(
        name="import", description="全サーバー共通の日程へCSV取込（プレビュー後に確定）"
    )
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.default_permissions(administrator=True)
    async def import_csv(
        self, interaction: discord.Interaction, file: discord.Attachment
    ):
        await interaction.response.defer(ephemeral=True)
        try:
            if file.size > MAX_CSV_BYTES:
                raise ValueError("CSVは1MB以内にしてください。")
            records, errors = parse_csv(await file.read(), now_jst().date())
            existing = {
                (r["target_date"], r["period"])
                for r in self.store.future(now_jst().date())
            }
            lines = [
                "**CSV取込プレビュー（全サーバー共通）**",
                f"有効 {len(records)}件 / エラー {len(errors)}件。無効行は登録しません。",
                "同じ日付・区分は更新。CSVにない予定は削除しません。",
                "A特＝午前、P特＝午後。メモはCSVから取り込みません。",
                "同じ予定の既存メモはCSV再取込で空欄になります。",
            ]
            lines.extend(
                f"{'更新' if (d, p) in existing else '追加'} {d} 特曜{p}"
                for d, p, n in records
            )
            lines.extend(errors)
            data = "\n".join(lines)
            # Full preview is always provided, not silently truncated.
            if len(data) > 1800:
                import io

                await interaction.followup.send(
                    "全件のプレビューを添付しました。内容を確認してから確定してください。",
                    file=discord.File(
                        io.BytesIO(data.encode("utf-8")), filename="tokuyo-preview.txt"
                    ),
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    data, ephemeral=True, allowed_mentions=NO_MENTIONS
                )
            if records:
                await interaction.followup.send(
                    "有効行だけを反映しますか？（5分以内）",
                    view=ImportView(self, interaction.user.id, records),
                    ephemeral=True,
                )
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        except discord.HTTPException:
            await interaction.followup.send(
                "CSVを取得・表示できませんでした。再実行してください。", ephemeral=True
            )

    @app_commands.command(name="list", description="今後の特曜日を日付順に表示")
    async def list_events(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        rows = self.store.future(now_jst().date())
        lines = [
            f"#{r['id']} {r['target_date']} 特曜{r['period']} {discord.utils.escape_markdown(r['note'])}"
            for r in rows
        ]
        text = (
            "\n".join(lines)
            or "登録済みの今後の特曜日はありません。未登録の日が通常日とは限りません。"
        )
        if len(text) > 1800:
            import io

            await interaction.followup.send(
                "今後の日程一覧",
                file=discord.File(
                    io.BytesIO(text.encode("utf-8")), filename="tokuyo-list.txt"
                ),
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                text, ephemeral=True, allowed_mentions=NO_MENTIONS
            )

    @app_commands.command(
        name="cancel", description="全サーバー共通の日程を番号で取消（管理者）"
    )
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.default_permissions(administrator=True)
    async def cancel_event(self, interaction: discord.Interaction, event_id: int):
        await interaction.response.defer(ephemeral=True)
        try:
            async with self.lock:
                day = self.store.cancel(event_id, now_jst())
            await interaction.followup.send(
                f"全サーバー共通：{day}の予定 #{event_id}を取り消しました。\n通知済みの本文は次の確認時に訂正します。DMでの訂正・追加メンションは送りません。",
                ephemeral=True,
            )
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)

    @app_commands.command(
        name="settings", description="このサーバーの通知先とON/OFFを設定（管理者）"
    )
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.default_permissions(administrator=True)
    async def settings(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        enabled: bool = True,
        role: discord.Role | None = None,
    ):
        if channel.guild.id != interaction.guild_id or (
            role and role.guild.id != interaction.guild_id
        ):
            await interaction.response.send_message(
                "同じサーバーのチャンネル・ロールを指定してください。", ephemeral=True
            )
            return
        perms = channel.permissions_for(interaction.guild.me)
        if not (
            perms.view_channel and perms.send_messages and perms.read_message_history
        ):
            await interaction.response.send_message(
                "Botに閲覧・送信・履歴参照権限が必要です。", ephemeral=True
            )
            return
        if role and (
            role.is_default() or (not role.mentionable and not perms.mention_everyone)
        ):
            await interaction.response.send_message(
                "@everyoneは指定不可です。通知用ロールはメンション可能にしてください。",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        async with self.lock:
            self.store.configure(
                interaction.guild_id, channel.id, role.id if role else None, enabled
            )
        await interaction.followup.send(
            f"通知を{'ON' if enabled else 'OFF'}にしました。全体通知は前日16:00（日本時間）。\nロールは{'指定あり' if role else 'なし'}。OFF中は個別DMも停止します。過ぎた通知の再送はしません。",
            ephemeral=True,
        )

    @app_commands.command(
        name="status", description="このサーバーの設定と直近の送信失敗を表示（管理者）"
    )
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.default_permissions(administrator=True)
    async def status(self, interaction: discord.Interaction):
        settings = self.store.settings(interaction.guild_id)
        failures = self.store.rows(
            "SELECT status,COUNT(*) AS n FROM special_day_notifications WHERE guild_id=? AND status IN ('failed','missed','sending','correction_failed') GROUP BY status",
            (interaction.guild_id,),
        )
        text = (
            f"通知設定：{settings[0] if settings else '未設定'}\n"
            + "\n".join(f"{r['status']}: {r['n']}件" for r in failures)
            + "\nsendingのままの通知はクラッシュ等で送信結果不明です。重複防止のため自動再送しません。"
        )
        await interaction.response.send_message(
            text, ephemeral=True, allowed_mentions=NO_MENTIONS
        )

    async def cog_app_command_error(self, interaction, error):
        text = (
            "管理者権限が必要です。"
            if isinstance(error, app_commands.CheckFailure)
            else "処理に失敗しました。管理者へ連絡してください。"
        )
        if not isinstance(error, app_commands.CheckFailure):
            LOGGER.error(
                "tokuyo command failed",
                exc_info=(type(error), error, error.__traceback__),
            )
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)

    @tasks.loop(minutes=1)
    async def dispatch(self):
        try:
            async with self.lock:
                await self.tick(now_jst())
        except Exception:
            LOGGER.exception("tokuyo dispatch failed; next tick will continue")

    async def initialize_default_channel(self):
        """Resolve the guild from the destination; never guess or overwrite settings."""
        try:
            channel = self.bot.get_channel(DEFAULT_PUBLIC_CHANNEL_ID)
            if channel is None:
                channel = await self.bot.fetch_channel(DEFAULT_PUBLIC_CHANNEL_ID)
            if not isinstance(channel, discord.TextChannel):
                LOGGER.warning("tokuyo initial destination is not a text channel")
                return
            if self.store.settings(channel.guild.id):
                return
            permissions = channel.permissions_for(channel.guild.me)
            if not (
                permissions.view_channel
                and permissions.send_messages
                and permissions.read_message_history
            ):
                LOGGER.warning(
                    "tokuyo initial destination lacks required Bot permissions"
                )
                return
            self.store.configure(channel.guild.id, channel.id, None, True)
            LOGGER.info("tokuyo initial destination configured: %s", channel.id)
        except discord.HTTPException:
            LOGGER.warning(
                "tokuyo initial destination could not be resolved; use /tokuyo settings"
            )

    @dispatch.before_loop
    async def before_dispatch(self):
        await self.bot.wait_until_ready()
        await self.initialize_default_channel()

    async def tick(self, now):
        # Reconcile already-published messages first (cancellations/notes/period changes).
        for row in self.store.public_messages():
            events = self.store.events(row["target_date"])
            snapshot = display_events(row["target_date"], events)
            if snapshot != row["snapshot"]:
                try:
                    channel = self.bot.get_channel(
                        row["channel_id"]
                    ) or await self.bot.fetch_channel(row["channel_id"])
                    message = await channel.fetch_message(row["message_id"])
                    view = (
                        PublicView(self, row["guild_id"], row["target_date"])
                        if events and scheduled(row["target_date"], 9) > now
                        else None
                    )
                    await message.edit(
                        content=snapshot, view=view, allowed_mentions=NO_MENTIONS
                    )
                    self.store.finish(
                        row["guild_id"],
                        row["target_date"],
                        0,
                        -1,
                        "sent",
                        now,
                        channel.id,
                        message.id,
                        snapshot,
                    )
                except discord.NotFound:
                    self.store.finish(
                        row["guild_id"],
                        row["target_date"],
                        0,
                        -1,
                        "deleted",
                        now,
                        error="message/channel deleted",
                    )
                except discord.HTTPException as exc:
                    self.store.finish(
                        row["guild_id"],
                        row["target_date"],
                        0,
                        -1,
                        "correction_failed",
                        now,
                        row["channel_id"],
                        row["message_id"],
                        row["snapshot"],
                        type(exc).__name__,
                    )
                    LOGGER.warning(
                        "tokuyo public correction failed: %s/%s",
                        row["guild_id"],
                        row["target_date"],
                    )
        dates = sorted({r["target_date"] for r in self.store.future(now.date())})
        for setting in self.store.settings():
            if not setting["enabled"]:
                continue
            guild_id = setting["guild_id"]
            if self.bot.get_guild(guild_id) is None:
                continue
            for day in dates:
                await self.deliver(setting, day, 0, -1, now)
            for sub in self.store.subscriptions(guild_id, now.date()):
                await self.deliver(
                    setting, sub["target_date"], sub["user_id"], sub["slot"], now
                )

    async def deliver(self, setting, day, user_id, slot, now):
        due = scheduled(day, slot)
        if now < due:
            return
        guild_id = setting["guild_id"]
        if not self.store.claim(guild_id, day, user_id, slot, now):
            return
        if now - due > GRACE:
            self.store.finish(guild_id, day, user_id, slot, "missed", now)
            return
        events = self.store.events(day)
        snapshot = display_events(day, events)
        try:
            if user_id:
                member = await require_member(self.bot, guild_id, user_id)
                message = await member.send(
                    snapshot + f"\n選択した通知時刻：{SLOTS[slot]}（日本時間）",
                    allowed_mentions=NO_MENTIONS,
                )
            else:
                channel = self.bot.get_channel(
                    setting["channel_id"]
                ) or await self.bot.fetch_channel(setting["channel_id"])
                if channel.guild.id != guild_id:
                    raise ValueError("通知先サーバーが違います。")
                role_id = setting["mention_role_id"]
                role = (
                    self.bot.get_guild(guild_id).get_role(role_id) if role_id else None
                )
                prefix = f"{role.mention}\n" if role and not role.is_default() else ""
                message = await channel.send(
                    prefix + "明日の予定です。\n" + snapshot,
                    view=PublicView(self, guild_id, day),
                    allowed_mentions=discord.AllowedMentions(
                        everyone=False,
                        users=False,
                        roles=[role] if role and not role.is_default() else [],
                        replied_user=False,
                    ),
                )
            self.store.finish(
                guild_id,
                day,
                user_id,
                slot,
                "sent",
                now,
                message.channel.id,
                message.id,
                snapshot,
            )
        except (discord.HTTPException, ValueError) as exc:
            # No public fallback and no unbounded retries, including DM Forbidden.
            self.store.finish(
                guild_id, day, user_id, slot, "failed", now, error=type(exc).__name__
            )
            LOGGER.warning(
                "tokuyo send failed: guild=%s day=%s kind=%s",
                guild_id,
                day,
                "DM" if user_id else "public",
            )


async def setup(bot):
    await bot.add_cog(SpecialDays(bot))
