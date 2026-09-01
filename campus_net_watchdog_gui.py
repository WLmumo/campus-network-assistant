# -*- coding: utf-8 -*-
"""燕山大学校园网助手：tkinter + Playwright + Microsoft Edge。

将本文件放到原 campus_net_watchdog.py 所在目录，即可复用
edge_campus_profile。配置含学号、网络及监控策略；密码仅由 Edge 管理。
运行：双击 校园网助手1.1.2.exe，或 py -3.13 campus_net_watchdog_gui.py
依赖：py -3.13 -m pip install requests playwright

Tk 只在主线程使用；所有 Playwright 对象在同一个后台线程创建、
操作和关闭。停止采用 Event 协作取消，不强杀线程或用户的 Edge。
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import queue
import re
import shutil
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from urllib.parse import urlsplit
import tkinter as tk
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText


# 单文件 exe 启动时，__file__ 指向临时解包目录。
# 可修改数据必须保存在用户启动的 exe 旁边，才能复用旧 Profile 并持久保存。
BASE_DIR = (Path(sys.executable) if getattr(sys, "frozen", False)
            else Path(__file__)).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"
LOG_FILE = BASE_DIR / "campus_watchdog.log"
EDGE_PROFILE_DIR = BASE_DIR / "edge_campus_profile"
SERVICES = ("校园网", "中国电信", "中国移动", "中国联通")
CHECK_INTERVAL = 30
RETRY_INTERVAL = 60
MAX_FAILURES = 3
VERIFY_TIMEOUT = 35
ACTION_TIMEOUT_MS = 5000
NAVIGATION_TIMEOUT_MS = 10000
LAUNCH_TIMEOUT_MS = 20000
CONNECTIVITY_URL = "http://www.msftconnecttest.com/connecttest.txt"
CONNECTIVITY_EXPECTED = "Microsoft Connect Test"
# 独立提供商的固定响应；只在主探测失败时访问备用 HTTPS 地址。
CONNECTIVITY_PROBES = (
    ("Microsoft HTTP", CONNECTIVITY_URL, CONNECTIVITY_EXPECTED),
    ("Apple HTTPS", "https://captive.apple.com/hotspot-detect.html",
     "<HTML><HEAD><TITLE>Success</TITLE></HEAD><BODY>Success</BODY></HTML>"),
)
DISCONNECT_CONFIRMATIONS = 3
DISCONNECT_CONFIRM_INTERVAL = 5
PORTAL_TRIGGER_URL = CONNECTIVITY_URL
AUTH_FALLBACK_URL = "https://auth1.ysu.edu.cn"
LOGGER = logging.getLogger("campus_watchdog_gui")
APP_NAME = "校园网助手1.1.2"


@dataclass(frozen=True)
class Settings:
    username: str = ""
    service_name: str = "中国电信"
    check_interval: int = CHECK_INTERVAL
    retry_interval: int = RETRY_INTERVAL
    max_failures: int = MAX_FAILURES

    @classmethod
    def from_dict(cls, data: dict) -> "Settings":
        if not isinstance(data, dict):
            raise ValueError("配置必须是 JSON 对象。")
        username = data.get("username", "")
        service = data.get("service_name", "中国电信")
        if not isinstance(username, str):
            raise ValueError("学号必须是字符串，以保留开头的 0。")
        username = username.strip()
        if len(username) > 64 or any(ch.isspace() or ord(ch) < 32 for ch in username):
            raise ValueError("学号不能超过 64 个字符，也不能包含空格或控制字符。")
        if service not in SERVICES:
            raise ValueError("网络必须是校园网、中国电信、中国移动或中国联通。")

        def integer(key, default, minimum, maximum, label):
            value = data.get(key, default)
            if isinstance(value, str) and re.fullmatch(r"[0-9]+", value.strip()):
                value = int(value.strip())
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError(f"{label}必须是 {minimum}～{maximum} 之间的整数。")
            return value

        return cls(username, service,
                   integer("check_interval", CHECK_INTERVAL, 5, 3600, "检测间隔（秒）"),
                   integer("retry_interval", RETRY_INTERVAL, 5, 3600, "失败重试间隔（秒）"),
                   integer("max_failures", MAX_FAILURES, 1, 20, "连续失败上限（次）"))


class ConfigStore:
    """原子写入；后台恢复每轮取一份不可变配置，避免半途换账号。"""

    def __init__(self, path: Path = CONFIG_FILE):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._settings = Settings()
        self.warning = ""
        self.blocked = False
        self.needs_upgrade = False
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8-sig"))
                self._settings = Settings.from_dict(data)
                self.needs_upgrade = not set(asdict(self._settings)).issubset(data)
            except (OSError, ValueError, TypeError) as exc:
                backup = self.path.with_name(
                    f"config.invalid-{time.time_ns()}.json")
                try:
                    shutil.copy2(self.path, backup)
                    self.warning = f"配置无法读取，已备份为 {backup.name}；请重新填写配置。"
                except OSError:
                    self.blocked = True
                    self.warning = "配置无法读取且备份失败。请检查目录权限，修复 config.json 后重启。"
                LOGGER.warning("%s（%s）", self.warning, type(exc).__name__)

    def snapshot(self) -> Settings:
        with self._lock:
            return self._settings

    def save(self, settings: Settings) -> None:
        settings = Settings.from_dict(asdict(settings))
        if self.blocked:
            raise OSError(self.warning)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent,
                prefix=".campus-config-", suffix=".tmp", delete=False,
            ) as stream:
                temporary = Path(stream.name)
                json.dump(asdict(settings), stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            with self._lock:
                self._settings = settings
                self.needs_upgrade = False
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


class GUILogHandler(logging.Handler):
    """不调用任何 Tk API；队列满时丢弃最旧的窗口日志，文件日志不受影响。"""

    def __init__(self, messages: queue.Queue):
        super().__init__()
        self.messages = messages

    def emit(self, record):
        try:
            item = (record.levelname, self.format(record))
            try:
                self.messages.put_nowait(item)
            except queue.Full:
                try:
                    self.messages.get_nowait()
                except queue.Empty:
                    pass
                self.messages.put_nowait(item)
        except Exception:
            self.handleError(record)


def setup_logging(messages: queue.Queue, path: Path = LOG_FILE) -> None:
    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False
    for handler in LOGGER.handlers[:]:
        LOGGER.removeHandler(handler)
        handler.close()
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s", "%Y-%m-%d %H:%M:%S")
    file_handler = RotatingFileHandler(
        path, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
    gui_handler = GUILogHandler(messages)
    for handler in (file_handler, gui_handler):
        handler.setFormatter(formatter)
        LOGGER.addHandler(handler)


class SingleInstance:
    """同一专用 Profile 的 GUI 在一个 Windows 登录会话内只允许运行一份。"""

    def __init__(self, profile: Path):
        self.handle = None
        self.kernel = None
        if os.name != "nt":
            return
        from ctypes import wintypes
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
        self.kernel.CreateMutexW.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        self.kernel.CloseHandle.restype = wintypes.BOOL
        name = hashlib.sha256(str(profile.resolve()).casefold().encode()).hexdigest()[:24]
        self.handle = self.kernel.CreateMutexW(None, False, "Local\\CampusWatchdogGUI_" + name)
        error = ctypes.get_last_error()
        if not self.handle:
            raise OSError(error, "无法建立程序实例锁。")
        if error == 183:  # ERROR_ALREADY_EXISTS
            self.close()
            raise RuntimeError("校园网助手已在运行，请单击任务栏右下角的托盘图标恢复窗口；也可查看已有窗口。")

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class WindowsTray(threading.Thread):
    """原生通知区域图标。独立消息线程只向 Tk 队列发送事件，不调用 Tk。"""

    CALLBACK_MESSAGE = 0x8001
    SHOW_COMMAND = 1
    EXIT_COMMAND = 2

    def __init__(self, events):
        super().__init__(name="CampusTray", daemon=False)
        self.events = events
        self.ready = threading.Event()
        self.available = threading.Event()
        self.stop_requested = threading.Event()
        self.hwnd = None
        self.registered = False
        self.user32 = None
        self.shell32 = None
        self.nid = None
        self.taskbar_created = 0
        self.version4 = False

    def emit(self, action):
        self.events.put(("tray_action", {"action": action}))

    def stop(self):
        self.stop_requested.set()
        if self.hwnd and self.user32:
            # 取消可能打开的右键菜单，再结束消息循环。
            self.user32.PostMessageW(self.hwnd, 0x001F, 0, 0)  # WM_CANCELMODE
            self.user32.PostMessageW(self.hwnd, 0x0010, 0, 0)  # WM_CLOSE

    def _add_icon(self):
        if self.stop_requested.is_set():
            return
        self.nid.uVersion = 4
        self.registered = bool(self.shell32.Shell_NotifyIconW(0, ctypes.byref(self.nid)))
        if self.registered:
            self.version4 = bool(self.shell32.Shell_NotifyIconW(4, ctypes.byref(self.nid)))
            self.available.set()
        else:
            self.available.clear()
            self.events.put(("tray_unavailable", {}))

    def _remove_icon(self):
        if self.registered:
            self.shell32.Shell_NotifyIconW(2, ctypes.byref(self.nid))
        self.registered = False
        self.available.clear()

    def _dispatch_command(self, command):
        if command == self.SHOW_COMMAND:
            self.emit("show")
        elif command == self.EXIT_COMMAND:
            self.emit("exit")

    def _show_menu(self):
        from ctypes import wintypes
        menu = self.user32.CreatePopupMenu()
        if not menu:
            self.emit("show")
            return
        try:
            self.user32.AppendMenuW(menu, 0x0000, self.SHOW_COMMAND, "显示窗口")
            self.user32.AppendMenuW(menu, 0x0800, 0, None)  # MF_SEPARATOR
            self.user32.AppendMenuW(menu, 0x0000, self.EXIT_COMMAND, "退出程序")
            self.user32.SetMenuDefaultItem(menu, self.SHOW_COMMAND, False)
            position = wintypes.POINT()
            self.user32.GetCursorPos(ctypes.byref(position))
            self.user32.SetForegroundWindow(self.hwnd)
            command = self.user32.TrackPopupMenu(
                menu, 0x0102, position.x, position.y, 0, self.hwnd, None)
            self.user32.PostMessageW(self.hwnd, 0, 0, 0)
            self._dispatch_command(command)
        finally:
            self.user32.DestroyMenu(menu)

    def _window_proc(self, hwnd, message, wparam, lparam):
        try:
            if message == self.CALLBACK_MESSAGE:
                event = lparam & 0xFFFF
                if event in (0x0202, 0x0203, 0x0400, 0x0401):
                    self.emit("show")  # 点击、双击或键盘激活
                elif event in (0x0205, 0x007B):
                    self._show_menu()
                return 0
            if message == 0x0111:  # WM_COMMAND：菜单命令
                self._dispatch_command(wparam & 0xFFFF)
                return 0
            if self.taskbar_created and message == self.taskbar_created:
                # Explorer 重启后，通知区域图标需要重新添加。
                self.registered = False
                self.available.clear()
                self._add_icon()
                return 0
            if message == 0x0113 and not self.registered:  # WM_TIMER
                self._add_icon()
                return 0
            if message == 0x0011:  # WM_QUERYENDSESSION：不阻止系统注销/关机
                return 1
            if message == 0x0016 and wparam:  # WM_ENDSESSION
                self.emit("exit")
                return 0
            if message == 0x0010:
                self._remove_icon()
                self.user32.DestroyWindow(hwnd)
                return 0
            if message == 0x0002:  # WM_DESTROY
                self.user32.PostQuitMessage(0)
                return 0
        except Exception as exc:
            LOGGER.error("托盘操作失败（%s）。", type(exc).__name__)
            self.events.put(("tray_unavailable", {}))
        return self.user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def run(self):
        if os.name != "nt":
            self.ready.set()
            return
        from ctypes import wintypes
        user = ctypes.WinDLL("user32", use_last_error=True)
        shell = ctypes.WinDLL("shell32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.user32, self.shell32 = user, shell
        pointer_int = ctypes.c_size_t
        signed_pointer = ctypes.c_ssize_t
        wndproc_type = ctypes.WINFUNCTYPE(signed_pointer, wintypes.HWND,
                                         wintypes.UINT, pointer_int, signed_pointer)

        class WindowClass(ctypes.Structure):
            _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", wndproc_type),
                        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                        ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HANDLE),
                        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HANDLE),
                        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                        ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

        class NotifyIconData(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
                        ("uID", wintypes.UINT), ("uFlags", wintypes.UINT),
                        ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HANDLE),
                        ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD),
                        ("dwStateMask", wintypes.DWORD), ("szInfo", wintypes.WCHAR * 256),
                        ("uVersion", wintypes.UINT), ("szInfoTitle", wintypes.WCHAR * 64),
                        ("dwInfoFlags", wintypes.DWORD), ("guidItem", GUID),
                        ("hBalloonIcon", wintypes.HANDLE)]

        def signature(dll, name, arguments, result):
            function = getattr(dll, name)
            function.argtypes, function.restype = arguments, result

        signature(kernel, "GetModuleHandleW", [wintypes.LPCWSTR], wintypes.HINSTANCE)
        signature(user, "RegisterClassW", [ctypes.POINTER(WindowClass)], wintypes.WORD)
        signature(user, "UnregisterClassW", [wintypes.LPCWSTR, wintypes.HINSTANCE], wintypes.BOOL)
        signature(user, "CreateWindowExW", [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                  wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                  wintypes.HWND, wintypes.HANDLE, wintypes.HINSTANCE, ctypes.c_void_p], wintypes.HWND)
        signature(user, "DefWindowProcW", [wintypes.HWND, wintypes.UINT, pointer_int, signed_pointer], signed_pointer)
        signature(user, "PostMessageW", [wintypes.HWND, wintypes.UINT, pointer_int, signed_pointer], wintypes.BOOL)
        signature(user, "DestroyWindow", [wintypes.HWND], wintypes.BOOL)
        signature(user, "IsWindow", [wintypes.HWND], wintypes.BOOL)
        signature(user, "PostQuitMessage", [ctypes.c_int], None)
        signature(user, "LoadIconW", [wintypes.HINSTANCE, ctypes.c_void_p], wintypes.HANDLE)
        signature(user, "RegisterWindowMessageW", [wintypes.LPCWSTR], wintypes.UINT)
        signature(user, "SetTimer", [wintypes.HWND, pointer_int, wintypes.UINT, ctypes.c_void_p], pointer_int)
        signature(user, "GetMessageW", [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT], ctypes.c_int)
        signature(user, "TranslateMessage", [ctypes.POINTER(wintypes.MSG)], wintypes.BOOL)
        signature(user, "DispatchMessageW", [ctypes.POINTER(wintypes.MSG)], signed_pointer)
        signature(user, "CreatePopupMenu", [], wintypes.HANDLE)
        signature(user, "AppendMenuW", [wintypes.HANDLE, wintypes.UINT, pointer_int, wintypes.LPCWSTR], wintypes.BOOL)
        signature(user, "SetMenuDefaultItem", [wintypes.HANDLE, wintypes.UINT, wintypes.UINT], wintypes.BOOL)
        signature(user, "GetCursorPos", [ctypes.POINTER(wintypes.POINT)], wintypes.BOOL)
        signature(user, "SetForegroundWindow", [wintypes.HWND], wintypes.BOOL)
        signature(user, "TrackPopupMenu", [wintypes.HANDLE, wintypes.UINT, ctypes.c_int, ctypes.c_int,
                  ctypes.c_int, wintypes.HWND, ctypes.c_void_p], wintypes.UINT)
        signature(user, "DestroyMenu", [wintypes.HANDLE], wintypes.BOOL)
        signature(shell, "Shell_NotifyIconW", [wintypes.DWORD, ctypes.POINTER(NotifyIconData)], wintypes.BOOL)
        module = kernel.GetModuleHandleW(None)
        class_name = f"CampusNetTray_{os.getpid()}_{id(self)}"
        self.callback = wndproc_type(self._window_proc)  # 保留回调，直到窗口及窗口类销毁。
        window_class = WindowClass()
        window_class.lpfnWndProc = self.callback
        window_class.hInstance = module
        window_class.lpszClassName = class_name
        atom = None
        try:
            if self.stop_requested.is_set():
                return
            atom = user.RegisterClassW(ctypes.byref(window_class))
            if not atom:
                raise ctypes.WinError(ctypes.get_last_error())
            self.hwnd = user.CreateWindowExW(0, class_name, APP_NAME + " Tray", 0,
                                            0, 0, 0, 0, None, None, module, None)
            if not self.hwnd:
                raise ctypes.WinError(ctypes.get_last_error())
            self.taskbar_created = user.RegisterWindowMessageW("TaskbarCreated")
            self.nid = NotifyIconData()
            self.nid.cbSize = ctypes.sizeof(NotifyIconData)
            self.nid.hWnd = self.hwnd
            self.nid.uID = 1
            self.nid.uFlags = 0x0087  # MESSAGE | ICON | TIP | SHOWTIP
            self.nid.uCallbackMessage = self.CALLBACK_MESSAGE
            self.nid.hIcon = user.LoadIconW(None, ctypes.c_void_p(32516))  # 共享系统信息图标
            self.nid.szTip = APP_NAME
            self._add_icon()
            self.ready.set()
            user.SetTimer(self.hwnd, 1, 5000, None)
            if self.stop_requested.is_set():
                return
            message = wintypes.MSG()
            while True:
                result = user.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result == 0:
                    break
                if result == -1:
                    raise ctypes.WinError(ctypes.get_last_error())
                user.TranslateMessage(ctypes.byref(message))
                user.DispatchMessageW(ctypes.byref(message))
        except Exception as exc:
            LOGGER.warning("系统托盘暂不可用（%s），关闭窗口时将最小化到任务栏。", type(exc).__name__)
            self.events.put(("tray_unavailable", {}))
        finally:
            self.ready.set()
            if self.nid is not None:
                self._remove_icon()
            if self.hwnd and user.IsWindow(self.hwnd):
                user.DestroyWindow(self.hwnd)
            self.hwnd = None
            if atom:
                user.UnregisterClassW(class_name, module)


class StopRequested(Exception):
    pass


class RecoveryEngine:
    """基于原脚本的页面认证流程；实例及其浏览器仅由工作线程持有。"""

    def __init__(self, stop: threading.Event, emit, profile: Path = EDGE_PROFILE_DIR):
        # 在后台导入，缺少依赖时 GUI 仍可打开并显示安装提示。
        try:
            import requests
            from playwright.sync_api import sync_playwright, Error
        except ImportError as exc:
            raise RuntimeError(
                "缺少依赖，请用运行本程序的 Python 执行："
                "python -m pip install requests playwright") from exc
        self.requests = requests
        self.sync_playwright = sync_playwright
        self.playwright_error = Error
        self.stop = stop
        self.emit = emit
        self.profile = Path(profile)
        self.session = requests.Session()
        # 检测校园网直接联网情况，避免系统/环境代理导致假阳性。
        self.session.trust_env = False
        self.network_confirmed_offline = False
        self.last_probe_detail = ""
        self.last_success_probe = None
        self._trace_serial = 0
        self._trace_id = None
        self._trace_started = None
        self._trace_step = 0
        self._trace_last_action = "尚未启动浏览器操作"

    def close(self):
        if getattr(self, "_trace_id", None) is not None:
            self.finish_recovery_trace(False, browser_ok=None,
                                       detail="监控停止或后台流程提前结束")
        self.session.close()

    @staticmethod
    def safe_page_address(page) -> str:
        """只记录页面定位所需的信息；去掉查询、fragment 和 Portal 分号参数。"""
        try:
            address = urlsplit(page.url)
        except Exception:
            return "<无法读取页面地址>"
        scheme = address.scheme or "unknown"
        host = address.hostname or "<无主机名>"
        path = (address.path or "/").split(";", 1)[0]
        # 防止异常页面把过长标识符放进路径并进入日志。
        path = re.sub(r"(?i)(token|ticket|session|code)[^/]{0,128}", r"\1=<已隐藏>", path)
        path = re.sub(r"/[A-Za-z0-9_-]{48,}(?=/|$)", "/<长标识已隐藏>", path)
        return f"{scheme}://{host}{path}"

    def begin_recovery_trace(self, settings: Settings, attempt: int,
                             failure_limit: int, trigger_reason: str) -> str:
        """建立一次可关联的恢复记录；不写学号、密码、Cookie 或完整认证 URL。"""
        if getattr(self, "_trace_id", None) is not None:
            self.finish_recovery_trace(False, browser_ok=None,
                                       detail="新的恢复尝试开始前，上一条追踪尚未正常结束")
        self._trace_serial += 1
        self._trace_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{self._trace_serial:02d}"
        self._trace_started = time.monotonic()
        self._trace_step = 0
        self._trace_last_action = "建立恢复追踪"
        self.recovery_log(
            "开始自动恢复；触发原因=%s；尝试=%s/%s；网络服务=%s；断网探测=%s。",
            trigger_reason, attempt, failure_limit, settings.service_name,
            self.last_probe_detail or "无详细结果", level=logging.WARNING)
        return self._trace_id

    def recovery_log(self, message: str, *args, level=logging.INFO) -> None:
        """为恢复操作添加追踪编号、步骤序号和相对耗时。"""
        if getattr(self, "_trace_id", None) is None:
            LOGGER.log(level, message, *args)
            return
        rendered = message % args if args else message
        self._trace_step += 1
        elapsed = max(0.0, time.monotonic() - self._trace_started)
        self._trace_last_action = rendered[:160]
        LOGGER.log(level, "[恢复 %s | 步骤 %02d | +%.1fs] %s",
                   self._trace_id, self._trace_step, elapsed, rendered)

    def finish_recovery_trace(self, success: bool, *, browser_ok=None,
                              detail: str = "") -> None:
        if getattr(self, "_trace_id", None) is None:
            return
        browser_result = ("未取得结果" if browser_ok is None else
                          "流程返回成功" if browser_ok else "流程未确认成功")
        result = "恢复成功" if success else "恢复失败"
        suffix = f"；{detail}" if detail else ""
        self.recovery_log("结束本次追踪：%s；浏览器=%s；最终探测=%s%s。",
                          result, browser_result,
                          self.last_probe_detail or "无详细结果", suffix,
                          level=logging.INFO if success else logging.WARNING)
        self._trace_id = None
        self._trace_started = None
        self._trace_step = 0
        self._trace_last_action = "追踪已结束"

    def check_stop(self):
        if self.stop.is_set():
            raise StopRequested()

    def pause(self, seconds: float, page=None):
        deadline = time.monotonic() + seconds
        while True:
            self.check_stop()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            if page is None:
                self.stop.wait(min(0.1, remaining))
            else:
                # 让 Playwright 继续处理浏览器事件，同时每 100ms 检查取消。
                page.wait_for_timeout(min(100, remaining * 1000))

    def probe_endpoint(self, name, url, expected):
        """限定响应大小、拒绝重定向；只返回固定诊断文本，不记录响应内容。"""
        self.check_stop()
        deadline = time.monotonic() + 8
        try:
            with self.session.get(
                url, timeout=(3, 3), allow_redirects=False, stream=True,
                headers={"Cache-Control": "no-cache", "Pragma": "no-cache"},
            ) as response:
                if response.status_code != 200:
                    return False, f"HTTP {response.status_code}"
                payload = bytearray()
                for piece in response.iter_content(chunk_size=1):
                    self.check_stop()
                    if time.monotonic() > deadline:
                        return False, "响应读取超时"
                    if len(payload) >= 256:
                        return False, "响应超过预期大小"
                    payload.extend(piece)
                self.check_stop()
                content = payload.decode("utf-8", errors="replace").strip()
                if content == expected:
                    return True, "成功"
                return False, "响应内容不符（可能被认证页拦截）"
        except self.requests.Timeout:
            return False, "请求超时"
        except self.requests.exceptions.SSLError:
            return False, "HTTPS 证书校验失败"
        except self.requests.ConnectionError:
            return False, "连接或 DNS 失败"
        except self.requests.RequestException:
            return False, "请求失败"

    def internet_ok(self) -> bool:
        """一轮多站点探测：任意固定响应通过即成功，单轮失败不新判定断网。"""
        details = []
        for index, (name, url, expected) in enumerate(CONNECTIVITY_PROBES):
            self.check_stop()
            ok, reason = self.probe_endpoint(name, url, expected)
            self.check_stop()
            details.append(f"{name}：{reason}")
            if ok:
                self.last_probe_detail = "；".join(details)
                if index and self.last_success_probe != name:
                    LOGGER.info("主探测未通过，但备用探测正常，不触发恢复。%s", self.last_probe_detail)
                self.last_success_probe = name
                self.network_confirmed_offline = False
                self.emit("network", online=True, confirmed=True, checked_at=time.time())
                return True
        self.last_probe_detail = "；".join(details)
        self.last_success_probe = None
        self.check_stop()
        self.emit("network", online=False, confirmed=self.network_confirmed_offline, checked_at=time.time())
        return False

    def confirmed_internet_ok(self, phase="断网确认") -> bool:
        """自动恢复和失败计数前进行连续复核；取消、单次抖动均不算恢复失败。"""
        for attempt in range(1, DISCONNECT_CONFIRMATIONS + 1):
            if getattr(self, "_trace_id", None) is not None:
                self.recovery_log("%s：开始第 %s/%s 轮联网探测。",
                                  phase, attempt, DISCONNECT_CONFIRMATIONS)
            if self.internet_ok():
                if getattr(self, "_trace_id", None) is not None:
                    self.recovery_log("%s：第 %s/%s 轮通过；%s。",
                                      phase, attempt, DISCONNECT_CONFIRMATIONS,
                                      self.last_probe_detail)
                if attempt > 1:
                    LOGGER.info("联网复核已通过，刚才的探测异常未持续，不按持续断网处理。")
                return True
            if getattr(self, "_trace_id", None) is not None:
                self.recovery_log("%s：第 %s/%s 轮未通过；%s。",
                                  phase, attempt, DISCONNECT_CONFIRMATIONS,
                                  self.last_probe_detail, level=logging.WARNING)
            if attempt < DISCONNECT_CONFIRMATIONS:
                LOGGER.warning("联网探测第 %s/%s 轮未通过，%s 秒后复核，暂不启动认证。%s",
                               attempt, DISCONNECT_CONFIRMATIONS, DISCONNECT_CONFIRM_INTERVAL,
                               self.last_probe_detail)
                self.emit("state", text="确认网络中", attempt=attempt,
                          total=DISCONNECT_CONFIRMATIONS, interval=DISCONNECT_CONFIRM_INTERVAL)
                self.pause(DISCONNECT_CONFIRM_INTERVAL)
        self.check_stop()
        self.network_confirmed_offline = True
        self.emit("network", online=False, confirmed=True, checked_at=time.time())
        LOGGER.warning("连续 %s 轮所有探测站点均未通过，判定为持续联网异常。%s",
                       DISCONNECT_CONFIRMATIONS, self.last_probe_detail)
        return False

    def wait_for_internet(self, page, timeout: float = VERIFY_TIMEOUT) -> bool:
        started = time.monotonic()
        checks = 0
        self.recovery_log("开始等待联网恢复；最长等待 %.1f 秒，每约 2 秒复核一次。", timeout)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            checks += 1
            if self.internet_ok():
                self.recovery_log("联网等待通过；共探测 %s 次，耗时 %.1f 秒；%s。",
                                  checks, time.monotonic() - started, self.last_probe_detail)
                return True
            self.pause(min(2, max(0, deadline - time.monotonic())), page)
        self.recovery_log("联网等待超时；共探测 %s 次，耗时 %.1f 秒；最后结果=%s。",
                          checks, time.monotonic() - started,
                          self.last_probe_detail or "无详细结果", level=logging.WARNING)
        return False

    def first_visible(self, locator):
        self.check_stop()
        try:
            for index in range(locator.count()):
                item = locator.nth(index)
                if item.is_visible():
                    return item
        except self.playwright_error:
            pass
        return None

    def find_username_input(self, page):
        for loc in (page.get_by_placeholder(re.compile(r"学号|工号")),
                    page.locator('input[type="text"]')):
            found = self.first_visible(loc)
            if found is not None:
                return found
        return None

    def find_password_input(self, page):
        return self.first_visible(page.locator('input[type="password"]'))

    @staticmethod
    def school_page(page, https_only=False) -> bool:
        address = urlsplit(page.url)
        host = (address.hostname or "").lower()
        return (host == "ysu.edu.cn" or host.endswith(".ysu.edu.cn")) and (
            address.scheme == "https" if https_only else address.scheme in ("http", "https"))

    def on_login_page(self, page) -> bool:
        return self.school_page(page, https_only=True) and (
            self.find_username_input(page) is not None and
            self.find_password_input(page) is not None)

    def on_service_page(self, page, settings: Settings) -> bool:
        return self.school_page(page) and any(
            self.first_visible(page.get_by_text(text, exact=True)) is not None
            for text in ("请选择服务", settings.service_name))

    def describe_page(self, page, settings: Settings) -> str:
        """返回固定分类，不采集标题、DOM、表单内容或认证参数。"""
        if self.on_login_page(page):
            return "统一身份认证登录页"
        if self.on_service_page(page, settings):
            return "网络服务选择页"
        if self.school_page(page):
            return "学校认证相关页面（未匹配已知控件）"
        address = urlsplit(page.url)
        if address.scheme in ("about", "edge", "chrome"):
            return "浏览器内部页面或空白页"
        return "非学校页面或未知页面"

    def goto_safely(self, page, url: str, label="目标页面"):
        self.check_stop()
        self.recovery_log("导航开始：%s；等待页面 DOM，超时上限 %.1f 秒。",
                          label, NAVIGATION_TIMEOUT_MS / 1000)
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
            status = getattr(response, "status", None)
            self.recovery_log("导航完成：%s；HTTP 状态=%s；当前页面=%s。",
                              label, status if status is not None else "无",
                              self.safe_page_address(page))
        except self.playwright_error as exc:
            # 不把完整 URL、页面内容或浏览器调用参数写入日志。
            self.recovery_log("导航暂未完成：%s；异常=%s；当前页面=%s；继续识别页面。",
                              label, type(exc).__name__, self.safe_page_address(page),
                              level=logging.WARNING)
        self.check_stop()

    def trigger_portal(self, page, settings: Settings):
        self.recovery_log("尝试通过 Microsoft HTTP 探测页触发校园网 Portal。")
        self.goto_safely(page, PORTAL_TRIGGER_URL, "Microsoft HTTP Portal 触发页")
        self.recovery_log("等待 Portal 重定向稳定，持续 2.5 秒。")
        self.pause(2.5, page)
        page_type = self.describe_page(page, settings)
        self.recovery_log("HTTP 触发后的页面识别：%s；当前页面=%s。",
                          page_type, self.safe_page_address(page))
        if page_type in ("统一身份认证登录页", "网络服务选择页",
                          "学校认证相关页面（未匹配已知控件）"):
            return
        self.recovery_log("HTTP 触发后未进入学校页面，改为直接打开认证服务器。",
                          level=logging.WARNING)
        self.goto_safely(page, AUTH_FALLBACK_URL, "学校认证服务器")
        self.recovery_log("等待认证服务器页面稳定，持续 2.5 秒。")
        self.pause(2.5, page)
        self.recovery_log("直接导航后的页面识别：%s；当前页面=%s。",
                          self.describe_page(page, settings), self.safe_page_address(page))

    def password_ready(self, username_input, password_input, username: str) -> bool:
        self.check_stop()
        try:
            # 只从 DOM 取“有无密码”的布尔值，密码本身不进入 Python。
            filled = password_input.evaluate("element => element.value.length > 0")
            return bool(filled) and username_input.input_value().strip() == username
        except self.playwright_error:
            return False

    def try_saved_password_autofill(self, page, username_input, password_input, settings) -> bool:
        self.recovery_log("已识别登录表单；开始填写配置中的学号并调用 Edge 已保存密码（日志不记录学号和密码）。")
        self.check_stop()
        # 清空页面上可能残留的旧账号密码，再触发 Edge 对当前学号的填充。
        # 这里只写入空字符串；不会读取或保存任何密码。
        password_input.fill("")
        username_input.click()
        page.keyboard.press("Control+A")
        page.keyboard.press("Backspace")
        page.keyboard.type(settings.username, delay=70)
        self.pause(1, page)
        page.keyboard.press("ArrowDown")
        self.pause(0.25, page)
        page.keyboard.press("Enter")
        self.pause(1.2, page)
        if self.password_ready(username_input, password_input, settings.username):
            self.recovery_log("第一次自动填充检查通过：密码框已有值，页面学号与当前配置一致。")
            return True
        self.recovery_log("第一次自动填充检查未通过；改从密码框调用 Edge 已保存密码。",
                          level=logging.WARNING)
        password_input.click()
        self.pause(0.5, page)
        page.keyboard.press("ArrowDown")
        self.pause(0.25, page)
        page.keyboard.press("Enter")
        self.pause(1.2, page)
        if self.password_ready(username_input, password_input, settings.username):
            self.recovery_log("第二次自动填充检查通过：密码框已有值，页面学号与当前配置一致。")
            return True
        self.recovery_log("自动填充失败或页面学号与当前配置不一致；未点击登录。请使用“配置已保存密码”检查。",
                          level=logging.ERROR)
        return False

    def check_seven_day_checkbox(self, page):
        self.check_stop()
        try:
            checkbox = self.first_visible(page.get_by_role("checkbox", name=re.compile(r"7\s*天免登录")))
            if checkbox is None:
                checkbox = self.first_visible(page.locator('input[type="checkbox"]'))
            if checkbox is not None:
                if not checkbox.is_checked():
                    checkbox.check()
                    self.recovery_log("已勾选“7天免登录”。")
                else:
                    self.recovery_log("“7天免登录”原本已勾选，无需修改。")
                return
            label = self.first_visible(page.get_by_text(re.compile(r"7\s*天免登录")))
            if label is not None:
                label.click()
                self.pause(0.3, page)
                self.recovery_log("已通过文字标签尝试启用“7天免登录”。")
            else:
                self.recovery_log("页面未显示“7天免登录”选项，继续登录。")
        except self.playwright_error:
            self.recovery_log("未能确认“7天免登录”状态，继续登录。",
                              level=logging.WARNING)

    def click_first(self, candidates, description: str) -> bool:
        for index, loc in enumerate(candidates, 1):
            self.check_stop()
            found = self.first_visible(loc)
            if found is not None:
                try:
                    self.recovery_log("已通过候选定位方式 %s/%s 找到控件：%s；准备点击。",
                                      index, len(candidates), description)
                    found.click()
                    self.check_stop()
                    self.recovery_log("控件点击完成：%s。", description)
                    return True
                except self.playwright_error as exc:
                    self.recovery_log("控件点击未完成：%s；候选方式=%s/%s；异常=%s；继续尝试。",
                                      description, index, len(candidates), type(exc).__name__,
                                      level=logging.WARNING)
                    continue
        self.recovery_log("所有候选定位方式均失败，无法找到或点击：%s。", description,
                          level=logging.ERROR)
        return False

    def click_login(self, page, settings: Settings) -> bool:
        if not self.school_page(page, https_only=True):
            self.recovery_log("当前页面不是学校 HTTPS 页面，已停止自动填写学号；当前页面=%s。",
                              self.safe_page_address(page), level=logging.ERROR)
            return False
        username_input = self.find_username_input(page)
        password_input = self.find_password_input(page)
        if username_input is None or password_input is None:
            self.recovery_log("登录页控件识别失败：学号输入框=%s，密码输入框=%s。",
                              "已找到" if username_input is not None else "未找到",
                              "已找到" if password_input is not None else "未找到",
                              level=logging.ERROR)
            return False
        self.recovery_log("登录页控件识别完成：学号输入框和密码输入框均已找到。")
        if not self.try_saved_password_autofill(page, username_input, password_input, settings):
            return False
        self.check_seven_day_checkbox(page)
        ok = self.click_first([
            page.get_by_role("button", name=re.compile(r"登\s*录")),
            page.locator("button").filter(has_text=re.compile(r"登\s*录")),
            page.get_by_text(re.compile(r"^登\s*录$")),
        ], "点击“登录”")
        if ok:
            self.pause(2.5, page)
        return ok

    def click_service_and_confirm(self, page, settings: Settings) -> bool:
        service = self.first_visible(page.get_by_text(settings.service_name, exact=True))
        if service is None:
            self.recovery_log("服务选择失败：页面中没有识别到“%s”。", settings.service_name,
                              level=logging.ERROR)
            return False
        self.recovery_log("已识别目标网络服务“%s”，准备选择。", settings.service_name)
        self.check_stop()
        service.click()
        self.recovery_log("网络服务选择完成：%s。", settings.service_name)
        self.pause(0.5, page)
        return self.click_first([
            page.get_by_role("button", name=re.compile(r"确\s*定")),
            page.locator("button").filter(has_text=re.compile(r"确\s*定")),
            page.get_by_text("确定", exact=True),
        ], "点击“确定”")

    def handle_portal(self, page, settings: Settings) -> bool:
        deadline = time.monotonic() + 60
        login_attempted = service_attempted = False
        last_page_type = None
        self.recovery_log("开始处理认证页面；最长处理 60 秒。")
        while time.monotonic() < deadline:
            self.check_stop()
            if self.internet_ok():
                self.recovery_log("认证页面处理期间联网探测已通过；%s。", self.last_probe_detail)
                return True
            self.pause(0.7, page)
            page_type = self.describe_page(page, settings)
            if page_type != last_page_type:
                self.recovery_log("页面状态变化：%s；当前页面=%s。",
                                  page_type, self.safe_page_address(page))
                last_page_type = page_type
            if page_type == "网络服务选择页" and not service_attempted:
                service_attempted = True
                self.recovery_log("进入网络服务选择步骤；目标服务=%s。", settings.service_name)
                if self.click_service_and_confirm(page, settings):
                    if self.wait_for_internet(page, min(VERIFY_TIMEOUT, max(0, deadline - time.monotonic()))):
                        self.recovery_log("选择 %s 并确认后 Internet 恢复成功。",
                                          settings.service_name)
                        return True
                    self.recovery_log("点击“确定”后等待期结束，Internet 仍未恢复。",
                                      level=logging.WARNING)
                continue
            if page_type == "统一身份认证登录页" and not login_attempted:
                login_attempted = True
                self.recovery_log("进入统一身份认证登录步骤。")
                if not self.click_login(page, settings):
                    return False
                continue
            self.pause(1, page)
        online = self.internet_ok()
        self.recovery_log("认证页面处理达到 60 秒上限；最终即时探测=%s；%s。",
                          "通过" if online else "未通过",
                          self.last_probe_detail or "无详细结果",
                          level=logging.INFO if online else logging.WARNING)
        return online

    def browser_session(self, settings: Settings, setup=False, debug=False, *,
                        recovery_attempt=None, failure_limit=None,
                        trigger_reason="自动监控检测") -> bool:
        self.check_stop()
        profile_existed = self.profile.exists()
        if not setup and recovery_attempt is not None:
            self.begin_recovery_trace(settings, recovery_attempt,
                                      failure_limit or settings.max_failures,
                                      trigger_reason)
        if not setup:
            self.recovery_log("准备专用 Edge Profile；目录状态=%s；不会使用普通 Edge Profile。",
                              "已存在" if profile_existed else "首次创建")
        self.profile.mkdir(parents=True, exist_ok=True)
        context = None
        with self.sync_playwright() as playwright:
            try:
                self.check_stop()
                if not setup:
                    self.recovery_log("Playwright 已初始化；开始启动 Microsoft Edge 专用实例，最长等待 %.1f 秒。",
                                      LAUNCH_TIMEOUT_MS / 1000)
                context = playwright.chromium.launch_persistent_context(
                    user_data_dir=str(self.profile), channel="msedge",
                    headless=False, no_viewport=True, args=["--start-maximized"],
                    timeout=LAUNCH_TIMEOUT_MS,
                )
                self.check_stop()
                if not setup:
                    self.recovery_log("专用 Edge 启动成功；初始页面数量=%s。", len(context.pages))
                page = context.pages[0] if context.pages else context.new_page()
                page.set_default_timeout(ACTION_TIMEOUT_MS)
                page.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)
                if setup:
                    self.goto_safely(page, AUTH_FALLBACK_URL, "学校认证服务器")
                    LOGGER.info("请在专用 Edge 中手工登录并保存密码；完成后关闭该 Edge 窗口，或点击“结束配置”。")
                    while context.pages:
                        self.check_stop()
                        current = context.pages[0]
                        try:
                            self.pause(0.2, current)
                        except self.playwright_error:
                            if not context.pages:
                                break
                            raise
                    return True
                self.trigger_portal(page, settings)
                self.recovery_log("Portal 触发阶段结束；进入页面处理；当前页面=%s。",
                                  self.safe_page_address(page))
                ok = self.handle_portal(page, settings)
                self.recovery_log("浏览器内认证流程返回：%s。",
                                  "成功" if ok else "未确认成功",
                                  level=logging.INFO if ok else logging.WARNING)
                if not ok:
                    self.recovery_log("本次浏览器流程未成功；可能原因包括基础网络、页面结构、账号保存密码或 Portal 状态。",
                                      level=logging.WARNING)
                    if debug:
                        self.recovery_log("调试模式：保留浏览器 120 秒；可随时点击停止或关闭脚本。")
                        self.pause(120, page)
                return ok
            except self.playwright_error as exc:
                self.check_stop()
                if type(exc).__name__ == "TargetClosedError":
                    self.recovery_log("专用 Edge 页面或连接提前关闭；发生前最后操作=%s。将重新探测网络。",
                                      self._trace_last_action, level=logging.WARNING)
                else:
                    self.recovery_log("浏览器操作失败；异常=%s；发生前最后操作=%s。请确认 Edge 已安装且专用 Profile 未被占用。",
                                      type(exc).__name__, self._trace_last_action,
                                      level=logging.ERROR)
                return False
            finally:
                if context is not None:
                    self.recovery_log("开始关闭本程序的专用 Edge；保留 Profile 中已保存的密码和登录配置。")
                    try:
                        # 必须在创建 context 的同一线程关闭，不能由 Tk 线程调用。
                        context.close()
                        self.recovery_log("专用 Edge 已安全关闭。")
                    except self.playwright_error as exc:
                        self.recovery_log("关闭专用 Edge 时连接已中断；异常=%s。",
                                          type(exc).__name__, level=logging.WARNING)
                elif not setup:
                    self.recovery_log("专用 Edge 上下文未建立，无需执行浏览器关闭。",
                                      level=logging.WARNING)


class MonitorWorker(threading.Thread):
    def __init__(self, store: ConfigStore, events: queue.Queue, *, mode="monitor",
                 debug=False, force=False, profile=EDGE_PROFILE_DIR, engine_factory=RecoveryEngine,
                 clock=time.monotonic):
        super().__init__(name="CampusWatchdogWorker", daemon=False)
        self.store = store
        self.events = events
        self.mode = mode
        self.debug = debug
        self.force = force
        self.profile = profile
        self.engine_factory = engine_factory
        self.clock = clock
        self.consecutive_failures = 0
        self.finished_reason = ""
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.manual_check = threading.Event()

    def emit(self, kind, **payload):
        self.events.put((kind, payload))

    def request_stop(self):
        self.stop_event.set()
        self.wake_event.set()

    def request_check(self):
        self.manual_check.set()
        self.wake_event.set()

    def configuration_changed(self):
        # 只唤醒调度，重新计算截止时间；不绕过重试间隔，也不清零失败次数。
        self.wake_event.set()

    def _report_failures(self, settings):
        self.emit("failures", count=self.consecutive_failures, limit=settings.max_failures)

    def _limit_reached(self, settings):
        if self.consecutive_failures < settings.max_failures:
            return False
        self.finished_reason = "failure_limit"
        self._report_failures(settings)
        LOGGER.error("连续恢复失败 %s 次，已达到上限 %s 次。退出监控循环；请检查配置和网络后手动点击“启动监控”。",
                     self.consecutive_failures, settings.max_failures)
        self.emit("limit_reached", count=self.consecutive_failures, limit=settings.max_failures)
        return True

    def _waiting_state(self, online, last_failure, settings):
        if online:
            self.emit("state", text="运行中")
        elif not settings.username:
            self.emit("state", text="等待填写学号")
        else:
            self.emit("state", text="等待重试", retry_at=(
                last_failure + settings.retry_interval if last_failure is not None else None))

    def run(self):
        engine = None
        try:
            if self.stop_event.is_set():
                return
            engine = self.engine_factory(self.stop_event, self.emit, self.profile)
            if self.mode == "setup":
                self.emit("state", text="浏览器配置中")
                LOGGER.info("打开 exe 同目录的专用 Edge 配置：%s", self.profile.name)
                engine.browser_session(self.store.snapshot(), setup=True)
                return
            settings = self.store.snapshot()
            LOGGER.info("校园网看门狗已启动。检测间隔：%s 秒；失败重试：%s 秒；连续失败上限：%s 次（含首次恢复）。",
                        settings.check_interval, settings.retry_interval, settings.max_failures)
            LOGGER.info("断网保护：主站点失败后使用独立 HTTPS 备用探测；连续 %s 轮均失败才触发恢复，复核间隔 %s 秒。",
                        DISCONNECT_CONFIRMATIONS, DISCONNECT_CONFIRM_INTERVAL)
            last_probe = None
            last_failure = None
            previous_online = None
            while not self.stop_event.is_set():
                self.wake_event.clear()
                engine.check_stop()
                settings = self.store.snapshot()
                if self._limit_reached(settings):
                    break
                manual = self.manual_check.is_set()
                if manual:
                    self.manual_check.clear()
                now = self.clock()
                next_probe = now if last_probe is None else last_probe + settings.check_interval
                next_due = next_probe
                if last_failure is not None and settings.username:
                    next_due = min(next_due, last_failure + settings.retry_interval)
                if not manual and now < next_due:
                    self._report_failures(settings)
                    self._waiting_state(previous_online, last_failure, settings)
                    self.wake_event.wait(next_due - now)
                    continue
                self.emit("state", text="检测中")
                online = engine.confirmed_internet_ok()
                last_probe = self.clock()
                if online != previous_online or online:
                    LOGGER.log(logging.INFO if online else logging.WARNING,
                               "Internet 正常。" if online else "持续联网异常（探测站点经多轮复核仍未通过）。")
                previous_online = online
                if online:
                    if self.consecutive_failures:
                        LOGGER.info("Internet 已恢复，连续失败次数已清零。")
                    self.consecutive_failures = 0
                    last_failure = None
                settings = self.store.snapshot()
                if self._limit_reached(settings):
                    break
                retry_due = last_failure is None or self.clock() >= last_failure + settings.retry_interval
                if (not online or self.force) and (manual or retry_due):
                    if not settings.username:
                        self.emit("state", text="等待填写学号")
                        LOGGER.warning("学号为空，暂不执行认证；填写并保存后自动继续。")
                    else:
                        engine.check_stop()
                        self.emit("state", text="恢复中")
                        attempt_number = self.consecutive_failures + 1
                        trigger_reason = ("用户手动立即检测" if manual else
                                          "启动参数要求强制恢复" if self.force else
                                          "自动监控确认持续断网")
                        ok = engine.browser_session(
                            settings, debug=self.debug,
                            recovery_attempt=attempt_number,
                            failure_limit=settings.max_failures,
                            trigger_reason=trigger_reason)
                        self.force = False
                        engine.check_stop()
                        # 浏览器操作结果不等同于网络状态，再做真实探测。
                        online = engine.confirmed_internet_ok(
                            phase="浏览器关闭后的最终联网复核")
                        last_probe = self.clock()
                        previous_online = online
                        if online:
                            finish_trace = getattr(engine, "finish_recovery_trace", None)
                            if finish_trace is not None:
                                finish_trace(True, browser_ok=ok,
                                             detail="连续失败计数将清零")
                            self.consecutive_failures = 0
                            last_failure = None
                            LOGGER.info("自动恢复完成。" if ok else "当前联网探测已通过。")
                        else:
                            self.consecutive_failures += 1
                            last_failure = self.clock()
                            settings = self.store.snapshot()
                            finish_trace = getattr(engine, "finish_recovery_trace", None)
                            if finish_trace is not None:
                                finish_trace(False, browser_ok=ok,
                                             detail=(f"连续失败将记为 "
                                                     f"{self.consecutive_failures}/{settings.max_failures}"))
                            LOGGER.warning("本次恢复失败；连续失败 %s / %s 次。",
                                           self.consecutive_failures, settings.max_failures)
                            if self._limit_reached(settings):
                                break
                            LOGGER.warning("%s 秒后可重试；期间按检测间隔继续检查网络。", settings.retry_interval)
                settings = self.store.snapshot()
                self._report_failures(settings)
                self._waiting_state(online, last_failure, settings)
                if self.mode == "once":
                    break
        except StopRequested:
            LOGGER.info("已收到停止请求。")
        except Exception as exc:
            # 不转储页面 DOM、密码、浏览器底层调用参数。
            hint = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
            LOGGER.error("后台任务已停止：%s", hint)
            self.emit("error", text=hint)
        finally:
            if engine is not None:
                engine.close()
            LOGGER.info("后台任务已结束，监控已停止。")
            self.emit("state", text="已停止（失败上限）" if self.finished_reason == "failure_limit" else "已停止")


class CampusApp:
    BG = "#F3F6FA"
    INK = "#17263C"
    MUTED = "#62738B"
    BLUE = "#2267D8"
    GREEN = "#14835B"
    RED = "#C44444"
    AMBER = "#A56612"

    def __init__(self, root, store, events, log_messages, *, autostart=True,
                 startup_mode="monitor", debug=False, force=False,
                 worker_factory=MonitorWorker):
        self.root = root
        self.store = store
        self.events = events
        self.log_messages = log_messages
        self.worker_factory = worker_factory
        self.worker = None
        self.tray = None
        self.closing = False
        self.closed = False
        self.close_after = None
        self.stopping = False
        self.save_after = None
        self.poll_after = None
        self.start_after = None
        self.debug = debug
        self.checked_at = None
        self.retry_at = None
        self.failure_count = 0
        self.script_state = "已停止"
        settings = store.snapshot()
        self.username = tk.StringVar(root, settings.username)
        self.service = tk.StringVar(root, settings.service_name)
        self.check_interval_var = tk.StringVar(root, str(settings.check_interval))
        self.retry_interval_var = tk.StringVar(root, str(settings.retry_interval))
        self.max_failures_var = tk.StringVar(root, str(settings.max_failures))
        self.failure_note = tk.StringVar(root)
        self.footer_note = tk.StringVar(root)
        self.config_note = tk.StringVar(root, "配置已读取" if store.path.exists() and not store.warning else "填写学号后开始监控")
        self.script_note = tk.StringVar(root, "后台任务未启动")
        self.network_note = tk.StringVar(root, "启动监控后显示实际联网探测结果")
        self.auto_scroll = tk.BooleanVar(root, True)
        # 默认完整显示。历史只存在内存中，用于切换过滤时重绘当前 GUI；文件日志不受影响。
        self.hide_normal_logs = False
        self.log_history = []
        self._build_ui()
        self._update_policy_summary()
        if os.name == "nt":
            self.tray = WindowsTray(events)
            self.tray.start()
        self.username.trace_add("write", self._schedule_save)
        for variable in (self.check_interval_var, self.retry_interval_var, self.max_failures_var):
            variable.trace_add("write", self._schedule_save)
        root.protocol("WM_DELETE_WINDOW", self.hide_to_tray)
        root.bind("<Control-s>", lambda event: self.save_config(show_errors=True))
        self._refresh_controls()
        self.poll_after = root.after(100, self._poll)
        LOGGER.info("界面已就绪；密码仅由 Edge 管理，本程序不保存密码。")
        LOGGER.info("点击 X 会隐藏到托盘并继续监控；彻底退出请使用“关闭脚本”或托盘菜单“退出程序”。")
        LOGGER.info("请勿同时运行旧版 campus_net_watchdog.py 或占用同一个专用 Edge Profile。")
        if store.warning:
            self.config_note.set(store.warning)
            LOGGER.warning(store.warning)
        elif startup_mode == "setup":
            self.start_after = root.after(250, lambda: self.start("setup"))
        elif autostart and settings.username:
            self.start_after = root.after(250, lambda: self.start(startup_mode, force=force))

    def _build_ui(self):
        root = self.root
        root.title(APP_NAME)
        root.configure(bg=self.BG)
        root.geometry("920x760")
        root.minsize(860, 720)
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TButton", font=("Microsoft YaHei UI", 10), padding=(12, 8))
        style.configure("Primary.TButton", background=self.BLUE, foreground="white")
        style.map("Primary.TButton", background=[("active", "#1855B6"), ("disabled", "#D6DEEA")])
        style.configure("Danger.TButton", foreground=self.RED)
        style.configure("LogFilter.TButton", font=("Microsoft YaHei UI", 9), padding=(8, 3))
        style.configure("TEntry", padding=7, font=("Microsoft YaHei UI", 11))
        style.configure("TCheckbutton", background="white", font=("Microsoft YaHei UI", 9))
        body = tk.Frame(root, bg=self.BG, padx=24, pady=20)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(4, weight=1)
        header = tk.Frame(body, bg=self.BG)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 16))
        self._label(header, APP_NAME, 23, bold=True, bg=self.BG).pack(anchor="w")
        self._label(header, "燕山大学  /  自动重连与运行监控", 10, color=self.MUTED, bg=self.BG).pack(anchor="w", pady=(3, 0))

        cards = tk.Frame(body, bg=self.BG)
        cards.grid(row=1, column=0, sticky="ew", pady=(0, 14))
        cards.columnconfigure((0, 1), weight=1, uniform="status")
        for column, title in enumerate(("脚本状态", "网络状态")):
            card = tk.Frame(cards, bg="white", padx=18, pady=13)
            card.grid(row=0, column=column, sticky="nsew", padx=(0, 6) if column == 0 else (6, 0))
            self._label(card, title, 10, color=self.MUTED).pack(anchor="w")
            label = self._label(card, "已停止" if column == 0 else "未检测", 20, bold=True)
            label.pack(anchor="w", pady=(6, 6))
            note = self.script_note if column == 0 else self.network_note
            self._label(card, "", 9, color=self.MUTED, textvariable=note).pack(anchor="w")
            if column == 0:
                self.script_label = label
            else:
                self.network_label = label

        config = tk.Frame(body, bg="white", padx=18, pady=14)
        config.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        config.columnconfigure(1, weight=1)
        self._label(config, "连接配置", 12, bold=True).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))
        self._label(config, "学号", 10).grid(row=1, column=0, sticky="w", padx=(0, 14))
        self.username_entry = ttk.Entry(config, textvariable=self.username, font=("Microsoft YaHei UI", 11))
        self.username_entry.grid(row=1, column=1, sticky="ew", padx=(0, 10))
        self.save_button = ttk.Button(config, text="保存配置", command=lambda: self.save_config(show_errors=True))
        self.save_button.grid(row=1, column=2, sticky="e")
        self._label(config, "网络", 10).grid(row=2, column=0, sticky="w", padx=(0, 14), pady=(12, 0))
        service_bar = tk.Frame(config, bg="white")
        service_bar.grid(row=2, column=1, columnspan=2, sticky="ew", pady=(12, 0))
        self.service_buttons = []
        for index, service in enumerate(SERVICES):
            service_bar.columnconfigure(index, weight=1, uniform="service")
            button = tk.Radiobutton(
                service_bar, text=service, value=service, variable=self.service,
                indicatoron=False, command=self._select_service, cursor="hand2",
                font=("Microsoft YaHei UI", 10), padx=10, pady=9,
                relief="flat", bd=0, highlightthickness=1,
                highlightbackground="#DCE4EF", selectcolor=self.BLUE,
                activebackground="#E8F0FD", takefocus=True,
            )
            button.grid(row=0, column=index, sticky="ew", padx=(0, 7) if index < 3 else 0)
            self.service_buttons.append(button)
        policy_bar = tk.Frame(config, bg="white")
        policy_bar.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(14, 0))
        self.interval_inputs = []
        for index, (title, variable, minimum, maximum) in enumerate((
            ("检测间隔（秒）", self.check_interval_var, 5, 3600),
            ("失败重试间隔（秒）", self.retry_interval_var, 5, 3600),
            ("连续失败上限（次）", self.max_failures_var, 1, 20),
        )):
            policy_bar.columnconfigure(index, weight=1, uniform="policy")
            item = tk.Frame(policy_bar, bg="white")
            item.grid(row=0, column=index, sticky="ew", padx=(0, 10) if index < 2 else 0)
            self._label(item, title, 9).pack(side="left", padx=(0, 4))
            control = ttk.Spinbox(item, from_=minimum, to=maximum, increment=1,
                                  width=6, textvariable=variable, font=("Microsoft YaHei UI", 10))
            control.pack(side="left", fill="x", expand=True)
            self.interval_inputs.append(control)
        self._label(config, "", 9, color=self.AMBER, textvariable=self.failure_note).grid(
            row=4, column=0, columnspan=3, sticky="w", pady=(9, 0))
        self.config_status_label = self._label(config, "", 9, color=self.GREEN, textvariable=self.config_note)
        self.config_status_label.grid(row=5, column=0, columnspan=3, sticky="w", pady=(9, 5))
        self._label(config, "间隔保存后生效；学号与网络用于下一次恢复。更换学号后，请先配置该账号的已保存密码。",
                    9, color=self.MUTED, wraplength=770, justify="left").grid(row=6, column=0, columnspan=3, sticky="w")
        self._paint_services()

        actions = tk.Frame(body, bg=self.BG)
        actions.grid(row=3, column=0, sticky="ew", pady=(0, 12))
        self.start_button = ttk.Button(actions, text="启动监控", style="Primary.TButton", command=self.start)
        self.stop_button = ttk.Button(actions, text="停止监控", command=self.stop)
        self.check_button = ttk.Button(actions, text="立即检测", command=self.check_now)
        self.setup_button = ttk.Button(actions, text="配置已保存密码", command=lambda: self.start("setup"))
        for button in (self.start_button, self.stop_button, self.check_button, self.setup_button):
            button.pack(side="left", padx=(0, 7))
        self.close_button = ttk.Button(actions, text="关闭脚本", style="Danger.TButton", command=self.close)
        self.close_button.pack(side="right")

        log_panel = tk.Frame(body, bg="white", padx=14, pady=12)
        log_panel.grid(row=4, column=0, sticky="nsew")
        log_panel.rowconfigure(1, weight=1)
        log_panel.columnconfigure(0, weight=1)
        log_header = tk.Frame(log_panel, bg="white")
        log_header.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        self._label(log_header, "运行日志", 11, bold=True).pack(side="left")
        self._label(log_header, "  同步写入 campus_watchdog.log", 9, color=self.MUTED).pack(side="left")
        ttk.Checkbutton(log_header, text="自动滚动", variable=self.auto_scroll).pack(side="right")
        self.log_filter_button = ttk.Button(log_header, text="隐藏正常检测", style="LogFilter.TButton",
                                            command=self.toggle_log_filter)
        self.log_filter_button.pack(side="right", padx=(0, 8))
        self.log_text = ScrolledText(log_panel, height=8, wrap="word", state="disabled",
                                     font=("Microsoft YaHei UI", 9), bg="#F8FAFD",
                                     fg="#33445C", relief="flat", padx=10, pady=8)
        self.log_text.grid(row=1, column=0, sticky="nsew")
        self.log_text.tag_configure("WARNING", foreground=self.AMBER)
        self.log_text.tag_configure("ERROR", foreground=self.RED)
        self._label(body, "", 9, color=self.MUTED, bg=self.BG, textvariable=self.footer_note).grid(
            row=5, column=0, sticky="w", pady=(10, 0))
        # Tk 字体随 Windows 缩放变化，固定窗口高度会挤掉日志区域。
        # 按实际控件尺寸决定初始窗口，并保留至少数行日志的空间。
        root.update_idletasks()
        available_width = max(640, root.winfo_screenwidth() - 80)
        available_height = max(600, root.winfo_screenheight() - 100)
        width = min(max(920, root.winfo_reqwidth() + 24), available_width)
        height = min(max(760, root.winfo_reqheight()), available_height)
        root.geometry(f"{width}x{height}")
        root.minsize(width, min(height, max(720, root.winfo_reqheight() - 60)))

    def _label(self, parent, text, size=10, *, color=None, bg="white", bold=False, **kwargs):
        return tk.Label(parent, text=text, bg=bg, fg=color or self.INK, anchor="w",
                        font=("Microsoft YaHei UI", size, "bold" if bold else "normal"), **kwargs)

    def _paint_services(self):
        for button in self.service_buttons:
            selected = button.cget("value") == self.service.get()
            button.configure(bg=self.BLUE if selected else "#F6F8FC",
                             fg="white" if selected else self.INK,
                             activeforeground="white" if selected else self.INK,
                             activebackground=self.BLUE if selected else "#E8F0FD")

    @staticmethod
    def _routine_normal_log(level, message):
        """只过滤明确的周期性成功心跳；恢复成功等业务事件必须保留。"""
        if level != "INFO":
            return False
        body = message.rsplit(" | ", 1)[-1].strip()
        body = body.rstrip("。.!！").strip()
        return body in {
            "Internet 正常",
            "连接正常",
            "网络连接正常",
            "Internet 当前正常，无需执行恢复",
            "Internet 正常，无需执行恢复",
        }

    def _visible_log_lines(self, lines):
        if not self.hide_normal_logs:
            return lines
        return [item for item in lines if not self._routine_normal_log(*item)]

    def _append_log_lines(self, lines):
        visible = self._visible_log_lines(lines)
        if not visible:
            return
        self.log_text.configure(state="normal")
        for level, message in visible:
            self.log_text.insert("end", message + "\n", level)
        line_count = int(self.log_text.index("end-1c").split(".")[0])
        if line_count > 3000:
            self.log_text.delete("1.0", f"{line_count - 2500}.0")
        self.log_text.configure(state="disabled")
        if self.auto_scroll.get():
            self.log_text.see("end")

    def _drain_log_queue(self, limit=150):
        lines = []
        for _ in range(limit):
            try:
                lines.append(self.log_messages.get_nowait())
            except queue.Empty:
                break
        if lines:
            self.log_history.extend(lines)
            if len(self.log_history) > 3000:
                del self.log_history[:-3000]
        return lines

    def _render_log_history(self):
        position = self.log_text.yview()[0]
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        for level, message in self._visible_log_lines(self.log_history):
            self.log_text.insert("end", message + "\n", level)
        self.log_text.configure(state="disabled")
        if self.auto_scroll.get():
            self.log_text.see("end")
        else:
            self.log_text.yview_moveto(position)

    def toggle_log_filter(self):
        if self.closing:
            return
        # 先接收尚在队列中的日志，确保切换后当前历史完整且不会丢行。
        self._drain_log_queue(limit=10000)
        self.hide_normal_logs = not self.hide_normal_logs
        self.log_filter_button.configure(text="显示全部日志" if self.hide_normal_logs else "隐藏正常检测")
        self._render_log_history()
        LOGGER.info("GUI 日志已切换为%s；campus_watchdog.log 继续记录全部内容。",
                    "仅显示关键事件" if self.hide_normal_logs else "显示全部")

    def _schedule_save(self, *_):
        if self.closing:
            return
        if self.save_after is not None:
            self.root.after_cancel(self.save_after)
        self.config_note.set("正在编辑，稍后自动保存…")
        self.config_status_label.configure(fg=self.MUTED)
        self.save_after = self.root.after(800, self.save_config)

    def _select_service(self):
        self._paint_services()
        self.save_config(show_errors=True)

    def _update_policy_summary(self):
        settings = self.store.snapshot()
        self.failure_note.set(f"连续恢复失败：{self.failure_count} / {settings.max_failures} 次（含首次）；达到上限后停止监控，需手动启动。")
        self.footer_note.set(f"检测 {settings.check_interval} 秒 · 失败重试 {settings.retry_interval} 秒 · 上限 {settings.max_failures} 次 · 断网需 3 轮确认 · X 隐藏到托盘")

    def save_config(self, show_errors=False):
        if self.save_after is not None:
            self.root.after_cancel(self.save_after)
            self.save_after = None
        try:
            settings = Settings.from_dict({"username": self.username.get(), "service_name": self.service.get(),
                                           "check_interval": self.check_interval_var.get(),
                                           "retry_interval": self.retry_interval_var.get(),
                                           "max_failures": self.max_failures_var.get()})
            previous = self.store.snapshot()
            if settings != previous or not self.store.path.exists() or self.store.warning or self.store.needs_upgrade:
                self.store.save(settings)
                self.store.warning = ""
                LOGGER.info("配置已保存：%s；检测 %s 秒、失败重试 %s 秒、连续失败上限 %s 次。",
                            settings.service_name, settings.check_interval, settings.retry_interval, settings.max_failures)
                if self.worker is not None:
                    self.worker.configuration_changed()
                if previous.username and previous.username != settings.username:
                    LOGGER.warning("学号已更改。有效的旧 SSO 不会自动退出；请停止监控，打开密码配置，退出旧账号并登录新账号。")
            self.config_note.set("已保存  ·  " + time.strftime("%H:%M:%S") + "  ·  " + settings.service_name)
            self.config_status_label.configure(fg=self.GREEN)
            self._update_policy_summary()
            if self.script_state == "运行中":
                self.script_note.set(f"后台监控 · 每 {settings.check_interval} 秒检测一次")
            return True
        except (OSError, ValueError) as exc:
            self.config_note.set("未保存：" + str(exc))
            self.config_status_label.configure(fg=self.RED)
            if show_errors:
                messagebox.showerror("配置未保存", str(exc), parent=self.root)
            return False

    def _cancel_pending_start(self):
        if self.start_after is not None:
            self.root.after_cancel(self.start_after)
            self.start_after = None

    def start(self, mode="monitor", force=False):
        self._cancel_pending_start()
        if self.closing or self.worker is not None:
            return
        if not self.save_config(show_errors=True):
            return
        if mode != "setup" and not self.store.snapshot().username:
            self.config_note.set("请先填写学号，再启动监控。")
            self.username_entry.focus_set()
            return
        self.stopping = False
        self.retry_at = None
        self.failure_count = 0
        self._update_policy_summary()
        self.checked_at = None
        self.network_label.configure(text="未检测", fg=self.MUTED)
        self.network_note.set("等待本次监控的联网探测" if mode != "setup" else "配置模式下不执行网络监控")
        self.worker = self.worker_factory(self.store, self.events, mode=mode, debug=self.debug, force=force)
        self._set_state("启动中")
        self.worker.start()
        self._refresh_controls()

    def stop(self):
        self._cancel_pending_start()
        if self.worker is not None and not self.stopping:
            self.stopping = True
            self.worker.request_stop()
            self._set_state("正在停止")
            LOGGER.info("已请求停止；等待当前操作返回并关闭专用 Edge，请稍候。")
            self._refresh_controls()

    def check_now(self):
        if self.worker is not None and not self.stopping:
            self.worker.request_check()
            LOGGER.info("已请求立即检测；当前恢复若尚未结束，将先完成该轮操作。")

    def hide_to_tray(self):
        if self.closing:
            return
        self.save_config(show_errors=False)
        if self.tray is not None and self.tray.available.is_set():
            self.root.withdraw()
            LOGGER.info("窗口已隐藏到托盘，后台任务保持运行。单击托盘图标可恢复窗口。")
        else:
            self.root.iconify()
            LOGGER.warning("托盘暂不可用，窗口已最小化到任务栏，后台任务保持运行。")

    def show_window(self):
        if self.closed:
            return
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        LOGGER.info("窗口已从托盘恢复。")

    def close(self):
        if self.closing:
            return
        self._cancel_pending_start()
        if not self.save_config(show_errors=False):
            LOGGER.warning("本次编辑未保存，将保留上一次有效配置。")
        self.closing = True
        self.root.deiconify()
        self.stop()
        self._set_state("正在退出")
        self._refresh_controls()
        if self.worker is None:
            self._finish_close()
        else:
            self._set_state("正在退出")

    def _finish_close(self):
        self.close_after = None
        if self.closed:
            return
        if self.poll_after is not None:
            self.root.after_cancel(self.poll_after)
            self.poll_after = None
        if self.tray is not None:
            self.tray.stop()
            if self.tray.is_alive():
                self.close_after = self.root.after(100, self._finish_close)
                return
            self.tray.join(timeout=0)
        LOGGER.info("程序已安全退出。")
        self.closed = True
        self.root.destroy()

    def _set_state(self, text):
        self.script_state = text
        color = self.GREEN if text == "运行中" else self.AMBER if text in (
            "恢复中", "确认网络中", "等待重试", "正在停止", "正在退出") else self.INK
        if text == "已停止（失败上限）":
            color = self.RED
        self.script_label.configure(text=text, fg=color)
        if text in ("正在停止", "正在退出"):
            self.script_note.set("等待当前操作结束并保存浏览器配置…")
        elif text == "已停止":
            self.script_note.set("可重新启动；网络状态为最后一次检测结果")
        elif text == "已停止（失败上限）":
            self.script_note.set("已退出循环，请检查网络或配置后手动启动")
        elif text == "浏览器配置中":
            self.script_note.set("请在专用 Edge 中完成登录和保存密码")
        elif text == "恢复中":
            self.script_note.set("正在处理校园网认证，请勿操作专用 Edge")
        elif text == "确认网络中":
            self.script_note.set("正在复核联网状态，暂不启动认证")
        elif text == "等待填写学号":
            self.script_note.set("学号为空，自动认证暂不执行")
        else:
            self.script_note.set(f"后台监控 · 每 {self.store.snapshot().check_interval} 秒检测一次")

    def _refresh_controls(self):
        active = self.worker is not None
        disabled = self.closing or self.stopping
        self.start_button.configure(state="disabled" if active or disabled else "normal")
        self.setup_button.configure(state="disabled" if active or disabled else "normal")
        self.stop_button.configure(state="normal" if active and not disabled else "disabled",
                                   text="结束配置" if active and self.worker.mode == "setup" else "停止监控")
        self.check_button.configure(state="normal" if active and not disabled and self.worker.mode != "setup" else "disabled")
        self.close_button.configure(state="disabled" if self.closing else "normal")
        self.log_filter_button.configure(state="disabled" if self.closing else "normal")
        self.save_button.configure(state="disabled" if self.closing else "normal")
        self.username_entry.configure(state="disabled" if self.closing else "normal")
        for button in self.service_buttons:
            button.configure(state="disabled" if self.closing else "normal")
        for control in self.interval_inputs:
            control.configure(state="disabled" if self.closing else "normal")

    def _poll(self):
        self.poll_after = None
        for _ in range(100):
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "network":
                self.checked_at = payload["checked_at"]
                suspect = not payload["online"] and not payload.get("confirmed", True)
                self.network_label.configure(
                    text="网络待确认" if suspect else "Internet 正常" if payload["online"] else "Internet 断开",
                    fg=self.AMBER if suspect else self.GREEN if payload["online"] else self.RED)
            elif kind == "state" and not (self.stopping or self.closing):
                self.retry_at = payload.get("retry_at")
                self._set_state(payload["text"])
                if payload["text"] == "确认网络中":
                    self.script_note.set(f"已失败 {payload['attempt']}/{payload['total']} 轮 · {payload['interval']} 秒后复核")
            elif kind == "error":
                self.config_note.set("后台任务异常，请查看日志。")
            elif kind == "failures":
                self.failure_count = payload["count"]
                self._update_policy_summary()
            elif kind == "limit_reached":
                self.failure_count = payload["count"]
                self._update_policy_summary()
                self.config_note.set("已达连续失败上限，自动监控停止。请检查后手动点击“启动监控”。")
                self.config_status_label.configure(fg=self.RED)
            elif kind == "tray_action":
                if payload["action"] == "show":
                    self.show_window()
                elif payload["action"] == "exit":
                    self.root.after_idle(self.close)
            elif kind == "tray_unavailable" and not self.closing:
                if self.root.state() == "withdrawn":
                    self.show_window()
        lines = self._drain_log_queue()
        self._append_log_lines(lines)
        if self.worker is not None and not self.worker.is_alive():
            self.worker.join(timeout=0)
            finished_reason = self.worker.finished_reason
            self.failure_count = self.worker.consecutive_failures
            self.worker = None
            self.stopping = False
            if self.closing:
                self._finish_close()
                return
            self.retry_at = None
            self._set_state("已停止（失败上限）" if finished_reason == "failure_limit" else "已停止")
            self._update_policy_summary()
            self._refresh_controls()
        if self.checked_at:
            checked = time.strftime("%H:%M:%S", time.localtime(self.checked_at))
            age = max(0, int(time.time() - self.checked_at))
            suffix = " · 已停止更新" if self.worker is None else f" · {age} 秒前"
            self.network_note.set("最近检测 " + checked + suffix)
        if self.script_state == "等待重试" and self.retry_at:
            remaining = max(0, int(self.retry_at - time.monotonic()) + 1)
            self.script_note.set(f"约 {remaining} 秒后重试 · 期间继续检测网络")
        self.poll_after = self.root.after(100, self._poll)


def run_self_test() -> int:
    """可选打包自检；只用临时 Profile 和本地页面，不访问校园网或已保存密码。"""
    report = {"passed": False, "base_dir": str(BASE_DIR),
              "app_name": APP_NAME,
              "frozen": bool(getattr(sys, "frozen", False)),
              "check_interval": CHECK_INTERVAL, "retry_interval": RETRY_INTERVAL,
              "max_failures": MAX_FAILURES, "checks": []}
    report["disconnect_confirmations"] = DISCONNECT_CONFIRMATIONS
    report["disconnect_confirm_interval"] = DISCONNECT_CONFIRM_INTERVAL
    report["probe_count"] = len(CONNECTIVITY_PROBES)
    root = None
    engine = None
    tray = None
    try:
        root = tk.Tk()
        root.withdraw()
        root.update()
        report["checks"].append("tkinter")
        assert CampusApp._routine_normal_log("INFO", "Internet 正常。")
        assert not CampusApp._routine_normal_log("INFO", "Internet 已恢复。")
        report["checks"].append("gui_log_filter")
        if os.name == "nt":
            tray = WindowsTray(queue.Queue())
            tray.start()
            if not tray.ready.wait(10) or not tray.available.is_set():
                raise RuntimeError("系统托盘初始化失败。")
            tray.stop()
            tray.join(10)
            if tray.is_alive():
                raise RuntimeError("系统托盘未能退出。")
            report["checks"].append("windows_tray")
        with tempfile.TemporaryDirectory(prefix="campus-self-test-") as folder:
            temporary = Path(folder)
            store = ConfigStore(temporary / "config.json")
            store.save(Settings("000000", "校园网", 45, 15, 2))
            assert ConfigStore(store.path).snapshot() == Settings("000000", "校园网", 45, 15, 2)
            report["checks"].append("config_roundtrip")
            engine = RecoveryEngine(threading.Event(), lambda *args, **kwargs: None,
                                    temporary / "test_profile")
            report["checks"].append("requests_and_playwright_imports")
            trace_messages = queue.Queue()
            trace_handler = GUILogHandler(trace_messages)
            trace_handler.setFormatter(logging.Formatter("%(message)s"))
            previous_log_level = LOGGER.level
            LOGGER.setLevel(logging.INFO)
            LOGGER.addHandler(trace_handler)
            try:
                secret = "000000"
                engine.last_probe_detail = "Microsoft HTTP：模拟失败"
                trace_id = engine.begin_recovery_trace(
                    Settings(secret, "校园网"), 1, 2, "离线自检")
                page_stub = type("PageStub", (), {
                    "url": "https://auth1.ysu.edu.cn/portal/entry;ticket=PRIVATE?username=" + secret
                })()
                engine.recovery_log("页面=%s。", engine.safe_page_address(page_stub))
                engine.finish_recovery_trace(True, browser_ok=True, detail="离线自检完成")
                trace_text = "\n".join(item[1] for item in list(trace_messages.queue))
                assert f"[恢复 {trace_id} | 步骤 01 |" in trace_text
                assert "结束本次追踪：恢复成功" in trace_text
                assert secret not in trace_text and "PRIVATE" not in trace_text
                report["checks"].append("recovery_trace_logging")
            finally:
                LOGGER.removeHandler(trace_handler)
                trace_handler.close()
                LOGGER.setLevel(previous_log_level)
            import ssl
            ssl.create_default_context(cafile=engine.requests.certs.where())
            report["checks"].append("https_certificate_bundle")
            with engine.sync_playwright() as playwright:
                context = playwright.chromium.launch_persistent_context(
                    str(engine.profile), channel="msedge", headless=True, timeout=20000,
                    args=["--host-resolver-rules=MAP * 127.0.0.1, EXCLUDE localhost",
                          "--no-proxy-server"],
                )
                try:
                    context.route("**/*", lambda route: route.abort())
                    page = context.pages[0] if context.pages else context.new_page()
                    page.set_content("<button onclick=\"this.textContent='OK'\">test</button>")
                    page.get_by_role("button", name="test").click()
                    assert page.get_by_role("button", name="OK").count() == 1
                    report["checks"].append("edge_driver_and_page_interaction")
                finally:
                    context.close()
            report["checks"].append("edge_cleanup")
        report["passed"] = True
    except Exception as exc:
        report["error"] = type(exc).__name__ + ": " + str(exc)
    finally:
        if tray is not None:
            tray.stop()
            tray.join(10)
        if engine is not None:
            engine.close()
        if root is not None:
            root.destroy()
    (BASE_DIR / "campus_self_test.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["passed"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--setup", action="store_true", help="打开 GUI 和专用 Edge，手工配置保存密码")
    parser.add_argument("--no-autostart", action="store_true", help="只打开界面，不自动启动监控")
    parser.add_argument("--once", action="store_true", help="在 GUI 中检查/恢复一次，完成后保留窗口")
    parser.add_argument("--force", action="store_true", help="在 GUI 中强制打开 Portal 一次；不保证触发登录")
    parser.add_argument("--debug", action="store_true", help="恢复失败后保留 Edge 120 秒，可取消")
    parser.add_argument("--self-test", action="store_true", help="使用临时 Profile 离线检查运行依赖，写入 campus_self_test.json 后退出")
    args = parser.parse_args()
    if args.self_test:
        raise SystemExit(run_self_test())
    if os.name == "nt":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
    root = tk.Tk()
    root.withdraw()
    instance = None
    try:
        instance = SingleInstance(EDGE_PROFILE_DIR)
        events = queue.Queue()
        log_messages = queue.Queue(maxsize=2000)
        setup_logging(log_messages)
        store = ConfigStore()
        mode = "setup" if args.setup else "once" if args.once or args.force else "monitor"
        app = CampusApp(root, store, events, log_messages,
                        autostart=not args.no_autostart, startup_mode=mode,
                        debug=args.debug, force=args.force)
        root.deiconify()
        try:
            root.mainloop()
        except KeyboardInterrupt:
            app.close()
            if not app.closed:
                root.mainloop()
    except (OSError, RuntimeError) as exc:
        messagebox.showerror(APP_NAME + "无法启动", str(exc), parent=root)
        root.destroy()
    finally:
        if instance is not None:
            instance.close()
        for handler in LOGGER.handlers[:]:
            handler.close()
            LOGGER.removeHandler(handler)


if __name__ == "__main__":
    main()
