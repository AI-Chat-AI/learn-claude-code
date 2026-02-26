#!/usr/bin/env python3
"""
s11_autonomous_agents.py - 自主代理

空闲循环与任务板轮询，自动认领未分配任务，以及上下文压缩后的
身份重新注入。基于s10的协议构建。

    队友生命周期：
    +-------+
    | spawn |
    +---+---+
        |
        v
    +-------+  tool_use    +-------+
    | WORK  | <----------- |  LLM  |
    +---+---+              +-------+
        |
        | stop_reason != tool_use
        v
    +--------+
    | IDLE   | 每5秒轮询，最长60秒
    +---+----+
        |
        +---> 检查收件箱 -> 有消息? -> 恢复工作
        |
        +---> 扫描.tasks/ -> 有未分配? -> 认领 -> 恢复工作
        |
        +---> 超时(60秒) -> 关闭

    压缩后的身份重新注入：
    messages = [identity_block, ...remaining...]
    "You are 'coder', role: backend, team: my-team"

关键洞察："代理自己寻找工作。"
"""

# 导入标准库
import json
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path

# 导入第三方库
from anthropic import Anthropic
from dotenv import load_dotenv

# 加载环境变量
load_dotenv(override=True)
# 如果设置了ANTHROPIC_BASE_URL，则移除ANTHROPIC_AUTH_TOKEN
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

# 工作目录
WORKDIR = Path.cwd()
# 创建Anthropic客户端
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
# 模型ID
MODEL = os.environ["MODEL_ID"]
# 团队目录
TEAM_DIR = WORKDIR / ".team"
# 收件箱目录
INBOX_DIR = TEAM_DIR / "inbox"
# 任务目录
TASKS_DIR = WORKDIR / ".tasks"

# 轮询间隔（秒）
POLL_INTERVAL = 5
# 空闲超时（秒）
IDLE_TIMEOUT = 60

# 系统提示词：团队领导者角色，队友是自主的
SYSTEM = f"You are a team lead at {WORKDIR}. Teammates are autonomous -- they find work themselves."

# 有效的消息类型集合
VALID_MSG_TYPES = {
    "message",
    "broadcast",
    "shutdown_request",
    "shutdown_response",
    "plan_approval_response",
}

# -- 请求追踪器 --
# 关闭请求追踪字典
shutdown_requests = {}
# 计划请求追踪字典
plan_requests = {}
# 线程锁，保护共享数据
_tracker_lock = threading.Lock()
# 认领任务锁
_claim_lock = threading.Lock()


# -- MessageBus: 每个队友一个JSONL收件箱 --
class MessageBus:
    # 初始化MessageBus
    def __init__(self, inbox_dir: Path):
        # 保存收件箱目录
        self.dir = inbox_dir
        # 创建目录（如果不存在）
        self.dir.mkdir(parents=True, exist_ok=True)

    # 发送消息到指定队友的收件箱
    def send(self, sender: str, to: str, content: str,
             msg_type: str = "message", extra: dict = None) -> str:
        # 检查消息类型是否有效
        if msg_type not in VALID_MSG_TYPES:
            # 返回错误信息
            return f"Error: Invalid type '{msg_type}'. Valid: {VALID_MSG_TYPES}"
        # 创建消息字典
        msg = {
            "type": msg_type,
            "from": sender,
            "content": content,
            "timestamp": time.time(),
        }
        # 如果有额外数据，合并到消息中
        if extra:
            msg.update(extra)
        # 获取目标收件箱文件路径
        inbox_path = self.dir / f"{to}.jsonl"
        # 以追加模式打开文件
        with open(inbox_path, "a") as f:
            # 写入JSON格式的消息（每行一条）
            f.write(json.dumps(msg) + "\n")
        # 返回发送成功信息
        return f"Sent {msg_type} to {to}"

    # 读取并清空指定队友的收件箱
    def read_inbox(self, name: str) -> list:
        # 获取收件箱文件路径
        inbox_path = self.dir / f"{name}.jsonl"
        # 如果文件不存在，返回空列表
        if not inbox_path.exists():
            return []
        # 初始化消息列表
        messages = []
        # 逐行读取文件
        for line in inbox_path.read_text().strip().splitlines():
            # 跳过空行
            if line:
                # 解析JSON并添加到消息列表
                messages.append(json.loads(line))
        # 清空收件箱文件
        inbox_path.write_text("")
        # 返回消息列表
        return messages

    # 广播消息给所有队友
    def broadcast(self, sender: str, content: str, teammates: list) -> str:
        # 计数器
        count = 0
        # 遍历队友列表
        for name in teammates:
            # 跳过发送者本人
            if name != sender:
                # 发送广播消息
                self.send(sender, name, content, "broadcast")
                # 计数加一
                count += 1
        # 返回广播成功信息
        return f"Broadcast to {count} teammates"


