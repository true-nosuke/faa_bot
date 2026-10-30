from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import shutil
import sys
import zipfile
from pathlib import Path

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

LOGGER = logging.getLogger(__name__)
BASE_DIR = Path(__file__).parent.parent

# ---- 設定（.env の DEPLOY_REPO / DEPLOY_BRANCH / DEPLOY_ADMIN_IDS でも上書き可）----
DEFAULT_REPO = "true-nosuke/faa_bot"
DEFAULT_BRANCH = "main"
STATE_FILE = BASE_DIR / "deploy_state.json"   # gitが無い環境で、最後に反映したコミットを記録する
GIT_TIMEOUT = 60
PIP_TIMEOUT = 300

# 更新・上書きしないもの（zip方式のとき）。gitの場合は .gitignore が守る。
PROTECTED_NAMES = {"ai_state.json", "deploy_state.json", "manual_dm_state.json", "manual_dm_persons.json"}
PROTECTED_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".part"}
PROTECTED_TOP_DIRS = {".git", ".venv", "backups", "m45_cache", "out", "tests", "__pycache__"}


def _protected(rel: Path) -> bool:
    if rel.parts[0] in PROTECTED_TOP_DIRS or "__pycache__" in rel.parts:
        return True
    return rel.name.startswith(".env") or rel.name in PROTECTED_NAMES or rel.suffix in PROTECTED_SUFFIXES


def check_syntax(base: Path) -> str | None:
    """main.py と cogs/*.py の構文を確認する。問題があればその内容を返す。"""
    targets = [base / "main.py", *sorted((base / "cogs").glob("*.py"))]
    for path in targets:
        if not path.exists():
            continue
        try:
            compile(path.read_text(encoding="utf-8"), str(path), "exec")
        except SyntaxError as error:
            return f"{path.relative_to(base)} {error.lineno}行目: {error.msg}"
        except UnicodeDecodeError:
            return f"{path.relative_to(base)}: 文字コードが不正です"
    return None


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""


class UpdateError(Exception):
    pass


