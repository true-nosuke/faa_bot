"""School-wide special-day calendar; Discord settings/subscriptions are per guild."""

from __future__ import annotations

import csv
import io
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
DB_PATH = Path(__file__).resolve().parent.parent / "bot.db"
SLOTS = tuple(
    [f"前日{h}:00" for h in range(18, 25)] + [f"当日{h}:00" for h in range(6, 9)]
)
GRACE = timedelta(minutes=10)
MAX_CSV_BYTES = 1024 * 1024
MAX_ROWS = 1000
SCHEMA = """
CREATE TABLE IF NOT EXISTS special_day_events (
 id INTEGER PRIMARY KEY, target_date TEXT NOT NULL, period TEXT NOT NULL
 CHECK(period IN ('午前','午後')), note TEXT NOT NULL DEFAULT '',
 active INTEGER NOT NULL DEFAULT 1, source TEXT NOT NULL, created_by INTEGER NOT NULL,
 updated_at TEXT NOT NULL, UNIQUE(target_date,period)
);
CREATE TABLE IF NOT EXISTS special_day_settings (
 guild_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL,
 mention_role_id INTEGER, enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS special_day_personal_reminders (
 guild_id INTEGER NOT NULL, target_date TEXT NOT NULL, user_id INTEGER NOT NULL,
 slot INTEGER NOT NULL CHECK(slot BETWEEN 0 AND 9), enabled INTEGER NOT NULL,
 PRIMARY KEY(guild_id,target_date,user_id,slot)
);
CREATE TABLE IF NOT EXISTS special_day_notifications (
 guild_id INTEGER NOT NULL, target_date TEXT NOT NULL,
 user_id INTEGER NOT NULL, slot INTEGER NOT NULL,
 status TEXT NOT NULL, channel_id INTEGER, message_id INTEGER,
 snapshot TEXT, error TEXT, updated_at TEXT NOT NULL,
 PRIMARY KEY(guild_id,target_date,user_id,slot)
);
CREATE INDEX IF NOT EXISTS special_day_active_date ON special_day_events(active,target_date);
"""


def now_jst():
    return datetime.now(JST)


def parse_date(value: str) -> date:
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%Y年%m月%d日"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            pass
    raise ValueError("日付は2026-10-31、2026/10/31などで入力してください。")


def scheduled(day: str, slot: int) -> datetime:
    midnight = datetime.combine(date.fromisoformat(day), time(), JST)
    if slot == -1:
        return midnight - timedelta(hours=8)  # previous day 16:00
    if not 0 <= slot < 10:
        raise ValueError("通知時刻が不正です。")
    return midnight + (
        timedelta(hours=slot - 6) if slot < 7 else timedelta(hours=slot - 1)
    )


def parse_csv(data: bytes, today: date):
    if len(data) > MAX_CSV_BYTES:
        raise ValueError("CSVは1MB以内にしてください。")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = data.decode("cp932")
        except UnicodeDecodeError as exc:
            raise ValueError("CSVはUTF-8またはCP932で保存してください。") from exc
    reader = csv.DictReader(io.StringIO(text), strict=True)
    if not reader.fieldnames or not {"Subject", "Start Date"} <= set(reader.fieldnames):
        raise ValueError("Subject と Start Date の列が必要です。")
    events, errors = {}, []
    try:
        for index, row in enumerate(reader, 1):
            if index > MAX_ROWS:
                raise ValueError("CSVは1000行以内にしてください。")
            line = reader.line_num
            subject = (row.get("Subject") or "").strip()
            period = {"A特": "午前", "P特": "午後"}.get(subject)
            try:
                if not period:
                    raise ValueError("SubjectはA特/P特のみ対象")
                day = parse_date(row.get("Start Date") or "")
                if day < today:
                    raise ValueError("過去の日付")
                key = (day.isoformat(), period)
                if key in events:
                    raise ValueError("CSV内の日付・区分が重複")
                events[key] = (
                    ""  # Description is not imported (no arbitrary mentions/notes).
                )
            except ValueError as exc:
                errors.append(f"{line}行目: {exc}")
    except csv.Error as exc:
        raise ValueError(f"CSV形式が不正です（{reader.line_num}行目）。") from exc
    return [(d, p, n) for (d, p), n in sorted(events.items())], errors


