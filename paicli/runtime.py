"""后续阶段共享的运行时基础类。"""

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
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class DurableTask:
    id: str
    prompt: str
    status: TaskStatus
    created_at: float
    updated_at: float
    result: str = ""
    error: str = ""


TaskRunner = Callable[[str, CancellationToken], str]
RuntimeListener = Callable[[dict[str, object]], None]


class DurableTaskManager:
    """Runs background prompts and persists every state transition."""

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
        self._tasks: dict[str, DurableTask] = {}
        self._tokens: dict[str, CancellationToken] = {}
        self._futures: dict[str, Future[None]] = {}
        self._listeners: list[RuntimeListener] = []
        self._lock = threading.RLock()
        self._load()

    def submit(self, prompt: str) -> DurableTask:
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
            self._persist()
            self._futures[task.id] = self._executor.submit(
                self._run,
                task.id,
            )
        self._emit(task)
        return task

    def get(self, task_id: str) -> DurableTask:
        with self._lock:
            try:
                return self._tasks[task_id]
            except KeyError as exc:
                raise KeyError(f"unknown task: {task_id}") from exc

    def list(self) -> list[DurableTask]:
        with self._lock:
            return sorted(self._tasks.values(), key=lambda task: task.created_at)

    def cancel(self, task_id: str) -> bool:
        with self._lock:
            task = self.get(task_id)
            if task.status in {
                TaskStatus.SUCCEEDED,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
            }:
                return False
            self._tokens[task_id].cancel()
            future = self._futures.get(task_id)
            if future:
                future.cancel()
            task.status = TaskStatus.CANCELLED
            task.updated_at = time.time()
            self._persist()
        self._emit(task)
        return True

    def wait(self, task_id: str, timeout: float | None = None) -> DurableTask:
        future = self._futures.get(task_id)
        if future:
            future.result(timeout=timeout)
        return self.get(task_id)

    def subscribe(self, listener: RuntimeListener) -> None:
        self._listeners.append(listener)

    def close(self) -> None:
        self._executor.shutdown(wait=True, cancel_futures=True)

    def _run(self, task_id: str) -> None:
        with self._lock:
            task = self._tasks[task_id]
            if task.status is TaskStatus.CANCELLED:
                return
            task.status = TaskStatus.RUNNING
            task.updated_at = time.time()
            self._persist()
        self._emit(task)
        try:
            result = self.runner(task.prompt, self._tokens[task_id])
            self._tokens[task_id].check()
        except CancelledError:
            status, result, error = TaskStatus.CANCELLED, "", ""
        except Exception as exc:
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
        temporary.replace(self.store)

    def _load(self) -> None:
        if not self.store.is_file():
            return
        for item in json.loads(self.store.read_text(encoding="utf-8")):
            status = TaskStatus(item["status"])
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
        event = {
            "type": "task_status",
            "task_id": task.id,
            "status": task.status.value,
        }
        for listener in tuple(self._listeners):
            listener(event)


class RuntimeApiServer:
    """Optional localhost API for submitting and observing durable tasks."""

    def __init__(
        self,
        manager: DurableTaskManager,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        self.manager = manager
        handler = self._handler(manager)
        self.server = ThreadingHTTPServer((host, port), handler)
        self.thread: threading.Thread | None = None

    @property
    def address(self) -> tuple[str, int]:
        host, port = self.server.server_address[:2]
        return str(host), int(port)

    def start(self) -> None:
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            name="paicli-runtime-api",
            daemon=True,
        )
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        if self.thread:
            self.thread.join(timeout=2)

    @staticmethod
    def _handler(manager: DurableTaskManager) -> type[BaseHTTPRequestHandler]:
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
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
                return None

            def _json(self, status: int, payload: dict[str, object]) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler
