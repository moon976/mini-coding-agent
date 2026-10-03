"""上下文（对话历史）管理。

题目明确点名「对话历史与上下文管理」必须自己写，指的就是这个文件。

问题从哪来？
    每一轮我们都会把「全部历史」交给模型。工具返回的结果往往很大
    （一次测试失败的完整堆栈就有几千字符），跑十几轮之后
    历史就涨到几万字符：更贵、更慢，超过上限还会直接请求失败。

最简单的办法是「超过预算就扔掉最早的几条」——但这里有个致命细节：
    一条 assistant 消息如果带了 tool_calls，后面必然跟着若干条 tool 消息，
    它们是一组。如果只删 assistant 而留下 tool 消息，接口会直接报错
    （相当于你对模型说"这是 call_3 的结果"，但历史里根本没有 call_3）。
    所以必须**整组整组地删**，而且切口要落在 assistant 消息之前。
"""

from __future__ import annotations

from .config import MAX_CONTEXT_CHARS


MAX_COMPACT_ROUNDS = 50


class Conversation:
    """一段对话历史，带「超预算就压缩」的能力。"""

    def __init__(self, system_prompt: str, budget_chars: int = MAX_CONTEXT_CHARS):
        self.system = {"role": "system", "content": system_prompt}
        self.messages: list = []
        self.budget = budget_chars
        self.task: str = ""
        self.compactions = 0

    def add(self, message: dict) -> None:
        if message.get("role") == "user" and not self.task:
            self.task = message.get("content", "")
        self.messages.append(message)

    def all(self) -> list:
        """交给模型的完整消息列表。"""
        return [self.system] + self.messages

    def size(self) -> int:
        """当前历史大概占多少字符（粗略估算，够用了）。"""
        total = len(self.system["content"])
        for m in self.messages:
            total += len(str(m.get("content") or ""))
        return total

    def compact(self) -> bool:
        """压缩一次：删掉最早的一整组「模型说话 + 它引发的工具结果」。成功返回 True。

        必须整组删，理由见文件开头。这里还有一个容易写错的坑：
        如果每次删完都往头部再插一条「说明话」，而下一次删除又只删到那条说明为止，
        就会出现「删一条、加一条」，大小纹丝不动 → 主循环里 while 直接死循环。
        （我们第一版就踩了这个坑。）
        所以这里的做法是：连同之前的说明话一起删掉，再插入一条新的，
        保证每一轮都净减少至少一整组内容。
        """
        start = next(
            (i for i, m in enumerate(self.messages) if m.get("role") == "assistant"),
            None,
        )
        if start is None:  # 还没有任何 assistant，无从下手
            return False

        # 这组消息包括 assistant 本身，以及紧跟其后的所有 tool 结果
        end = start + 1
        while end < len(self.messages) and self.messages[end].get("role") == "tool":
            end += 1

        dropped_n = end  # 含前面可能被一并删掉的提示消息
        del self.messages[:end]
        self.compactions += 1

        self.messages.insert(0, {
            "role": "user",
            "content": (
                f"[上下文已压缩] 为控制长度，最早的 {dropped_n} 条记录已被移除。"
                f"你最初的任务是：{self.task}。"
                f"缺少信息请用工具重新查看，然后继续。"
            ),
        })
        return True

    def ensure_fits(self) -> int:
        """反复压缩直到装得下。返回压缩次数。

        这里额外加了一道保险：最多压 MAX_COMPACT_ROUNDS 次。
        哪怕将来压缩逻辑被人改坏、每轮不再变小，也只是放弃压缩而不是卡死整个程序。
        """
        rounds = 0
        while self.size() > self.budget and rounds < MAX_COMPACT_ROUNDS:
            if not self.compact():
                break
            rounds += 1
        return rounds
