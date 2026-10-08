"""上下文 token 估算器 —— 零依赖，靠真实 usage 回喂自动校准。

为什么需要它？
    原来我们用「字符数」当预算。这对中文是**系统性地失真**的：
    英文大约 4 个字符 1 个 token，而中文一个字往往就要 1 个 token 甚至更多。
    同一份预算，装英文能装 4 倍的量，装中文只能装 1 倍 ——
    按字符算，中文任务会**过早压缩**（白白丢信息），英文任务会**过晚压缩**（请求超限报错）。

为什么不用 tiktoken？
    这个项目的硬约束是「零第三方依赖」。而且每个厂商的分词表不一样，
    装了 tiktoken 也只是拿 OpenAI 的表去猜别人的模型，未必更准。

那怎么准？用模型自己告诉我们的数字。
    每次请求返回里都有真实的 `prompt_tokens`。攒够两次样本，
    就能反推出「这个模型 + 这份对话，平均几个字符算 1 个 token」。

为什么要用「差分」而不是直接除？
    因为真实 `prompt_tokens` 里不只有对话历史，还有一份**固定的工具说明书开销**
    （6 个工具的 JSON schema，每次请求都要带上，大概几百 token）。
    直接拿 `总token / 总字符` 会把这份固定开销摊进单价里，越算越离谱。
    做两次差分，常数项就被消掉了：

        第 N 次：tokens_N = schema + chars_N / cpt
        第 N+1次：tokens_{N+1} = schema + chars_{N+1} / cpt
        两式相减：Δtokens = Δchars / cpt   →   cpt = Δchars / Δtokens

    这样得到的才是**真正的边际单价**。
"""

from __future__ import annotations

from .config import CHARS_PER_TOKEN_HINT


class TokenEstimator:
    """用「上一次的真实 token 数」校准「这一次该估多少」。

    用法：
        est = TokenEstimator()
        est.estimate(chars)          # 请求前：这段历史大概多少 token
        est.calibrate(chars, real)   # 请求后：把真实数字喂回来，下次更准
    """

    def __init__(self, chars_per_token: float = CHARS_PER_TOKEN_HINT):
        # 初始猜测值。中文多、英文少会在跑过两轮之后自动修正，所以起点粗糙点没关系。
        self.chars_per_token = chars_per_token
        self._prev: tuple[int, int] | None = None  # (当时字符数, 当时真实 token)
        self.calibrations = 0

    def estimate(self, chars: int) -> int:
        """字符数 → 估算 token 数。"""
        if chars <= 0:
            return 0
        return int(chars / self.chars_per_token) + 1

    def calibrate(self, chars: int, real_tokens: int) -> bool:
        """喂回一次真实观测。攒够两次样本才算得出单价，返回是否真的校准了。"""
        if real_tokens <= 0 or chars <= 0:
            return False

        calibrated = False
        if self._prev is not None:
            prev_chars, prev_tokens = self._prev
            delta_chars = chars - prev_chars
            delta_tokens = real_tokens - prev_tokens
            # 两个样本差得太少时噪声会淹没信号，宁可不校准
            if delta_chars >= CHARS_PER_TOKEN_HINT * 200 and delta_tokens > 0:
                observed = delta_chars / delta_tokens
                # 平滑：新观测只占一半权重，防止某一次的抖动把单价带跑偏
                self.chars_per_token = self.chars_per_token * 0.5 + observed * 0.5
                # 夹住上下限。跑飞了（比如拿到异常 usage）也不能让预算失控。
                # 上限取 4.0（约等于纯英文的水平）而不是更大：估算偏低会导致**过晚压缩**，
                # 后果是请求直接超限失败；估算偏高只是早一点压缩，损失一点信息。
                # 两者之间，宁可早压。
                self.chars_per_token = max(0.5, min(self.chars_per_token, 4.0))
                calibrated = True
                self.calibrations += 1

        self._prev = (chars, real_tokens)
        return calibrated

    def describe(self) -> str:
        """给统计行用的一句话说明。"""
        if self.calibrations:
            return f"{self.chars_per_token:.2f} 字符/token（已校准 {self.calibrations} 次）"
        return f"{self.chars_per_token:.2f} 字符/token（初始值，尚未校准）"
