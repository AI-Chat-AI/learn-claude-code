# Python解释器声明，使用Python3运行
#!/usr/bin/env python3
"""
s08_background_tasks.py - Background Tasks

Run commands in background threads. A notification queue is drained
before each LLM call to deliver results.

    Main thread                Background thread
    +-----------------+        +-----------------+
    | agent loop      |        | task executes   |
    | ...             |        | ...             |
    | [LLM call] <---+------- | enqueue(result) |
    |  ^drain queue   |        +-----------------+
    +-----------------+

    Timeline:
    Agent ----[spawn A]----[spawn B]----[other work]----
                 |              |
                 v              v
              [A runs]      [B runs]        (parallel)
                 |              |
                 +-- notification queue --> [results injected]

Key insight: "Fire and forget -- the agent doesn't block while the command runs."
"""

# 导入os模块，用于操作系统相关功能
import os
# 导入subprocess模块，用于执行shell命令
import subprocess
# 导入threading模块，用于多线程编程
import threading
# 导入uuid模块，用于生成唯一ID
import uuid
# 导入Path类，用于路径操作
from pathlib import Path

# 从anthropic库导入Anthropic客户端
from anthropic import Anthropic
# 从dotenv库导入load_dotenv函数，用于加载环境变量
from dotenv import load_dotenv

# 加载.env文件中的环境变量，override=True表示覆盖已存在的变量
load_dotenv(override=True)

# 如果设置了ANTHROPIC_BASE_URL环境变量
if os.getenv("ANTHROPIC_BASE_URL"):
    # 则移除ANTHROPIC_AUTH_TOKEN（使用base URL时不需要认证令牌）
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

# 设置工作目录为当前目录
WORKDIR = Path.cwd()
# 创建Anthropic客户端，使用环境变量中的base_url
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
# 从环境变量获取要使用的模型ID
MODEL = os.environ["MODEL_ID"]

# 系统提示词：告诉AI它在当前目录工作，使用background_run执行长时间运行的命令
SYSTEM = f"You are a coding agent at {WORKDIR}. Use background_run for long-running commands."


# BackgroundManager类：线程执行 + 通知队列
# -- BackgroundManager: threaded execution + notification queue --
class BackgroundManager:
    # 初始化BackgroundManager
    def __init__(self):
        # 任务字典：task_id -> {status, result, command}
        self.tasks = {}
        # 通知队列：已完成任务的结果
        self._notification_queue = []
        # 线程锁，用于保护共享数据
        self._lock = threading.Lock()

    # 启动后台任务
    def run(self, command: str) -> str:
        """Start a background thread, return task_id immediately."""
        # 生成短UUID作为任务ID
        task_id = str(uuid.uuid4())[:8]
        # 初始化任务状态为running
        self.tasks[task_id] = {"status": "running", "result": None, "command": command}
        # 创建后台线程
        thread = threading.Thread(
            # 目标函数为_execute
            target=self._execute, args=(task_id, command), daemon=True
        )
        # 启动线程
        thread.start()
        # 立即返回任务ID和命令信息
        return f"Background task {task_id} started: {command[:80]}"

    # 执行后台任务（在线程中运行）
    def _execute(self, task_id: str, command: str):
        """Thread target: run subprocess, capture output, push to queue."""
        # 尝试执行命令
        try:
            # 使用subprocess运行命令，shell=True允许shell解释，cwd设置工作目录，timeout=300秒
            r = subprocess.run(
                command, shell=True, cwd=WORKDIR,
                capture_output=True, text=True, timeout=300
            )
            # 合并stdout和stderr，限制50000字符
            output = (r.stdout + r.stderr).strip()[:50000]
            # 状态设为completed
            status = "completed"
        # 如果命令执行超时
        except subprocess.TimeoutExpired:
            # 设置超时错误信息
            output = "Error: Timeout (300s)"
            # 状态设为timeout
            status = "timeout"
        # 捕获其他异常
        except Exception as e:
            # 设置错误信息
            output = f"Error: {e}"
            # 状态设为error
            status = "error"
        # 更新任务状态和结果
        self.tasks[task_id]["status"] = status
        self.tasks[task_id]["result"] = output or "(no output)"
        # 获取锁后添加通知到队列
        with self._lock:
            self._notification_queue.append({
                # 任务ID、状态、命令、结果（限制500字符）
                "task_id": task_id,
                "status": status,
                "command": command[:80],
                "result": (output or "(no output)")[:500],
            })

    # 检查任务状态
    def check(self, task_id: str = None) -> str:
        """Check status of one task or list all."""
        # 如果提供了task_id
        if task_id:
            # 获取任务信息
            t = self.tasks.get(task_id)
            # 如果任务不存在
            if not t:
                # 返回错误信息
                return f"Error: Unknown task {task_id}"
            # 返回任务状态和结果
            return f"[{t['status']}] {t['command'][:60]}\n{t.get('result') or '(running)'}"
        # 否则列出所有任务
        # 初始化行列表
        lines = []
        # 遍历每个任务
        for tid, t in self.tasks.items():
            # 格式化任务信息
            lines.append(f"{tid}: [{t['status']}] {t['command'][:60]}")
        # 如果有任务，返回合并的字符串，否则返回提示信息
        return "\n".join(lines) if lines else "No background tasks."

    # 排出通知队列
    def drain_notifications(self) -> list:
        """Return and clear all pending completion notifications."""
        # 获取锁后取出所有通知并清空队列
        with self._lock:
            # 复制通知列表
            notifs = list(self._notification_queue)
            # 清空原始队列
            self._notification_queue.clear()
        # 返回通知列表
        return notifs


