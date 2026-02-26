# Python解释器声明，使用Python3运行
#!/usr/bin/env python3
"""
s06_context_compact.py - Compact

Three-layer compression pipeline so the agent can work forever:

    Every turn:
    +------------------+
    | Tool call result |
    +------------------+
            |
            v
    [Layer 1: micro_compact]        (silent, every turn)
      Replace tool_result content older than last 3
      with "[Previous: used {tool_name}]"
            |
            v
    [Check: tokens > 50000?]
       |               |
       no              yes
       |               |
       v               v
    continue    [Layer 2: auto_compact]
                  Save full transcript to .transcripts/
                  Ask LLM to summarize conversation.
                  Replace all messages with [summary].
                        |
                        v
                [Layer 3: compact tool]
                  Model calls compact -> immediate summarization.
                  Same as auto, triggered manually.

Key insight: "The agent can forget strategically and keep working forever."
"""

# 导入json模块，用于序列化和反序列化数据
import json
# 导入os模块，用于操作系统相关功能
import os
# 导入subprocess模块，用于执行shell命令
import subprocess
# 导入time模块，用于时间相关功能
import time
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

# 系统提示词：告诉AI它在当前目录工作，使用工具解决问题
SYSTEM = f"You are a coding agent at {WORKDIR}. Use tools to solve tasks."

# 设置token阈值，超过此值时触发自动压缩
THRESHOLD = 50000
# 定义transcript保存目录
TRANSCRIPT_DIR = WORKDIR / ".transcripts"
# 保留最近几次的tool结果不被压缩
KEEP_RECENT = 3


# 估算消息列表的token数量
def estimate_tokens(messages: list) -> int:
    """Rough token count: ~4 chars per token."""
    # 简单估算：字符数除以4
    return len(str(messages)) // 4


# Layer 1：micro_compact - 将旧的tool结果替换为占位符
# -- Layer 1: micro_compact - replace old tool results with placeholders --
# 定义micro_compact函数，每轮自动执行，将旧的tool结果替换为简短占位符
def micro_compact(messages: list) -> list:
    # 收集所有tool_result条目的（消息索引，部分索引，工具结果字典）
    # Collect (msg_index, part_index, tool_result_dict) for all tool_result entries
    tool_results = []
    # 遍历所有消息
    for msg_idx, msg in enumerate(messages):
        # 检查是否是用户消息且内容是列表
        if msg["role"] == "user" and isinstance(msg.get("content"), list):
            # 遍历消息内容中的每个部分
            for part_idx, part in enumerate(msg["content"]):
                # 如果是字典类型且是tool_result
                if isinstance(part, dict) and part.get("type") == "tool_result":
                    # 添加到列表
                    tool_results.append((msg_idx, part_idx, part))
    # 如果tool结果数量小于等于保留数量，直接返回
    if len(tool_results) <= KEEP_RECENT:
        return messages
    # 通过匹配先前assistant消息中的tool_use_id来查找每个结果的tool_name
    # Find tool_name for each result by matching tool_use_id in prior assistant messages
    # 创建tool名称映射字典
    tool_name_map = {}
    # 遍历消息
    for msg in messages:
        # 如果是assistant消息
        if msg["role"] == "assistant":
            # 获取内容
            content = msg.get("content", [])
            # 如果内容是列表
            if isinstance(content, list):
                # 遍历每个块
                for block in content:
                    # 如果有type属性且是tool_use
                    if hasattr(block, "type") and block.type == "tool_use":
                        # 记录tool_use_id到tool名称的映射
                        tool_name_map[block.id] = block.name
    # 清除旧的结果（保留最近KEEP_RECENT个）
    # Clear old results (keep last KEEP_RECENT)
    # 获取需要清除的旧结果
    to_clear = tool_results[:-KEEP_RECENT]
    # 遍历需要清除的结果
    for _, _, result in to_clear:
        # 如果内容是字符串且长度大于100
        if isinstance(result.get("content"), str) and len(result["content"]) > 100:
            # 获取tool_use_id
            tool_id = result.get("tool_use_id", "")
            # 获取tool名称（未知则返回"unknown"）
            tool_name = tool_name_map.get(tool_id, "unknown")
            # 将内容替换为占位符
            result["content"] = f"[Previous: used {tool_name}]"
    # 返回处理后的消息列表
    return messages


