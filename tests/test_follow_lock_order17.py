"""Offline lock regressions for the v20 follow integration.

Run this file with -B. To reproduce against the untouched source, supply
--bot-dir <absolute base/bot path> --expect-deadlock. This workspace lacks the
server terrain/codec packages: compile the original Python control definitions,
without editing their bodies, and supply only in-memory inputs/geometry/Actor
leaves. Real threads, RLocks, control transactions and their decorators run.
"""
import argparse
import ast
from contextlib import contextmanager
import functools
import math
from pathlib import Path
import sys
import threading
import time
from types import MethodType, ModuleType, SimpleNamespace as NS
import unittest
from unittest.mock import patch


BOT_DIR = Path(__file__).resolve().parents[1] / "app" / "bot"  # client layout
EXPECT_DEADLOCK = False
NOW = 100.0
GOAL = (3.0, 5.0, 0.0)
SETUP_TIMEOUT = 5.0
JOIN_TIMEOUT = 1.0


class Sources:
    def __init__(self, root):
        self.root = root

    @functools.lru_cache()
    def tree(self, name):
        path = self.root / (name + ".py")
        return str(path), ast.parse(path.read_text(encoding="utf-8"), str(path))

    def node(self, name, *parents):
        _, node = self.tree(name)
        for parent in parents:
            node = next(n for n in node.body if getattr(n, "name", None) == parent)
        return node

    def execute(self, module, name, *definitions):
        path, _ = self.tree(name)
        exec(compile(ast.Module(body=list(definitions), type_ignores=[]), path, "exec"), vars(module))

    def make_class(self, module, name, class_name, members, *, extra_prefix=()):
        original = self.node(name, class_name)
        cls = ast.ClassDef(name=class_name, bases=[], keywords=[], decorator_list=[],
                           body=[n for n in original.body
                                 if getattr(n, "name", None) in members
                                 or any(getattr(n, "name", "").startswith(p) for p in extra_prefix)])
        self.execute(module, name, ast.fix_missing_locations(ast.copy_location(cls, original)))
        return getattr(module, class_name)


class ObservedRLock:
    """Add Event hooks around the underlying real RLock, never a timeout."""
    def __init__(self):
        self.lock = threading.RLock()
        self.before = self.after = lambda: None

    def acquire(self, *args, **kwargs):
        self.before()
        acquired = self.lock.acquire(*args, **kwargs)
        if acquired:
            self.after()
        return acquired

    def release(self):
        self.lock.release()

    def owned(self):
        return self.lock._is_owned()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_):
        self.release()


class AirMotionInput:
    def __init__(self, plan, now, **_):
        self.seconds = 1.0


class AirborneObservation:
    def observe(self, *_args, **_kwargs):
        return {"mode": "airborne", "reason": "fixture"}


def module(name, **symbols):
    mod = ModuleType(name)
    vars(mod).update(symbols)
    return mod


def noop(*_args, **_kwargs):
    return None


def fail(*_args, **_kwargs):
    raise AssertionError("unexpected branch in the offline fixture")


