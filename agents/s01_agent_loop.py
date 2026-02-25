# Python解释器声明，使用Python3运行
#!/usr/bin/env python3

# s01_agent_loop.py - Agent循环
"""
s01_agent_loop.py - The Agent Loop

# AI编程Agent的全部秘密就在于这一个模式：
The entire secret of an AI coding agent in one pattern:

    # 当停止原因是"tool_use"时循环：
    while stop_reason == "tool_use":
        # 调用LLM获取响应，传入消息历史和工具列表
        response = LLM(messages, tools)
        # 执行工具调用
        execute tools
        # 将工具结果追加到消息历史
        append results

    # 用户 -> LLM -> 工具执行 -> 返回结果 -> 循环继续
    +----------+      +-------+      +---------+
    |   User   | ---> |  LLM  | ---> |  Tool   |
    |  prompt  |      |       |      | execute |
    +----------+      +---+---+      +----+----+
                          ^               |
                          |   tool_result |
                          +---------------+
                          (loop continues)

# 这就是核心循环：将工具结果反馈给模型
This is the core loop: feed tool results back to the model
# 直到模型决定停止。生产级Agent在此基础上添加策略、钩子和生命周期控制
until the model decides to stop. Production agents layer
policy, hooks, and lifecycle controls on top.
"""

# 导入os模块，用于操作系统相关功能
import os
# 导入subprocess模块，用于执行shell命令
import subprocess
# 导入platform模块，用于获取系统信息
import platform
# 导入sys模块，用于获取系统信息
import sys

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

# 创建Anthropic客户端，使用环境变量中的base_url
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))

# 从环境变量获取要使用的模型ID
MODEL = os.environ["MODEL_ID"]

# subprocess.run 使用的 shell：Windows 是 PowerShell，Linux/Mac 是 bash
subprocess_shell = "PowerShell" if os.name == "nt" else "bash"
# 用户运行的 shell 环境
user_shell = "PowerShell" if os.getenv("PSModulePath") else ("cmd" if os.name == "nt" else "bash")
SYSTEM = f"""You are a coding agent at {os.getcwd()}.
OS: {platform.system()} {platform.version()}
User Shell: {user_shell}
Subprocess Shell: {subprocess_shell}
Python: {platform.python_version()} ({sys.executable})
Use {subprocess_shell} commands in subprocess. Act, don't explain."""

# 定义可用的工具列表
TOOLS = [{
    # 工具名称：bash
    "name": "bash",
    # 工具描述：运行shell命令
    "description": "Run a shell command.",
    # 输入模式定义
    "input_schema": {
        # 类型为对象
        "type": "object",
        # 属性：command参数，类型为字符串
        "properties": {"command": {"type": "string"}},
        # 必需参数：command
        "required": ["command"],
    },
}]


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
        # Windows 上使用 PowerShell，Linux/Mac 使用默认 shell
        if os.name == "nt":
            # 使用 PowerShell 执行命令
            r = subprocess.run(
                ["powershell", "-Command", command],
                cwd=os.getcwd(),
                capture_output=True,
                text=True,
                timeout=120
            )
        else:
            # 使用默认 shell
            r = subprocess.run(
                command,
                shell=True,
                cwd=os.getcwd(),
                capture_output=True,
                text=True,
                timeout=120
            )
        # 合并stdout和stderr，去除首尾空白
        out = (r.stdout + r.stderr).strip()
        # 如果有输出则返回（限制50000字符），否则返回"(no output)"
        return out[:50000] if out else "(no output)"
    # 如果命令执行超时
    except subprocess.TimeoutExpired:
        # 返回超时错误信息
        return "Error: Timeout (120s)"


def print_messages_history(messages: list):
    """打印消息历史"""
    print(f"\n[Messages History] Total messages: {len(messages)}")
    for i, msg in enumerate(messages):
        print(f"\n{i+1}. Role: {msg['role']}")
        content = msg['content']
        if isinstance(content, list):
            print(f"   Content type: list (length: {len(content)})")
            for j, block in enumerate(content):
                if hasattr(block, 'type'):
                    print(f"     Block {j+1}: {block.type}")
                    if hasattr(block, 'input'):
                        print(f"       Input: {block.input}")
                    if hasattr(block, 'text'):
                        print(f"       Text: {block.text[:100]}..." if len(block.text) > 100 else f"       Text: {block.text}")
                elif isinstance(block, dict):
                    # 处理字典类型的内容（如工具结果）
                    print(f"     Block {j+1}: dict")
                    if 'type' in block:
                        print(f"       Type: {block['type']}")
                    if 'content' in block:
                        content_str = str(block['content'])
                        print(f"       Content: {content_str[:100]}..." if len(content_str) > 100 else f"       Content: {content_str}")
        else:
            content_str = str(content)
            print(f"   Content: {content_str[:100]}..." if len(content_str) > 100 else f"   Content: {content_str}")


# -- 核心模式：持续调用工具直到模型停止 --
# -- The core pattern: a while loop that calls tools until the model stops --
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
        # Append assistant turn
        # 以assistant角色添加响应内容到消息历史
        messages.append({"role": "assistant", "content": response.content})
        # 打印消息历史
        print_messages_history(messages)

        # 如果模型没有调用工具，则任务完成
        # If the model didn't call a tool, we're done
        # 如果停止原因不是"tool_use"
        if response.stop_reason != "tool_use":
            # 退出函数，结束循环
            return

        # 执行每个工具调用并收集结果
        # Execute each tool call, collect results
        # 初始化结果列表
        results = []
        # 遍历响应内容中的每个块
        for block in response.content:
            # 如果是工具调用块
            if block.type == "tool_use":
                # 打印命令（黄色）

                print(f"\n执行指令：")
                print(f"\033[33m$ {block.input['command']}\033[0m")
                # 执行bash命令
                output = run_bash(block.input["command"])
                # 打印输出结果（截取前200字符）
                print(f"\n执行结果：")
                print(output[:200])
                # 将工具结果添加到结果列表
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": output})
        # 以user角色将工具结果添加到消息历史，继续循环
        messages.append({"role": "user", "content": results})

        print_messages_history(messages)

# 主程序入口
if __name__ == "__main__":
    # 初始化消息历史列表
    history = []
    # 无限循环，等待用户输入
    while True:
        # 读取用户输入（青色提示符）
        try:
            query = input("\033[36ms01 >> \033[0m")
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
        # 获取最后的响应内容
        response_content = history[-1]["content"]
        # 如果响应内容是列表
        if isinstance(response_content, list):
            # 遍历每个块
            for block in response_content:
                # 如果块有text属性
                if hasattr(block, "text"):
                    # 打印文本内容
                    print(block.text)
        # 打印空行分隔输出
        print()
