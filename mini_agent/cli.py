"""命令行入口：把各层拼起来，并提供两种运行模式。

    python main.py --mock                 不需要 key，用假模型跑一遍完整循环
    python main.py "帮我修好 hello.py"     用真实模型
"""

from __future__ import annotations

import argparse
import sys

from .agent import Agent
from .config import TRACES_DIR, load_settings
from .llm import MockClient, OpenAICompatClient
from .tools import set_confirm_hook


def build_client(use_mock: bool, script: str = "fix", announce: bool = True):
    """造一个模型客户端。

    注意这里【不再直接 sys.exit】，而是把问题抛成 RuntimeError。
    原因：这个函数现在有两个调用方 —— 命令行可以直接退出，
    但网页模式下退出整个进程意味着服务挂掉，它得把错误显示给用户。
    「谁调用谁决定怎么处理错误」，被调用方只负责说清楚出了什么事。
    """
    if use_mock:
        if announce:
            print(f"[模式] 假模型（剧本：{script}）：不需要 API key，用来验证主循环本身是否正常。")
        return MockClient(script)

    s = load_settings()
    if not s["api_key"]:
        raise RuntimeError(
            "没有读到 API key。请先复制 .env.example 为 .env 并填入你的 key，\n"
            "或者先跑 `python main.py --mock` 看看循环是怎么转的。"
        )
    if not s["model"]:
        raise RuntimeError("没有读到模型名，请在 .env 里设置 LLM_MODEL。")
    if "*" in s["api_key"] or len(s["api_key"]) < 20:
        # 这是一个真实踩过的坑：厂商后台列表里显示的是 sk-4444****99u8 这种打码版，
        # 复制它填进 .env，程序不报错，只会在真正请求时来一句含糊的 401。
        # 在这里直接拦下来，省得对着 401 猜半天。
        raise RuntimeError(
            f"这个 key 看着不对（长度 {len(s['api_key'])}，含打码字符）。\n"
            "注意：key 只在【创建成功的那一刻】完整显示一次，之后后台列表里看到的\n"
            "sk-4444****99u8 是打码版，不能用来请求。请去后台新建一条 key 并完整复制。"
        )

    if announce:
        print(
            f"[模式] 真实模型：{s['model']}  @ {s['base_url'] or '默认地址'}"
            f"  (temperature={s['temperature']})"
        )
    return OpenAICompatClient(
        api_key=s["api_key"],
        base_url=s["base_url"],
        model=s["model"],
        temperature=s["temperature"],
    )


def main() -> None:
    # Windows 控制台默认可能是 GBK，强制用 UTF-8 输出，避免中文乱码
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="mini coding agent")
    parser.add_argument("task", nargs="?", help="交给 agent 的任务；不给则进入交互输入")
    parser.add_argument("--mock", action="store_true", help="用假模型，不需要 API key")
    parser.add_argument(
        "--script",
        default="fix",
        choices=["fix", "retry", "loop"],
        help="假模型的剧本：fix=正常完成 / retry=失败后换方案 / loop=一直打转（默认 fix）",
    )
    parser.add_argument("--max-steps", type=int, default=20, help="最大循环步数，默认 20")
    parser.add_argument("--quiet", action="store_true", help="不打印中间过程")
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="高危命令不再询问，直接执行。只在无人值守时用，默认关闭",
    )
    parser.add_argument(
        "--web",
        action="store_true",
        help="启动网页界面（浏览器打开 http://127.0.0.1:8000）",
    )
    parser.add_argument("--port", type=int, default=8000, help="网页界面的端口，默认 8000")
    parser.add_argument(
        "--trace",
        action="store_true",
        help="把本次任务的完整事件流存到 traces/ 目录（jsonl），可事后复盘与统计",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="读取 traces/ 里所有记录，打印成功率 / 平均步数 / 平均 token 等统计",
    )
    args = parser.parse_args()

    if args.report:
        from .trace import report

        print(report(TRACES_DIR))
        return

    if args.web:
        from .web import serve

        serve(port=args.port, use_mock=args.mock, script=args.script,
              max_steps=args.max_steps, auto_yes=args.yes)
        return

    if args.yes:
        # 自动放行，但每一条都打印出来。绝不能静默通过 —— 否则事后
        # 你根本不知道 agent 到底删了什么。留痕比拦截更容易被忽略，但同样重要。
        print("[警告] 已开启 --yes：高危命令将不再询问直接执行。")

        def _auto(cmd: str, reason: str) -> bool:
            print(f"  [自动放行] {reason} → {cmd}")
            return True

        set_confirm_hook(_auto)

    try:
        client = build_client(args.mock, args.script)
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    agent = Agent(client, max_steps=args.max_steps, verbose=False)

    # 把「打印给人看」和「落盘供复盘」串在同一个 handler 上。
    # 主循环不知道、也不需要知道有第二个人在听 —— 它只管 emit。
    printer = agent.print_event if not args.quiet else None
    current: list = [None]

    def _emit(kind: str, payload: dict) -> None:
        if current[0] is not None:
            current[0].record(kind, payload)
        if printer is not None:
            printer(kind, payload)

    agent.on_event = _emit

    def run_one(task: str) -> None:
        if args.trace:
            from .trace import TraceRecorder

            rec = TraceRecorder.new_file(TRACES_DIR, hint=task)
            current[0] = rec
            print(f"[trace] 本次过程会存到 {rec.path}")
        try:
            agent.run(task)
        finally:
            if current[0] is not None:
                current[0].close()
                current[0] = None

    if args.task:
        run_one(args.task)
        return

    print("已进入交互模式，输入任务回车执行；输入 exit 退出。")
    while True:
        try:
            task = input("\n任务> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见。")
            break
        if not task:
            continue
        if task.lower() in ("exit", "quit", "q"):
            break
        run_one(task)
