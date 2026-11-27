"""指定した絵文字のリアクションが多いメッセージをランキング表示する。

Discord にはリアクション数でメッセージを検索する Bot 向け API が無いため、
Bot が読める全チャンネル・スレッドの履歴を走査して集計する総当たり方式。
リアクション数はメッセージ取得時に付いてくるので追加の API コールは要らない。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
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
CACHE_FILE = Path(__file__).parent.parent / "reaction_rank_cache.json"
OVERLAP_DAYS = 7  # 差分走査でキャッシュ末尾からさかのぼって再走査する日数
MAX_CACHE_EMOJIS = 10  # 1回のキャッシュ作成で指定できる絵文字の上限

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


def reaction_key(emoji) -> str:
    """リアクションの絵文字をキャッシュと同じキー形式にする。

    Unicode 絵文字は文字列そのもの、カスタム絵文字は ID で識別する。
    """
    if isinstance(emoji, str):
        return f"unicode:{emoji}"
    return f"custom:{emoji.id}"


def make_emoji_records(message) -> list[dict]:
    """メッセージに付いた全リアクションを、絵文字ごとのレコードにする。"""
    records = []
    for reaction in getattr(message, "reactions", None) or []:
        if reaction.count <= 0:
            continue
        emoji = reaction.emoji
        record = make_record(message, reaction.count)
        record["emoji"] = str(emoji)
        record["emoji_key"] = reaction_key(emoji)
        record["emoji_id"] = getattr(emoji, "id", None)
        record["emoji_name"] = (emoji if isinstance(emoji, str)
                                else getattr(emoji, "name", None))
        record["emoji_animated"] = bool(getattr(emoji, "animated", False))
        records.append(record)
    return records


def aggregate_emojis(records: list[dict], limit: int) -> list[dict]:
    """絵文字ごとに累計を集計する（Bot の投稿に付いた分も含む）。

    各絵文字は {emoji, total, messages, best, first_at}。
    並びは累計の多い順、同数は最初にヒットした投稿が古い順。
    """
    emojis: dict = {}
    for r in records:
        e = emojis.get(r["emoji_key"])
        if e is None:
            e = emojis[r["emoji_key"]] = {"emoji": r["emoji"], "total": 0,
                                          "messages": 0, "best": r,
                                          "first_at": r["created_at"]}
        e["total"] += r["count"]
        e["messages"] += 1
        if r["count"] > e["best"]["count"]:
            e["best"] = r
        if r["created_at"] < e["first_at"]:
            e["first_at"] = r["created_at"]
    ranked = sorted(emojis.values(), key=lambda e: (-e["total"], e["first_at"]))
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
        "message_id": getattr(message, "id", None),
        "channel_id": getattr(channel, "id", None),
        "channel": getattr(channel, "name", None) or str(getattr(channel, "id", "")),
        "snippet": content or "（本文なし）",
        "url": message.jump_url,
    }


def emoji_key(target: discord.PartialEmoji) -> str:
    """キャッシュのキー。カスタム絵文字は ID、Unicode 絵文字は文字そのもの。"""
    return f"custom:{target.id}" if target.id else f"unicode:{target.name}"


def parse_emojis(text: str) -> tuple[list[discord.PartialEmoji], list[str]]:
    """空白・カンマ区切りの複数絵文字をパースする。(成功リスト, 失敗トークン) を返す。"""
    valid: list[discord.PartialEmoji] = []
    invalid: list[str] = []
    seen: set[str] = set()
    for token in re.split(r"[\s,]+", (text or "").strip()):
        if not token:
            continue
        parsed = parse_emoji(token)
        if parsed is None:
            invalid.append(token)
        elif emoji_key(parsed) not in seen:
            seen.add(emoji_key(parsed))
            valid.append(parsed)
    return valid, invalid


def serialize_record(record: dict) -> dict:
    data = dict(record)
    data["created_at"] = record["created_at"].isoformat()
    return data


def deserialize_record(data: dict) -> dict:
    record = dict(data)
    record["created_at"] = datetime.fromisoformat(data["created_at"])
    return record


def load_cache(path) -> dict:
    """キャッシュファイルを読み込む。無い・壊れている場合は空 dict。

    構造: {guild_id(int): {emoji_key: {emoji, cached_until(datetime),
    coverage_start(datetime|None), records}}}
    """
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    cache: dict = {}
    for gid, per_guild in (raw.get("guilds") or {}).items():
        entries = {}
        for key, e in (per_guild or {}).items():
            try:
                entries[key] = {
                    "emoji": e["emoji"],
                    "cached_until": datetime.fromisoformat(e["cached_until"]),
                    "coverage_start": (datetime.fromisoformat(e["coverage_start"])
                                       if e.get("coverage_start") else None),
                    "records": [deserialize_record(r) for r in e.get("records", [])],
                }
            except (KeyError, TypeError, ValueError):
                continue
        try:
            cache[int(gid)] = entries
        except (TypeError, ValueError):
            continue
    return cache


def save_cache(path, cache: dict) -> None:
    """キャッシュを JSON で保存する（一時ファイル経由で原子的に差し替える）。"""
    data = {"guilds": {}}
    for gid, per_guild in cache.items():
        data["guilds"][str(gid)] = {
            key: {
                "emoji": e["emoji"],
                "cached_until": e["cached_until"].isoformat(),
                "coverage_start": (e["coverage_start"].isoformat()
                                   if e["coverage_start"] else None),
                "records": [serialize_record(r) for r in e["records"]],
            }
            for key, e in per_guild.items()
        }
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        LOGGER.warning("reaction-rank: キャッシュを保存できませんでした", exc_info=True)


def cache_covers(entry: dict | None, start: datetime | None) -> bool:
    """キャッシュが、開始日時 start 以降の期間をカバーしているか。"""
    if entry is None:
        return False
    if entry["coverage_start"] is None:
        return True
    return start is not None and entry["coverage_start"] <= start


def wider_coverage(a: datetime | None, b: datetime | None) -> datetime | None:
    """2つのカバー開始日時のうち広い方（None は全期間＝最も広い）。"""
    if a is None or b is None:
        return None
    return min(a, b)


def merge_records(cached: list[dict], fresh: list[dict],
                  window_start: datetime | None) -> list[dict]:
    """キャッシュと再走査分を合体させる。

    window_start 以降のキャッシュ分は fresh 側で置き換える（fresh に無いものは
    リアクションが消えた／削除されたとして落とす）。window_start が None なら
    全件 fresh で置き換える。
    """
    if window_start is None:
        return list(fresh)
    fresh_ids = {r["message_id"] for r in fresh if r.get("message_id") is not None}
    merged = [r for r in cached
              if r["created_at"] < window_start and r.get("message_id") not in fresh_ids]
    merged.extend(fresh)
    return merged


def filter_records(records: list[dict], start: datetime | None) -> list[dict]:
    """開始日時以降のレコードだけに絞る（None なら全件）。"""
    if start is None:
        return list(records)
    return [r for r in records if r["created_at"] >= start]


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
                 scanned: int, channels: int, note: str = "") -> list[str]:
    """メッセージ別ランキングを Markdown 整形したページのリストにする。"""
    header = (f"## {emoji_text} リアクションランキング（{scope_label}）\n"
              f"走査：{channels}チャンネル・スレッド／{scanned}件のメッセージ")
    if note:
        header += f"\n{note}"
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
                      scanned: int, channels: int, note: str = "") -> list[str]:
    """ユーザー別（累計）ランキングを Markdown 整形したページのリストにする。"""
    header = (f"## {emoji_text} リアクション ユーザーランキング（{scope_label}）\n"
              f"走査：{channels}チャンネル・スレッド／{scanned}件のメッセージ")
    if note:
        header += f"\n{note}"
    entries = [format_user_entry(emoji_text, i, u) for i, u in enumerate(users, 1)]
    return _paginate(header, entries,
                     "対象のリアクションを得たユーザーはいなかったよ。")


def format_emoji_entry(rank: int, item: dict) -> str:
    """絵文字別ランキングの1件分の Markdown ブロック。"""
    best = item["best"]
    date = best["created_at"].astimezone(JST).strftime("%Y/%m/%d")
    return (f"### {rank}位　{item['emoji']}×{item['total']}\n"
            f"対象メッセージ{item['messages']}件\n"
            f"> {best['snippet']}\n"
            f"最多のメッセージ：{item['emoji']}×{best['count']} ・ {best['author']} ・ "
            f"#{best['channel']} ・ {date} ・ [メッセージへ]({best['url']})")


def format_emoji_pages(items: list[dict], scope_label: str,
                       scanned: int, channels: int) -> list[str]:
    """絵文字別（累計）ランキングを Markdown 整形したページのリストにする。"""
    header = (f"## リアクション 絵文字ランキング（{scope_label}）\n"
              f"走査：{channels}チャンネル・スレッド／{scanned}件のメッセージ")
    entries = [format_emoji_entry(i, e) for i, e in enumerate(items, 1)]
    return _paginate(header, entries,
                     "リアクションが付いたメッセージは見つからなかったよ。")


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


async def scan_channel_multi(channel, jobs: list, after: datetime | None) -> int:
    """複数絵文字を1回の履歴走査でまとめて拾う。

    jobs は (target, window_start, sink) のリスト。メッセージが window_start
    より前ならその絵文字については対象外。走査件数を返す。
    """
    scanned = 0
    async for message in channel.history(limit=None, after=after):
        scanned += 1
        for target, window_start, sink in jobs:
            if window_start is not None and message.created_at < window_start:
                continue
            count = match_count(message, target)
            if count > 0:
                sink.append(make_record(message, count))
    return scanned


async def scan_channel_emojis(channel, after: datetime | None,
                              records: list[dict]) -> int:
    """1チャンネル（またはスレッド）を走査し、全絵文字のヒットを records に追加。"""
    scanned = 0
    async for message in channel.history(limit=None, after=after):
        scanned += 1
        records.extend(make_emoji_records(message))
    return scanned


class ReactionRank(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._running: set[int] = set()
        self.cache_path = CACHE_FILE
        self.cache: dict = load_cache(self.cache_path)

    def _store_entry(self, guild_id: int, target: discord.PartialEmoji,
                     records: list[dict], coverage_start: datetime | None,
                     cached_until: datetime):
        """1絵文字ぶんのキャッシュを更新して保存する。"""
        self.cache.setdefault(guild_id, {})[emoji_key(target)] = {
            "emoji": str(target),
            "cached_until": cached_until,
            "coverage_start": coverage_start,
            "records": records,
        }
        save_cache(self.cache_path, self.cache)

    async def _message_exists(self, record: dict) -> bool:
        """キャッシュ由来のレコードの実在確認。消えていたら False。"""
        message_id = record.get("message_id")
        channel_id = record.get("channel_id")
        if not message_id or not channel_id:
            return True
        channel = self.bot.get_channel(channel_id)
        if channel is None or not hasattr(channel, "fetch_message"):
            return True
        try:
            await channel.fetch_message(message_id)
            return True
        except discord.NotFound:
            return False
        except (discord.Forbidden, discord.HTTPException):
            return True

    async def _top_existing(self, records: list[dict], limit: int) -> list[dict]:
        """上位から実在するものだけを limit 件集める（キャッシュ併用時の削除対策）。"""
        chosen: list[dict] = []
        attempts = 0
        for record in rank_records(records, len(records)):
            if len(chosen) >= limit or attempts >= limit + 10:
                break
            attempts += 1
            if await self._message_exists(record):
                chosen.append(record)
        return chosen

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
        scan_started = datetime.now(timezone.utc)
        entry = (self.cache.get(guild.id) or {}).get(emoji_key(target))
        use_cache = cache_covers(entry, after)
        if use_cache:
            window = entry["cached_until"] - timedelta(days=OVERLAP_DAYS)
            if after is not None and after > window:
                window = after
            coverage = entry["coverage_start"]
        else:
            window = after
            coverage = after
        try:
            async for channel in self._targets(guild):
                channels += 1
                try:
                    scanned += await scan_channel(channel, target, window, records)
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
            if use_cache:
                merged = merge_records(entry["records"], records, window)
                note = (f"キャッシュ併用：{entry['cached_until'].astimezone(JST):%Y/%m/%d %H:%M} "
                        f"時点のキャッシュ＋差分走査（直近{OVERLAP_DAYS}日は再走査）")
            else:
                merged = records
                note = ""
            ranked_source = filter_records(merged, after)
            if mode == "user":
                pages = format_user_pages(emoji_text, aggregate_users(ranked_source, limit),
                                          scope_label, scanned, channels, note=note)
            elif use_cache:
                pages = format_pages(emoji_text, await self._top_existing(ranked_source, limit),
                                     scope_label, scanned, channels, note=note)
            else:
                pages = format_pages(emoji_text, rank_records(ranked_source, limit),
                                     scope_label, scanned, channels)
            if len(pages) > 1:
                view = RankingView(pages)
                await status.edit(content=pages[0], view=view)
                view.message = status
            else:
                await status.edit(content=pages[0])
            self._store_entry(guild.id, target, merged, coverage, scan_started)
        except Exception:
            LOGGER.exception("reaction-rank: 走査に失敗")
            try:
                await status.edit(content="集計中にエラーが起きたため中断しました。")
            except discord.HTTPException:
                pass
        finally:
            self._running.discard(guild.id)

    async def _run_emoji_scan(self, guild: discord.Guild, status: discord.Message,
                              limit: int, scope_label: str, after: datetime | None):
        """絵文字別（累計）モード。全絵文字を対象にフル走査する。

        キャッシュは使わない（全絵文字の完全な集計にはフル走査が要る）が、
        走査結果は見つかった全絵文字ぶんのキャッシュとして保存する。
        """
        records: list[dict] = []
        scanned = 0
        channels = 0
        scan_started = datetime.now(timezone.utc)
        try:
            async for channel in self._targets(guild):
                channels += 1
                try:
                    scanned += await scan_channel_emojis(channel, after, records)
                except (discord.Forbidden, discord.HTTPException) as exc:
                    LOGGER.info("reaction-rank: #%s を走査できずスキップ: %s",
                                getattr(channel, "name", channel.id), exc)
                if channels % 5 == 0:
                    try:
                        await status.edit(content=(
                            "🔍 リアクションの絵文字を集計中… "
                            f"{channels}チャンネル・{scanned}件を走査済み"))
                    except discord.HTTPException:
                        pass
            pages = format_emoji_pages(aggregate_emojis(records, limit),
                                       scope_label, scanned, channels)
            if len(pages) > 1:
                view = RankingView(pages)
                await status.edit(content=pages[0], view=view)
                view.message = status
            else:
                await status.edit(content=pages[0])
            by_key: dict = {}
            for r in records:
                stored = {k: v for k, v in r.items() if not k.startswith("emoji")}
                by_key.setdefault(r["emoji_key"], (r["emoji"], []))[1].append(stored)
            if by_key:
                per_guild = self.cache.setdefault(guild.id, {})
                for key, (emoji_text, group) in by_key.items():
                    per_guild[key] = {
                        "emoji": emoji_text,
                        "cached_until": scan_started,
                        "coverage_start": after,
                        "records": group,
                    }
                save_cache(self.cache_path, self.cache)
        except Exception:
            LOGGER.exception("reaction-rank: 絵文字別集計に失敗")
            try:
                await status.edit(content="集計中にエラーが起きたため中断しました。")
            except discord.HTTPException:
                pass
        finally:
            self._running.discard(guild.id)

    async def _run_cache_build(self, interaction: discord.Interaction,
                               guild: discord.Guild,
                               targets: list[discord.PartialEmoji],
                               scope: str, rebuild: bool):
        """キャッシュだけを作る。結果（ランキング・件数）は一切出さない。"""
        try:
            scope_start = period_start(scope)
            scan_started = datetime.now(timezone.utc)
            jobs = []
            plans = {}
            for target in targets:
                key = emoji_key(target)
                entry = (self.cache.get(guild.id) or {}).get(key)
                if rebuild or not cache_covers(entry, scope_start):
                    window = scope_start
                    full = True
                else:
                    window = entry["cached_until"] - timedelta(days=OVERLAP_DAYS)
                    if scope_start is not None and scope_start > window:
                        window = scope_start
                    full = False
                sink: list[dict] = []
                jobs.append((target, window, sink))
                plans[key] = (target, window, sink, entry, full)
            global_after = (None if any(w is None for _, w, _ in jobs)
                            else min(w for _, w, _ in jobs))
            scanned = 0
            channels = 0
            async for channel in self._targets(guild):
                channels += 1
                try:
                    scanned += await scan_channel_multi(channel, jobs, global_after)
                except (discord.Forbidden, discord.HTTPException) as exc:
                    LOGGER.info("reaction-rank-cache: #%s を走査できずスキップ: %s",
                                getattr(channel, "name", channel.id), exc)
                if channels % 5 == 0:
                    try:
                        await interaction.edit_original_response(content=(
                            f"💾 キャッシュを作成中… {channels}チャンネル・"
                            f"{scanned}件を走査済み（結果は表示されません）"))
                    except discord.HTTPException:
                        pass
            for key, (target, window, sink, entry, full) in plans.items():
                if full or entry is None:
                    merged = sink
                    coverage = scope_start
                else:
                    merged = merge_records(entry["records"], sink, window)
                    coverage = wider_coverage(entry["coverage_start"], scope_start)
                self.cache.setdefault(guild.id, {})[key] = {
                    "emoji": str(target),
                    "cached_until": scan_started,
                    "coverage_start": coverage,
                    "records": merged,
                }
            save_cache(self.cache_path, self.cache)
            emoji_list = " ".join(str(t) for t in targets)
            try:
                await interaction.edit_original_response(content=(
                    f"💾 キャッシュを保存しました（結果は表示していません）。\n"
                    f"対象：{emoji_list}\n"
                    f"走査済み：{scan_started.astimezone(JST):%Y/%m/%d %H:%M} 時点まで"))
            except discord.HTTPException:
                pass
        except Exception:
            LOGGER.exception("reaction-rank-cache: キャッシュ作成に失敗")
            try:
                await interaction.edit_original_response(
                    content="キャッシュ作成中にエラーが起きたため中断しました。")
            except discord.HTTPException:
                pass
        finally:
            self._running.discard(guild.id)

    @app_commands.command(name="reaction-rank",
                          description="リアクションランキングを表示（メッセージ別／ユーザー別／絵文字別、サーバー全走査）")
    @app_commands.describe(
        emoji="数える絵文字（Unicode絵文字 or カスタム絵文字。絵文字別モードでは不要）",
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
        app_commands.Choice(name="絵文字別（累計）", value="emoji"),
    ])
    @app_commands.guild_only()
    async def reaction_rank(self, interaction: discord.Interaction,
                            emoji: str = None, count: int = DEFAULT_COUNT,
                            scope: str = "year", mode: str = "message"):
        guild = interaction.guild
        if guild.id in self._running:
            await interaction.response.send_message(
                "このサーバーでは既に集計が実行中です。終わってからもう一度どうぞ。", ephemeral=True)
            return
        target = None
        if mode != "emoji":
            if not emoji:
                await interaction.response.send_message(
                    "絵文字別モード以外では、emoji に数える絵文字を指定してください。",
                    ephemeral=True)
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
        if mode == "emoji":
            await interaction.response.send_message(
                f"リアクションの絵文字別ランキングの集計を開始します（{scope_label}）。"
                "結果はこのチャンネルに出します。", ephemeral=True)
            status = await interaction.channel.send(
                "🔍 リアクションの絵文字を集計中… サーバーの履歴を走査しています")
            asyncio.create_task(self._run_emoji_scan(
                guild, status, count, scope_label, after))
            return
        emoji_text = str(target)
        await interaction.response.send_message(
            f"{emoji_text} の集計を開始します（{scope_label}）。結果はこのチャンネルに出します。", ephemeral=True)
        status = await interaction.channel.send(
            f"🔍 {emoji_text} のリアクションを集計中… サーバーの履歴を走査しています")
        asyncio.create_task(self._run_scan(
            guild, status, target, emoji_text, count, scope_label, after, mode))

    @app_commands.command(name="reaction-rank-cache",
                          description="ランキング用キャッシュだけを作成/更新します（結果は表示しません）")
    @app_commands.describe(
        emojis="数える絵文字（空白区切りで複数指定可、最大10個）",
        scope="対象期間（省略時は全期間）",
        rebuild="既存キャッシュを破棄して作り直す",
    )
    @app_commands.choices(scope=[
        app_commands.Choice(name="今年", value="year"),
        app_commands.Choice(name="過去1年", value="past_year"),
        app_commands.Choice(name="全期間", value="all"),
    ])
    @app_commands.guild_only()
    async def reaction_rank_cache(self, interaction: discord.Interaction,
                                  emojis: str, scope: str = "all", rebuild: bool = False):
        guild = interaction.guild
        if guild.id in self._running:
            await interaction.response.send_message(
                "このサーバーでは既に集計が実行中です。終わってからもう一度どうぞ。", ephemeral=True)
            return
        targets, invalid = parse_emojis(emojis)
        if invalid:
            await interaction.response.send_message(
                f"読み取れない絵文字がありました：{' '.join(invalid)}。"
                "絵文字そのもの、または `<:名前:ID>` 形式で指定してください。", ephemeral=True)
            return
        if not targets:
            await interaction.response.send_message(
                "絵文字が指定されていません。", ephemeral=True)
            return
        if len(targets) > MAX_CACHE_EMOJIS:
            await interaction.response.send_message(
                f"一度に指定できる絵文字は{MAX_CACHE_EMOJIS}個までです。", ephemeral=True)
            return
        self._running.add(guild.id)
        await interaction.response.defer(ephemeral=True)
        await interaction.edit_original_response(
            content="💾 キャッシュの作成を開始します（結果は表示されません）。")
        asyncio.create_task(self._run_cache_build(
            interaction, guild, targets, scope, rebuild))


async def setup(bot: commands.Bot):
    await bot.add_cog(ReactionRank(bot))
