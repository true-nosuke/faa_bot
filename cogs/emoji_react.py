from __future__ import annotations

import asyncio
import io
import json
import logging
import math
import os
import re
import secrets
import time
import unicodedata
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from PIL import Image, ImageOps, ImageSequence

from .tasks import DB_PATH

LOGGER = logging.getLogger(__name__)

MAP_PATH = DB_PATH.parent / "emoji_map.json"

# Discord の絵文字の上限サイズ。これを超える画像は自動で縮小する。
MAX_EMOJI_BYTES = 256 * 1024
MAX_INPUT_BYTES = 10 * 1024 * 1024
MAX_PIXELS = 25_000_000
MAX_FRAMES = 300
APP_EMOJI_LIMIT = 2000        # Botに登録できる絵文字数の目安（実際の上限はDiscord側の仕様）
ALLOWED_FORMATS = {"PNG", "JPEG", "GIF", "WEBP"}

MAX_KEYWORD_LEN = 20
MAX_TRIGGER_KEYWORDS = 3      # 1回の返信に書けるキーワード数
FORBIDDEN_KEYWORD_CHARS = set("<>@#")
NAME_RE = re.compile(r"^[A-Za-z0-9_]{2,32}$")
EMOJI_REF_RE = re.compile(r"^<(a?):(\w{1,32}):(\d{15,25})>$")
EMOJI_ID_RE = re.compile(r"^\d{15,25}$")
PAGE_SIZE = 20

# 荒らし対策のゆるい制限（ユーザーごと）
EDIT_LIMITS = ((60, 5), (3600, 60))          # 登録・変更系: 1分に5回 / 1時間に60回
REACT_LIMITS = ((60, 10), (3600, 120))       # リアクション発動: 1分に10回 / 1時間に120回

# 以前は typst_render.py に直接書かれていた「ファミチキ」を、初回だけ登録として引き継ぐ。
SEED = {
    "ファミチキ": {
        "keyword": "ファミチキ", "emoji_id": 1558708828831223878, "name": "famichiki", "animated": True,
        "owned": False, "added_by": 0, "added_at": "",
    }
}


class EmojiError(Exception):
    """利用者にそのまま見せてよいエラー。"""


def normalize(text: str) -> str:
    """全角半角・大文字小文字の違いを無視して比較するための形。"""
    return unicodedata.normalize("NFKC", text).strip().casefold()


def check_keyword(text: str) -> str:
    text = text.strip()
    if not text:
        raise EmojiError("キーワードを入力してください。")
    if len(text) > MAX_KEYWORD_LEN:
        raise EmojiError(f"キーワードは{MAX_KEYWORD_LEN}文字までです。")
    if any(ch.isspace() or unicodedata.category(ch).startswith("C") for ch in text):
        raise EmojiError("キーワードに空白や制御文字は使えません。")
    if any(ch in FORBIDDEN_KEYWORD_CHARS for ch in text):
        raise EmojiError("キーワードに `<` `>` `@` `#` は使えません。")
    return text


def parse_emoji_ref(text: str) -> tuple[int, bool | None]:
    """`<:name:id>` / `<a:name:id>` / IDだけ、のどれかから (id, animatedか) を取り出す。IDだけのときは不明(None)。"""
    text = text.strip()
    if m := EMOJI_REF_RE.match(text):
        return int(m.group(3)), bool(m.group(1))
    if EMOJI_ID_RE.match(text):
        return int(text), None
    raise EmojiError("`emoji_id` には、絵文字のID（数字）か `<:name:123...>` の形式を指定してください。")


# ---------------- 画像の読み込みと縮小 ----------------
def _png_bytes(im: Image.Image) -> bytes:
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def _gif_bytes(frames: list[Image.Image], durations: list[int]) -> bytes:
    buf = io.BytesIO()
    frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:], duration=durations, loop=0, disposal=2)
    return buf.getvalue()


def _shrink_static(img: Image.Image) -> bytes:
    base = ImageOps.exif_transpose(img).convert("RGBA")
    for side in (512, 256, 128, 96, 64, 48):
        im = base.copy()
        im.thumbnail((side, side), Image.LANCZOS)
        data = _png_bytes(im)
        if len(data) <= MAX_EMOJI_BYTES:
            return data
        data = _png_bytes(im.quantize(colors=256, method=Image.Quantize.FASTOCTREE))
        if len(data) <= MAX_EMOJI_BYTES:
            return data
    raise EmojiError("縮小しても256KB以下にできませんでした。単純な画像で試してください。")


