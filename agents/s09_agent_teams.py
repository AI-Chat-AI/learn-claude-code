# Python解释器声明，使用Python3运行
#!/usr/bin/env python3
"""
s09_agent_teams.py - Agent Teams

Persistent named agents with file-based JSONL inboxes. Each teammate runs
its own agent loop in a separate thread. Communication via append-only inboxes.

    Subagent (s04):  spawn -> execute -> return summary -> destroyed
    Teammate (s09):  spawn -> work -> idle -> work -> ... -> shutdown

    .team/config.json                   .team/inbox/
    +----------------------------+      +------------------+
    | {"team_name": "default",   |      | alice.jsonl      |
    |  "members": [              |      | bob.jsonl        |
    |    {"name":"alice",        |      | lead.jsonl       |
    |     "role":"coder",        |      +------------------+
    |     "status":"idle"}       |
    |  ]}                        |      send_message("alice", "fix bug"):
    +----------------------------+        open("alice.jsonl", "a").write(msg)

                                        read_inbox("alice"):
    spawn_teammate("alice","coder",...)   msgs = [json.loads(l) for l in ...]
         |                                open("alice.jsonl", "w").close()
         v                                return msgs  # drain
    Thread: alice             Thread: bob
    +------------------+      +------------------+
    | agent_loop       |      | agent_loop       |
    | status: working  |      | status: idle     |
    | ... runs tools   |      | ... waits ...    |
    | status -> idle   |      |                  |
    +------------------+      +------------------+

    5 message types (all declared, not all handled here):
    +-------------------------+-----------------------------------+
    | message                 | Normal text message               |
    | broadcast               | Sent to all teammates             |
    | shutdown_request        | Request graceful shutdown (s10)   |
    | shutdown_response       | Approve/reject shutdown (s10)     |
    | plan_approval_response  | Approve/reject plan (s10)         |
    +-------------------------+-----------------------------------+

Key insight: "Teammates that can talk to each other."
"""

# 导入json模块，用于序列化和反序列化数据
import json
# 导入os模块，用于操作系统相关功能
import os
# 导入subprocess模块，用于执行shell命令
import subprocess
# 导入threading模块，用于多线程编程
import threading
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
# 定义team目录和inbox目录路径
TEAM_DIR = WORKDIR / ".team"
INBOX_DIR = TEAM_DIR / "inbox"

# 系统提示词：告诉AI它是团队lead，在当前目录工作，通过inbox与队员通信
SYSTEM = f"You are a team lead at {WORKDIR}. Spawn teammates and communicate via inboxes."

# 定义有效的消息类型集合
VALID_MSG_TYPES = {
    # 普通文本消息
    "message",
    # 广播消息发送给所有队员，
    "broadcast",
    # 关闭请求（s10）
    "shutdown_request",
    # 关闭响应（s10）
    "shutdown_response",
    # 计划审批响应（s10）
    "plan_approval_response",
}


