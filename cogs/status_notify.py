from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks

LOGGER = logging.getLogger(__name__)

# ---- 設定（.env の STATUS_CHANNEL_ID でも上書き可。0 で通知なし）----
NOTIFY_CHANNEL_ID = 1559499654150750269
STATE_FILE = Path(__file__).parent.parent / "status_state.json"
DISCONNECT_REPORT_SECONDS = 60       # これ以上の接続断だけ知らせる
ERROR_DEDUP_SECONDS = 600            # 同じ種類のエラーは、この間隔で1回だけ知らせる
ERROR_HOURLY_LIMIT = 6               # エラー通知の1時間あたりの上限
SEND_TIMEOUT = 5                     # 通知の送信に待つ最大秒数（停止時に固まらないように）

# 全員が見るチャンネルなので、内部情報（ID・パス・エラー本文・スタックトレース）は載せない。
TEXT_START = "🟢 Botが起動しました。"
TEXT_RESTARTED = "🟢 再起動が完了しました。"
TEXT_UPDATED = "🟢 更新を反映して、再起動が完了しました。"
TEXT_CRASH = "⚠️ 前回は予期せず停止したようです。現在は復旧して動作しています。{gap}"
TEXT_STOP = "🔴 Botを停止します。"
TEXT_RESTARTING = "🔄 Botを再起動します。"
TEXT_UPDATING = "🔄 更新のため、Botを再起動します。"
TEXT_DISCONNECT = "🟡 Discordとの接続が約{minutes}分間不安定でした。現在は復旧しています。"
TEXT_ERROR = "⚠️ 内部エラーが発生しました。（対象：{where}｜種類：{kind}）\n管理者が確認します。"
USER_ERROR_TEXT = "⚠️ エラーが発生しました。しばらくしてからもう一度お試しください。"

EVENT_NAMES = {"on_message": "メッセージ処理", "on_raw_reaction_add": "リアクション処理",
               "on_raw_reaction_remove": "リアクション処理", "on_member_join": "メンバー参加処理"}
IGNORED_APP_ERRORS = (app_commands.CheckFailure, app_commands.CommandOnCooldown, app_commands.TransformerError,
                      app_commands.CommandNotFound)


def _minutes(seconds: float) -> int:
    return max(1, round(seconds / 60))


