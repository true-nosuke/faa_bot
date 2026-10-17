from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

import discord
from discord.ext import commands

LOGGER = logging.getLogger(__name__)

# ---- コード上で切り替える設定（変更後はBotを再起動）----
MANUAL_REPLY_ENABLED = True   # True: 手動返答モードを有効 / False: 完全に無効
REPLY_GUILD_ID = 1405430304767873085            # 返答原稿を受け取るサーバーID
REPLY_CHANNEL_ID = 1559837558450561044          # 返答原稿を受け取るチャンネルID
WAIT_TIMEOUT_SECONDS = 600   # 原稿が届かない場合は5分で解除
# 空集合なら、その原稿チャンネルに投稿できる人を受け付ける。
ALLOWED_REPLY_USER_IDS: set[int] = set()
ACCEPT_OTHER_BOT_REPLIES = False  # 別Botの原稿を使う場合だけTrue。自分の投稿は常に無視。
# 原稿チャンネルには質問本文を転載せず、元のメッセージへのリンクだけ通知する。
NOTIFY_REPLY_CHANNEL = True
MAX_TOTAL_ATTACHMENT_BYTES = 8 * 1024 * 1024

BUSY_TEXT = "今は別のメッセージへの返答を待っています。同時に複数のメッセージには対応できません。返答が終わってから、もう一度メンションしてください。"


@dataclass
class PendingReply:
    request: discord.Message
    source_channel: discord.abc.Messageable
    done: asyncio.Event = field(default_factory=asyncio.Event)
    delivering: bool = False
    typing_task: asyncio.Task | None = None
    delivery_task: asyncio.Task | None = None


