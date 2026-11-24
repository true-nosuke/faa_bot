"""tokuhen（前期特編時間割）の単体テスト。"""
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cogs.tokuhen import (Tokuhen, build_typst_source, detect_lang, format_day,
                          is_timetable_query, load_data, load_profiles,
                          parse_column_ref, parse_date, period_range,
                          profile_column, resolve_subject, save_profiles,
                          wants_table)

DATA = load_data()
START, END = period_range(DATA)
TODAY = date(2026, 12, 1)  # 火曜日


class IntentTests(unittest.TestCase):
    def test_positives(self):
        for text in ["今日の時間割は？", "明日の特編教えて", "12月3日の時間割を表で",
                     "金曜日は何限まである？", "What's my timetable for tomorrow?",
                     "what classes do I have today", "schedule for December 3"]:
            self.assertTrue(is_timetable_query(text), text)

    def test_negatives(self):
        for text in ["明日の時間割をタスクに追加して", "時間割のリマインドを設定して",
                     "おはよう", "明日の天気は？", "add a task for tomorrow"]:
            self.assertFalse(is_timetable_query(text), text)

    def test_wants_table(self):
        self.assertTrue(wants_table("12月3日の時間割を表で見せて"))
        self.assertTrue(wants_table("時間割をtypstで"))
        self.assertFalse(wants_table("今日の時間割は？"))

    def test_detect_lang(self):
        self.assertEqual(detect_lang("timetable for tomorrow"), "en")
        self.assertEqual(detect_lang("明日の時間割"), "ja")


class ParseDateTests(unittest.TestCase):
    def parse(self, text):
        return parse_date(text, TODAY, START, END)

    def test_relative_days(self):
        self.assertEqual(self.parse("今日の時間割"), date(2026, 12, 1))
        self.assertEqual(self.parse("明日の時間割"), date(2026, 12, 2))
        self.assertEqual(self.parse("明後日の時間割"), date(2026, 12, 3))
        self.assertEqual(self.parse("昨日の時間割"), date(2026, 11, 30))
        self.assertEqual(self.parse("timetable for tomorrow"), date(2026, 12, 2))

    def test_month_day_and_year_resolution(self):
        self.assertEqual(self.parse("12月3日の時間割"), date(2026, 12, 3))
        self.assertEqual(self.parse("1月18日の時間割"), date(2027, 1, 18))
        self.assertEqual(self.parse("2026-12-03"), date(2026, 12, 3))
        self.assertEqual(self.parse("December 3"), date(2026, 12, 3))
        self.assertEqual(self.parse("3 December"), date(2026, 12, 3))

    def test_weekday_only_is_next_occurrence(self):
        self.assertEqual(self.parse("金曜日の時間割"), date(2026, 12, 4))
        self.assertEqual(self.parse("火曜日の時間割"), date(2026, 12, 1))  # 今日が該当

    def test_week_plus_weekday(self):
        self.assertEqual(self.parse("来週月曜日の時間割"), date(2026, 12, 7))
        self.assertEqual(self.parse("今週金曜日の時間割"), date(2026, 12, 4))

    def test_default_is_today(self):
        self.assertEqual(self.parse("時間割教えて"), TODAY)


class PeriodTests(unittest.TestCase):
    def test_range(self):
        self.assertEqual(START, date(2026, 11, 30))
        self.assertEqual(END, date(2027, 1, 18))