class StatusNotify(commands.Cog):
    """起動・停止・再起動・予期しないエラーを、指定チャンネルに知らせる。"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        config = getattr(bot, "config", {}) or {}
        try:
            self.channel_id = int(config.get("STATUS_CHANNEL_ID", NOTIFY_CHANNEL_ID))
        except (TypeError, ValueError):
            self.channel_id = NOTIFY_CHANNEL_ID
        self.pending_exit: str | None = None       # Deployなどが "restart" / "update" を設定する
        self._announced = False
        self._stopping = False
        self._disconnected_at: float | None = None
        self._error_seen: dict[str, float] = {}
        self._error_times: list[float] = []
        self._orig_tree_error = None
        self._orig_on_error = None
        self._orig_loop_handler = None

    # ---------- 状態ファイル ----------
    def _read(self) -> dict:
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write(self, **changes):
        data = self._read()
        data.update(changes)
        try:
            tmp = STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            tmp.replace(STATE_FILE)
        except OSError as error:
            LOGGER.warning("status_notify: 状態を保存できません: %s", error)

    # ---------- 送信 ----------
    async def _post(self, text: str):
        if self.channel_id <= 0:
            return
        try:
            channel = self.bot.get_channel(self.channel_id) or await asyncio.wait_for(
                self.bot.fetch_channel(self.channel_id), SEND_TIMEOUT)
            await asyncio.wait_for(channel.send(text, allowed_mentions=discord.AllowedMentions.none(), silent=False),
                                   SEND_TIMEOUT)
        except (discord.HTTPException, asyncio.TimeoutError, AttributeError) as error:
            LOGGER.warning("status_notify: 通知を送れませんでした: %s", type(error).__name__)

    # ---------- 起動 ----------
    async def cog_load(self):
        # エラーの検知を取り付ける（元の処理も残す）
        self._orig_tree_error = self.bot.tree.on_error
        self.bot.tree.on_error = self._on_app_error
        self._orig_on_error = self.bot.on_error
        self.bot.on_error = self._on_event_error
        loop = asyncio.get_running_loop()
        self._orig_loop_handler = loop.get_exception_handler()
        loop.set_exception_handler(self._on_loop_error)
        # ホスティング側の停止（SIGTERM）でも、停止の通知を出して正常終了する
        try:
            loop.add_signal_handler(signal.SIGTERM, lambda: asyncio.create_task(self._graceful_stop()))
        except (NotImplementedError, RuntimeError, ValueError):
            pass
        self.alive_loop.start()

    async def cog_unload(self):
        self.alive_loop.cancel()
        self.bot.tree.on_error = self._orig_tree_error or self.bot.tree.on_error
        if self._orig_on_error:
            self.bot.on_error = self._orig_on_error
        try:
            asyncio.get_running_loop().set_exception_handler(self._orig_loop_handler)
        except RuntimeError:
            pass
        await self._announce_stop()

    @commands.Cog.listener()
    async def on_ready(self):
        if self._announced:
            await self._report_gap()
            return
        self._announced = True
        prev = self._read()
        now = time.time()
        if prev.get("running"):                      # 正常な停止の記録なしに止まっていた
            alive = float(prev.get("last_alive") or 0)
            gap = f"（約{_minutes(now - alive)}分間、応答がなかった可能性があります）" if alive and now - alive >= 90 else ""
            text = TEXT_CRASH.format(gap=gap)
        elif prev.get("exit") == "update":
            text = TEXT_UPDATED
        elif prev.get("exit") == "restart":
            text = TEXT_RESTARTED
        else:
            text = TEXT_START
        self._write(running=True, exit=None, started_at=now, last_alive=now)
        await self._post(text)

    # ---------- 停止 ----------
    async def _announce_stop(self):
        if self._stopping:
            return
        self._stopping = True
        reason = self.pending_exit or "stop"
        self._write(running=False, exit=reason)
        await self._post({"update": TEXT_UPDATING, "restart": TEXT_RESTARTING}.get(reason, TEXT_STOP))

    async def _graceful_stop(self):
        LOGGER.info("SIGTERM を受け取りました。停止します")
        await self._announce_stop()
        await self.bot.close()

    # ---------- 生存記録・接続 ----------
    @tasks.loop(seconds=60)
    async def alive_loop(self):
        if self.bot.is_ready() and not self._stopping:
            self._write(last_alive=time.time())

    @commands.Cog.listener()
    async def on_disconnect(self):
        if not self._stopping and self._disconnected_at is None:
            self._disconnected_at = time.monotonic()

    @commands.Cog.listener()
    async def on_resumed(self):
        await self._report_gap()

    async def _report_gap(self):
        started, self._disconnected_at = self._disconnected_at, None
        if started is not None and not self._stopping:
            gap = time.monotonic() - started
            if gap >= DISCONNECT_REPORT_SECONDS:
                await self._post(TEXT_DISCONNECT.format(minutes=_minutes(gap)))

    # ---------- エラー ----------
    async def _report_error(self, key: str, where: str, error: BaseException | None):
        now = time.monotonic()
        kind = type(error).__name__ if error is not None else "不明"
        full = f"{key}:{kind}"
        if now - self._error_seen.get(full, -1e9) < ERROR_DEDUP_SECONDS:
            return
        self._error_times = [t for t in self._error_times if now - t < 3600]
        if len(self._error_times) >= ERROR_HOURLY_LIMIT:
            return
        self._error_seen[full] = now
        self._error_times.append(now)
        await self._post(TEXT_ERROR.format(where=where, kind=kind))

    async def _on_app_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        original = getattr(error, "original", error)
        command = interaction.command.qualified_name if interaction.command else "不明"
        if isinstance(error, IGNORED_APP_ERRORS):
            return
        if isinstance(original, discord.HTTPException) and getattr(original, "code", None) in (10062, 40060):
            return                                    # 応答期限切れなど。Botの不具合ではない
        LOGGER.error("コマンド /%s でエラー", command, exc_info=original)
        await self._report_error(f"cmd:{command}", f"/{command}", original)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(USER_ERROR_TEXT, ephemeral=True)
            else:
                await interaction.response.send_message(USER_ERROR_TEXT, ephemeral=True)
        except discord.HTTPException:
            pass

    async def _on_event_error(self, event_method: str, *args, **kwargs):
        error = sys.exc_info()[1]
        LOGGER.error("イベント %s でエラー", event_method, exc_info=error)
        if error is not None and not isinstance(error, (asyncio.CancelledError, discord.ConnectionClosed)):
            await self._report_error(f"event:{event_method}", EVENT_NAMES.get(event_method, event_method), error)

    def _on_loop_error(self, loop, context):
        if self._orig_loop_handler:
            self._orig_loop_handler(loop, context)
        else:
            loop.default_exception_handler(context)
        error = context.get("exception")
        if error is None or isinstance(error, (asyncio.CancelledError, ConnectionError)) or self._stopping:
            return
        asyncio.ensure_future(self._report_error("loop", "バックグラウンド処理", error), loop=loop)


async def setup(bot: commands.Bot):
    await bot.add_cog(StatusNotify(bot))