class Deploy(commands.Cog):
    """Discordのコマンドから、GitHubの最新コードを取り込んでBotを再起動する（Bot所有者のみ）。"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        config = getattr(bot, "config", {}) or {}
        self.repo = config.get("DEPLOY_REPO") or DEFAULT_REPO
        self.branch = config.get("DEPLOY_BRANCH") or DEFAULT_BRANCH
        self.base = Path(config.get("DEPLOY_BASE_DIR") or BASE_DIR)
        self.admin_ids = {int(x) for x in (config.get("DEPLOY_ADMIN_IDS") or "").replace(" ", "").split(",") if x.isdigit()}
        self._lock = asyncio.Lock()

    deploy = app_commands.Group(name="deploy", description="Botの更新・再起動（所有者のみ）", guild_only=True)

    # ---------- 権限 ----------
    async def _is_admin(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id in self.admin_ids or await self.bot.is_owner(interaction.user)

    async def _deny(self, interaction: discord.Interaction) -> bool:
        if await self._is_admin(interaction):
            return False
        await interaction.response.send_message("この操作はBotの所有者だけが実行できます。", ephemeral=True)
        return True

    # ---------- 共通処理 ----------
    @property
    def use_git(self) -> bool:
        return (self.base / ".git").exists() and shutil.which("git") is not None

    async def _run(self, *cmd: str, timeout: int = GIT_TIMEOUT) -> tuple[int, str]:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=self.base, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise UpdateError(f"`{cmd[0]}` がタイムアウトしました（{timeout}秒）")
        return proc.returncode, out.decode("utf-8", "replace").strip()

    async def _git(self, *args: str) -> str:
        code, out = await self._run("git", *args)
        if code != 0:
            raise UpdateError(f"`git {args[0]}` に失敗しました:\n{out[-600:]}")
        return out

    async def _install_requirements(self):
        code, out = await self._run(sys.executable, "-m", "pip", "install", "-r", "requirements.txt", "--quiet",
                                    timeout=PIP_TIMEOUT)
        if code != 0:
            raise UpdateError(f"依存パッケージのインストールに失敗しました:\n{out[-600:]}")

    # ---------- git方式 ----------
    async def _update_git(self, force: bool) -> dict:
        old = await self._git("rev-parse", "HEAD")
        branch = await self._git("rev-parse", "--abbrev-ref", "HEAD")
        if branch == "HEAD":
            branch = self.branch
        req_before = _file_hash(self.base / "requirements.txt")
        await self._git("fetch", "origin", branch)
        if force:
            await self._git("reset", "--hard", "FETCH_HEAD")
        else:
            try:
                await self._git("merge", "--ff-only", "FETCH_HEAD")
            except UpdateError as error:
                raise UpdateError(
                    f"{error}\n\nコードを手動で上書きしている場合は `force` をTrueにして実行してください"
                    "（追跡中のファイルはGitHubの内容に置き換わります。`.env`・DBなどは消えません）。")
        new = await self._git("rev-parse", "HEAD")
        if new == old:
            return {"changed": False, "sha": new[:7]}
        files = (await self._git("diff", "--name-only", old, new)).splitlines()
        log = await self._git("log", "--oneline", "-n", "5", f"{old}..{new}") if not force else \
            await self._git("log", "--oneline", "-n", "5", new)
        problem = check_syntax(self.base)
        if problem:
            await self._git("reset", "--hard", old)
            raise UpdateError(f"更新後のコードに構文エラーがありました。元に戻したので再起動していません。\n{problem}")
        try:
            if _file_hash(self.base / "requirements.txt") != req_before:
                await self._install_requirements()
        except UpdateError:
            await self._git("reset", "--hard", old)
            raise
        return {"changed": True, "sha": new[:7], "old": old[:7], "files": files, "log": log}

    # ---------- zip方式（gitが使えない環境） ----------
    async def _fetch_latest(self) -> tuple[str, str]:
        url = f"https://api.github.com/repos/{self.repo}/commits/{self.branch}"
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            async with session.get(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "faa-bot"}) as r:
                if r.status != 200:
                    raise UpdateError(f"GitHubから最新コミットを取得できません（HTTP {r.status}）。リポジトリが公開か確認してください。")
                data = await r.json()
        return data["sha"], (data.get("commit", {}).get("message") or "").splitlines()[0]

    async def _download_zip(self, sha: str) -> bytes:
        url = f"https://codeload.github.com/{self.repo}/zip/{sha}"
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as session:
            async with session.get(url, headers={"User-Agent": "faa-bot"}) as r:
                if r.status != 200:
                    raise UpdateError(f"GitHubからコードをダウンロードできません（HTTP {r.status}）。")
                return await r.read()

    def _saved_sha(self) -> str:
        try:
            return json.loads(STATE_FILE_FOR(self.base).read_text(encoding="utf-8")).get("sha", "")
        except (OSError, ValueError):
            return ""

    async def _update_zip(self, force: bool) -> dict:
        sha, subject = await self._fetch_latest()
        old = self._saved_sha()
        if old == sha and not force:
            return {"changed": False, "sha": sha[:7]}
        blob = await self._download_zip(sha)
        req_before = _file_hash(self.base / "requirements.txt")
        backups: dict[Path, bytes | None] = {}
        changed: list[str] = []
        try:
            with zipfile.ZipFile(io.BytesIO(blob)) as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    parts = Path(info.filename).parts[1:]   # 先頭のルートフォルダを除く
                    if not parts:
                        continue
                    rel = Path(*parts)
                    if _protected(rel):
                        continue
                    target = (self.base / rel).resolve()
                    if self.base.resolve() not in target.parents:
                        continue
                    data = zf.read(info)
                    current = target.read_bytes() if target.exists() else None
                    if current == data:
                        continue
                    backups[target] = current
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                    changed.append(str(rel))
            problem = check_syntax(self.base)
            if problem:
                raise UpdateError(f"更新後のコードに構文エラーがありました。元に戻したので再起動していません。\n{problem}")
            if _file_hash(self.base / "requirements.txt") != req_before:
                await self._install_requirements()
        except Exception:
            for path, content in backups.items():
                if content is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(content)
            raise
        STATE_FILE_FOR(self.base).write_text(json.dumps({"sha": sha}), encoding="utf-8")
        return {"changed": True, "sha": sha[:7], "old": old[:7] or "不明", "files": changed, "log": f"{sha[:7]} {subject}"}

    # ---------- 再起動 ----------
    async def _restart_process(self):
        await asyncio.sleep(1.0)
        await self.bot.close()
        logging.shutdown()
        try:
            os.execv(sys.executable, [sys.executable, *sys.argv])
        except OSError:
            raise SystemExit(0)   # 実行し直せない場合は終了（パネル側の自動再起動に任せる）

    # ---------- コマンド ----------
    @deploy.command(name="update", description="GitHubの最新コードを取り込み、変更があればBotを再起動する")
    @app_commands.describe(force="手元のコードを手動で書き換えている場合、GitHubの内容で強制的に置き換える")
    async def update(self, interaction: discord.Interaction, force: bool = False):
        if await self._deny(interaction):
            return
        if self._lock.locked():
            await interaction.response.send_message("別の更新が実行中です。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self._lock:
            try:
                result = await (self._update_git(force) if self.use_git else self._update_zip(force))
            except UpdateError as error:
                await interaction.followup.send(f"❌ 更新できませんでした。\n{error}", ephemeral=True)
                return
            except Exception:
                LOGGER.exception("更新中にエラーが発生しました")
                await interaction.followup.send("❌ 更新中に想定外のエラーが発生しました。ログを確認してください。", ephemeral=True)
                return
            if not result["changed"]:
                await interaction.followup.send(f"✅ すでに最新です（`{result['sha']}`）。再起動はしていません。", ephemeral=True)
                return
            shown = result["files"][:10]
            more = f" ほか{len(result['files']) - 10}件" if len(result["files"]) > 10 else ""
            text = (f"✅ 更新しました `{result['old']}` → `{result['sha']}`（{'git' if self.use_git else 'zip'}方式）\n"
                    f"変更ファイル: {', '.join(f'`{f}`' for f in shown) or 'なし'}{more}\n```\n{result['log'][:600]}\n```\n"
                    "🔄 Botを再起動します。数十秒お待ちください。")
            await interaction.followup.send(text, ephemeral=True)
            LOGGER.info("deploy: %s → %s、再起動します", result["old"], result["sha"])
            asyncio.create_task(self._restart_process())

    @deploy.command(name="restart", description="Botを再起動する（コードは更新しない）")
    async def restart(self, interaction: discord.Interaction):
        if await self._deny(interaction):
            return
        await interaction.response.send_message("🔄 Botを再起動します。数十秒お待ちください。", ephemeral=True)
        LOGGER.info("deploy: 手動再起動")
        asyncio.create_task(self._restart_process())

    @deploy.command(name="status", description="現在のバージョンとGitHubの最新を表示")
    async def status(self, interaction: discord.Interaction):
        if await self._deny(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            latest, subject = await self._fetch_latest()
        except UpdateError as error:
            latest, subject = "", str(error)
        if self.use_git:
            try:
                current = await self._git("rev-parse", "HEAD")
                msg = await self._git("log", "-1", "--format=%s")
            except UpdateError as error:
                current, msg = "", str(error)
            method = "git"
        else:
            current, msg, method = self._saved_sha(), "", "zip（gitなし）"
        state = "✅ 最新" if current and latest and current == latest else ("⚠️ 更新あり" if latest and current else "❓ 比較できません")
        await interaction.followup.send(
            f"**Bot更新状況** {state}\n方式: {method} / リポジトリ: `{self.repo}`（{self.branch}）\n"
            f"現在: `{current[:7] or '不明'}` {msg}\nGitHub最新: `{latest[:7] or '不明'}` {subject}",
            ephemeral=True)


def STATE_FILE_FOR(base: Path) -> Path:
    return base / "deploy_state.json"


async def setup(bot: commands.Bot):
    await bot.add_cog(Deploy(bot))
