import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from cogs import ai_reply as ar
from cogs.ai_reply import AIReply, deep_search_requested, detect_deep


def make_cog(search_allowed=True):
    cog = AIReply.__new__(AIReply)
    cog.model = ar.GROQ_MODEL
    cog.deep_model = ar.DEEP_MODEL
    cog._search_allowed = lambda user_id: search_allowed
    cog._groq = AsyncMock(return_value={
        "choices": [{"message": {"content": "あ" * 2000}}],
        "usage": {},
    })
    cog.bot = SimpleNamespace(user=SimpleNamespace(id=999999))
    return cog


def fake_message(content, author_id=1, name="質問者", msg_id=10):
    return SimpleNamespace(
        id=msg_id,
        content=content,
        attachments=[],
        mentions=[],
        reference=None,
        author=SimpleNamespace(id=author_id, display_name=name),
    )


class DetectDeepTests(unittest.TestCase):
    def test_trigger_at_start(self):
        self.assertEqual(detect_deep("深く考えて、なぜ空は青いの？"), "なぜ空は青いの？")

    def test_trigger_in_middle(self):
        self.assertEqual(detect_deep("この解法が合っているか、じっくり検証してほしい"), "この解法が合っているか、ほしい")

    def test_each_trigger(self):
        for trigger in ar.DEEP_TRIGGERS:
            self.assertIsNotNone(detect_deep(f"{trigger}ください"), trigger)

    def test_no_trigger(self):
        self.assertIsNone(detect_deep("なぜ空は青いの？"))
        self.assertIsNone(detect_deep("考えておいて"))

    def test_trigger_only_is_empty(self):
        self.assertEqual(detect_deep("深く考えて"), "")


class DeepSearchTests(unittest.TestCase):
    def test_search_words(self):
        self.assertTrue(deep_search_requested("最新の観測結果を調べて"))
        self.assertTrue(deep_search_requested("検索して考えて"))

    def test_no_search_word(self):
        self.assertFalse(deep_search_requested("この式を導出して"))


class AskTests(unittest.IsolatedAsyncioTestCase):
    async def test_deep_payload(self):
        cog = make_cog()
        answer = await cog._ask("質問だけのプロンプト", user_id=7, deep=True)
        payload = cog._groq.await_args.args[0]
        self.assertEqual(payload["model"], ar.DEEP_MODEL)
        self.assertEqual(payload["reasoning_effort"], "medium")
        self.assertEqual(payload["max_completion_tokens"], ar.DEEP_MAX_COMPLETION_TOKENS)
        self.assertNotIn("tools", payload)
        self.assertIn("深く考える回", payload["messages"][0]["content"])
        self.assertEqual(len(answer), ar.DEEP_MAX_OUTPUT_CHARS)

    async def test_deep_payload_with_search(self):
        cog = make_cog()
        await cog._ask("プロンプト", user_id=7, deep=True, want_search=True)
        payload = cog._groq.await_args.args[0]
        self.assertEqual(payload["tools"], [{"type": "browser_search"}])
        self.assertEqual(payload["max_completion_tokens"], ar.DEEP_SEARCH_COMPLETION_TOKENS)

    async def test_deep_search_respects_daily_search_limit(self):
        cog = make_cog(search_allowed=False)
        await cog._ask("プロンプト", user_id=7, deep=True, want_search=True)
        payload = cog._groq.await_args.args[0]
        self.assertNotIn("tools", payload)

    async def test_normal_payload_unchanged(self):
        cog = make_cog()
        answer = await cog._ask("プロンプト", user_id=7)
        payload = cog._groq.await_args.args[0]
        self.assertEqual(payload["model"], ar.GROQ_MODEL)
        self.assertEqual(payload["reasoning_effort"], "low")
        self.assertEqual(payload["tools"], [{"type": "browser_search"}])
        self.assertEqual(len(answer), ar.MAX_OUTPUT_CHARS)


class BuildPromptTests(unittest.IsolatedAsyncioTestCase):
    def make_cog(self):
        cog = AIReply.__new__(AIReply)
        cog.bot = SimpleNamespace(user=SimpleNamespace(id=999999))
        return cog

    async def test_deep_without_reply_sends_question_only(self):
        cog = self.make_cog()
        message = fake_message("質問です")

        def forbidden_history(**kwargs):
            raise AssertionError("Deepでは直近の会話を読まない")
        message.channel = SimpleNamespace(history=forbidden_history)
        prompt = await cog._build_prompt(message, "質問です", deep=True)
        self.assertEqual(prompt, "【質問】\n質問者: 質問です")

    async def test_deep_with_reply_includes_only_direct_parent(self):
        cog = self.make_cog()
        parent = fake_message("対象の発言", author_id=2, name="先輩", msg_id=5)
        message = fake_message("深く考えて")
        message.reference = SimpleNamespace(message_id=5, resolved=None)

        async def fetch_message(message_id):
            return parent
        message.channel = SimpleNamespace(fetch_message=fetch_message)
        prompt = await cog._build_prompt(message, "これについて考えて", deep=True)
        self.assertIn("【返信先の発言】\n先輩: 対象の発言", prompt)
        self.assertIn("【質問】\n質問者: これについて考えて", prompt)
        self.assertNotIn("直近の会話", prompt)
        self.assertNotIn("返信先の会話", prompt)

    async def test_normal_prompt_still_includes_recent(self):
        cog = self.make_cog()
        recent_item = fake_message("直近の発言", author_id=3, name="友人", msg_id=9)
        message = fake_message("質問です")

        def history(limit, before):
            async def gen():
                yield recent_item
            return gen()
        message.channel = SimpleNamespace(history=history)
        prompt = await cog._build_prompt(message, "質問です")
        self.assertIn("【直近の会話】", prompt)
        self.assertIn("友人: 直近の発言", prompt)


class DeepLimitTests(unittest.TestCase):
    def test_daily_limit(self):
        cog = AIReply.__new__(AIReply)
        cog._deep_daily = {}
        self.assertIsNone(cog._check_deep_limit(42))
        for _ in range(ar.DEEP_USER_DAILY_LIMIT):
            cog._count_deep_use(42)
        self.assertEqual(cog._check_deep_limit(42), ar.DEEP_LIMIT_TEXT)
        self.assertIsNone(cog._check_deep_limit(43))


if __name__ == "__main__":
    unittest.main()
