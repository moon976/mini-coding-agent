"""配置层：所有凭据只从环境变量 / .env 文件读取，绝不写死在代码里。

为什么单独放一个文件？
    凭据是唯一的敏感信息，集中在一处，方便你确认「它没有被提交到仓库」。
    .gitignore 里已经排除了 .env，你只要不手动把它加进 git 就安全。
"""

import os
import re

# 项目根目录（coding-agent/）
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# agent 只能在这个目录里读写文件，防止它误改你电脑上的其它文件
WORKSPACE_DIR = os.path.join(ROOT_DIR, "workspace")
# 任务过程落盘的目录（traces/*.jsonl），见 trace.py。里面是运行产物，不进仓库。
TRACES_DIR = os.path.join(ROOT_DIR, "traces")

# 对话历史的预算。超过就会触发压缩（见 context.py）。
#
# 这里用 **token** 而不是字符，因为字符数对中文是系统性失真的：
#   英文大约 4 字符 = 1 token，中文常常 1 字就要 1 token。
#   按字符算，中文任务会过早压缩（白白丢信息），英文任务会过晚压缩（请求超限报错）。
#
# 精确算 token 需要分词库，而我们要保持零第三方依赖 —— 所以用 tokens.py 里的
# TokenEstimator：起点是个粗略猜测，跑过两轮用真实 usage 自动校准。
# 预算留足余量（deepseek-chat 标称 64K 上下文，这里只给到 24K）：
# 压缩的代价是丢一点信息，超上限的代价是整个请求失败，不划算。
MAX_CONTEXT_TOKENS = 24000
# 校准前的初始猜测：约 2.5 个字符算 1 个 token（中英混合的折中）。
CHARS_PER_TOKEN_HINT = 2.5

# 第一层：绝对黑名单。命中直接拒绝，连问都不问。
#
# 比对前必须先用 normalize_command() 把两边的连续空白压成一个 ——
# 否则 `rm  -rf  /`（中间多打一个空格）就绕过去了：子串匹配怕空白变化。
# 这里不用正则，是因为「加了 $ 锚点会让匹配范围悄悄变窄」这类陷阱更容易发生，
# 而黑名单要的是最大覆盖；规范化之后再比对，两种写法的优点就都拿到了。
COMMAND_BLACKLIST = ("rm -rf /", "format c:", "shutdown", "del /f /s /q c:")


def normalize_command(cmd: str) -> str:
    """把命令里的连续空白压成一个空格，并转小写，供黑名单比对使用。

    实测：`rm  -rf  /` 用子串判据是漏报的，规范化之后才是 `rm -rf /`，能正常命中。
    """
    return re.sub(r"\s+", " ", cmd.strip()).lower()

# 第二层：高危命令。不直接拒绝，但执行前必须让人按一次 y。
#
# 这里的取舍是「宁可误报，不可漏报」：
#   漏报的代价 = 文件没了，找不回来；
#   误报的代价 = 用户按一下 y，几秒钟的事。
# 所以宁可让它多问一次，也不能放过一个真危险的。
#
# 反过来，为什么不干脆全禁掉？因为删文件、回滚 git 本来就是 agent 的正经活，
# 一刀切禁掉它就没法干活了。真正的做法是「让它干，但人得看着」。
DANGEROUS_PATTERNS = (
    (r"\brm\s", "删除文件/目录，不可恢复"),
    (r"\brmdir\b", "删除目录"),
    (r"\bdel\s", "删除文件"),
    (r"\bmkfs\b", "格式化磁盘"),
    (r"git\s+reset\s+--hard", "丢弃本地所有未提交的改动"),
    (r"git\s+clean\b", "删除未跟踪的文件"),
    (r"git\s+push\s+[^\n]*(-f\b|--force)", "强制推送，可能覆盖远程历史"),
    (r">\s*[/\\]?[^>&|\s]+\.(py|json|md|txt|env|yml|yaml)\b", "重定向覆盖文件，原内容会丢失"),
)


def ensure_workspace() -> str:
    """确保工作目录存在，并返回它的路径。"""
    os.makedirs(WORKSPACE_DIR, exist_ok=True)
    return WORKSPACE_DIR


def _read_dotenv(path: str) -> None:
    """自己动手读 .env 文件，把里面的键值对放进环境变量。

    为什么不直接用现成的 python-dotenv 库？
        1. 少一个依赖，别人拿到你的项目不用先装一堆东西就能跑；
        2. 读 .env 本来就是十行代码的事，自己写更可控；
        3. 万一这个库没装上，程序会「静默失败」—— 表现就是读不到 key，
           却没有任何报错，这种 bug 最难查（我们刚踩过）。
    """
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip().strip('"').strip("'")
            # 环境变量优先于 .env：这样在服务器/CI 上可以用环境变量覆盖
            if key and key not in os.environ:
                os.environ[key] = value


def load_settings() -> dict:
    """读取配置。优先级：环境变量 > .env 文件 > 默认值。"""
    _read_dotenv(os.path.join(ROOT_DIR, ".env"))

    return {
        "api_key": os.environ.get("LLM_API_KEY", ""),
        "base_url": os.environ.get("LLM_BASE_URL", ""),
        "model": os.environ.get("LLM_MODEL", ""),
        "temperature": float(os.environ.get("LLM_TEMPERATURE", "0")),
    }
