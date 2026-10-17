from __future__ import annotations

import logging
import random
import re
import sqlite3
import unicodedata
from statistics import median_low
from calendar import monthrange
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

LOGGER = logging.getLogger(__name__)
JST = ZoneInfo("Asia/Tokyo")
DB_PATH = Path(__file__).parent.parent / "bot.db"

PERCENTS = (0, 25, 50, 75, 100)
DEFAULT_INTERVAL_DAYS = 1
DEFAULT_ASK_TIME = "20:00"
# True: タスク作成者だけがボタンで回答できる / False: チャンネルの全員が回答できる
ONLY_OWNER_CAN_ANSWER = True

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id        INTEGER NOT NULL,
    channel_id      INTEGER NOT NULL,
    owner_id        INTEGER NOT NULL,
    name            TEXT NOT NULL,
    progress_name   TEXT NOT NULL,
    due             TEXT NOT NULL,
    interval_days   INTEGER NOT NULL,
    ask_time        TEXT NOT NULL,
    percent         INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'active',
    next_ask_at     TEXT NOT NULL,
    last_message_id INTEGER,
    created_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS progress_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     INTEGER NOT NULL,
    percent     INTEGER NOT NULL,
    answered_by INTEGER NOT NULL,
    answered_at TEXT NOT NULL
);
"""

# 小・中・大（ユーザー指定のアニメーション絵文字）
SIZE_EMOJIS = {
    1: discord.PartialEmoji(name="small", id=1559465303480733817, animated=True),
    2: discord.PartialEmoji(name="medium", id=1559465208529821696, animated=True),
    3: discord.PartialEmoji(name="big", id=1559465302151008347, animated=True),
}
SIZE_NAMES = {1: "small", 2: "medium", 3: "big"}
SIZE_LABELS = {"small": "小（0.5倍）", "medium": "中（1倍）", "big": "大（2倍）"}
SIZE_VOTE_HOURS = 24
UPDATABLE = {"percent", "status", "next_ask_at", "last_message_id", "registration_message_id"}


async def _acknowledge_task(interaction: discord.Interaction) -> bool:
    """検証・DB更新より先に初回応答する。期限切れなら変更せず中止。"""
    if interaction.response.is_done():
        return True
    try:
        await interaction.response.defer(ephemeral=True, thinking=True)
        return True
    except discord.InteractionResponded:
        LOGGER.warning("タスク操作への初回応答が競合しました。今回の処理は中止します。")
        return False
    except discord.HTTPException as error:
        if error.code not in (10062, 40060):
            raise
        age = (datetime.now(JST) - interaction.created_at).total_seconds()
        LOGGER.warning(
            "タスク操作の初回応答に失敗しました（code=%s、要求から%.2f秒）。"
            "今回の登録・進捗更新は行いません。Botの二重起動、回線遅延、"
            "イベントループの停止を確認し、新しく操作し直してください。",
            error.code, age,
        )
        return False


async def _task_reply(interaction: discord.Interaction, text: str):
    """初回応答済みならfollowupを使い、二重に初回応答しない。"""
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


# ---------- 日付まわり ----------
WEEKDAYS = {"月": 0, "火": 1, "水": 2, "木": 3, "金": 4, "土": 5, "日": 6}
DUE_HELP = "例: `明日` `来週` `来週金曜` `3日後` `今週末` `月末` `10/31` `2026-10-31`"


def _add_months(d: date, n: int) -> date:
    y, m = divmod(d.year * 12 + d.month - 1 + n, 12)
    m += 1
    return date(y, m, min(d.day, monthrange(y, m)[1]))


def _month_offset(word: str | None) -> int:
    return {"今月": 0, "来月": 1, "再来月": 2}.get(word or "今月", 0)


def _week_offset(word: str) -> int:
    return {"今週": 0, "来週": 1, "再来週": 2}[word]


def parse_due(text: str, today: date) -> date:
    """日本語の自然な表現と日付形式を締切日に変換する。

    今日 / 明日 / 明後日 / N日後 / N週間後 / Nか月後
    今週末 / 今週 / 来週 / 再来週 / 金曜 / 来週金曜 / 今週金曜
    月末 / 来月 / 来月末 / 来月5日 / 5日
    10/31 / 10月31日 / 2026-10-31 / 2026年10月31日
    """
    s = unicodedata.normalize("NFKC", text).lower()
    s = re.sub(r"\s+", "", s).replace("の", "")
    s = re.sub(r"(まで|に)$", "", s)
    wd = today.weekday()
    monday = today - timedelta(days=wd)

    try:
        words = {"今日": 0, "本日": 0, "きょう": 0, "明日": 1, "あした": 1, "あす": 1,
                 "明後日": 2, "あさって": 2, "明々後日": 3, "しあさって": 3}
        if s in words:
            return today + timedelta(days=words[s])

        if m := re.fullmatch(r"(\d+)日後", s):
            return today + timedelta(days=int(m[1]))
        if m := re.fullmatch(r"(\d+)週(?:間)?後", s):
            return today + timedelta(weeks=int(m[1]))
        if m := re.fullmatch(r"(\d+)[かカヶケヵ]月後", s):
            return _add_months(today, int(m[1]))

        if s in ("今週末", "週末", "今週"):
            return today + timedelta(days=6 - wd)
        if s in ("来週", "再来週"):
            return today + timedelta(weeks=_week_offset(s))
        if m := re.fullmatch(r"(今週|来週|再来週)?([月火水木金土日])(?:曜日|曜)?", s):
            idx = WEEKDAYS[m[2]]
            if m[1]:
                return monday + timedelta(weeks=_week_offset(m[1]), days=idx)
            return today + timedelta(days=(idx - wd) % 7 or 7)

        if m := re.fullmatch(r"(今月|来月|再来月)?月?末日?", s):
            first = _add_months(today.replace(day=1), _month_offset(m[1]))
            return first.replace(day=monthrange(first.year, first.month)[1])
        if s in ("来月", "再来月"):
            return _add_months(today, _month_offset(s))
        if m := re.fullmatch(r"(今月|来月|再来月)(\d{1,2})日?", s):
            first = _add_months(today.replace(day=1), _month_offset(m[1]))
            return first.replace(day=int(m[2]))

        if m := re.fullmatch(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})日?", s):
            return date(int(m[1]), int(m[2]), int(m[3]))
        if m := re.fullmatch(r"(\d{1,2})[-/月](\d{1,2})日?", s):
            d = date(today.year, int(m[1]), int(m[2]))
            return d if d >= today else date(today.year + 1, int(m[1]), int(m[2]))
        if m := re.fullmatch(r"(\d{1,2})日", s):
            day = int(m[1])
            this = today.replace(day=1)
            d = this.replace(day=day)  # 範囲外は ValueError
            return d if d >= today else _add_months(this, 1).replace(day=day)
    except ValueError:
        pass
    raise ValueError(f"締切「{text}」を読み取れませんでした。{DUE_HELP}")


def parse_ask_time(text: str) -> time:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", text.strip())
    if not m or int(m[1]) > 23 or int(m[2]) > 59:
        raise ValueError("時刻は `20:00` の形式で入力してください。")
    return time(int(m[1]), int(m[2]))


def at(d: date, t: time) -> datetime:
    return datetime.combine(d, t, tzinfo=JST)


def due_end(due: str) -> datetime:
    return at(date.fromisoformat(due), time(23, 59, 59))


def days_left_text(due: str, now: datetime) -> str:
    d = date.fromisoformat(due)
    n = (d - now.date()).days
    return f"{d.month}/{d.day}（今日まで）" if n == 0 else f"{d.month}/{d.day}（あと{n}日）"


def interval_text(days: int) -> str:
    return {1: "毎日", 7: "毎週"}.get(days, f"{days}日ごと")


def progress_bar(percent: int) -> str:
    filled = min(4, percent // 25)
    return "▰" * filled + "▱" * (4 - filled)


def format_task(row, now: datetime, location: str | None = None) -> str:
    """一覧の1件分。location を渡すと、場所（サーバー/チャンネル）の行を足す。"""
    esc = discord.utils.escape_markdown
    d = date.fromisoformat(row["due"])
    head = f"`#{row['id']}` **{esc(row['name'])}**"
    if row["progress_name"] != row["name"]:
        head += f" ／ {esc(row['progress_name'])}"
    bar = f"{progress_bar(row['percent'])} {row['percent']}%"
    if row["status"] == "pending":
        detail = f"{bar}｜📨 依頼への承認待ち"
    elif row["status"] == "done":
        detail = f"{bar}｜✅ 完了"
    elif row["status"] == "expired":
        detail = f"{bar}｜⏰ 期限切れ（締切 {d.month}/{d.day}）"
    else:
        days = (d - now.date()).days
        warn = " ⚠️" if days <= 1 and row["percent"] < 75 else ""
        detail = (
            f"{bar}｜締切 {days_left_text(row['due'], now)}{warn}"
            f"｜{interval_text(row['interval_days'])} {row['ask_time']}"
        )
    lines = [head, f"　{detail}"]
    if "size_choice" in row.keys():
        lines.append(f"　大きさ：{SIZE_LABELS[row['size_choice']]}" + ("（確定）" if row["size_locked"] else "（投票中）"))
    if location:
        lines.append(f"　📍 {location}")
    return "\n".join(lines)


def join_limited(blocks: list[str], limit: int = 1900) -> str:
    """Discordの文字数上限に収まるよう、入りきらない分は件数で省略する。"""
    out, used = [], 0
    for i, b in enumerate(blocks):
        if used + len(b) + 1 > limit:
            out.append(f"…ほか {len(blocks) - i} 件")
            break
        out.append(b)
        used += len(b) + 1
    return "\n".join(out)


def apply_progress(store, task, percent: int, user_id: int, who: str, now: datetime,
                   comment: str = "") -> tuple[str, int]:
    """進捗・一言・質問の終了を1トランザクションで記録する。"""
    name = discord.utils.escape_markdown(task["name"])
    who = discord.utils.escape_markdown(who)
    if percent >= 100:
        text = f"🎉 完了！ **{name}** が100%になりました。（{who}・{now:%m/%d %H:%M}）"
    else:
        text = f"✅ **{name}**：{task['percent']}% → {percent}%（{who}・{now:%m/%d %H:%M}）"
    text += f"\n大きさ：{SIZE_LABELS[task['size_choice']]}"
    history_id = store.record_progress(task["id"], percent, user_id, now, comment, text)
    return store.render_history(history_id), history_id


def award_progress(bot, task, percent: int, user_id: int, now: datetime):
    """好感度ポイントを付与する（Affinity cog が無い場合は何もしない）。apply_progress の後に呼ぶ。"""
    affinity = bot.get_cog("Affinity")
    if affinity is None:
        return
    try:
        affinity.on_answer(user_id, task, percent, now)
        if percent >= 100:
            affinity.on_complete(task["owner_id"], task, now)
    except Exception:
        LOGGER.exception("好感度の付与に失敗しました: #%s", task["id"])


# ---------- DB ----------
class TaskStore:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(path, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        # 既存DBを消さずに必要な列だけ追加する。
        migrations = {
            "tasks": {
                "creator_id": "INTEGER", "registration_message_id": "INTEGER",
                "size_choice": "TEXT NOT NULL DEFAULT 'medium'",
                "size_locked": "INTEGER NOT NULL DEFAULT 0",
            },
            "progress_history": {
                "comment": "TEXT NOT NULL DEFAULT ''", "action": "TEXT NOT NULL DEFAULT 'update'",
                "summary": "TEXT NOT NULL DEFAULT ''", "message_id": "INTEGER",
            },
        }
        for table, columns in migrations.items():
            existing = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for name, definition in columns.items():
                if name not in existing:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
        # 登録者の記録がない旧タスクには、依頼ボーナスを付けない。
        self.conn.execute("UPDATE tasks SET creator_id=owner_id WHERE creator_id IS NULL")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS task_size_votes (
            task_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
            value INTEGER NOT NULL CHECK(value BETWEEN 1 AND 3), updated_at TEXT NOT NULL,
            PRIMARY KEY(task_id,user_id)
        )""")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_registration ON tasks(registration_message_id)")
        self.conn.commit()

    def add(self, **v) -> int:
        v.setdefault("progress_name", v["name"])
        v.setdefault("creator_id", v["owner_id"])
        v.setdefault("status", "active")
        cur = self.conn.execute(
            """INSERT INTO tasks (guild_id, channel_id, owner_id, name, progress_name,
                due, interval_days, ask_time, next_ask_at, created_at, creator_id, status)
               VALUES (:guild_id, :channel_id, :owner_id, :name, :progress_name,
                :due, :interval_days, :ask_time, :next_ask_at, :created_at, :creator_id, :status)""",
            v,
        )
        self.conn.commit()
        return cur.lastrowid

    def get(self, task_id: int):
        return self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()

    def list_active(self, *, channel_id: int | None = None, owner_id: int | None = None,
                    guild_id: int | None = None):
        sql, args = "SELECT * FROM tasks WHERE status = 'active'", []
        for col, val in (("channel_id", channel_id), ("owner_id", owner_id), ("guild_id", guild_id)):
            if val is not None:
                sql += f" AND {col} = ?"
                args.append(val)
        return self.conn.execute(sql + " ORDER BY id", args).fetchall()

    def query(self, *, owner_id: int | None = None, guild_id: int | None = None,
              channel_id: int | None = None, include_finished: bool = False):
        """一覧表示用。締切が近い順（進行中が先、完了・期限切れは後）。"""
        statuses = ("active", "pending", "done", "expired") if include_finished else ("active", "pending")
        sql = f"SELECT * FROM tasks WHERE status IN ({','.join('?' * len(statuses))})"
        args: list = list(statuses)
        for col, val in (("owner_id", owner_id), ("guild_id", guild_id), ("channel_id", channel_id)):
            if val is not None:
                sql += f" AND {col} = ?"
                args.append(val)
        return self.conn.execute(sql + " ORDER BY (status != 'active'), due, id", args).fetchall()

    def update(self, task_id: int, **fields):
        assert set(fields) <= UPDATABLE
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE tasks SET {sets} WHERE id = ?", (*fields.values(), task_id))
        self.conn.commit()

    def delete(self, task_id: int):
        self.conn.execute("DELETE FROM task_size_votes WHERE task_id = ?", (task_id,))
        self.conn.execute("DELETE FROM progress_history WHERE task_id = ?", (task_id,))
        self.conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        self.conn.commit()

    def freeze_size(self, task_id: int):
        row = self.get(task_id)
        if row and not row["size_locked"]:
            values = [r[0] for r in self.conn.execute(
                "SELECT value FROM task_size_votes WHERE task_id=?", (task_id,))]
            # 偶数票の中間は小さい側。無投票は中。
            choice = SIZE_NAMES[median_low(values)] if values else "medium"
            self.conn.execute("UPDATE tasks SET size_choice=?,size_locked=1 WHERE id=?", (choice,task_id))
            self.conn.commit()
        return self.get(task_id)

    def vote(self, message_id: int, user_id: int, value: int, now: datetime, *, remove=False):
        row = self.conn.execute("SELECT * FROM tasks WHERE registration_message_id=?", (message_id,)).fetchone()
        if row is None or row["status"] not in ("active", "pending") or row["size_locked"]:
            return False
        if now > datetime.fromisoformat(row["created_at"]) + timedelta(hours=SIZE_VOTE_HOURS):
            self.freeze_size(row["id"])
            return False
        with self.conn:
            if remove:
                self.conn.execute("DELETE FROM task_size_votes WHERE task_id=? AND user_id=? AND value=?",
                                  (row["id"],user_id,value))
            else:
                self.conn.execute("""INSERT INTO task_size_votes VALUES (?,?,?,?)
                    ON CONFLICT(task_id,user_id) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""",
                                  (row["id"],user_id,value,now.isoformat()))
            values = [r[0] for r in self.conn.execute("SELECT value FROM task_size_votes WHERE task_id=?",(row["id"],))]
            choice = SIZE_NAMES[median_low(values)] if values else "medium"
            self.conn.execute("UPDATE tasks SET size_choice=? WHERE id=?",(choice,row["id"]))
        return True

    def clear_votes(self, message_id: int, value: int | None = None):
        row = self.conn.execute("SELECT * FROM tasks WHERE registration_message_id=?",(message_id,)).fetchone()
        if not row or row["size_locked"]:
            return
        now = datetime.now(JST)
        if now > datetime.fromisoformat(row["created_at"]) + timedelta(hours=SIZE_VOTE_HOURS):
            self.freeze_size(row["id"])
            return
        with self.conn:
            sql = "DELETE FROM task_size_votes WHERE task_id=?"
            args = [row["id"]]
            if value is not None:
                sql += " AND value=?"; args.append(value)
            self.conn.execute(sql,args)
            values = [r[0] for r in self.conn.execute("SELECT value FROM task_size_votes WHERE task_id=?",(row["id"],))]
            self.conn.execute("UPDATE tasks SET size_choice=? WHERE id=?",
                              (SIZE_NAMES[median_low(values)] if values else "medium",row["id"]))

    def record_progress(self, task_id: int, percent: int, user_id: int, now: datetime,
                        comment: str, summary: str) -> int:
        with self.conn:
            cur = self.conn.execute("""INSERT INTO progress_history
                (task_id,percent,answered_by,answered_at,comment,summary)
                VALUES (?,?,?,?,?,?)""", (task_id,percent,user_id,now.isoformat(),comment[:200],summary))
            self.conn.execute("UPDATE tasks SET percent=?,status=?,last_message_id=NULL WHERE id=?",
                              (percent,"done" if percent==100 else "active",task_id))
        return cur.lastrowid

    def skip(self, task, user_id: int, now: datetime):
        with self.conn:
            self.conn.execute("""INSERT INTO progress_history
                (task_id,percent,answered_by,answered_at,action) VALUES (?,?,?,?,'skip')""",
                (task["id"],task["percent"],user_id,now.isoformat()))
            self.conn.execute("UPDATE tasks SET last_message_id=NULL WHERE id=?",(task["id"],))

    def history(self, history_id: int):
        return self.conn.execute("""SELECT h.*,t.owner_id,t.guild_id,t.channel_id
            FROM progress_history h JOIN tasks t ON t.id=h.task_id WHERE h.id=?""",(history_id,)).fetchone()

    def render_history(self, history_id: int):
        row = self.history(history_id)
        text = row["summary"]
        if row["comment"]:
            text += "\n💬 " + discord.utils.escape_markdown(row["comment"])
        return text

    def set_history_message(self, history_id: int, message_id: int):
        with self.conn:
            self.conn.execute("UPDATE progress_history SET message_id=? WHERE id=?",(message_id,history_id))


