import io
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from PIL import Image

from cogs.countdown import load_state, remaining_days, render_image, save_state

BASE_DATE = date(2026, 11, 27)


def make_state(days=50):
    return {"base_days": days, "base_date": BASE_DATE.isoformat(), "enabled": True}


class RemainingDaysTests(unittest.TestCase):
    def test_same_day(self):
        self.assertEqual(remaining_days(make_state(), BASE_DATE), 50)

    def test_next_day(self):
        self.assertEqual(remaining_days(make_state(), date(2026, 11, 28)), 49)

    def test_zero_on_target_day(self):
        self.assertEqual(remaining_days(make_state(), date(2027, 1, 16)), 0)

    def test_negative_after_target_day(self):
        self.assertEqual(remaining_days(make_state(), date(2027, 1, 17)), -1)


class StateTests(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            save_state(make_state(), path)
            self.assertEqual(load_state(path), make_state())

    def test_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(load_state(Path(tmp) / "none.json"))

    def test_broken_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("{broken", encoding="utf-8")
            self.assertIsNone(load_state(path))

    def test_missing_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text(json.dumps({"enabled": True}), encoding="utf-8")
            self.assertIsNone(load_state(path))


class RenderTests(unittest.TestCase):
    def test_size_matches_base(self):
        im = Image.open(io.BytesIO(render_image(42)))
        self.assertEqual(im.size, (1179, 717))

    def test_number_changes_pixels_only_in_patch(self):
        base = Image.open(
            Path(__file__).parent.parent / "assets" / "countdown_base.jpg"
        ).convert("RGB")
        rendered = Image.open(io.BytesIO(render_image(42))).convert("RGB")
        # パッチの外（吹き出し付近）は元の画像とピクセル単位で一致する
        region = (700, 100, 900, 300)
        self.assertEqual(base.crop(region).tobytes(), rendered.crop(region).tobytes())
        # 数字の範囲は変わっている
        digits = (336, 240, 431, 330)
        self.assertNotEqual(base.crop(digits).tobytes(), rendered.crop(digits).tobytes())

    def test_different_days_differ(self):
        self.assertNotEqual(render_image(42), render_image(41))


if __name__ == "__main__":
    unittest.main()
