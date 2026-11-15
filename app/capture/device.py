# -*- coding: utf-8 -*-
"""找 MuMu / adb、确认 root、认当前地图。

显式 XG_ADB / 配置手填 adb 路径始终优先，不因自动探测而替换。
自动发现运行中的 MuMu / Nemu / nx_ 安装目录、注册表、常见位置和 PATH；
支持 nx_device/<任意版本>/shell/adb.exe、nx_main/adb.exe 及旧 shell 布局。
自动候选按可见的 device 状态、运行安装目录、运行版本亲和及数字版本排序；
目录枚举和 devices 探测都有上限，不遍历整盘、不主动重启或 kill-server。
先使用 adb devices 的真实在线序号；16384 + 32*n / 7555 仅作为旧版回退。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import native_log
import xg_exec

PACKAGE = "com.netease.sky"
DEFAULT_PORTS = [16384 + 32 * i for i in range(16)] + [7555]
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _run(argv, timeout=20):
    try:
        p = xg_exec.run(argv, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return -1, "", str(exc)
    return (p.returncode,
            (p.stdout or b"").decode("utf-8", "replace").strip(),
            (p.stderr or b"").decode("utf-8", "replace").strip())


def adb_argv(path):
    """adb 可以是 exe，也可以是测试用的 .py/.cmd。"""
    p = str(path)
    low = p.lower()
    if low.endswith(".py"):
        return [sys.executable, p]
    if low.endswith((".cmd", ".bat")):
        return [os.environ.get("COMSPEC", "cmd.exe"), "/c", p]
    return [p]


def adb_run(adb, *args, serial=None, timeout=20):
    argv = [*adb_argv(adb), *(["-s", serial] if serial else []), *args]
    return _run(argv, timeout=timeout)


# 自动发现的边界：不递归搜索磁盘，也不逐个无限等待 ADB。
MAX_PROCESS_ENTRIES = 4096
MAX_ROOTS = 96
MAX_DIRECTORY_ENTRIES = 128
MAX_FALLBACK_DIRECTORIES = 64
MAX_FALLBACK_ENTRIES = 1024
MAX_ADB_CANDIDATES = 96
MAX_ADB_PROBES = 8
ADB_PROBE_BUDGET = 8.0
GAME_DISCOVERY_BUDGET = 45.0


def _capture_config():
    try:
        import xg_paths
        value = (xg_paths.config() or {}).get("capture") or {}
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _path_key(path):
    return os.path.normcase(os.path.abspath(str(path))).casefold()


def _unique_paths(paths, limit=MAX_ROOTS):
    seen, result = set(), []
    for value in paths:
        try:
            path = Path(value)
            key = _path_key(path)
        except (TypeError, ValueError, OSError):
            continue
        if key not in seen:
            seen.add(key)
            result.append(path)
            if len(result) >= limit:
                break
    return result


def _process_executable_paths(api=None):
    """只用 Windows 系统 API；权限不足的进程跳过，所有句柄及时关闭。

    api 参数用于离线测试；正常调用不会依赖 psutil、PowerShell 或第三方模块。
    """
    if api is None and os.name != "nt":
        return []
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]

    if api is None:
        try:
            api = ctypes.WinDLL("kernel32", use_last_error=True)
            declarations = (
                ("CreateToolhelp32Snapshot", [wintypes.DWORD, wintypes.DWORD], wintypes.HANDLE),
                ("Process32FirstW", [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)], wintypes.BOOL),
                ("Process32NextW", [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)], wintypes.BOOL),
                ("OpenProcess", [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
                ("QueryFullProcessImageNameW", [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                               ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
                ("CloseHandle", [wintypes.HANDLE], wintypes.BOOL),
            )
            for name, args, result in declarations:
                function = getattr(api, name)
                function.argtypes, function.restype = args, result
        except (AttributeError, OSError):
            return []

    def close(handle):
        try:
            api.CloseHandle(handle)
        except (OSError, ValueError, AttributeError):
            pass

    try:
        snapshot = api.CreateToolhelp32Snapshot(0x00000002, 0)
    except (OSError, ValueError, AttributeError):
        return []
    invalid = ctypes.c_void_p(-1).value
    handle_value = getattr(snapshot, "value", snapshot)
    if handle_value in (None, 0, -1, invalid):
        return []
    paths = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        more = api.Process32FirstW(snapshot, ctypes.byref(entry))
        count = 0
        while more and count < MAX_PROCESS_ENTRIES:
            count += 1
            handle = None
            try:
                handle = api.OpenProcess(0x1000, False, entry.th32ProcessID)
                if getattr(handle, "value", handle):
                    length = wintypes.DWORD(32768)
                    buffer = ctypes.create_unicode_buffer(length.value)
                    if api.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)):
                        path = Path(buffer.value)
                        if path.is_absolute() and any(token in str(path).casefold()
                                                      for token in ("mumu", "nemu", "nx_")):
                            paths.append(path)
            except (OSError, ValueError, AttributeError):
                pass
            finally:
                if getattr(handle, "value", handle):
                    close(handle)
            more = api.Process32NextW(snapshot, ctypes.byref(entry))
    except (OSError, ValueError, AttributeError):
        pass
    finally:
        close(snapshot)
    return _unique_paths(paths, limit=64)


def _running_mumu_roots(process_paths=None):
    """从完整进程路径最多向上六级找明确布局，优先安装根而非内部 shell。"""
    if process_paths is None:
        process_paths = _process_executable_paths()
    roots = []
    for executable in list(process_paths)[:64]:
        path = Path(executable)
        if not any(token in str(path).casefold() for token in ("mumu", "nemu", "nx_")):
            continue
        selected, fallback = None, None
        for ancestor in list(path.parents)[:6]:
            if ancestor == ancestor.parent:
                break
            try:
                if (ancestor / "nx_device").is_dir() or (ancestor / "nx_main").is_dir():
                    selected = ancestor
                    break
                if fallback is None and (ancestor / "shell").is_dir():
                    fallback = ancestor
            except OSError:
                continue
        if selected is not None or fallback is not None:
            roots.append(selected if selected is not None else fallback)
    return _unique_paths(roots, limit=32)


def _registry_roots():
    roots = []
    if os.name != "nt":
        return roots
    try:
        import winreg
    except ImportError:
        return roots
    # 枚举厂商的直接子项以兼容未知版本，不扫描整个注册表。
    keys = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for vendor in (r"SOFTWARE\Netease", r"SOFTWARE\WOW6432Node\Netease"):
            keys.extend((hive, vendor + "\\" + name) for name in ("MuMuPlayer", "MuMuPlayer-12.0"))
            try:
                with winreg.OpenKey(hive, vendor) as key:
                    for index in range(MAX_DIRECTORY_ENTRIES):
                        try:
                            name = winreg.EnumKey(key, index)
                        except OSError:
                            break
                        if any(token in name.casefold() for token in ("mumu", "nemu", "nx_")):
                            keys.append((hive, vendor + "\\" + name))
            except OSError:
                continue
    seen = set()
    for hive, key_name in keys:
        if (hive, key_name.casefold()) in seen:
            continue
        seen.add((hive, key_name.casefold()))
        try:
            with winreg.OpenKey(hive, key_name) as key:
                for name in ("InstallPath", "InstallDir", "Path", "InstallLocation"):
                    try:
                        value, _ = winreg.QueryValueEx(key, name)
                    except OSError:
                        continue
                    if isinstance(value, str) and value.strip():
                        roots.append(Path(os.path.expandvars(value.strip().strip('"'))))
        except OSError:
            continue
    return _unique_paths(roots)


def _child_directories(root):
    result = []
    try:
        with os.scandir(root) as entries:
            for index, entry in enumerate(entries):
                if index >= MAX_DIRECTORY_ENTRIES:
                    break
                try:
                    if entry.is_dir(follow_symlinks=False):
                        result.append(Path(entry.path))
                except OSError:
                    continue
    except OSError:
        pass
    return result


def candidate_roots(*, running_roots=None):
    cfg = _capture_config()
    out = [Path(str(v)).expanduser() for v in (os.environ.get("XG_MUMU_DIR"), cfg.get("mumu_dir")) if v]
    out.extend(_running_mumu_roots() if running_roots is None else running_roots)
    out.extend(_registry_roots())
    envs = [os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
            os.environ.get("LOCALAPPDATA"), os.environ.get("ProgramData")]
    names = ["Netease/MuMuPlayer-12.0", "Netease/MuMuPlayer", "MuMuPlayer-12.0",
             "MuMuPlayer", "MuMu"]
    for base in envs:
        if not base:
            continue
        out.extend(Path(base) / name for name in names)
        for parent in (Path(base), Path(base) / "Netease"):
            out.extend(path for path in _child_directories(parent)
                       if any(token in path.name.casefold() for token in ("mumu", "nemu", "nx_")))
    out.extend(Path(fixed) for fixed in (
        r"C:\Program Files\Netease\MuMuPlayer-12.0",
        r"C:\Program Files (x86)\Netease\MuMuPlayer-12.0",
        r"D:\Program Files\Netease\MuMuPlayer-12.0", r"D:\GAME\MuMuPlayer", r"D:\MuMuPlayer-12.0"))
    result = []
    for path in _unique_paths(out):
        try:
            if path.is_dir():
                result.append(path)
        except OSError:
            continue
    return result


def _version_key(path):
    """数字而非字符串比较：15.10 > 15.9 > 12.0。"""
    parts = Path(path).parts
    version = ""
    for index, part in enumerate(parts[:-1]):
        if part.casefold() == "nx_device":
            version = parts[index + 1]
            break
    if not version:
        for part in reversed(parts):
            match = re.search(r"(?:mumuplayer|nemu)[-_](\d[\w.-]*)", part, re.I)
            if match:
                version = match.group(1)
                break
    numbers = [int(value) for value in re.findall(r"\d+", version)[:8]]
    return tuple((numbers + [0] * 8)[:8])


def _bounded_adb_fallback(root):
    """旧布局兼容：路径深度 <= 5，总目录/条目限额，不跟随链接或重解析点。"""
    root = Path(root)
    pending, found, visited, entries_seen = [(root, 0)], [], 0, 0
    while pending and visited < MAX_FALLBACK_DIRECTORIES and entries_seen < MAX_FALLBACK_ENTRIES:
        directory, depth = pending.pop(0)
        try:
            info = directory.lstat()
            if directory.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                continue
            visited += 1
            with os.scandir(directory) as entries:
                for entry in entries:
                    entries_seen += 1
                    if entries_seen > MAX_FALLBACK_ENTRIES:
                        break
                    try:
                        info = entry.stat(follow_symlinks=False)
                        if entry.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                            continue
                        if entry.name.casefold() == "adb.exe" and depth + 1 <= 5 and entry.is_file(follow_symlinks=False):
                            found.append(Path(entry.path))
                            if len(found) >= MAX_ADB_CANDIDATES:
                                return found
                        elif depth + 1 < 5 and entry.is_dir(follow_symlinks=False):
                            if len(pending) + visited < MAX_FALLBACK_DIRECTORIES:
                                pending.append((Path(entry.path), depth + 1))
                    except OSError:
                        continue
        except OSError:
            continue
    return found


def _adb_candidates_under(root):
    """先查已知布局和 nx_device 版本；未命中才进行有界旧布局搜索。"""
    root = Path(root)
    versions = sorted(_child_directories(root / "nx_device"),
                      key=lambda path: (_version_key(path), path.name.casefold()), reverse=True)
    direct = [path / "shell" / "adb.exe" for path in versions]
    direct.extend([root / "shell" / "adb.exe", root / "nx_main" / "adb.exe",
                   root / "nx_main" / "shell" / "adb.exe", root / "adb.exe",
                   root / "emulator" / "nemu" / "shell" / "adb.exe"])
    result = []
    for path in _unique_paths(direct, limit=MAX_ADB_CANDIDATES):
        try:
            if path.is_file():
                result.append(path)
        except OSError:
            continue
    return sorted(result or _bounded_adb_fallback(root), key=_version_key, reverse=True)


def _adb_under(root):
    """兼容旧调用：返回数字版本最高的已发现 adb 或 None。"""
    paths = _adb_candidates_under(root)
    return paths[0] if paths else None


def _probe_adb_devices(adb, timeout=2.0):
    code, out, _ = adb_run(adb, "devices", timeout=timeout)
    if code != 0:
        return []
    ready, header = [], False
    for line in out.splitlines():
        if line.strip() == "List of devices attached":
            header = True
            continue
        columns = line.split()
        if header and len(columns) >= 2 and columns[1] == "device" and not columns[0].endswith(":") and columns[0] not in ready:
            ready.append(columns[0])
    return ready[:32]


def find_adb():
    """返回 (adb 路径, MuMu 目录或 None)；手填优先，自动候选有界探测。"""
    cfg = _capture_config()
    for value in (os.environ.get("XG_ADB"), cfg.get("adb")):
        if value:
            path = Path(str(value)).expanduser()
            try:
                if path.is_file():
                    return path, path.parent.parent if path.parent.name.casefold() == "shell" else path.parent
            except OSError:
                continue
    processes = _process_executable_paths()
    running = _running_mumu_roots(processes)
    running_keys = {_path_key(path) for path in running}
    records, seen = [], set()
    for root in candidate_roots(running_roots=running):
        for path in _adb_candidates_under(root):
            key = _path_key(path)
            if key in seen:
                continue
            seen.add(key)
            is_running = _path_key(root) in running_keys
            affinity = is_running and any(
                _path_key(process).startswith(_path_key(root) + os.sep)
                and _version_key(process) == _version_key(path) and any(_version_key(path))
                for process in processes)
            records.append((path, root, is_running, affinity))
            if len(records) >= MAX_ADB_CANDIDATES:
                break
        if len(records) >= MAX_ADB_CANDIDATES:
            break
    found = shutil.which("adb")
    if found and _path_key(found) not in seen:
        records.append((Path(found), None, False, False))
    if not records:
        return None, None
    records.sort(key=lambda row: (row[2], row[3], _version_key(row[0])), reverse=True)
    deadline = time.monotonic() + ADB_PROBE_BUDGET
    best = records[0]
    for row in records[:MAX_ADB_PROBES]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if _probe_adb_devices(row[0], timeout=min(2.0, remaining)):
            # records 已按运行目录/版本排序，首个就绪项即最优项。
            best = row
            break
    return best[0], best[1]


def _connect(adb, serial, timeout=4):
    code, out, err = adb_run(adb, "connect", serial, timeout=timeout)
    text = (out + " " + err).lower()
    return code == 0 and "cannot" not in text and "failed" not in text and "unable" not in text


def _tcp_serial(serial):
    match = re.fullmatch(r"(?:[A-Za-z0-9_.-]+|\[[0-9a-fA-F:]+\]):(\d{1,5})", serial)
    return bool(match and 0 < int(match.group(1)) < 65536)


def find_game(adb, serials=None):
    """先查真实在线设备（含非默认端口/USB），只对未列出的 TCP 回退 connect。"""
    cfg = _capture_config()
    preferred = [os.environ.get("XG_ADB_SERIAL"), cfg.get("serial"), *(serials or [])]
    preferred = list(dict.fromkeys(str(value).strip() for value in preferred if value))[:32]
    deadline = time.monotonic() + GAME_DISCOVERY_BUDGET
    ready = _probe_adb_devices(adb, timeout=2.0)
    # 没有显式偏好时，本机模拟器排在物理 USB 设备前；端口来自真实列表。
    local = [value for value in ready if (
        value.startswith("emulator-") and value[len("emulator-"):].isdigit()
        or _tcp_serial(value) and value.rsplit(":", 1)[0].casefold() in ("127.0.0.1", "localhost", "[::1]")
    )]
    order = list(dict.fromkeys([value for value in preferred if value in ready] + local + ready))
    seen = set()

    def pid_for(serial):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        code, out, _ = adb_run(adb, "shell", "pidof " + PACKAGE, serial=serial,
                               timeout=min(8.0, remaining))
        if code == 0 and re.fullmatch(r"\d+", out or ""):
            return {"serial": serial, "pid": int(out)}
        return None

    for serial in order:
        seen.add(serial)
        result = pid_for(serial)
        if result:
            return result
    fallback = preferred + [f"127.0.0.1:{port}" for port in DEFAULT_PORTS]
    for serial in fallback:
        if serial in seen or not _tcp_serial(serial):
            continue
        seen.add(serial)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if not _connect(adb, serial, timeout=min(4.0, remaining)):
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        # connect 成功的文字不是在线证明，offline / unauthorized 均不进入 shell。
        if serial not in _probe_adb_devices(adb, timeout=min(2.0, remaining)):
            continue
        result = pid_for(serial)
        if result:
            return result
    return None


def ensure_root(adb, serial):
    code, out, _ = adb_run(adb, "shell", "id", serial=serial, timeout=8)
    if "uid=0" in (out or ""):
        return True
    adb_run(adb, "root", serial=serial, timeout=8)
    time.sleep(2)
    _connect(adb, serial)
    adb_run(adb, "wait-for-device", serial=serial, timeout=15)
    code, out, _ = adb_run(adb, "shell", "id", serial=serial, timeout=8)
    return "uid=0" in (out or "")


def game_version(adb, serial):
    code, out, _ = adb_run(adb, "shell", "dumpsys package " + PACKAGE, serial=serial, timeout=12)
    m = re.search(r"\bversionCode=(\d+)", out or "")
    return m.group(1) if m else None


def current_scene(adb, serial, pid, revision="V76318"):
    """返回 (scene, level_id) 或 (None, None)。"""
    code, out, _ = adb_run(adb, "logcat", "-d", "-v", "threadtime", "--pid=" + str(pid),
                           serial=serial, timeout=20)
    if code != 0:
        return None, None
    lines = [ln for ln in out.splitlines()
             if (m := re.match(r"^\d\d-\d\d \d\d:\d\d:\d\d\.\d{3}\s+(\d+)\s+", ln))
             and int(m.group(1)) == int(pid)]
    try:
        rows = native_log.bind(lines, revision, "entered-world")
        return (rows[-1]["scene"], rows[-1]["level_id"]) if rows else (None, None)
    except Exception:
        return None, None


def check_device(adb_path=None):
    """首次向导第 2 步：一次性检查 adb / MuMu / root / 游戏版本。"""
    adb, mumu = (Path(adb_path), None) if adb_path else find_adb()
    out = {"ok": False, "adb": str(adb) if adb else None, "mumu_dir": str(mumu) if mumu else None,
           "serial": None, "pid": None, "root": False, "version_code": None, "scene": None,
           "game_running": False, "message": ""}
    if not adb:
        out["message"] = "没找到 adb：MuMu 是不是没装？也可以在上面的输入框手填 adb.exe 路径。"
        return out
    game = find_game(adb)
    if not game:
        out["message"] = ("找到了 adb（%s），但没找到正在运行的光遇。"
                          "先在 MuMu 里装好并打开游戏、登进遇境，再点一次检查。" % adb)
        return out
    out.update(serial=game["serial"], pid=game["pid"], game_running=True)
    out["root"] = ensure_root(adb, game["serial"])
    if not out["root"]:
        out["message"] = ("找到了 MuMu（%s）和游戏，但拿不到 root。"
                          "在 MuMu 设置 → 其他设置里打开「手机 Root 权限」，重启模拟器后再试。"
                          % game["serial"])
        return out
    out["version_code"] = game_version(adb, game["serial"])
    out["scene"], _ = current_scene(adb, game["serial"], game["pid"])
    if out["version_code"] and out["version_code"] not in ("421589", "421629"):
        out["message"] = ("MuMu 里装的光遇版本是 %s，小光只适配 0.16.8 / 421629（也识别 0.16.7 / 421589）。"
                          "换成 0.16.8 的安装包再装一次。" % out["version_code"])
        return out
    out["ok"] = True
    out["message"] = "MuMu、adb、Root、游戏都正常。"
    return out
