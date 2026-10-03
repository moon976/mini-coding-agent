"""配置层：所有凭据只从环境变量 / .env 文件读取，绝不写死在代码里。

为什么单独放一个文件？
    凭据是唯一的敏感信息，集中在一处，方便你确认「它没有被提交到仓库」。
    .gitignore 里已经排除了 .env，你只要不手动把它加进 git 就安全。
"""

import os

# 项目根目录（coding-agent/）
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# agent 只能在这个目录里读写文件，防止它误改你电脑上的其它文件
WORKSPACE_DIR = os.path.join(ROOT_DIR, "workspace")

# 对话历史的字符预算。超过就会触发压缩（见 context.py）。
# 为什么用「字符数」而不是「token 数」？
#   精确算 token 需要额外的分词库，而我们只需要一个「够用就行」的粗略阈值；
#   中文大约 1 字 ≈ 1 token，英文 1 token ≈ 4 字符，这个数设得保守一点就够。
MAX_CONTEXT_CHARS = 20000

# 危险命令黑名单：命中就直接拒绝执行（很粗糙，但聊胜于无）
COMMAND_BLACKLIST = ("rm -rf /", "format c:", "shutdown", "del /f /s /q c:")


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
