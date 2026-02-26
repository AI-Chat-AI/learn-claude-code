#!/usr/bin/env python3
"""
s12_worktree_task_isolation.py - 工作树与任务隔离

用于并行任务执行的目录级隔离。
任务是控制平面，工作树是执行平面。

    .tasks/task_12.json
      {
        "id": 12,
        "subject": "Implement auth refactor",
        "status": "in_progress",
        "worktree": "auth-refactor"
      }

    .worktrees/index.json
      {
        "worktrees": [
          {
            "name": "auth-refactor",
            "path": ".../.worktrees/auth-refactor",
            "branch": "wt/auth-refactor",
            "task_id": 12,
            "status": "active"
          }
        ]
      }

关键洞察："通过目录隔离，通过任务ID协调。"
"""

# 导入标准库
import json
import os
import re
import subprocess
import time
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


# 检测git仓库根目录
def detect_repo_root(cwd: Path) -> Path | None:
    """如果cwd在git仓库内，返回仓库根目录，否则返回None。"""
    try:
        # 执行git命令获取仓库根目录
        r = subprocess.run(
            # git命令参数
            ["git", "rev-parse", "--show-toplevel"],
            # 工作目录
            cwd=cwd,
            # 捕获输出
            capture_output=True,
            # 文本模式
            text=True,
            # 超时时间
            timeout=10,
        )
        # 如果命令失败，返回None
        if r.returncode != 0:
            return None
        # 解析输出路径
        root = Path(r.stdout.strip())
        # 检查路径是否存在
        return root if root.exists() else None
    except Exception:
        # 发生异常返回None
        return None


# 获取仓库根目录
REPO_ROOT = detect_repo_root(WORKDIR) or WORKDIR

# 系统提示词：编码代理角色
SYSTEM = (
    f"You are a coding agent at {WORKDIR}. "
    "Use task + worktree tools for multi-task work. "
    "For parallel or risky changes: create tasks, allocate worktree lanes, "
    "run commands in those lanes, then choose keep/remove for closeout. "
    "Use worktree_events when you need lifecycle visibility."
)


# -- EventBus: 用于可观测性的仅追加生命周期事件 --
class EventBus:
    # 初始化EventBus
    def __init__(self, event_log_path: Path):
        # 保存事件日志路径
        self.path = event_log_path
        # 创建父目录（如果不存在）
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 如果文件不存在，创建空文件
        if not self.path.exists():
            self.path.write_text("")

    # 发射事件
    def emit(
        self,
        event: str,
        task: dict | None = None,
        worktree: dict | None = None,
        error: str | None = None,
    ):
        # 创建事件载荷
        payload = {
            # 事件名称
            "event": event,
            # 时间戳
            "ts": time.time(),
            # 任务信息
            "task": task or {},
            # 工作树信息
            "worktree": worktree or {},
        }
        # 如果有错误信息
        if error:
            # 添加到载荷
            payload["error"] = error
        # 追加写入事件日志
        with self.path.open("a", encoding="utf-8") as f:
            # 写入JSON格式的事件（每行一条）
            f.write(json.dumps(payload) + "\n")

    # 获取最近的事件
    def list_recent(self, limit: int = 20) -> str:
        # 限制数量在1-200之间
        n = max(1, min(int(limit or 20), 200))
        # 读取所有行
        lines = self.path.read_text(encoding="utf-8").splitlines()
        # 获取最近的n行
        recent = lines[-n:]
        # 事件列表
        items = []
        # 遍历每一行
        for line in recent:
            try:
                # 解析JSON
                items.append(json.loads(line))
            except Exception:
                # 解析失败添加原始行
                items.append({"event": "parse_error", "raw": line})
        # 返回格式化的JSON字符串
        return json.dumps(items, indent=2)


