# Python解释器声明，使用Python3运行
#!/usr/bin/env python3
"""
s02_tool_use.py - Tools

The agent loop from s01 didn't change. We just added tools to the array
and a dispatch map to route calls.

    +----------+      +-------+      +------------------+
    |   User   | ---> |  LLM  | ---> | Tool Dispatch    |
    |  prompt  |      |       |      | {                |
    +----------+      +---+---+      |   bash: run_bash |
                          ^          |   read: run_read |
                          |          |   write: run_wr  |
                          +----------+   edit: run_edit |
                          tool_result| }                |
                                     +------------------+

Key insight: "The loop didn't change at all. I just added tools."
"""

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

# 系统提示词：告诉AI它是一个在当前目录工作的编程Agent，使用工具解决问题而不是解释
SYSTEM = f"You are a coding agent at {WORKDIR}. Use tools to solve tasks. Act, don't explain."


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
        # 读取文件内容
        text = safe_path(path).read_text()
        # 按行分割文本
        lines = text.splitlines()
        # 如果设置了限制且行数超过限制
        if limit and limit < len(lines):
            # 只返回前limit行，并在末尾添加省略信息
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
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
        return f"Wrote {len(content)} bytes to {path}"
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
        content = fp.read_text()
        # 检查旧文本是否存在于文件中
        if old_text not in content:
            # 如果不存在，返回错误信息
            return f"Error: Text not found in {path}"
        # 替换文本（只替换第一个匹配项）
        fp.write_text(content.replace(old_text, new_text, 1))
        # 返回成功信息
        return f"Edited {path}"
    # 捕获所有异常
    except Exception as e:
        # 返回错误信息
        return f"Error: {e}"


# 工具调度映射表：将工具名称映射到处理函数
# The dispatch map: {tool_name: handler}
TOOL_HANDLERS = {
    # bash工具：执行shell命令
    "bash":       lambda **kw: run_bash(kw["command"]),
    # read_file工具：读取文件
    "read_file":  lambda **kw: run_read(kw["path"], kw.get("limit")),
    # write_file工具：写入文件
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    # edit_file工具：编辑文件
    "edit_file":  lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
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
        # 如果停止原因不是"tool_use"
        if response.stop_reason != "tool_use":
            # 退出函数，结束循环
            return

        # 执行每个工具调用，收集结果
        # 初始化结果列表
        results = []
        # 遍历响应内容中的每个块
        for block in response.content:
            # 如果是工具调用块
            if block.type == "tool_use":
                # 从调度表中获取对应的处理函数
                handler = TOOL_HANDLERS.get(block.name)
                # 调用处理函数，传入参数
                output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
                # 打印工具名称和输出结果（截取前200字符）
                print(f"> {block.name}: {output[:200]}")
                # 将工具结果添加到结果列表
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})
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
            query = input("\033[36ms02 >> \033[0m")
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