class Fixture:
    def __init__(self):
        src = Sources(BOT_DIR)
        self.solo, self.air_ground, self.stop, self.runtime_lock = (
            ObservedRLock() for _ in range(4)
        )
        self.terrain = NS(surface=object())
        self.logs = []
        self.anim = NS(ContinuousEmoteEvent=type("ContinuousEmoteEvent", (), {}),
                       AnimEvent=type("AnimEvent", (), {}), read_origin=fail, WALK_SPEED=0.25)
        guard = module("prop_motion_guard", math=math)
        src.execute(guard, "prop_motion_guard", *(
            src.node("prop_motion_guard", name) for name in ("backend", "selection", "focus", "covers")
        ))
        recovery = NS(nearest_supported_goal=lambda *_: dict(goal=None, reason="no_support", tested=1))
        src.execute(recovery, "stuck_recovery", src.node("stuck_recovery", "classify_failure"))
        fake_clock = NS(monotonic=lambda: NOW)
        room = module("room_pose", math=math, time=fake_clock, threading=threading,
                      functools=functools, anim_pose=self.anim, air_motion=NS(AirMotion=AirMotionInput),
                      prop_motion_guard=guard, stuck_recovery=recovery)
        src.execute(room, "seat_tilt_runtime", src.node("seat_tilt_runtime", "upright_action"))
        src.execute(room, "room_pose", src.node("room_pose", "_solo_action"))
        room_cls = src.make_class(room, "room_pose", "PoseController", {
            "_safe_post", "_chibi_wire_body", "_solo_defer", "_air_ground_start", "_terrain_invalidate",
            "_terrain_stop_state", "_terrain_invalidate_locked", "_terrain_social_owns",
            "_stuck_binding", "_stuck_current",
            "_stuck_goal_geometry_revision", "_stuck_invalid_goal", "_hop_running",
            "_hop_hard_cancel", "_air_block_reason", "_air_start", "_air_maybe",
            "_follow_tick", "_active_running",
        }, extra_prefix=("_air_start_", "_air_commit_", "_stuck_invalid_goal_"))
        self.recovery = recovery
        c = self.c = room_cls()
        c.__dict__.update(
            _solo_lock=self.solo, _air_ground_lock=self.air_ground,
            _terrain_stop=[self.stop, 0, False, None], base=b"fixture base",
            follow=dict(target="peer", distance=1.2), position=(0.0, 0.0, 0.0), map_id=1,
            route=[], after_arrive=None, _air_motion=None, motion=None, _terrain_motion=None,
            social=None, sitting=None, holding=False, candle=None, event=None,
            _command_seq=1, walk_clip=None, _air_cfg=dict(enabled=True, follow=True),
            log=lambda name, **kw: self.logs.append((name, kw)),
            _air_enabled=lambda: True, _air_terrain=lambda: self.terrain,
            _air_clips=lambda: NS(ok=True), _spot_near=lambda *_: (GOAL, GOAL, 3.0),
            _air_target_settled=lambda *_: (False, "fixture"), _air_log_plan=noop,
            _once_before_motion=noop, _set_override=noop, _walk_record_tick=noop,
            _active_tick=noop, _candle_reply_tick=noop, _relink_tick=noop, _candle_tick=noop,
            _social_tick=noop, _air_step_maybe=lambda *_: False, walk_to=noop, reset=fail,
        )
        c.tracker = NS(lock=threading.RLock(), ready=True, _engine=NS(_closed=False),
                       room_generation=1, epoch=1, slots={2: "peer"}, origins={2: GOAL},
                       position_observed={2: (GOAL, NOW)}, _slot_versions={2: 1})
        air = module("air_follow_runtime", math=math, threading=threading, time=fake_clock,
                     contextmanager=contextmanager, prop_motion_guard=guard, LOG_INTERVAL=5.,
                     OBSERVATION_TTL=1.5, AirFollowMotion=AirMotionInput,
                     air_follow=NS(TargetTracker=AirborneObservation,
                                   takeoff_plan=lambda *_: dict(kind="fly", start=c.position),
                                   DEFAULT_POLICY=NS(max_horizontal=2.5)))
        src.execute(air, "air_follow_runtime", *(
            n for n in src.tree("air_follow_runtime")[1].body if isinstance(n, ast.FunctionDef)
        ))
        hop = module("hop_follow_runtime_v20", threading=threading, time=fake_clock, math=math)
        src.execute(hop, "hop_follow_runtime_v20", src.node("hop_follow_runtime_v20", "need"))
        hop_cls = src.make_class(hop, "hop_follow_runtime_v20", "FollowHopCoordinator",
                                 {"__init__", "active", "suppress_ground_replan"})
        self.co = hop_cls(None, publication_lock=self.stop)
        hop.post_runtime_hop = lambda runtime, body, **kw: NS(
            emitted=False, body=kw["original_post"](body)
        )
        self.modules = {"air_follow_runtime": air, "hop_follow_runtime_v20": hop}

        pilot = module("bounded_runtime", math=math, AdmissionError=ValueError,
                       Decision=lambda status, **kw: NS(status=status, **kw), inside_patch=lambda *_: True)
        src.execute(pilot, "pilot_contract", *(
            src.node("pilot_contract", name) for name in ("require", "finite", "vec")
        ))
        pilot_cls = src.make_class(pilot, "bounded_runtime", "BoundedPilotRuntime",
                                   {"invalidate", "request", "cancel"}, extra_prefix=("_cancel_",))
        runtime = self.runtime = pilot_cls.__new__(pilot_cls)
        runtime.__dict__.update(
            _lock=self.runtime_lock, _generation=0, _request_id=2, _lease=None, _attached=True,
            _follow_hop=self.co, _motion=None, _permission=lambda: object(),
            controller=c, admission=NS(max_speed=1.0, max_requested_distance=10.0),
            _future_request=2, last_prepare_decision=NS(status="blocked", reason="goal_support_missing"),
            tick=lambda: "waiting_for_supported_goal",
        )
        c._terrain_adapter = runtime
        c._stuck_intent = dict(binding=c._stuck_binding(), key=(1, "follow", "peer"), label="follow", goal=GOAL)
        c._stuck_request = (runtime, runtime._future_request, c._stuck_intent["key"], None)

        entry = module("experimental_ground_entry", c=c)
        entry_cls = src.make_class(entry, "experimental_ground_entry", "ExperimentalGroundHook",
                                   {"tick", "_handle_invalid_ground_goal"})
        hook = self.hook = entry_cls()
        hook.__dict__.update(
            c=c, runtime=runtime, _hop_dispatch=NS(coordinator=self.co), _closed=False,
            _expired=False, deadline=NOW + 5.0, clock=lambda: NOW,
            _config={}, _config_generation=1, _config_lock=threading.RLock(),
            _follow_settings_generation=1, _active_generation=1, _future=None,
            _request_label="follow", _desired=GOAL, _rebind_returned_follow_target=noop,
            _rebind_seat_target=noop, _recover_failed_worker=noop, _queue_worker_recovery=noop,
            _refresh_follow_goal=noop, _submit_desired=noop,
            _dispatch_after_arrival=noop, _report_status=noop, _remember_handoff_pose=noop,
            _revoke=fail,
        )
        entry.self = hook    # Bind the unchanged __init__ closures to this offline fixture.
        src.execute(entry, "experimental_ground_entry", *(
            src.node("experimental_ground_entry", "ExperimentalGroundHook", "__init__", n)
            for n in ("_hop_active_probe", "_hop_cancel_probe", "post")
        ))
        c._hop_active_probe = entry._hop_active_probe
        c._hop_cancel_probe = entry._hop_cancel_probe
        c._post = MethodType(entry.post, c)
        exec("def encode(c, body): return b'constructed ' + body", vars(room))
        hook._original_post = MethodType(room.encode, c)

    def start(self, name, target):
        result = NS(value=None, error=None)

        def run():
            try:
                result.value = target()
            except Exception as error:
                result.error = error

        thread = threading.Thread(name=name, target=run, daemon=True)
        thread.start()
        return thread, result


