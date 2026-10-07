"""任务过程落盘与离线统计（可观测性）。

主循环已经把每一步 emit 成事件，但事件打印完、渲染完就没了。落盘之后多出三件事：

1. **事后复盘**：某个任务为什么失败，把事件流重放一遍就清楚了，
   不用靠记忆猜"它当时好像是这么做的"。
2. **量化评测**：跑一批任务，统计成功率 / 平均步数 / 平均 token。
   "我做了个 agent" 谁都能说，"在 12 个任务上成功率 75%、平均 6.3 步" 才有说服力。
3. **回归对比**：改了提示词或工具之后，跑同一批任务看数字有没有变好，
   而不是凭感觉判断"好像快了一点"。

为什么用 JSON Lines（每行一条事件）而不是一个大 JSON 数组？
    追加写的时候不用先读出来、不用补括号，写到一半崩了也不损坏已有内容；
    而且任何语言都能一行行读。这是日志文件的事实标准。
"""

from __future__ import annotations

import json
import os
import re
import time

# 四种结局，按「越好越靠前」排。一个任务只会命中其中一种。
OUTCOMES = ("done", "max_steps", "stopped", "error")


def _looks_failed(result: str) -> bool:
    """判断一次工具调用算不算失败。

    判据跟前端高亮用的是同一套：错误文本、被取消、或者退出码非 0。
    这个数字有意义 —— 它能说明 agent 有没有真的在「失败后换方案」，
    而不是一路顺风顺水跑完。
    """
    text = str(result or "")
    if text.startswith("错误") or text.startswith("已取消"):
        return True
    m = re.search(r"\[退出码 (\d+)\]", text)
    return bool(m and m.group(1) != "0")


class TraceRecorder:
    """把一个任务的事件流写成 jsonl 文件。"""

    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._f = open(path, "w", encoding="utf-8")

    def record(self, kind: str, payload: dict) -> None:
        item = dict(payload or {})
        item["type"] = kind
        item["ts"] = time.time()
        self._f.write(json.dumps(item, ensure_ascii=False) + "\n")
        self._f.flush()  # 任务是长跑的，崩了也要保住已经发生的部分

    def close(self) -> None:
        try:
            self._f.close()
        except Exception:  # noqa: BLE001 —— 关文件失败不该影响任务结果
            pass

    def __enter__(self) -> "TraceRecorder":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @classmethod
    def new_file(cls, directory: str, hint: str = "") -> "TraceRecorder":
        """按时间自动生成文件名，并保证不覆盖已有记录。

        hint 只保留 ASCII（中文任务名在这里会变成空）—— 文件名交给 ASCII 更稳，
        任务原文反正已经写进文件里的 start 事件了。
        时间戳只到秒，所以同名时必须再补一个序号，否则一秒内跑两次就会互相覆盖
        （这个坑是实测踩到的：两条中文任务只剩一条）。
        """
        ts = time.strftime("%Y%m%d-%H%M%S")
        slug = re.sub(r"[^0-9A-Za-z]+", "-", hint or "").strip("-")[:24] or "task"
        base = f"{ts}-{slug}"
        name = base + ".jsonl"
        n = 2
        while os.path.exists(os.path.join(directory, name)):
            name = f"{base}-{n}.jsonl"
            n += 1
        return cls(os.path.join(directory, name))


def load_events(path: str) -> list[dict]:
    """把一个 trace 文件读回成事件列表（也就是「回放」）。"""
    events = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # 写到一半被中断时最后一行可能不完整，跳过
    return events


def summarize(events: list[dict], name: str = "") -> dict:
    """把一个任务的事件流压成一行统计。"""
    kinds = [e.get("type") for e in events]
    outcome = next((k for k in OUTCOMES if k in kinds), "unknown")

    start = next((e for e in events if e.get("type") == "start"), {})
    usage = {}
    for e in events:
        if e.get("type") in ("done", "max_steps", "stopped"):
            if isinstance(e.get("usage"), dict):
                usage = e["usage"]

    calls = [e for e in events if e.get("type") == "tool_call"]
    results = [e for e in events if e.get("type") == "tool_result"]

    return {
        "file": name,
        "task": str(start.get("task", ""))[:40],
        "outcome": outcome,
        "steps": sum(1 for k in kinds if k == "step"),
        "tool_calls": len(calls),
        "tool_failures": sum(1 for e in results if _looks_failed(e.get("result"))),
        "tools": sorted({str(e.get("name")) for e in calls}),
        "prompt_tokens": usage.get("prompt", 0),
        "completion_tokens": usage.get("completion", 0),
        "total_tokens": usage.get("total", 0),
        "requests": usage.get("requests", 0),
        "retries": usage.get("retries", 0),
        "compactions": sum(e.get("rounds", 0) for e in events if e.get("type") == "compact"),
    }


