from __future__ import annotations

import json
import random
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands

JST = ZoneInfo("Asia/Tokyo")
MESSAGES_PATH = Path(__file__).parent.parent / "messages.json"

# True: 同じ日・同じユーザーなら /omikuji の結果を固定する
OMIKUJI_DAILY_FIXED = True


class Messages:
    """messages.json の読み込み。ファイルが更新されたら自動で再読み込みする。"""

    def __init__(self, path: Path):
        self.path = path
        self._mtime = 0.0
        self._data: dict = {}

    @property
    def data(self) -> dict:
        mtime = self.path.stat().st_mtime
        if mtime != self._mtime:
            with self.path.open("r", encoding="utf-8") as f:
                self._data = json.load(f)
            self._mtime = mtime
        return self._data

    def fixed(self, name: str) -> str:
        entry = self.data.get(name)
        if not entry:
            return "（この定型文はまだ登録されていません）"
        return entry.get("text", "")

    def random(self, name: str, rng: random.Random | None = None) -> str:
        entry = self.data.get(name)
        if not entry or not entry.get("choices"):
            return "（候補がまだ登録されていません）"
        rng = rng or random
        choices = entry["choices"]
        weights = [c.get("weight", 1) for c in choices]
        return rng.choices(choices, weights=weights, k=1)[0]["text"]