class ManualReply(commands.Cog):
    """AIは呼ばず、指定チャンネルの原稿を元のメンションへそのまま返す。"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.enabled = MANUAL_REPLY_ENABLED
        self._closing = False
        self._pending: PendingReply | None = None

    async def cog_load(self):
        if self.enabled and (REPLY_GUILD_ID <= 0 or REPLY_CHANNEL_ID <= 0):
            LOGGER.warning("サーバーID・チャンネルIDが未設定です。有効化前に設定してください。")

    async def cog_unload(self):
        self._closing = True
        pending = self._pending
        if pending is None:
            return
        pending.done.set()
        active = [task for task in (pending.typing_task, pending.delivery_task) if task is not None]
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        self._finish(pending)

    def _finish(self, pending: PendingReply):
        pending.done.set()
        if self._pending is pending:
            self._pending = None

    async def _safe_reply(self, message: discord.Message, text: str):
        try:
            await message.reply(text, mention_author=False,
                                allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as error:
            LOGGER.warning("手動返答モードの案内を送れませんでした: HTTP %s", error.status)

    def _source_channel(self):
        channel = self.bot.get_channel(REPLY_CHANNEL_ID)
        guild = getattr(channel, "guild", None)
        if channel is None or guild is None or guild.id != REPLY_GUILD_ID:
            return None
        # テキストチャンネル、スレッドなど、通常メッセージを送れる場所だけ。
        if not isinstance(channel, discord.abc.Messageable) or not hasattr(channel, "permissions_for"):
            return None
        permissions = channel.permissions_for(guild.me)
        if not permissions.view_channel or not permissions.send_messages or not permissions.read_message_history:
            return None
        return channel

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not self.enabled or self._closing or self.bot.user is None or message.guild is None:
            return
        if message.author.id == self.bot.user.id:
            return
        in_source = message.guild.id == REPLY_GUILD_ID and message.channel.id == REPLY_CHANNEL_ID

        # 原稿チャンネルのメッセージは新規質問ではなく、待機中の1件への返答として扱う。
        if in_source:
            if message.author.bot and not ACCEPT_OTHER_BOT_REPLIES:
                return
            if ALLOWED_REPLY_USER_IDS and message.author.id not in ALLOWED_REPLY_USER_IDS:
                return
            pending = self._pending
            if pending is None:
                return  # 待機していないときの原稿は保存・転送しない
            # 遅れて届いた古い原稿イベントは、新しい質問への返答に使わない。
            if message.created_at <= pending.request.created_at:
                return
            if pending.delivering:
                await self._safe_reply(message, "現在、返答を送信中です。このメッセージは転送していません。")
                return
            if len(message.content) > 2000:
                await self._safe_reply(message, "返答文は2000文字以内にしてください。待機は続けています。")
                return
            if not message.content and not message.attachments:
                await self._safe_reply(message, "テキストか添付ファイルを送ってください。スタンプのみの転送は未対応です。")
                return
            target_limit = getattr(pending.request.guild, "filesize_limit", MAX_TOTAL_ATTACHMENT_BYTES)
            if (len(message.attachments) > 10
                    or sum(item.size for item in message.attachments) > MAX_TOTAL_ATTACHMENT_BYTES
                    or any(item.size > target_limit for item in message.attachments)):
                await self._safe_reply(message, "添付は最大10個・合計8MB以内、かつ送信先サーバーの上限以内にしてください。待機は続けています。")
                return
            # awaitする前に送信中へ切り替え、2つの原稿が同時に採用されることを防ぐ。
            pending.delivering = True
            pending.delivery_task = asyncio.create_task(self._deliver(pending, message))
            return

        if message.author.bot:
            return
        mentioned = any(user.id == self.bot.user.id for user in message.mentions)
        if not mentioned:
            return
        if self._pending is not None:
            await self._safe_reply(message, BUSY_TEXT)
            return
        channel = self._source_channel()
        if channel is None:
            await self._safe_reply(message, "手動返答用チャンネルが未設定、またはBotがアクセスできません。管理者に設定を確認してもらってください。")
            return

        # 全サーバーで1件だけ。最初のネットワーク待ちより先に予約する。
        pending = PendingReply(request=message, source_channel=channel)
        self._pending = pending
        pending.typing_task = asyncio.create_task(self._wait(pending))
        if NOTIFY_REPLY_CHANNEL:
            try:
                await channel.send(
                    "📝 返答待ちのメッセージがあります。\n"
                    f"元のメッセージ：{message.jump_url}\n"
                    "このチャンネルへ次に送られた原稿1件を、Botからの返答として転送します。"
                    "質問本文は自動転載していません。",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException as error:
                LOGGER.warning("返答原稿チャンネルへの通知に失敗: HTTP %s", error.status)
                # すでに原稿が採用されたなら、その送信を妨げない。
                if self._pending is pending and not pending.delivering:
                    self._finish(pending)
                    await self._safe_reply(message, "返答用チャンネルに通知できませんでした。時間を置いて、もう一度メンションしてください。")

    async def _wait(self, pending: PendingReply):
        try:
            # discord.pyが入力中表示を定期更新する。元の質問チャンネルに表示する。
            async with pending.request.channel.typing():
                try:
                    await asyncio.wait_for(pending.done.wait(), timeout=WAIT_TIMEOUT_SECONDS)
                except asyncio.TimeoutError:
                    if pending.delivering:
                        # 送信途中で次の質問を受け付けない。送信処理のfinallyで解除する。
                        await pending.done.wait()
                    elif self._pending is pending:
                        self._finish(pending)
                        await self._safe_reply(pending.request, "返答待ちがタイムアウトしました。必要なら、もう一度メンションしてください。")
        except asyncio.CancelledError:
            raise
        except discord.HTTPException as error:
            LOGGER.warning("入力中表示を開始できませんでした: HTTP %s", error.status)
            if self._pending is pending and not pending.delivering:
                self._finish(pending)
                await self._safe_reply(pending.request, "入力中表示を開始できませんでした。Botの送信権限を確認してください。")

    async def _deliver(self, pending: PendingReply, draft: discord.Message):
        files: list[discord.File] = []
        try:
            for attachment in draft.attachments:
                files.append(await attachment.to_file(spoiler=attachment.is_spoiler()))
            # 本文を要約・改変せず、送信者名や転送元のラベルも付けない。
            # 通知を抑止し、原稿内の@everyone等で意図しない通知が飛ばないようにする。
            await pending.request.reply(
                content=draft.content or None, files=files, mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except asyncio.CancelledError:
            raise
        except discord.HTTPException as error:
            LOGGER.warning("手動返答の転送に失敗: HTTP %s", error.status)
            await self._safe_reply(draft, "返答を送れませんでした。元のメッセージが削除されていないか、Botの送信・添付権限を確認してください。待機は解除します。")
        except Exception:
            # 原稿や添付URLをログに残さない。
            LOGGER.warning("手動返答の転送中にエラーが発生しました。待機を解除します。")
            await self._safe_reply(draft, "返答の転送中にエラーが発生しました。待機は解除します。")
        finally:
            for item in files:
                item.close()
            self._finish(pending)


async def setup(bot: commands.Bot):
    await bot.add_cog(ManualReply(bot))
