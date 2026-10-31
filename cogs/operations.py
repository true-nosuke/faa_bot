from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import sqlite3
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
from aiohttp import web
from discord.ext import commands, tasks

from .tasks import DB_PATH

LOGGER = logging.getLogger(__name__)
BASE_DIR = DB_PATH.parent


def integer_setting(config, key, default, low, high):
    try:
        return min(high, max(low, int(config.get(key, default))))
    except (TypeError, ValueError):
        return default


HEARTBEAT_HOSTS = {"hc-ping.com", "heartbeat.uptimerobot.com"}


def validate_heartbeat_url(url: str):
    """秘密のURLを誤って別の宛先に送らないよう、許可したサービスのHTTPS URLだけを受け付ける。"""
    parsed = urlparse(url)
    if (parsed.scheme != "https" or parsed.hostname not in HEARTBEAT_HOSTS or parsed.username or parsed.password
            or parsed.port not in (None, 443)):
        raise ValueError("HEARTBEAT_URLは、Healthchecks.io（hc-ping.com）かUptimeRobotのHTTPSハートビートURLを設定してください")


def create_backup(config: dict) -> Path | None:
    """SQLiteのオンラインバックアップで整合したDBを保存。秘密の.envは含めない。"""
    if not DB_PATH.exists():
        return None
    directory = Path(config.get("BACKUP_DIR") or BASE_DIR / "backups").expanduser()
    if not directory.is_absolute():
        directory = BASE_DIR / directory
    directory.mkdir(parents=True, exist_ok=True)
    keep = integer_setting(config, "BACKUP_KEEP", 56, 2, 1000)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    final = directory / f"bot-backup-{stamp}.zip"
    partial = directory / f".bot-backup-{stamp}.partial"
    try:
        with tempfile.TemporaryDirectory(prefix="pbot-backup-") as work:
            snapshot = Path(work) / "bot.db"
            source = sqlite3.connect(DB_PATH.resolve().as_uri()+"?mode=ro", uri=True, timeout=10)
            dest = sqlite3.connect(snapshot)
            deadline = time.monotonic() + 60
            def progress(status, remaining, total):
                if time.monotonic() > deadline:
                    raise TimeoutError("DBバックアップが60秒以内に完了しませんでした")
            try:
                source.backup(dest, pages=128, progress=progress, sleep=0.05)
                if dest.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("DBバックアップの整合性検証に失敗しました")
            finally:
                dest.close(); source.close()
            manifest = {"created_at": datetime.now(timezone.utc).isoformat(), "files": {}}
            with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED) as archive:
                for path in (snapshot, BASE_DIR / "messages.json"):
                    if path.exists():
                        raw = path.read_bytes()
                        archive.writestr(path.name, raw)
                        manifest["files"][path.name] = hashlib.sha256(raw).hexdigest()
                archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            with zipfile.ZipFile(partial) as archive:
                if archive.testzip() is not None:
                    raise RuntimeError("バックアップZIPの検証に失敗しました")
            os.replace(partial, final)
        # 完成した新バックアップを検証してから世代を整理する。
        for old in sorted(directory.glob("bot-backup-*.zip"), reverse=True)[keep:]:
            old.unlink()
        return final
    finally:
        partial.unlink(missing_ok=True)


class Operations(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.config = getattr(bot, "config", {})
        self.runner = None
        self.session = None
        # HEARTBEAT_URL（Healthchecks.io / UptimeRobot）。旧設定名 UPTIMEROBOT_HEARTBEAT_URL も使える。
        self.heartbeat_url = (self.config.get("HEARTBEAT_URL") or self.config.get("UPTIMEROBOT_HEARTBEAT_URL") or "").strip()
        if self.heartbeat_url:
            validate_heartbeat_url(self.heartbeat_url)

    async def cog_load(self):
        interval = integer_setting(self.config, "BACKUP_INTERVAL_HOURS", 6, 1, 168)
        self.backup_loop.change_interval(hours=interval)
        self.backup_loop.start()
        # 公開しない限り外部からアクセス不可。DBやトークンを返すルートはない。
        port = integer_setting(self.config, "HEALTH_PORT", 0, 0, 65535)
        if port:
            app = web.Application()
            app.router.add_get("/health", self.health)
            self.runner = web.AppRunner(app, access_log=None)
            await self.runner.setup()
            try:
                await web.TCPSite(self.runner, self.config.get("HEALTH_HOST", "127.0.0.1"), port).start()
            except OSError:
                LOGGER.exception("ヘルスチェック用ポートを開けませんでした。Botの他機能は継続します")
                await self.runner.cleanup(); self.runner = None
        if self.heartbeat_url:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
            self.heartbeat_loop.change_interval(seconds=integer_setting(self.config,"HEARTBEAT_SECONDS",60,30,3600))
            self.heartbeat_loop.start()

    async def cog_unload(self):
        self.backup_loop.cancel(); self.heartbeat_loop.cancel()
        if self.runner: await self.runner.cleanup()
        if self.session: await self.session.close()

    def healthy(self):
        cog = self.bot.get_cog("Tasks")
        if not self.bot.is_ready() or self.bot.is_closed() or cog is None or not cog.check_tasks.is_running():
            return False
        if cog.last_check_at is None or (datetime.now(cog.last_check_at.tzinfo)-cog.last_check_at).total_seconds() > 180:
            return False
        if not math.isfinite(self.bot.latency) or self.bot.latency > 60:
            return False
        try:
            cog.store.conn.execute("SELECT id FROM tasks LIMIT 1").fetchone()
        except sqlite3.Error:
            return False
        return True

    async def health(self, request):
        healthy = self.healthy()
        return web.json_response({"status": "ok" if healthy else "unhealthy"}, status=200 if healthy else 503)

    @tasks.loop(hours=6)
    async def backup_loop(self):
        try:
            path = await asyncio.to_thread(create_backup, self.config)
            if path: LOGGER.info("データの自動バックアップを作成しました")
        except Exception:
            LOGGER.exception("自動バックアップに失敗しました")

    @tasks.loop(seconds=60)
    async def heartbeat_loop(self):
        if not self.healthy(): return
        try:
            async with self.session.get(self.heartbeat_url, allow_redirects=False) as response:
                if response.status >= 400:
                    LOGGER.warning("ハートビートの送信に失敗: HTTP %s",response.status)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            # 例外メッセージに秘密のURLが含まれる可能性があるので出力しない。
            LOGGER.warning("ハートビートの送信に失敗しました")

    @heartbeat_loop.before_loop
    async def before_heartbeat(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(Operations(bot))
