"""Phase 20：可持久化的后台任务与可选本地 Runtime API。

任务状态机：

    QUEUED -> RUNNING -> SUCCEEDED
                       -> FAILED
       |       |
       +-------+-------> CANCELLED

DurableTaskManager 用线程池执行任务，每次状态变化都将整个任务表
原子替换到 JSON 文件。RuntimeApiServer 只是绑定 localhost 的最小 HTTP 外壳，
当前没有鉴权、TLS、跨进程队列或分布式锁，不应直接暴露到公网。
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass
from enum import Enum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable


class CancelledError(RuntimeError):
    """任务收到取消信号后在安全检查点抛出的异常。"""


class CancellationToken:
    """基于 threading.Event 的线程安全协作式取消令牌。"""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        # cancel 不会强制终止线程，只设置一个可被观察的信号。
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        # 长任务需在合适位置主动调用 check，这就是“协作式”。
        if self.cancelled:
            raise CancelledError("operation cancelled")


class TaskStatus(str, Enum):
    """后台任务的五种可持久化状态。"""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class DurableTask:
    """一个可持久化任务的当前快照。"""

    id: str
    prompt: str
    status: TaskStatus
    created_at: float
    updated_at: float
    result: str = ""
    error: str = ""


# runner 接收 prompt 和取消令牌；真正的 Agent.run 可包装成这个形状。
TaskRunner = Callable[[str, CancellationToken], str]
# listener 目前用普通字典接收状态事件，可供 UI 或日志订阅。
RuntimeListener = Callable[[dict[str, object]], None]


class DurableTaskManager:
    """在后台运行 prompt，并持久化每次状态转换。"""

    def __init__(
        self,
        store: str | Path,
        runner: TaskRunner,
        *,
        max_workers: int = 2,
    ) -> None:
        self.store = Path(store)
        self.runner = runner
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        # task 是持久化数据；token/future 只在本次进程中有效。
        self._tasks: dict[str, DurableTask] = {}
        self._tokens: dict[str, CancellationToken] = {}
        self._futures: dict[str, Future[None]] = {}
        self._listeners: list[RuntimeListener] = []
        # 用 RLock 是因为 cancel() 持锁时会再调用同样加锁的 get()。
        self._lock = threading.RLock()
        # 启动时先恢复历史任务，但不会自动重试未完任务。
        self._load()

    def submit(self, prompt: str) -> DurableTask:
        """创建 QUEUED 任务、立即持久化，再提交给线程池。"""

        if not prompt.strip():
            raise ValueError("task prompt cannot be empty")
        now = time.time()
        task = DurableTask(
            uuid.uuid4().hex,
            prompt,
            TaskStatus.QUEUED,
            now,
            now,
        )
        token = CancellationToken()
        with self._lock:
            self._tasks[task.id] = task
            self._tokens[task.id] = token
            # 先落盘再提交，避免线程刚开始时任务尚未入库。
            self._persist()
            self._futures[task.id] = self._executor.submit(
                self._run,
                task.id,
            )
        # listener 在锁外调用，避免慢 listener 阻塞内部状态操作。
        self._emit(task)
        return task

    def get(self, task_id: str) -> DurableTask:
        """按 id 取任务；未知 id 转成信息更明确的 KeyError。"""

        with self._lock:
            try:
                return self._tasks[task_id]
            except KeyError as exc:
                raise KeyError(f"unknown task: {task_id}") from exc

    def list(self) -> list[DurableTask]:
        """按创建时间从旧到新列出所有任务。"""

        with self._lock:
            return sorted(self._tasks.values(), key=lambda task: task.created_at)

    def cancel(self, task_id: str) -> bool:
        """尝试取消任务；已终结返回 False，取消信号发出返回 True。"""

        with self._lock:
            task = self.get(task_id)
            if task.status in {
                TaskStatus.SUCCEEDED,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
            }:
                return False
            # token.cancel 通知已运行任务；future.cancel 只可能取消还没开始的任务。
            self._tokens[task_id].cancel()
            future = self._futures.get(task_id)
            if future:
                future.cancel()
            # API 立即对外呈现 CANCELLED，runner 仍需在检查点合作退出。
            task.status = TaskStatus.CANCELLED
            task.updated_at = time.time()
            self._persist()
        self._emit(task)
        return True

    def wait(self, task_id: str, timeout: float | None = None) -> DurableTask:
        """等待线程任务结束，再返回最新持久化状态。"""

        future = self._futures.get(task_id)
        if future:
            future.result(timeout=timeout)
        return self.get(task_id)

    def subscribe(self, listener: RuntimeListener) -> None:
        """订阅之后的任务状态变化；本期没有 unsubscribe。"""

        self._listeners.append(listener)

    def close(self) -> None:
        """关闭线程池，等待已开始任务，取消尚未开始的 future。"""

        self._executor.shutdown(wait=True, cancel_futures=True)

    def _run(self, task_id: str) -> None:
        """工作线程入口：RUNNING -> runner -> 终态，每步都持久化。"""

        with self._lock:
            task = self._tasks[task_id]
            # 任务可能在排队时已被 cancel()标记，此时不再运行 runner。
            if task.status is TaskStatus.CANCELLED:
                return
            task.status = TaskStatus.RUNNING
            task.updated_at = time.time()
            self._persist()
        self._emit(task)
        try:
            result = self.runner(task.prompt, self._tokens[task_id])
            # 即使 runner 忘记检查，返回后也再检查一次，避免取消被记为成功。
            self._tokens[task_id].check()
        except CancelledError:
            status, result, error = TaskStatus.CANCELLED, "", ""
        except Exception as exc:
            # 保存异常类型+消息，不保存可能很大或含敏感路径的 traceback。
            status, result, error = (
                TaskStatus.FAILED,
                "",
                f"{type(exc).__name__}: {exc}",
            )
        else:
            status, error = TaskStatus.SUCCEEDED, ""

        with self._lock:
            task.status = status
            task.result = result
            task.error = error
            task.updated_at = time.time()
            self._persist()
        self._emit(task)

    def _persist(self) -> None:
        """先写临时文件，再用 replace 原子替换任务库。"""

        self.store.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.store.with_suffix(self.store.suffix + ".tmp")
        temporary.write_text(
            json.dumps(
                [
                    {**asdict(task), "status": task.status.value}
                    for task in self._tasks.values()
                ],
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        # 这比直接覆盖 store 更耐受中途崩溃，但仍不是多进程数据库。
        temporary.replace(self.store)

    def _load(self) -> None:
        """恢复历史终态；上次未完的任务标记为 FAILED。"""

        if not self.store.is_file():
            return
        for item in json.loads(self.store.read_text(encoding="utf-8")):
            status = TaskStatus(item["status"])
            # 重启后旧 future/token 已不存在，不能假装它仍在运行。
            if status in {TaskStatus.QUEUED, TaskStatus.RUNNING}:
                status = TaskStatus.FAILED
                item["error"] = "runtime stopped before task completed"
            task = DurableTask(
                id=item["id"],
                prompt=item["prompt"],
                status=status,
                created_at=float(item["created_at"]),
                updated_at=float(item["updated_at"]),
                result=item.get("result", ""),
                error=item.get("error", ""),
            )
            self._tasks[task.id] = task

    def _emit(self, task: DurableTask) -> None:
        """对 listener 发送最小状态事件。"""

        event = {
            "type": "task_status",
            "task_id": task.id,
            "status": task.status.value,
        }
        # 遍历快照，避免 listener 回调期间修改列表影响当次遍历。
        for listener in tuple(self._listeners):
            listener(event)


class RuntimeApiServer:
    """可选的 localhost API，用于提交和查询可持久任务。"""

    def __init__(
        self,
        manager: DurableTaskManager,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        self.manager = manager
        handler = self._handler(manager)
        # port=0 表示让操作系统选一个空闲端口，特别适合测试。
        self.server = ThreadingHTTPServer((host, port), handler)
        self.thread: threading.Thread | None = None

    @property
    def address(self) -> tuple[str, int]:
        """返回服务器实际绑定的 host/port。"""

        host, port = self.server.server_address[:2]
        return str(host), int(port)

    def start(self) -> None:
        """用 daemon 线程启动 HTTP 循环，不阻塞 CLI 主线程。"""

        self.thread = threading.Thread(
            target=self.server.serve_forever,
            name="paicli-runtime-api",
            daemon=True,
        )
        self.thread.start()

    def close(self) -> None:
        """停止 HTTP 循环、关闭 socket，并短暂等待服务线程退出。"""

        self.server.shutdown()
        self.server.server_close()
        if self.thread:
            self.thread.join(timeout=2)

    @staticmethod
    def _handler(manager: DurableTaskManager) -> type[BaseHTTPRequestHandler]:
        # 定义在工厂内，让 Handler 通过闭包使用指定 manager。
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                # POST /tasks {"prompt": "..."} -> 202 Accepted。
                if self.path != "/tasks":
                    self.send_error(404)
                    return
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
                try:
                    task = manager.submit(str(payload.get("prompt", "")))
                except ValueError as exc:
                    self._json(400, {"error": str(exc)})
                    return
                self._json(202, {"id": task.id, "status": task.status.value})

            def do_GET(self) -> None:
                # GET /tasks/{id} -> 任务完整快照；未知 id 返回 404。
                if not self.path.startswith("/tasks/"):
                    self.send_error(404)
                    return
                task_id = self.path.removeprefix("/tasks/")
                try:
                    task = manager.get(task_id)
                except KeyError:
                    self.send_error(404)
                    return
                self._json(
                    200,
                    {**asdict(task), "status": task.status.value},
                )

            def log_message(self, _format: str, *_args: object) -> None:
                # 禁用 BaseHTTPRequestHandler 默认的 stderr 访问日志，避免打乱 CLI UI。
                return None

            def _json(self, status: int, payload: dict[str, object]) -> None:
                # 所有成功/业务错误响应统一走 UTF-8 JSON。
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler
