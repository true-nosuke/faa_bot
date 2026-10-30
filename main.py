import logging
import os
from pathlib import Path

import discord
from discord.ext import commands

BASE_DIR = Path(__file__).parent
GUILD_IDS = [1405430304767873085, 1428304833307475990, 1502415792036249734, 1526887667252334652]
# メンション返答の方式。"ai": Groq AIが返答 / "manual": 原稿チャンネル式（cogs/manual_reply.py）
# manual を使う場合は cogs/manual_reply.py 先頭の設定（MANUAL_REPLY_ENABLED・各ID）も変更する。
REPLY_MODE = "ai"
REPLY_EXTENSION = "cogs.manual_reply" if REPLY_MODE == "manual" else "cogs.ai_reply"
EXTENSIONS = ["cogs.general", "cogs.tasks", "cogs.affinity", "cogs.typst_render", "cogs.m45", "cogs.operations", REPLY_EXTENSION, "cogs.special_days"]

logging.basicConfig(level=logging.INFO)


# Botのトークンを.envファイルから取得
def load_env():
    env = {}
    env_path = BASE_DIR / ".env"
    if env_path.exists():
        with env_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ("\"", "'"):
                    value = value[1:-1]
                env[key.strip()] = value
    return env


class AcademicBot(commands.Bot):
    def __init__(self, config=None):
        self.config = dict(config or {})
        # Typst変換のためメッセージ本文を読む（Developer Portalで MESSAGE CONTENT INTENT をONにすること）
        intents = discord.Intents.default()
        intents.message_content = True
        # 全員ランキングのため既定で有効。Developer PortalのSERVER MEMBERS INTENTもONにする。
        intents.members = self.config.get("ENABLE_MEMBERS_INTENT", "true").lower() in ("1", "true", "yes")
        super().__init__(command_prefix=commands.when_mentioned_or("!"), intents=intents,
                         chunk_guilds_at_startup=intents.members)

    async def setup_hook(self):
        # DB列追加前にもバックアップを1回作成。通常運用の自動保存はOperationsが担当。
        import asyncio
        from cogs.operations import create_backup
        try:
            await asyncio.to_thread(create_backup, self.config)
        except Exception:
            logging.exception("起動前バックアップに失敗しました")
        for ext in EXTENSIONS:
            await self.load_extension(ext)
        await self.sync_to_guilds()

    async def sync_to_guilds(self):
        synced_total = 0
        for guild_id in GUILD_IDS:
            guild = discord.Object(id=guild_id)
            self.tree.copy_global_to(guild=guild)
            try:
                synced = await self.tree.sync(guild=guild)
            except (discord.Forbidden, discord.NotFound):
                logging.warning(
                    "guild %s にアクセスできないため、コマンド同期をスキップします。", guild_id
                )
                continue
            synced_total += len(synced)
        logging.info("Synced %s command(s).", synced_total)

    async def on_ready(self):
        logging.info("Logged in as %s (%s)", self.user.name, self.user.id)


def main():
    env = load_env()
    token = env.get("DISCORD_BOT_TOKEN") or os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN が .env に設定されていません")
    AcademicBot({**os.environ, **env}).run(token)


if __name__ == "__main__":
    main()
