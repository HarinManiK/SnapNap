# SnapNap
# Copyright (c) 2026 Harin Mani Karri
# Licensed under the SnapNap Personal Use License (SPUL-1.0)
# See LICENSE file in the repository for full license text.

import os, sys, time, ctypes, ctypes.wintypes, logging, threading, subprocess, psutil, win32gui, win32process, win32api, win32con, pystray
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from PIL import Image, ImageDraw

# Setting up the logs folder.
_APPDATA = os.environ.get("APPDATA", os.path.expanduser("~"))
_LOG_DIR = os.path.join(_APPDATA, APP_NAME := "SnapNap")
os.makedirs(_LOG_DIR, exist_ok=True)
LOG_PATH = os.path.join(_LOG_DIR, "suspend_manager.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(message)s",
    handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8")],
)
logger = logging.getLogger("SnapNap")



# Hotkey setup
HOTKEY_ID  = 1
MOD_ALT    = 0x0001
MOD_SHIFT  = 0x0004
VK_A       = 0x41



# Protected processes(lets not mess with OS lol)
PROTECTED_PROCESSES = {
    "explorer.exe", "dwm.exe", "csrss.exe", "winlogon.exe",
    "services.exe", "lsass.exe", "smss.exe", "svchost.exe",
    "wininit.exe", "system", "idle",
}
PROTECTED_PID_CEIL = 1000

DESKTOP_CLASSES = {"Shell_TrayWnd", "Progman", "WorkerW", "Shell_SecondaryTrayWnd"}



# ntdll function pointers
_ntdll  = ctypes.WinDLL("ntdll", use_last_error=True)
_k32    = ctypes.windll.kernel32

PROCESS_SUSPEND_RESUME = 0x0800

NtSuspendProcess = _ntdll.NtSuspendProcess
NtSuspendProcess.restype  = ctypes.c_long
NtSuspendProcess.argtypes = [ctypes.wintypes.HANDLE]

NtResumeProcess = _ntdll.NtResumeProcess
NtResumeProcess.restype  = ctypes.c_long
NtResumeProcess.argtypes = [ctypes.wintypes.HANDLE]

OpenProcess  = _k32.OpenProcess
CloseHandle  = _k32.CloseHandle



# Prevent multiple instances using a global mutex
_ERROR_ALREADY_EXISTS = 183

def ensure_single_instance():
    mutex = _k32.CreateMutexW(None, False, f"Global\\{APP_NAME}_Mutex")
    if ctypes.GetLastError() == _ERROR_ALREADY_EXISTS:
        logger.info("Another instance is already running. Exiting.")
        sys.exit(0)
    return mutex



# Privilege check and UAC elevation
def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def elevate_self():
    if getattr(sys, "frozen", False):
        exe    = sys.executable
        params = ""
    else:
        exe    = sys.executable
        params = f'"{os.path.abspath(__file__)}"'
    ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 0)
    sys.exit(0)