# 创建全局BackgroundManager实例
BG = BackgroundManager()


# 工具实现函数
# -- Tool implementations --
# 定义safe_path函数，确保路径在工作目录内，防止目录遍历攻击
def safe_path(p: str) -> Path:
    # 将相对路径转换为绝对路径
    path = (WORKDIR / p).resolve()
    # 检查路径是否在工作目录内
    if not path.is_relative_to(WORKDIR):
        # 如果路径超出工作目录，抛出异常
        raise ValueError(f"Path escapes workspace: {p}")
    # 返回安全的路径
    return path

# 定义run_bash函数，执行shell命令并返回结果
def run_bash(command: str) -> str:
    # 定义危险命令列表，用于安全检查
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    # 如果命令包含任何危险内容
    if any(d in command for d in dangerous):
        # 返回错误信息，阻止执行
        return "Error: Dangerous command blocked"
    # 尝试执行命令
    try:
        # 使用subprocess运行命令，shell=True允许shell解释，cwd设置工作目录，capture_output捕获输出，timeout=120秒
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=120)
        # 合并stdout和stderr，去除首尾空白
        out = (r.stdout + r.stderr).strip()
        # 如果有输出则返回（限制50000字符），否则返回"(no output)"
        return out[:50000] if out else "(no output)"
    # 如果命令执行超时
    except subprocess.TimeoutExpired:
        # 返回超时错误信息
        return "Error: Timeout (120s)"

# 定义run_read函数，读取文件内容
def run_read(path: str, limit: int = None) -> str:
    # 尝试读取文件
    try:
        # 读取文件内容并按行分割
        lines = safe_path(path).read_text().splitlines()
        # 如果设置了限制且行数超过限制
        if limit and limit < len(lines):
            # 只返回前limit行，并在末尾添加省略信息
            lines = lines[:limit] + [f"... ({len(lines) - limit} more)"]
        # 返回合并后的文本（限制50000字符）
        return "\n".join(lines)[:50000]
    # 捕获所有异常
    except Exception as e:
        # 返回错误信息
        return f"Error: {e}"

# 定义run_write函数，写入文件内容
def run_write(path: str, content: str) -> str:
    # 尝试写入文件
    try:
        # 获取安全路径
        fp = safe_path(path)
        # 创建父目录（如果不存在）
        fp.parent.mkdir(parents=True, exist_ok=True)
        # 写入文件内容
        fp.write_text(content)
        # 返回成功信息
        return f"Wrote {len(content)} bytes"
    # 捕获所有异常
    except Exception as e:
        # 返回错误信息
        return f"Error: {e}"

# 定义run_edit函数，编辑文件内容
def run_edit(path: str, old_text: str, new_text: str) -> str:
    # 尝试编辑文件
    try:
        # 获取安全路径
        fp = safe_path(path)
        # 读取文件内容
        c = fp.read_text()
        # 检查旧文本是否存在于文件中
        if old_text not in c:
            # 如果不存在，返回错误信息
            return f"Error: Text not found in {path}"
        # 替换文本（只替换第一个匹配项）
        fp.write_text(c.replace(old_text, new_text, 1))
        # 返回成功信息
        return f"Edited {path}"
    # 捕获所有异常
    except Exception as e:
        # 返回错误信息
        return f"Error: {e}"


