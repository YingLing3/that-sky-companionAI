"""Single in-memory cleanup task slot. No config, credentials or task files."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
import time
import uuid

ACTIONS = frozenset(("preview", "execute", "restore-preview", "restore", "purge-preview", "purge"))
CATEGORIES = ("terrain", "catalog", "names", "captures", "props")
_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")


def _error(code, message, **fields):
    return {"ok": False, "error_code": code, "message": message, **fields}


def _exception_result(error):
    from data_cleanup import CleanupError
    cause = {"type": type(error).__name__}
    if type(getattr(error, "errno", None)) is int:
        cause["errno"] = error.errno
    if isinstance(error, CleanupError):
        return _error(error.code, error.message, cause=cause)
    return _error("cleanup_internal", "数据维护异常终止；请保留回收区记录并重新预览检查", cause=cause)


def validate_request(action, payload):
    if action not in ACTIONS:
        return None, _error("unknown_action", "未知维护操作")
    if not isinstance(payload, dict):
        return None, _error("bad_request", "清理请求格式无效")
    result = {}
    if action == "preview":
        selection = payload.get("selection")
        if not isinstance(selection, dict):
            return None, _error("bad_selection", "请选择要清理的数据类别")
        if set(selection) - set(CATEGORIES) or any(type(v) is not bool for v in selection.values()):
            return None, _error("invalid_selection", "清理选项必须是五类布尔值")
        selection = {key: selection.get(key, False) for key in CATEGORIES}
        if not any(selection.values()):
            return None, _error("empty_selection", "请至少选择一项")
        result["selection"] = selection
    elif action in ("execute", "restore", "purge"):
        plan = payload.get("plan_id")
        if not isinstance(plan, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", plan):
            return None, _error("invalid_plan", "请先预览并确认本次操作")
        if payload.get("confirm") is not True:
            return None, _error("confirmation_required", "需要二次确认")
        result.update(plan_id=plan, confirm=True)
    request_id = payload.get("request_id")
    if not isinstance(request_id, str) or not _ID.fullmatch(request_id):
        return None, _error("invalid_request_id", "请使用有效的维护请求编号")
    result["request_id"] = request_id
    return result, None


class CleanupTasks:
    """Serialize maintenance under the shared operation lock, not the polling lock."""

    def __init__(self, *, operation_lock, worker, preflight, clock=time.time,
                 max_tasks=32, retention_seconds=1800, thread_factory=threading.Thread):
        self.operation_lock = operation_lock
        self.worker = worker
        self.preflight = preflight
        self.clock = clock
        self.max_tasks = max(1, min(int(max_tasks), 128))
        self.retention = max(1, min(float(retention_seconds), 86400))
        self.thread_factory = thread_factory
        self._lock = threading.Lock()
        self._tasks = {}
        self._requests = {}
        self._active = None

    def _snapshot(self, task):
        return copy.deepcopy({key: value for key, value in task.items() if not key.startswith("_")})

    def _remove(self, task_id):
        task = self._tasks.pop(task_id)
        self._requests.pop((task["action"], task["request_id"]), None)

    def _prune(self):
        now = self.clock()
        for key, task in list(self._tasks.items()):
            if not task["running"] and now - task["finished_at"] > self.retention:
                self._remove(key)
        completed = sorted((task for task in self._tasks.values() if not task["running"]),
                           key=lambda task: task["finished_at"])
        while len(self._tasks) > self.max_tasks and completed:
            self._remove(completed.pop(0)["id"])

    def _busy(self):
        task = self._tasks.get(self._active)
        return _error("cleanup_busy", "上一个维护或启动操作还没完成",
                      task=self._snapshot(task) if task else None,
                      task_id=task["id"] if task else None)

    def submit(self, action, payload, *, worker=None, scope=None):
        normalized, error = validate_request(action, payload)
        if error:
            return error
        request_id = normalized.pop("request_id")
        fingerprint = hashlib.sha256(json.dumps({"payload": normalized, "scope": scope}, sort_keys=True,
                                                separators=(",", ":")).encode()).hexdigest()
        request_key = (action, request_id)
        with self._lock:
            self._prune()
            prior = self._tasks.get(self._requests.get(request_key))
            if prior:
                if fingerprint != prior["_fingerprint"]:
                    return _error("request_conflict", "同一请求编号不能更换维护内容",
                                  task_id=prior["id"], task=self._snapshot(prior))
                return {"ok": True, "task_id": prior["id"], "task": self._snapshot(prior), "reused": True}
            if self._active is not None:
                return self._busy()
        if not self.operation_lock.acquire(blocking=False):
            with self._lock:
                return self._busy()
        transferred = False
        try:
            # A fast prior worker can finish between the first lookup and acquiring
            # the operation lock. Recheck while owning the operation lock.
            with self._lock:
                prior = self._tasks.get(self._requests.get(request_key))
                if prior:
                    if fingerprint != prior["_fingerprint"]:
                        return _error("request_conflict", "同一请求编号不能更换维护内容",
                                      task_id=prior["id"], task=self._snapshot(prior))
                    return {"ok": True, "task_id": prior["id"], "task": self._snapshot(prior), "reused": True}
                if self._active is not None:
                    return self._busy()
            error = self.preflight()
            if error:
                return error
            now = self.clock()
            task_id = uuid.uuid4().hex
            task = {
                "id": task_id, "request_id": request_id, "action": action,
                "selection": copy.deepcopy(normalized.get("selection")),
                "running": True, "status": "queued",
                "progress": {"phase": "queued", "message": "维护任务已排队",
                             "files_done": 0, "files_total": None, "bytes_done": 0, "bytes_total": None},
                "result": None, "created_at": now, "updated_at": now, "finished_at": None,
                "_fingerprint": fingerprint,
            }
            with self._lock:
                completed = sorted((row for row in self._tasks.values() if not row["running"]),
                                   key=lambda row: row["finished_at"])
                while len(self._tasks) >= self.max_tasks and completed:
                    self._remove(completed.pop(0)["id"])
                task["_worker"] = worker or self.worker
                self._tasks[task_id] = task
                self._requests[request_key] = task_id
                self._active = task_id
            try:
                thread = self.thread_factory(target=self._run, args=(task_id, normalized),
                                             name="xg-cleanup-" + task_id[:8], daemon=False)
                thread.start()
            except Exception as exc:
                with self._lock:
                    self._finish(task_id, _exception_result(exc))
                return _error("cleanup_start_failed", "维护任务未能启动", task_id=task_id,
                              task=self.lookup(task_id=task_id)["task"])
            transferred = True
            with self._lock:
                return {"ok": True, "task_id": task_id, "task": self._snapshot(task)}
        except Exception as exc:
            return _exception_result(exc)
        finally:
            if not transferred:
                self.operation_lock.release()

    def _progress(self, task_id, event):
        if not isinstance(event, dict):
            return
        with self._lock:
            task = self._tasks.get(task_id)
            if not task or not task["running"]:
                return
            progress = task["progress"]
            for key in ("phase", "message"):
                if isinstance(event.get(key), str):
                    progress[key] = event[key][:64 if key == "phase" else 500]
            for key in ("files_done", "files_total", "bytes_done", "bytes_total"):
                value = event.get(key)
                if type(value) is int and value >= 0 or value is None and key.endswith("_total"):
                    if key in event:
                        progress[key] = value
            task["updated_at"] = self.clock()

    def _finish(self, task_id, result):
        task = self._tasks[task_id]
        task.update(running=False, status="succeeded" if result.get("ok") is True else "failed",
                    result=copy.deepcopy(result), finished_at=self.clock(), updated_at=self.clock())
        if isinstance(result.get("selection"), dict):
            task["selection"] = copy.deepcopy(result["selection"])
        task["progress"]["phase"] = "complete" if result.get("ok") is True else "failed"
        task["progress"]["message"] = ("维护任务已完成" if result.get("ok") is True
                                       else str(result.get("message") or result.get("error") or "维护任务失败")[:500])
        if self._active == task_id:
            self._active = None
        task.pop("_worker", None)

    def _run(self, task_id, payload):
        try:
            with self._lock:
                task = self._tasks[task_id]
                task.update(status="running", updated_at=self.clock())
                task["progress"].update(phase="starting", message="正在核对维护条件")
                action = task["action"]
                worker = task["_worker"]
            result = self.preflight()
            if not result:
                result = worker(action, payload, lambda event: self._progress(task_id, event))
            if not isinstance(result, dict) or type(result.get("ok")) is not bool:
                result = _error("cleanup_bad_result", "维护结果未确认，请重新预览并检查回收区")
            result = json.loads(json.dumps(result, ensure_ascii=False))
        except BaseException as exc:
            result = _exception_result(exc)
        finally:
            # Keep the shared operation lock until all worker callbacks/state hooks have returned.
            with self._lock:
                try:
                    self._finish(task_id, result)
                finally:
                    self.operation_lock.release()

    def lookup(self, *, task_id=None, request_id=None, action=None):
        with self._lock:
            self._prune()
            if task_id is not None:
                if not isinstance(task_id, str) or not _ID.fullmatch(task_id):
                    return _error("task_not_found", "没有找到此维护任务")
                key = task_id
            elif request_id is not None or action is not None:
                if (not isinstance(request_id, str) or not _ID.fullmatch(request_id)
                        or action not in ACTIONS):
                    return _error("bad_task_lookup", "任务查询需要有效的请求编号与操作名称")
                key = self._requests.get((action, request_id))
            else:
                key = self._active
                if key is None:
                    return {"ok": True, "task": None}
            task = self._tasks.get(key)
            if not task:
                return _error("task_not_found", "没有找到此维护任务；任务可能已过期或服务已重启")
            return {"ok": True, "task": self._snapshot(task)}
