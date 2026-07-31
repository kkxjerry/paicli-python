"""后续阶段共享的运行时基础类。"""

from __future__ import annotations

import threading


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
