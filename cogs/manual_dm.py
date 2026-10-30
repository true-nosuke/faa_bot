from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import discord
from discord.ext import commands

LOGGER = logging.getLogger(__name__)

# ---- 設定（変更後は /deploy update か再起動）----
MANUAL_DM_ENABLED = True
# 「名前」→ 本人のユーザーID。別名（aliases）は任意。user_id が 0 の人は無効。
MANUAL_PERSONS: dict[str, dict] = {
    "nosukeGPT": {"user_id": 0, "aliases": []},
}
WAIT_TIMEOUT_SECONDS = 600       # 回答が届かないときの締切
MAX_PENDING_PER_TARGET = 3       # 同じ人への同時依頼の上限
ASKER_COOLDOWN_SECONDS = 30      # 質問者1人あたりの間隔
ASKER_HOURLY_LIMIT = 5           # 質問者1人あたり1時間の回数
MAX_QUESTION_CHARS = 1500        # DMに載せる質問の最大文字数
MAX_TOTAL_ATTACHMENT_BYTES = 8 * 1024 * 1024
STATE_FILE = Path(__file__).parent.parent / "manual_dm_state.json"

# 質問者から見える文面（AIが答えているように見せるため、人の存在は出さない）
UNAVAILABLE_TEXT = "⚠️ {name} は現在回答できません。しばらくしてからもう一度お試しください。"
CONNECT_FAIL_TEXT = "⚠️ {name} に接続できませんでした。"
BUSY_TEXT = "⚠️ {name} は混み合っています。少し待ってからお試しください。"
RATE_TEXT = "⚠️ 利用回数の上限に達しました。しばらくしてからお試しください。"
EMPTY_TEXT = "⚠️ 質問を入力してください。"

# ---- 「◯◯で回答して」の判定 ----
_KEYWORD = r"(?:回答|返答|答え|応答|返事|聞い|訊い|質問|お願い|おねがい|頼)"
_SUFFIX = r"(?:(?:し)?て(?:ください|下さい|くれ|ほしい|欲しい|ね|よ)?|ください|下さい|(?:お願い|おねがい)(?:します|ね)?|します|いたします|ね|よ)?"
_TAIL = r"[。、，,.!！?？ー〜~\s]*"


def _fuzzy(text: str) -> str:
    """英数字は大文字小文字・全角半角を区別せずに一致させる正規表現にする。"""
    out = []
    for ch in text:
        if ch.isascii() and ch.isalnum():
            variants = {ch.lower(), ch.upper(), chr(ord(ch.lower()) + 0xFEE0), chr(ord(ch.upper()) + 0xFEE0)}
            out.append("[" + "".join(sorted(variants)) + "]")
        else:
            out.append(re.escape(ch))
    return "".join(out)


def build_pattern(names: list[str]) -> re.Pattern:
    alternatives = "|".join(_fuzzy(n) for n in sorted(names, key=len, reverse=True))
    return re.compile(rf"(?P<name>{alternatives})(?:さん|くん|ちゃん|君)?\s*[でにはがをの]?\s*{_KEYWORD}{_SUFFIX}{_TAIL}")


@dataclass
class Request:
    channel_id: int
    message_id: int
    guild_id: int
    asker_id: int
    target_id: int
    name: str
    dm_channel_id: int = 0
    dm_message_id: int = 0
    expires: float = 0.0
    delivering: bool = False
    done: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None

    def to_json(self) -> dict:
        return {k: getattr(self, k) for k in ("channel_id", "message_id", "guild_id", "asker_id", "target_id",
                                              "name", "dm_channel_id", "dm_message_id", "expires")}


