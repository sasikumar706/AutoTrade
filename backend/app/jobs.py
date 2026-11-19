"""Background jobs with progress + cancel (used for options backtests: RUN / STOP)."""
from __future__ import annotations

import threading
import time
import traceback
import uuid
from typing import Any, Callable


class Job:
    def __init__(self, kind: str):
        self.id = uuid.uuid4().hex[:10]
        self.kind = kind
        self.status = "running"  # running | done | error | cancelled
        self.result: Any = None
        self.error: str | None = None
        self.progress = ""
        self.started = time.time()
        self.finished: float | None = None
        self.cancel = threading.Event()

    def snapshot(self, with_result: bool = True) -> dict:
        d = {"id": self.id, "kind": self.kind, "status": self.status, "error": self.error,
             "progress": self.progress, "started": self.started, "finished": self.finished}
        if with_result and self.status == "done":
            d["result"] = self.result
        return d


class JobRunner:
    def __init__(self, keep: int = 20):
        self.jobs: dict[str, Job] = {}
        self.keep = keep
        self.lock = threading.Lock()

    def start(self, kind: str, fn: Callable[[Job], Any]) -> Job:
        job = Job(kind)

        def run():
            from .options_backtest import Cancelled

            try:
                job.result = fn(job)
                job.status = "cancelled" if job.cancel.is_set() else "done"
            except Cancelled:
                job.status = "cancelled"
            except Exception as e:  # surfaced to the app
                job.status, job.error = "error", f"{e}"
                if not isinstance(e, (ValueError, PermissionError, KeyError)):
                    job.error = f"{type(e).__name__}: {e}"
                    traceback.print_exc()
            finally:
                job.finished = time.time()

        with self.lock:
            self.jobs[job.id] = job
            if len(self.jobs) > self.keep:
                for k in sorted(self.jobs, key=lambda k: self.jobs[k].started)[: len(self.jobs) - self.keep]:
                    if self.jobs[k].status != "running":
                        self.jobs.pop(k, None)
        threading.Thread(target=run, daemon=True).start()
        return job

    def get(self, jid: str) -> Job:
        if jid not in self.jobs:
            raise KeyError("Job not found")
        return self.jobs[jid]

    def cancel(self, jid: str) -> Job:
        j = self.get(jid)
        j.cancel.set()
        return j