# -- TaskManager: 具有可选工作树绑定的持久化任务板 --
class TaskManager:
    # 初始化TaskManager
    def __init__(self, tasks_dir: Path):
        # 保存任务目录
        self.dir = tasks_dir
        # 创建目录（如果不存在）
        self.dir.mkdir(parents=True, exist_ok=True)
        # 初始化下一个任务ID
        self._next_id = self._max_id() + 1

    # 获取最大任务ID
    def _max_id(self) -> int:
        # 任务ID列表
        ids = []
        # 遍历所有任务文件
        for f in self.dir.glob("task_*.json"):
            try:
                # 提取ID
                ids.append(int(f.stem.split("_")[1]))
            except Exception:
                # 忽略无效文件名
                pass
        # 返回最大ID或0
        return max(ids) if ids else 0

    # 获取任务文件路径
    def _path(self, task_id: int) -> Path:
        # 返回任务文件路径
        return self.dir / f"task_{task_id}.json"

    # 加载任务
    def _load(self, task_id: int) -> dict:
        # 获取文件路径
        path = self._path(task_id)
        # 如果文件不存在，抛出异常
        if not path.exists():
            raise ValueError(f"Task {task_id} not found")
        # 读取并解析JSON
        return json.loads(path.read_text())

    # 保存任务
    def _save(self, task: dict):
        # 写入JSON文件
        self._path(task["id"]).write_text(json.dumps(task, indent=2))

    # 创建新任务
    def create(self, subject: str, description: str = "") -> str:
        # 创建任务字典
        task = {
            # 任务ID
            "id": self._next_id,
            # 主题
            "subject": subject,
            # 描述
            "description": description,
            # 初始状态为pending
            "status": "pending",
            # 初始owner为空
            "owner": "",
            # 初始worktree为空
            "worktree": "",
            # 依赖任务列表
            "blockedBy": [],
            # 创建时间
            "created_at": time.time(),
            # 更新时间
            "updated_at": time.time(),
        }
        # 保存任务
        self._save(task)
        # 下一个任务ID递增
        self._next_id += 1
        # 返回JSON格式的任务信息
        return json.dumps(task, indent=2)

    # 获取任务详情
    def get(self, task_id: int) -> str:
        # 返回JSON格式的任务信息
        return json.dumps(self._load(task_id), indent=2)

    # 检查任务是否存在
    def exists(self, task_id: int) -> bool:
        # 返回任务文件是否存在
        return self._path(task_id).exists()

    # 更新任务
    def update(self, task_id: int, status: str = None, owner: str = None) -> str:
        # 加载任务
        task = self._load(task_id)
        # 如果提供了状态
        if status:
            # 验证状态是否有效
            if status not in ("pending", "in_progress", "completed"):
                # 抛出异常
                raise ValueError(f"Invalid status: {status}")
            # 更新状态
            task["status"] = status
        # 如果提供了owner
        if owner is not None:
            # 更新owner
            task["owner"] = owner
        # 更新时间戳
        task["updated_at"] = time.time()
        # 保存任务
        self._save(task)
        # 返回JSON格式的任务信息
        return json.dumps(task, indent=2)

    # 绑定工作树到任务
    def bind_worktree(self, task_id: int, worktree: str, owner: str = "") -> str:
        # 加载任务
        task = self._load(task_id)
        # 设置worktree
        task["worktree"] = worktree
        # 如果提供了owner
        if owner:
            # 更新owner
            task["owner"] = owner
        # 如果任务状态为pending，改为进行中
        if task["status"] == "pending":
            task["status"] = "in_progress"
        # 更新时间戳
        task["updated_at"] = time.time()
        # 保存任务
        self._save(task)
        # 返回JSON格式的任务信息
        return json.dumps(task, indent=2)

    # 解除工作树绑定
    def unbind_worktree(self, task_id: int) -> str:
        # 加载任务
        task = self._load(task_id)
        # 清空worktree
        task["worktree"] = ""
        # 更新时间戳
        task["updated_at"] = time.time()
        # 保存任务
        self._save(task)
        # 返回JSON格式的任务信息
        return json.dumps(task, indent=2)

    # 列出所有任务
    def list_all(self) -> str:
        # 任务列表
        tasks = []
        # 遍历所有任务文件
        for f in sorted(self.dir.glob("task_*.json")):
            # 读取任务内容
            tasks.append(json.loads(f.read_text()))
        # 如果没有任务
        if not tasks:
            # 返回提示信息
            return "No tasks."
        # 初始化行列表
        lines = []
        # 遍历任务
        for t in tasks:
            # 根据状态获取标记
            marker = {
                "pending": "[ ]",
                "in_progress": "[>]",
                "completed": "[x]",
            }.get(t["status"], "[?]")
            # 获取owner信息
            owner = f" owner={t['owner']}" if t.get("owner") else ""
            # 获取worktree信息
            wt = f" wt={t['worktree']}" if t.get("worktree") else ""
            # 添加任务信息行
            lines.append(f"{marker} #{t['id']}: {t['subject']}{owner}{wt}")
        # 返回格式化字符串
        return "\n".join(lines)


