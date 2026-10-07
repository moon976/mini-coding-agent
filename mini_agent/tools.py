"""工具层：定义工具（给模型看的说明书）+ 本地执行（真正干活的代码）。

三条最重要的设计决定，面试时你一定会用到：

1. 每个工具有两样东西：
   - JSON Schema：告诉模型「这个工具叫什么、要什么参数」（TOOL_SCHEMAS）
   - Python 函数：真正在你电脑上干活（TOOL_FUNCTIONS）
   模型只看到前者，永远碰不到后者 —— 工具是「我们替它执行」的。

2. 工具绝不把异常抛给主循环，所有失败都转成一段错误文本。
   这样模型能看到报错、自己改方案重试 —— 这就是 agent 能「自我修复」的原因。
   如果直接抛异常让程序崩掉，模型就再也没有改正的机会了。

3. 所有文件操作都被限制在 workspace/ 目录内（_safe_path），防止越界读写。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

from .config import (
    COMMAND_BLACKLIST,
    DANGEROUS_PATTERNS,
    WORKSPACE_DIR,
    ensure_workspace,
    normalize_command,
)

# 工具输出的最大字符数。模型上下文有限，超长输出会把对话撑爆。
MAX_OUTPUT_CHARS = 8000


def _clip(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    """截断过长的输出，并告诉模型被截断了（不要悄悄截断，否则模型会误判）。"""
    text = str(text)
    if len(text) > limit:
        return text[:limit] + f"\n...[输出过长，已截断 {len(text) - limit} 个字符]"
    return text


def _safe_path(rel_path: str) -> str:
    """把相对路径解析成 workspace 内的绝对路径，越界就报错。"""
    base = os.path.abspath(WORKSPACE_DIR)
    target = os.path.abspath(os.path.join(base, rel_path))
    if not (target == base or target.startswith(base + os.sep)):
        raise ValueError(f"路径越界：{rel_path} 不在工作目录内")
    return target


# ---------------------------------------------------------------- 具体工具


def tool_list_dir(path: str = ".") -> str:
    """列出目录内容，用来让 agent 先搞清楚项目里有什么。"""
    target = _safe_path(path)
    if not os.path.isdir(target):
        return f"错误：{path} 不是一个目录"
    # 这些目录和我们要做的事无关，混进来只会干扰模型判断「某个文件到底存不存在」
    IGNORE_DIRS = {"__pycache__", "node_modules", ".git", ".venv", "venv"}

    lines = []
    for root, dirs, files in os.walk(target):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in IGNORE_DIRS]
        depth = root[len(target):].count(os.sep)
        indent = "  " * depth
        lines.append(f"{indent}{os.path.basename(root) or '.'}/")
        for f in sorted(files):
            if f.startswith("."):
                continue
            size = os.path.getsize(os.path.join(root, f))
            lines.append(f"{indent}  {f}  ({size} B)")
        if depth >= 2:  # 最多往下看两层，避免刷屏
            break
    return "\n".join(lines) if lines else "[空目录]"


def tool_read_file(path: str) -> str:
    """读取文本文件内容。"""
    target = _safe_path(path)
    if not os.path.exists(target):
        return f"错误：文件不存在 {path}"
    if not os.path.isfile(target):
        return f"错误：{path} 不是文件"
    try:
        with open(target, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError as e:
        return f"错误：读取失败 {e}"
    numbered = "\n".join(f"{i:4d}| {line}" for i, line in enumerate(content.splitlines(), 1))
    return numbered or "[空文件]"


def tool_write_file(path: str, content: str) -> str:
    """写入文件（整文件覆盖）。第一阶段只做整文件覆盖，后面再升级成精确编辑。"""
    target = _safe_path(path)
    os.makedirs(os.path.dirname(target) or WORKSPACE_DIR, exist_ok=True)
    existed = os.path.exists(target)
    try:
        with open(target, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
    except OSError as e:
        return f"错误：写入失败 {e}"
    action = "已覆盖" if existed else "已新建"
    return f"{action}文件 {path}，共 {len(content)} 个字符"


def tool_edit_file(path: str, old_text: str, new_text: str) -> str:
    """精确编辑：把文件里【第一次出现】的 old_text 替换成 new_text。

    为什么需要它，而不是一律用 write_file 整个重写？
      1. 省钱：改一行也要把 300 行文件重发一遍，token 全浪费在没用的重复内容上。
      2. 安全：整文件重写一旦模型记岔了，就会悄悄弄丢你没让它动的代码。
      3. 可核对：返回改动前后的对照，人一眼能看出它改了什么。

    两个刻意的严格之处（面试会问）：
      - old_text 找不到 → 不猜、不近似匹配，直接报错让它重读文件。
      - old_text 出现多次 → 拒绝执行。否则它想改第 3 处，结果改掉了第 1 处。
    """
    target = _safe_path(path)
    if not os.path.exists(target):
        return f"错误：文件不存在 {path}"
    try:
        with open(target, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError as e:
        return f"错误：读取失败 {e}"

    count = content.count(old_text)
    if count == 0:
        return (
            f"错误：在 {path} 里找不到这段文字，没有任何改动。\n"
            f"要找的是：{old_text[:200]!r}\n"
            f"可能原因：文件已被改过、缩进/空格不一致、或你看的是旧内容。请重新 read_file 确认现状。"
        )
    if count > 1:
        return (
            f"错误：这段文字在 {path} 里出现了 {count} 次，无法确定改哪一处，已放弃。\n"
            f"请把 old_text 写得更长一些，带上能唯一确定位置的上下文。"
        )

    new_content = content.replace(old_text, new_text, 1)
    try:
        with open(target, "w", encoding="utf-8", newline="\n") as f:
            f.write(new_content)
    except OSError as e:
        return f"错误：写入失败 {e}"

    diff = (
        f"已修改 {path}（1 处）\n"
        f"  - {old_text.strip()[:120]}\n"
        f"  + {new_text.strip()[:120]}"
    )
    return diff


def _decode_output(data: bytes) -> str:
    """把命令的原始输出解成我们能看懂的文字。

    为什么要折腾？Windows 中文系统的 cmd 默认用 GBK 输出，
    而我们之前写死了按 UTF-8 解码 —— 结果模型收到的报错全是「锟斤拷」，
    它看不懂就只能靠猜，于是乱试命令、白白多烧好几步。
    这里的做法是按可能性依次尝试，总比直接乱码好。
    """
    if not data:
        return ""
    for enc in ("utf-8", "gbk", "cp936", "mbcs" if os.name == "nt" else "latin-1"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def _subprocess_env() -> dict:
    """构造子进程的环境变量：把「当前这个 Python 解释器」所在目录插到 PATH 最前面。

    为什么要多此一举？（这是一个在 Windows 上真实踩到的坑）
    PATH 里常常排着一个微软商店的 python.exe 占位程序，它不是真 Python。
    于是 agent 执行 `python hello.py` 时系统会先找到那个假的，
    结果什么都没跑，只返回一个「退出码 9009」（Windows 的「找不到命令」）。
    把我们自己的解释器目录放到 PATH 最前面，子进程就能找到真正的 python。
    效果等价于：给 agent 自动激活了它自己的运行环境。
    """
    env = os.environ.copy()
    py_dir = os.path.dirname(sys.executable)
    env["PATH"] = py_dir + os.pathsep + env.get("PATH", "")
    # 让 Python 子进程也用 UTF-8 输出，减少一层编码麻烦
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    return env


# 确认钩子：由命令行层注入。默认行为是在终端里问一句。
# 做成可注入的回调而不是写死 input()，是为了让这段逻辑可以被单独测试 ——
# 测试时传一个固定返回 True/False 的函数就行，不用真的去按键盘。
_confirm_hook = None


def set_confirm_hook(fn) -> None:
    """注入确认函数。签名：fn(command: str, reason: str) -> bool"""
    global _confirm_hook
    _confirm_hook = fn


def _default_confirm(command: str, reason: str) -> bool:
    """默认的确认方式：在终端问一句，等人回答。

    拿不到回答时（非交互环境、Ctrl+C）一律当作「不执行」。
    安全相关的默认值必须选保守的那个：问不清就别动。
    """
    print(f"\n  ⚠️  这条命令需要确认：{reason}")
    print(f"      {command}")
    try:
        ans = input("      确定执行吗？[y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("      没有收到回答，按「不执行」处理。")
        return False
    return ans in ("y", "yes")


def _danger_reason(command: str) -> str | None:
    """检查命令是否属于高危操作。返回原因，安全则返回 None。"""
    for pattern, reason in DANGEROUS_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return reason
    return None


def tool_run_command(command: str, timeout: int = 30) -> str:
    """在工作目录里执行一条 shell 命令，返回 stdout、stderr 和退出码。

    为什么必须返回退出码？因为很多命令「有输出但其实是失败的」，
    模型只有看到退出码才能判断要不要重试。

    三道安全闸门，从上到下依次收紧：
      1. 黑名单：直接拒绝，连问都不问（针对毁灭性操作）
      2. 高危：停下来问人，人点头才执行
      3. 沙盒：命令的工作目录固定在 workspace/ 内
    """
    # 先把两边都规范化（连续空白压成一个），否则 `rm  -rf  /` 这种多打空格的写法会漏过去
    low = normalize_command(command)
    for bad in COMMAND_BLACKLIST:
        if normalize_command(bad) in low:
            return f"错误：命令被安全策略拒绝（包含危险片段：{bad}）"

    reason = _danger_reason(command)
    if reason:
        hook = _confirm_hook or _default_confirm
        if not hook(command, reason):
            return (
                f"已取消执行：这条命令会【{reason}】，人没有确认。\n"
                f"请换一个更安全的方式达成目的，不要原样重试这条命令。"
            )

    ensure_workspace()
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=WORKSPACE_DIR,
            capture_output=True,          # 拿原始字节，编码交给 _decode_output 处理
            timeout=timeout,
            env=_subprocess_env(),
        )
    except subprocess.TimeoutExpired:
        return f"错误：命令执行超时（超过 {timeout} 秒）"

    parts = []
    stdout = _decode_output(proc.stdout)
    stderr = _decode_output(proc.stderr)
    if stdout:
        parts.append("--- stdout ---\n" + stdout)
    if stderr:
        parts.append("--- stderr ---\n" + stderr)
    parts.append(f"[退出码 {proc.returncode}]")
    return "\n".join(parts)


def tool_search_text(pattern: str, path: str = ".") -> str:
    """在文件里搜索文本（简化版 grep）。"""
    target = _safe_path(path)
    hits = []
    for root, dirs, files in os.walk(target):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in files:
            if f.startswith("."):
                continue
            full = os.path.join(root, f)
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as fh:
                    for i, line in enumerate(fh, 1):
                        if pattern in line:
                            rel = os.path.relpath(full, target)
                            hits.append(f"{rel}:{i}: {line.rstrip()}")
                            if len(hits) >= 30:
                                return "\n".join(hits) + "\n...[结果过多，已截断]"
            except OSError:
                continue
    return "\n".join(hits) if hits else f"没有找到包含 {pattern!r} 的内容"


# ---------------------------------------------------------------- 注册表

TOOL_FUNCTIONS = {
    "list_dir": tool_list_dir,
    "read_file": tool_read_file,
    "write_file": tool_write_file,
    "edit_file": tool_edit_file,
    "run_command": tool_run_command,
    "search_text": tool_search_text,
}

# 给模型看的「工具说明书」。description 写得越清楚，模型用得越准。
TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "列出工作目录中的文件和子目录，用来了解项目结构。",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "相对路径，默认 '.'"}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取文件内容，带行号。修改文件前必须先读。",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "相对路径，如 'hello.py'"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "把完整内容写入文件（会覆盖原有内容）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对路径"},
                    "content": {"type": "string", "description": "文件的完整内容"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "精确修改文件：把已有的 old_text 替换成 new_text。"
                "【修改已有文件时优先用它】，不要用 write_file 整个重写，既浪费又容易弄丢内容。"
                "old_text 必须与文件中的内容【完全一致】，包括缩进和空格；"
                "且在文件中只能出现一次，否则会拒绝执行。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对路径"},
                    "old_text": {"type": "string", "description": "要被替换的原文，需原文照抄且唯一"},
                    "new_text": {"type": "string", "description": "替换后的新内容"},
                },
                "required": ["path", "old_text", "new_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": (
                "执行一条 shell 命令（运行脚本、跑测试等）。"
                "注意：命令的工作目录【已经是 workspace】，"
                "不要再加 cd workspace 之类的切换目录语句，直接用相对路径操作文件即可。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的命令"},
                    "timeout": {"type": "integer", "description": "超时秒数，默认 30"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_text",
            "description": "在文件里搜索指定文本，返回文件名和行号。",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "要搜索的文本"},
                    "path": {"type": "string", "description": "搜索起点，默认 '.'"},
                },
                "required": ["pattern"],
            },
        },
    },
]


def execute_tool(name: str, arguments_json: str) -> str:
    """统一入口：解析参数 → 执行 → 返回字符串。无论出什么错，都不抛异常。"""
    if name not in TOOL_FUNCTIONS:
        return f"错误：没有名为 '{name}' 的工具。可用工具：{', '.join(TOOL_FUNCTIONS)}"
    try:
        kwargs = json.loads(arguments_json or "{}")
    except json.JSONDecodeError as e:
        return f"错误：参数不是合法 JSON（{e}）。请重新调用并给出合法 JSON。"
    if not isinstance(kwargs, dict):
        return "错误：参数必须是一个 JSON 对象。"

    try:
        result = TOOL_FUNCTIONS[name](**kwargs)
    except TypeError as e:
        return f"错误：调用 {name} 的参数不对（{e}）。请检查参数名和类型后重试。"
    except ValueError as e:      # 主要是路径越界
        return f"错误：{e}"
    except Exception as e:       # 兜底：任何意外都变成模型能看懂的文字
        return f"错误：执行 {name} 时发生 {type(e).__name__}: {e}"

    return _clip(result)
