"""网页界面后端 —— 只用 Python 标准库，不引入 Flask / FastAPI。

为什么不装一个 Web 框架？
    这个项目从头到尾刻意保持「零第三方依赖」（除了模型厂商的 HTTP 客户端）。
    网页层只是给同一个 agent 换一种呈现方式，不该为此拖进来一整套框架。
    而且标准库的 http.server 对这种「一个人用的本地工具」完全够用。

整个文件只做三件事：
    1. 把 agent 跑出来的事件，通过 SSE 实时推给浏览器
    2. 接收浏览器的指令（发任务、停止、确认危险命令）
    3. 提供 workspace 的文件树和内容，供界面右侧浏览

这里有一个必须想清楚的对应关系：
    命令行里，危险命令是靠 input() 阻塞等人敲 y；
    网页里没人敲键盘，于是改成 —— agent 线程发一个 confirm 事件后阻塞在队列上，
    等浏览器 POST 回「允许/拒绝」。逻辑没变，只是把「人」从终端换成了按钮。
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .agent import Agent
from .cli import build_client
from .config import WORKSPACE_DIR, ensure_workspace
from .tools import set_confirm_hook

WEB_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web"
)
# 等人点「允许/拒绝」的最长时间。超时按「不执行」处理 ——
# 安全相关的默认值永远选保守的那个，拿不到回答就别动。
CONFIRM_TIMEOUT = 180
IGNORE_DIRS = {"__pycache__", "node_modules", ".git", ".venv", "venv", ".idea"}


class Session:
    """一次服务的共享状态：事件订阅者、确认队列、当前在跑的 agent。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.subscribers: list[queue.Queue] = []
        self.confirm_queue: queue.Queue = queue.Queue()
        self.agent: Agent | None = None
        self.busy = False

    # ---- 事件广播 ----

    def subscribe(self) -> queue.Queue:
        """每个 SSE 连接一个自己的队列（刷新页面也能收到完整事件）。"""
        q: queue.Queue = queue.Queue()
        with self.lock:
            self.subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.lock:
            if q in self.subscribers:
                self.subscribers.remove(q)

    def emit(self, kind: str, **payload) -> None:
        item = dict(payload)
        item["type"] = kind
        item["ts"] = time.time()
        with self.lock:
            subs = list(self.subscribers)
        for q in subs:
            q.put(item)

    # ---- 危险命令确认 ----

    def ask_confirm(self, command: str, reason: str) -> bool:
        """在 agent 线程里被调用：弹给浏览器，然后阻塞等回答。"""
        self.emit("confirm", command=command, reason=reason)
        try:
            return bool(self.confirm_queue.get(timeout=CONFIRM_TIMEOUT))
        except queue.Empty:
            self.emit("notice", message="等待确认超时，已按「不执行」处理。")
            return False


def _run_task(session: Session, task: str, use_mock: bool, script: str, max_steps: int) -> None:
    """在后台线程里跑一个任务，把全过程变成事件流。"""
    try:
        client = build_client(use_mock, script, announce=False)
    except RuntimeError as e:
        session.emit("error", message=str(e))
        session.emit("end", ok=False)
        session.busy = False
        return

    mode = f"假模型（剧本 {script}）" if use_mock else f"真实模型 {client.model}"
    session.emit("mode", mode=mode, mock=use_mock)

    agent = Agent(
        client,
        max_steps=max_steps,
        verbose=False,
        on_event=lambda kind, payload: session.emit(kind, **payload),
    )
    session.agent = agent
    try:
        agent.run(task)
    except Exception as e:  # 兜底：任何意外都要告诉界面，而不是让线程悄悄死掉
        session.emit("error", message=f"{type(e).__name__}: {e}")
    finally:
        session.agent = None
        session.busy = False
        session.emit("end", ok=True)


# ------------------------------------------------------------------ 文件浏览


def _file_tree(max_depth: int = 3) -> dict:
    """列出 workspace 的文件树，供界面侧栏显示。"""
    base = os.path.abspath(WORKSPACE_DIR)

    def walk(abs_dir: str, depth: int) -> list:
        if depth > max_depth:
            return []
        items = []
        try:
            entries = sorted(os.listdir(abs_dir))
        except OSError:
            return []
        for name in entries:
            if name.startswith(".") or name in IGNORE_DIRS:
                continue
            full = os.path.join(abs_dir, name)
            rel = os.path.relpath(full, base).replace("\\", "/")
            if os.path.isdir(full):
                items.append({
                    "name": name, "path": rel, "type": "dir",
                    "children": walk(full, depth + 1),
                })
            else:
                try:
                    size = os.path.getsize(full)
                except OSError:
                    size = 0
                items.append({"name": name, "path": rel, "type": "file", "size": size})
        return items

    return {"name": "workspace", "path": ".", "type": "dir", "children": walk(base, 1)}


