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
PAGE_SIZE = 5
VIEW_TIMEOUT = 600  # ページ切り替えボタンの有効時間（秒）

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


def past_year_start(now: datetime | None = None) -> datetime:
    """過去1年の範囲の下限（ちょうど1年前の同時刻）。"""
    now = now or datetime.now(JST)
    try:
        return now.replace(year=now.year - 1)
    except ValueError:  # 2月29日には前年に同じ日が無い
        return now.replace(year=now.year - 1, day=28)


def period_start(scope: str, now: datetime | None = None) -> datetime | None:
    """scope（year / past_year / all）に対応する走査の下限。全期間は None。"""
    if scope == "year":
        return year_start(now)
    if scope == "past_year":
        return past_year_start(now)
    return None


def rank_records(records: list[dict], limit: int) -> list[dict]:
    """個数の多い順、同数は古い順に並べて上位 limit 件を返す。"""
    return sorted(records, key=lambda r: (-r["count"], r["created_at"]))[:limit]


def aggregate_users(records: list[dict], limit: int) -> list[dict]:
    """投稿者ごとに累計を集計する。Bot の投稿は除く。

    各ユーザーは {author, total, messages, best, first_at}。
    並びは累計の多い順、同数は最初にヒットした投稿が古い順。
    """
    users: dict = {}
    for r in records:
        if r.get("author_bot"):
            continue
        key = r.get("author_id") or r["author"]
        u = users.get(key)
        if u is None:
            u = users[key] = {"author": r["author"], "total": 0, "messages": 0,
                              "best": r, "first_at": r["created_at"]}
        u["total"] += r["count"]
        u["messages"] += 1
        if r["count"] > u["best"]["count"]:
            u["best"] = r
        if r["created_at"] < u["first_at"]:
            u["first_at"] = r["created_at"]
    ranked = sorted(users.values(), key=lambda u: (-u["total"], u["first_at"]))
    return ranked[:limit]


def make_record(message, count: int) -> dict:
    content = " ".join((message.content or "").split())
    if len(content) > SNIPPET_LEN:
        content = content[:SNIPPET_LEN] + "…"
    channel = message.channel
    return {
        "count": count,
        "created_at": message.created_at,
        "author": str(message.author),
        "author_id": getattr(message.author, "id", None),
        "author_bot": bool(getattr(message.author, "bot", False)),
        "channel": getattr(channel, "name", None) or str(getattr(channel, "id", "")),
        "snippet": content or "（本文なし）",
        "url": message.jump_url,
    }


def format_entry(emoji_text: str, rank: int, record: dict) -> str:
    """1件分の Markdown ブロック。"""
    date = record["created_at"].astimezone(JST).strftime("%Y/%m/%d")
    return (f"### {rank}位　{emoji_text}×{record['count']}\n"
            f"> {record['snippet']}\n"
            f"{record['author']} ・ #{record['channel']} ・ {date} ・ "
            f"[メッセージへ]({record['url']})")


def _paginate(header: str, entries: list[str], empty_text: str) -> list[str]:
    """ヘッダー＋エントリ列を PAGE_SIZE 件ずつのページ文字列にする。"""
    if not entries:
        return [header + "\n" + empty_text]
    total = (len(entries) + PAGE_SIZE - 1) // PAGE_SIZE
    pages = []
    for p in range(total):
        body = "\n\n".join(entries[p * PAGE_SIZE:(p + 1) * PAGE_SIZE])
        text = f"{header}\n\n{body}"
        if total > 1:
            text += f"\n\n-# ページ {p + 1} / {total}"
        pages.append(text)
    return pages


def format_pages(emoji_text: str, records: list[dict], scope_label: str,
                 scanned: int, channels: int) -> list[str]:
    """メッセージ別ランキングを Markdown 整形したページのリストにする。"""
    header = (f"## {emoji_text} リアクションランキング（{scope_label}）\n"
              f"走査：{channels}チャンネル・スレッド／{scanned}件のメッセージ")
    entries = [format_entry(emoji_text, i, r) for i, r in enumerate(records, 1)]
    return _paginate(header, entries,
                     "対象のリアクションが付いたメッセージは見つからなかったよ。")