# Layer 2：auto_compact - 保存transcript，摘要，替换消息
# -- Layer 2: auto_compact - save transcript, summarize, replace messages --
# 定义auto_compact函数，当token超过阈值时自动执行
def auto_compact(messages: list) -> list:
    # 保存完整transcript到磁盘
    # Save full transcript to disk
    # 创建transcript目录（如果不存在）
    TRANSCRIPT_DIR.mkdir(exist_ok=True)
    # 生成transcript文件路径（使用时间戳命名）
    transcript_path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    # 打开文件写入
    with open(transcript_path, "w") as f:
        # 遍历每条消息
        for msg in messages:
            # 写入JSON格式的消息（每行一条）
            f.write(json.dumps(msg, default=str) + "\n")
    # 打印transcript保存信息
    print(f"[transcript saved: {transcript_path}]")
    # 让LLM进行摘要
    # Ask LLM to summarize
    # 将消息序列化为JSON字符串（限制80000字符）
    conversation_text = json.dumps(messages, default=str)[:80000]
    # 调用Anthropic API进行摘要
    response = client.messages.create(
        # 使用相同模型
        model=MODEL,
        # 传入包含摘要请求的消息
        messages=[{"role": "user", "content":
            # 摘要提示词，要求包含：1）完成的工作 2）当前状态 3）关键决策
            "Summarize this conversation for continuity. Include: "
            "1) What was accomplished, 2) Current state, 3) Key decisions made. "
            "Be concise but preserve critical details.\n\n" + conversation_text}],
        # 最大生成token数2000
        max_tokens=2000,
    )
    # 获取摘要文本
    summary = response.content[0].text
    # 用压缩后的摘要替换所有消息
    # Replace all messages with compressed summary
    # 返回包含摘要和确认消息的新列表
    return [
        # 用户消息：包含transcript路径和摘要内容
        {"role": "user", "content": f"[Conversation compressed. Transcript: {transcript_path}]\n\n{summary}"},
        # Assistant消息：确认理解摘要内容
        {"role": "assistant", "content": "Understood. I have the context from the summary. Continuing."},
    ]


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
TOOL_HANDLERS = {
    # bash工具：执行shell命令
    "bash":       lambda **kw: run_bash(kw["command"]),
    # read_file工具：读取文件
    "read_file":  lambda **kw: run_read(kw["path"], kw.get("limit")),
    # write_file工具：写入文件
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    # edit_file工具：编辑文件
    "edit_file":  lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    # compact工具：手动触发压缩
    "compact":    lambda **kw: "Manual compression requested.",
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
    # compact工具定义：手动触发对话压缩
    {"name": "compact", "description": "Trigger manual conversation compression.",
     "input_schema": {"type": "object", "properties": {"focus": {"type": "string", "description": "What to preserve in the summary"}}}},
]


# 定义agent_loop函数，接收消息历史列表
def agent_loop(messages: list):
    # 无限循环，直到模型不再调用工具
    while True:
        # Layer 1：每次LLM调用前执行micro_compact
        # Layer 1: micro_compact before each LLM call
        # 调用micro_compact函数压缩旧的tool结果
        micro_compact(messages)
        # Layer 2：如果token估算超过阈值则执行auto_compact
        # Layer 2: auto_compact if token estimate exceeds threshold
        # 检查token数量是否超过阈值
        if estimate_tokens(messages) > THRESHOLD:
            # 打印自动压缩触发信息
            print("[auto_compact triggered]")
            # 执行自动压缩并更新消息列表（使用切片赋值）
            messages[:] = auto_compact(messages)
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
        # 初始化手动压缩标志
        manual_compact = False
        # 遍历响应内容中的每个块
        for block in response.content:
            # 如果是工具调用块
            if block.type == "tool_use":
                # 如果调用的是compact工具
                if block.name == "compact":
                    # 标记手动压缩
                    manual_compact = True
                    # 设置输出信息
                    output = "Compressing..."
                # 否则调用其他工具
                else:
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
        # Layer 3：如果compact工具被调用则执行手动压缩
        # Layer 3: manual compact triggered by the compact tool
        # 检查是否触发了手动压缩
        if manual_compact:
            # 打印手动压缩信息
            print("[manual compact]")
            # 执行自动压缩并更新消息列表
            messages[:] = auto_compact(messages)


# 主程序入口
if __name__ == "__main__":
    # 初始化消息历史列表
    history = []
    # 无限循环，等待用户输入
    while True:
        # 读取用户输入（青色提示符）
        try:
            query = input("\033[36ms06 >> \033[0m")
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
