from __future__ import annotations

import json
import logging
import math
import sqlite3
import time as _time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

from .tasks import DB_PATH

LOGGER = logging.getLogger(__name__)
JST = ZoneInfo("Asia/Tokyo")

# ---- 調整用の数値（旧ポイントは維持。新しい行動から適用）----
DAILY_GAIN_LIMIT = 30          # 回答・チャット合計の1日上限
ANSWER_UP = 4                 # 進捗が上がった回答。ランダムではなく固定
ANSWER_SAME = 1
COMPLETE_BASE = 15
COMPLETE_EARLY_PER_DAY = 2
COMPLETE_EARLY_MAX = 10
COMPLETE_DAILY_LIMIT = 100     # 完了＋依頼ボーナスの合計上限（大量分割対策）
REQUEST_BONUS = 5             # 他人が登録し、本人が承認したタスクの完了
REQUEST_DAILY_LIMIT = 20       # 相互依頼による大量加点を抑える
EXPIRE_BASE = 10
EXPIRE_PER_MISSING = 0.2
EXPIRE_RISK = 1.25
SIZE_MULTIPLIERS = {"small": 0.5, "medium": 1.0, "big": 2.0}
CHAT_POINTS = 1               # 抽選なし
CHAT_COOLDOWN = 600           # 10分に1回まで
CHAT_DAILY_LIMIT = 5          # チャット由来の加点は1日最大5
IDLE_GRACE_DAYS = 5
IDLE_PENALTY = 3
IDLE_PENALTY_MAX = 30
IDLE_MAX_GAP = 10
RANKING_PAGE_SIZE = 10
RANKING_CACHE_SEC = 300       # ランキング結果は5分使い回す

