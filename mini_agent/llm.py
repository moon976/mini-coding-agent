"""模型层：只做一件事 —— 把 messages 发给模型，把回复「归一化」成同一种结构。

这里我们只用厂商 SDK 的「发一个 HTTP 请求」能力，
判断、循环、工具执行全部在我们自己的代码里 —— 这是题目明确要求的边界。

设计要点：真实客户端和假客户端返回同一种数据结构（AssistantMessage），
所以主循环完全不需要知道自己在跟谁说话。好处有两个：
  1. 没有 API key 也能把整个循环跑通（--mock 模式），方便学习和自测；
  2. 换模型厂商时，主循环一行都不用改。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass
class ToolCall:
    """模型的一次工具调用请求。arguments 是 JSON 字符串（还没解析）。"""
    id: str
    name: str
    arguments: str


@dataclass
class AssistantMessage:
    """模型回复的统一表示：要么说一段话，要么要求调用若干工具，也可能两者都有。"""
    content: str = ""
    tool_calls: list = field(default_factory=list)

    def to_message(self) -> dict:
        """转回 OpenAI 消息格式，以便追加进对话历史。"""
        msg = {"role": "assistant", "content": self.content or ""}
        if self.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.name, "arguments": c.arguments},
                }
                for c in self.tool_calls
            ]
        return msg


class OpenAICompatClient:
    """真实模型客户端。兼容 OpenAI 接口的所有厂商都能用（DeepSeek、通义、智谱、硅基流动…）。"""

    def __init__(self, api_key: str, base_url: str, model: str, temperature: float = 0.0):
        try:
            from openai import OpenAI
        except ImportError as e:
            raise RuntimeError("缺少依赖，请先执行：pip install openai") from e
        self.model = model
        # temperature 控制「发挥的稳定性」：0 表示每次尽量给出同样的答案。
        # 做 agent 一般都调得很低 —— 我们是来干活的，不是来抽奖的。
        self.temperature = temperature
        self.client = OpenAI(api_key=api_key, base_url=base_url or None)

    def chat(self, messages: list, tools: list) -> AssistantMessage:
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tools,
            tool_choice="auto",  # 让模型自己决定要不要用工具
            temperature=self.temperature,
        )
        msg = resp.choices[0].message
        calls = [
            ToolCall(id=tc.id, name=tc.function.name, arguments=tc.function.arguments)
            for tc in (msg.tool_calls or [])
        ]
        return AssistantMessage(content=msg.content or "", tool_calls=calls)


class MockClient:
    """假模型：按剧本回放固定回复，用来在没有 key 时验证主循环。

    为什么要做假模型？
        它把「模型」这个变量固定住，于是你能单独验证「主循环」对不对。
        这跟做实验时控制变量是一个道理：模型的行为是随机的，
        而循环的逻辑必须每次都一样，出问题才查得出来。

    三个剧本分别演示三件事：
        fix   —— 正常完成任务（终止条件 A）
        retry —— 命令失败后换方案重试（错误回传给模型的价值）
        loop  —— 一直打转不结束（终止条件 B：步数兜底）
    """

    SCRIPTS = {
        "fix": [
            AssistantMessage(
                content="我先看看工作目录里有什么。",
                tool_calls=[ToolCall(id="call_1", name="list_dir", arguments='{"path": "."}')],
            ),
            AssistantMessage(
                content="有个 hello.py，读一下。",
                tool_calls=[ToolCall(id="call_2", name="read_file", arguments='{"path": "hello.py"}')],
            ),
            AssistantMessage(
                content="找到了：add 函数写成了减法。我改成加法并验证一下。",
                tool_calls=[
                    ToolCall(
                        id="call_3",
                        name="write_file",
                        arguments=json.dumps({
                            "path": "hello.py",
                            "content": "def add(a, b):\n    return a + b\n\n\nif __name__ == '__main__':\n    print(add(2, 3))\n",
                        }),
                    )
                ],
            ),
            AssistantMessage(
                content="改完了，跑一下确认结果。",
                tool_calls=[ToolCall(id="call_4", name="run_command", arguments='{"command": "python hello.py"}')],
            ),
            AssistantMessage(
                content=(
                    "任务完成。hello.py 里的 add 函数原本写成了 `return a - b`，"
                    "导致 add(2, 3) 返回 -1；已改成 `return a + b`，运行验证输出 5，符合预期。"
                ),
                tool_calls=[],  # 不再调用工具 —— 主循环就在这里结束（终止条件 A）
            ),
        ],
        "retry": [
            AssistantMessage(
                content="先读一下 hello.py。",
                tool_calls=[ToolCall(id="r1", name="read_file", arguments='{"path": "hello.py"}')],
            ),
            AssistantMessage(
                content="add 写成了减法，改成加法。",
                tool_calls=[
                    ToolCall(
                        id="r2",
                        name="write_file",
                        arguments=json.dumps({
                            "path": "hello.py",
                            "content": "def add(a, b):\n    return a + b\n\n\nif __name__ == '__main__':\n    print(add(2, 3))\n",
                        }),
                    )
                ],
            ),
            AssistantMessage(
                content="跑一下验证。",
                # 故意用了一个你这台机器上不存在的命令，好看看失败长什么样
                tool_calls=[ToolCall(id="r3", name="run_command", arguments='{"command": "python3 hello.py"}')],
            ),
            AssistantMessage(
                content=(
                    "上一条命令返回退出码 9009，说明系统里没有 python3 这个命令，"
                    "并不是我改错了。换个名字再试一次。"
                ),
                tool_calls=[ToolCall(id="r4", name="run_command", arguments='{"command": "python hello.py"}')],
            ),
            AssistantMessage(
                content="这次输出 5、退出码 0，验证通过。任务完成。",
                tool_calls=[],
            ),
        ],
        "loop": [
            AssistantMessage(
                content="我得再确认一下目录结构。",
                tool_calls=[ToolCall(id="l1", name="list_dir", arguments='{"path": "."}')],
            ),
        ],
    }

    def __init__(self, script: str = "fix"):
        if script not in self.SCRIPTS:
            raise ValueError(f"没有这个剧本：{script}，可选：{', '.join(self.SCRIPTS)}")
        self.script = self.SCRIPTS[script]
        self.task = None

    def chat(self, messages: list, tools: list = None) -> AssistantMessage:
        # 用「历史里已有几条 assistant 消息」决定现在该说第几句
        turn = sum(1 for m in messages if m.get("role") == "assistant")

        # 第一次被问到时，把用户给的任务记下来，并在第一句话里复述出来。
        # 这样你一眼就能确认：任务确实传进来了，只是假模型平时不看它。
        if self.task is None:
            self.task = next(
                (m.get("content", "") for m in messages if m.get("role") == "user"), ""
            )

        reply = self.script[min(turn, len(self.script) - 1)]
        if turn == 0:
            return AssistantMessage(
                content=f"（我收到的任务是：{self.task}）" + reply.content,
                tool_calls=reply.tool_calls,
            )
        return reply
