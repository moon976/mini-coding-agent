"""主循环测试 —— 用假模型把「模型」这个变量钉死，单独验证循环。

守住的是四件事：
    1. 三个终止出口各自都能触发（正常 / 步数兜底 / 用户停止）
    2. 模型出错时返回一段文字，而不是让整个程序崩掉
    3. tool 结果的 id 必须能对上 assistant 发出的 tool_call，否则接口会报错
    4. 事件流完整（网页和 trace 都靠它）
"""

import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from mini_agent import tools  # noqa: E402
from mini_agent.agent import Agent  # noqa: E402
from mini_agent.llm import MockClient  # noqa: E402


class BoomClient:
    """最小假客户端：一被问就抛异常，用来验证 agent 不会跟着崩。"""

    def __init__(self, message="网络炸了"):
        self.message = message
        self.usage = {"prompt": 0, "completion": 0, "total": 0, "requests": 0, "retries": 0}

    def chat(self, messages, tools=None):
        raise RuntimeError(self.message)


class AgentLoopTest(unittest.TestCase):
    def setUp(self):
        # 假模型的剧本会真的写文件、跑命令，所以把工作目录指到临时目录
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        old = tools.WORKSPACE_DIR
        tools.WORKSPACE_DIR = self._tmp.name
        self.addCleanup(setattr, tools, "WORKSPACE_DIR", old)
        self.events: list = []

    def _agent(self, client, **kw) -> Agent:
        kw.setdefault("max_steps", 20)
        return Agent(client, verbose=False,
                     on_event=lambda kind, payload: self.events.append((kind, payload)),
                     **kw)

    def _kinds(self) -> list:
        return [k for k, _ in self.events]

    # ------------------------------------------------------ 终止条件

    def test_terminates_normally(self):
        """条件 A：模型不再要工具 → 正常结束。"""
        result = self._agent(MockClient("fix")).run("修好 hello.py")
        self.assertIn("任务完成", result)
        self.assertIn("done", self._kinds())
        self.assertEqual(self._kinds()[0], "start")

    def test_max_steps_brake(self):
        """条件 B：一直打转 → 被步数兜底拦下，绝不能无限跑。"""
        result = self._agent(MockClient("loop"), max_steps=3).run("一直打转")
        self.assertIn("达到最大步数", result)
        self.assertIn("max_steps", self._kinds())
        self.assertNotIn("done", self._kinds())
        self.assertEqual(sum(1 for k in self._kinds() if k == "step"), 3)

    def test_user_can_stop_midway(self):
        """条件 C：跑到一半叫停 → 下一步开始前就退出。"""
        client = MockClient("loop")
        agent = Agent(client, max_steps=50, verbose=False)

        def handler(kind, payload):
            self.events.append((kind, payload))
            if kind == "step":
                agent.request_stop()  # 刚跑完第一步就叫停

        agent.on_event = handler
        result = agent.run("随时能停")
        self.assertIn("已被手动停止", result)
        self.assertIn("stopped", self._kinds())
        self.assertNotIn("done", self._kinds())
        # 停止标记是在【下一步开始前】检查的，所以当前这一步会走完
        self.assertEqual(sum(1 for k in self._kinds() if k == "step"), 1)

    def test_stop_flag_is_cleared_at_next_run(self):
        """停止标记必须在新任务开始时清掉，否则第二个任务一启动就秒退。"""
        agent = Agent(MockClient("loop"), max_steps=5, verbose=False)

        def handler(kind, payload):
            if kind == "step":
                agent.request_stop()

        agent.on_event = handler
        self.assertIn("已被手动停止", agent.run("第一个任务"))

        agent.on_event = None  # 第二个任务不再叫停
        second = agent.run("第二个任务")
        self.assertNotIn("已被手动停止", second)
        self.assertIn("达到最大步数", second)

    # ------------------------------------------------------ 健壮性

    def test_model_error_becomes_text_not_crash(self):
        result = self._agent(BoomClient("网络炸了")).run("随便一个任务")
        self.assertIn("出错", result)
        self.assertIn("error", self._kinds())

    # ------------------------------------------------------ 历史一致性

    def test_tool_results_match_tool_calls(self):
        """每条 tool 结果都必须有对应的 tool_call，否则接口会直接报错。"""
        agent = self._agent(MockClient("fix"))
        agent.run("修好 hello.py")

        called, answered = set(), set()
        for m in agent.history.all():
            for tc in (m.get("tool_calls") or []):
                called.add(tc["id"])
            if m.get("role") == "tool":
                answered.add(m.get("tool_call_id"))

        self.assertTrue(called, "假模型剧本应该产生过工具调用")
        self.assertEqual(answered, called)

    def test_each_run_starts_a_fresh_history(self):
        """每个任务都是一本新记录本 —— 这是「不记得上一轮」的已知限制。"""
        agent = self._agent(MockClient("fix"))
        agent.run("第一个任务")
        first = agent.history
        agent.run("第二个任务")
        self.assertIsNot(agent.history, first)

    # ------------------------------------------------------ 事件流

    def test_event_stream_is_complete(self):
        """网页渲染和 trace 落盘都靠这些事件，缺一种界面就会少一块。"""
        self._agent(MockClient("fix")).run("修好 hello.py")
        kinds = self._kinds()
        for expected in ("start", "step", "message", "tool_call", "tool_result", "done"):
            with self.subTest(event=expected):
                self.assertIn(expected, kinds)