class General(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.messages = Messages(MESSAGES_PATH)

    # ---- A. 定型文コマンド ----
    @app_commands.command(name="ping", description="動作確認")
    async def ping(self, interaction: discord.Interaction):
        await interaction.response.send_message("Pong!")

    @app_commands.command(name="help", description="コマンド一覧と使い方を表示")
    async def help(self, interaction: discord.Interaction):
        text = (
            "進捗PちゃんBotは、人間ではなく、プログラムです。\n"
            "/と入力すると、コマンドの候補が出てきます。\n\n"
            "**基本**\n"
            "`/help` このコマンド一覧を表示\n"
            "`/ping` Botの動作確認\n"
            "`/about` このサーバー・Botの説明\n"
            "`/rules` サーバーのルール\n"
            "`/faq` よくある質問を検索\n"
            "`/say` Botに文章を発言させる（管理者向け）\n\n"
            "**進捗管理**\n"
            "`/task panel` 常設操作パネルを設置（管理者用・登録後はボタン操作）\n"
            "`/task add` タスクを登録（大きさは小・中・大のリアクション投票）\n"
            "`/task my` 自分のタスクを表示（すべてのサーバー）\n"
            "`/task user` 指定した人のタスクを表示（このサーバー内）\n"
            "`/task channel` このチャンネルの全員のタスクを表示\n"
            "`/task progress` タスクの進捗を1%単位で更新・一言を追加（省略すると自分のタスク一覧）\n"
            "`/task delete` タスクを削除（省略すると自分のタスク一覧）\n"
            "`/pick-task` 自分の進行中タスクからランダムに選択\n\n"
            "進捗の質問では、1%単位の入力・一言・今回の回答スキップができます。\n\n"
            "**好感度**\n"
            "`/affinity me` 自分の好感度・順位・直近の増減を表示（自分にだけ）\n"
            "`/affinity ranking` キャッシュによる好感度ランキング（全員に表示・API照会なし）\n\n"
            "**ランダム・便利**\n"
            "`/omikuji` 今日の運勢\n"
            "`/tip` 勉強法・研究のヒント\n"
            "`/quote` 名言・科学のトリビア\n"
            "`/cheer` 応援メッセージ\n"
            "`/dice` サイコロを振る\n"
            "`/choose` 候補から1つ選択\n"
            "`/m45` 部誌 M45 の該当ページの PDF リンクを送る\n\n"
            "**Typst**\n"
            "Typstのコード（```typst ... ``` または #import から始まる本文）・数式を送信すると、変換可能な場合は "
            "Typst絵文字が付きます。そのリアクションを付けると画像が返信されます。\n"
            "`/typst-status` Typst変換の動作状況を確認"
        )
        await interaction.response.send_message(text, ephemeral=True)

    @app_commands.command(name="about", description="このサーバー・Botの説明")
    async def about(self, interaction: discord.Interaction):
        await interaction.response.send_message(self.messages.fixed("about"))

    @app_commands.command(name="rules", description="サーバーのルール")
    async def rules(self, interaction: discord.Interaction):
        await interaction.response.send_message(self.messages.fixed("rules"))

    @app_commands.command(name="faq", description="よくある質問")
    @app_commands.describe(keyword="キーワード（省略すると一覧を表示）")
    async def faq(self, interaction: discord.Interaction, keyword: str | None = None):
        entries: dict = self.messages.data.get("faq", {}).get("entries", {})
        if not keyword:
            keys = "、".join(f"`{k}`" for k in entries) or "（未登録）"
            await interaction.response.send_message(
                f"キーワードを指定してください: {keys}", ephemeral=True
            )
            return
        for key, answer in entries.items():
            if keyword in key or key in keyword:
                await interaction.response.send_message(f"**{key}**\n{answer}")
                return
        await interaction.response.send_message(
            f"「{keyword}」に当てはまるFAQは見つかりませんでした。", ephemeral=True
        )

    @faq.autocomplete("keyword")
    async def faq_autocomplete(self, interaction: discord.Interaction, current: str):
        entries = self.messages.data.get("faq", {}).get("entries", {})
        return [
            app_commands.Choice(name=k, value=k) for k in entries if current in k
        ][:25]

    @app_commands.command(name="say", description="入力した文章をBotが代わりに発言")
    @app_commands.describe(message="発言させる文章")
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.guild_only()
    async def say(self, interaction: discord.Interaction, message: str):
        await interaction.response.send_message(
            message, allowed_mentions=discord.AllowedMentions.none()
        )

    # ---- C. ランダムコマンド ----
    @app_commands.command(name="omikuji", description="今日の運勢")
    async def omikuji(self, interaction: discord.Interaction):
        rng = None
        if OMIKUJI_DAILY_FIXED:
            today = datetime.now(JST).date().isoformat()
            rng = random.Random(f"{today}-{interaction.user.id}")
        await interaction.response.send_message(
            f"{interaction.user.mention} の今日の運勢は…\n{self.messages.random('omikuji', rng)}"
        )

    @app_commands.command(name="tip", description="勉強法・研究のヒント")
    async def tip(self, interaction: discord.Interaction):
        await interaction.response.send_message(self.messages.random("tip"))

    @app_commands.command(name="quote", description="名言・科学のトリビア")
    async def quote(self, interaction: discord.Interaction):
        await interaction.response.send_message(self.messages.random("quote"))

    @app_commands.command(name="cheer", description="応援メッセージ")
    async def cheer(self, interaction: discord.Interaction):
        await interaction.response.send_message(self.messages.random("cheer"))

    @app_commands.command(name="dice", description="サイコロを振る")
    @app_commands.describe(sides="面の数（既定6）", count="個数（既定1）")
    async def dice(
        self,
        interaction: discord.Interaction,
        sides: app_commands.Range[int, 2, 1000] = 6,
        count: app_commands.Range[int, 1, 20] = 1,
    ):
        rolls = [random.randint(1, sides) for _ in range(count)]
        detail = f"（{' + '.join(map(str, rolls))}）" if count > 1 else ""
        await interaction.response.send_message(
            f"🎲 {count}d{sides} → **{sum(rolls)}** {detail}"
        )

    @app_commands.command(name="choose", description="候補から1つ選ぶ")
    @app_commands.describe(options="候補をスペースか「,」「、」で区切って入力（例: 数学 物理 化学）")
    async def choose(self, interaction: discord.Interaction, options: str):
        items = [o for o in re.split(r"[,\s、，]+", options) if o]
        if len(items) < 2:
            await interaction.response.send_message(
                "候補を2つ以上入力してください。（例: `数学 物理 化学`）", ephemeral=True
            )
            return
        await interaction.response.send_message(
            f"🎯 **{random.choice(items)}** に決定！",
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(General(bot))
