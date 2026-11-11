"""指定した絵文字のリアクションが多いメッセージをランキング表示する。

Discord にはリアクション数でメッセージを検索する Bot 向け API が無いため、
Bot が読める全チャンネル・スレッドの履歴を走査して集計する総当たり方式。
リアクション数はメッセージ取得時に付いてくるので追加の API コールは要らない。
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands

LOGGER = logging.getLogger(__name__)

JST = ZoneInfo("Asia/Tokyo")
MAX_COUNT = 25
DEFAULT_COUNT = 10
SNIPPET_LEN = 80
CHUNK_LIMIT = 1900

CUSTOM_EMOJI_RE = re.compile(r"^<(a?):([A-Za-z0-9_]{2,32}):(\d{15,25})>$")


def parse_emoji(text: str) -> discord.PartialEmoji | None:
    """ユーザー入力（Unicode絵文字 or <:名前:ID> 形式）を PartialEmoji にする。不正なら None。"""
    text = (text or "").strip()
    if not text:
        return None
    m = CUSTOM_EMOJI_RE.match(text)
    if m:
        return discord.PartialEmoji(name=m.group(2), id=int(m.group(3)), animated=bool(m.group(1)))
    # Unicode絵文字: 短く、英数字・記号だけの文字列でないものをそのまま絵文字名として扱う
    if len(text) <= 10 and not re.search(r"[A-Za-z0-9_:\s]", text):
        return discord.PartialEmoji(name=text)
    return None


def emoji_matches(reaction_emoji, target: discord.PartialEmoji) -> bool:
    """reaction.emoji（str / Emoji / PartialEmoji）と対象絵文字が同じか。"""
    if target.id:
        return getattr(reaction_emoji, "id", None) == target.id
    if isinstance(reaction_emoji, str):
        return reaction_emoji == target.name
    return str(reaction_emoji) == target.name


def match_count(message, target: discord.PartialEmoji) -> int:
    """メッセージに付いた対象絵文字のリアクション数（無ければ0）。"""
    for reaction in getattr(message, "reactions", None) or []:
        if emoji_matches(reaction.emoji, target):
            return int(reaction.count)
    return 0


def year_start(now: datetime | None = None) -> datetime:
    """今年の範囲の下限（JSTの1月1日0時）。"""
    now = now or datetime.now(JST)
    return datetime(now.year, 1, 1, tzinfo=JST)


def rank_records(records: list[dict], limit: int) -> list[dict]:
    """個数の多い順、同数は古い順に並べて上位 limit 件を返す。"""
    return sorted(records, key=lambda r: (-r["count"], r["created_at"]))[:limit]


def make_record(message, count: int) -> dict:
    content = " ".join((message.content or "").split())
    if len(content) > SNIPPET_LEN:
        content = content[:SNIPPET_LEN] + "…"
    channel = message.channel
    return {
        "count": count,
        "created_at": message.created_at,
        "author": str(message.author),
        "channel": getattr(channel, "name", None) or str(getattr(channel, "id", "")),
        "snippet": content or "（本文なし）",
        "url": message.jump_url,
    }


def format_ranking(emoji_text: str, records: list[dict], scope_label: str,
                   scanned: int, channels: int) -> list[str]:
    """結果テキストを Discord の文字数制限に収まる塊のリストにする。"""
    header = (f"**{emoji_text} リアクションランキング（{scope_label}）**\n"
              f"走査: {channels}チャンネル・スレッド / {scanned}件のメッセージ")
    if not records:
        return [header + "\n対象のリアクションが付いたメッセージは見つからなかったよ。"]
    lines = [header]
    for i, r in enumerate(records, 1):
        date = r["created_at"].astimezone(JST).strftime("%Y/%m/%d")
        lines.append(
            f"{i}. {emoji_text}×{r['count']}　{r['author']}（#{r['channel']}・{date}）\n"
            f"{r['snippet']}\n{r['url']}")
    chunks: list[str] = []
    current = ""
    for line in lines:
        candidate = line if not current else current + "\n" + line
        if len(candidate) > CHUNK_LIMIT and current:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


async def scan_channel(channel, target: discord.PartialEmoji,
                       after: datetime | None, records: list[dict]) -> int:
    """1チャンネル（またはスレッド）を走査し、ヒットを records に追加。走査件数を返す。"""
    scanned = 0
    async for message in channel.history(limit=None, after=after):
        scanned += 1
        count = match_count(message, target)
        if count > 0:
            records.append(make_record(message, count))
    return scanned


class ReactionRank(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._running: set[int] = set()

    async def _targets(self, guild: discord.Guild):
        """Bot が履歴を読めるチャンネルとスレッドを列挙する（重複は除く）。"""
        seen: set[int] = set()
        me = guild.me

        def readable(channel) -> bool:
            try:
                perms = channel.permissions_for(me)
            except Exception:
                return False
            return perms.view_channel and perms.read_message_history

        for channel in guild.channels:
            if not hasattr(channel, "history"):
                continue
            if channel.id not in seen and readable(channel):
                seen.add(channel.id)
                yield channel
            # スレッド（フォーラムの投稿含む）
            for thread in getattr(channel, "threads", []) or []:
                if thread.id not in seen and readable(thread):
                    seen.add(thread.id)
                    yield thread
            archived = getattr(channel, "archived_threads", None)
            if archived is not None:
                try:
                    async for thread in archived(limit=None):
                        if thread.id not in seen and readable(thread):
                            seen.add(thread.id)
                            yield thread
                except (discord.Forbidden, discord.HTTPException):
                    pass

    async def _run_scan(self, guild: discord.Guild, status: discord.Message,
                        target: discord.PartialEmoji, emoji_text: str,
                        limit: int, scope_label: str, after: datetime | None):
        records: list[dict] = []
        scanned = 0
        channels = 0
        try:
            async for channel in self._targets(guild):
                channels += 1
                try:
                    scanned += await scan_channel(channel, target, after, records)
                except (discord.Forbidden, discord.HTTPException) as exc:
                    LOGGER.info("reaction-rank: #%s を走査できずスキップ: %s",
                                getattr(channel, "name", channel.id), exc)
                if channels % 5 == 0:
                    try:
                        await status.edit(content=(
                            f"🔍 {emoji_text} のリアクションを集計中… "
                            f"{channels}チャンネル・{scanned}件を走査済み"))
                    except discord.HTTPException:
                        pass
            chunks = format_ranking(emoji_text, rank_records(records, limit),
                                    scope_label, scanned, channels)
            await status.edit(content=chunks[0])
            for extra in chunks[1:]:
                await status.channel.send(extra)
        except Exception:
            LOGGER.exception("reaction-rank: 走査に失敗")
            try:
                await status.edit(content="集計中にエラーが起きたため中断しました。")
            except discord.HTTPException:
                pass
        finally:
            self._running.discard(guild.id)

    @app_commands.command(name="reaction-rank",
                          description="指定した絵文字のリアクションが多いメッセージを表示（サーバー全走査）")
    @app_commands.describe(
        emoji="数える絵文字（Unicode絵文字 or カスタム絵文字）",
        count="表示する件数（1〜25、省略時は10）",
        scope="対象期間（省略時は今年）",
    )
    @app_commands.choices(scope=[
        app_commands.Choice(name="今年", value="year"),
        app_commands.Choice(name="全期間", value="all"),
    ])
    @app_commands.guild_only()
    async def reaction_rank(self, interaction: discord.Interaction,
                            emoji: str, count: int = DEFAULT_COUNT, scope: str = "year"):
        guild = interaction.guild
        if guild.id in self._running:
            await interaction.response.send_message(
                "このサーバーでは既に集計が実行中です。終わってからもう一度どうぞ。", ephemeral=True)
            return
        target = parse_emoji(emoji)
        if target is None:
            await interaction.response.send_message(
                "絵文字を読み取れませんでした。絵文字そのもの、または `<:名前:ID>` 形式で指定してください。",
                ephemeral=True)
            return
        if not 1 <= count <= MAX_COUNT:
            await interaction.response.send_message(
                f"件数は1〜{MAX_COUNT}で指定してください。", ephemeral=True)
            return
        self._running.add(guild.id)
        after = year_start() if scope == "year" else None
        scope_label = f"{after.year}年" if after else "全期間"
        emoji_text = str(target)
        await interaction.response.send_message(
            f"{emoji_text} の集計を開始します（{scope_label}）。結果はこのチャンネルに出します。", ephemeral=True)
        status = await interaction.channel.send(
            f"🔍 {emoji_text} のリアクションを集計中… サーバーの履歴を走査しています")
        asyncio.create_task(self._run_scan(
            guild, status, target, emoji_text, count, scope_label, after))


async def setup(bot: commands.Bot):
    await bot.add_cog(ReactionRank(bot))