# MessageBus类：每个队员一个JSONL收件箱
# -- MessageBus: JSONL inbox per teammate --
class MessageBus:
    # 初始化MessageBus
    def __init__(self, inbox_dir: Path):
        # 保存inbox目录
        self.dir = inbox_dir
        # 创建目录（如果不存在）
        self.dir.mkdir(parents=True, exist_ok=True)

    # 发送消息到指定队员的收件箱
    def send(self, sender: str, to: str, content: str,
             # 消息类型，默认为"message"
             msg_type: str = "message", extra: dict = None) -> str:
        # 检查消息类型是否有效
        if msg_type not in VALID_MSG_TYPES:
            # 返回错误信息，列出有效类型
            return f"Error: Invalid type '{msg_type}'. Valid: {VALID_MSG_TYPES}"
        # 创建消息字典
        msg = {
            # 消息类型
            "type": msg_type,
            # 发送者
            "from": sender,
            # 消息内容
            "content": content,
            # 时间戳
            "timestamp": time.time(),
        }
        # 如果有额外数据
        if extra:
            # 合并到消息中
            msg.update(extra)
        # 获取目标收件箱文件路径
        inbox_path = self.dir / f"{to}.jsonl"
        # 以追加模式打开文件
        with open(inbox_path, "a") as f:
            # 写入JSON格式的消息（每行一条）
            f.write(json.dumps(msg) + "\n")
        # 返回发送成功信息
        return f"Sent {msg_type} to {to}"

    # 读取并清空指定队员的收件箱
    def read_inbox(self, name: str) -> list:
        # 获取收件箱文件路径
        inbox_path = self.dir / f"{name}.jsonl"
        # 如果文件不存在，返回空列表
        if not inbox_path.exists():
            return []
        # 初始化消息列表
        messages = []
        # 遍历文件的每一行
        for line in inbox_path.read_text().strip().splitlines():
            # 如果行不为空
            if line:
                # 解析JSON并添加到列表
                messages.append(json.loads(line))
        # 清空收件箱文件
        inbox_path.write_text("")
        # 返回消息列表
        return messages

    # 广播消息给所有队员
    def broadcast(self, sender: str, content: str, teammates: list) -> str:
        # 初始化计数
        count = 0
        # 遍历每个队员
        for name in teammates:
            # 如果不是发送者本人
            if name != sender:
                # 发送广播消息
                self.send(sender, name, content, "broadcast")
                # 计数+1
                count += 1
        # 返回广播成功的队员数量
        return f"Broadcast to {count} teammates"


# 创建全局MessageBus实例
BUS = MessageBus(INBOX_DIR)