class FollowLockOrderTests(unittest.TestCase):
    def wait(self, event, description):
        self.assertTrue(event.wait(SETUP_TIMEOUT), description)

    def finish(self, threads, expected_stacks):
        deadline = time.monotonic() + JOIN_TIMEOUT
        for thread, _ in threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        for thread, result in threads:
            if result.error is not None:
                raise AssertionError(thread.name) from result.error
        live = {}
        for thread, _ in threads:
            if not thread.is_alive():
                continue
            frame = sys._current_frames().get(thread.ident)
            names = []
            while frame is not None:
                names.append(frame.f_code.co_name)
                frame = frame.f_back
            live[thread.name] = names
        if EXPECT_DEADLOCK:
            self.assertEqual(set(live), set(expected_stacks), live)
            for name, expected in expected_stacks.items():
                self.assertTrue(set(expected).issubset(live[name]), live)
            print("confirmed lock cycle:", "; ".join(
                name + " -> " + " / ".join(stack) for name, stack in expected_stacks.items()
            ), flush=True)
        else:
            self.assertFalse(live, f"thread(s) did not finish: {live}")

    def test_invalid_ground_goal_frame_and_air_follow_takeoff(self):
        fx = Fixture()
        in_geometry = threading.Event()
        resume_frame = threading.Event()
        takeoff_at_solo = threading.Event()
        stopped_at = {}

        def goal_selection(*_):
            in_geometry.set()
            self.wait(resume_frame, "fixture did not release invalid-goal recheck")
            return dict(goal=None, reason="no_support", tested=1)

        def before_solo():
            if threading.current_thread().name == "pose-control":
                stopped_at.setdefault("pose_takes_solo_with_stop", fx.stop.owned())
                takeoff_at_solo.set()

        def before_stop():
            if threading.current_thread().name == "actor-frame" and resume_frame.is_set():
                stopped_at["frame_takes_stop_with_solo"] = fx.solo.owned()

        fx.stop.before = before_stop
        fx.recovery.nearest_supported_goal = goal_selection
        fx.solo.before = before_solo
        with patch.dict(sys.modules, fx.modules):
            publisher = fx.start("actor-frame", lambda: fx.c._safe_post(b"Actor"))
            try:
                self.wait(in_geometry, "frame did not reach missing-support geometry")
                takeoff = fx.start("pose-control", fx.c._follow_tick)
                self.wait(takeoff_at_solo, "pose-control did not reach the takeoff solo lock")
            finally:
                resume_frame.set()
            self.finish((publisher, takeoff), {
                "actor-frame": ("_safe_post", "post", "tick", "_handle_invalid_ground_goal",
                                "_stuck_invalid_goal", "_air_block_reason", "_hop_running", "active"),
                "pose-control": ("_follow_tick", "_air_maybe", "maybe_start", "_air_ground_start"),
            })
            if EXPECT_DEADLOCK:
                self.assertEqual(stopped_at, {"pose_takes_solo_with_stop": True,
                                              "frame_takes_stop_with_solo": True})
            else:
                self.assertEqual(stopped_at, {"pose_takes_solo_with_stop": False})
                self.assertEqual(publisher[1].value, b"constructed Actor")
                self.assertIsInstance(fx.c._air_motion, AirMotionInput)
                self.assertTrue(any(name == "air_follow_start" for name, _ in fx.logs))

    def test_runtime_request_and_takeoff_invalidation(self):
        fx = Fixture()
        request_has_runtime = threading.Event()
        resume_request = threading.Event()
        takeoff_at_runtime = threading.Event()
        stopped_at = {}

        def after_runtime():
            if threading.current_thread().name == "ground-request" and not request_has_runtime.is_set():
                request_has_runtime.set()
                self.wait(resume_request, "fixture did not release request cancellation")

        def before_runtime():
            if threading.current_thread().name == "pose-control":
                stopped_at.setdefault("takeoff_takes_runtime_with_stop", fx.stop.owned())
                takeoff_at_runtime.set()

        def before_stop():
            if threading.current_thread().name == "ground-request":
                stopped_at["request_takes_stop_with_runtime"] = fx.runtime_lock.owned()

        fx.stop.before = before_stop
        fx.runtime_lock.after = after_runtime
        fx.runtime_lock.before = before_runtime
        with patch.dict(sys.modules, fx.modules):
            request = fx.start("ground-request", lambda: fx.runtime.request(GOAL))
            try:
                self.wait(request_has_runtime, "ground request did not acquire its runtime lock")
                takeoff = fx.start("pose-control", fx.c._follow_tick)
                self.wait(takeoff_at_runtime, "pose-control did not reach adapter invalidation")
            finally:
                resume_request.set()
            self.finish((request, takeoff), {
                "ground-request": ("request", "cancel", "suppress_ground_replan"),
                "pose-control": ("maybe_start", "_air_start", "_terrain_invalidate", "invalidate"),
            })
            self.assertEqual(stopped_at, {"takeoff_takes_runtime_with_stop": EXPECT_DEADLOCK,
                                          "request_takes_stop_with_runtime": True})
            if not EXPECT_DEADLOCK:
                self.assertIsInstance(request[1].value, int)
                self.assertIsInstance(fx.c._air_motion, AirMotionInput)

    def test_takeoff_rechecks_generation_after_constructing_motion(self):
        if EXPECT_DEADLOCK:
            self.skipTest("tests the new two-phase publication contract, not a baseline deadlock")
        fx = Fixture()
        constructing = threading.Event()
        resume_takeoff = threading.Event()
        air = fx.modules["air_follow_runtime"]
        wait = self.wait

        class PausedMotion(AirMotionInput):
            def __init__(self, *args, **kwargs):
                constructing.set()
                wait(resume_takeoff, "fixture did not release motion creation")
                super().__init__(*args, **kwargs)

        air.AirFollowMotion = PausedMotion
        with patch.dict(sys.modules, fx.modules):
            worker = fx.start("pose-control", lambda: air.maybe_start(fx.c, fx.terrain, GOAL, NOW))
            try:
                self.wait(constructing, "takeoff did not reach motion creation")
                self.assertTrue(fx.stop.acquire(blocking=False), "motion creation held terrain_stop")
                try:
                    fx.c._terrain_stop[1] += 1
                finally:
                    fx.stop.release()
            finally:
                resume_takeoff.set()
            self.finish((worker,), {})
            self.assertFalse(worker[1].value)
            self.assertIsNone(fx.c.motion)
            self.assertIsNone(fx.c._air_motion)
            self.assertEqual(fx.runtime._generation, 0)
            self.assertEqual(fx.c._terrain_stop[1], 1)
            self.assertIn(("air_follow_deferred", {"reason": "takeoff_snapshot_changed"}), fx.logs)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--bot-dir", type=Path, default=BOT_DIR)
    parser.add_argument("--expect-deadlock", action="store_true")
    options, remaining = parser.parse_known_args()
    BOT_DIR = options.bot_dir.resolve(strict=True)
    EXPECT_DEADLOCK = options.expect_deadlock
    unittest.main(argv=[sys.argv[0], *remaining])
