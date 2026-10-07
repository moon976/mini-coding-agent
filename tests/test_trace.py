"""trace（事件落盘与统计）测试。

守住的是三件事：
    1. 落盘能原样读回来（否则复盘就是假的）
    2. 结局判定准确 —— 成功率这个数字错一点，简历上就是错的
    3. 文件名不覆盖已有记录
"""

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from mini_agent.trace import (  # noqa: E402
    TraceRecorder,
    load_events,
    report,
    summarize,
)


def _events(*kinds):
    return [{"type": k} for k in kinds]


class TraceTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name

    # ---------------------------------------------------------- 落盘与回放

    def test_roundtrip(self):
        path = os.path.join(self.dir, "a.jsonl")
        rec = TraceRecorder(path)
        rec.record("start", {"task": "修 bug"})
        rec.record("step", {"step": 1})
        rec.record("done", {"summary": "好了"})
        rec.close()

        events = load_events(path)
        self.assertEqual([e["type"] for e in events], ["start", "step", "done"])
        self.assertEqual(events[0]["task"], "修 bug")

    def test_filenames_do_not_collide(self):
        """一秒内跑两次（中文任务名会被过滤成空 slug）也不能互相覆盖。"""
        a = TraceRecorder.new_file(self.dir, hint="演示一个中文任务")
        a.close()
        b = TraceRecorder.new_file(self.dir, hint="演示另一个中文任务")
        b.close()
        self.assertNotEqual(a.path, b.path)
        self.assertEqual(len(os.listdir(self.dir)), 2)

    # ---------------------------------------------------------- 统计

    def test_summarize_outcome(self):
        self.assertEqual(summarize(_events("start", "step", "done"))["outcome"], "done")
        self.assertEqual(summarize(_events("start", "max_steps"))["outcome"], "max_steps")
        self.assertEqual(summarize(_events("start", "stopped"))["outcome"], "stopped")
        self.assertEqual(summarize(_events("start", "error"))["outcome"], "error")
        self.assertEqual(summarize(_events("start"))["outcome"], "unknown")

    def test_summarize_counts(self):
        events = [
            {"type": "start", "task": "t"},
            {"type": "step", "step": 1},
            {"type": "tool_call", "name": "read_file"},
            {"type": "tool_result", "name": "read_file", "result": "内容"},
            {"type": "step", "step": 2},
            {"type": "tool_call", "name": "run_command"},
            {"type": "tool_result", "name": "run_command", "result": "错误：退出码 1"},
            {"type": "done", "usage": {"prompt": 100, "completion": 10, "total": 110,
                                       "requests": 2, "retries": 0}},
        ]
        s = summarize(events)
        self.assertEqual(s["steps"], 2)
        self.assertEqual(s["tool_calls"], 2)
        self.assertEqual(s["tool_failures"], 1)   # 只有退出码 1 那条算失败
        self.assertEqual(s["tools"], ["read_file", "run_command"])
        self.assertEqual(s["total_tokens"], 110)
        self.assertEqual(s["requests"], 2)

    def test_failure_detection(self):
        """失败判据跟前端高亮用的是同一套：错误文本、被取消、退出码非 0。"""
        cases = [
            ("错误：找不到这段文字", True),
            ("已取消执行：…", True),
            ("--- stdout ---\n5\n[退出码 0]", False),
            ("--- stderr ---\nboom\n[退出码 1]", True),
        ]
        for result, expected in cases:
            with self.subTest(result=result[:20]):
                s = summarize([{"type": "tool_result", "result": result}])
                self.assertEqual(s["tool_failures"], 1 if expected else 0)

    def test_report_on_empty_dir(self):
        self.assertIn("没有", report(self.dir))

    def test_report_aggregates(self):
        for name, kinds in (
            ("one.jsonl", ("start", "step", "done")),
            ("two.jsonl", ("start", "step", "max_steps")),
        ):
            rec = TraceRecorder(os.path.join(self.dir, name))
            for k in kinds:
                rec.record(k, {})
            rec.close()

        text = report(self.dir)
        self.assertIn("50.0%", text)   # 2 个任务里 1 个成功
        self.assertIn("正常完成", text)
        self.assertIn("达到步数上限", text)


if __name__ == "__main__":
    unittest.main()