def _shrink_animated(img: Image.Image) -> tuple[bytes, bool]:
    n = getattr(img, "n_frames", 1)
    step = max(1, math.ceil(n / MAX_FRAMES))
    frames: list[Image.Image] = []
    durations: list[int] = []
    for i, frame in enumerate(ImageSequence.Iterator(img)):
        duration = int(frame.info.get("duration") or 100)
        if i % step:
            durations[-1] += duration
            continue
        f = frame.convert("RGBA")
        f.thumbnail((256, 256), Image.LANCZOS)
        frames.append(f)
        durations.append(duration)
    for halve in (False, True, True):
        if halve and len(frames) > 2:
            durations = [durations[i] + (durations[i + 1] if i + 1 < len(durations) else 0) for i in range(0, len(frames), 2)]
            frames = frames[::2]
        for side in (256, 128, 96, 64, 48, 32):
            small = []
            for f in frames:
                g = f.copy()
                g.thumbnail((side, side), Image.LANCZOS)
                small.append(g)
            data = _gif_bytes(small, [max(20, d) for d in durations])
            if len(data) <= MAX_EMOJI_BYTES:
                return data, len(small) > 1
    raise EmojiError("縮小しても256KB以下にできませんでした。短いGIFや単純な画像で試してください。")


def process_image(raw: bytes) -> tuple[bytes, bool]:
    """画像を絵文字として登録できる形にする。(画像データ, アニメか) を返す。256KB超なら縮小する。"""
    if len(raw) > MAX_INPUT_BYTES:
        raise EmojiError("画像が大きすぎます（10MBまで）。")
    try:
        img = Image.open(io.BytesIO(raw))
        fmt = img.format
        width, height = img.size
    except Exception:
        raise EmojiError("画像として読み込めませんでした。PNG・JPG・GIF・WebPの画像を指定してください。") from None
    if fmt not in ALLOWED_FORMATS:
        raise EmojiError("対応している画像形式は PNG・JPG・GIF・WebP です。")
    if width * height > MAX_PIXELS:
        raise EmojiError("画像の解像度が大きすぎます。")
    animated = getattr(img, "n_frames", 1) > 1
    try:
        if len(raw) <= MAX_EMOJI_BYTES and fmt in ("PNG", "JPEG", "GIF") and (not animated or fmt == "GIF"):
            return raw, animated
        if animated:
            return _shrink_animated(img)
        return _shrink_static(img), False
    except EmojiError:
        raise
    except Exception:
        LOGGER.exception("画像の変換に失敗しました")
        raise EmojiError("画像の変換に失敗しました。別の画像で試してください。") from None