# 创建全局TaskManager实例
TASKS = TaskManager(REPO_ROOT / ".tasks")
# 创建全局EventBus实例
EVENTS = EventBus(REPO_ROOT / ".worktrees" / "events.jsonl")


# -- WorktreeManager: 创建/列表/运行/删除git工作树 + 生命周期索引 --
class WorktreeManager:
    # 初始化WorktreeManager
    def __init__(self, repo_root: Path, tasks: TaskManager, events: EventBus):
        # 保存仓库根目录
        self.repo_root = repo_root
        # 保存任务管理器
        self.tasks = tasks
        # 保存事件总线
        self.events = events
        # 工作树目录
        self.dir = repo_root / ".worktrees"
        # 创建目录（如果不存在）
        self.dir.mkdir(parents=True, exist_ok=True)
        # 索引文件路径
        self.index_path = self.dir / "index.json"
        # 如果索引文件不存在，创建默认索引
        if not self.index_path.exists():
            self.index_path.write_text(json.dumps({"worktrees": []}, indent=2))
        # 检查git仓库是否可用
        self.git_available = self._is_git_repo()

    # 检查是否是git仓库
    def _is_git_repo(self) -> bool:
        try:
            # 执行git命令检查
            r = subprocess.run(
                # git命令参数
                ["git", "rev-parse", "--is-inside-work-tree"],
                # 工作目录
                cwd=self.repo_root,
                # 捕获输出
                capture_output=True,
                # 文本模式
                text=True,
                # 超时时间
                timeout=10,
            )
            # 返回命令是否成功
            return r.returncode == 0
        except Exception:
            # 发生异常返回False
            return False

    # 运行git命令
    def _run_git(self, args: list[str]) -> str:
        # 如果git不可用
        if not self.git_available:
            # 抛出异常
            raise RuntimeError("Not in a git repository. worktree tools require git.")
        # 执行git命令
        r = subprocess.run(
            # git命令参数
            ["git", *args],
            # 工作目录
            cwd=self.repo_root,
            # 捕获输出
            capture_output=True,
            # 文本模式
            text=True,
            # 超时时间
            timeout=120,
        )
        # 如果命令失败
        if r.returncode != 0:
            # 获取错误信息
            msg = (r.stdout + r.stderr).strip()
            # 抛出异常
            raise RuntimeError(msg or f"git {' '.join(args)} failed")
        # 返回输出结果
        return (r.stdout + r.stderr).strip() or "(no output)"

    # 加载索引
    def _load_index(self) -> dict:
        # 读取并解析索引文件
        return json.loads(self.index_path.read_text())

    # 保存索引
    def _save_index(self, data: dict):
        # 写入索引文件
        self.index_path.write_text(json.dumps(data, indent=2))

    # 查找工作树
    def _find(self, name: str) -> dict | None:
        # 加载索引
        idx = self._load_index()
        # 遍历工作树列表
        for wt in idx.get("worktrees", []):
            # 如果找到匹配名称
            if wt.get("name") == name:
                # 返回工作树信息
                return wt
        # 未找到返回None
        return None

    # 验证工作树名称
    def _validate_name(self, name: str):
        # 检查名称是否符合规范
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,40}", name or ""):
            # 抛出异常
            raise ValueError(
                "Invalid worktree name. Use 1-40 chars: letters, numbers, ., _, -"
            )

    # 创建工作树
    def create(self, name: str, task_id: int = None, base_ref: str = "HEAD") -> str:
        # 验证名称
        self._validate_name(name)
        # 如果工作树已存在
        if self._find(name):
            # 抛出异常
            raise ValueError(f"Worktree '{name}' already exists in index")
        # 如果提供了task_id但任务不存在
        if task_id is not None and not self.tasks.exists(task_id):
            # 抛出异常
            raise ValueError(f"Task {task_id} not found")

        # 工作树路径
        path = self.dir / name
        # 分支名称
        branch = f"wt/{name}"
        # 发射创建前事件
        self.events.emit(
            "worktree.create.before",
            task={"id": task_id} if task_id is not None else {},
            worktree={"name": name, "base_ref": base_ref},
        )
        try:
            # 执行git worktree add命令
            self._run_git(["worktree", "add", "-b", branch, str(path), base_ref])

            # 创建工作树条目
            entry = {
                # 名称
                "name": name,
                # 路径
                "path": str(path),
                # 分支
                "branch": branch,
                # 任务ID
                "task_id": task_id,
                # 状态
                "status": "active",
                # 创建时间
                "created_at": time.time(),
            }

            # 加载索引
            idx = self._load_index()
            # 添加工作树到索引
            idx["worktrees"].append(entry)
            # 保存索引
            self._save_index(idx)

            # 如果提供了task_id，绑定工作树到任务
            if task_id is not None:
                self.tasks.bind_worktree(task_id, name)

            # 发射创建后事件
            self.events.emit(
                "worktree.create.after",
                task={"id": task_id} if task_id is not None else {},
                worktree={
                    "name": name,
                    "path": str(path),
                    "branch": branch,
                    "status": "active",
                },
            )
            # 返回JSON格式的工作树信息
            return json.dumps(entry, indent=2)
        except Exception as e:
            # 发射创建失败事件
            self.events.emit(
                "worktree.create.failed",
                task={"id": task_id} if task_id is not None else {},
                worktree={"name": name, "base_ref": base_ref},
                error=str(e),
            )
            # 重新抛出异常
            raise

    # 列出所有工作树
    def list_all(self) -> str:
        # 加载索引
        idx = self._load_index()
        # 获取工作树列表
        wts = idx.get("worktrees", [])
        # 如果没有工作树
        if not wts:
            # 返回提示信息
            return "No worktrees in index."
        # 初始化行列表
        lines = []
        # 遍历工作树
        for wt in wts:
            # 获取任务ID信息
            suffix = f" task={wt['task_id']}" if wt.get("task_id") else ""
            # 添加工作树信息行
            lines.append(
                f"[{wt.get('status', 'unknown')}] {wt['name']} -> "
                f"{wt['path']} ({wt.get('branch', '-')}){suffix}"
            )
        # 返回格式化字符串
        return "\n".join(lines)

    # 获取工作树状态
    def status(self, name: str) -> str:
        # 查找工作树
        wt = self._find(name)
        # 如果不存在
        if not wt:
            # 返回错误信息
            return f"Error: Unknown worktree '{name}'"
        # 工作树路径
        path = Path(wt["path"])
        # 如果路径不存在
        if not path.exists():
            # 返回错误信息
            return f"Error: Worktree path missing: {path}"
        # 执行git status命令
        r = subprocess.run(
            # git命令参数
            ["git", "status", "--short", "--branch"],
            # 工作目录
            cwd=path,
            # 捕获输出
            capture_output=True,
            # 文本模式
            text=True,
            # 超时时间
            timeout=60,
        )
        # 获取输出文本
        text = (r.stdout + r.stderr).strip()
        # 返回状态或"Clean worktree"
        return text or "Clean worktree"

    # 在工作树中运行命令
    def run(self, name: str, command: str) -> str:
        # 危险命令列表
        dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
        # 检查是否包含危险命令
        if any(d in command for d in dangerous):
            # 返回错误信息
            return "Error: Dangerous command blocked"

        # 查找工作树
        wt = self._find(name)
        # 如果不存在
        if not wt:
            # 返回错误信息
            return f"Error: Unknown worktree '{name}'"
        # 工作树路径
        path = Path(wt["path"])
        # 如果路径不存在
        if not path.exists():
            # 返回错误信息
            return f"Error: Worktree path missing: {path}"

        try:
            # 执行命令
            r = subprocess.run(
                # 命令字符串
                command,
                # 使用shell
                shell=True,
                # 工作目录
                cwd=path,
                # 捕获输出
                capture_output=True,
                # 文本模式
                text=True,
                # 超时时间
                timeout=300,
            )
            # 合并输出
            out = (r.stdout + r.stderr).strip()
            # 返回输出结果，限制长度
            return out[:50000] if out else "(no output)"
        except subprocess.TimeoutExpired:
            # 超时返回错误信息
            return "Error: Timeout (300s)"

    # 删除工作树
    def remove(self, name: str, force: bool = False, complete_task: bool = False) -> str:
        # 查找工作树
        wt = self._find(name)
        # 如果不存在
        if not wt:
            # 返回错误信息
            return f"Error: Unknown worktree '{name}'"

        # 发射删除前事件
        self.events.emit(
            "worktree.remove.before",
            task={"id": wt.get("task_id")} if wt.get("task_id") is not None else {},
            worktree={"name": name, "path": wt.get("path")},
        )
        try:
            # git命令参数列表
            args = ["worktree", "remove"]
            # 如果强制删除
            if force:
                # 添加--force参数
                args.append("--force")
            # 添加路径
            args.append(wt["path"])
            # 执行git worktree remove命令
            self._run_git(args)

            # 如果需要完成任务
            if complete_task and wt.get("task_id") is not None:
                # 获取任务ID
                task_id = wt["task_id"]
                # 获取任务修改前的信息
                before = json.loads(self.tasks.get(task_id))
                # 更新任务状态为已完成
                self.tasks.update(task_id, status="completed")
                # 解除工作树绑定
                self.tasks.unbind_worktree(task_id)
                # 发射任务完成事件
                self.events.emit(
                    "task.completed",
                    task={
                        "id": task_id,
                        "subject": before.get("subject", ""),
                        "status": "completed",
                    },
                    worktree={"name": name},
                )

            # 加载索引
            idx = self._load_index()
            # 遍历工作树列表
            for item in idx.get("worktrees", []):
                # 如果找到匹配的工作树
                if item.get("name") == name:
                    # 更新状态为removed
                    item["status"] = "removed"
                    # 添加删除时间戳
                    item["removed_at"] = time.time()
            # 保存索引
            self._save_index(idx)

            # 发射删除后事件
            self.events.emit(
                "worktree.remove.after",
                task={"id": wt.get("task_id")} if wt.get("task_id") is not None else {},
                worktree={"name": name, "path": wt.get("path"), "status": "removed"},
            )
            # 返回成功信息
            return f"Removed worktree '{name}'"
        except Exception as e:
            # 发射删除失败事件
            self.events.emit(
                "worktree.remove.failed",
                task={"id": wt.get("task_id")} if wt.get("task_id") is not None else {},
                worktree={"name": name, "path": wt.get("path")},
                error=str(e),
            )
            # 重新抛出异常
            raise

    # 保留工作树（不删除）
    def keep(self, name: str) -> str:
        # 查找工作树
        wt = self._find(name)
        # 如果不存在
        if not wt:
            # 返回错误信息
            return f"Error: Unknown worktree '{name}'"

        # 加载索引
        idx = self._load_index()
        # 保留的工作树
        kept = None
        # 遍历工作树列表
        for item in idx.get("worktrees", []):
            # 如果找到匹配的工作树
            if item.get("name") == name:
                # 更新状态为kept
                item["status"] = "kept"
                # 添加保留时间戳
                item["kept_at"] = time.time()
                # 保存引用
                kept = item
        # 保存索引
        self._save_index(idx)

        # 发射保留事件
        self.events.emit(
            "worktree.keep",
            task={"id": wt.get("task_id")} if wt.get("task_id") is not None else {},
            worktree={
                "name": name,
                "path": wt.get("path"),
                "status": "kept",
            },
        )
        # 返回JSON格式的工作树信息或错误信息
        return json.dumps(kept, indent=2) if kept else f"Error: Unknown worktree '{name}'"