def _mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 1) if values else 0.0


def report(directory: str) -> str:
    """读一个目录里所有 trace，输出一份统计报告（文本）。"""
    if not os.path.isdir(directory):
        return f"还没有任何记录：{directory} 不存在。先跑 `python main.py --trace \"任务\"` 攒几条。"

    files = sorted(
        f for f in os.listdir(directory) if f.endswith(".jsonl")
    )
    if not files:
        return f"{directory} 里没有 .jsonl 记录。先跑 `python main.py --trace \"任务\"` 攒几条。"

    rows = [summarize(load_events(os.path.join(directory, f)), f) for f in files]

    lines = []
    lines.append("=" * 68)
    lines.append(f"  trace 报告：{directory}（{len(rows)} 个任务）")
    lines.append("=" * 68)

    # 结局分布
    dist = {o: sum(1 for r in rows if r["outcome"] == o) for o in OUTCOMES + ("unknown",)}
    total = len(rows)
    done = dist.get("done", 0)
    lines.append("")
    lines.append("【结局分布】")
    label = {
        "done": "正常完成",
        "max_steps": "达到步数上限（可能打转）",
        "stopped": "被手动停止",
        "error": "出错中断",
        "unknown": "记录不完整",
    }
    for o in OUTCOMES + ("unknown",):
        if dist.get(o):
            pct = dist[o] * 100.0 / total
            lines.append(f"  {label[o]:<22} {dist[o]:>3} 个  {pct:>5.1f}%")
    lines.append("")
    lines.append(f"  成功率（正常完成）：{done}/{total} = {done * 100.0 / total:.1f}%")

    # 平均量
    lines.append("")
    lines.append("【平均每个任务】")
    lines.append(f"  步数            {_mean([r['steps'] for r in rows]):>8.1f}")
    lines.append(f"  工具调用        {_mean([r['tool_calls'] for r in rows]):>8.1f}")
    lines.append(f"  其中失败        {_mean([r['tool_failures'] for r in rows]):>8.1f}")
    lines.append(f"  请求次数        {_mean([r['requests'] for r in rows]):>8.1f}")
    lines.append(f"  重试次数        {_mean([r['retries'] for r in rows]):>8.1f}")
    lines.append(f"  上下文压缩      {_mean([r['compactions'] for r in rows]):>8.1f}")

    tok = [(r["prompt_tokens"], r["completion_tokens"], r["total_tokens"]) for r in rows
           if r["total_tokens"]]
    if tok:
        lines.append("")
        lines.append("【token（只统计有 usage 的真实模型任务）】")
        lines.append(f"  输入            {_mean([t[0] for t in tok]):>8.1f}")
        lines.append(f"  输出            {_mean([t[1] for t in tok]):>8.1f}")
        lines.append(f"  合计            {_mean([t[2] for t in tok]):>8.1f}")
        p_in = sum(t[0] for t in tok)
        p_out = sum(t[1] for t in tok)
        if p_out:
            lines.append(f"  输入:输出 ≈     {p_in / p_out:>8.1f} : 1")
            lines.append("  （这个比值就是「上下文管理为什么重要」最直接的证据）")

    # 工具使用分布
    used: dict[str, int] = {}
    for r in rows:
        for name in r["tools"]:
            used[name] = used.get(name, 0) + 1
    if used:
        lines.append("")
        lines.append("【工具被用到的任务数】")
        for name, n in sorted(used.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {name:<16} {n:>3} 个任务用过")

    # 逐个任务
    lines.append("")
    lines.append("【逐条】")
    for r in rows:
        lines.append(
            f"  {r['outcome']:<10} {r['steps']:>2} 步  "
            f"{r['tool_calls']:>2} 次调用（失败 {r['tool_failures']}）  {r['task']}"
        )
    lines.append("")
    return "\n".join(lines)