LABELS = {
    "answer": "進捗の回答",
    "complete": "タスク完了",
    "request": "依頼タスクボーナス",
    "expire": "期限切れ",
    "chat": "チャット",
    "idle": "タスクなし",
    "admin": "管理者による調整",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS affinity (
    user_id      INTEGER PRIMARY KEY,
    points       INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT,
    uses_tasks   INTEGER NOT NULL DEFAULT 0,
    idle_days    INTEGER NOT NULL DEFAULT 0,
    idle_penalty INTEGER NOT NULL DEFAULT 0,
    idle_checked TEXT
);
CREATE TABLE IF NOT EXISTS affinity_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    delta      INTEGER NOT NULL,
    reason     TEXT NOT NULL,
    ref_id     INTEGER,
    note       TEXT,
    day        TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS affinity_events (
    user_id INTEGER NOT NULL, reason TEXT NOT NULL, ref_id INTEGER NOT NULL,
    PRIMARY KEY(user_id,reason,ref_id)
);
CREATE INDEX IF NOT EXISTS idx_affinity_log_user_day ON affinity_log (user_id, day);
"""


def rnd(x: float) -> int:
    """四捨五入（0.5 は切り上げ）。"""
    return math.floor(x + 0.5 + 1e-9)


def size_mult(size: str) -> float:
    return SIZE_MULTIPLIERS.get(size, 1.0)


def complete_points(size: str, due: str, today: date) -> int:
    remaining = max(0, (date.fromisoformat(due) - today).days)
    early = min(COMPLETE_EARLY_MAX, COMPLETE_EARLY_PER_DAY * remaining)
    return rnd((COMPLETE_BASE + early) * size_mult(size))


def expire_points(size: str, percent: int) -> int:
    base = EXPIRE_BASE + (100 - percent) * EXPIRE_PER_MISSING
    return -rnd(base * size_mult(size) * EXPIRE_RISK)


class RankingView(discord.ui.View):
    """表示中のサーバーのローカルキャッシュだけでページを切り替える。"""
    def __init__(self, cog: Affinity, origin: discord.Interaction, page: int, pages: int):
        super().__init__(timeout=300)
        self.cog, self.origin, self.page = cog, origin, page
        self._buttons(pages)

    def _buttons(self, pages: int):
        self.previous.disabled = self.page <= 0
        self.next_page.disabled = self.page >= pages - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.origin.user.id:
            await interaction.response.send_message(
                "ページ操作は実行した人だけが使えます。`/affinity ranking`で自分の一覧を開けます。", ephemeral=True)
            return False
        return True

    async def _change(self, interaction: discord.Interaction, offset: int):
        error = self.cog._ranking_error(interaction.guild)
        if error:
            return await interaction.response.send_message(error, ephemeral=True)
        rows = self.cog._ranking_rows(interaction.guild)
        text, self.page, pages = self.cog._ranking_text(rows, self.page + offset)
        self._buttons(pages)
        await interaction.response.edit_message(content=text, view=self,
                                                allowed_mentions=discord.AllowedMentions.none())

    @discord.ui.button(label="前の10人", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._change(interaction, -1)

    @discord.ui.button(label="次の10人", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._change(interaction, 1)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        try:
            await self.origin.edit_original_response(view=self)
        except discord.HTTPException:
            pass


class Affinity(commands.Cog):
    affinity = app_commands.Group(name="affinity", description="Botからの好感度", guild_only=True)

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = sqlite3.connect(DB_PATH, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()
        self._chat_last: dict[int, float] = {}
        self._rank_cache: dict[int, tuple[float, list[tuple[int, str, int]]]] = {}
        # 起動時に取得する全メンバーキャッシュを使用。コマンドごとのメンバー照会は行わない。

    async def cog_load(self):
        self._backfill()
        self.idle_loop.start()

    async def cog_unload(self):
        self.idle_loop.cancel()
        self.db.close()

    # ---------- 内部処理 ----------
    @staticmethod
    def _today(now: datetime) -> str:
        return now.astimezone(JST).date().isoformat()

    def _ensure(self, user_id: int):
        self.db.execute("INSERT OR IGNORE INTO affinity (user_id) VALUES (?)", (user_id,))

    def _gained_today(self, user_id: int, day: str) -> int:
        row = self.db.execute(
            "SELECT COALESCE(SUM(delta), 0) FROM affinity_log "
            "WHERE user_id=? AND day=? AND reason IN ('answer','chat')",
            (user_id, day),
        ).fetchone()
        return row[0]

    def _add(self, user_id: int, delta: int, reason: str, now: datetime, *,
             ref_id: int | None = None, note: str | None = None,
             capped: bool = False, day: str | None = None) -> int:
        """ポイントを増減して履歴に残す。実際に変わった量を返す（上限で0になれば何もしない）。"""
        day = day or self._today(now)
        if capped and delta > 0:
            delta = min(delta, max(0, DAILY_GAIN_LIMIT - self._gained_today(user_id, day)))
        if delta == 0:
            return 0
        with self.db:
            self._ensure(user_id)
            self.db.execute(
                "UPDATE affinity SET points = points + ?, updated_at = ? WHERE user_id = ?",
                (delta, now.isoformat(), user_id),
            )
            self.db.execute(
                "INSERT INTO affinity_log (user_id, delta, reason, ref_id, note, day, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, delta, reason, ref_id, note, day, now.isoformat()),
            )
        self._rank_cache.clear()
        return delta

    def adjust_by_admin(self, user_id: int, amount: int, actor_id: int, guild_id: int,
                        message_id: int, reason: str, now: datetime) -> tuple[int, int] | None:
        """調整・監査履歴・処理済み印を同時保存する。日次上限やタスク利用状態は変えない。"""
        reason = reason.strip() or "理由未指定"
        if amount == 0:
            raise ValueError("増減値には0以外の整数を指定してください。")
        if len(reason) > 200:
            raise ValueError("理由は200文字以内にしてください。")
        with self.db:
            event = self.db.execute(
                "INSERT OR IGNORE INTO affinity_events (user_id,reason,ref_id) VALUES (?,'admin',?)",
                (user_id, message_id),
            )
            if event.rowcount == 0:
                return None
            self._ensure(user_id)
            row = self.db.execute("SELECT points FROM affinity WHERE user_id=?", (user_id,)).fetchone()
            before = row["points"]
            after = before + amount
            # SQLiteが整数をREALへ変換して精度を落とさないようにする。
            if not -(2**63) <= amount < 2**63 or not -(2**63) <= after < 2**63:
                raise ValueError("増減値または調整後の好感度が大きすぎます。")
            note = json.dumps({"actor_id": actor_id, "guild_id": guild_id, "reason": reason},
                              ensure_ascii=False)
            self.db.execute("UPDATE affinity SET points=?,updated_at=? WHERE user_id=?",
                            (after, now.isoformat(), user_id))
            self.db.execute(
                "INSERT INTO affinity_log (user_id,delta,reason,ref_id,note,day,created_at) "
                "VALUES (?,?,'admin',?,?,?,?)",
                (user_id, amount, message_id, note, self._today(now), now.isoformat()),
            )
        # 好感度は全サーバー共通なので全ランキングの結果を無効化する。
        self._rank_cache.clear()
        return before, after

    def _logged(self, user_id: int, reason: str, ref_id: int, day: str | None = None) -> bool:
        sql = "SELECT 1 FROM affinity_log WHERE user_id=? AND reason=? AND ref_id=?"
        args = [user_id, reason, ref_id]
        if day:
            sql += " AND day=?"
            args.append(day)
        return self.db.execute(sql, args).fetchone() is not None

    def _backfill(self):
        """すでにタスクを持っている人を「タスク機能を使った人」として登録する。"""
        try:
            self.db.execute(
                "INSERT OR IGNORE INTO affinity (user_id) SELECT DISTINCT owner_id FROM tasks WHERE status IN ('active','done','expired')"
            )
            self.db.execute(
                "UPDATE affinity SET uses_tasks = 1 WHERE user_id IN (SELECT owner_id FROM tasks WHERE status IN ('active','done','expired'))"
            )
            self.db.commit()
        except sqlite3.Error:
            LOGGER.exception("好感度の初期登録に失敗しました")

    # ---------- tasks.py から呼ばれる ----------
    def on_task_created(self, user_id: int, now: datetime):
        """タスク登録：タスク機能を使った印をつけ、「タスクなし」の数え直しをリセット。"""
        self._ensure(user_id)
        self.db.execute(
            "UPDATE affinity SET uses_tasks=1, idle_days=0, idle_penalty=0, idle_checked=? WHERE user_id=?",
            (self._today(now), user_id),
        )
        self.db.commit()

    def on_answer(self, user_id: int, task, new_percent: int, now: datetime) -> int:
        """進捗の回答。同じタスクは1日1回まで。"""
        day = self._today(now)
        if self._logged(user_id, "answer", task["id"], day):
            return 0
        old = task["percent"]
        delta = ANSWER_UP if new_percent > old else ANSWER_SAME
        return self._add(user_id, delta, "answer", now, ref_id=task["id"],
                         note=f"{old}→{new_percent}%", capped=True)

    def on_complete(self, owner_id: int, task, now: datetime) -> int:
        """完了・依頼ボーナス。ポイントと処理済み印を同時に保存する。"""
        if self._logged(owner_id,"complete",task["id"]):return 0
        day=self._today(now)
        with self.db:
            cur=self.db.execute("INSERT OR IGNORE INTO affinity_events VALUES (?,?,?)",
                                (owner_id,"complete",task["id"]))
            if cur.rowcount==0:return 0
            used=self.db.execute("SELECT COALESCE(SUM(delta),0) FROM affinity_log WHERE user_id=? AND day=? AND reason IN ('complete','request')",
                                 (owner_id,day)).fetchone()[0]
            base=complete_points(task["size_choice"],task["due"],now.astimezone(JST).date())
            base=min(base,max(0,COMPLETE_DAILY_LIMIT-used))
            self._ensure(owner_id)
            total=0
            for reason,delta in (("complete",base),("request",REQUEST_BONUS if task["creator_id"]!=owner_id else 0)):
                if reason=="request":
                    request_used=self.db.execute("SELECT COALESCE(SUM(delta),0) FROM affinity_log WHERE user_id=? AND day=? AND reason='request'",
                                                 (owner_id,day)).fetchone()[0]
                    delta=min(delta,max(0,REQUEST_DAILY_LIMIT-request_used),max(0,COMPLETE_DAILY_LIMIT-used-total))
                if delta<=0:continue
                self.db.execute("UPDATE affinity SET points=points+?,updated_at=? WHERE user_id=?",(delta,now.isoformat(),owner_id))
                self.db.execute("INSERT INTO affinity_log(user_id,delta,reason,ref_id,note,day,created_at) VALUES (?,?,?,?,?,?,?)",
                    (owner_id,delta,reason,task["id"],f"大きさ:{task['size_choice']}",day,now.isoformat()))
                total+=delta
        if total:
            self._rank_cache.clear()
        return total

    def on_expire(self, task, now: datetime) -> int:
        """期限切れの減点（1タスク1回）。"""
        owner = task["owner_id"]
        if self._logged(owner, "expire", task["id"]):
            return 0
        delta = expire_points(task["size_choice"], task["percent"])
        return self._add(owner, delta, "expire", now, ref_id=task["id"],
                         note=f"最終{task['percent']}%")

    # ---------- メンバー変更・チャット ----------
    @commands.Cog.listener()
    async def on_member_join(self, member):
        self._rank_cache.pop(member.guild.id, None)

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        self._rank_cache.pop(member.guild.id, None)

    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        if before.display_name != after.display_name or before.bot != after.bot:
            self._rank_cache.pop(after.guild.id, None)

    @commands.Cog.listener()
    async def on_message(self,message: discord.Message):
        if message.author.bot or message.guild is None:return
        # 管理コマンドそのものにはチャット加点しない（自分を調整する場合も指定値だけ増減）。
        context = await self.bot.get_context(message)
        if context.command is not None and context.command.name == "affinity":return
        uid=message.author.id;t=_time.monotonic()
        if t-self._chat_last.get(uid,-CHAT_COOLDOWN)<CHAT_COOLDOWN:return
        self._chat_last[uid]=t
        now=datetime.now(JST);day=self._today(now)
        chat_gained=self.db.execute("SELECT COALESCE(SUM(delta),0) FROM affinity_log WHERE user_id=? AND day=? AND reason='chat'",(uid,day)).fetchone()[0]
        if chat_gained>=CHAT_DAILY_LIMIT:return
        try:self._add(uid,CHAT_POINTS,"chat",now,capped=True)
        except sqlite3.Error:LOGGER.exception("チャットの好感度を記録できませんでした")

    # ---------- 「タスクなし」の減点 ----------
    def idle_check(self, now: datetime):
        today = now.astimezone(JST).date()
        rows = self.db.execute(
            "SELECT * FROM affinity WHERE uses_tasks=1 AND (idle_checked IS NULL OR idle_checked < ?)",
            (today.isoformat(),),
        ).fetchall()
        for r in rows:
            uid = r["user_id"]
            active = self.db.execute(
                "SELECT COUNT(*) FROM tasks WHERE owner_id=? AND status='active'", (uid,)
            ).fetchone()[0]
            if active:
                self.db.execute(
                    "UPDATE affinity SET idle_days=0, idle_penalty=0, idle_checked=? WHERE user_id=?",
                    (today.isoformat(), uid),
                )
                self.db.commit()
                continue
            n = 1 if r["idle_checked"] is None else min(
                IDLE_MAX_GAP, max(1, (today - date.fromisoformat(r["idle_checked"])).days)
            )
            idle_days, penalty = r["idle_days"], r["idle_penalty"]
            for i in range(n):
                d = today - timedelta(days=n - 1 - i)
                idle_days += 1
                if idle_days > IDLE_GRACE_DAYS and penalty < IDLE_PENALTY_MAX:
                    pen = min(IDLE_PENALTY, IDLE_PENALTY_MAX - penalty)
                    self._add(uid, -pen, "idle", now, day=d.isoformat(),
                              note=f"{idle_days}日目")
                    penalty += pen
            self.db.execute(
                "UPDATE affinity SET idle_days=?, idle_penalty=?, idle_checked=? WHERE user_id=?",
                (idle_days, penalty, today.isoformat(), uid),
            )
            self.db.commit()

    @tasks.loop(minutes=10)
    async def idle_loop(self):
        try:
            self.idle_check(datetime.now(JST))
        except Exception:
            LOGGER.exception("好感度の定期処理に失敗しました")

    @idle_loop.before_loop
    async def _before_idle(self):
        await self.bot.wait_until_ready()

    # ---------- 管理者専用のテキストコマンド（スラッシュ登録・公開ヘルプには出さない） ----------
    @commands.group(name="affinity", hidden=True, invoke_without_command=True)
    @commands.guild_only()
    @commands.has_guild_permissions(administrator=True)
    async def affinity_admin(self, ctx: commands.Context):
        await ctx.send("使用方法：`!affinity add @ユーザー 増減値 [理由]`\n"
                       "例：`!affinity add @ユーザー -10 誤加算の訂正`",
                       allowed_mentions=discord.AllowedMentions.none())

    @affinity_admin.command(name="add", hidden=True)
    @commands.guild_only()
    @commands.has_guild_permissions(administrator=True)
    async def affinity_admin_add(self, ctx: commands.Context, user: discord.Member,
                                 amount: int, *, reason: str = "理由未指定"):
        if user.bot:
            return await ctx.send("Botの好感度は調整できません。",
                                  allowed_mentions=discord.AllowedMentions.none())
        reason = reason.strip() or "理由未指定"
        try:
            result = self.adjust_by_admin(user.id, amount, ctx.author.id, ctx.guild.id,
                                          ctx.message.id, reason, datetime.now(JST))
        except ValueError as error:
            return await ctx.send(str(error), allowed_mentions=discord.AllowedMentions.none())
        if result is None:
            return await ctx.send("このメッセージの調整はすでに処理済みです。再加算しません。",
                                  allowed_mentions=discord.AllowedMentions.none())
        before, after = result
        name = discord.utils.escape_markdown(user.display_name)
        note = discord.utils.escape_markdown(reason)
        await ctx.send(f"🛠️ **{name}** の好感度を調整しました。\n"
                       f"{before:+d} → **{after:+d}** pt（{amount:+d}）\n理由：{note}",
                       allowed_mentions=discord.AllowedMentions.none())

    @affinity_admin.error
    @affinity_admin_add.error
    async def affinity_admin_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.NoPrivateMessage):
            text = "このコマンドはサーバー内でのみ使用できます。"
        elif isinstance(error, commands.MissingPermissions):
            text = "このコマンドを実行できるのは、サーバーの「管理者」権限を持つ人だけです。"
        elif isinstance(error, commands.MemberNotFound):
            text = "このサーバーのユーザーをメンションかユーザーIDで指定してください。"
        elif isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument)):
            text = "使用方法：`!affinity add @ユーザー 増減値 [理由]`\n増減値は `10` や `-10` の整数です。"
        else:
            original = getattr(error, "original", error)
            LOGGER.error("管理者による好感度調整でエラーが発生しました",
                         exc_info=(type(original), original, original.__traceback__))
            text = "調整処理でエラーが発生しました。通信エラー時は保存済みの場合があるため、好感度を確認してから再実行してください。"
        await ctx.send(text, allowed_mentions=discord.AllowedMentions.none())

    # ---------- 公開プロフィール ----------
    def _profile_text(self, member: discord.Member, *, include_history: bool, show_reason: bool = True) -> str:
        uid = member.id
        now = datetime.now(JST)
        row = self.db.execute("SELECT points FROM affinity WHERE user_id=?", (uid,)).fetchone()
        points = row["points"] if row else 0
        higher = self.db.execute("SELECT COUNT(*) FROM affinity WHERE points > ?", (points,)).fetchone()[0]
        total = self.db.execute("SELECT COUNT(*) FROM affinity").fetchone()[0] + (0 if row else 1)
        gained = self._gained_today(uid, self._today(now))
        name = discord.utils.escape_markdown(member.display_name)
        lines = [
            f"**{name} の好感度**：**{points:+d}** pt",
            f"**順位**：{higher + 1}位 / {total}人（全サーバーの記録済みユーザー）",
            f"**今日の増加**：{gained:+d} / +{DAILY_GAIN_LIMIT}（回答・チャット分）",
        ]
        # 他人を照会する /user でも履歴は表示するが、管理者調整の理由は本人にだけ見せる。
        if include_history:
            logs = self.db.execute(
                "SELECT delta, reason, note, created_at FROM affinity_log WHERE user_id=? ORDER BY id DESC LIMIT 5",
                (uid,),
            ).fetchall()
            if logs:
                lines.extend(["", "**直近の増減**"])
            for g in logs:
                at = datetime.fromisoformat(g["created_at"])
                lines.append(f"`{g['delta']:+d}` {LABELS.get(g['reason'], g['reason'])}　{at:%m/%d %H:%M}")
                if g["reason"] == "admin" and g["note"] and show_reason:
                    try:
                        note = json.loads(g["note"])
                        reason = str(note.get("reason", "理由未指定"))
                    except (ValueError, AttributeError, TypeError):
                        reason = str(g["note"])
                    short = reason[:100].replace("\n", " ").replace("\r", " ")
                    lines.append("　理由：" + discord.utils.escape_markdown(short) + ("…" if len(reason) > 100 else ""))
        return "\n".join(lines)

    @affinity.command(name="my", description="自分の好感度・順位・直近の増減を公開表示")
    async def my_affinity(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            self._profile_text(interaction.user, include_history=True), ephemeral=False,
            allowed_mentions=discord.AllowedMentions.none())

    @affinity.command(name="user", description="指定ユーザーの好感度を公開表示")
    @app_commands.describe(user="好感度を表示するユーザー")
    async def user_affinity(self, interaction: discord.Interaction, user: discord.Member):
        if user.bot or user.guild.id != interaction.guild_id:
            return await interaction.response.send_message(
                "このサーバーのBot以外のユーザーを指定してください。", ephemeral=True)
        await interaction.response.send_message(
            self._profile_text(user, include_history=True, show_reason=user.id == interaction.user.id), ephemeral=False,
            allowed_mentions=discord.AllowedMentions.none())

    # ---------- 全メンバーランキング（コマンドごとのメンバー取得APIなし） ----------
    def _ranking_error(self, guild: discord.Guild | None) -> str | None:
        if guild is None:
            return "サーバー内で使用してください。"
        if not self.bot.intents.members:
            return ("全員ランキングにはメンバー情報が必要です。管理者は環境変数 "
                    "`ENABLE_MEMBERS_INTENT=true` とDeveloper Portalの "
                    "`SERVER MEMBERS INTENT` を両方有効にしてBotを再起動してください。")
        if not guild.chunked:
            return ("全メンバー情報の取得がまだ完了していないため、不完全なランキングは表示しません。"
                    "起動直後なら少し待って再実行してください。続く場合は管理者がMembers Intent設定を確認してください。")
        return None

    def _ranking_rows(self, guild: discord.Guild) -> list[tuple[int, str, int]]:
        cached = self._rank_cache.get(guild.id)
        now = _time.monotonic()
        if cached and now - cached[0] < RANKING_CACHE_SEC:
            return cached[1]
        points = {r["user_id"]: r["points"] for r in self.db.execute("SELECT user_id,points FROM affinity")}
        rows = [
            (member.id, discord.utils.escape_markdown(member.display_name[:40]), points.get(member.id, 0))
            for member in guild.members if not member.bot
        ]
        rows.sort(key=lambda row: (-row[2], row[0]))
        self._rank_cache[guild.id] = (now, rows)
        return rows

    @staticmethod
    def _ranking_text(rows: list[tuple[int, str, int]], page: int) -> tuple[str, int, int]:
        pages = max(1, (len(rows) + RANKING_PAGE_SIZE - 1) // RANKING_PAGE_SIZE)
        page = min(max(0, page), pages - 1)
        lines = ["# 好感度ランキング", f"このサーバーの全{len(rows)}人（Botを除く）｜{page + 1}/{pages}ページ"]
        offset = page * RANKING_PAGE_SIZE
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        for rank, (_, name, points) in enumerate(rows[offset:offset + RANKING_PAGE_SIZE], offset + 1):
            label = medals.get(rank, f"`{rank}.`")
            lines.append(f"{label} **{name}**　{points:+d} pt")
        if not rows:
            lines.append("表示できるメンバーがいません。")
        lines.append("-# 未記録は0pt。全員分を10人ずつ表示します。ページ操作は実行者のみ・5分間。")
        return "\n".join(lines), page, pages

    @affinity.command(name="ranking", description="サーバー全員の好感度ランキングを公開表示")
    @app_commands.describe(page="ページ番号（省略すると1ページ目）")
    async def ranking(self, interaction: discord.Interaction, page: app_commands.Range[int, 1] = 1):
        error = self._ranking_error(interaction.guild)
        if error:
            return await interaction.response.send_message(error, ephemeral=True)
        # 全メンバーの並べ替え前に受付を返し、3秒の応答期限を超えないようにする。
        await interaction.response.defer(thinking=True, ephemeral=False)
        rows = self._ranking_rows(interaction.guild)
        pages = max(1, (len(rows) + RANKING_PAGE_SIZE - 1) // RANKING_PAGE_SIZE)
        if not 1 <= page <= pages:
            return await interaction.edit_original_response(content=f"ページは1〜{pages}を指定してください。")
        text, index, pages = self._ranking_text(rows, page - 1)
        view = RankingView(self, interaction, index, pages) if pages > 1 else None
        await interaction.edit_original_response(
            content=text, view=view, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: commands.Bot):
    await bot.add_cog(Affinity(bot))
