# Python解释器声明，使用Python3运行
#!/usr/bin/env python3
"""
s07_task_system.py - Tasks

Tasks persist as JSON files in .tasks/ so they survive context compression.
Each task has a dependency graph (blockedBy/blocks).

    .tasks/
      task_1.json  {"id":1, "subject":"...", "status":"completed", ...}
      task_2.json  {"id":2, "blockedBy":[1], "status":"pending", ...}
      task_3.json  {"id":3, "blockedBy":[2], "blocks":[], ...}

    Dependency resolution:
    +----------+     +----------+     +----------+
    | task 1   | --> | task 2   | --> | task 3   |
    | complete |     | blocked  |     | blocked  |
    +----------+     +----------+     +----------+
         |                ^
         +--- completing task 1 removes it from task 2's blockedBy

Key insight: "State that survives compression -- because it's outside the conversation."
"""

# 导入json模块，用于序列化和反序列化数据
import json
# 导入os模块，用于操作系统相关功能
import os
# 导入subprocess模块，用于执行shell命令
import subprocess
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
# 定义tasks目录路径
TASKS_DIR = WORKDIR / ".tasks"

# 系统提示词：告诉AI它在当前目录工作，使用task工具规划和跟踪工作
SYSTEM = f"You are a coding agent at {WORKDIR}. Use task tools to plan and track work."


# TaskManager类：带有依赖图管理的CRUD操作，持久化为JSON文件
# -- TaskManager: CRUD with dependency graph, persisted as JSON files --
class TaskManager:
    # 初始化TaskManager
    def __init__(self, tasks_dir: Path):
        # 保存tasks目录
        self.dir = tasks_dir
        # 创建目录（如果不存在）
        self.dir.mkdir(exist_ok=True)
        # 初始化下一个任务ID
        self._next_id = self._max_id() + 1

    # 获取最大任务ID
    def _max_id(self) -> int:
        # 收集所有task_*.json文件的ID
        ids = [int(f.stem.split("_")[1]) for f in self.dir.glob("task_*.json")]
        # 返回最大ID（如果没有则返回0）
        return max(ids) if ids else 0

    # 加载指定ID的任务
    def _load(self, task_id: int) -> dict:
        # 构建任务文件路径
        path = self.dir / f"task_{task_id}.json"
        # 如果文件不存在，抛出异常
        if not path.exists():
            raise ValueError(f"Task {task_id} not found")
        # 读取并解析JSON文件
        return json.loads(path.read_text())

    # 保存任务到文件
    def _save(self, task: dict):
        # 构建任务文件路径
        path = self.dir / f"task_{task['id']}.json"
        # 写入JSON格式的任务数据
        path.write_text(json.dumps(task, indent=2))

    # 创建新任务
    def create(self, subject: str, description: str = "") -> str:
        # 创建任务字典
        task = {
            # 任务ID、主题、描述
            "id": self._next_id, "subject": subject, "description": description,
            # 初始状态为pending，无依赖
            "status": "pending", "blockedBy": [], "blocks": [], "owner": "",
        }
        # 保存任务
        self._save(task)
        # 下一个任务ID递增
        self._next_id += 1
        # 返回JSON格式的任务信息
        return json.dumps(task, indent=2)

    # 获取指定ID的任务详情
    def get(self, task_id: int) -> str:
        # 加载并返回任务JSON
        return json.dumps(self._load(task_id), indent=2)

    # 更新任务状态或依赖
    def update(self, task_id: int, status: str = None,
               # 可选的依赖参数
               add_blocked_by: list = None, add_blocks: list = None) -> str:
        # 加载任务
        task = self._load(task_id)
        # 如果提供了状态参数
        if status:
            # 验证状态是否有效
            if status not in ("pending", "in_progress", "completed"):
                # 无效状态抛出异常
                raise ValueError(f"Invalid status: {status}")
            # 更新任务状态
            task["status"] = status
            # 当任务完成时，从所有其他任务的blockedBy中移除它
            # When a task is completed, remove it from all other tasks' blockedBy
            if status == "completed":
                # 清除依赖关系
                self._clear_dependency(task_id)
        # 如果添加了blockedBy依赖
        if add_blocked_by:
            # 合并并去重
            task["blockedBy"] = list(set(task["blockedBy"] + add_blocked_by))
        # 如果添加了blocks依赖
        if add_blocks:
            # 合并并去重
            task["blocks"] = list(set(task["blocks"] + add_blocks))
            # 双向更新：也更新被阻塞任务的blockedBy列表
            # Bidirectional: also update the blocked tasks' blockedBy lists
            # 遍历被阻塞的任务ID
            for blocked_id in add_blocks:
                # 尝试加载被阻塞的任务
                try:
                    # 加载任务
                    blocked = self._load(blocked_id)
                    # 如果当前任务ID不在blockedBy中
                    if task_id not in blocked["blockedBy"]:
                        # 添加到blockedBy列表
                        blocked["blockedBy"].append(task_id)
                        # 保存更新
                        self._save(blocked)
                # 捕获ValueError异常（任务不存在）
                except ValueError:
                    # 忽略错误
                    pass
        # 保存更新后的任务
        self._save(task)
        # 返回JSON格式的任务信息
        return json.dumps(task, indent=2)

    # 清除依赖关系
    def _clear_dependency(self, completed_id: int):
        """Remove completed_id from all other tasks' blockedBy lists."""
        # 遍历所有任务文件
        for f in self.dir.glob("task_*.json"):
            # 加载任务
            task = json.loads(f.read_text())
            # 如果completed_id在blockedBy列表中
            if completed_id in task.get("blockedBy", []):
                # 从blockedBy中移除
                task["blockedBy"].remove(completed_id)
                # 保存更新
                self._save(task)

    # 列出所有任务
    def list_all(self) -> str:
        # 收集所有任务
        tasks = []
        # 按文件名排序遍历
        for f in sorted(self.dir.glob("task_*.json")):
            # 加载任务并添加到列表
            tasks.append(json.loads(f.read_text()))
        # 如果没有任务
        if not tasks:
            # 返回提示信息
            return "No tasks."
        # 初始化行列表
        lines = []
        # 遍历每个任务
        for t in tasks:
            # 根据状态选择标记符号
            marker = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}.get(t["status"], "[?]")
            # 如果有blockedBy，显示阻塞信息
            blocked = f" (blocked by: {t['blockedBy']})" if t.get("blockedBy") else ""
            # 格式化行
            lines.append(f"{marker} #{t['id']}: {t['subject']}{blocked}")
        # 返回合并后的字符串
        return "\n".join(lines)


