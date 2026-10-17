from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import io
import logging
import re
import sys
import tempfile
import time
from collections import OrderedDict
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

LOGGER = logging.getLogger(__name__)
WORKER = Path(__file__).parent.parent / "renderer_worker.py"

TRIGGER_EMOJI = "<:typst:1558680485427679422>"
FAMICHIKI_EMOJI = "<a:famichiki:1558708828831223878>"
RENDER_TIMEOUT = 30  # 秒（パッケージの初回ダウンロードを含む）
MAX_CODE_CHARS = 4000
MAX_MATH_SPANS = 10
MAX_TOTAL_BYTES = 8 * 1024 * 1024
USER_COOLDOWN = 5  # 秒
CONCURRENCY = 2

PREAMBLE = """\
#set page(width: auto, height: auto, margin: 14pt, fill: white)
#set text(size: 16pt, lang: "ja", font: ("Noto Sans JP", "Noto Sans CJK JP", "New Computer Modern", "Yu Gothic", "Meiryo", "Hiragino Sans"))
"""

CODE_BLOCK_RE = re.compile(r"```(?:typst|typ)[ \t]*\r?\n(.*?)```", re.DOTALL | re.IGNORECASE)
RAW_TYPST_RE = re.compile(r'(?m)^\s*#import\s+"@preview/[a-z0-9-]+:\d+\.\d+\.\d+"')
ANY_CODE_RE = re.compile(r"```.*?```|`[^`\n]*`", re.DOTALL)
DISPLAY_RE = re.compile(r"\$\$(.+?)\$\$", re.DOTALL)
INLINE_RE = re.compile(r"(?<![\\$])\$(?![\s$])([^$\n]+?)(?<![\s$])\$(?!\$)")
SPACED_RE = re.compile(r"(?<![\\$])\$[ \t]+([^$\n]+?)[ \t]+\$(?!\$)")
OPERATOR_RE = re.compile(r"[\^_=/<>+*()]")
# import は、下の許可リストにある @preview パッケージだけ使える。include と、ファイルなどの import は禁止。
# （cetz の `import draw: *` のように、文字列ではなく名前を指定する import は許可）
ALLOWED_PACKAGES = {"cetz", "fletcher", "physica", "unify", "showybox", "numbly"}
INCLUDE_RE = re.compile(r"\binclude\b")
STRING_IMPORT_RE = re.compile(r'\bimport\s*["(]')  # import "..." / import (...)
ALLOWED_IMPORT_RE = re.compile(r'\bimport\s*"@preview/([a-z0-9-]+):\d+\.\d+\.\d+"')


def has_forbidden_import(code: str) -> bool:
    if INCLUDE_RE.search(code):
        return True
    allowed = [m for m in ALLOWED_IMPORT_RE.finditer(code) if m.group(1) in ALLOWED_PACKAGES]
    return len(STRING_IMPORT_RE.findall(code)) != len(allowed)


def extract_source(content: str) -> str | None:
    """メッセージから、Typstとして変換できそうなソースを取り出す。なければNone。

    - ```typst（または ```typ）のコードブロック → コードとしてそのまま変換
    - $x^2$ / $$x^2$$ / $ x^2 $ → 数式として変換（複数あれば縦に並べる）
    """
    blocks = CODE_BLOCK_RE.findall(content)
    if blocks:
        code = "\n\n".join(b.strip("\n") for b in blocks).strip()
        if not code or len(code) > MAX_CODE_CHARS or has_forbidden_import(code):
            return None
        return PREAMBLE + code

    if RAW_TYPST_RE.search(content):
        code = content.strip()
        if len(code) > MAX_CODE_CHARS or has_forbidden_import(code):
            return None
        return PREAMBLE + code

    text = ANY_CODE_RE.sub(" ", content)  # 他のコードブロック内の $ は無視
    spans: list[tuple[int, str]] = []
    for m in DISPLAY_RE.finditer(text):
        spans.append((m.start(), m.group(1).strip()))
    text_wo_display = DISPLAY_RE.sub(lambda m: " " * len(m.group(0)), text)
    for m in INLINE_RE.finditer(text_wo_display):
        spans.append((m.start(), m.group(1).strip()))
    for m in SPACED_RE.finditer(text_wo_display):
        if OPERATOR_RE.search(m.group(1)):
            spans.append((m.start(), m.group(1).strip()))
    bodies = [b for _, b in sorted(set(spans))]
    if not bodies or len(bodies) > MAX_MATH_SPANS:
        return None
    if sum(len(b) for b in bodies) > MAX_CODE_CHARS or any(has_forbidden_import(b) for b in bodies):
        return None
    return PREAMBLE + "\n".join(f"$ {b} $\n" for b in bodies)


