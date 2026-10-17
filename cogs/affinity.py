from __future__ import annotations

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
RANKING_SIZE = 10
RANKING_CACHE_SEC = 300       # ランキング結果は5分使い回す
MEMBER_CACHE_SEC = 1800       # 直近の参加確認を30分覚える

LABELS = {
    "answer": "進捗の回答",
    "complete": "タスク完了",
    "request": "依頼タスクボーナス",
    "expire": "期限切れ",
    "chat": "チャット",
    "idle": "タスクなし",
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


class Affinity(commands.Cog):
    affinity = app_commands.Group(name="affinity", description="Botからの好感度", guild_only=True)

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = sqlite3.connect(DB_PATH, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()
        self._chat_last: dict[int, float] = {}
        self._rank_cache: dict[int, tuple[float, str]] = {}
        # ランキングでは fetch_member / fetch_members / query_members を一切呼ばない。
        self._known_members: dict[tuple[int, int], tuple[float, str]] = {}

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
        return delta

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
        return total

    def on_expire(self, task, now: datetime) -> int:
        """期限切れの減点（1タスク1回）。"""
        owner = task["owner_id"]
        if self._logged(owner, "expire", task["id"]):
            return 0
        delta = expire_points(task["size_choice"], task["percent"])
        return self._add(owner, delta, "expire", now, ref_id=task["id"],
                         note=f"最終{task['percent']}%")

    # ---------- チャット・ローカルメンバー情報 ----------
    def _remember_member(self,member):
        if member is None or member.bot or not hasattr(member,"guild"):return
        self._known_members[(member.guild.id,member.id)]=(_time.monotonic(),discord.utils.escape_markdown(member.display_name))

    @commands.Cog.listener()
    async def on_interaction(self,interaction):
        if interaction.guild:self._remember_member(interaction.user)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self,payload):
        self._remember_member(payload.member)

    @commands.Cog.listener()
    async def on_member_join(self,member):
        self._remember_member(member);self._rank_cache.pop(member.guild.id,None)

    @commands.Cog.listener()
    async def on_member_remove(self,member):
        self._known_members.pop((member.guild.id,member.id),None);self._rank_cache.pop(member.guild.id,None)

    @commands.Cog.listener()
    async def on_message(self,message: discord.Message):
        if message.author.bot or message.guild is None:return
        self._remember_member(message.author)
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

    # ---------- コマンド ----------
    @affinity.command(name="me", description="自分の好感度を見る（自分にだけ表示）")
    async def me(self, interaction: discord.Interaction):
        uid = interaction.user.id
        now = datetime.now(JST)
        row = self.db.execute("SELECT points FROM affinity WHERE user_id=?", (uid,)).fetchone()
        points = row["points"] if row else 0
        higher = self.db.execute("SELECT COUNT(*) FROM affinity WHERE points > ?", (points,)).fetchone()[0]
        total = self.db.execute("SELECT COUNT(*) FROM affinity").fetchone()[0] + (0 if row else 1)
        gained = self._gained_today(uid, self._today(now))
        logs = self.db.execute(
            "SELECT delta, reason, created_at FROM affinity_log WHERE user_id=? ORDER BY id DESC LIMIT 5",
            (uid,),
        ).fetchall()
        lines = [
            f"**好感度**：**{points:+d}** pt",
            f"**順位**：{higher + 1}位 / {total}人（全サーバー）",
            f"**今日の増加**：{gained:+d} / +{DAILY_GAIN_LIMIT}（回答・チャット分）",
        ]
        if logs:
            lines.append("")
            lines.append("**直近の増減**")
            for g in logs:
                at = datetime.fromisoformat(g["created_at"])
                lines.append(f"`{g['delta']:+d}` {LABELS.get(g['reason'], g['reason'])}　{at:%m/%d %H:%M}")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @affinity.command(name="ranking",description="キャッシュを使った好感度ランキング（メンバー照会APIなし）")
    async def ranking(self,interaction: discord.Interaction):
        guild=interaction.guild
        self._remember_member(interaction.user)
        cached=self._rank_cache.get(guild.id)
        if cached and _time.monotonic()-cached[0]<RANKING_CACHE_SEC:
            return await interaction.response.send_message(cached[1],allowed_mentions=discord.AllowedMentions.none())
        # 同期的なDB・ローカルキャッシュ参照だけなので、並行要求もAPI照会を増やさない。
        found=[];now=_time.monotonic()
        for row in self.db.execute("SELECT user_id,points FROM affinity ORDER BY points DESC,user_id"):
            member=guild.get_member(row["user_id"])
            if member is not None:
                if member.bot:continue
                name=discord.utils.escape_markdown(member.display_name)
            else:
                known=self._known_members.get((guild.id,row["user_id"]))
                if known is None or now-known[0]>=MEMBER_CACHE_SEC:continue
                name=known[1]
            found.append((name,row["points"]))
            if len(found)>=RANKING_SIZE:break
        lines=["# 好感度ランキング"]
        if not found:lines.append("現在のキャッシュに、点数を持つメンバーがいません。")
        medals={1:"🥇",2:"🥈",3:"🥉"}
        for i,(name,points) in enumerate(found,1):lines.append(f"{medals.get(i,f'`{i}.`')} **{name}**　{points:+d} pt")
        text="\n".join(lines)
        self._rank_cache[guild.id]=(now,text)
        await interaction.response.send_message(text,allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: commands.Bot):
    await bot.add_cog(Affinity(bot))