# 工具调度映射表：将工具名称映射到处理函数
TOOL_HANDLERS = {
    # bash工具：执行shell命令（阻塞）
    "bash":             lambda **kw: run_bash(kw["command"]),
    # read_file工具：读取文件
    "read_file":        lambda **kw: run_read(kw["path"], kw.get("limit")),
    # write_file工具：写入文件
    "write_file":       lambda **kw: run_write(kw["path"], kw["content"]),
    # edit_file工具：编辑文件
    "edit_file":        lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    # background_run工具：在后台线程运行命令
    "background_run":   lambda **kw: BG.run(kw["command"]),
    # check_background工具：检查后台任务状态
    "check_background": lambda **kw: BG.check(kw.get("task_id")),
}

# 定义可用的工具列表
TOOLS = [
    # bash工具定义：运行shell命令（阻塞）
    {"name": "bash", "description": "Run a shell command (blocking).",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    # read_file工具定义：读取文件内容
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    # write_file工具定义：写入文件内容
    {"name": "write_file", "description": "Write content to file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    # edit_file工具定义：替换文件中的精确文本
    {"name": "edit_file", "description": "Replace exact text in file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    # background_run工具定义：在后台线程运行命令，立即返回task_id
    {"name": "background_run", "description": "Run command in background thread. Returns task_id immediately.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    # check_background工具定义：检查后台任务状态，省略task_id则列出所有
    {"name": "check_background", "description": "Check background task status. Omit task_id to list all.",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "string"}}}},
]


# 定义agent_loop函数，接收消息历史列表
def agent_loop(messages: list):
    # 无限循环，直到模型不再调用工具
    while True:
        # 排出后台通知并在LLM调用前作为系统消息注入
        # Drain background notifications and inject as system message before LLM call
        # 排出所有待处理的通知
        notifs = BG.drain_notifications()
        # 如果有通知且消息列表不为空
        if notifs and messages:
            # 将通知格式化为文本
            notif_text = "\n".join(
                # 格式化每个通知
                f"[bg:{n['task_id']}] {n['status']}: {n['result']}" for n in notifs
            )
            # 添加包含后台结果的用户消息
            messages.append({"role": "user", "content": f"<background-results>\n{notif_text}\n</background-results>"})
            # 添加确认后台结果收到的助手消息
            messages.append({"role": "assistant", "content": "Noted background results."})
        # 调用Anthropic API创建消息
        response = client.messages.create(
            # 传入模型ID、系统提示词和消息历史
            model=MODEL, system=SYSTEM, messages=messages,
            # 传入工具定义，最大生成token数8000
            tools=TOOLS, max_tokens=8000,
        )
        # 将助手的消息添加到历史
        messages.append({"role": "assistant", "content": response.content})
        # 如果模型没有调用工具，则任务完成
        if response.stop_reason != "tool_use":
            # 退出函数，结束循环
            return
        # 初始化结果列表
        results = []
        # 遍历响应内容中的每个块
        for block in response.content:
            # 如果是工具调用块
            if block.type == "tool_use":
                # 从调度表中获取对应的处理函数
                handler = TOOL_HANDLERS.get(block.name)
                # 尝试调用处理函数
                try:
                    # 调用处理函数，传入参数
                    output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
                # 捕获异常
                except Exception as e:
                    # 返回错误信息
                    output = f"Error: {e}"
                # 打印工具名称和输出结果（截取前200字符）
                print(f"> {block.name}: {str(output)[:200]}")
                # 将工具结果添加到结果列表
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)})
        # 以user角色将工具结果添加到消息历史，继续循环
        messages.append({"role": "user", "content": results})


# 主程序入口
if __name__ == "__main__":
    # 初始化消息历史列表
    history = []
    # 无限循环，等待用户输入
    while True:
        # 读取用户输入（青色提示符）
        try:
            query = input("\033[36ms08 >> \033[0m")
        # 捕获EOF和Ctrl+C异常
        except (EOFError, KeyboardInterrupt):
            # 退出循环
            break
        # 如果输入为q、exit或空字符串
        if query.strip().lower() in ("q", "exit", ""):
            # 退出循环
            break
        # 将用户消息添加到历史
        history.append({"role": "user", "content": query})
        # 调用agent_loop处理任务
        agent_loop(history)
        # 打印空行分隔输出
        print()