class TypstRender(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.sem = asyncio.Semaphore(CONCURRENCY)
        self.cache: OrderedDict[str, list[bytes]] = OrderedDict()
        self.done: OrderedDict[int, None] = OrderedDict()  # 画像を返信済みのメッセージID
        self.last_used: dict[int, float] = {}
        self.warned_empty = False
        # 診断用カウンタ（/typst-status で表示）
        self.seen = 0
        self.seen_with_content = 0
        self.matched = 0
        self.reacted = 0
        self.last_error = "なし"

    async def cog_load(self):
        # 動かない原因を、起動時にターミナルへ表示する
        if importlib.util.find_spec("typst") is None:
            LOGGER.error("typst がインストールされていません。`python -m pip install typst` を実行してください。")
        if not self.bot.intents.message_content:
            LOGGER.error("message_content インテントが無効です。main.py を確認してください。")
        if not WORKER.exists():
            LOGGER.error("renderer_worker.py が見つかりません: %s", WORKER)
        print(f"[typst_render] 読み込み完了（トリガー: {TRIGGER_EMOJI}）", flush=True)

    # ---- 変換 ----
    async def render(self, source: str) -> tuple[list[bytes] | None, str | None]:
        """(画像のリスト, エラー文) のどちらかを返す。"""
        key = hashlib.sha1(source.encode()).hexdigest()
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key], None

        async with self.sem:
            with tempfile.TemporaryDirectory() as tmp:
                try:
                    proc = await asyncio.create_subprocess_exec(
                        sys.executable, str(WORKER), tmp,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.PIPE,
                        cwd=tmp,
                    )
                    try:
                        _, err = await asyncio.wait_for(
                            proc.communicate(source.encode()), timeout=RENDER_TIMEOUT
                        )
                    except asyncio.TimeoutError:
                        proc.kill()
                        await proc.wait()
                        return None, f"変換に{RENDER_TIMEOUT}秒以上かかったため中止しました。"
                except OSError as e:
                    LOGGER.exception("Typstの変換プロセスを起動できません")
                    return None, f"変換プロセスを起動できませんでした: {e}"

                if proc.returncode != 0:
                    return None, err.decode("utf-8", "replace").strip() or "変換に失敗しました。"
                pages = [p.read_bytes() for p in sorted(Path(tmp).glob("page-*.png"))]

        if not pages:
            return None, "画像が生成されませんでした。"
        if sum(map(len, pages)) > MAX_TOTAL_BYTES:
            return None, "画像が大きすぎます。"
        self.cache[key] = pages
        while len(self.cache) > 30:
            self.cache.popitem(last=False)
        return pages, None

    # ---- 診断 ----
    @app_commands.command(name="typst-status", description="Typst変換の動作状況を確認（診断用）")
    @app_commands.guild_only()
    async def typst_status(self, interaction: discord.Interaction):
        perms = interaction.channel.permissions_for(interaction.guild.me)

        def ok(v: bool) -> str:
            return "✅" if v else "❌"

        lines = [
            f"{ok(importlib.util.find_spec('typst') is not None)} typst パッケージ（このBotのPython環境）",
            f"{ok(WORKER.exists())} renderer_worker.py",
            f"{ok(self.bot.intents.message_content)} コード側の message_content インテント",
            f"{ok(perms.add_reactions)} このチャンネルで「リアクションの追加」",
            f"{ok(perms.read_message_history)} このチャンネルで「メッセージ履歴を読む」",
            f"{ok(perms.attach_files)} このチャンネルで「ファイルを添付」",
            "",
            f"起動後に受け取ったメッセージ: {self.seen} 件（本文あり {self.seen_with_content} 件）",
            f"Typstとして対象になったメッセージ: {self.matched} 件 / {TRIGGER_EMOJI} を付けた: {self.reacted} 件",
            f"直近のエラー: {self.last_error}",
        ]
        if self.seen > 0 and self.seen_with_content == 0:
            lines.append("⚠️ 本文が空で届いています。Developer Portal の MESSAGE CONTENT INTENT をONにしてください。")
        if self.seen == 0:
            lines.append("ℹ️ まだメッセージを受け取っていません。このチャンネルで何か送ってから、もう一度実行してください。")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    # ---- 送信されたメッセージ → 変換できそうなら typst 絵文字を付ける ----
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        if message.content.strip() == "ファミチキ" and message.reference is not None:
            perms = message.channel.permissions_for(message.guild.me)
            if not perms.add_reactions:
                LOGGER.warning("#%s で「リアクションの追加」権限がありません。", message.channel)
                return
            if not perms.manage_messages:
                LOGGER.warning("#%s で「メッセージの管理」権限がありません。", message.channel)
                return
            if message.reference.message_id is None:
                LOGGER.warning("ファミチキ返信の返信先メッセージIDを取得できませんでした。")
                return
            try:
                target = await message.channel.fetch_message(message.reference.message_id)
                await target.add_reaction(FAMICHIKI_EMOJI)
                await message.delete()
            except discord.NotFound:
                LOGGER.warning("ファミチキ返信または返信先メッセージが見つかりません。")
            except discord.Forbidden:
                LOGGER.warning("ファミチキ処理に必要なDiscord権限がありません。")
            except discord.HTTPException:
                LOGGER.exception("ファミチキのリアクション処理に失敗しました。")
            return
        self.seen += 1
        self.seen_with_content += bool(message.content)
        if not message.content and not self.warned_empty and not message.attachments:
            self.warned_empty = True
            LOGGER.warning(
                "メッセージ本文が空で届きました。Developer Portal で MESSAGE CONTENT INTENT がONか確認してください。"
            )
        source = extract_source(message.content)
        if source is None:
            return
        self.matched += 1
        if not message.channel.permissions_for(message.guild.me).add_reactions:
            LOGGER.warning("#%s で「リアクションの追加」権限がありません。", message.channel)
            return
        pages, err = await self.render(source)
        if pages is None:  # 変換できないものには付けない
            self.last_error = (err or "")[:300]
            print(f"[typst_render] 変換できないため {TRIGGER_EMOJI} を付けません: {self.last_error}", flush=True)
            return
        try:
            await message.add_reaction(TRIGGER_EMOJI)
            self.reacted += 1
        except discord.HTTPException as e:
            self.last_error = f"リアクション追加に失敗: {e}"
            print(f"[typst_render] {self.last_error}", flush=True)

    # ---- typst 絵文字が押された → 画像を返信 ----
    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if str(payload.emoji) != TRIGGER_EMOJI or payload.guild_id is None:
            return
        if payload.user_id == self.bot.user.id or payload.message_id in self.done:
            return
        now = time.monotonic()
        if now - self.last_used.get(payload.user_id, 0) < USER_COOLDOWN:
            return
        self.last_used[payload.user_id] = now

        guild = self.bot.get_guild(payload.guild_id)
        channel = self.bot.get_channel(payload.channel_id)
        if guild is None or channel is None:
            return
        perms = channel.permissions_for(guild.me)
        if not (perms.send_messages and perms.attach_files and perms.read_message_history):
            return
        try:
            message = await channel.fetch_message(payload.message_id)
        except discord.HTTPException:
            return
        # Botが付けた typst 絵文字がある（= 変換できると確認済み）メッセージだけ対象にする
        if not any(str(r.emoji) == TRIGGER_EMOJI and r.me for r in message.reactions):
            return
        source = extract_source(message.content)
        if source is None:
            return

        self.done[message.id] = None
        while len(self.done) > 500:
            self.done.popitem(last=False)

        async with channel.typing():
            pages, err = await self.render(source)
        if pages is None:
            self.done.pop(message.id, None)
            await message.reply(
                f"⚠️ Typstの変換に失敗しました。\n```\n{(err or '')[:800]}\n```",
                delete_after=30,
                mention_author=False,
            )
            return
        files = [discord.File(io.BytesIO(b), filename=f"typst-{i + 1}.png") for i, b in enumerate(pages)]
        try:
            await message.reply(files=files, mention_author=False)
        except discord.HTTPException:
            self.done.pop(message.id, None)
            LOGGER.exception("画像の送信に失敗")


async def setup(bot: commands.Bot):
    await bot.add_cog(TypstRender(bot))