# Setting up the task scheduler entry that runs at startup with admin rights.
def register_task_scheduler():
    if getattr(sys, "frozen", False):
        exe_path = sys.executable
        cmd = f'"{exe_path}"'
    else:
        exe_path = sys.executable
        script   = os.path.abspath(__file__)
        cmd = f'"{exe_path}" "{script}"'

    try:
        result = subprocess.run(
            [
                "schtasks.exe", "/Create",
                "/TN", APP_NAME,
                "/TR", cmd,
                "/SC", "ONLOGON",
                "/RL", "HIGHEST",
                "/IT",
                "/F",
            ],
            capture_output=True, text=True, timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if result.returncode == 0:
            logger.info("Task Scheduler entry created/updated: %s", cmd)

            subprocess.run([
                "powershell",
                "-WindowStyle", "Hidden",
                "-Command",
                f"$t = Get-ScheduledTask -TaskName '{APP_NAME}'; "
                "$t.Settings.DisallowStartIfOnBatteries=$false; "
                "$t.Settings.StopIfGoingOnBatteries=$false; "
                "Set-ScheduledTask $t"
            ], capture_output=True, timeout=10,
               creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            logger.warning("schtasks stderr: %s", result.stderr.strip())
    except Exception as e:
        logger.warning("Could not register Task Scheduler entry: %s", e)



# Suspend function
def _nt_suspend(pid: int) -> bool:
    handle = OpenProcess(PROCESS_SUSPEND_RESUME, False, pid)
    if not handle:
        logger.warning("OpenProcess failed for PID %d (error %d)", pid, ctypes.GetLastError())
        return False
    try:
        status = NtSuspendProcess(handle)
        if status != 0:
            logger.warning("NtSuspendProcess failed for PID %d (NTSTATUS 0x%08X)", pid, status & 0xFFFFFFFF)
            return False
        return True
    finally:
        CloseHandle(handle)



# Resume function
def _nt_resume(pid: int) -> bool:
    handle = OpenProcess(PROCESS_SUSPEND_RESUME, False, pid)
    if not handle:
        logger.warning("OpenProcess failed for PID %d (error %d)", pid, ctypes.GetLastError())
        return False
    try:
        status = NtResumeProcess(handle)
        if status != 0:
            logger.warning("NtResumeProcess failed for PID %d (NTSTATUS 0x%08X)", pid, status & 0xFFFFFFFF)
            return False
        return True
    finally:
        CloseHandle(handle)



@dataclass
class AppSession:
    hwnd: int
    pid: int
    # Store (pid, name) tuples — not live Process objects (which go stale)
    suspended_pids: List[Tuple[int, str]] = field(default_factory=list)



class WindowDetector:
    @staticmethod
    def get_foreground_window() -> Optional[int]:
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd or not win32gui.IsWindowVisible(hwnd):
            return None

        # Reject desktop / taskbar
        try:
            cls_name = win32gui.GetClassName(hwnd)
            if cls_name in DESKTOP_CLASSES:
                return None
        except Exception:
            return None

        # Reject our own process
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if pid == os.getpid():
                return None
        except Exception:
            return None

        return hwnd



class ProcessController:

    @staticmethod
    def is_safe(pid: int, name: str) -> bool:
        if pid < PROTECTED_PID_CEIL or pid == os.getpid():
            return False
        if name.lower() in PROTECTED_PROCESSES:
            return False
        return True


# Return list of (pid, name) tuples in root first, children after (BFS order) and include only safe to suspend entries.
    @staticmethod
    def get_process_tree(root_pid: int) -> List[Tuple[int, str]]:
        try:
            root = psutil.Process(root_pid)
            root_name = root.name()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return []

        if not ProcessController.is_safe(root_pid, root_name):
            logger.warning("Root %s (PID %d) is protected.", root_name, root_pid)
            return []

        tree: List[Tuple[int, str]] = [(root_pid, root_name)]

        try:
            children = root.children(recursive=True)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            children = []

        for child in children:
            try:
                cname = child.name()
                if ProcessController.is_safe(child.pid, cname):
                    tree.append((child.pid, cname))
                else:
                    logger.warning("Skipping protected child: %s (PID %d)", cname, child.pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        return tree


# Suspend deepest children first, root last
    @staticmethod
    def suspend_tree(tree: List[Tuple[int, str]]) -> List[Tuple[int, str]]:
        actually_suspended: List[Tuple[int, str]] = []
        for pid, name in reversed(tree):
            if _nt_suspend(pid):
                logger.info("Suspended: %s (PID %d)", name, pid)
                actually_suspended.append((pid, name))
            else:
                logger.warning("Failed to suspend: %s (PID %d)", name, pid)
        return actually_suspended


# Resume root first, children last(reverse of suspend order)
    @staticmethod
    def resume_tree(suspended_pids: List[Tuple[int, str]]):
        for pid, name in reversed(suspended_pids):
            if _nt_resume(pid):
                logger.info("Resumed: %s (PID %d)", name, pid)
            else:
                logger.warning("Failed to resume: %s (PID %d)", name, pid)



# Session Manager: idle to paused and vice versa. Single session only
class SessionManager:

    STATE_IDLE   = "IDLE"
    STATE_PAUSED = "PAUSED"

    def __init__(self):
        self.state   = self.STATE_IDLE
        self.session: Optional[AppSession] = None
        self._lock   = threading.Lock()
        self.tray_icon: Optional[pystray.Icon] = None


# Tell pystray to re-evaluate menu item enabled/checked states
    def _notify_tray(self):
        try:
            if self.tray_icon is not None:
                self.tray_icon.update_menu()
        except Exception:
            pass


# Pause session
    def pause(self):
        ...