# 创建全局MessageBus实例
BUS = MessageBus(INBOX_DIR)


# -- 任务板扫描 --
# 扫描未分配的任务
def scan_unclaimed_tasks() -> list:
    # 创建任务目录（如果不存在）
    TASKS_DIR.mkdir(exist_ok=True)
    # 未分配任务列表
    unclaimed = []
    # 遍历所有任务文件
    for f in sorted(TASKS_DIR.glob("task_*.json")):
        # 读取任务内容
        task = json.loads(f.read_text())
        # 检查任务是否满足条件：pending状态，无owner，无blockedBy
        if (task.get("status") == "pending"
                and not task.get("owner")
                and not task.get("blockedBy")):
            # 添加到未分配列表
            unclaimed.append(task)
    # 返回未分配任务列表
    return unclaimed


# 认领任务
def claim_task(task_id: int, owner: str) -> str:
    # 使用锁保护
    with _claim_lock:
        # 任务文件路径
        path = TASKS_DIR / f"task_{task_id}.json"
        # 如果文件不存在
        if not path.exists():
            # 返回错误信息
            return f"Error: Task {task_id} not found"
        # 读取任务内容
        task = json.loads(path.read_text())
        # 设置任务owner
        task["owner"] = owner
        # 更新任务状态为进行中
        task["status"] = "in_progress"
        # 写入更新后的任务
        path.write_text(json.dumps(task, indent=2))
    # 返回认领成功信息
    return f"Claimed task #{task_id} for {owner}"


# -- 上下文压缩后的身份重新注入 --
# 创建身份块
def make_identity_block(name: str, role: str, team_name: str) -> dict:
    # 返回身份块消息字典
    return {
        "role": "user",
        "content": f"<identity>You are '{name}', role: {role}, team: {team_name}. Continue your work.</identity>",
    }