# ---------- %ボタン（Bot再起動後も動く永続ボタン）----------
class ProgressButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"progress:(?P<task_id>\d+):(?P<percent>\d+)",
):
    def __init__(self, task_id: int, percent: int):
        super().__init__(
            discord.ui.Button(
                label=f"{percent}%",
                style=discord.ButtonStyle.success if percent == 100 else discord.ButtonStyle.secondary,
                custom_id=f"progress:{task_id}:{percent}",
            )
        )
        self.task_id = task_id
        self.percent = percent

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(int(match["task_id"]), int(match["percent"]))

    async def callback(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("Tasks")
        row, error = cog.validate_progress(interaction, self.task_id, interaction.message.id)
        if error:
            return await interaction.response.send_message(error, ephemeral=True)
        task = cog.store.freeze_size(self.task_id)
        text, history_id = apply_progress(cog.store,task,self.percent,interaction.user.id,
                                         interaction.user.display_name,datetime.now(JST))
        award_progress(interaction.client,task,self.percent,interaction.user.id,datetime.now(JST))
        cog.store.set_history_message(history_id,interaction.message.id)
        await interaction.response.edit_message(content=text,view=comment_view(history_id),
                                                allowed_mentions=discord.AllowedMentions.none())


def progress_view(task_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for p in PERCENTS:
        view.add_item(ProgressButton(task_id, p))
    view.add_item(TaskControlButton(task_id,"form"))
    view.add_item(TaskControlButton(task_id,"skip"))
    return view


def _to_int(text: str) -> int:
    """全角数字・「#12」「50%」のような入力も整数にする。"""
    t = unicodedata.normalize("NFKC", text).strip().lstrip("#").rstrip("%").strip()
    return int(t)


class ProgressModal(discord.ui.Modal, title="進捗を更新"):
    def __init__(self, cog: "Tasks", task_id: int | None = None, progress: int | None = None, expected_message_id: int | None = None, comment: str = ""):
        super().__init__()
        self.cog = cog
        self.expected_message_id = expected_message_id
        self.task_id = discord.ui.TextInput(
            label="タスク番号", placeholder="例: 12（一覧の # の番号）",
            default=None if task_id is None else str(task_id), max_length=10,
        )
        self.progress = discord.ui.TextInput(
            label="進捗（0〜100 の%）", placeholder="例: 50",
            default=None if progress is None else str(progress), max_length=5,
        )
        self.add_item(self.task_id)
        self.add_item(self.progress)
        self.comment = discord.ui.TextInput(label="一言（任意・このタスクのチャンネルに公開）",
            style=discord.TextStyle.paragraph,required=False,max_length=200,default=comment or None)
        self.add_item(self.comment)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            task_id = _to_int(self.task_id.value)
            progress = _to_int(self.progress.value)
        except ValueError:
            return await interaction.response.send_message(
                "⚠️ タスク番号と進捗は数字で入力してください。", ephemeral=True
            )
        if not 0 <= progress <= 100:
            return await interaction.response.send_message(
                "⚠️ 進捗は 0〜100 の数字で入力してください。", ephemeral=True
            )
        await self.cog.set_progress(interaction, task_id, progress, self.comment.value, self.expected_message_id)


class ProgressInputView(discord.ui.View):
    """/task progress で番号か進捗が空のときに付く「入力して更新」ボタン。"""

    def __init__(self, cog: "Tasks", user_id: int, task_id: int | None, progress: int | None, comment: str = ""):
        super().__init__(timeout=900)
        self.cog, self.user_id = cog, user_id
        self.task_id, self.progress, self.comment = task_id, progress, comment

    @discord.ui.button(label="番号と進捗を入力して更新", emoji="✏️", style=discord.ButtonStyle.primary)
    async def open_modal(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("このボタンはコマンドを実行した人だけが使えます。", ephemeral=True)
        await interaction.response.send_modal(ProgressModal(self.cog, self.task_id, self.progress, comment=self.comment))


# ---------- コマンド不要の常設パネル・追加ボタン ----------
class TaskControlButton(discord.ui.DynamicItem[discord.ui.Button],
                        template=r"taskctl:(?P<kind>form|skip):(?P<task_id>\d+)"):
    def __init__(self, task_id: int, kind: str):
        super().__init__(discord.ui.Button(
            label="1%単位・一言を入力" if kind=="form" else "今回はスキップ",
            emoji="✏️" if kind=="form" else "⏭️",row=1,
            style=discord.ButtonStyle.primary if kind=="form" else discord.ButtonStyle.secondary,
            custom_id=f"taskctl:{kind}:{task_id}"))
        self.task_id,self.kind=task_id,kind

    @classmethod
    async def from_custom_id(cls,interaction,item,match):
        return cls(int(match["task_id"]),match["kind"])

    async def callback(self,interaction):
        cog=interaction.client.get_cog("Tasks")
        row,error=cog.validate_progress(interaction,self.task_id,interaction.message.id)
        if error:
            return await interaction.response.send_message(error,ephemeral=True)
        if self.kind=="form":
            await interaction.response.send_modal(ProgressModal(cog,self.task_id,row["percent"],interaction.message.id))
        else:
            cog.store.skip(row,interaction.user.id,datetime.now(JST))
            await interaction.response.edit_message(
                content=f"⏭️ タスク #{self.task_id} の今回の回答をスキップしました。進捗・好感度は変わりません。次回は予定どおり確認します。",
                view=None,allowed_mentions=discord.AllowedMentions.none())


class CommentButton(discord.ui.DynamicItem[discord.ui.Button],template=r"tasknote:(?P<history_id>\d+)"):
    def __init__(self,history_id: int):
        super().__init__(discord.ui.Button(label="一言を追加・訂正（30分以内）",emoji="💬",
            custom_id=f"tasknote:{history_id}",style=discord.ButtonStyle.secondary))
        self.history_id=history_id

    @classmethod
    async def from_custom_id(cls,interaction,item,match):
        return cls(int(match["history_id"]))

    async def callback(self,interaction):
        cog=interaction.client.get_cog("Tasks")
        row=cog.store.history(self.history_id)
        if not row or row["guild_id"]!=interaction.guild_id or row["answered_by"]!=interaction.user.id:
            return await interaction.response.send_message("この回答をした本人だけが編集できます。",ephemeral=True)
        if datetime.now(JST)-datetime.fromisoformat(row["answered_at"])>timedelta(minutes=30):
            return await interaction.response.send_message("一言の編集期限（30分）を過ぎました。",ephemeral=True)
        await interaction.response.send_modal(NoteModal(cog,self.history_id,row["comment"]))


def comment_view(history_id: int):
    view=discord.ui.View(timeout=None);view.add_item(CommentButton(history_id));return view


class NoteModal(discord.ui.Modal,title="進捗への一言"):
    def __init__(self,cog,history_id,comment):
        super().__init__();self.cog,self.history_id=cog,history_id
        self.note=discord.ui.TextInput(label="一言（タスクのチャンネルに公開）",required=False,
            style=discord.TextStyle.paragraph,max_length=200,default=comment or None)
        self.add_item(self.note)

    async def on_submit(self,interaction):
        row=self.cog.store.history(self.history_id)
        if not row or row["guild_id"]!=interaction.guild_id or row["answered_by"]!=interaction.user.id:
            return await interaction.response.send_message("この一言は編集できません。",ephemeral=True)
        if datetime.now(JST)-datetime.fromisoformat(row["answered_at"])>timedelta(minutes=30):
            return await interaction.response.send_message("編集期限を過ぎました。",ephemeral=True)
        channel=self.cog.bot.get_channel(row["channel_id"])
        if channel is None or not channel.permissions_for(interaction.user).view_channel:
            return await interaction.response.send_message("タスクのチャンネルを閲覧できません。",ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        with self.cog.store.conn:
            self.cog.store.conn.execute("UPDATE progress_history SET comment=? WHERE id=?",
                                        (self.note.value.strip(),self.history_id))
        try:
            channel=await self.cog._get_channel(row)
            if row["message_id"]:
                await channel.get_partial_message(row["message_id"]).edit(
                    content=self.cog.store.render_history(self.history_id),
                    allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            return await interaction.followup.send("一言は保存しましたが、元のメッセージを更新できませんでした。",ephemeral=True)
        await interaction.followup.send("一言を保存しました。好感度は再加算しません。",ephemeral=True)


class TaskCreateModal(discord.ui.Modal,title="タスクを追加"):
    def __init__(self,cog,owners=None):
        super().__init__();self.cog,self.owners=cog,owners
        self.name_field=discord.ui.TextInput(label="タスク名",max_length=100)
        self.due_field=discord.ui.TextInput(label="締切",placeholder="明日・来週金曜・10/31 など",max_length=50)
        self.detail=discord.ui.TextInput(label="進捗名・完了条件（任意）",required=False,max_length=200)
        self.interval=discord.ui.TextInput(label="確認の間隔（日数・省略で毎日）",default="1",required=False,max_length=2)
        self.ask_time=discord.ui.TextInput(label="確認時刻（省略で20:00）",default=DEFAULT_ASK_TIME,required=False,max_length=5)
        for item in (self.name_field,self.due_field,self.detail,self.interval,self.ask_time):self.add_item(item)

    async def on_submit(self,interaction):
        try:days=_to_int(self.interval.value or "1")
        except ValueError:
            return await interaction.response.send_message("間隔は1〜30の日数で入力してください。",ephemeral=True)
        if not 1<=days<=30:
            return await interaction.response.send_message("間隔は1〜30の日数で入力してください。",ephemeral=True)
        await self.cog.create_tasks(interaction,self.name_field.value,self.due_field.value,
            self.owners or [interaction.user],days,self.ask_time.value or DEFAULT_ASK_TIME,self.detail.value)


class OwnerSelect(discord.ui.UserSelect):
    def __init__(self):
        super().__init__(placeholder="依頼する担当者を選択（最大5人）",min_values=1,max_values=5)

    async def callback(self,interaction):
        self.view.owners=list(self.values)
        await interaction.response.defer()


class RequestCreateView(discord.ui.View):
    def __init__(self,cog,user_id):
        super().__init__(timeout=900);self.cog,self.user_id,self.owners=cog,user_id,[]
        self.add_item(OwnerSelect())

    async def interaction_check(self,interaction):
        if interaction.user.id!=self.user_id:
            await interaction.response.send_message("この画面を開いた本人だけが使えます。",ephemeral=True);return False
        return True

    @discord.ui.button(label="選んだ担当者のタスクを入力",style=discord.ButtonStyle.primary,row=1)
    async def open(self,interaction,button):
        if not self.owners:
            return await interaction.response.send_message("先に担当者を選んでください。",ephemeral=True)
        await interaction.response.send_modal(TaskCreateModal(self.cog,self.owners))


class TaskPanelView(discord.ui.View):
    def __init__(self):super().__init__(timeout=None)

    @discord.ui.button(label="自分のタスクを追加",emoji="➕",style=discord.ButtonStyle.success,custom_id="tasks:panel:add")
    async def add(self,interaction,button):
        await interaction.response.send_modal(TaskCreateModal(interaction.client.get_cog("Tasks")))

    @discord.ui.button(label="他の人へタスクを依頼",emoji="📨",style=discord.ButtonStyle.secondary,custom_id="tasks:panel:request")
    async def request(self,interaction,button):
        await interaction.response.send_message("担当者を選んでから入力してください。相手が承認するまでは、この依頼による減点はありません。",
            ephemeral=True,view=RequestCreateView(interaction.client.get_cog("Tasks"),interaction.user.id))

    @discord.ui.button(label="自分の進捗を更新",emoji="✏️",style=discord.ButtonStyle.primary,custom_id="tasks:panel:progress")
    async def progress(self,interaction,button):
        cog=interaction.client.get_cog("Tasks")
        await cog._show_own_tasks(interaction,"下のボタンから番号・1%単位の進捗・一言を入力できます。",
                                  ProgressInputView(cog,interaction.user.id,None,None))


class RequestAction(discord.ui.DynamicItem[discord.ui.Button],template=r"taskrequest:(?P<kind>accept|decline):(?P<task_id>\d+)"):
    def __init__(self,task_id,kind):
        super().__init__(discord.ui.Button(label="依頼を引き受ける" if kind=="accept" else "辞退する",
            style=discord.ButtonStyle.success if kind=="accept" else discord.ButtonStyle.secondary,
            custom_id=f"taskrequest:{kind}:{task_id}"));self.task_id,self.kind=task_id,kind

    @classmethod
    async def from_custom_id(cls,interaction,item,match):return cls(int(match["task_id"]),match["kind"])

    async def callback(self,interaction):
        cog=interaction.client.get_cog("Tasks");row=cog.store.get(self.task_id)
        if not row or row["guild_id"]!=interaction.guild_id or row["owner_id"]!=interaction.user.id or row["status"]!="pending":
            return await interaction.response.send_message("担当者本人だけが、承認待ちの依頼に回答できます。",ephemeral=True)
        now=datetime.now(JST)
        if self.kind=="accept" and now<=due_end(row["due"]):
            first=at(now.date(),parse_ask_time(row["ask_time"]))
            if first<=now:first+=timedelta(days=1)
            cog.store.update(self.task_id,status="active",next_ask_at=first.isoformat())
            affinity=cog.bot.get_cog("Affinity")
            if affinity:affinity.on_task_created(row["owner_id"],now)
            text=f"✅ 依頼を引き受けました。タスク #{self.task_id}\n"+format_task(cog.store.get(self.task_id),now)
        else:
            cog.store.update(self.task_id,status="cancelled")
            text=f"タスク #{self.task_id} の依頼を終了しました。好感度の減点はありません。"
        await interaction.response.edit_message(content=text,view=None,allowed_mentions=discord.AllowedMentions.none())


def request_view(task_id):
    view=discord.ui.View(timeout=None)
    view.add_item(RequestAction(task_id,"accept"));view.add_item(RequestAction(task_id,"decline"));return view


# ---------- Cog ----------
class Tasks(commands.Cog):
    task = app_commands.Group(name="task", description="進捗確認タスク", guild_only=True)

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.store = TaskStore(DB_PATH)
        self.last_check_at: datetime | None = None

    async def cog_load(self):
        self.bot.add_dynamic_items(ProgressButton,TaskControlButton,CommentButton,RequestAction)
        self.bot.add_view(TaskPanelView())
        self.check_tasks.start()

    async def cog_unload(self):
        self.check_tasks.cancel()
        self.store.conn.close()

    # ---- コマンド ----
    @task.command(name="add", description="タスクを登録し、定期的な進捗確認を始める")
    @app_commands.describe(
        task_name="タスク名",
        due="締切（例: 明日 / 来週 / 来週金曜 / 3日後 / 月末 / 10/31）",
        user="担当者（省略すると自分。他の人だけでもOK）",
        user2="担当者を追加（任意）",
        user3="担当者を追加（任意）",
        user4="担当者を追加（任意）",
        user5="担当者を追加（任意）",
        interval="確認の間隔（既定: 毎日）",
        ask_time="確認する時刻（例: 20:00 / 既定 20:00）",
    )
    @app_commands.choices(
        interval=[
            app_commands.Choice(name="毎日", value=1),
            app_commands.Choice(name="2日ごと", value=2),
            app_commands.Choice(name="毎週", value=7),
        ]
    )
    async def add(
        self,
        interaction: discord.Interaction,
        task_name: app_commands.Range[str, 1, 100],
        due: str,
        user: Optional[discord.Member] = None,
        user2: Optional[discord.Member] = None,
        user3: Optional[discord.Member] = None,
        user4: Optional[discord.Member] = None,
        user5: Optional[discord.Member] = None,
        interval: Optional[app_commands.Choice[int]] = None,
        ask_time: Optional[str] = None,
    ):
        if not await _acknowledge_task(interaction):
            return
        owners=[]
        for member in (user,user2,user3,user4,user5):
            if member is not None and all(member.id!=m.id for m in owners):owners.append(member)
        await self.create_tasks(interaction,task_name,due,owners or [interaction.user],
                                interval.value if interval else DEFAULT_INTERVAL_DAYS,ask_time or DEFAULT_ASK_TIME)

    async def create_tasks(self,interaction,task_name,due,owners,days,ask_time,progress_name=""):
        # コマンド・入力フォームのどちらから来ても、最初に応答を確保する。
        if not await _acknowledge_task(interaction):
            return
        task_name=task_name.strip();progress_name=progress_name.strip() or task_name
        now=datetime.now(JST)
        try:
            due_date=parse_due(due,now.date());t=parse_ask_time(ask_time)
        except ValueError as error:
            return await _task_reply(interaction,f"⚠️ {error}")
        if not task_name or len(task_name)>100 or not 1<=days<=30 or due_date<now.date():
            return await _task_reply(interaction,"⚠️ タスク名・確認間隔・締切を確認してください。")
        channel=interaction.channel;guild=interaction.guild
        if not guild or not channel or not hasattr(channel,"permissions_for"):
            return await _task_reply(interaction,"サーバーのチャンネルで使ってください。")
        perms=channel.permissions_for(guild.me)
        if not perms.view_channel or not perms.send_messages or not perms.add_reactions or not perms.read_message_history:
            return await _task_reply(interaction,"Botに閲覧・送信・リアクション追加・履歴閲覧の権限を付けてください。")
        if any(not isinstance(m,discord.Member) or m.guild.id!=guild.id or m.bot or not channel.permissions_for(m).view_channel for m in owners):
            return await _task_reply(interaction,"担当者は、このチャンネルを見られる同じサーバーの人を指定してください。Botは指定できません。")
        first=at(now.date(),t)
        if first<=now:first+=timedelta(days=1)
        ids=[];failed_emojis=False
        for owner in owners:
            requested=owner.id!=interaction.user.id
            task_id=self.store.add(guild_id=guild.id,channel_id=channel.id,owner_id=owner.id,
                creator_id=interaction.user.id,name=task_name,progress_name=progress_name[:200],
                due=due_date.isoformat(),interval_days=days,ask_time=t.strftime("%H:%M"),
                next_ask_at=first.isoformat(),created_at=now.isoformat(),status="pending" if requested else "active")
            ids.append(task_id)
            affinity=self.bot.get_cog("Affinity")
            if affinity and not requested:affinity.on_task_created(owner.id,now)
            text=(f"📋 **{discord.utils.escape_markdown(task_name)}**（#{task_id}）\n"
                  f"担当：{owner.mention}　登録者：{interaction.user.mention}\n"
                  f"締切：{days_left_text(due_date.isoformat(),now)}\n"
                  f"進捗名・完了条件：{discord.utils.escape_markdown(progress_name[:200])}\n"
                  f"進捗確認：{interval_text(days)} {t:%H:%M}\n"
                  "**大きさ投票（1人1票）**\n")
            if requested:text+="\n承認待ち：担当者が引き受けるまでは、この依頼による好感度の増減・定期質問はありません。"
            try:
                msg=await channel.send(text,view=request_view(task_id) if requested else None,
                    allowed_mentions=discord.AllowedMentions(users=[owner] if requested else []))
                self.store.update(task_id,registration_message_id=msg.id)
                for emoji in SIZE_EMOJIS.values():
                    try:await msg.add_reaction(emoji)
                    except discord.HTTPException:
                        failed_emojis=True;LOGGER.warning("大きさ絵文字を追加できません: #%s",task_id)
            except discord.HTTPException:
                self.store.update(task_id,status="cancelled")
                await interaction.followup.send(f"タスク #{task_id} の投稿に失敗したため、登録を取り消しました。",ephemeral=True)
        message="登録処理をしました："+"、".join(f"#{i}" for i in ids)
        if failed_emojis:message+="\n一部の絵文字を追加できませんでした。絵文字の利用権限を確認してください。投票がなければ中として扱います。"
        await interaction.followup.send(message,ephemeral=True)

    @task.command(name="panel",description="コマンド不要のタスク操作パネルを設置（管理者用）")
    @app_commands.default_permissions(manage_messages=True)
    async def panel(self,interaction: discord.Interaction):
        if not interaction.user.guild_permissions.manage_messages:
            return await interaction.response.send_message("パネルの設置にはメッセージ管理権限が必要です。",ephemeral=True)
        await interaction.response.send_message(
            "📋 **タスク操作パネル**\n下のボタンから登録・依頼・進捗更新ができます。\n"
            "進捗への一言は、対象タスクのチャンネルに公開されます。\nこのメッセージをピン留めすると便利です。",
            view=TaskPanelView(),allowed_mentions=discord.AllowedMentions.none())

    @commands.Cog.listener()
    async def on_raw_reaction_add(self,payload):
        value=next((v for v,e in SIZE_EMOJIS.items() if e.id==payload.emoji.id),None)
        if value is None or payload.guild_id is None or payload.member is None or payload.member.bot:return
        self.store.vote(payload.message_id,payload.user_id,value,datetime.now(JST))

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self,payload):
        value=next((v for v,e in SIZE_EMOJIS.items() if e.id==payload.emoji.id),None)
        if value is not None:
            self.store.vote(payload.message_id,payload.user_id,value,datetime.now(JST),remove=True)

    @commands.Cog.listener()
    async def on_raw_reaction_clear(self,payload):self.store.clear_votes(payload.message_id)

    @commands.Cog.listener()
    async def on_raw_reaction_clear_emoji(self,payload):
        value=next((v for v,e in SIZE_EMOJIS.items() if e.id==payload.emoji.id),None)
        if value is not None:self.store.clear_votes(payload.message_id,value)

    @add.autocomplete("due")
    async def due_autocomplete(self, interaction: discord.Interaction, current: str):
        today = datetime.now(JST).date()
        candidates = ["今日", "明日", "明後日", "3日後", "今週末", "来週", "来週金曜", "1週間後", "月末", "来月"]
        if current.strip():
            candidates = [current.strip()] + [c for c in candidates if current in c]
        choices = []
        for c in candidates:
            try:
                d = parse_due(c, today)
            except ValueError:
                continue
            label = f"{c}（{d.month}/{d.day} {'月火水木金土日'[d.weekday()]}）"
            choices.append(app_commands.Choice(name=label[:100], value=c[:100]))
        return choices[:25]

    def _location(self, row, *, with_guild: bool) -> str:
        """タスクがあるチャンネルの表示名。"""
        guild = self.bot.get_guild(row["guild_id"])
        channel = self.bot.get_channel(row["channel_id"])
        ch = f"#{channel.name}" if channel else "（不明なチャンネル）"
        if with_guild:
            return f"{guild.name if guild else '（不明なサーバー）'} / {ch}"
        return f"<#{row['channel_id']}>"

    @task.command(name="my", description="自分のタスクを表示（すべてのサーバー・チャンネル）")
    @app_commands.describe(include_finished="完了・期限切れのタスクも表示する")
    async def my_tasks(self, interaction: discord.Interaction, include_finished: bool = False):
        rows = self.store.query(owner_id=interaction.user.id, include_finished=include_finished)
        if not rows:
            return await interaction.response.send_message(
                "進行中のタスクはありません。`/task add` で登録しましょう。", ephemeral=True
            )
        now = datetime.now(JST)
        blocks = [format_task(r, now, self._location(r, with_guild=True)) for r in rows]
        await interaction.response.send_message(
            f"📋 **自分のタスク**（{len(rows)}件）\n" + join_limited(blocks),
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
        )

    @task.command(name="user", description="指定した人のタスクを表示（このサーバー内）")
    @app_commands.describe(user="タスクを見たい人")
    async def user_tasks(self, interaction: discord.Interaction, user: discord.Member):
        rows = self.store.query(owner_id=user.id, guild_id=interaction.guild_id)
        if not rows:
            return await interaction.response.send_message(
                f"{user.display_name} さんの進行中タスクは、このサーバーにありません。", ephemeral=True
            )
        now = datetime.now(JST)
        blocks = [format_task(r, now, self._location(r, with_guild=False)) for r in rows]
        await interaction.response.send_message(
            f"📋 **{discord.utils.escape_markdown(user.display_name)} さんのタスク**（{len(rows)}件）\n"
            + join_limited(blocks),
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
        )

    @task.command(name="channel", description="このチャンネルの全員のタスクを表示")
    async def channel_tasks(self, interaction: discord.Interaction):
        rows = self.store.query(channel_id=interaction.channel_id)
        if not rows:
            return await interaction.response.send_message(
                "このチャンネルに進行中のタスクはありません。", ephemeral=True
            )
        now = datetime.now(JST)
        by_owner: dict[int, list] = {}
        for r in rows:
            by_owner.setdefault(r["owner_id"], []).append(r)
        blocks = []
        for owner_id, items in by_owner.items():
            blocks.append(f"**<@{owner_id}>**")
            blocks.extend(format_task(r, now) for r in items)
        await interaction.response.send_message(
            f"📋 **このチャンネルのタスク**（{len(rows)}件）\n" + join_limited(blocks),
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _show_own_tasks(self, interaction: discord.Interaction, hint: str, view: discord.ui.View | None = None):
        """このサーバーにある自分のタスクの一覧（番号つき）を、自分にだけ表示する。"""
        rows = self.store.query(owner_id=interaction.user.id, guild_id=interaction.guild_id)
        if not rows:
            return await interaction.response.send_message(
                "このサーバーに進行中のタスクはありません。`/task add` で登録しましょう。", ephemeral=True
            )
        now = datetime.now(JST)
        blocks = [format_task(r, now, self._location(r, with_guild=False)) for r in rows]
        await interaction.response.send_message(
            f"📋 **このサーバーの自分のタスク**（{len(rows)}件）\n{hint}\n" + join_limited(blocks, 1700),
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
            view=view if view is not None else discord.utils.MISSING,
        )

    async def _own_task_autocomplete(self, interaction: discord.Interaction, current: str):
        rows = self.store.query(owner_id=interaction.user.id, guild_id=interaction.guild_id)
        choices = []
        for r in rows:
            label = f"#{r['id']} {r['name']}（{r['percent']}%）"
            if current and current not in label:
                continue
            choices.append(app_commands.Choice(name=label[:100], value=r["id"]))
        return choices[:25]

    @task.command(name="delete", description="タスクを削除し、確認を止める（番号を省略すると自分のタスク一覧）")
    @app_commands.describe(task_id="タスク番号（省略すると、このサーバーの自分のタスク一覧を表示）")
    async def delete(self, interaction: discord.Interaction, task_id: Optional[int] = None):
        if task_id is None:
            return await self._show_own_tasks(
                interaction, "削除するには `/task delete task_id:番号` を実行してください。"
            )
        row = self.store.get(task_id)
        if row is None or row["guild_id"] != interaction.guild_id:
            return await interaction.response.send_message("⚠️ そのタスクは見つかりません。", ephemeral=True)
        is_admin = interaction.user.guild_permissions.manage_guild
        if interaction.user.id != row["owner_id"] and not is_admin:
            return await interaction.response.send_message(
                "⚠️ 削除できるのは担当者かサーバー管理者だけです。", ephemeral=True
            )
        await self._disable_last_message(row)
        self.store.delete(task_id)
        await interaction.response.send_message(
            f"🗑️ タスク #{task_id}「{discord.utils.escape_markdown(row['name'])}」を削除しました。"
        )

    @task.command(name="progress", description="タスクの進捗を更新する（省略すると自分のタスク一覧）")
    @app_commands.describe(
        task_id="タスク番号（省略すると、このサーバーの自分のタスク一覧を表示）",
        progress="進捗（0〜100 の%）",
        comment="一言（任意・タスクのチャンネルに公開、200文字まで）",
    )
    async def update_progress(
        self,
        interaction: discord.Interaction,
        task_id: Optional[int] = None,
        progress: Optional[app_commands.Range[int, 0, 100]] = None,
        comment: Optional[app_commands.Range[str, 0, 200]] = None,
    ):
        if task_id is None or progress is None:
            return await self._show_own_tasks(
                interaction,
                "更新するには `/task progress task_id:番号 progress:0〜100` を実行するか、下のボタンから入力してください。",
                view=ProgressInputView(self, interaction.user.id, task_id, progress, comment or ""),
            )
        await self.set_progress(interaction, task_id, progress, comment or "")

    def validate_progress(self,interaction,task_id,expected_message_id=None):
        row=self.store.get(task_id)
        if row is None or row["guild_id"]!=interaction.guild_id:return None,"⚠️ そのタスクは見つかりません。"
        if row["status"]=="pending":return None,"依頼を先に引き受けてください。登録メッセージの承認ボタンから回答できます。"
        if row["status"]!="active":return None,"このタスクの確認は終了しています。"
        if ONLY_OWNER_CAN_ANSWER and interaction.user.id!=row["owner_id"]:return None,"担当者だけが回答できます。"
        if datetime.now(JST)>due_end(row["due"]):return None,"締切を過ぎています。"
        if expected_message_id is not None and row["last_message_id"]!=expected_message_id:return None,"この質問は回答済み、または古くなっています。"
        channel=self.bot.get_channel(row["channel_id"])
        if channel is None or not channel.permissions_for(interaction.user).view_channel:
            return None,"タスクのチャンネルを閲覧できません。"
        return row,None

    async def set_progress(self,interaction: discord.Interaction,task_id: int,progress: int,
                           comment: str="",expected_message_id: int | None=None):
        # 応答失敗後にデータだけ更新されることを避ける。
        if not await _acknowledge_task(interaction):
            return
        row,error=self.validate_progress(interaction,task_id,expected_message_id)
        if error:return await _task_reply(interaction,error)
        if not 0<=progress<=100 or len(comment)>200:
            return await _task_reply(interaction,"進捗は0〜100、一言は200文字以内です。")
        # HTTP待ちの前に記録する。二重送信はactive/last_message_idの検証で防ぐ。
        task=self.store.freeze_size(task_id)
        now=datetime.now(JST)
        text,history_id=apply_progress(self.store,task,progress,interaction.user.id,
                                       interaction.user.display_name,now,comment.strip())
        award_progress(self.bot,task,progress,interaction.user.id,now)
        await self._disable_last_message(row)
        try:
            channel=await self._get_channel(row)
            msg=await channel.send(text,view=comment_view(history_id),allowed_mentions=discord.AllowedMentions.none())
            self.store.set_history_message(history_id,msg.id)
        except discord.HTTPException:
            return await interaction.followup.send("進捗・一言は保存しましたが、チャンネルへ投稿できませんでした。",ephemeral=True)
        await interaction.followup.send("進捗を更新し、タスクのチャンネルに投稿しました。",ephemeral=True)

    delete.autocomplete("task_id")(_own_task_autocomplete)
    update_progress.autocomplete("task_id")(_own_task_autocomplete)

    @app_commands.command(name="pick-task", description="自分の進行中タスクからランダムに1つ選ぶ")
    @app_commands.guild_only()
    async def pick_task(self, interaction: discord.Interaction):
        rows = self.store.list_active(owner_id=interaction.user.id, guild_id=interaction.guild_id)
        if not rows:
            return await interaction.response.send_message(
                "進行中のタスクがありません。`/task add` で登録しましょう。", ephemeral=True
            )
        r = random.choice(rows)
        await interaction.response.send_message(
            f"ランダムなタスク： **{discord.utils.escape_markdown(r['name'])}**（現在 {r['percent']}%）"
        )

    # ---- 定期処理 ----
    @tasks.loop(minutes=1)
    async def check_tasks(self):
        now = datetime.now(JST)
        # 投票締切を処理。Bot停止中に行われたリアクション変更はイベントを受け取れないため、
        # 起動後に外して押し直してもらう（履歴全件のAPI再取得はしない）。
        for row in self.store.conn.execute("SELECT * FROM tasks WHERE status IN ('active','pending') AND size_locked=0").fetchall():
            if now>=datetime.fromisoformat(row["created_at"])+timedelta(hours=SIZE_VOTE_HOURS):self.store.freeze_size(row["id"])
        with self.store.conn:
            self.store.conn.execute("UPDATE tasks SET status='cancelled' WHERE status='pending' AND due < ?",(now.date().isoformat(),))
        for row in self.store.list_active():
            try:
                if now > due_end(row["due"]):
                    await self._expire(row)
                elif datetime.fromisoformat(row["next_ask_at"]) <= now:
                    await self._ask(row, now)
            except Exception:
                LOGGER.exception("タスク #%s の処理に失敗", row["id"])
        self.last_check_at=datetime.now(JST)

    @check_tasks.before_loop
    async def _before_check(self):
        await self.bot.wait_until_ready()

    async def _get_channel(self, row):
        return self.bot.get_channel(row["channel_id"]) or await self.bot.fetch_channel(row["channel_id"])

    async def _disable_last_message(self, row):
        if not row["last_message_id"]:
            return
        try:
            channel = await self._get_channel(row)
            await channel.get_partial_message(row["last_message_id"]).edit(view=None)
        except discord.HTTPException:
            pass

    async def _ask(self, row, now: datetime):
        try:
            channel = await self._get_channel(row)
        except discord.NotFound:
            self.store.update(row["id"], status="cancelled")
            return
        await self._disable_last_message(row)  # 古い質問のボタンを無効化

        task_name = discord.utils.escape_markdown(row["name"])
        text = (
            f"<@{row['owner_id']}>\n"
            f"# 進捗どうですか？\n"
            f"**{task_name}**\n"
            f"締切  ：{days_left_text(row['due'], now)}　前回：{row['percent']}%\n"
            f"今の進捗..."
        )
        try:
            msg = await channel.send(
                text,
                view=progress_view(row["id"]),
                allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=row["owner_id"])]),
            )
        except discord.Forbidden:
            LOGGER.warning("チャンネル %s に送信できません。10分後に再試行します。", row["channel_id"])
            self.store.update(row["id"], next_ask_at=(now + timedelta(minutes=10)).isoformat())
            return

        next_at = at(now.date() + timedelta(days=row["interval_days"]), parse_ask_time(row["ask_time"]))
        self.store.update(row["id"], last_message_id=msg.id, next_ask_at=next_at.isoformat())

    async def _expire(self, row):
        row=self.store.freeze_size(row["id"])
        self.store.update(row["id"], status="expired")
        affinity = self.bot.get_cog("Affinity")
        if affinity is not None:
            try:
                affinity.on_expire(row, datetime.now(JST))
            except Exception:
                LOGGER.exception("好感度の減点に失敗しました: #%s", row["id"])
        await self._disable_last_message(row)
        try:
            channel = await self._get_channel(row)
            await channel.send(
                f"# DEADLINE\n"
                f"<@{row['owner_id']}> {discord.utils.escape_markdown(row['name'])} は"
                f"締切（{row['due']}）を過ぎました！進捗の確認を終了します　最終進捗：{row['percent']}%",
                allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=row["owner_id"])]),
            )
        except discord.HTTPException:
            LOGGER.warning("期限切れ通知を送れませんでした: #%s", row["id"])


async def setup(bot: commands.Bot):
    await bot.add_cog(Tasks(bot))