# 创建全局TaskManager实例
TASKS = TaskManager(TASKS_DIR)


# 基础工具实现函数
# -- Base tool implementations --
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
    # bash工具：执行shell命令
    "bash":        lambda **kw: run_bash(kw["command"]),
    # read_file工具：读取文件
    "read_file":   lambda **kw: run_read(kw["path"], kw.get("limit")),
    # write_file工具：写入文件
    "write_file":  lambda **kw: run_write(kw["path"], kw["content"]),
    # edit_file工具：编辑文件
    "edit_file":   lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    # task_create工具：创建新任务
    "task_create": lambda **kw: TASKS.create(kw["subject"], kw.get("description", "")),
    # task_update工具：更新任务状态或依赖
    "task_update": lambda **kw: TASKS.update(kw["task_id"], kw.get("status"), kw.get("addBlockedBy"), kw.get("addBlocks")),
    # task_list工具：列出所有任务
    "task_list":   lambda **kw: TASKS.list_all(),
    # task_get工具：获取指定ID的任务详情
    "task_get":    lambda **kw: TASKS.get(kw["task_id"]),
}

# 定义可用的工具列表
TOOLS = [
    # bash工具定义：运行shell命令
    {"name": "bash", "description": "Run a shell command.",
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
    # task_create工具定义：创建新任务
    {"name": "task_create", "description": "Create a new task.",
     "input_schema": {"type": "object", "properties": {"subject": {"type": "string"}, "description": {"type": "string"}}, "required": ["subject"]}},
    # task_update工具定义：更新任务状态或依赖
    {"name": "task_update", "description": "Update a task's status or dependencies.",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}, "addBlockedBy": {"type": "array", "items": {"type": "integer"}}, "addBlocks": {"type": "array", "items": {"type": "integer"}}}, "required": ["task_id"]}},
    # task_list工具定义：列出所有任务及状态摘要
    {"name": "task_list", "description": "List all tasks with status summary.",
     "input_schema": {"type": "object", "properties": {}}},
    # task_get工具定义：通过ID获取任务完整详情
    {"name": "task_get", "description": "Get full details of a task by ID.",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}},
]


# 定义agent_loop函数，接收消息历史列表
def agent_loop(messages: list):
    # 无限循环，直到模型不再调用工具
    while True:
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
            query = input("\033[36ms07 >> \033[0m")
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