# 创建全局WorktreeManager实例
WORKTREES = WorktreeManager(REPO_ROOT, TASKS, EVENTS)


# -- 基础工具（保持最小化，与之前的会话相同风格）--
# 安全路径检查函数
def safe_path(p: str) -> Path:
    # 解析路径
    path = (WORKDIR / p).resolve()
    # 检查路径是否在工作目录内
    if not path.is_relative_to(WORKDIR):
        # 抛出异常
        raise ValueError(f"Path escapes workspace: {p}")
    # 返回安全路径
    return path

# 执行bash命令的函数
def run_bash(command: str) -> str:
    # 危险命令列表
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
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
def run_read(path: str, limit: int = None) -> str:
    try:
        # 读取文件所有行
        lines = safe_path(path).read_text().splitlines()
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
def run_write(path: str, content: str) -> str:
    try:
        # 获取安全路径
        fp = safe_path(path)
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
def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        # 获取安全路径
        fp = safe_path(path)
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


# 工具处理器字典
TOOL_HANDLERS = {
    # bash命令
    "bash": lambda **kw: run_bash(kw["command"]),
    # 读取文件
    "read_file": lambda **kw: run_read(kw["path"], kw.get("limit")),
    # 写入文件
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    # 编辑文件
    "edit_file": lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    # 创建任务
    "task_create": lambda **kw: TASKS.create(kw["subject"], kw.get("description", "")),
    # 列出任务
    "task_list": lambda **kw: TASKS.list_all(),
    # 获取任务
    "task_get": lambda **kw: TASKS.get(kw["task_id"]),
    # 更新任务
    "task_update": lambda **kw: TASKS.update(kw["task_id"], kw.get("status"), kw.get("owner")),
    # 绑定工作树到任务
    "task_bind_worktree": lambda **kw: TASKS.bind_worktree(kw["task_id"], kw["worktree"], kw.get("owner", "")),
    # 创建工作树
    "worktree_create": lambda **kw: WORKTREES.create(kw["name"], kw.get("task_id"), kw.get("base_ref", "HEAD")),
    # 列出工作树
    "worktree_list": lambda **kw: WORKTREES.list_all(),
    # 工作树状态
    "worktree_status": lambda **kw: WORKTREES.status(kw["name"]),
    # 在工作树中运行命令
    "worktree_run": lambda **kw: WORKTREES.run(kw["name"], kw["command"]),
    # 保留工作树
    "worktree_keep": lambda **kw: WORKTREES.keep(kw["name"]),
    # 删除工作树
    "worktree_remove": lambda **kw: WORKTREES.remove(kw["name"], kw.get("force", False), kw.get("complete_task", False)),
    # 查看工作树事件
    "worktree_events": lambda **kw: EVENTS.list_recent(kw.get("limit", 20)),
}