# ------------------------------------------------------------------ HTTP


class Handler(BaseHTTPRequestHandler):
    session: Session = None  # 由 serve() 注入

    def log_message(self, fmt, *args):  # 静音：默认会把每条请求打到终端，很吵
        pass

    # ---- 工具方法 ----

    def _send_json(self, data, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, rel_path: str) -> None:
        # 只服务 web/ 目录里的文件，且把 ../ 挡在外面
        safe = os.path.normpath(os.path.join(WEB_DIR, rel_path.lstrip("/")))
        if not safe.startswith(WEB_DIR) or not os.path.isfile(safe):
            self.send_error(404)
            return
        ext = os.path.splitext(safe)[1].lower()
        ctype = {".html": "text/html; charset=utf-8",
                 ".css": "text/css; charset=utf-8",
                 ".js": "application/javascript; charset=utf-8"}.get(ext, "application/octet-stream")
        with open(safe, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    # ---- GET ----

    def do_GET(self):
        url = urlparse(self.path)
        path, qs = url.path, parse_qs(url.query)

        if path in ("/", "/index.html"):
            self._send_file("index.html")
        elif path in ("/style.css", "/app.js"):
            self._send_file(path.lstrip("/"))
        elif path == "/api/events":
            self._stream_events()
        elif path == "/api/files":
            self._send_json(_file_tree())
        elif path == "/api/file":
            self._send_file_content((qs.get("path") or [""])[0])
        else:
            self.send_error(404)

    def _send_file_content(self, rel: str) -> None:
        if not rel:
            self._send_json({"error": "缺少 path 参数"}, 400)
            return
        base = os.path.abspath(WORKSPACE_DIR)
        target = os.path.abspath(os.path.join(base, rel))
        if not (target == base or target.startswith(base + os.sep)):
            self._send_json({"error": "路径越界"}, 400)
            return
        if not os.path.isfile(target):
            self._send_json({"error": "文件不存在"}, 404)
            return
        try:
            with open(target, "r", encoding="utf-8", errors="replace") as f:
                self._send_json({"path": rel, "content": f.read()})
        except OSError as e:
            self._send_json({"error": str(e)}, 500)

    def _stream_events(self) -> None:
        """SSE：把 session 里的事件一条条推给浏览器。"""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")  # 防止中间层缓冲导致不实时
        self.end_headers()

        q = self.session.subscribe()
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    item = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")  # 心跳，防止连接被判定为闲置
                    self.wfile.flush()
                    continue
                data = json.dumps(item, ensure_ascii=False)
                self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # 浏览器关掉了页面，正常
        finally:
            self.session.unsubscribe(q)

    # ---- POST ----

    def do_POST(self):
        path = urlparse(self.path).path
        body = self._read_body()

        if path == "/api/task":
            self._post_task(body)
        elif path == "/api/confirm":
            self.session.confirm_queue.put(bool(body.get("allow")))
            self._send_json({"ok": True})
        elif path == "/api/stop":
            if self.session.agent:
                self.session.agent.request_stop()
            self._send_json({"ok": True})
        else:
            self.send_error(404)

    def _post_task(self, body: dict) -> None:
        task = (body.get("task") or "").strip()
        if not task:
            self._send_json({"error": "任务内容为空"}, 400)
            return
        if self.session.busy:
            self._send_json({"error": "agent 正在忙，等它跑完或点停止"}, 409)
            return

        use_mock = bool(body.get("mock"))
        script = body.get("script") or "fix"
        try:
            max_steps = int(body.get("max_steps") or 20)
        except (TypeError, ValueError):
            max_steps = 20
        max_steps = max(1, min(max_steps, 100))

        self.session.busy = True
        threading.Thread(
            target=_run_task,
            args=(self.session, task, use_mock, script, max_steps),
            daemon=True,  # 主线程退出时不必等它
        ).start()
        self._send_json({"ok": True})


def serve(port: int = 8000, use_mock: bool = False, script: str = "fix",
          max_steps: int = 20, auto_yes: bool = False, open_browser: bool = True) -> None:
    """启动网页服务。"""
    try:
        import sys
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    ensure_workspace()
    session = Session()

    if auto_yes:
        def _auto(cmd, reason):
            session.emit("notice", message=f"[auto_yes] {reason} → {cmd}")
            return True
        set_confirm_hook(_auto)
    else:
        set_confirm_hook(session.ask_confirm)

    Handler.session = session

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    httpd = Server(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}"
    print("=" * 56)
    print("  mini coding agent —— 网页界面")
    print(f"  地址：{url}")
    print("  停止：在这个窗口按 Ctrl+C")
    print("=" * 56)
    print("提示：网页只是给同一个 agent 换了个界面，主循环、工具、安全策略完全共用。")

    if open_browser:
        try:
            threading.Timer(0.8, lambda: webbrowser.open(url)).start()
        except Exception:
            pass

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
