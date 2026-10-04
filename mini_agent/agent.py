"""Agent 主循环 —— 整个项目的心脏，也是评委最想听你讲清楚的部分。

循环一共只有四步，反复执行直到结束：
    1. 把「到目前为止发生的一切」（messages）发给模型
    2. 模型回复：要么要调工具，要么直接说话
    3. 要调工具 → 我们在本地真的执行 → 把结果作为一条新消息追加进 messages
    4. 回到第 1 步

三个终止条件（必须都有，缺一不可）：
    A. 模型不再要求调用工具 → 说明它认为任务完成了（正常结束）
    B. 达到最大步数 → 强制结束（防止模型陷入死循环，把你的钱烧光）
    C. 用户点了「停止」→ 强制结束（人必须随时能叫停它）

【这一版最重要的改造：把 print 换成事件】
之前的写法是边跑边 print，这等于把「逻辑」和「显示」焊死在一起 ——
命令行能用，网页就完全拿不到过程。现在改成每一步 emit 一个事件，
由调用方决定怎么呈现：命令行打印成文字，网页渲染成卡片。
核心逻辑因此只有一份，两种界面共用它。
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
    def __init__(self, client, max_steps: int = 20, verbose: bool = True, on_event=None):
        self.client = client
        self.max_steps = max_steps
        self.verbose = verbose
        # on_event：外部注入的事件接收器。网页模式下由它把事件推给浏览器；
        # 不给的话就退回命令行打印（verbose=False 时什么都不做）。
        self.on_event = on_event
        # 对话历史 = agent 的全部「记忆」，交给 Conversation 统一管理
        self.history: Conversation | None = None
        # 停止开关：网页上的「停止」按钮就是置这个标记。
        # 为什么不用 kill 线程？强杀线程可能留下写了一半的文件，
        # 让它自己走完当前这一步再收手，状态才是干净的。
        self._stop_requested = False

    def request_stop(self) -> None:
        """请求停止。agent 会在下一步开始前退出循环。"""
        self._stop_requested = True

    # ------------------------------------------------------------ 事件

    def emit(self, kind: str, **payload) -> None:
        """把一件事告诉外部。kind 决定了呈现方式，payload 是具体内容。"""
        fn = self.on_event
        if fn is None:
            fn = self._print_event if self.verbose else None
        if fn is None:
            return
        try:
            fn(kind, payload)
        except Exception:  # 显示层出错绝不能反过来把 agent 弄崩
            pass

    def _print_event(self, kind: str, p: dict) -> None:
        """命令行的默认呈现方式（保持和改造前一模一样的输出格式）。"""
        if kind == "step":
            print(f"\n─── 第 {p['step']} 步 " + "─" * 30)
        elif kind == "compact":
            print(f"（上下文超过预算，压缩了 {p['rounds']} 次）")
        elif kind == "message":
            print(f"模型说：{p['content']}")
        elif kind == "tool_call":
            print(f"  调用工具 → {p['name']}({p['arguments']})")
        elif kind == "tool_result":
            r = p["result"]
            preview = r if len(r) <= 400 else r[:400] + " …(已省略)"
            print(f"  返回结果 → {preview}")
        elif kind == "done":
            print("（模型没有调用工具，任务结束）")
            print(p["stats"])
        elif kind == "max_steps":
            print("\n" + p["summary"])
            print(p["stats"])
        elif kind == "stopped":
            print("\n" + p["summary"])
            print(p["stats"])
        elif kind == "error":
            print(f"\n[出错] {p['message']}")

    def _stats(self) -> str:
        """统计这一轮产生了多大的对话历史。

        注意这里的对比：本次总大小 vs 预算。
        一旦超过预算 Conversation 就会自动压缩，这就是为什么
        长任务跑下去也不会把上下文撑爆。
        """
        total = self.history.size() if self.history else 0
        compactions = self.history.compactions if self.history else 0
        line = (
            f"[统计] 对话历史：{total} 字符 / 预算 {MAX_CONTEXT_CHARS}"
            f"，压缩过 {compactions} 次"
        )
        usage = getattr(self.client, "usage", None)
        if usage and usage.get("requests"):
            extra = f"，共 {usage['requests']} 次请求"
            if usage.get("retries"):
                extra += f"（含 {usage['retries']} 次重试）"
            if usage.get("total"):
                extra += (
                    f"，消耗 token：输入 {usage['prompt']} / 输出 {usage['completion']}"
                    f" / 合计 {usage['total']}"
                )
            line += extra
        return line

    def _usage(self) -> dict:
        usage = getattr(self.client, "usage", None)
        return dict(usage) if usage else {}

    # ------------------------------------------------------------ 主循环

    def run(self, task: str) -> str:
        """执行一个任务，返回模型的最终答复。"""
        self._stop_requested = False
        self.history = Conversation(SYSTEM_PROMPT, budget_chars=MAX_CONTEXT_CHARS)
        self.history.add({"role": "user", "content": task})
        self.emit("start", task=task, max_steps=self.max_steps)

        for step in range(1, self.max_steps + 1):
            if self._stop_requested:
                summary = "已被手动停止（用户点了停止按钮）。"
                self.emit("stopped", summary=summary, stats=self._stats(), usage=self._usage())
                return summary

            self.emit("step", step=step)

            # 0) 每次发请求前先看看历史有没有超预算，超了就压缩
            rounds = self.history.ensure_fits()
            if rounds:
                self.emit("compact", rounds=rounds)

            # 1) 问模型接下来做什么
            try:
                reply: AssistantMessage = self.client.chat(self.history.all(), TOOL_SCHEMAS)
            except Exception as e:  # 网络/认证出错时不能让整个程序崩掉
                self.emit("error", message=str(e))
                return f"出错：{e}"
            self.history.add(reply.to_message())

            if reply.content:
                self.emit("message", content=reply.content)

            # 2) 终止条件 A：模型不再要工具了
            if not reply.tool_calls:
                self.emit(
                    "done",
                    summary=reply.content,
                    stats=self._stats(),
                    usage=self._usage(),
                    steps=step,
                )
                return reply.content

            # 3) 逐个执行工具，把结果回写进对话历史
            for call in reply.tool_calls:
                self.emit("tool_call", id=call.id, name=call.name, arguments=call.arguments)
                result = execute_tool(call.name, call.arguments)
                self.emit("tool_result", id=call.id, name=call.name, result=result)

                self.history.add({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": result,
                })

        # 4) 终止条件 B：步数兜底
        summary = f"达到最大步数 {self.max_steps}，循环被强制结束（可能是模型陷入了反复重试）。"
        self.emit("max_steps", summary=summary, stats=self._stats(), usage=self._usage())
        return summary