# TeammateManager类：带有config.json的持久化命名Agent
# -- TeammateManager: persistent named agents with config.json --
class TeammateManager:
    # 初始化TeammateManager
    def __init__(self, team_dir: Path):
        # 保存team目录
        self.dir = team_dir
        # 创建目录（如果不存在）
        self.dir.mkdir(exist_ok=True)
        # 配置文件路径
        self.config_path = self.dir / "config.json"
        # 加载配置
        self.config = self._load_config()
        # 线程字典
        self.threads = {}

    # 加载配置文件
    def _load_config(self) -> dict:
        # 如果配置文件存在
        if self.config_path.exists():
            # 读取并解析JSON
            return json.loads(self.config_path.read_text())
        # 返回默认配置
        return {"team_name": "default", "members": []}

    # 保存配置文件
    def _save_config(self):
        # 写入JSON格式的配置
        self.config_path.write_text(json.dumps(self.config, indent=2))

    # 根据名称查找队员
    def _find_member(self, name: str) -> dict:
        # 遍历所有队员
        for m in self.config["members"]:
            # 如果名称匹配
            if m["name"] == name:
                # 返回队员信息
                return m
        # 未找到返回None
        return None

    # 生成（启动）新队员
    def spawn(self, name: str, role: str, prompt: str) -> str:
        # 尝试查找已存在的队员
        member = self._find_member(name)
        # 如果队员存在
        if member:
            # 检查状态是否为idle或shutdown
            if member["status"] not in ("idle", "shutdown"):
                # 返回错误信息
                return f"Error: '{name}' is currently {member['status']}"
            # 更新状态为working
            member["status"] = "working"
            # 更新角色
            member["role"] = role
        # 如果队员不存在，创建新的
        else:
            member = {"name": name, "role": role, "status": "working"}
            # 添加到成员列表
            self.config["members"].append(member)
        # 保存配置
        self._save_config()
        # 创建后台线程
        thread = threading.Thread(
            # 目标函数为_teammate_loop
            target=self._teammate_loop,
            # 传入参数：名称、角色、初始提示
            args=(name, role, prompt),
            # 设置为守护线程
            daemon=True,
        )
        # 保存线程引用
        self.threads[name] = thread
        # 启动线程
        thread.start()
        # 返回生成成功信息
        return f"Spawned '{name}' (role: {role})"

    # 队员的工作循环（在线程中运行）
    def _teammate_loop(self, name: str, role: str, prompt: str):
        # 构建系统提示词
        sys_prompt = (
            # 包含名称、角色、工作目录
            f"You are '{name}', role: {role}, at {WORKDIR}. "
            # 告诉队员使用send_message通信
            f"Use send_message to communicate. Complete your task."
        )
        # 初始化消息列表（包含初始提示）
        messages = [{"role": "user", "content": prompt}]
        # 获取队员可用的工具列表
        tools = self._teammate_tools()
        # 最多循环50次作为安全限制
        for _ in range(50):
            # 读取收件箱
            inbox = BUS.read_inbox(name)
            # 将收到的消息添加到对话历史
            for msg in inbox:
                # 将消息作为用户消息添加
                messages.append({"role": "user", "content": json.dumps(msg)})
            # 尝试调用Anthropic API
            try:
                response = client.messages.create(
                    # 使用相同模型
                    model=MODEL,
                    # 系统提示词
                    system=sys_prompt,
                    # 消息历史
                    messages=messages,
                    # 工具列表
                    tools=tools,
                    # 最大生成token数8000
                    max_tokens=8000,
                )
            # 捕获异常并退出循环
            except Exception:
                break
            # 将助手消息添加到历史
            messages.append({"role": "assistant", "content": response.content})
            # 如果模型没有调用工具，则任务完成
            if response.stop_reason != "tool_use":
                # 退出循环
                break
            # 初始化结果列表
            results = []
            # 遍历响应内容中的每个块
            for block in response.content:
                # 如果是工具调用块
                if block.type == "tool_use":
                    # 执行工具并获取输出
                    output = self._exec(name, block.name, block.input)
                    # 打印工具执行信息
                    print(f"  [{name}] {block.name}: {str(output)[:120]}")
                    # 将工具结果添加到结果列表
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": str(output),
                    })
            # 将工具结果作为用户消息添加到历史
            messages.append({"role": "user", "content": results})
        # 获取队员信息
        member = self._find_member(name)
        # 如果队员存在且状态不是shutdown
        if member and member["status"] != "shutdown":
            # 更新状态为idle
            member["status"] = "idle"
            # 保存配置
            self._save_config()

    # 执行工具（队员版本）
    def _exec(self, sender: str, tool_name: str, args: dict) -> str:
        # 这些基础工具与s02相同
        # these base tools are unchanged from s02
        # 如果是bash工具
        if tool_name == "bash":
            # 返回bash执行结果
            return _run_bash(args["command"])
        # 如果是read_file工具
        if tool_name == "read_file":
            # 返回文件读取结果
            return _run_read(args["path"])
        # 如果是write_file工具
        if tool_name == "write_file":
            # 返回文件写入结果
            return _run_write(args["path"], args["content"])
        # 如果是edit_file工具
        if tool_name == "edit_file":
            # 返回文件编辑结果
            return _run_edit(args["path"], args["old_text"], args["new_text"])
        # 如果是send_message工具
        if tool_name == "send_message":
            # 发送消息到消息总线
            return BUS.send(sender, args["to"], args["content"], args.get("msg_type", "message"))
        # 如果是read_inbox工具
        if tool_name == "read_inbox":
            # 读取并返回收件箱（JSON格式）
            return json.dumps(BUS.read_inbox(sender), indent=2)
        # 未知工具返回错误信息
        return f"Unknown tool: {tool_name}"

    # 获取队员可用的工具列表
    def _teammate_tools(self) -> list:
        # 这些基础工具与s02相同
        # these base tools are unchanged from s02
        # 返回工具定义列表
        return [
            # bash工具：运行shell命令
            {"name": "bash", "description": "Run a shell command.",
             "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
            # read_file工具：读取文件内容
            {"name": "read_file", "description": "Read file contents.",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
            # write_file工具：写入文件内容
            {"name": "write_file", "description": "Write content to file.",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
            # edit_file工具：替换文件中的精确文本
            {"name": "edit_file", "description": "Replace exact text in file.",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
            # send_message工具：发送消息给队员
            {"name": "send_message", "description": "Send message to a teammate.",
             "input_schema": {"type": "object", "properties": {"to": {"type": "string"}, "content": {"type": "string"}, "msg_type": {"type": "string", "enum": list(VALID_MSG_TYPES)}}, "required": ["to", "content"]}},
            # read_inbox工具：读取并清空收件箱
            {"name": "read_inbox", "description": "Read and drain your inbox.",
             "input_schema": {"type": "object", "properties": {}}},
        ]

    # 列出所有队员
    def list_all(self) -> str:
        # 如果没有队员
        if not self.config["members"]:
            # 返回提示信息
            return "No teammates."
        # 初始化行列表
        lines = [f"Team: {self.config['team_name']}"]
        # 遍历每个队员
        for m in self.config["members"]:
            # 格式化队员信息
            lines.append(f"  {m['name']} ({m['role']}): {m['status']}")
        # 返回合并后的字符串
        return "\n".join(lines)

    # 获取所有队员的名称列表
    def member_names(self) -> list:
        # 返回所有队员名称组成的列表
        return [m["name"] for m in self.config["members"]]


# 创建全局TeammateManager实例
TEAM = TeammateManager(TEAM_DIR)


# 基础工具实现函数（这些基础工具与s02相同）
# -- Base tool implementations (these base tools are unchanged from s02) --
# 定义safe_path函数，确保路径在工作目录内
def _safe_path(p: str) -> Path:
    # 将相对路径转换为绝对路径
    path = (WORKDIR / p).resolve()
    # 检查路径是否在工作目录内
    if not path.is_relative_to(WORKDIR):
        # 如果路径超出工作目录，抛出异常
        raise ValueError(f"Path escapes workspace: {p}")
    # 返回安全的路径
    return path


# 定义run_bash函数，执行shell命令并返回结果
def _run_bash(command: str) -> str:
    # 定义危险命令列表
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot"]
    # 如果命令包含危险内容
    if any(d in command for d in dangerous):
        # 返回错误信息
        return "Error: Dangerous command blocked"
    # 尝试执行命令
    try:
        # 使用subprocess运行命令
        r = subprocess.run(
            command, shell=True, cwd=WORKDIR,
            capture_output=True, text=True, timeout=120,
        )
        # 合并stdout和stderr
        out = (r.stdout + r.stderr).strip()
        # 返回结果（限制50000字符）
        return out[:50000] if out else "(no output)"
    # 超时处理
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"


# 定义run_read函数，读取文件内容
def _run_read(path: str, limit: int = None) -> str:
    # 尝试读取文件
    try:
        # 读取并分割行
        lines = _safe_path(path).read_text().splitlines()
        # 如果有限制且超过限制
        if limit and limit < len(lines):
            # 只返回前limit行
            lines = lines[:limit] + [f"... ({len(lines) - limit} more)"]
        # 返回合并的文本
        return "\n".join(lines)[:50000]
    except Exception as e:
        return f"Error: {e}"


# 定义run_write函数，写入文件内容
def _run_write(path: str, content: str) -> str:
    try:
        # 获取安全路径
        fp = _safe_path(path)
        # 创建父目录
        fp.parent.mkdir(parents=True, exist_ok=True)
        # 写入内容
        fp.write_text(content)
        return f"Wrote {len(content)} bytes"
    except Exception as e:
        return f"Error: {e}"


# 定义run_edit函数，编辑文件内容
def _run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        fp = _safe_path(path)
        c = fp.read_text()
        if old_text not in c:
            return f"Error: Text not found in {path}"
        fp.write_text(c.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


# Lead工具调度（9个工具）
# -- Lead tool dispatch (9 tools) --
# 工具调度映射表
TOOL_HANDLERS = {
    # bash工具：执行shell命令
    "bash":            lambda **kw: _run_bash(kw["command"]),
    # read_file工具：读取文件
    "read_file":       lambda **kw: _run_read(kw["path"], kw.get("limit")),
    # write_file工具：写入文件
    "write_file":      lambda **kw: _run_write(kw["path"], kw["content"]),
    # edit_file工具：编辑文件
    "edit_file":       lambda **kw: _run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    # spawn_teammate工具：生成持久化队员
    "spawn_teammate":  lambda **kw: TEAM.spawn(kw["name"], kw["role"], kw["prompt"]),
    # list_teammates工具：列出所有队员
    "list_teammates":  lambda **kw: TEAM.list_all(),
    # send_message工具：发送消息给队员
    "send_message":    lambda **kw: BUS.send("lead", kw["to"], kw["content"], kw.get("msg_type", "message")),
    # read_inbox工具：读取lead的收件箱
    "read_inbox":      lambda **kw: json.dumps(BUS.read_inbox("lead"), indent=2),
    # broadcast工具：广播消息给所有队员
    "broadcast":       lambda **kw: BUS.broadcast("lead", kw["content"], TEAM.member_names()),
}

# 工具定义列表（基础工具与s02相同）
# these base tools are unchanged from s02
TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "spawn_teammate", "description": "Spawn a persistent teammate that runs in its own thread.",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "role": {"type": "string"}, "prompt": {"type": "string"}}, "required": ["name", "role", "prompt"]}},
    {"name": "list_teammates", "description": "List all teammates with name, role, status.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "send_message", "description": "Send a message to a teammate's inbox.",
     "input_schema": {"type": "object", "properties": {"to": {"type": "string"}, "content": {"type": "string"}, "msg_type": {"type": "string", "enum": list(VALID_MSG_TYPES)}}, "required": ["to", "content"]}},
    {"name": "read_inbox", "description": "Read and drain the lead's inbox.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "broadcast", "description": "Send a message to all teammates.",
     "input_schema": {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]}},
]