class UsageCalibrationTest(unittest.TestCase):
    """验证「真实 token 消耗」真的回流到了 token 估算器里。

    这条链路很容易接漏：估算器写好了、context 也有 calibrate 方法，
    但主循环如果忘了在每次请求后调用它，校准就永远不会发生 ——
    而代码照样能跑，只是悄悄退化成「永远用初始猜测值」。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        old = tools.WORKSPACE_DIR
        tools.WORKSPACE_DIR = self._tmp.name
        self.addCleanup(setattr, tools, "WORKSPACE_DIR", old)

    def test_real_usage_is_fed_back_to_estimator(self):
        # 一条能产生大段输出的命令，保证两次观测之间历史涨得够多（阈值 500 字符）
        dump = json.dumps({"command": "python -c \"print('x'*1200)\""})

        class FakeClient:
            """第一次要工具、第二次收尾；每次都报一个「真实」的 prompt token 数。

            构造方式：500 是固定的工具说明书开销，2.0 是真实单价（2 字符 = 1 token）。
            能从这个假数据里反推出 2.0，就说明差分确实把那 500 的固定开销消掉了。
            """

            def __init__(self):
                self.usage = {"prompt": 0, "completion": 0, "total": 0,
                              "requests": 0, "retries": 0}
                self.last_prompt_tokens = 0
                self.calls = 0

            def chat(self, messages, tools=None):
                self.calls += 1
                chars = sum(len(str(m.get("content") or "")) for m in messages)
                self.last_prompt_tokens = 500 + int(chars / 2.0)
                self.usage["requests"] += 1
                from mini_agent.llm import AssistantMessage, ToolCall
                if self.calls == 1:
                    return AssistantMessage(
                        content="",
                        tool_calls=[ToolCall(id="c1", name="run_command", arguments=dump)],
                    )
                return AssistantMessage(content="好了", tool_calls=[])

        client = FakeClient()
        agent = Agent(client, max_steps=3, verbose=False)
        agent.run("任务")

        self.assertEqual(client.calls, 2, "这条测试需要恰好两次请求才校准得出单价")
        est = agent.history.estimator
        self.assertEqual(est.calibrations, 1)
        # 初始 2.5 被拉向真实值 2.0：2.5*0.5 + 2.0*0.5 = 2.25
        self.assertAlmostEqual(est.chars_per_token, 2.25, delta=0.05)

    def test_zero_usage_leaves_estimator_untouched(self):
        """假模型 usage 恒为 0，估算器应该保持初始猜测，不能被 0 带偏。"""
        agent = Agent(MockClient("fix"), max_steps=1, verbose=False)
        agent.run("随便一个任务")
        self.assertEqual(agent.history.estimator.calibrations, 0)
        self.assertEqual(agent.history.estimator.chars_per_token, 2.5)


if __name__ == "__main__":
    unittest.main()
