"""前期特編授業の時間割を、自然文の質問に AI を使わず即答する。

データは Web 版（https://github.com/igarinpiano/pre-tokuhen 、Apache License 2.0）の
data/timetable.json を同梱したもの（cogs/tokuhen_data.json）。
メンション経路は ai_reply の on_message から try_handle が呼ばれる。
"""
from __future__ import annotations

import io
import json
import logging
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands

LOGGER = logging.getLogger(__name__)

JST = ZoneInfo("Asia/Tokyo")
DATA_FILE = Path(__file__).parent / "tokuhen_data.json"
STATE_FILE = Path(__file__).parent.parent / "tokuhen_state.json"

WEEKDAYS_JA = ["月", "火", "水", "木", "金", "土", "日"]  # date.weekday() の順
WEEKDAYS_EN = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
WEEKDAY_JA_INDEX = {"月": 0, "火": 1, "水": 2, "木": 3, "金": 4, "土": 5, "日": 6}
MONTHS_EN = {name: i + 1 for i, name in enumerate([
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december"])}
for _name, _num in list(MONTHS_EN.items()):
    MONTHS_EN[_name[:3]] = _num

ELECTIVE_GROUPS = ["soc-abn", "soc-bckl", "geo-pol", "sci-ab", "sci-bc"]

# ---------- データ ----------


def load_data(path=DATA_FILE) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        LOGGER.warning("tokuhen: 時間割データを読めませんでした: %s", path)
        return {}


def period_range(data: dict) -> tuple[date | None, date | None]:
    """days に存在する日付の範囲 = 特編期間。"""
    keys = sorted((data.get("days") or {}).keys())
    if not keys:
        return None, None
    return date.fromisoformat(keys[0]), date.fromisoformat(keys[-1])


def class_division_maps(data: dict) -> tuple[dict[str, str], dict[str, str]]:
    """columns から (組→クラス, クラス→組) の対応を作る。"""
    to_div: dict[str, str] = {}
    to_class: dict[str, str] = {}
    for col in data.get("columns") or []:
        to_div[col["class"]] = col["division"]
        to_class[col["division"]] = col["class"]
    return to_div, to_class


def column_of(data: dict, class_no: str | None = None,
              division: str | None = None) -> dict | None:
    for col in data.get("columns") or []:
        if class_no is not None and col["class"] == str(class_no):
            return col
        if division is not None and col["division"] == division:
            return col
    return None


# ---------- プロファイル ----------


def load_profiles(path=STATE_FILE) -> dict:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def save_profiles(path, profiles: dict) -> None:
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(profiles, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        LOGGER.warning("tokuhen: プロファイルを保存できませんでした", exc_info=True)


def profile_column(data: dict, profile: dict | None) -> dict | None:
    if not profile:
        return None
    return column_of(data, profile.get("class"), profile.get("division"))


# ---------- 意図判定 ----------

_OP_JA = ["追加", "削除", "消して", "完了", "リマインド"]
_DATE_HINT = re.compile(
    r"今日|明日|明後日|昨日|今週|来週|再来週|曜日|曜|[0-9０-９]+月|[0-9０-９]+/[0-9０-９]+"
    r"|today|tomorrow|yesterday|monday|tuesday|wednesday|thursday|friday|saturday|sunday",
    re.IGNORECASE)
_EN_TRIGGER = re.compile(
    r"timetable|what classes|(?:schedule|lessons?)\b.{0,40}?"
    r"(?:today|tomorrow|yesterday|monday|tuesday|wednesday|thursday|friday|saturday|sunday"
    r"|january|february|march|april|may|june|july|august|september|october|november|december"
    r"|class)",
    re.IGNORECASE | re.DOTALL)
_TABLE_HINT = re.compile(r"表で|表にして|表形式|typst", re.IGNORECASE)


def is_timetable_query(text: str) -> bool:
    """時間割の質問かどうか。操作系の文は False（ChatOps 等に譲る）。"""
    text = text or ""
    if any(w in text for w in _OP_JA):
        return False
    if re.search(r"\b(add|delete|remind)\b", text, re.IGNORECASE):
        return False
    if "時間割" in text or "特編" in text:
        return True
    if "何限" in text and _DATE_HINT.search(text):
        return True
    return bool(_EN_TRIGGER.search(text))


def wants_table(text: str) -> bool:
    return bool(_TABLE_HINT.search(text or ""))


def detect_lang(text: str) -> str:
    """英語で聞かれたら 'en'、それ以外は 'ja'。"""
    if re.search(r"[ぁ-んァ-ヶ一-龥]", text or ""):
        return "ja"
    return "en" if _EN_TRIGGER.search(text or "") else "ja"


# ---------- 日付解析 ----------


def _resolve_year(month: int, day: int, today: date,
                  start: date | None, end: date | None) -> date | None:
    """年の無い月日を、期間に入る年で解決する。"""
    for year in (today.year, today.year + 1):
        try:
            cand = date(year, month, day)
        except ValueError:
            return None
        if start is None or start <= cand <= end:
            return cand
    try:
        return date(today.year, month, day)
    except ValueError:
        return None


def parse_date(text: str, today: date,
               start: date | None = None, end: date | None = None) -> date:
    """本文から対象日を1つ決める。見つからなければ今日。"""
    text = text or ""
    m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", text)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    m = re.search(r"(\d{1,2})月(\d{1,2})日", text)
    if m:
        found = _resolve_year(int(m.group(1)), int(m.group(2)), today, start, end)
        if found:
            return found
    m = re.search(r"\b(\d{1,2})/(\d{1,2})\b", text)
    if m:
        found = _resolve_year(int(m.group(1)), int(m.group(2)), today, start, end)
        if found:
            return found
    low = text.lower()
    m = re.search(r"(january|february|march|april|may|june|july|august|september|october"
                  r"|november|december|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)"
                  r"\s+(\d{1,2})", low)
    if m:
        found = _resolve_year(MONTHS_EN[m.group(1)], int(m.group(2)), today, start, end)
        if found:
            return found
    m = re.search(r"(\d{1,2})\s+(january|february|march|april|may|june|july|august"
                  r"|september|october|november|december|jan|feb|mar|apr|jun|jul|aug"
                  r"|sep|oct|nov|dec)", low)
    if m:
        found = _resolve_year(MONTHS_EN[m.group(2)], int(m.group(1)), today, start, end)
        if found:
            return found
    # 相対（日）。長い語から先に判定する
    for words, delta in ((("一昨日", "おととい"), -2), (("昨日", "きのう", "yesterday"), -1),
                         (("明後日", "あさって"), 2), (("明日", "あした", "あす", "tomorrow"), 1),
                         (("今日", "きょう", "today"), 0)):
        if any(w in low for w in words):
            return today + timedelta(days=delta)
    # 週＋曜日
    wd = _weekday_in(text)
    if wd is not None:
        week_offset = 0
        if "再来週" in text:
            week_offset = 2
        elif "来週" in text:
            week_offset = 1
        monday = today - timedelta(days=today.weekday())
        if "今週" in text or week_offset:
            return monday + timedelta(weeks=week_offset, days=wd)
        return today + timedelta(days=(wd - today.weekday()) % 7)
    return today


def _weekday_in(text: str) -> int | None:
    m = re.search(r"([月火水木金土日])曜日?曜?日?", text or "")
    if m and ("曜" in m.group(0)):
        return WEEKDAY_JA_INDEX[m.group(1)]
    m = re.search(r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday"
                  r"|mon|tue|wed|thu|fri|sat|sun)\b", (text or "").lower())
    if m:
        name = m.group(1)
        for i, full in enumerate(WEEKDAYS_EN):
            if name == full.lower() or name == full[:3].lower():
                return i
    return None


def parse_column_ref(text: str, data: dict) -> dict | None:
    """本文中の組・クラスの指定を取り出す。"""
    m = re.search(r"([1-8])\s*組", text or "")
    if m:
        return column_of(data, class_no=m.group(1))
    m = re.search(r"class\s*([1-8])", text or "", re.IGNORECASE)
    if m:
        return column_of(data, class_no=m.group(1))
    _, to_class = class_division_maps(data)
    m = re.search(r"division\s*([a-o])", text or "", re.IGNORECASE)
    if m and m.group(1).lower() in to_class:
        return column_of(data, division=m.group(1).lower())
    m = re.search(r"([A-O])\s*(?:の|クラス)", text or "")
    if m and m.group(1).lower() in to_class:
        return column_of(data, division=m.group(1).lower())
    return None


# ---------- 表示 ----------


def subject_name(data: dict, subject_id: str, lang: str) -> str:
    subj = (data.get("subjects") or {}).get(subject_id) or {}
    return subj.get(lang) or subj.get("ja") or subject_id


def subject_short(data: dict, subject_id: str, lang: str) -> str:
    subj = (data.get("subjects") or {}).get(subject_id) or {}
    short = subj.get("short") or {}
    return short.get(lang) or short.get("ja") or subject_name(data, subject_id, lang)


def resolve_subject(data: dict, subject_id: str, profile: dict | None,
                    lang: str, with_detail: bool = False) -> str:
    """選択科目はプロファイルの講座で実際の科目名に解決する。"""
    if subject_id in (data.get("electives") or {}) and profile:
        chosen = (profile.get("electives") or {}).get(subject_id)
        if chosen:
            return subject_name(data, chosen, lang)
    name = subject_name(data, subject_id, lang)
    if with_detail:
        detail = ((data.get("subjects") or {}).get(subject_id) or {}).get("detail") or {}
        text = detail.get(lang) or detail.get("ja")
        if text:
            return f"{name}（{text}）"
    return name


def slot_by_class(data: dict, slot: dict) -> dict[str, str]:
    """スロットの cells を組（1〜8）キーにそろえる。"""
    if slot.get("by") == "class":
        return dict(slot.get("cells") or {})
    _, to_class = class_division_maps(data)
    return {to_class[div]: sid for div, sid in (slot.get("cells") or {}).items()
            if div in to_class}


def slot_changed(data: dict, slot: dict) -> set[str]:
    """変更セルの組キー集合。"""
    changed = slot.get("changed") or []
    if slot.get("by") == "class":
        return set(changed)
    _, to_class = class_division_maps(data)
    return {to_class[d] for d in changed if d in to_class}


def period_label(periods: list[int], lang: str) -> str:
    if lang == "en":
        return f"P{periods[0]}" if len(periods) == 1 else f"P{periods[0]}-{periods[-1]}"
    return f"{periods[0]}限" if len(periods) == 1 else f"{periods[0]}〜{periods[-1]}限"


def date_heading(data: dict, d: date, lang: str) -> str:
    if lang == "en":
        return f"{WEEKDAYS_EN[d.weekday()][:3]}, {d.strftime('%B')} {d.day}"
    return f"{d.month}月{d.day}日（{WEEKDAYS_JA[d.weekday()]}）"


def day_type_label(data: dict, entry: dict, lang: str) -> str:
    kind = entry.get("type")
    if not kind:
        return ""
    info = (data.get("dayTypes") or {}).get(kind) or {}
    return info.get(lang) or info.get("ja") or ""


def format_event(data: dict, d: date, lang: str) -> str:
    entry = data["days"][d.isoformat()]
    event = (data.get("events") or {}).get(entry["event"]) or {}
    name = event.get(lang) or event.get("ja") or entry["event"]
    lines = [f"## {date_heading(data, d, lang)} {name}"]
    note = (event.get("note") or {})
    text = note.get(lang) or note.get("ja")
    if text:
        lines += ["", text]
    return "\n".join(lines)


def format_day(data: dict, d: date, profile: dict | None = None,
               column: dict | None = None, lang: str = "ja") -> str:
    """1日分の時間割を Markdown で整形する。"""
    entry = (data.get("days") or {}).get(d.isoformat())
    if entry is None:
        if lang == "en":
            return f"No Tokuhen classes on {date_heading(data, d, lang)}."
        return f"{date_heading(data, d, lang)} は特編授業がありません。"
    if "event" in entry:
        return format_event(data, d, lang)
    type_label = day_type_label(data, entry, lang)
    type_suffix = f"（{type_label}）" if type_label else ""
    slots = entry.get("slots") or []
    if column is not None:
        if lang == "en":
            head = (f"## {date_heading(data, d, lang)} "
                    f"Class {column['class']} (Room {column['room']}) Timetable{type_suffix}")
        else:
            head = (f"## {date_heading(data, d, lang)} "
                    f"{column['class']}組（教室{column['room']}）の時間割{type_suffix}")
        lines = [head, ""]
        for slot in slots:
            cells = slot_by_class(data, slot)
            sid = cells.get(column["class"])
            if sid is None:
                continue
            name = resolve_subject(data, sid, profile, lang, with_detail=True)
            if column["class"] in slot_changed(data, slot):
                name += " (changed)" if lang == "en" else "（変更）"
            lines.append(f"- **{period_label(slot['periods'], lang)}** {name}")
        return "\n".join(lines)
    if lang == "en":
        head = f"## {date_heading(data, d, lang)} Tokuhen Timetable{type_suffix}"
    else:
        head = f"## {date_heading(data, d, lang)} 前期特編 時間割{type_suffix}"
    lines = [head, ""]
    classes = data.get("classes") or [str(i) for i in range(1, 9)]
    for slot in slots:
        cells = slot_by_class(data, slot)
        changed = slot_changed(data, slot)
        parts = []
        for class_no in classes:
            sid = cells.get(class_no)
            if sid is None:
                continue
            name = subject_short(data, sid, lang)
            if class_no in changed:
                name += " (changed)" if lang == "en" else "（変更）"
            parts.append(f"Class {class_no}: {name}" if lang == "en"
                         else f"{class_no}組：{name}")
        lines.append(f"- **{period_label(slot['periods'], lang)}** " + "／".join(parts))
    pairs = "、".join(f"{c['class']}組={c['division']}" for c in data.get("columns") or [])
    if pairs:
        lines += ["", ("Class/division: " if lang == "en" else "クラスの対応: ") + pairs]
    return "\n".join(lines)


# ---------- Typst（表形式） ----------


def _typst_escape(text: str) -> str:
    return (text.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
            .replace("#", "\\#").replace("$", "\\$"))


def build_typst_source(data: dict, d: date, profile: dict | None = None,
                       column: dict | None = None, lang: str = "ja") -> str:
    """表形式表示用の Typst ソース（PREAMBLE 込み）を組み立てる。"""
    from cogs.typst_render import PREAMBLE

    entry = (data.get("days") or {})[d.isoformat()]
    title = format_day(data, d, profile, column, lang).split("\n")[0].lstrip("# ").strip()
    if "event" in entry:
        return PREAMBLE + f"#text(size: 18pt, weight: \"bold\")[{_typst_escape(title)}]"
    slots = entry.get("slots") or []
    rows: list[tuple[str, dict[str, str], set[str]]] = []
    for slot in slots:
        cells = slot_by_class(data, slot)
        changed = slot_changed(data, slot)
        for p in slot["periods"]:
            rows.append((f"P{p}" if lang == "en" else f"{p}限", cells, changed))
    if column is not None:
        header = ["Period", "Subject"] if lang == "en" else ["時限", "科目"]
        body = []
        for label, cells, changed in rows:
            sid = cells.get(column["class"])
            if sid is None:
                continue
            name = resolve_subject(data, sid, profile, lang, with_detail=True)
            if column["class"] in changed:
                name += " (changed)" if lang == "en" else "（変更）"
            body.append((label, name))
        cols = 2
        cells_typst = [header] + [list(r) for r in body]
    else:
        classes = data.get("classes") or [str(i) for i in range(1, 9)]
        header = (["Period"] + [f"Class {c}" for c in classes]) if lang == "en" \
            else ["時限"] + [f"{c}組" for c in classes]
        cells_typst = [header]
        for label, cells, changed in rows:
            row = [label]
            for class_no in classes:
                sid = cells.get(class_no)
                name = subject_short(data, sid, lang) if sid else ""
                if sid and class_no in changed:
                    name += "（変更）" if lang != "en" else " (changed)"
                row.append(name)
            cells_typst.append(row)
        cols = len(header)
    flat = ", ".join(f"[{_typst_escape(c)}]" for row in cells_typst for c in row)
    return (PREAMBLE
            + f"#text(size: 18pt, weight: \"bold\")[{_typst_escape(title)}]\n\n"
            + f"#table(columns: {cols}, align: center, {flat})")


# ---------- Cog ----------


class Tokuhen(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.data = load_data()
        self.state_path = STATE_FILE
        self.profiles: dict = load_profiles(self.state_path)
        self.period_start, self.period_end = period_range(self.data)

    # ----- 返信まわり -----

    async def _send(self, message: discord.Message, content: str,
                    file: discord.File | None = None):
        try:
            await message.reply(content, file=file, mention_author=False)
        except discord.HTTPException:
            await message.channel.send(content, file=file)

    def _period_error(self, d: date, lang: str) -> str:
        if lang == "en":
            return (f"The Tokuhen timetable is only available from "
                    f"{self.period_start:%B} {self.period_start.day} to "
                    f"{self.period_end:%B} {self.period_end.day}. "
                    f"{d:%B} {d.day} is outside the term.")
        return (f"前期特編の時間割は {self.period_start.month}月{self.period_start.day}日"
                f"〜{self.period_end.month}月{self.period_end.day}日 の期間だけ表示できます。"
                f"{d.month}月{d.day}日 は期間外です。")

    async def _answer(self, send, author_id: int, d: date, text: str,
                      column: dict | None, force_table: bool):
        """send(content, file) を使って1日分に答える共通処理。"""
        lang = detect_lang(text)
        profile = self.profiles.get(str(author_id))
        if column is None and profile is not None:
            if profile.get("grade") == 3:
                column = profile_column(self.data, profile)
            elif profile.get("grade") is not None:
                msg = ("The Tokuhen timetable is for third-year students only."
                       if lang == "en" else
                       "前期特編は3年生対象です。組を指定すれば他の学年からも参照できます。")
                await send(msg, None)
                return
        if self.period_start is None or not (self.period_start <= d <= self.period_end):
            await send(self._period_error(d, lang), None)
            return
        entry = (self.data.get("days") or {}).get(d.isoformat())
        table = force_table and entry is not None and "event" not in entry
        if table:
            renderer = self.bot.get_cog("TypstRender")
            if renderer is not None:
                source = build_typst_source(self.data, d, profile, column, lang)
                pages, error = await renderer.render(source)
                if pages:
                    file = discord.File(io.BytesIO(pages[0]), filename="timetable.png")
                    await send(format_day(self.data, d, profile, column, lang)
                               .split("\n")[0].lstrip("# ").strip(), file)
                    return
                LOGGER.info("tokuhen: Typst変換に失敗したためテキストで返します: %s", error)
            note = ("(Could not render the table, so here it is as text.)"
                    if lang == "en" else "（表の画像化に失敗したためテキストで表示します）")
            await send(note + "\n" + format_day(self.data, d, profile, column, lang), None)
            return
        content = format_day(self.data, d, profile, column, lang)
        if column is None and profile is None:
            content += ("\n\nTip: run /tokuhen-setup once and I will remember your class "
                        "and electives." if lang == "en" else
                        "\n\n学年・組・講座を /tokuhen-setup で一度設定すると、"
                        "次回から自分の時間割を表示します。")
        await send(content, None)

    async def try_handle(self, message: discord.Message, text: str) -> bool:
        """メンション経路。時間割の質問なら答えて True、違えば False。"""
        if not is_timetable_query(text) or self.period_start is None:
            return False
        today = datetime.now(JST).date()
        d = parse_date(text, today, self.period_start, self.period_end)
        column = parse_column_ref(text, self.data)

        async def send(content, file):
            await self._send(message, content, file)

        await self._answer(send, message.author.id, d, text, column, wants_table(text))
        return True

    # ----- スラッシュコマンド -----

    @app_commands.command(name="tokuhen",
                          description="前期特編授業の時間割を表示します")
    @app_commands.describe(date="日付（2026-12-03 や「明日」など。省略時は今日）",
                           class_no="組（省略時は自分の設定→全体）",
                           format="表示形式")
    @app_commands.choices(class_no=[app_commands.Choice(name=f"{i}組", value=i)
                                    for i in range(1, 9)])
    @app_commands.choices(format=[
        app_commands.Choice(name="テキスト", value="text"),
        app_commands.Choice(name="表（画像）", value="table"),
    ])
    async def tokuhen(self, interaction: discord.Interaction,
                      date: str | None = None, class_no: int | None = None,
                      format: str = "text"):
        today = datetime.now(JST).date()
        d = parse_date(date, today, self.period_start, self.period_end) if date else today
        column = column_of(self.data, class_no=str(class_no)) if class_no else None
        if format == "table":
            await interaction.response.defer()

            async def send(content, file):
                await interaction.followup.send(content, file=file)
        else:
            async def send(content, file):
                await interaction.response.send_message(content, file=file)
        await self._answer(send, interaction.user.id, d, date or "", column,
                           format == "table")

    @app_commands.command(name="tokuhen-setup",
                          description="特編時間割用に学年・組・講座を記憶します（一度だけ）")
    @app_commands.describe(grade="学年", class_no="組", division="特編クラス",
                           soc_abn="社会ABNの講座", soc_bckl="社会BCKLの講座",
                           geo_pol="地理／政経の講座", sci_ab="理科基礎ABの講座",
                           sci_bc="理科基礎BCの講座", reset="設定を消す")
    @app_commands.choices(grade=[app_commands.Choice(name=f"{i}年", value=i)
                                 for i in (1, 2, 3)])
    @app_commands.choices(class_no=[app_commands.Choice(name=f"{i}組", value=i)
                                    for i in range(1, 9)])
    @app_commands.choices(division=[app_commands.Choice(name=c, value=c)
                                    for c in ("a", "b", "c", "k", "l", "m", "n", "o")])
    @app_commands.choices(soc_abn=[app_commands.Choice(name=n, value=v) for n, v in
                                   (("地理", "geo"), ("日本史", "jh"),
                                    ("世界史", "wh"), ("政経", "pol"))])
    @app_commands.choices(soc_bckl=[app_commands.Choice(name=n, value=v) for n, v in
                                    (("地理", "geo"), ("日本史", "jh"),
                                     ("世界史", "wh"), ("政経", "pol"))])
    @app_commands.choices(geo_pol=[app_commands.Choice(name=n, value=v) for n, v in
                                   (("地理", "geo"), ("政経", "pol"))])
    @app_commands.choices(sci_ab=[app_commands.Choice(name=n, value=v) for n, v in
                                  (("化学基礎", "chem-b"), ("生物基礎", "bio-b"),
                                   ("地学基礎", "earth-b"))])
    @app_commands.choices(sci_bc=[app_commands.Choice(name=n, value=v) for n, v in
                                  (("生物基礎", "bio-b"), ("地学基礎", "earth-b"))])
    async def tokuhen_setup(self, interaction: discord.Interaction,
                            grade: int | None = None, class_no: int | None = None,
                            division: str | None = None, soc_abn: str | None = None,
                            soc_bckl: str | None = None, geo_pol: str | None = None,
                            sci_ab: str | None = None, sci_bc: str | None = None,
                            reset: bool = False):
        key = str(interaction.user.id)
        if reset:
            self.profiles.pop(key, None)
            save_profiles(self.state_path, self.profiles)
            await interaction.response.send_message(
                "特編時間割の設定を消しました。", ephemeral=True)
            return
        profile = dict(self.profiles.get(key) or {})
        if grade is not None:
            profile["grade"] = grade
        to_div, to_class = class_division_maps(self.data)
        if class_no is not None:
            profile["class"] = str(class_no)
            if str(class_no) in to_div:
                profile["division"] = to_div[str(class_no)]
        elif division is not None:
            profile["division"] = division
            if division in to_class:
                profile["class"] = to_class[division]
        electives = dict(profile.get("electives") or {})
        for group, value in (("soc-abn", soc_abn), ("soc-bckl", soc_bckl),
                             ("geo-pol", geo_pol), ("sci-ab", sci_ab),
                             ("sci-bc", sci_bc)):
            if value is not None:
                electives[group] = value
        if electives:
            profile["electives"] = electives
        self.profiles[key] = profile
        save_profiles(self.state_path, self.profiles)
        await interaction.response.send_message(
            "特編時間割の設定を保存しました。\n" + self._profile_summary(profile),
            ephemeral=True)

    @app_commands.command(name="tokuhen-profile",
                          description="記憶している特編時間割の設定を表示します")
    async def tokuhen_profile(self, interaction: discord.Interaction):
        profile = self.profiles.get(str(interaction.user.id))
        if not profile:
            await interaction.response.send_message(
                "まだ設定がありません。/tokuhen-setup で学年・組・講座を設定してください。",
                ephemeral=True)
            return
        await interaction.response.send_message(
            self._profile_summary(profile), ephemeral=True)

    def _profile_summary(self, profile: dict) -> str:
        parts = []
        if profile.get("grade"):
            parts.append(f"学年: {profile['grade']}年")
        if profile.get("class"):
            parts.append(f"組: {profile['class']}組")
        if profile.get("division"):
            parts.append(f"特編クラス: {profile['division']}")
        for group, sid in (profile.get("electives") or {}).items():
            parts.append(f"{subject_name(self.data, group, 'ja')}: "
                         f"{subject_name(self.data, sid, 'ja')}")
        return "／".join(parts) if parts else "（未設定の項目があります）"


async def setup(bot: commands.Bot):
    await bot.add_cog(Tokuhen(bot))