# 定义agent_loop函数，接收消息历史列表
def agent_loop(messages: list):
    # 无限循环，直到模型不再调用工具
    while True:
        # 读取lead的收件箱
        inbox = BUS.read_inbox("lead")
        # 如果有收到消息
        if inbox:
            # 将消息添加到对话历史
            messages.append({
                "role": "user",
                # 消息内容包装在inbox标签中
                "content": f"<inbox>{json.dumps(inbox, indent=2)}</inbox>",
            })
            # 添加确认收到消息的助手消息
            messages.append({
                "role": "assistant",
                "content": "Noted inbox messages.",
            })
        # 调用Anthropic API创建消息
        response = client.messages.create(
            # 传入模型ID、系统提示词和消息历史
            model=MODEL,
            system=SYSTEM,
            messages=messages,
            # 传入工具定义，最大生成token数8000
            tools=TOOLS,
            max_tokens=8000,
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
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": str(output),
                })
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
            query = input("\033[36ms09 >> \033[0m")
        # 捕获EOF和Ctrl+C异常
        except (EOFError, KeyboardInterrupt):
            # 退出循环
            break
        # 如果输入为q、exit或空字符串
        if query.strip().lower() in ("q", "exit", ""):
            # 退出循环
            break
        # 如果输入/team，显示队员列表
        if query.strip() == "/team":
            # 打印队员列表
            print(TEAM.list_all())
            # 继续等待下一个命令
            continue
        # 如果输入/inbox，显示收件箱
        if query.strip() == "/inbox":
            # 打印收件箱内容
            print(json.dumps(BUS.read_inbox("lead"), indent=2))
            # 继续等待下一个命令
            continue
        # 将用户消息添加到历史
        history.append({"role": "user", "content": query})
        # 调用agent_loop处理任务
        agent_loop(history)
        # 打印空行分隔输出
        print()