# 工具定义列表
TOOLS = [
    # bash命令工具
    {
        "name": "bash",
        "description": "Run a shell command in the current workspace (blocking).",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
    # 读取文件工具
    {
        "name": "read_file",
        "description": "Read file contents.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["path"],
        },
    },
    # 写入文件工具
    {
        "name": "write_file",
        "description": "Write content to file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    # 编辑文件工具
    {
        "name": "edit_file",
        "description": "Replace exact text in file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
            "required": ["path", "old_text", "new_text"],
        },
    },
    # 创建任务工具
    {
        "name": "task_create",
        "description": "Create a new task on the shared task board.",
        "input_schema": {
            "type": "object",
            "properties": {
                "subject": {"type": "string"},
                "description": {"type": "string"},
            },
            "required": ["subject"],
        },
    },
    # 列出任务工具
    {
        "name": "task_list",
        "description": "List all tasks with status, owner, and worktree binding.",
        "input_schema": {"type": "object", "properties": {}},
    },
    # 获取任务工具
    {
        "name": "task_get",
        "description": "Get task details by ID.",
        "input_schema": {
            "type": "object",
            "properties": {"task_id": {"type": "integer"}},
            "required": ["task_id"],
        },
    },
    # 更新任务工具
    {
        "name": "task_update",
        "description": "Update task status or owner.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer"},
                "status": {
                    "type": "string",
                    "enum": ["pending", "in_progress", "completed"],
                },
                "owner": {"type": "string"},
            },
            "required": ["task_id"],
        },
    },
    # 绑定工作树工具
    {
        "name": "task_bind_worktree",
        "description": "Bind a task to a worktree name.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer"},
                "worktree": {"type": "string"},
                "owner": {"type": "string"},
            },
            "required": ["task_id", "worktree"],
        },
    },
    # 创建工作树工具
    {
        "name": "worktree_create",
        "description": "Create a git worktree and optionally bind it to a task.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "task_id": {"type": "integer"},
                "base_ref": {"type": "string"},
            },
            "required": ["name"],
        },
    },
    # 列出工作树工具
    {
        "name": "worktree_list",
        "description": "List worktrees tracked in .worktrees/index.json.",
        "input_schema": {"type": "object", "properties": {}},
    },
    # 工作树状态工具
    {
        "name": "worktree_status",
        "description": "Show git status for one worktree.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    # 工作树运行命令工具
    {
        "name": "worktree_run",
        "description": "Run a shell command in a named worktree directory.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "command": {"type": "string"},
            },
            "required": ["name", "command"],
        },
    },
    # 删除工作树工具
    {
        "name": "worktree_remove",
        "description": "Remove a worktree and optionally mark its bound task completed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "force": {"type": "boolean"},
                "complete_task": {"type": "boolean"},
            },
            "required": ["name"],
        },
    },
    # 保留工作树工具
    {
        "name": "worktree_keep",
        "description": "Mark a worktree as kept in lifecycle state without removing it.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    # 工作树事件工具
    {
        "name": "worktree_events",
        "description": "List recent worktree/task lifecycle events from .worktrees/events.jsonl.",
        "input_schema": {
            "type": "object",
            "properties": {"limit": {"type": "integer"}},
        },
    },
]


# 代理主循环
def agent_loop(messages: list):
    # 无限循环
    while True:
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
                results.append(
                    {
                        # 结果类型
                        "type": "tool_result",
                        # 工具使用ID
                        "tool_use_id": block.id,
                        # 结果内容
                        "content": str(output),
                    }
                )
        # 将工具结果添加到消息历史
        messages.append({"role": "user", "content": results})


# 主程序入口
if __name__ == "__main__":
    # 打印仓库根目录
    print(f"Repo root for s12: {REPO_ROOT}")
    # 如果git不可用
    if not WORKTREES.git_available:
        # 打印提示信息
        print("Note: Not in a git repo. worktree_* tools will return errors.")

    # 初始化历史消息列表
    history = []
    # 无限循环
    while True:
        try:
            # 获取用户输入
            query = input("\033[36ms12 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            # 捕获退出信号
            break
        # 如果输入为q、exit或空行，退出循环
        if query.strip().lower() in ("q", "exit", ""):
            break
        # 将用户消息添加到历史
        history.append({"role": "user", "content": query})
        # 调用代理主循环
        agent_loop(history)
        # 打印空行
        print()
