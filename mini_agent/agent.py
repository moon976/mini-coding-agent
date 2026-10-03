"""Agent 主循环 —— 整个项目的心脏，也是评委最想听你讲清楚的部分。

循环一共只有四步，反复执行直到结束：
    1. 把「到目前为止发生的一切」（messages）发给模型
    2. 模型回复：要么要调工具，要么直接说话
    3. 要调工具 → 我们在本地真的执行 → 把结果作为一条新消息追加进 messages
    4. 回到第 1 步

两个终止条件（必须都有，缺一不可）：
    A. 模型不再要求调用工具 → 说明它认为任务完成了（正常结束）
    B. 达到最大步数 → 强制结束（防止模型陷入死循环，把你的钱烧光）
"""

from __future__ import annotations

from .config import MAX_CONTEXT_CHARS
from .context import Conversation
from .llm import AssistantMessage
from .tools import TOOL_SCHEMAS, execute_tool

SYSTEM_PROMPT = """你是一个编程智能体（coding agent），只能在用户的 workspace 目录中工作。

【语言】所有输出一律使用简体中文，包括每一步的思考说明和最终总结。

工作原则：
1. 先看再改：先用 list_dir / read_file / search_text 搞清楚现状，不要凭空猜测文件内容。
2. 改已有文件用 edit_file（只改要改的那几行），不要用 write_file 整个重写；
   只有新建文件、或内容几乎全变时才用 write_file。
3. 小步验证：修改后尽量用 run_command 跑一下（运行脚本、跑测试）确认真的生效。
4. 会看报错：命令失败时，读 stderr 和退出码，换一个方案重试，不要原样重试同一个命令。
5. 坦诚报告：如果做不到，直接说做不到的原因，不要假装成功。
6. 任务完成后，不要再调用工具，用自然语言说明你改了什么、验证结果如何。
"""


class Agent:
    def __init__(self, client, max_steps: int = 20, verbose: bool = True):
        self.client = client
        self.max_steps = max_steps
        self.verbose = verbose
        # 对话历史 = agent 的全部「记忆」，交给 Conversation 统一管理
        self.history: Conversation | None = None

    def _log(self, text: str) -> None:
        if self.verbose:
            print(text)

    def _stats(self) -> str:
        """统计这一轮产生了多大的对话历史。

        注意这里的对比：本次总大小 vs 预算。
        一旦超过预算 Conversation 就会自动压缩，这就是为什么
        长任务跑下去也不会把上下文撑爆。
        """
        total = self.history.size() if self.history else 0
        compactions = self.history.compactions if self.history else 0
        return (
            f"[统计] 对话历史：{total} 字符 / 预算 {MAX_CONTEXT_CHARS}"
            f"，压缩过 {compactions} 次"
        )

    def run(self, task: str) -> str:
        """执行一个任务，返回模型的最终答复。"""
        self.history = Conversation(SYSTEM_PROMPT, budget_chars=MAX_CONTEXT_CHARS)
        self.history.add({"role": "user", "content": task})

        for step in range(1, self.max_steps + 1):
            self._log(f"\n─── 第 {step} 步 " + "─" * 30)

            # 0) 每次发请求前先看看历史有没有超预算，超了就压缩
            rounds = self.history.ensure_fits()
            if rounds:
                self._log(f"（上下文超过预算，压缩了 {rounds} 次）")

            # 1) 问模型接下来做什么
            reply: AssistantMessage = self.client.chat(self.history.all(), TOOL_SCHEMAS)
            self.history.add(reply.to_message())

            if reply.content:
                self._log(f"模型说：{reply.content}")

            # 2) 终止条件 A：模型不再要工具了
            if not reply.tool_calls:
                self._log("（模型没有调用工具，任务结束）")
                self._log(self._stats())
                return reply.content

            # 3) 逐个执行工具，把结果回写进对话历史
            for call in reply.tool_calls:
                self._log(f"  调用工具 → {call.name}({call.arguments})")
                result = execute_tool(call.name, call.arguments)
                preview = result if len(result) <= 400 else result[:400] + " …(已省略)"
                self._log(f"  返回结果 → {preview}")

                self.history.add({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": result,
                })

        # 4) 终止条件 B：步数兜底
        summary = f"达到最大步数 {self.max_steps}，循环被强制结束（可能是模型陷入了反复重试）。"
        self._log("\n" + summary)
        self._log(self._stats())
        return summary
