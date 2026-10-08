"""工具层测试。

运行：python -m unittest discover -s tests -v

这个文件守住的是三条底线：
    1. 路径锁得住（不能读写 workspace 之外）
    2. edit_file 拒绝模糊（找不到不猜、多处不改）
    3. 工具永不抛异常，失败都变成文字
外加一条回归：黑名单不能因为多打一个空格就被绕过。
"""

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from mini_agent import tools  # noqa: E402
from mini_agent.tools import (  # noqa: E402
    execute_tool,
    set_confirm_hook,
    tool_edit_file,
    tool_run_command,
    tool_write_file,
)


class ToolsTest(unittest.TestCase):
    """所有会碰文件系统的用例都改指向临时目录，不污染真实的 workspace/。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        old = tools.WORKSPACE_DIR
        tools.WORKSPACE_DIR = self._tmp.name
        self.addCleanup(setattr, tools, "WORKSPACE_DIR", old)
        set_confirm_hook(None)
        self.addCleanup(set_confirm_hook, None)

    def _read(self, rel):
        with open(os.path.join(self._tmp.name, rel), "r", encoding="utf-8") as f:
            return f.read()

    # ---------------------------------------------------------- 路径锁

    def test_safe_path_blocks_escape(self):
        with self.assertRaises(ValueError):
            tools._safe_path("../outside.txt")
        with self.assertRaises(ValueError):
            tools._safe_path(os.path.join("..", "..", "etc", "passwd"))

    def test_safe_path_allows_inside(self):
        p = tools._safe_path("sub/a.txt")
        self.assertTrue(p.startswith(self._tmp.name + os.sep))

    # ---------------------------------------------------------- edit_file

    def test_edit_file_single_match(self):
        tool_write_file("a.py", "x = 1\ny = 2\n")
        result = tool_edit_file("a.py", "y = 2", "y = 3")
        self.assertIn("已修改", result)
        self.assertEqual(self._read("a.py"), "x = 1\ny = 3\n")

    def test_edit_file_no_match_changes_nothing(self):
        tool_write_file("b.py", "x = 1\n")
        result = tool_edit_file("b.py", "这段根本不存在", "y = 3")
        self.assertIn("找不到", result)
        self.assertEqual(self._read("b.py"), "x = 1\n")

    def test_edit_file_multiple_matches_refuses(self):
        """出现多处时必须放弃 —— 宁可多跑一步，也不能改错地方。"""
        tool_write_file("c.py", "x = 1\nx = 1\n")
        result = tool_edit_file("c.py", "x = 1", "x = 2")
        self.assertIn("无法确定改哪一处", result)
        self.assertEqual(self._read("c.py"), "x = 1\nx = 1\n")

    # ---------------------------------------------------------- run_command

    def test_blacklist_rejects(self):
        for cmd in ("rm -rf /", "format c:", "shutdown"):
            with self.subTest(cmd=cmd):
                self.assertIn("安全策略拒绝", tool_run_command(cmd))

    def test_blacklist_rejects_extra_whitespace(self):
        """回归用例：子串判据怕空白变化，规范化之后必须仍然拦得住。"""
        self.assertIn("安全策略拒绝", tool_run_command("rm  -rf  /"))
        self.assertIn("安全策略拒绝", tool_run_command("rm\t-rf\t/"))

    def test_dangerous_command_asks_and_aborts_when_denied(self):
        seen = {}

        def deny(cmd, reason):
            seen["cmd"] = cmd
            seen["reason"] = reason
            return False

        set_confirm_hook(deny)
        result = tool_run_command("rm a.py")
        self.assertIn("已取消执行", result)
        self.assertEqual(seen["cmd"], "rm a.py")
        self.assertTrue(seen["reason"])

    def test_dangerous_command_runs_when_allowed(self):
        set_confirm_hook(lambda cmd, reason: True)
        result = tool_run_command("echo hi > out.txt")
        self.assertIn("退出码 0", result)
        self.assertTrue(os.path.isfile(os.path.join(self._tmp.name, "out.txt")))

    def test_safe_command_does_not_ask(self):
        """正常命令不该弹确认，否则 agent 每一步都要人批准，没法干活。"""

        def fail(_cmd, _reason):
            raise AssertionError("安全的命令不该请求确认")

        set_confirm_hook(fail)
        result = tool_run_command("python -c \"print(1)\"")
        self.assertIn("退出码", result)

    def test_multiline_command_is_refused(self):
        """含换行的命令必须被拦下来，而不是「静默只跑第一行还报成功」。

        这是通过 trace 复盘挖出来的：实测 `echo a↵echo b` 只输出 a，退出码却是 0；
        `python -c "↵print(1)↵"` 干脆什么都没有。对 agent 来说这比报错更危险 ——
        它会拿着「执行成功但没输出」这个假结论继续推理。
        """
        result = tool_run_command("echo a\necho b")
        self.assertIn("换行", result)
        self.assertIn("静默丢弃", result)
        self.assertIn("write_file", result)  # 得给一条能走通的替代方案

    def test_multiline_command_does_not_execute_first_line(self):
        """拦下来还不够：连第一行都不能执行，否则副作用照样发生。

        实测 `echo a↵echo b` 在修复前是「只输出 a」，所以这里断言 b 不在也 a 不在。
        （刻意不写文件：写文件的命令会命中「重定向覆盖」那条高危规则，
        先弹确认框，测不到换行这一层。）
        """
        result = tool_run_command("echo aaa\necho bbb")
        self.assertNotIn("aaa", result)
        self.assertNotIn("bbb", result)

    def test_dangerous_multiline_command_still_refused_as_dangerous(self):
        """既危险又多行时，优先按危险处理 —— 安全判据不能因为换行就失效。"""
        result = tool_run_command("rm -rf /\necho ok")
        self.assertIn("安全策略拒绝", result)

    # ---------------------------------------------------------- execute_tool

    def test_unknown_tool_lists_available_ones(self):
        """不只是报错，还要顺手告诉模型有什么 —— 它下一轮才能自己改对。"""
        result = execute_tool("no_such_tool", "{}")
        self.assertIn("没有名为", result)
        self.assertIn("read_file", result)

    def test_bad_json_is_reported_not_raised(self):
        result = execute_tool("read_file", "{not json")
        self.assertIn("合法 JSON", result)

    def test_wrong_argument_name_is_reported(self):
        result = execute_tool("read_file", '{"wrong_key": 1}')
        self.assertIn("参数不对", result)

    def test_path_escape_becomes_text_not_exception(self):
        result = execute_tool("read_file", '{"path": "../../etc/passwd"}')
        self.assertIn("路径越界", result)

    def test_path_escape_explains_the_boundary(self):
        """错误文案必须把边界讲清楚，不能只说「越界」。

        实测教训：早期只说「路径越界」，模型理解成「文件不存在」，
        还会一本正经地编解释 —— 它压根不知道是自己被挡住了。
        """
        result = execute_tool("read_file", '{"path": "../.env"}')
        self.assertIn("workspace/", result)
        self.assertIn("不等于文件不存在", result)


if __name__ == "__main__":
    unittest.main()
