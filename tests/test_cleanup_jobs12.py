"""Asynchronous maintenance tests: synthetic workers/roots and loopback HTTP only."""
from pathlib import Path
import http.client
import json
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
sys.path[:0] = [str(APP / "launcher"), str(APP), str(APP / "bot")]
from cleanup_tasks import CleanupTasks, validate_request
from data_cleanup import CleanupError
import control_api as api

SELECTION = dict(terrain=True, catalog=False, names=False, captures=False, props=False)


def wait_task(manager, task_id):
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        result = manager.lookup(task_id=task_id)
        if not result.get("ok") or not result["task"]["running"]:
            return result
        threading.Event().wait(0.002)
    raise AssertionError("synthetic task did not finish")


class CleanupTaskUnitTests(unittest.TestCase):
    def setUp(self):
        self.gate = threading.Event()
        self.entered = threading.Event()
        self.operation = threading.Lock()
        self.calls = []
        self.managers = []

    def tearDown(self):
        self.gate.set()
        for manager in self.managers:
            current = manager.lookup().get("task")
            if current:
                wait_task(manager, current["id"])

    def make(self, worker=None, **kwargs):
        if worker is None:
            worker = lambda a, p, emit: {"ok": True, "selection": p.get("selection")}
        manager = CleanupTasks(operation_lock=kwargs.pop("operation_lock", self.operation),
                               worker=worker, preflight=kwargs.pop("preflight", lambda: None), **kwargs)
        self.managers.append(manager)
        return manager

    def slow(self, action, payload, emit):
        self.calls.append((action, payload))
        emit({"phase": "copy", "message": "synthetic copy", "files_done": 1, "files_total": 2,
              "bytes_done": 3, "bytes_total": 6})
        self.entered.set()
        if not self.gate.wait(3):
            raise AssertionError("test gate was not released")
        return {"ok": True, "selection": payload.get("selection"), "value": "unchanged"}

    def start(self, manager, request_id="request-1", selection=None):
        return manager.submit("preview", {"request_id": request_id,
                                         "selection": selection if selection is not None else SELECTION})

    def test_submit_returns_while_slow_worker_running(self):
        manager = self.make(self.slow)
        before = time.monotonic()
        response = self.start(manager)
        self.assertLess(time.monotonic() - before, 0.5)
        self.assertTrue(response["ok"])
        self.assertTrue(self.entered.wait(1))
        self.assertTrue(manager.lookup(task_id=response["task_id"])["task"]["running"])
        self.assertTrue(self.operation.locked())

    def test_live_progress_and_lookup_do_not_require_operation_lock(self):
        manager = self.make(self.slow)
        response = self.start(manager)
        self.assertTrue(self.entered.wait(1))
        state = manager.lookup(task_id=response["task_id"])["task"]
        self.assertEqual(state["progress"]["bytes_done"], 3)
        self.assertEqual(state["progress"]["bytes_total"], 6)
        self.assertEqual(state["progress"]["phase"], "copy")
        self.assertTrue(self.operation.locked())

    def test_active_duplicate_returns_same_job_and_runs_once(self):
        manager = self.make(self.slow)
        a = self.start(manager)
        self.assertTrue(self.entered.wait(1))
        b = self.start(manager)
        self.assertEqual(a["task_id"], b["task_id"])
        self.assertTrue(b["reused"])
        self.assertEqual(len(self.calls), 1)

    def test_completed_duplicate_at_capacity_is_not_evicted_or_reexecuted(self):
        work = Mock(return_value={"ok": True})
        manager = self.make(work, max_tasks=1)
        a = self.start(manager)
        wait_task(manager, a["task_id"])
        b = self.start(manager)
        self.assertEqual(a["task_id"], b["task_id"])
        self.assertTrue(b["reused"])
        work.assert_called_once()

    def test_same_id_different_payload_is_conflict(self):
        manager = self.make(self.slow)
        a = self.start(manager)
        b = self.start(manager, selection={"props": True})
        self.assertFalse(b["ok"])
        self.assertEqual(b["error_code"], "request_conflict")
        self.assertEqual(b["task_id"], a["task_id"])

    def test_data_root_scope_is_part_of_idempotency_identity(self):
        manager = self.make(self.slow)
        payload = {"request_id": "scope", "selection": SELECTION}
        a = manager.submit("preview", payload, scope="root-a")
        b = manager.submit("preview", payload, scope="root-b")
        self.assertTrue(a["ok"])
        self.assertEqual(b["error_code"], "request_conflict")

    def test_busy_job_exposes_existing_task_without_another_worker(self):
        manager = self.make(self.slow)
        a = self.start(manager)
        b = self.start(manager, request_id="request-2")
        self.assertEqual(b["error_code"], "cleanup_busy")
        self.assertEqual(b["task"]["id"], a["task_id"])

    def test_other_global_operation_is_fail_fast(self):
        work = Mock()
        manager = self.make(work)
        with self.operation:
            response = self.start(manager)
        self.assertEqual(response["error_code"], "cleanup_busy")
        self.assertIsNone(response["task"])
        work.assert_not_called()

    def test_lookup_by_request_identity_and_current(self):
        manager = self.make(self.slow)
        result = self.start(manager)
        self.assertEqual(manager.lookup(request_id="request-1", action="preview")["task"]["id"],
                         result["task_id"])
        self.assertEqual(manager.lookup()["task"]["id"], result["task_id"])
        self.gate.set()
        wait_task(manager, result["task_id"])
        self.assertIsNone(manager.lookup()["task"])

    def test_unknown_task_and_incomplete_lookup_are_explicit(self):
        manager = self.make()
        self.assertEqual(manager.lookup(task_id="missing")["error_code"], "task_not_found")
        self.assertEqual(manager.lookup(request_id="missing")["error_code"], "bad_task_lookup")

    def test_invalid_action_confirmation_and_id_do_not_start_worker(self):
        work = Mock()
        manager = self.make(work)
        for action, body in [
            ("unknown", {"request_id": "x"}),
            ("preview", {"selection": SELECTION}),
            ("execute", {"plan_id": "valid", "confirm": "true", "request_id": "x"}),
            ("execute", {"plan_id": "valid", "confirm": 1, "request_id": "x"}),
            ("execute", {"plan_id": "../secrets", "confirm": True, "request_id": "x"}),
            ("preview", {"selection": {"terrain": 1}, "request_id": "x"}),
            ("preview", {"selection": {}, "request_id": "x"}),
        ]:
            with self.subTest(action=action, body=body):
                self.assertFalse(manager.submit(action, body)["ok"])
        work.assert_not_called()
        self.assertFalse(self.operation.locked())

    def test_preflight_failure_returns_original_safe_body(self):
        work = Mock()
        error = {"ok": False, "error_code": "client_online", "message": "must stop"}
        manager = self.make(work, preflight=lambda: error)
        self.assertEqual(self.start(manager), error)
        work.assert_not_called()
        self.assertFalse(self.operation.locked())

    def test_preflight_rechecked_in_worker(self):
        work = Mock()
        preflight = Mock(side_effect=[None, {"ok": False, "error_code": "task_busy", "message": "busy"}])
        manager = self.make(work, preflight=preflight)
        result = wait_task(manager, self.start(manager)["task_id"])["task"]
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["result"]["error_code"], "task_busy")
        work.assert_not_called()

    def test_failure_result_preserves_rollback_cause_and_selection(self):
        original = {"ok": False, "error_code": "rollback_incomplete", "message": "restore needed",
                    "cause": "synthetic_move_error", "selection": SELECTION, "batch_id": "synthetic"}
        manager = self.make(lambda *a: original)
        task = wait_task(manager, self.start(manager)["task_id"])["task"]
        self.assertEqual(task["result"], original)
        self.assertEqual(task["selection"], SELECTION)
        self.assertEqual(task["status"], "failed")

    def test_known_cleanup_error_keeps_business_code_message(self):
        def fail(*args):
            raise CleanupError("preview_expired", "请重新预览")
        manager = self.make(fail)
        result = wait_task(manager, self.start(manager)["task_id"])["task"]["result"]
        self.assertEqual(result["error_code"], "preview_expired")
        self.assertEqual(result["message"], "请重新预览")

    def test_unexpected_error_never_returns_private_text(self):
        def fail(*args):
            raise OSError(5, "synthetic-private-path-or-content")
        manager = self.make(fail)
        result = wait_task(manager, self.start(manager)["task_id"])["task"]["result"]
        self.assertNotIn("synthetic-private", json.dumps(result))
        self.assertEqual(result["cause"]["errno"], 5)
        self.assertEqual(result["error_code"], "cleanup_internal")
        self.assertFalse(self.operation.locked())

    def test_invalid_worker_body_is_failed_not_success(self):
        manager = self.make(lambda *args: {"result": True})
        task = wait_task(manager, self.start(manager)["task_id"])["task"]
        self.assertEqual(task["result"]["error_code"], "cleanup_bad_result")
        self.assertFalse(self.operation.locked())

    def test_thread_start_failure_releases_global_lock(self):
        class BrokenThread:
            def __init__(self, **kwargs):
                pass
            def start(self):
                raise RuntimeError("private start details")
        manager = self.make(thread_factory=BrokenThread)
        result = self.start(manager)
        self.assertEqual(result["error_code"], "cleanup_start_failed")
        self.assertFalse(self.operation.locked())
        self.assertFalse(result["task"]["running"])
        self.assertNotIn("private start", json.dumps(result))

    def test_thread_constructor_failure_finishes_slot_and_next_job_succeeds(self):
        calls = []
        def factory(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise RuntimeError("synthetic private constructor detail")
            return threading.Thread(**kwargs)
        manager = self.make(thread_factory=factory)
        first = self.start(manager, "ctor-first")
        self.assertEqual(first["error_code"], "cleanup_start_failed")
        self.assertFalse(first["task"]["running"])
        self.assertEqual(first["task"]["status"], "failed")
        self.assertIsNone(manager.lookup()["task"])
        self.assertFalse(self.operation.locked())
        self.assertNotIn("private constructor", json.dumps(first))
        second = self.start(manager, "ctor-second")
        self.assertTrue(second["ok"])
        self.assertEqual(wait_task(manager, second["task_id"])["task"]["status"], "succeeded")

    def test_retention_bound_and_expiry(self):
        now = [100.0]
        manager = self.make(max_tasks=2, retention_seconds=10, clock=lambda: now[0])
        ids = []
        for index in range(3):
            now[0] += 1
            result = self.start(manager, "retention-%d" % index)
            ids.append(result["task_id"])
            wait_task(manager, result["task_id"])
        self.assertEqual(manager.lookup(task_id=ids[0])["error_code"], "task_not_found")
        self.assertTrue(manager.lookup(task_id=ids[2])["ok"])
        now[0] += 11
        self.assertEqual(manager.lookup(task_id=ids[2])["error_code"], "task_not_found")

    def test_active_task_never_expires(self):
        now = [100.0]
        manager = self.make(self.slow, clock=lambda: now[0], retention_seconds=1)
        result = self.start(manager)
        now[0] += 1000
        self.assertTrue(manager.lookup(task_id=result["task_id"])["task"]["running"])

    def test_public_snapshot_mutation_does_not_modify_saved_task(self):
        manager = self.make(self.slow)
        result = self.start(manager)
        result["task"]["progress"]["message"] = "mutated"
        result["task"]["selection"]["terrain"] = False
        task = manager.lookup(task_id=result["task_id"])["task"]
        self.assertTrue(task["selection"]["terrain"])
        self.assertNotEqual(task["progress"]["message"], "mutated")
        self.assertFalse(any(key.startswith("_") for key in task))

    def test_worker_is_nondaemon_for_graceful_shutdown(self):
        args = []
        def factory(**kwargs):
            args.append(kwargs)
            return threading.Thread(**kwargs)
        manager = self.make(thread_factory=factory)
        wait_task(manager, self.start(manager)["task_id"])
        self.assertIs(args[0]["daemon"], False)

    def test_progress_filters_unverified_fields_and_invalid_counters(self):
        def work(action, payload, emit):
            emit({"phase": "inventory", "message": "real scan", "files_done": 3,
                  "files_total": None, "bytes_done": 8, "bytes_total": 10})
            emit({"files_done": -1, "files_total": True, "bytes_done": "guess",
                  "internal_private": "not public"})
            self.entered.set()
            self.gate.wait(3)
            return {"ok": True}
        manager = self.make(work)
        task_id = self.start(manager)["task_id"]
        self.assertTrue(self.entered.wait(1))
        progress = manager.lookup(task_id=task_id)["task"]["progress"]
        self.assertEqual(progress["files_done"], 3)
        self.assertIsNone(progress["files_total"])
        self.assertEqual(progress["bytes_done"], 8)
        self.assertNotIn("internal_private", progress)

    def test_per_action_request_ids_are_independent(self):
        manager = self.make()
        first = self.start(manager, "same-request")
        wait_task(manager, first["task_id"])
        second = manager.submit("restore-preview", {"request_id": "same-request"})
        wait_task(manager, second["task_id"])
        self.assertNotEqual(first["task_id"], second["task_id"])

    def _fast_worker_race(self, conflicting=False):
        blocked = threading.Event()
        resume = threading.Event()
        underlying = threading.Lock()
        class PausingLock:
            def acquire(inner, blocking=False):
                if threading.current_thread().name == "delayed-submit":
                    blocked.set()
                    self.assertTrue(resume.wait(2))
                return underlying.acquire(blocking=blocking)
            def release(inner):
                underlying.release()
        work = Mock(return_value={"ok": True})
        manager = self.make(work, operation_lock=PausingLock())
        replies = []
        second = threading.Thread(name="delayed-submit", target=lambda: replies.append(
            self.start(manager, selection={"props": True} if conflicting else SELECTION)))
        second.start()
        self.assertTrue(blocked.wait(1))
        first = self.start(manager)
        wait_task(manager, first["task_id"])
        resume.set()
        second.join(2)
        self.assertFalse(second.is_alive())
        work.assert_called_once()
        if conflicting:
            self.assertEqual(replies[0]["error_code"], "request_conflict")
        else:
            self.assertEqual(replies[0]["task_id"], first["task_id"])
            self.assertTrue(replies[0]["reused"])
        self.assertFalse(underlying.locked())

    def test_fast_completed_worker_race_rechecks_identical_id(self):
        self._fast_worker_race()

    def test_fast_completed_worker_race_rechecks_payload_conflict(self):
        self._fast_worker_race(conflicting=True)


class CleanupJobsHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Handler(api.Handler):
            server_token = "synthetic-cleanup-jobs-cookie"
        cls.server = api.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(2)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cleanup-jobs-http-", dir=ROOT / "tests")
        self.data = Path(self.temp.name)
        self.gate = threading.Event()
        self.entered = threading.Event()
        self.calls = []
        self.online = Mock(return_value=False)
        self.busy = Mock(return_value=False)
        self.patches = [
            patch.object(api.xg_paths, "DATA", self.data),
            patch.object(api, "_CLEANUP_TASKS", None),
            patch.object(api, "_cleanup_online", self.online),
            patch.object(api, "_cleanup_busy", self.busy),
            patch.object(api, "cleanup_action", side_effect=self.worker),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        self.gate.set()
        if api._CLEANUP_TASKS is not None:
            active = api._CLEANUP_TASKS.lookup().get("task")
            if active:
                wait_task(api._CLEANUP_TASKS, active["id"])
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def worker(self, action, payload, *, progress, data_root):
        self.calls.append((action, payload, data_root))
        progress({"phase": "inventory", "message": "synthetic inventory", "files_done": 2,
                  "files_total": None, "bytes_done": 7, "bytes_total": None})
        self.entered.set()
        if not self.gate.wait(3):
            raise AssertionError("HTTP test barrier not released")
        return {"ok": True, "action": action, "selection": payload.get("selection")}

    def request(self, method, path, body=None, *, auth=True, origin=None):
        headers = {"Content-Type": "application/json", "Origin": origin or "http://127.0.0.1:%d" % self.port}
        if auth:
            headers["Cookie"] = api.COOKIE + "=synthetic-cleanup-jobs-cookie"
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        try:
            connection.request(method, "/xiaoguang" + path,
                               json.dumps(body).encode() if body is not None else None, headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def start(self, request_id="http-one", action="preview", **fields):
        body = {"request_id": request_id, **fields}
        if action == "preview":
            body.setdefault("selection", SELECTION)
        return self.request("POST", "/api/wizard/cleanup/" + action, body)

    def test_http_returns_task_before_work_and_poll_is_responsive_under_global_lock(self):
        before = time.monotonic()
        code, response = self.start()
        self.assertEqual(code, 200)
        self.assertLess(time.monotonic() - before, 1)
        self.assertTrue(response["task_id"])
        self.assertTrue(self.entered.wait(1))
        code, state = self.request("GET", "/api/wizard/cleanup/task?task_id=" + response["task_id"])
        self.assertEqual(code, 200)
        self.assertTrue(state["task"]["running"])
        self.assertEqual(state["task"]["progress"]["files_done"], 2)
        self.assertTrue(api.LOCK.locked())
        code, busy = self.request("POST", "/api/wizard/data/start", {})
        self.assertEqual(code, 409)
        self.assertIn("busy", busy["error"])

    def test_auth_and_origin_precede_worker_and_status_lookup(self):
        for auth, origin in ((False, None), (True, "https://foreign.invalid")):
            code, _ = self.request("POST", "/api/wizard/cleanup/preview",
                                   {"request_id": "blocked", "selection": SELECTION}, auth=auth, origin=origin)
            self.assertEqual(code, 403)
            code, _ = self.request("GET", "/api/wizard/cleanup/task", auth=auth, origin=origin)
            self.assertEqual(code, 403)
        self.assertEqual(self.calls, [])

    def test_same_id_post_retry_and_get_request_recovery(self):
        code, first = self.start()
        self.assertEqual(code, 200)
        code, second = self.start()
        self.assertEqual(code, 200)
        self.assertEqual(first["task_id"], second["task_id"])
        code, result = self.request("GET", "/api/wizard/cleanup/task?" +
                                   urlencode({"request_id": "http-one", "action": "preview"}))
        self.assertEqual(code, 200)
        self.assertEqual(result["task"]["id"], first["task_id"])
        self.assertEqual(len(self.calls), 1)

    def test_busy_post_includes_existing_task_and_current_lookup(self):
        _, first = self.start()
        code, other = self.start("http-two")
        self.assertEqual(code, 409)
        self.assertEqual(other["task_id"], first["task_id"])
        code, current = self.request("GET", "/api/wizard/cleanup/task")
        self.assertEqual(code, 200)
        self.assertEqual(current["task"]["id"], first["task_id"])

    def test_unknown_request_is_explicit_not_found(self):
        code, result = self.request("GET", "/api/wizard/cleanup/task?request_id=unknown&action=preview")
        self.assertEqual(code, 404)
        self.assertEqual(result["error_code"], "task_not_found")
        code, current = self.request("GET", "/api/wizard/cleanup/task")
        self.assertEqual(code, 200)
        self.assertIsNone(current["task"])

    def test_online_busy_confirmation_invalidaction_fail_before_task(self):
        self.online.return_value = True
        code, result = self.start()
        self.assertEqual((code, result["error_code"]), (409, "client_online"))
        self.online.return_value = False
        self.busy.return_value = True
        code, result = self.start()
        self.assertEqual((code, result["error_code"]), (409, "task_busy"))
        self.busy.return_value = False
        code, result = self.start(action="execute", plan_id="valid-plan", confirm="true")
        self.assertEqual((code, result["error_code"]), (409, "confirmation_required"))
        code, result = self.start(action="invalid")
        self.assertEqual((code, result["error_code"]), (409, "unknown_action"))
        self.assertEqual(self.calls, [])

    def test_all_six_routes_use_async_envelope_and_original_final_result(self):
        self.gate.set()
        for index, action in enumerate(("preview", "execute", "restore-preview", "restore", "purge-preview", "purge")):
            kwargs = {"plan_id": "synthetic-plan", "confirm": True} if action in ("execute", "restore", "purge") else {}
            code, response = self.start("all-%d" % index, action=action, **kwargs)
            self.assertEqual(code, 200, response)
            task = wait_task(api._CLEANUP_TASKS, response["task_id"])["task"]
            self.assertEqual(task["result"]["action"], action)
            self.assertEqual(task["status"], "succeeded")
        self.assertEqual(len(self.calls), 6)

    def test_worker_root_is_bound_before_later_root_change(self):
        _, response = self.start()
        self.assertTrue(self.entered.wait(1))
        with patch.object(api.xg_paths, "DATA", self.data / "another-root"):
            self.gate.set()
            wait_task(api._CLEANUP_TASKS, response["task_id"])
        self.assertEqual(self.calls[0][2], self.data.absolute())


class CleanupCallbackBindingTests(unittest.TestCase):
    def test_service_progress_callback_is_unbound_after_success(self):
        callback = Mock()
        class Service:
            def set_progress(self, value):
                self.progress = value
            def preview(self, selection):
                self.progress({"phase": "inventory", "files_done": 1})
                return {"ok": True, "selection": selection}
        service = Service()
        with patch.object(api, "_cleanup_service", return_value=service), api.LOCK:
            result = api.cleanup_action("preview", {"selection": SELECTION}, progress=callback)
        self.assertTrue(result["ok"])
        self.assertIsNone(service.progress)
        callback.assert_called_once_with({"phase": "inventory", "files_done": 1})

    def test_service_progress_callback_is_unbound_after_business_failure(self):
        class Service:
            def set_progress(self, value):
                self.progress = value
            def preview(self, selection):
                raise CleanupError("unsafe_path", "synthetic rejection")
        service = Service()
        with patch.object(api, "_cleanup_service", return_value=service), api.LOCK:
            with self.assertRaises(CleanupError):
                api.cleanup_action("preview", {"selection": SELECTION}, progress=Mock())
        self.assertIsNone(service.progress)


if __name__ == "__main__":
    unittest.main()
