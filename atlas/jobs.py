"""Background run management: bounded concurrency, progress, cancellation."""
from __future__ import annotations

import threading
import time

from .db import Database, now
from .engine import Progress, ResearchEngine


class JobManager:
    def __init__(self, db: Database, engine_factory, max_concurrent: int = 1):
        self.db = db
        self.engine_factory = engine_factory          # () -> ResearchEngine (fresh settings each run)
        self.sem = threading.BoundedSemaphore(max_concurrent)
        self.jobs: dict[int, tuple[Progress, threading.Event, threading.Thread]] = {}
        self.lock = threading.Lock()
        # Runs left 'running' by a previous process were interrupted.
        db.ex("UPDATE runs SET status='interrupted', finished_at=? WHERE status IN ('running','queued')", (now(),))

    def start(self, run_id: int) -> Progress:
        prog, cancel = Progress(), threading.Event()
        t = threading.Thread(target=self._run, args=(run_id, prog, cancel), daemon=True, name=f"atlas-run-{run_id}")
        with self.lock:
            self.jobs[run_id] = (prog, cancel, t)
        t.start()
        return prog

    def _run(self, run_id, prog, cancel):
        prog.set("queued", "Waiting for a free research slot", 0)
        with self.sem:
            if cancel.is_set():
                self.db.ex("UPDATE runs SET status='cancelled', finished_at=? WHERE id=?", (now(), run_id))
                prog.status, prog.finished = "cancelled", time.time()
                prog.set("done", "Cancelled before start", 100)
                return
            try:
                self.engine_factory().execute(run_id, prog, cancel)
            except Exception as e:                           # noqa: BLE001
                from .security import safe_error
                self.db.ex("UPDATE runs SET status='failed', finished_at=?, error=? WHERE id=?", (now(), safe_error(e), run_id))
                prog.status = "failed"
                prog.set("done", "Run failed", 100)

    def cancel(self, run_id: int) -> bool:
        with self.lock:
            j = self.jobs.get(run_id)
        if not j or not j[2].is_alive():
            return False
        j[1].set()
        j[0].set("cancelling", "Cancellation requested", None)
        return True

    def progress(self, run_id: int) -> dict:
        with self.lock:
            j = self.jobs.get(run_id)
        if j:
            return j[0].snapshot()
        row = self.db.one("SELECT status FROM runs WHERE id=?", (run_id,))
        if not row:
            raise KeyError(run_id)
        return {"stage": "done" if row["status"] not in ("queued", "running") else row["status"], "message": row["status"],
                "percent": 100 if row["status"] not in ("queued", "running") else 0, "counts": {}, "events": [],
                "elapsed_s": 0, "provider": "", "status": row["status"]}

    def wait(self, run_id: int, timeout: float = 30) -> None:
        with self.lock:
            j = self.jobs.get(run_id)
        if j:
            j[2].join(timeout)
