"""命令行入口：把各层拼起来，并提供两种运行模式。

    python main.py --mock                 不需要 key，用假模型跑一遍完整循环
    python main.py "帮我修好 hello.py"     用真实模型
"""

from __future__ import annotations

import argparse
import os
import sys

from .agent import Agent
from .config import load_settings
from .llm import MockClient, OpenAICompatClient


def build_client(use_mock: bool, script: str = "fix"):
    if use_mock:
        print(f"[模式] 假模型（剧本：{script}）：不需要 API key，用来验证主循环本身是否正常。")
        return MockClient(script)

    s = load_settings()
    if not s["api_key"]:
        print(
            "没有读到 API key。请先复制 .env.example 为 .env 并填入你的 key，\n"
            "或者先跑 `python main.py --mock` 看看循环是怎么转的。",
            file=sys.stderr,
        )
        sys.exit(1)
    if not s["model"]:
        print("没有读到模型名，请在 .env 里设置 LLM_MODEL。", file=sys.stderr)
        sys.exit(1)

    print(f"[模式] 真实模型：{s['model']}  @ {s['base_url'] or '默认地址'}  (temperature={s['temperature']})")
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
    args = parser.parse_args()

    client = build_client(args.mock, args.script)
    agent = Agent(client, max_steps=args.max_steps, verbose=not args.quiet)

    if args.task:
        agent.run(args.task)
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
        agent.run(task)
