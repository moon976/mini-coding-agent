# mini-coding-agent：从零手写的最小编程智能体

不依赖任何 agent 框架，自己实现「模型要工具 → 本地执行 → 结果回写 → 再问模型」这个循环。

## 快速开始（不需要 API key）

```bash
python main.py --mock "工作目录里的 hello.py 有个 bug，请找出来修好"
```

`--mock` 用的是内置假模型：它按写好的剧本念台词，不联网。**这一步验证的是主循环本身对不对**，跟模型好不好无关——模型是变量，先把它钉死。

其它两个剧本：

```bash
python main.py --mock --script retry "修好 hello.py"        # 演示失败后换方案重试
python main.py --mock --script loop --max-steps 3 "修好 hello.py"  # 演示步数上限这个刹车
```

## 接上真实模型

1. 复制 `.env.example` 为 `.env`，填入 `LLM_API_KEY`（DeepSeek / 任意 OpenAI 兼容网关均可）。
2. `python main.py "你的任务"`

首次运行需要 `pip install openai`。`.env` 已在 `.gitignore` 中，不会进仓库。
**key 一旦泄露到仓库或视频里，立刻去厂商后台作废重发。**

## 目录结构

```
coding-agent/
├── main.py              入口
├── mini_agent/
│   ├── config.py        配置与凭据（唯一碰敏感信息的地方）
│   ├── tools.py         工具：JSON Schema 说明书 + 本地真正执行
│   ├── llm.py           模型客户端（真实 / 假模型，返回同一种结构）
│   ├── agent.py         主循环：判断、执行、终止  ← 心脏
│   ├── context.py       对话历史管理与超限压缩
│   └── cli.py           命令行
└── workspace/           agent 唯一能读写文件的目录
```

## 工具

| 工具 | 作用 |
|---|---|
| list_dir | 列目录（自动过滤 `__pycache__` 等噪音） |
| read_file | 读文件，带行号 |
| edit_file | **精确替换**：只改要改的那一段，找不到或匹配到多处就拒绝 |
| write_file | 整文件写入（新建文件用） |
| run_command | 执行命令，返回 stdout / stderr / 退出码，带超时 |
| search_text | 文本搜索（简化 grep） |

## 四条核心设计（也是这个项目真正的内容）

1. **工具出错不抛异常，而是把错误变成一段文字回传给模型。**
   程序崩了，模型就再没机会改正；错误回到对话里，它才能换方案重试。这是 agent 和「脚本」的根本区别。
2. **所有文件操作锁死在 `workspace/` 内**（`_safe_path`），越界直接报错。
3. **循环有两个出口**：模型不再要工具（正常结束）、达到最大步数（防死循环烧钱）。缺一不可。
4. **命令执行有三道闸门**：黑名单直接拒绝 → 高危命令停下来问人 → 沙盒限定工作目录。

## 安全机制

agent 是在你电脑上真跑命令的，所以「它能不能删我文件」必须想清楚。三道闸门：

| 层级 | 做法 | 例子 |
|---|---|---|
| 黑名单 | 直接拒绝，连问都不问 | `rm -rf /`、`format c:`、`shutdown` |
| 高危确认 | 打印出来，等你按 y | `rm x.py`、`git reset --hard`、`git push --force`、`echo x > a.py` |
| 沙盒 | 工作目录锁死在 `workspace/` | 所有文件读写都过 `_safe_path` |

取舍原则是**宁可误报，不可漏报**：漏报的代价是文件没了，误报只是多按一下 y。
但也不能误伤到没法干活——`python xxx.py`、`python -m unittest` 这类必须直接放行。

确认钩子做成可注入的（`set_confirm_hook`），测试时传个固定返回值就行，不用真按键盘。
**拿不到回答时一律当作「不执行」**：安全相关的默认值，必须选保守的那个。

无人值守时可加 `--yes` 自动放行，但每条都会打印出来——绝不能静默通过，否则事后你根本不知道它删了什么。

```bash
python main.py "删掉 workspace 里所有 .pyc 文件"      # 会停下来问你
python main.py --yes "同样的任务"                      # 自动放行，但会打印
```

## 还没做的

- 长任务的分级摘要（目前超限是丢弃最早的整组消息，会丢细节）
- 会话持久化（退出后再进来，上下文就没了）
- 按 token 而不是字符数来估算上下文预算
