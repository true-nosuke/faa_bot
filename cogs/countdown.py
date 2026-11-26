from __future__ import annotations

import io
import json
import logging
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks
from PIL import Image, ImageDraw, ImageFont

LOGGER = logging.getLogger(__name__)
JST = ZoneInfo("Asia/Tokyo")

# ---- 設定（.env の COUNTDOWN_CHANNEL_ID でも上書き可）----
DEFAULT_CHANNEL_ID = 1530498317220577390
STATE_FILE = Path(__file__).parent.parent / "countdown_state.json"
POST_TIME = time(6, 0, tzinfo=JST)  # 毎朝6時に送信

# ---- 画像合成（ベース画像の「50」だけを差し替える）----
BASE_IMAGE = Path(__file__).parent.parent / "assets" / "countdown_base.jpg"
FONT_PATH = Path(__file__).parent.parent / "fonts" / "NotoSansCJKjp-Regular.otf"
REF_SIZE = (1179, 717)          # ベース画像のサイズ（座標の基準）
PATCH_BOX = (336, 240, 431, 330)  # 元の数字を消す範囲
NUMBER_CENTER = (386, 285)      # 数字の中心
NUMBER_MAX_WIDTH = 118          # 右の「日」に当たらない最大幅
NUMBER_HEIGHT = 80              # 元の数字の高さ
NUMBER_COLOR = (35, 35, 35)


def load_state(path: Path = STATE_FILE) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or "base_days" not in data or "base_date" not in data:
        return None
    return data


def save_state(state: dict, path: Path = STATE_FILE) -> None:
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def remaining_days(state: dict, today: date) -> int:
    """設定した日（base_date）時点の base_days から、経過日数を引いた残り日数。"""
    base = date.fromisoformat(state["base_date"])
    return int(state["base_days"]) - (today - base).days