class RateLimiter:
    """ユーザーごとの「1分に○回・1時間に○回」の緩い制限。"""

    def __init__(self, limits):
        self.limits = limits
        self._hits: dict[int, deque] = {}

    def allow(self, user_id: int, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        hits = self._hits.setdefault(user_id, deque())
        longest = max(w for w, _ in self.limits)
        while hits and now - hits[0] > longest:
            hits.popleft()
        for window, count in self.limits:
            if sum(1 for t in hits if now - t <= window) >= count:
                return False
        hits.append(now)
        return True


class EmojiReact(commands.Cog):
    """返信で「キーワードだけ」を送ると、返信先にそのキーワードの絵文字リアクションを付ける。キーワードと絵文字は誰でも登録できる。"""

    def __init__(self, bot: commands.Bot, path: Path | None = None):
        self.bot = bot
        self.path = Path(path) if path else MAP_PATH
        self.data: dict[str, dict] = self._load()
        self._lock = asyncio.Lock()
        self._edit_limit = RateLimiter(EDIT_LIMITS)
        self._react_limit = RateLimiter(REACT_LIMITS)

    # ---------- 保存 ----------
    def _load(self) -> dict[str, dict]:
        if not self.path.exists():
            data = {k: dict(v) for k, v in SEED.items()}
            self._write(data)
            return data
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            LOGGER.exception("emoji_map.json を読み込めませんでした。空の状態で起動します（ファイルは上書きしません）。")
            return {}

    @staticmethod
    def _dump(path: Path, data: dict):
        tmp = path.with_name(path.name + ".part")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def _write(self, data: dict):
        self._dump(self.path, data)

    def _save(self):
        self._write(self.data)

    # ---------- 共通 ----------
    @staticmethod
    def _emoji(entry: dict) -> discord.PartialEmoji:
        return discord.PartialEmoji(name=entry["name"], id=int(entry["emoji_id"]), animated=bool(entry.get("animated")))

    def _label(self, entry: dict) -> str:
        return f"{self._emoji(entry)}「{entry['keyword']}」"

    def _users_of(self, emoji_id: int, exclude: str | None = None) -> list[str]:
        return [k for k, v in self.data.items() if int(v["emoji_id"]) == emoji_id and k != exclude]

    async def _keyword_autocomplete(self, interaction: discord.Interaction, current: str):
        key = normalize(current)
        return [app_commands.Choice(name=v["keyword"][:100], value=v["keyword"][:100])
                for k, v in self.data.items() if key in k][:25]

    async def _throttle(self, interaction: discord.Interaction) -> bool:
        if self._edit_limit.allow(interaction.user.id):
            return False
        await interaction.response.send_message("⏳ 操作が続いたので、少し待ってからもう一度お試しください。", ephemeral=True)
        return True

    async def _download(self, url: str) -> bytes | None:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                chunks, total = [], 0
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    total += len(chunk)
                    if total > MAX_INPUT_BYTES:
                        raise EmojiError("画像が大きすぎます（10MBまで）。")
                    chunks.append(chunk)
                return b"".join(chunks)

    async def _fetch_source(self, image: discord.Attachment | None, emoji_id: str | None) -> bytes:
        """`image` か `emoji_id` のどちらか一方から、画像の元データを取得する。"""
        if (image is None) == (emoji_id is None):
            raise EmojiError("`image`（画像）か `emoji_id`（既存の絵文字）の、どちらか一方だけを指定してください。")
        if image is not None:
            if image.size > MAX_INPUT_BYTES:
                raise EmojiError("画像が大きすぎます（10MBまで）。")
            if image.content_type and not image.content_type.startswith("image/"):
                raise EmojiError("画像ファイルを添付してください。")
            return await image.read()
        eid, animated = parse_emoji_ref(emoji_id)
        exts = ["gif"] if animated else ["png"] if animated is False else ["gif", "png"]
        for ext in exts:
            try:
                raw = await self._download(f"https://cdn.discordapp.com/emojis/{eid}.{ext}?size=128")
            except (aiohttp.ClientError, asyncio.TimeoutError):
                raise EmojiError("絵文字の画像を取得できませんでした。時間をおいて試してください。") from None
            if raw:
                if animated is None and ext == "gif":
                    try:
                        if getattr(Image.open(io.BytesIO(raw)), "n_frames", 1) == 1:
                            raw = await self._download(f"https://cdn.discordapp.com/emojis/{eid}.png?size=128") or raw
                    except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
                        pass
                return raw
        raise EmojiError("その絵文字IDの画像が見つかりませんでした。IDが正しいか確認してください。")

    async def _register(self, image, emoji_id, name: str | None) -> discord.Emoji:
        """画像を用意（必要なら縮小）して、Botの絵文字として登録する。"""
        if name is not None and not NAME_RE.match(name):
            raise EmojiError("`name` は半角の英数字と `_` で2〜32文字にしてください。")
        raw = await self._fetch_source(image, emoji_id)
        data, _ = await asyncio.to_thread(process_image, raw)
        name = name or f"e_{secrets.token_hex(4)}"
        try:
            return await self.bot.create_application_emoji(name=name, image=data)
        except discord.HTTPException as error:
            LOGGER.warning("絵文字を登録できませんでした: HTTP %s code=%s %s", error.status, error.code, error.text)
            if "maximum" in (error.text or "").lower():
                raise EmojiError("Botの絵文字枠がいっぱいです。`/emoji slots` で使用数を確認して、不要な絵文字を `/emoji remove` で消してください。") from None
            raise EmojiError(f"Discordに絵文字を登録できませんでした（{(error.text or '不明なエラー')[:100]}）。") from None

    async def _delete_owned(self, entry: dict):
        """Botが作った絵文字だけを、他のキーワードが使っていなければ削除する。"""
        if not entry.get("owned"):
            return
        try:
            emoji = await self.bot.fetch_application_emoji(int(entry["emoji_id"]))
            await emoji.delete()
        except discord.NotFound:
            pass
        except discord.HTTPException:
            LOGGER.warning("絵文字 %s を削除できませんでした。", entry.get("emoji_id"))

    async def _reply(self, interaction: discord.Interaction, text: str, ephemeral: bool = False):
        await interaction.followup.send(text, ephemeral=ephemeral, allowed_mentions=discord.AllowedMentions.none())

    # ---------- コマンド ----------
    emoji = app_commands.Group(name="emoji", description="返信でキーワードを送ると付くリアクション絵文字の管理", guild_only=True)

    @emoji.command(name="add", description="キーワードと絵文字を登録する（画像か、既存の絵文字のどちらか一方を指定）")
    @app_commands.describe(keyword="キーワード（1〜20文字・空白なし）", image="絵文字にする画像（PNG/JPG/GIF/WebP・大きければ自動で縮小）",
                           emoji_id="既存の絵文字のIDか <:name:id>（コピーして登録します）", name="絵文字の内部名（任意・半角英数字と_）")
    async def emoji_add(self, interaction: discord.Interaction, keyword: str, image: discord.Attachment | None = None,
                        emoji_id: str | None = None, name: str | None = None):
        if await self._throttle(interaction):
            return
        try:
            keyword = check_keyword(keyword)
            if normalize(keyword) in self.data:
                raise EmojiError(f"「{keyword}」はすでに登録されています。変更は `/emoji edit`、削除は `/emoji remove` です。")
            if (image is None) == (emoji_id is None):
                raise EmojiError("`image`（画像）か `emoji_id`（既存の絵文字）の、どちらか一方だけを指定してください。")
        except EmojiError as error:
            return await interaction.response.send_message(f"⚠️ {error}", ephemeral=True)
        await interaction.response.defer(thinking=True)
        async with self._lock:
            try:
                if normalize(keyword) in self.data:
                    raise EmojiError(f"「{keyword}」はすでに登録されています。")
                created = await self._register(image, emoji_id, name)
            except EmojiError as error:
                return await self._reply(interaction, f"⚠️ {error}", ephemeral=True)
            self.data[normalize(keyword)] = {
                "keyword": keyword, "emoji_id": created.id, "name": created.name, "animated": created.animated,
                "owned": True, "added_by": interaction.user.id, "added_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            try:
                self._save()
            except OSError:
                del self.data[normalize(keyword)]
                LOGGER.exception("emoji_map.json を保存できませんでした")
                await self._delete_owned({"owned": True, "emoji_id": created.id})
                return await self._reply(interaction, "❌ 保存に失敗したため、登録を取り消しました。", ephemeral=True)
        await self._reply(interaction, f"✅ {created}「{keyword}」を登録しました。\n返信で「{keyword}」とだけ送ると、返信先にこの絵文字が付きます。")

    @emoji.command(name="alias", description="登録済みの絵文字に、別のキーワードを追加する")
    @app_commands.describe(keyword="今あるキーワード", new_keyword="追加するキーワード")
    @app_commands.autocomplete(keyword=_keyword_autocomplete)
    async def emoji_alias(self, interaction: discord.Interaction, keyword: str, new_keyword: str):
        if await self._throttle(interaction):
            return
        try:
            entry = self.data.get(normalize(keyword))
            if entry is None:
                raise EmojiError(f"「{keyword.strip()}」は登録されていません。`/emoji list` で確認できます。")
            new_keyword = check_keyword(new_keyword)
            if normalize(new_keyword) in self.data:
                raise EmojiError(f"「{new_keyword}」はすでに登録されています。")
        except EmojiError as error:
            return await interaction.response.send_message(f"⚠️ {error}", ephemeral=True)
        async with self._lock:
            if normalize(new_keyword) in self.data:
                return await interaction.response.send_message(f"⚠️ 「{new_keyword}」はすでに登録されています。", ephemeral=True)
            self.data[normalize(new_keyword)] = {**entry, "keyword": new_keyword, "owned": entry.get("owned", False),
                                                 "added_by": interaction.user.id,
                                                 "added_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            self._save()
        await interaction.response.send_message(f"✅ {self._emoji(entry)}「{entry['keyword']}」に、別のキーワード「{new_keyword}」を追加しました。",
                                                allowed_mentions=discord.AllowedMentions.none())

    @emoji.command(name="edit", description="絵文字の画像やキーワードを変更する（指定した項目だけ変わります）")
    @app_commands.describe(keyword="変更するキーワード", image="新しい画像（大きければ自動で縮小）",
                           emoji_id="新しい絵文字（既存の絵文字のIDか <:name:id>）", new_keyword="新しいキーワード")
    @app_commands.autocomplete(keyword=_keyword_autocomplete)
    async def emoji_edit(self, interaction: discord.Interaction, keyword: str, image: discord.Attachment | None = None,
                         emoji_id: str | None = None, new_keyword: str | None = None):
        if await self._throttle(interaction):
            return
        key = normalize(keyword)
        try:
            if key not in self.data:
                raise EmojiError(f"「{keyword.strip()}」は登録されていません。`/emoji list` で確認できます。")
            if image is not None and emoji_id is not None:
                raise EmojiError("`image` と `emoji_id` は、どちらか一方だけを指定してください。")
            if image is None and emoji_id is None and new_keyword is None:
                raise EmojiError("変更する項目（`image`・`emoji_id`・`new_keyword`）を指定してください。")
            if new_keyword is not None:
                new_keyword = check_keyword(new_keyword)
                if normalize(new_keyword) != key and normalize(new_keyword) in self.data:
                    raise EmojiError(f"「{new_keyword}」はすでに登録されています。")
        except EmojiError as error:
            return await interaction.response.send_message(f"⚠️ {error}", ephemeral=True)
        await interaction.response.defer(thinking=True)
        notes = []
        async with self._lock:
            entry = self.data.get(key)
            if entry is None:
                return await self._reply(interaction, f"⚠️ 「{keyword.strip()}」は登録されていません。", ephemeral=True)
            backup = json.loads(json.dumps(self.data))
            old_entry = dict(entry)
            created = None
            try:
                if image is not None or emoji_id is not None:
                    created = await self._register(image, emoji_id, None)
                    # 同じ絵文字を使っている別キーワード（alias）も、まとめて新しい絵文字にする。
                    shared = self._users_of(int(old_entry["emoji_id"]))
                    for k in shared:
                        self.data[k].update(emoji_id=created.id, name=created.name, animated=created.animated, owned=True)
                    if len(shared) > 1:
                        notes.append("同じ絵文字を使う別のキーワードも一緒に変わります。")
                if new_keyword is not None and new_keyword != entry["keyword"]:
                    item = self.data.pop(key)
                    item["keyword"] = new_keyword
                    self.data[normalize(new_keyword)] = item
                    key = normalize(new_keyword)
                try:
                    self._save()
                except OSError:
                    self.data = backup
                    LOGGER.exception("emoji_map.json を保存できませんでした")
                    if created is not None:
                        await self._delete_owned({"owned": True, "emoji_id": created.id})
                    return await self._reply(interaction, "❌ 保存に失敗したため、変更を取り消しました。", ephemeral=True)
            except EmojiError as error:
                return await self._reply(interaction, f"⚠️ {error}", ephemeral=True)
            if (image is not None or emoji_id is not None) and not self._users_of(int(old_entry["emoji_id"])):
                await self._delete_owned(old_entry)
            current = self.data[key]
        await self._reply(interaction, f"✅ 変更しました。{self._label(current)}" + (("\n" + " ".join(notes)) if notes else ""))

    @emoji.command(name="remove", description="キーワードを削除する（そのキーワードだけが使っていた絵文字も消えます）")
    @app_commands.describe(keyword="削除するキーワード")
    @app_commands.autocomplete(keyword=_keyword_autocomplete)
    async def emoji_remove(self, interaction: discord.Interaction, keyword: str):
        if await self._throttle(interaction):
            return
        key = normalize(keyword)
        await interaction.response.defer(thinking=True)
        async with self._lock:
            entry = self.data.get(key)
            if entry is None:
                return await self._reply(interaction, f"⚠️ 「{keyword.strip()}」は登録されていません。`/emoji list` で確認できます。", ephemeral=True)
            del self.data[key]
            try:
                self._save()
            except OSError:
                self.data[key] = entry
                LOGGER.exception("emoji_map.json を保存できませんでした")
                return await self._reply(interaction, "❌ 保存に失敗したため、削除を取り消しました。", ephemeral=True)
            kept = bool(self._users_of(int(entry["emoji_id"])))
            if not kept:
                await self._delete_owned(entry)
        await self._reply(interaction, f"🗑️ {self._emoji(entry)}「{entry['keyword']}」を削除しました。"
                          + ("\n-# 同じ絵文字を使う別のキーワードが残っているため、絵文字自体は残しています。" if kept else ""))

    @emoji.command(name="list", description="登録されているキーワードと絵文字を表示")
    @app_commands.describe(page="ページ番号（20件ずつ）")
    async def emoji_list(self, interaction: discord.Interaction, page: app_commands.Range[int, 1, 1000] = 1):
        if not self.data:
            return await interaction.response.send_message("登録はありません。`/emoji add` で追加できます。", ephemeral=True)
        items = sorted(self.data.values(), key=lambda v: v["keyword"])
        pages = max(1, math.ceil(len(items) / PAGE_SIZE))
        page = min(page, pages)
        lines = []
        for v in items[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]:
            by = f" ・<@{v['added_by']}>" if v.get("added_by") else ""
            lines.append(f"{self._emoji(v)}「{v['keyword']}」{by}")
        await interaction.response.send_message(
            f"😀 **登録されている絵文字**（{len(items)}件・{page}/{pages}ページ）\n" + "\n".join(lines)
            + "\n-# 返信でキーワードだけを送ると、返信先に絵文字が付きます。",
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @emoji.command(name="slots", description="Botの絵文字枠の使用数を表示")
    async def emoji_slots(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            emojis = await self.bot.fetch_application_emojis()
        except discord.HTTPException:
            return await self._reply(interaction, "⚠️ 絵文字枠の使用数を取得できませんでした。", ephemeral=True)
        animated = sum(1 for e in emojis if e.animated)
        await self._reply(
            interaction,
            f"📦 Botの絵文字枠：{len(emojis)} / {APP_EMOJI_LIMIT}個（目安）を使用中\n"
            f"静止画 {len(emojis) - animated}個・アニメ {animated}個／登録キーワード {len(self.data)}件\n"
            "-# 実際の上限はDiscordの仕様に従います。いっぱいになったら `/emoji remove` で不要なものを消してください。", ephemeral=True)

    # ---------- 返信でキーワード → リアクション ----------
    def match_keywords(self, content: str) -> list[dict] | None:
        """本文が「登録済みキーワードだけ」（空白区切りで最大3つ）なら、その登録を返す。"""
        parts = content.split()
        if not parts or len(parts) > MAX_TRIGGER_KEYWORDS:
            return None
        entries, seen = [], set()
        for part in parts:
            entry = self.data.get(normalize(part))
            if entry is None:
                return None
            if int(entry["emoji_id"]) not in seen:
                seen.add(int(entry["emoji_id"]))
                entries.append(entry)
        return entries

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None or message.reference is None:
            return
        entries = self.match_keywords(message.content)
        if not entries:
            return
        if message.reference.message_id is None:
            LOGGER.warning("絵文字キーワードの返信先メッセージIDを取得できませんでした。")
            return
        perms = message.channel.permissions_for(message.guild.me)
        if not (perms.add_reactions and perms.read_message_history):
            LOGGER.warning("#%s で「リアクションの追加」または「メッセージ履歴を読む」権限がありません。", message.channel)
            return
        if not self._react_limit.allow(message.author.id):
            LOGGER.info("絵文字リアクションの回数制限により無視しました: user=%s", message.author.id)
            return
        try:
            target = message.reference.cached_message or await message.channel.fetch_message(message.reference.message_id)
            mine = {str(r.emoji) for r in target.reactions if r.me}
            todo = [e for e in entries if str(self._emoji(e)) not in mine]
            if not todo:
                LOGGER.info("すでにリアクション済みのため何もしません: message=%s", target.id)
                return
            added = 0
            for entry in todo:
                try:
                    await target.add_reaction(self._emoji(entry))
                    added += 1
                except discord.NotFound as error:
                    if error.code == 10014:
                        LOGGER.warning("絵文字 %s (%s) が見つかりません。`/emoji remove` で登録を消してください。", entry["name"], entry["emoji_id"])
                        continue
                    raise
            if added and perms.manage_messages:
                await message.delete()
            elif added:
                LOGGER.info("#%s で「メッセージの管理」権限がないため、キーワードの返信は削除しません。", message.channel)
        except discord.NotFound:
            LOGGER.warning("絵文字キーワードの返信または返信先メッセージが見つかりません。")
        except discord.Forbidden:
            LOGGER.warning("絵文字リアクションに必要なDiscord権限がありません。")
        except discord.HTTPException:
            LOGGER.exception("絵文字リアクションの処理に失敗しました。")


async def setup(bot: commands.Bot):
    await bot.add_cog(EmojiReact(bot))