def format_user_entry(emoji_text: str, rank: int, user: dict) -> str:
    """ユーザー別ランキングの1件分の Markdown ブロック。"""
    best = user["best"]
    date = best["created_at"].astimezone(JST).strftime("%Y/%m/%d")
    return (f"### {rank}位　{emoji_text}×{user['total']}\n"
            f"{user['author']} ・ 対象メッセージ{user['messages']}件\n"
            f"> {best['snippet']}\n"
            f"最多は {emoji_text}×{best['count']}（#{best['channel']} ・ {date} ・ "
            f"[メッセージへ]({best['url']})）")


def format_user_pages(emoji_text: str, users: list[dict], scope_label: str,
                      scanned: int, channels: int) -> list[str]:
    """ユーザー別（累計）ランキングを Markdown 整形したページのリストにする。"""
    header = (f"## {emoji_text} リアクション ユーザーランキング（{scope_label}）\n"
              f"走査：{channels}チャンネル・スレッド／{scanned}件のメッセージ")
    entries = [format_user_entry(emoji_text, i, u) for i, u in enumerate(users, 1)]
    return _paginate(header, entries,
                     "対象のリアクションを得たユーザーはいなかったよ。")


class RankingView(discord.ui.View):
    """ランキングのページ切り替えボタン。誰でも押せる。"""

    def __init__(self, pages: list[str]):
        super().__init__(timeout=VIEW_TIMEOUT)
        self.pages = pages
        self.index = 0
        self.message: discord.Message | None = None
        self._sync_buttons()

    def _sync_buttons(self):
        self.prev_page.disabled = self.index <= 0
        self.next_page.disabled = self.index >= len(self.pages) - 1
        self.page_label.label = f"{self.index + 1} / {len(self.pages)}"

    async def _show(self, interaction: discord.Interaction):
        self._sync_buttons()
        await interaction.response.edit_message(content=self.pages[self.index], view=self)

    @discord.ui.button(label="前へ", emoji="◀", style=discord.ButtonStyle.secondary)
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.index > 0:
            self.index -= 1
        await self._show(interaction)

    @discord.ui.button(label="1 / 1", style=discord.ButtonStyle.secondary, disabled=True)
    async def page_label(self, interaction: discord.Interaction, button: discord.ui.Button):
        pass  # 表示専用（disabled なので押せない）

    @discord.ui.button(label="次へ", emoji="▶", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.index < len(self.pages) - 1:
            self.index += 1
        await self._show(interaction)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


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
                        limit: int, scope_label: str, after: datetime | None,
                        mode: str = "message"):
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
            if mode == "user":
                pages = format_user_pages(emoji_text, aggregate_users(records, limit),
                                          scope_label, scanned, channels)
            else:
                pages = format_pages(emoji_text, rank_records(records, limit),
                                     scope_label, scanned, channels)
            if len(pages) > 1:
                view = RankingView(pages)
                await status.edit(content=pages[0], view=view)
                view.message = status
            else:
                await status.edit(content=pages[0])
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
        mode="集計単位（省略時はメッセージ別）",
    )
    @app_commands.choices(scope=[
        app_commands.Choice(name="今年", value="year"),
        app_commands.Choice(name="過去1年", value="past_year"),
        app_commands.Choice(name="全期間", value="all"),
    ])
    @app_commands.choices(mode=[
        app_commands.Choice(name="メッセージ別", value="message"),
        app_commands.Choice(name="ユーザー別（累計）", value="user"),
    ])
    @app_commands.guild_only()
    async def reaction_rank(self, interaction: discord.Interaction,
                            emoji: str, count: int = DEFAULT_COUNT, scope: str = "year",
                            mode: str = "message"):
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
        after = period_start(scope)
        if scope == "past_year":
            scope_label = "過去1年"
        elif after is not None:
            scope_label = f"{after.year}年"
        else:
            scope_label = "全期間"
        emoji_text = str(target)
        await interaction.response.send_message(
            f"{emoji_text} の集計を開始します（{scope_label}）。結果はこのチャンネルに出します。", ephemeral=True)
        status = await interaction.channel.send(
            f"🔍 {emoji_text} のリアクションを集計中… サーバーの履歴を走査しています")
        asyncio.create_task(self._run_scan(
            guild, status, target, emoji_text, count, scope_label, after, mode))


async def setup(bot: commands.Bot):
    await bot.add_cog(ReactionRank(bot))
