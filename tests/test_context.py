"""上下文（对话历史）测试。

这个文件守住的是两条最容易写错、且错了就会让接口报错的规则：
    1. system 守则永远不会被压缩删掉
    2. 压缩必须「整组删」—— 绝不能留下找不到主人的孤儿 tool 消息
外加一条：保险丝必须真的能终止循环。
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from mini_agent.context import MAX_COMPACT_ROUNDS, Conversation  # noqa: E402
from mini_agent.tokens import TokenEstimator  # noqa: E402


class ConversationTest(unittest.TestCase):
    def _conv(self, budget: int = 20000) -> Conversation:
        # 预算单位是 token。测试里把单价钉死成 1 字符 = 1 token，
        # 这样「多少字符 = 多少 token」，断言写起来直观，也不受校准影响。
        return Conversation("员工守则", budget_tokens=budget,
                            estimator=TokenEstimator(chars_per_token=1.0))

    def _turn(self, conv: Conversation, call_id: str, content: str) -> None:
        """往历史里追加一组「模型说话 + 工具结果」。"""
        conv.add({"role": "assistant", "content": f"说一句 {call_id}",
                  "tool_calls": [{"id": call_id}]})
        conv.add({"role": "tool", "tool_call_id": call_id, "content": content})

    def assertNoOrphanTool(self, messages: list) -> None:
        """断言不存在「前面没有 assistant 的 tool 消息」。

        这种孤儿消息会让 OpenAI 接口直接报错：
        相当于你告诉模型"这是 call_3 的结果"，可历史里根本没有发出 call_3 的那条。
        """
        seen_assistant = False
        for m in messages:
            role = m.get("role")
            if role == "assistant":
                seen_assistant = True
            elif role == "tool" and not seen_assistant:
                self.fail(f"出现孤儿 tool 消息：{m}")

    def test_system_prompt_survives_compaction(self):
        conv = self._conv()
        conv.add({"role": "user", "content": "任务"})
        self._turn(conv, "a", "结果 A")
        conv.compact()
        self.assertEqual(conv.all()[0]["role"], "system")
        self.assertEqual(conv.all()[0]["content"], "员工守则")

    def test_compact_removes_whole_group(self):
        conv = self._conv()
        conv.add({"role": "user", "content": "原始任务"})
        self._turn(conv, "a", "结果 A")
        self._turn(conv, "b", "结果 B")

        self.assertTrue(conv.compact())

        self.assertNoOrphanTool(conv.messages)
        remaining = [str(m.get("content")) for m in conv.messages]
        self.assertNotIn("结果 A", remaining)   # 第一组整组消失
        self.assertIn("结果 B", remaining)      # 第二组还在

    def test_task_is_remembered_after_compaction(self):
        """最初那条 user 任务消息本身会被删掉，所以必须重新插回去。"""
        conv = self._conv()
        conv.add({"role": "user", "content": "帮我修好 hello.py"})
        self._turn(conv, "a", "结果 A")

        conv.compact()

        joined = " ".join(str(m.get("content")) for m in conv.messages)
        self.assertIn("帮我修好 hello.py", joined)

    def test_compact_returns_false_without_assistant(self):
        conv = self._conv()
        conv.add({"role": "user", "content": "还没开始"})
        self.assertFalse(conv.compact())

    def test_compaction_shrinks_history(self):
        """每压一轮必须净减少，否则主循环里的 while 永远出不来（第一版踩过）。"""
        conv = self._conv()
        conv.add({"role": "user", "content": "任务"})
        for i in range(6):
            self._turn(conv, f"c{i}", "x" * 500)

        before = conv.size()
        conv.compact()
        self.assertLess(conv.size(), before)

    def test_ensure_fits_stops_at_fuse(self):
        """保险丝：哪怕压缩逻辑被改坏，也只是放弃压缩而不是卡死。"""
        conv = self._conv(budget=1)
        conv.add({"role": "user", "content": "x"})
        for i in range(300):
            self._turn(conv, f"c{i}", "y" * 200)

        rounds = conv.ensure_fits()
        self.assertLessEqual(rounds, MAX_COMPACT_ROUNDS)
        self.assertEqual(conv.compactions, rounds)


if __name__ == "__main__":
    unittest.main()
