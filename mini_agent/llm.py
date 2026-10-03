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
import time
from dataclasses import dataclass, field

# 遇到这几类错误【不重试】：重试一百次也不会变好，只会白白浪费时间。
# 比如 key 填错了，重试一万次还是 401。
NO_RETRY_ERRORS = (
    "AuthenticationError",    # key 错了 / 没充钱
    "PermissionDeniedError",
    "BadRequestError",        # 请求本身有问题
    "NotFoundError",          # 模型名写错了
)


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

    # 退避的等待秒数：第 1 次等 1 秒，第 2 次 2 秒，第 3 次 4 秒……
    # 为什么要越等越久？网络抖动通常很快恢复，等 1 秒就够；
    # 但如果是服务端限流，立刻猛重试只会让它限得更狠。
    BACKOFF_BASE = 1.0

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        temperature: float = 0.0,
        max_retries: int = 3,
    ):
        try:
            from openai import OpenAI
        except ImportError as e:
            raise RuntimeError("缺少依赖，请先执行：pip install openai") from e
        self.model = model
        # temperature 控制「发挥的稳定性」：0 表示每次尽量给出同样的答案。
        # 做 agent 一般都调得很低 —— 我们是来干活的，不是来抽奖的。
        self.temperature = temperature
        self.max_retries = max_retries
        self.client = OpenAI(api_key=api_key, base_url=base_url or None)
        # 累计消耗。有了它，「上下文管理为什么重要」就从一句空话变成了能看见的数字。
        self.usage = {"prompt": 0, "completion": 0, "total": 0, "requests": 0, "retries": 0}

    def _should_retry(self, err: Exception) -> bool:
        """判断这个错误值不值得再试一次。"""
        if type(err).__name__ in NO_RETRY_ERRORS:
            return False
        code = getattr(err, "status_code", None)
        # 4xx 是「你发错了」，重试没意义；只有 429（限流）例外，等一会儿可能就放行了
        if code is not None and 400 <= code < 500 and code != 429:
            return False
        return True

    def _friendly_error(self, err: Exception) -> str:
        """把 SDK 抛的英文异常翻译成能直接照做的话。"""
        name = type(err).__name__
        code = getattr(err, "status_code", None)
        if name == "AuthenticationError" or code == 401:
            return (
                "认证失败（401）。最可能的原因：\n"
                "  1. .env 里的 key 不对 —— 注意 key 只在创建那一刻完整显示一次，\n"
                "     后台列表里看到的 sk-4444****99u8 是打码版，不能用来请求；\n"
                "  2. 账户余额不足。"
            )
        if name == "NotFoundError" or code == 404:
            return f"找不到模型（404）。检查 .env 里的 LLM_MODEL 是否拼写正确（当前：{self.model}）。"
        if code == 429:
            return "被限流（429）：请求太密集。已自动退避重试，若持续出现请放慢调用。"
        if name == "APIConnectionError":
            return "连不上服务器。检查网络，或确认 LLM_BASE_URL 填对了。"
        return f"请求失败：{type(err).__name__}: {err}"

    def chat(self, messages: list, tools: list) -> AssistantMessage:
        last_err = None
        for attempt in range(self.max_retries):
            try:
                resp = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    tools=tools,
                    tool_choice="auto",  # 让模型自己决定要不要用工具
                    temperature=self.temperature,
                )
                self.usage["requests"] += 1
                if getattr(resp, "usage", None):
                    u = resp.usage
                    self.usage["prompt"] += getattr(u, "prompt_tokens", 0) or 0
                    self.usage["completion"] += getattr(u, "completion_tokens", 0) or 0
                    self.usage["total"] += getattr(u, "total_tokens", 0) or 0
                msg = resp.choices[0].message
                calls = [
                    ToolCall(id=tc.id, name=tc.function.name, arguments=tc.function.arguments)
                    for tc in (msg.tool_calls or [])
                ]
                return AssistantMessage(content=msg.content or "", tool_calls=calls)

            except Exception as err:  # noqa: BLE001 —— 任何异常都要先判断能不能重试
                last_err = err
                if not self._should_retry(err):
                    raise RuntimeError(self._friendly_error(err)) from err
                if attempt < self.max_retries - 1:
                    wait = self.BACKOFF_BASE * (2 ** attempt)
                    self.usage["retries"] += 1
                    print(
                        f"[重试 {attempt + 1}/{self.max_retries - 1}] "
                        f"{type(err).__name__}，{wait:.1f} 秒后再试…"
                    )
                    time.sleep(wait)

        raise RuntimeError(
            f"连续 {self.max_retries} 次请求都失败了。最后一次的原因：{self._friendly_error(last_err)}"
        )


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
        # 假模型不消耗真 token，但保持同样的结构，主循环就不用区分两种客户端
        self.usage = {"prompt": 0, "completion": 0, "total": 0, "requests": 0, "retries": 0}

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