class Store:
    def __init__(self, path=DB_PATH):
        self.path = Path(path)
        with self.connect() as con:
            con.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        con = sqlite3.connect(self.path, timeout=5)
        con.row_factory = sqlite3.Row
        try:
            with con:
                yield con
        finally:
            con.close()

    def rows(self, sql, args=()):
        with self.connect() as con:
            return [dict(r) for r in con.execute(sql, args)]

    def events(self, day):
        return self.rows(
            "SELECT * FROM special_day_events WHERE target_date=? AND active=1 ORDER BY period ASC",
            (day,),
        )

    def future(self, today):
        return self.rows(
            "SELECT * FROM special_day_events WHERE target_date>=? AND active=1 ORDER BY target_date,period ASC",
            (today.isoformat(),),
        )

    def upsert(self, records, user_id, source, now):
        # Validation and import are atomic. Revalidate after the confirmation delay.
        with self.connect() as con:
            for day, period, note in records:
                if (
                    date.fromisoformat(day) < now.date()
                    or period not in ("午前", "午後")
                    or len(note) > 100
                ):
                    raise ValueError("過去の日付・区分・メモを確認してください。")
                con.execute(
                    """INSERT INTO special_day_events
                 (target_date,period,note,source,created_by,updated_at) VALUES(?,?,?,?,?,?)
                 ON CONFLICT(target_date,period) DO UPDATE SET note=excluded.note,
                 active=1,source=excluded.source,updated_at=excluded.updated_at""",
                    (day, period, note, source, user_id, now.isoformat()),
                )

    def cancel(self, event_id, now):
        with self.connect() as con:
            event = con.execute(
                "SELECT * FROM special_day_events WHERE id=? AND active=1", (event_id,)
            ).fetchone()
            if not event:
                raise ValueError("有効な予定番号がありません。")
            con.execute(
                "UPDATE special_day_events SET active=0,updated_at=? WHERE id=?",
                (now.isoformat(), event_id),
            )
            day = event["target_date"]
            if not con.execute(
                "SELECT 1 FROM special_day_events WHERE target_date=? AND active=1",
                (day,),
            ).fetchone():
                con.execute(
                    "UPDATE special_day_personal_reminders SET enabled=0 WHERE target_date=?",
                    (day,),
                )
            return day

    def settings(self, guild_id=None):
        return self.rows(
            "SELECT * FROM special_day_settings"
            + (" WHERE guild_id=?" if guild_id else ""),
            (guild_id,) if guild_id else (),
        )

    def configure(self, guild_id, channel_id, role_id, enabled):
        with self.connect() as con:
            con.execute(
                "INSERT OR REPLACE INTO special_day_settings VALUES(?,?,?,?)",
                (guild_id, channel_id, role_id, int(enabled)),
            )

    def selections(self, guild_id, day, user_id):
        return {
            r["slot"]
            for r in self.rows(
                "SELECT slot FROM special_day_personal_reminders WHERE guild_id=? AND target_date=? AND user_id=? AND enabled=1",
                (guild_id, day, user_id),
            )
        }

    def toggle(self, guild_id, day, user_id, slot, now):
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            if not con.execute(
                "SELECT 1 FROM special_day_events WHERE target_date=? AND active=1",
                (day,),
            ).fetchone():
                raise ValueError("対象の予定は取り消されています。")
            current = con.execute(
                "SELECT enabled FROM special_day_personal_reminders WHERE guild_id=? AND target_date=? AND user_id=? AND slot=?",
                (guild_id, day, user_id, slot),
            ).fetchone()
            enabled = not (current and current[0])
            if enabled and scheduled(day, slot) <= now:
                raise ValueError("過ぎた時刻は有効化できません。")
            if (
                enabled
                and con.execute(
                    "SELECT 1 FROM special_day_notifications WHERE guild_id=? AND target_date=? AND user_id=? AND slot=?",
                    (guild_id, day, user_id, slot),
                ).fetchone()
            ):
                raise ValueError("処理済みの通知は再設定できません。")
            con.execute(
                "INSERT OR REPLACE INTO special_day_personal_reminders VALUES(?,?,?,?,?)",
                (guild_id, day, user_id, slot, int(enabled)),
            )

    def claim(self, guild_id, day, user_id, slot, now):
        # Claim before network I/O: bounded at-most-once attempts; never auto-resend after a crash.
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            if not con.execute(
                "SELECT 1 FROM special_day_events WHERE target_date=? AND active=1",
                (day,),
            ).fetchone():
                return False
            if (
                user_id
                and not con.execute(
                    "SELECT 1 FROM special_day_personal_reminders WHERE guild_id=? AND target_date=? AND user_id=? AND slot=? AND enabled=1",
                    (guild_id, day, user_id, slot),
                ).fetchone()
            ):
                return False
            cur = con.execute(
                "INSERT OR IGNORE INTO special_day_notifications(guild_id,target_date,user_id,slot,status,updated_at) VALUES(?,?,?,?,'sending',?)",
                (guild_id, day, user_id, slot, now.isoformat()),
            )
            return cur.rowcount == 1

    def finish(
        self,
        guild_id,
        day,
        user_id,
        slot,
        status,
        now,
        channel_id=None,
        message_id=None,
        snapshot=None,
        error=None,
    ):
        with self.connect() as con:
            con.execute(
                """UPDATE special_day_notifications SET status=?,updated_at=?,channel_id=?,message_id=?,snapshot=?,error=?
             WHERE guild_id=? AND target_date=? AND user_id=? AND slot=?""",
                (
                    status,
                    now.isoformat(),
                    channel_id,
                    message_id,
                    snapshot,
                    error,
                    guild_id,
                    day,
                    user_id,
                    slot,
                ),
            )

    def subscriptions(self, guild_id, today):
        return self.rows(
            "SELECT * FROM special_day_personal_reminders WHERE guild_id=? AND target_date>=? AND enabled=1",
            (guild_id, today.isoformat()),
        )

    def public_messages(self):
        return self.rows(
            "SELECT * FROM special_day_notifications WHERE user_id=0 AND status='sent' AND message_id IS NOT NULL"
        )