# -- 自主队友管理器 --
class TeammateManager:
    # 初始化TeammateManager
    def __init__(self, team_dir: Path):
        # 保存团队目录
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
            # 读取并解析JSON配置
            return json.loads(self.config_path.read_text())
        # 返回默认配置
        return {"team_name": "default", "members": []}

    # 保存配置文件
    def _save_config(self):
        # 写入JSON格式的配置
        self.config_path.write_text(json.dumps(self.config, indent=2))

    # 查找团队成员
    def _find_member(self, name: str) -> dict:
        # 遍历成员列表
        for m in self.config["members"]:
            # 如果找到匹配的名称
            if m["name"] == name:
                # 返回成员信息
                return m
        # 未找到返回None
        return None

    # 设置成员状态
    def _set_status(self, name: str, status: str):
        # 查找成员
        member = self._find_member(name)
        # 如果成员存在
        if member:
            # 更新状态
            member["status"] = status
            # 保存配置
            self._save_config()

    # 生成（创建）新队友
    def spawn(self, name: str, role: str, prompt: str) -> str:
        # 查找成员
        member = self._find_member(name)
        # 如果成员存在
        if member:
            # 检查状态是否为idle或shutdown
            if member["status"] not in ("idle", "shutdown"):
                # 返回错误信息
                return f"Error: '{name}' is currently {member['status']}"
            # 更新状态为working
            member["status"] = "working"
            # 更新角色
            member["role"] = role
        else:
            # 创建新成员字典
            member = {"name": name, "role": role, "status": "working"}
            # 添加到成员列表
            self.config["members"].append(member)
        # 保存配置
        self._save_config()
        # 创建后台线程
        thread = threading.Thread(
            # 目标函数为_loop
            target=self._loop,
            # 传递参数：name, role, prompt
            args=(name, role, prompt),
            # 设置为守护线程
            daemon=True,
        )
        # 保存线程引用
        self.threads[name] = thread
        # 启动线程
        thread.start()
        # 返回成功信息
        return f"Spawned '{name}' (role: {role})"

    # 队友主循环
    def _loop(self, name: str, role: str, prompt: str):
        # 获取团队名称
        team_name = self.config["team_name"]
        # 构建系统提示词
        sys_prompt = (
            f"You are '{name}', role: {role}, team: {team_name}, at {WORKDIR}. "
            f"Use idle tool when you have no more work. You will auto-claim new tasks."
        )
        # 初始化消息列表
        messages = [{"role": "user", "content": prompt}]
        # 获取队友工具列表
        tools = self._teammate_tools()

        # 无限循环
        while True:
            # -- 工作阶段：标准代理循环 --
            # 最多运行50轮
            for _ in range(50):
                # 读取收件箱
                inbox = BUS.read_inbox(name)
                # 遍历收到的消息
                for msg in inbox:
                    # 如果是关闭请求
                    if msg.get("type") == "shutdown_request":
                        # 设置状态为关闭
                        self._set_status(name, "shutdown")
                        # 退出函数
                        return
                    # 将消息添加到历史
                    messages.append({"role": "user", "content": json.dumps(msg)})
                try:
                    # 调用LLM API
                    response = client.messages.create(
                        # 模型ID
                        model=MODEL,
                        # 系统提示词
                        system=sys_prompt,
                        # 消息历史
                        messages=messages,
                        # 工具列表
                        tools=tools,
                        # 最大令牌数
                        max_tokens=8000,
                    )
                except Exception:
                    # 发生异常则设置状态为空闲
                    self._set_status(name, "idle")
                    # 退出函数
                    return
                # 将LLM响应添加到消息历史
                messages.append({"role": "assistant", "content": response.content})
                # 如果不是工具调用，跳出循环
                if response.stop_reason != "tool_use":
                    break
                # 工具结果列表
                results = []
                # 空闲请求标志
                idle_requested = False
                # 遍历响应内容
                for block in response.content:
                    # 如果是工具使用块
                    if block.type == "tool_use":
                        # 如果是空闲工具
                        if block.name == "idle":
                            # 设置空闲标志
                            idle_requested = True
                            # 设置输出信息
                            output = "Entering idle phase. Will poll for new tasks."
                        else:
                            # 执行工具
                            output = self._exec(name, block.name, block.input)
                        # 打印工具执行信息
                        print(f"  [{name}] {block.name}: {str(output)[:120]}")
                        # 添加工具结果
                        results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": str(output),
                        })
                # 将工具结果添加到消息历史
                messages.append({"role": "user", "content": results})
                # 如果请求了空闲，跳出循环
                if idle_requested:
                    break

            # -- 空闲阶段：轮询收件箱消息和未分配任务 --
            # 设置状态为空闲
            self._set_status(name, "idle")
            # 恢复工作标志
            resume = False
            # 计算轮询次数
            polls = IDLE_TIMEOUT // max(POLL_INTERVAL, 1)
            # 轮询循环
            for _ in range(polls):
                # 等待轮询间隔
                time.sleep(POLL_INTERVAL)
                # 读取收件箱
                inbox = BUS.read_inbox(name)
                # 如果有消息
                if inbox:
                    # 遍历消息
                    for msg in inbox:
                        # 如果是关闭请求
                        if msg.get("type") == "shutdown_request":
                            # 设置状态为关闭
                            self._set_status(name, "shutdown")
                            # 退出函数
                            return
                        # 将消息添加到历史
                        messages.append({"role": "user", "content": json.dumps(msg)})
                    # 设置恢复标志
                    resume = True
                    # 跳出循环
                    break
                # 扫描未分配任务
                unclaimed = scan_unclaimed_tasks()
                # 如果有未分配任务
                if unclaimed:
                    # 获取第一个任务
                    task = unclaimed[0]
                    # 认领任务
                    claim_task(task["id"], name)
                    # 构建任务提示
                    task_prompt = (
                        f"<auto-claimed>Task #{task['id']}: {task['subject']}\n"
                        f"{task.get('description', '')}</auto-claimed>"
                    )
                    # 如果消息列表很短（上下文被压缩后）
                    if len(messages) <= 3:
                        # 在开头插入身份块
                        messages.insert(0, make_identity_block(name, role, team_name))
                        # 插入助手响应
                        messages.insert(1, {"role": "assistant", "content": f"I am {name}. Continuing."})
                    # 添加任务提示到消息
                    messages.append({"role": "user", "content": task_prompt})
                    # 添加助手响应
                    messages.append({"role": "assistant", "content": f"Claimed task #{task['id']}. Working on it."})
                    # 设置恢复标志
                    resume = True
                    # 跳出循环
                    break

            # 如果没有恢复
            if not resume:
                # 设置状态为关闭
                self._set_status(name, "shutdown")
                # 退出函数
                return
            # 设置状态为工作中
            self._set_status(name, "working")

    # 执行工具
    def _exec(self, sender: str, tool_name: str, args: dict) -> str:
        # 这些基础工具与s02相同
        # 如果是bash命令
        if tool_name == "bash":
            # 执行bash命令
            return _run_bash(args["command"])
        # 如果是读取文件
        if tool_name == "read_file":
            # 读取文件内容
            return _run_read(args["path"])
        # 如果是写入文件
        if tool_name == "write_file":
            # 写入文件内容
            return _run_write(args["path"], args["content"])
        # 如果是编辑文件
        if tool_name == "edit_file":
            # 编辑文件
            return _run_edit(args["path"], args["old_text"], args["new_text"])
        # 如果是发送消息
        if tool_name == "send_message":
            # 通过MessageBus发送消息
            return BUS.send(sender, args["to"], args["content"], args.get("msg_type", "message"))
        # 如果是读取收件箱
        if tool_name == "read_inbox":
            # 读取并返回JSON格式的收件箱内容
            return json.dumps(BUS.read_inbox(sender), indent=2)
        # 如果是关闭响应
        if tool_name == "shutdown_response":
            # 获取request_id
            req_id = args["request_id"]
            # 加锁访问共享字典
            with _tracker_lock:
                # 如果request_id存在，更新状态
                if req_id in shutdown_requests:
                    shutdown_requests[req_id]["status"] = "approved" if args["approve"] else "rejected"
            # 发送关闭响应消息给组长
            BUS.send(
                sender, "lead", args.get("reason", ""),
                "shutdown_response", {"request_id": req_id, "approve": args["approve"]},
            )
            # 返回处理结果
            return f"Shutdown {'approved' if args['approve'] else 'rejected'}"
        # 如果是计划审批
        if tool_name == "plan_approval":
            # 获取计划文本
            plan_text = args.get("plan", "")
            # 生成request_id
            req_id = str(uuid.uuid4())[:8]
            # 加锁访问共享字典
            with _tracker_lock:
                # 添加计划请求到追踪字典
                plan_requests[req_id] = {"from": sender, "plan": plan_text, "status": "pending"}
            # 发送计划审批请求给组长
            BUS.send(
                sender, "lead", plan_text, "plan_approval_response",
                {"request_id": req_id, "plan": plan_text},
            )
            # 返回提交成功信息
            return f"Plan submitted (request_id={req_id}). Waiting for approval."
        # 如果是认领任务
        if tool_name == "claim_task":
            # 认领指定任务
            return claim_task(args["task_id"], sender)
        # 未知工具
        return f"Unknown tool: {tool_name}"

    # 获取队友可用的工具列表
    def _teammate_tools(self) -> list:
        # 这些基础工具与s02相同
        return [
            # bash命令工具
            {"name": "bash", "description": "Run a shell command.",
             "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
            # 读取文件工具
            {"name": "read_file", "description": "Read file contents.",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
            # 写入文件工具
            {"name": "write_file", "description": "Write content to file.",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
            # 编辑文件工具
            {"name": "edit_file", "description": "Replace exact text in file.",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
            # 发送消息工具
            {"name": "send_message", "description": "Send message to a teammate.",
             "input_schema": {"type": "object", "properties": {"to": {"type": "string"}, "content": {"type": "string"}, "msg_type": {"type": "string", "enum": list(VALID_MSG_TYPES)}}, "required": ["to", "content"]}},
            # 读取收件箱工具
            {"name": "read_inbox", "description": "Read and drain your inbox.",
             "input_schema": {"type": "object", "properties": {}}},
            # 关闭响应工具
            {"name": "shutdown_response", "description": "Respond to a shutdown request.",
             "input_schema": {"type": "object", "properties": {"request_id": {"type": "string"}, "approve": {"type": "boolean"}, "reason": {"type": "string"}}, "required": ["request_id", "approve"]}},
            # 计划审批工具
            {"name": "plan_approval", "description": "Submit a plan for lead approval.",
             "input_schema": {"type": "object", "properties": {"plan": {"type": "string"}}, "required": ["plan"]}},
            # 空闲工具
            {"name": "idle", "description": "Signal that you have no more work. Enters idle polling phase.",
             "input_schema": {"type": "object", "properties": {}}},
            # 认领任务工具
            {"name": "claim_task", "description": "Claim a task from the task board by ID.",
             "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}},
        ]

    # 列出所有队友
    def list_all(self) -> str:
        # 如果没有成员
        if not self.config["members"]:
            # 返回提示信息
            return "No teammates."
        # 初始化行列表
        lines = [f"Team: {self.config['team_name']}"]
        # 遍历成员
        for m in self.config["members"]:
            # 添加成员信息行
            lines.append(f"  {m['name']} ({m['role']}): {m['status']}")
        # 返回格式化字符串
        return "\n".join(lines)

    # 获取所有成员名称列表
    def member_names(self) -> list:
        # 返回所有成员的名称列表
        return [m["name"] for m in self.config["members"]]


# 创建全局TeammateManager实例
TEAM = TeammateManager(TEAM_DIR)


# -- 基础工具实现（这些基础工具与s02相同）--
# 安全路径检查函数
def _safe_path(p: str) -> Path:
    # 解析路径
    path = (WORKDIR / p).resolve()
    # 检查路径是否在工作目录内
    if not path.is_relative_to(WORKDIR):
        # 抛出异常
        raise ValueError(f"Path escapes workspace: {p}")
    # 返回安全路径
    return path

# 执行bash命令的函数
def _run_bash(command: str) -> str:
    # 危险命令列表
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot"]
    # 检查是否包含危险命令
    if any(d in command for d in dangerous):
        # 返回错误信息
        return "Error: Dangerous command blocked"
    try:
        # 执行子进程
        r = subprocess.run(
            # 命令字符串
            command,
            # 使用shell
            shell=True,
            # 工作目录
            cwd=WORKDIR,
            # 捕获输出
            capture_output=True,
            # 文本模式
            text=True,
            # 超时时间
            timeout=120,
        )
        # 合并stdout和stderr
        out = (r.stdout + r.stderr).strip()
        # 返回输出结果，限制长度
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        # 超时返回错误信息
        return "Error: Timeout (120s)"

# 读取文件函数
def _run_read(path: str, limit: int = None) -> str:
    try:
        # 读取文件所有行
        lines = _safe_path(path).read_text().splitlines()
        # 如果限制了行数
        if limit and limit < len(lines):
            # 截断并添加提示
            lines = lines[:limit] + [f"... ({len(lines) - limit} more)"]
        # 返回限制长度后的内容
        return "\n".join(lines)[:50000]
    except Exception as e:
        # 返回错误信息
        return f"Error: {e}"

# 写入文件函数
def _run_write(path: str, content: str) -> str:
    try:
        # 获取安全路径
        fp = _safe_path(path)
        # 创建父目录
        fp.parent.mkdir(parents=True, exist_ok=True)
        # 写入内容
        fp.write_text(content)
        # 返回写入字节数
        return f"Wrote {len(content)} bytes"
    except Exception as e:
        # 返回错误信息
        return f"Error: {e}"

# 编辑文件函数
def _run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        # 获取安全路径
        fp = _safe_path(path)
        # 读取文件内容
        c = fp.read_text()
        # 检查旧文本是否存在
        if old_text not in c:
            # 返回错误信息
            return f"Error: Text not found in {path}"
        # 替换文本（只替换第一个匹配）
        fp.write_text(c.replace(old_text, new_text, 1))
        # 返回成功信息
        return f"Edited {path}"
    except Exception as e:
        # 返回错误信息
        return f"Error: {e}"


# -- 组长专用的协议处理器 --
# 处理关闭请求
def handle_shutdown_request(teammate: str) -> str:
    # 生成request_id
    req_id = str(uuid.uuid4())[:8]
    # 加锁访问共享字典
    with _tracker_lock:
        # 添加关闭请求到追踪字典
        shutdown_requests[req_id] = {"target": teammate, "status": "pending"}
    # 发送关闭请求消息给指定队友
    BUS.send(
        "lead", teammate, "Please shut down gracefully.",
        "shutdown_request", {"request_id": req_id},
    )
    # 返回请求发送成功信息
    return f"Shutdown request {req_id} sent to '{teammate}'"

# 处理计划审批
def handle_plan_review(request_id: str, approve: bool, feedback: str = "") -> str:
    # 先获取请求信息（不加锁）
    with _tracker_lock:
        # 从计划请求字典中获取请求
        req = plan_requests.get(request_id)
    # 如果请求不存在
    if not req:
        # 返回错误信息
        return f"Error: Unknown plan request_id '{request_id}'"
    # 更新请求状态
    with _tracker_lock:
        # 设置审批状态
        req["status"] = "approved" if approve else "rejected"
    # 发送审批响应给队友
    BUS.send(
        "lead", req["from"], feedback, "plan_approval_response",
        {"request_id": request_id, "approve": approve, "feedback": feedback},
    )
    # 返回处理结果
    return f"Plan {req['status']} for '{req['from']}'"

# 检查关闭请求状态
def _check_shutdown_status(request_id: str) -> str:
    # 加锁访问共享字典
    with _tracker_lock:
        # 返回JSON格式的请求状态
        return json.dumps(shutdown_requests.get(request_id, {"error": "not found"}))


# -- 组长工具调度器（14个工具）--
# 工具处理器字典
TOOL_HANDLERS = {
    # bash命令
    "bash":              lambda **kw: _run_bash(kw["command"]),
    # 读取文件
    "read_file":         lambda **kw: _run_read(kw["path"], kw.get("limit")),
    # 写入文件
    "write_file":        lambda **kw: _run_write(kw["path"], kw["content"]),
    # 编辑文件
    "edit_file":        lambda **kw: _run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    # 生成队友
    "spawn_teammate":    lambda **kw: TEAM.spawn(kw["name"], kw["role"], kw["prompt"]),
    # 列出队友
    "list_teammates":    lambda **kw: TEAM.list_all(),
    # 发送消息
    "send_message":      lambda **kw: BUS.send("lead", kw["to"], kw["content"], kw.get("msg_type", "message")),
    # 读取收件箱
    "read_inbox":        lambda **kw: json.dumps(BUS.read_inbox("lead"), indent=2),
    # 广播消息
    "broadcast":         lambda **kw: BUS.broadcast("lead", kw["content"], TEAM.member_names()),
    # 关闭请求
    "shutdown_request":  lambda **kw: handle_shutdown_request(kw["teammate"]),
    # 关闭响应
    "shutdown_response": lambda **kw: _check_shutdown_status(kw.get("request_id", "")),
    # 计划审批
    "plan_approval":     lambda **kw: handle_plan_review(kw["request_id"], kw["approve"], kw.get("feedback", "")),
    # 空闲
    "idle":              lambda **kw: "Lead does not idle.",
    # 认领任务
    "claim_task":        lambda **kw: claim_task(kw["task_id"], "lead"),
}

# 这些基础工具与s02相同
# 工具定义列表
TOOLS = [
    # bash命令工具
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    # 读取文件工具
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    # 写入文件工具
    {"name": "write_file", "description": "Write content to file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    # 编辑文件工具
    {"name": "edit_file", "description": "Replace exact text in file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    # 生成队友工具
    {"name": "spawn_teammate", "description": "Spawn an autonomous teammate.",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "role": {"type": "string"}, "prompt": {"type": "string"}}, "required": ["name", "role", "prompt"]}},
    # 列出队友工具
    {"name": "list_teammates", "description": "List all teammates.",
     "input_schema": {"type": "object", "properties": {}}},
    # 发送消息工具
    {"name": "send_message", "description": "Send a message to a teammate.",
     "input_schema": {"type": "object", "properties": {"to": {"type": "string"}, "content": {"type": "string"}, "msg_type": {"type": "string", "enum": list(VALID_MSG_TYPES)}}, "required": ["to", "content"]}},
    # 读取收件箱工具
    {"name": "read_inbox", "description": "Read and drain the lead's inbox.",
     "input_schema": {"type": "object", "properties": {}}},
    # 广播消息工具
    {"name": "broadcast", "description": "Send a message to all teammates.",
     "input_schema": {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]}},
    # 关闭请求工具
    {"name": "shutdown_request", "description": "Request a teammate to shut down.",
     "input_schema": {"type": "object", "properties": {"teammate": {"type": "string"}}, "required": ["teammate"]}},
    # 关闭响应工具
    {"name": "shutdown_response", "description": "Check shutdown request status.",
     "input_schema": {"type": "object", "properties": {"request_id": {"type": "string"}}, "required": ["request_id"]}},
    # 计划审批工具
    {"name": "plan_approval", "description": "Approve or reject a teammate's plan.",
     "input_schema": {"type": "object", "properties": {"request_id": {"type": "string"}, "approve": {"type": "boolean"}, "feedback": {"type": "string"}}, "required": ["request_id", "approve"]}},
    # 空闲工具
    {"name": "idle", "description": "Enter idle state (for lead -- rarely used).",
     "input_schema": {"type": "object", "properties": {}}},
    # 认领任务工具
    {"name": "claim_task", "description": "Claim a task from the board by ID.",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}},
]


# 代理主循环
def agent_loop(messages: list):
    # 无限循环
    while True:
        # 读取组长的收件箱
        inbox = BUS.read_inbox("lead")
        # 如果有消息
        if inbox:
            # 将收件箱消息添加到历史
            messages.append({
                "role": "user",
                # 包装为XML格式
                "content": f"<inbox>{json.dumps(inbox, indent=2)}</inbox>",
            })
            # 添加助手响应
            messages.append({
                "role": "assistant",
                "content": "Noted inbox messages.",
            })
        # 调用LLM API
        response = client.messages.create(
            # 模型ID
            model=MODEL,
            # 系统提示词
            system=SYSTEM,
            # 消息历史
            messages=messages,
            # 工具列表
            tools=TOOLS,
            # 最大令牌数
            max_tokens=8000,
        )
        # 将助手响应添加到消息历史
        messages.append({"role": "assistant", "content": response.content})
        # 如果不是工具调用，退出函数
        if response.stop_reason != "tool_use":
            return
        # 工具结果列表
        results = []
        # 遍历响应内容
        for block in response.content:
            # 如果是工具使用块
            if block.type == "tool_use":
                # 获取工具处理器
                handler = TOOL_HANDLERS.get(block.name)
                try:
                    # 执行工具
                    output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
                except Exception as e:
                    # 捕获异常
                    output = f"Error: {e}"
                # 打印工具执行信息
                print(f"> {block.name}: {str(output)[:200]}")
                # 添加工具结果
                results.append({
                    # 结果类型
                    "type": "tool_result",
                    # 工具使用ID
                    "tool_use_id": block.id,
                    # 结果内容
                    "content": str(output),
                })
        # 将工具结果添加到消息历史
        messages.append({"role": "user", "content": results})


# 主程序入口
if __name__ == "__main__":
    # 初始化历史消息列表
    history = []
    # 无限循环
    while True:
        try:
            # 获取用户输入
            query = input("\033[36ms11 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            # 捕获退出信号
            break
        # 如果输入为q、exit或空行，退出循环
        if query.strip().lower() in ("q", "exit", ""):
            break
        # 如果输入为/team，显示团队列表
        if query.strip() == "/team":
            # 打印团队信息
            print(TEAM.list_all())
            # 继续下一次循环
            continue
        # 如果输入为/inbox，显示收件箱
        if query.strip() == "/inbox":
            # 打印收件箱内容
            print(json.dumps(BUS.read_inbox("lead"), indent=2))
            # 继续下一次循环
            continue
        # 如果输入为/tasks，显示任务列表
        if query.strip() == "/tasks":
            # 创建任务目录（如果不存在）
            TASKS_DIR.mkdir(exist_ok=True)
            # 遍历所有任务文件
            for f in sorted(TASKS_DIR.glob("task_*.json")):
                # 读取任务内容
                t = json.loads(f.read_text())
                # 根据状态获取标记
                marker = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}.get(t["status"], "[?]")
                # 获取owner信息
                owner = f" @{t['owner']}" if t.get("owner") else ""
                # 打印任务信息
                print(f"  {marker} #{t['id']}: {t['subject']}{owner}")
            # 继续下一次循环
            continue
        # 将用户消息添加到历史
        history.append({"role": "user", "content": query})
        # 调用代理主循环
        agent_loop(history)
        # 打印空行
        print()