def render_image(days: int) -> bytes:
    """ベース画像の数字だけを days に差し替えた PNG のバイト列を返す。"""
    im = Image.open(BASE_IMAGE).convert("RGB")
    w, h = im.size
    sx, sy = w / REF_SIZE[0], h / REF_SIZE[1]
    draw = ImageDraw.Draw(im)
    x0, y0, x1, y1 = (int(PATCH_BOX[0] * sx), int(PATCH_BOX[1] * sy),
                      int(PATCH_BOX[2] * sx), int(PATCH_BOX[3] * sy))
    # 紙の色は数字の左隣の余白からサンプリングする
    strip = im.crop((max(0, x0 - int(28 * sx)), y0, x0 - int(4 * sx), y1))
    raw = strip.tobytes()
    pixels = sorted((tuple(raw[i:i + 3]) for i in range(0, len(raw), 3)),
                    key=lambda p: p[0] + p[1] + p[2])
    draw.rectangle([x0, y0, x1, y1], fill=pixels[len(pixels) // 2])
    text = str(days)
    cx, cy = NUMBER_CENTER[0] * sx, NUMBER_CENTER[1] * sy
    size = int(NUMBER_HEIGHT * 1.6 * sy)
    box = (0, 0, 0, 0)
    font = ImageFont.truetype(str(FONT_PATH), size)
    while size > 10:
        font = ImageFont.truetype(str(FONT_PATH), size)
        box = draw.textbbox((0, 0), text, font=font)
        if box[2] - box[0] <= NUMBER_MAX_WIDTH * sx and box[3] - box[1] <= NUMBER_HEIGHT * sy:
            break
        size -= 2
    draw.text((cx - (box[2] - box[0]) / 2 - box[0], cy - (box[3] - box[1]) / 2 - box[1]),
              text, font=font, fill=NUMBER_COLOR)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


class Countdown(commands.Cog):
    """共通テストまでのカウントダウン画像を、毎朝6時に決まったチャンネルへ送る。"""

    countdown = app_commands.Group(name="countdown", description="カウントダウン", guild_only=True)

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        config = getattr(bot, "config", {}) or {}
        try:
            self.channel_id = int(config.get("COUNTDOWN_CHANNEL_ID", DEFAULT_CHANNEL_ID))
        except (TypeError, ValueError):
            self.channel_id = DEFAULT_CHANNEL_ID

    async def cog_load(self):
        self.daily_post.start()

    async def cog_unload(self):
        self.daily_post.cancel()

    @staticmethod
    def _today() -> date:
        return datetime.now(JST).date()

    async def _send_image(self, days: int) -> None:
        channel = self.bot.get_channel(self.channel_id)
        if channel is None:
            channel = await self.bot.fetch_channel(self.channel_id)
        file = discord.File(io.BytesIO(render_image(days)), filename="countdown.png")
        await channel.send(file=file)

    # ---------- 毎朝6時の自動送信 ----------
    @tasks.loop(time=POST_TIME)
    async def daily_post(self):
        state = load_state()
        if not state or not state.get("enabled", True):
            return
        days = remaining_days(state, self._today())
        if days < 0:
            # 対象日を過ぎたら自動で止める
            state["enabled"] = False
            save_state(state)
            LOGGER.info("カウントダウンは対象日を過ぎたため自動停止しました")
            return
        try:
            await self._send_image(days)
        except Exception:
            LOGGER.exception("カウントダウンの定時送信に失敗しました")

    @daily_post.before_loop
    async def before_daily_post(self):
        await self.bot.wait_until_ready()

    # ---------- コマンド ----------
    @countdown.command(name="set", description="カウントダウンの残り日数を設定する（毎朝6時に自動送信）")
    @app_commands.describe(days="今日からあと何日か（0以上）")
    async def set_days(self, interaction: discord.Interaction, days: app_commands.Range[int, 0, 36500]):
        today = self._today()
        save_state({"base_days": days, "base_date": today.isoformat(), "enabled": True})
        file = discord.File(io.BytesIO(render_image(days)), filename="countdown.png")
        await interaction.response.send_message(
            f"✅ カウントダウンを **あと{days}日** に設定しました。毎朝6時に <#{self.channel_id}> へ送信します。",
            file=file, ephemeral=True)

    @countdown.command(name="send", description="カウントダウン画像を今すぐ送信する")
    async def send_now(self, interaction: discord.Interaction):
        state = load_state()
        if not state:
            return await interaction.response.send_message(
                "⚠️ カウントダウンが設定されていません。`/countdown set` で設定してください。", ephemeral=True)
        days = remaining_days(state, self._today())
        if days < 0:
            return await interaction.response.send_message(
                "⚠️ 対象日を過ぎているため送信しませんでした。`/countdown set` で再設定してください。", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        try:
            await self._send_image(days)
        except Exception:
            LOGGER.exception("カウントダウンの手動送信に失敗しました")
            return await interaction.followup.send("⚠️ 送信できませんでした。", ephemeral=True)
        await interaction.followup.send(f"✅ <#{self.channel_id}> へ送信しました（あと{days}日）。", ephemeral=True)

    @countdown.command(name="show", description="現在のカウントダウン設定を確認する")
    async def show(self, interaction: discord.Interaction):
        state = load_state()
        if not state:
            return await interaction.response.send_message(
                "⚠️ カウントダウンが設定されていません。`/countdown set` で設定してください。", ephemeral=True)
        days = remaining_days(state, self._today())
        status = "有効" if state.get("enabled", True) else "停止中"
        text = (f"設定：{state['base_date']} の時点で あと{state['base_days']}日\n"
                f"今日時点：**あと{days}日**\n"
                f"毎朝6時の送信：{status}（送信先 <#{self.channel_id}>）")
        if days >= 0:
            file = discord.File(io.BytesIO(render_image(days)), filename="countdown.png")
            await interaction.response.send_message(text, file=file, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)

    @countdown.command(name="off", description="毎朝6時の自動送信を止める")
    async def off(self, interaction: discord.Interaction):
        state = load_state()
        if not state:
            return await interaction.response.send_message(
                "⚠️ カウントダウンが設定されていません。", ephemeral=True)
        state["enabled"] = False
        save_state(state)
        await interaction.response.send_message("⏸️ 毎朝6時の自動送信を止めました。", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Countdown(bot))
