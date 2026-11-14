# -*- coding: utf-8 -*-
"""简单的进度事件分发（供前端 SSE 订阅）"""
import queue
import threading
import time


class EventHub:
    def __init__(self):
        self._lock = threading.Lock()
        self._subs = []
        self.state = {}

    def publish(self, task: str, payload: dict):
        data = dict(payload)
        data["task"] = task
        data["ts"] = time.time()
        with self._lock:
            self.state[task] = data
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(data)
            except Exception:
                pass

    def subscribe(self) -> queue.Queue:
        q = queue.Queue()
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)