class FormatTests(unittest.TestCase):
    def test_overview(self):
        text = format_day(DATA, date(2026, 11, 30))
        self.assertIn("## 11月30日（月） 前期特編 時間割", text)
        self.assertIn("1組：理基", text)
        self.assertIn("クラスの対応:", text)
        self.assertNotIn("📅", text)

    def test_day_type_label(self):
        text = format_day(DATA, date(2026, 12, 8))  # 午前特
        self.assertIn("（午前特）", text)

    def test_detail_with_room(self):
        column = profile_column(DATA, {"class": "7"})
        text = format_day(DATA, date(2026, 11, 30), column=column)
        self.assertIn("7組（教室242）の時間割", text)
        self.assertIn("数学①", text)

    def test_elective_resolution(self):
        column = profile_column(DATA, {"class": "1"})
        profile = {"grade": 3, "class": "1", "electives": {"sci-ab": "chem-b"}}
        text = format_day(DATA, date(2026, 11, 30), profile=profile, column=column)
        self.assertIn("化学基礎", text)
        plain = format_day(DATA, date(2026, 11, 30), column=column)
        self.assertIn("理科基礎AB（化・生・地）", plain)

    def test_resolve_subject_direct(self):
        profile = {"electives": {"geo-pol": "pol"}}
        self.assertEqual(resolve_subject(DATA, "geo-pol", profile, "ja"), "政経")
        self.assertEqual(resolve_subject(DATA, "geo-pol", None, "ja", with_detail=True),
                         "地理／政経（地理・政経）")

    def test_event_day(self):
        text = format_day(DATA, date(2027, 1, 5))
        self.assertIn("河合塾 Kパック模試", text)
        self.assertIn("8:40", text)

    def test_no_class_day(self):
        text = format_day(DATA, date(2026, 12, 26))  # 冬休み
        self.assertIn("特編授業がありません", text)

    def test_changed_mark(self):
        text = format_day(DATA, date(2027, 1, 13))
        self.assertIn("（変更）", text)

    def test_english(self):
        text = format_day(DATA, date(2026, 11, 30), lang="en")
        self.assertIn("Tokuhen Timetable", text)
        self.assertIn("Class 1: Sci", text)


class ColumnRefTests(unittest.TestCase):
    def test_class_ref(self):
        col = parse_column_ref("3組の時間割は？", DATA)
        self.assertEqual(col["class"], "3")
        self.assertEqual(col["division"], "c")

    def test_division_ref(self):
        col = parse_column_ref("Lの時間割", DATA)
        self.assertEqual(col["class"], "7")
        self.assertEqual(col["division"], "l")

    def test_no_ref(self):
        self.assertIsNone(parse_column_ref("今日の時間割は？", DATA))


class TypstTests(unittest.TestCase):
    def test_overview_table_source(self):
        source = build_typst_source(DATA, date(2026, 11, 30))
        self.assertIn("#table", source)
        self.assertIn("理基", source)
        self.assertIn("1組", source)

    def test_detail_table_source(self):
        column = profile_column(DATA, {"class": "7"})
        source = build_typst_source(DATA, date(2026, 11, 30), column=column)
        self.assertIn("数学①", source)


class ProfileStoreTests(unittest.TestCase):
    def test_roundtrip(self):
        profiles = {"123": {"grade": 3, "class": "7", "division": "l",
                            "electives": {"sci-ab": "chem-b"}}}
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "tokuhen_state.json"
            save_profiles(path, profiles)
            self.assertEqual(load_profiles(path), profiles)


class FakeBot:
    def get_cog(self, name):
        return None


class HandleTests(unittest.IsolatedAsyncioTestCase):
    def make_cog(self):
        cog = Tokuhen.__new__(Tokuhen)
        cog.bot = FakeBot()
        cog.data = DATA
        cog.state_path = Path(tempfile.mkdtemp()) / "state.json"
        cog.profiles = {}
        cog.period_start, cog.period_end = START, END
        return cog

    def make_message(self, content):
        message = SimpleNamespace(
            author=SimpleNamespace(id=999),
            channel=SimpleNamespace(send=AsyncMock()),
            reply=AsyncMock())
        return message

    async def test_handles_timetable_question(self):
        cog = self.make_cog()
        message = self.make_message("12月3日の時間割は？")
        handled = await cog.try_handle(message, "12月3日の時間割は？")
        self.assertTrue(handled)
        content = message.reply.call_args[0][0]
        self.assertIn("12月3日", content)
        self.assertIn("前期特編 時間割", content)

    async def test_period_out_error(self):
        cog = self.make_cog()
        message = self.make_message("今日の時間割は？")
        # 実日付は期間外（2026-11-30より前）とは限らないため、期間外の日付で確認
        handled = await cog.try_handle(message, "11月29日の時間割は？")
        self.assertTrue(handled)
        self.assertIn("期間外", message.reply.call_args[0][0])

    async def test_ignores_other_messages(self):
        cog = self.make_cog()
        message = self.make_message("おはよう")
        self.assertFalse(await cog.try_handle(message, "おはよう"))
        message.reply.assert_not_called()


if __name__ == "__main__":
    unittest.main()