class DeclineButton(discord.ui.DynamicItem[discord.ui.Button], template=r"manualdm:decline:(?P<id>\d+)"):
    def __init__(self, request_id: int):
        super().__init__(discord.ui.Button(label="辞退する", style=discord.ButtonStyle.secondary,
                                           custom_id=f"manualdm:decline:{request_id}"))
        self.request_id = request_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["id"]))

    async def callback(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("ManualDM")
        if cog is None:
            return await interaction.response.send_message("この機能は現在使えません。")
        await cog.decline(interaction, self.request_id)


class ManualDM(commands.Cog):
    """「◯◯で回答して」と頼まれたら、その人にDMで質問を送り、届いた原稿をBotの返答として投稿する。"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.persons: dict[str, tuple[str, int]] = {}   # 正規化名 → (表示名, user_id)
        names = []
        for name, info in MANUAL_PERSONS.items():
            uid = int(info.get("user_id") or 0)
            if uid <= 0:
                continue
            for n in [name, *info.get("aliases", [])]:
                names.append(n)
                self.persons[n.casefold()] = (name, uid)
        self._pattern = build_pattern(names) if names else None
        self._reqs: dict[int, Request] = {}      # 質問メッセージID → 依頼
        self._by_dm: dict[int, int] = {}         # DM通知のメッセージID → 質問メッセージID
        self._asks: dict[int, list[float]] = {}
        self._closing = False

    # ---------- 起動・終了 ----------
    async def cog_load(self):
        self.bot.add_dynamic_items(DeclineButton)
        if self.persons:
            asyncio.create_task(self._restore())

    async def cog_unload(self):
        self._closing = True
        for req in list(self._reqs.values()):
            if req.task:
                req.task.cancel()

    def _save(self):
        try:
            STATE_FILE.write_text(json.dumps([r.to_json() for r in self._reqs.values()]), encoding="utf-8")
        except OSError as error:
            LOGGER.warning("manual_dm の状態を保存できません: %s", error)

    async def _restore(self):
        await self.bot.wait_until_ready()
        try:
            items = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for raw in items:
            try:
                req = Request(**{k: raw[k] for k in Request.__dataclass_fields__ if k in raw and k not in ("delivering", "done", "task")})
            except TypeError:
                continue
            self._register(req)
            req.task = asyncio.create_task(self._wait(req))

    def _register(self, req: Request):
        self._reqs[req.message_id] = req
        if req.dm_message_id:
            self._by_dm[req.dm_message_id] = req.message_id

    # ---------- 判定（ai_reply / manual_reply から呼ばれる） ----------
    def parse(self, message: discord.Message):
        """「◯◯で回答して」の依頼なら (表示名, user_id, 質問文) を返す。違えば None。"""
        if not MANUAL_DM_ENABLED or self._pattern is None or self.bot.user is None or message.guild is None:
            return None
        if message.author.bot or message.mention_everyone:
            return None
        if not any(u.id == self.bot.user.id for u in message.mentions):
            return None
        text = re.sub(rf"<@!?{self.bot.user.id}>", "", message.content)
        m = self._pattern.search(text)
        if not m:
            return None
        name, uid = self._lookup(m.group("name"))
        if name is None:
            return None
        rest = (text[:m.start()] + " " + text[m.end():]).strip()
        rest = re.sub(r"^[\s。、，,.!！:：]+", "", rest).strip()
        return name, uid, rest

    def _lookup(self, raw: str):
        key = "".join(chr(ord(c) - 0xFEE0) if 0xFF01 <= ord(c) <= 0xFF5E else c for c in raw).casefold()
        return self.persons.get(key, (None, None))

    def claims(self, message: discord.Message) -> bool:
        return self.parse(message) is not None

    # ---------- 質問の受付 ----------
    def _render(self, message: discord.Message, text: str) -> str:
        for user in message.mentions:
            text = text.replace(f"<@{user.id}>", f"@{user.display_name}").replace(f"<@!{user.id}>", f"@{user.display_name}")
        for ch in message.channel_mentions:
            text = text.replace(f"<#{ch.id}>", f"#{ch.name}")
        text = re.sub(r"<@&\d+>", "@ロール", text)
        return text.replace("@everyone", "＠everyone").replace("@here", "＠here").strip()

    def _reply_text(self, message: discord.Message) -> str:
        ref = message.reference.resolved if message.reference else None
        if isinstance(ref, discord.Message) and ref.content:
            return self._render(ref, ref.content)
        return ""

    async def _reply(self, channel_id: int, message_id: int, text: str):
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            return
        try:
            await channel.get_partial_message(message_id).reply(
                text, mention_author=False, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            try:
                await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException as error:
                LOGGER.warning("manual_dm: 返信できませんでした: HTTP %s", error.status)

    def _rate_problem(self, user_id: int) -> str | None:
        now = time.monotonic()
        hist = [t for t in self._asks.get(user_id, []) if now - t < 3600]
        self._asks[user_id] = hist
        if hist and now - hist[-1] < ASKER_COOLDOWN_SECONDS:
            return RATE_TEXT
        if len(hist) >= ASKER_HOURLY_LIMIT:
            return RATE_TEXT
        return None

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None:
            return await self._on_dm(message)
        parsed = self.parse(message)
        if parsed is None:
            return
        name, target_id, question = parsed
        question = self._render(message, question)
        reply_ctx = ""
        if not question:
            question = self._reply_text(message)          # 返信先の発言をそのまま質問にする
        else:
            reply_ctx = self._reply_text(message)
        if not question:
            return await self._reply(message.channel.id, message.id, EMPTY_TEXT)
        if rate := self._rate_problem(message.author.id):
            return await self._reply(message.channel.id, message.id, rate)
        if sum(1 for r in self._reqs.values() if r.target_id == target_id) >= MAX_PENDING_PER_TARGET \
                or message.id in self._reqs:
            return await self._reply(message.channel.id, message.id, BUSY_TEXT.format(name=name))
        self._asks.setdefault(message.author.id, []).append(time.monotonic())

        req = Request(channel_id=message.channel.id, message_id=message.id, guild_id=message.guild.id,
                      asker_id=message.author.id, target_id=target_id, name=name,
                      expires=time.time() + WAIT_TIMEOUT_SECONDS)
        self._register(req)             # 先に予約して二重受付を防ぐ
        text = self._dm_text(message, name, question, reply_ctx)
        try:
            user = self.bot.get_user(target_id) or await self.bot.fetch_user(target_id)
            view = discord.ui.View(timeout=None)
            view.add_item(DeclineButton(req.message_id))
            sent = await user.send(text, view=view, allowed_mentions=discord.AllowedMentions.none())
        except (discord.HTTPException, AttributeError) as error:
            LOGGER.warning("manual_dm: %s へのDMに失敗: %s", name, getattr(error, "status", error))
            self._reqs.pop(req.message_id, None)
            return await self._reply(message.channel.id, message.id, CONNECT_FAIL_TEXT.format(name=name))
        req.dm_channel_id, req.dm_message_id = sent.channel.id, sent.id
        self._by_dm[sent.id] = req.message_id
        self._save()
        req.task = asyncio.create_task(self._wait(req))

    def _dm_text(self, message: discord.Message, name: str, question: str, reply_ctx: str) -> str:
        quote = "\n".join("> " + line for line in question[:MAX_QUESTION_CHARS].splitlines() or [""])
        place = getattr(message.channel, "name", "") or "チャンネル"
        lines = [f"**{name} へのリクエスト**",
                 f"質問者：{discord.utils.escape_markdown(message.author.display_name)}"
                 f"（{discord.utils.escape_markdown(message.guild.name)} / #{place}）",
                 quote]
        if reply_ctx:
            lines.append("-# 返信先：" + reply_ctx[:150].replace("\n", " "))
        lines.append(f"🔗 {message.jump_url}")
        lines.append(f"この通知に返信すると、{name}（AI）の回答として投稿されます。")
        return "\n".join(lines)[:2000]

    # ---------- 待機・期限 ----------
    async def _wait(self, req: Request):
        channel = self.bot.get_channel(req.channel_id)
        remaining = max(0.0, req.expires - time.time())
        try:
            try:
                async with channel.typing():
                    await asyncio.wait_for(req.done.wait(), remaining)
            except (discord.HTTPException, AttributeError):
                await asyncio.wait_for(req.done.wait(), max(0.0, req.expires - time.time()))
        except asyncio.TimeoutError:
            if req.delivering:
                await req.done.wait()
            elif self._reqs.get(req.message_id) is req:
                await self._close(req, UNAVAILABLE_TEXT.format(name=req.name), "⌛ 期限切れになりました。")
        except asyncio.CancelledError:
            raise

    async def _dm_partial(self, req: Request):
        user = self.bot.get_user(req.target_id) or await self.bot.fetch_user(req.target_id)
        dm = user.dm_channel or await user.create_dm()
        return dm.get_partial_message(req.dm_message_id)

    async def _clear(self, req: Request, note: str | None = None, content: str | None = None):
        """依頼を終了し、DM通知のボタンを外す。"""
        self._reqs.pop(req.message_id, None)
        self._by_dm.pop(req.dm_message_id, None)
        self._save()
        req.done.set()
        if not req.dm_message_id:
            return
        try:
            partial = await self._dm_partial(req)
            kwargs = {"view": None}
            if content is not None and note:
                kwargs["content"] = (content + "\n-# " + note)[:2000]
            await partial.edit(**kwargs)
        except discord.HTTPException:
            pass

    async def _close(self, req: Request, asker_text: str, note: str):
        """期限切れ・辞退。質問者にはAIのエラーとして返す。"""
        content = None
        try:
            partial = await self._dm_partial(req)
            content = (await partial.fetch()).content
        except (discord.HTTPException, AttributeError):
            pass
        await self._clear(req, note, content)
        await self._reply(req.channel_id, req.message_id, asker_text)

    async def decline(self, interaction: discord.Interaction, request_id: int):
        req = self._reqs.get(request_id)
        if req is None:
            return await interaction.response.edit_message(
                content=(interaction.message.content + "\n-# ⌛ この依頼は終了しています。")[:2000], view=None)
        if interaction.user.id != req.target_id:
            return await interaction.response.send_message("この依頼の担当者ではありません。", ephemeral=True)
        await interaction.response.edit_message(
            content=(interaction.message.content + "\n-# 辞退しました。")[:2000], view=None)
        await self._clear(req)
        await self._reply(req.channel_id, req.message_id, UNAVAILABLE_TEXT.format(name=req.name))

    # ---------- 回答者からのDM ----------
    async def _on_dm(self, message: discord.Message):
        if message.author.bot or self.bot.user is None:
            return
        mine = [r for r in self._reqs.values() if r.target_id == message.author.id and r.dm_message_id]
        if not mine:
            return
        req = None
        if message.reference and message.reference.message_id in self._by_dm:
            req = self._reqs.get(self._by_dm[message.reference.message_id])
            if req is not None and req.target_id != message.author.id:
                req = None
        elif len(mine) == 1:
            req = mine[0]
        if req is None:
            if len(mine) > 1 or message.reference:
                await self._dm_say(message, "どの依頼への回答か分かりません。回答したい依頼の通知に「返信」してください。")
            return
        if req.delivering:
            return await self._dm_say(message, "現在、投稿中です。少し待ってください。")
        if len(message.content) > 2000:
            return await self._dm_say(message, "回答は2000文字以内にしてください。待機は続けています。")
        if not message.content and not message.attachments:
            return await self._dm_say(message, "テキストか添付ファイルを送ってください。待機は続けています。")
        channel = self.bot.get_channel(req.channel_id)
        limit = getattr(getattr(channel, "guild", None), "filesize_limit", MAX_TOTAL_ATTACHMENT_BYTES)
        if (len(message.attachments) > 10
                or sum(a.size for a in message.attachments) > MAX_TOTAL_ATTACHMENT_BYTES
                or any(a.size > limit for a in message.attachments)):
            return await self._dm_say(message, "添付は最大10個・合計8MB以内（送信先サーバーの上限以内）にしてください。待機は続けています。")
        req.delivering = True
        await self._deliver(req, message, channel)

    async def _dm_say(self, message: discord.Message, text: str):
        try:
            await message.reply(text, mention_author=False)
        except discord.HTTPException:
            pass

    async def _deliver(self, req: Request, draft: discord.Message, channel):
        ok = False
        try:
            blobs = [(a.filename, await a.read(), a.is_spoiler()) for a in draft.attachments]
            def files():
                return [discord.File(io.BytesIO(data), filename=fn, spoiler=sp) for fn, data, sp in blobs]
            none = discord.AllowedMentions.none()
            try:
                await channel.get_partial_message(req.message_id).reply(
                    content=draft.content or None, files=files(), mention_author=False, allowed_mentions=none)
            except discord.HTTPException:
                # 元のメッセージが消えていた場合などは、通常の投稿にする
                await channel.send(content=draft.content or None, files=files(), allowed_mentions=none)
            ok = True
        except Exception as error:
            LOGGER.warning("manual_dm: 回答を投稿できませんでした: %s", type(error).__name__)
        req.delivering = False
        await self._clear(req)
        await self._dm_say(draft, "投稿しました。" if ok else
                           "投稿できませんでした。元のメッセージの削除や、Botの送信・添付権限を確認してください。この依頼は終了します。")


async def setup(bot: commands.Bot):
    await bot.add_cog(ManualDM(bot))
