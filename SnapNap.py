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
    ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
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
        )
        if result.returncode == 0:
            logger.info("Task Scheduler entry created/updated: %s", cmd)

            subprocess.run([
                "powershell",
                "-Command",
                f"$t = Get-ScheduledTask -TaskName '{APP_NAME}'; "
                "$t.Settings.DisallowStartIfOnBatteries=$false; "
                "$t.Settings.StopIfGoingOnBatteries=$false; "
                "Set-ScheduledTask $t"
            ], capture_output=True, timeout=10)
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



# Session Manager: idle to paused and vice verse. Single session only
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
        with self._lock:
            if self.state != self.STATE_IDLE:
                logger.info("Already in state %s — ignoring pause request.", self.state)
                return

            hwnd = None
            for _ in range(20):
                hwnd = WindowDetector.get_foreground_window()
                if hwnd:
                    break
                time.sleep(0.05)

            if not hwnd:
                logger.info("No suspendable foreground window detected.")
                return

            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            try:
                proc_name = psutil.Process(pid).name()
            except Exception:
                proc_name = "Unknown"

            if not ProcessController.is_safe(pid, proc_name):
                logger.warning("Safety block: cannot suspend %s (PID %d)", proc_name, pid)
                return

            logger.info("Suspending: %s (PID %d)", proc_name, pid)

# Post minimize via the window's own message queue (thread-safe)
            win32gui.PostMessage(hwnd, win32con.WM_SYSCOMMAND, win32con.SC_MINIMIZE, 0)
            time.sleep(0.200)

            tree = ProcessController.get_process_tree(pid)
            if not tree:
                logger.warning("Empty process tree — aborting.")
                return

            actually_suspended = ProcessController.suspend_tree(tree)
            if not actually_suspended:
                logger.warning("No processes were suspended.")
                return

            self.session = AppSession(hwnd=hwnd, pid=pid,
                                      suspended_pids=actually_suspended)
            self.state = self.STATE_PAUSED
            logger.info("State → PAUSED  (%d processes)", len(actually_suspended))
            self._notify_tray()


# Resume session
    def resume(self):
        with self._lock:
            if self.state != self.STATE_PAUSED or not self.session:
                self.state = self.STATE_IDLE
                self.session = None
                return

            logger.info("Resuming session (root PID %d)…", self.session.pid)

            ProcessController.resume_tree(self.session.suspended_pids)

            time.sleep(0.200)

# Restore and focus
            try:
                hwnd = self.session.hwnd
                if win32gui.IsWindow(hwnd):
                    win32gui.PostMessage(hwnd, win32con.WM_SYSCOMMAND,
                                         win32con.SC_RESTORE, 0)
                    time.sleep(0.100)

# Bypass SetForegroundWindow restriction by simulating Alt key
                    user32 = ctypes.windll.user32
                    VK_MENU = 0x12
                    KEYEVENTF_KEYUP = 0x0002
                    user32.keybd_event(VK_MENU, 0, 0, 0) #Alt down
                    user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0) #Alt up

# Now AttachThreadInput + SetForegroundWindow
                    cur = win32api.GetCurrentThreadId()
                    fg, _ = win32process.GetWindowThreadProcessId(hwnd)
                    if cur != fg:
                        win32api.AttachThreadInput(cur, fg, True)
                        win32gui.SetForegroundWindow(hwnd)
                        win32api.AttachThreadInput(cur, fg, False)
                    else:
                        win32gui.SetForegroundWindow(hwnd)
                else:
                    logger.warning("Window handle no longer valid.")
            except Exception as e:
                logger.debug("Could not restore window focus: %s", e)

            self.session = None
            self.state   = self.STATE_IDLE
            logger.info("State → IDLE")
            self._notify_tray()


# Toggle between pause and resume
    def toggle(self):
        logger.info("[Hotkey] toggle — current state: %s", self.state)
        try:
            if self.state == self.STATE_IDLE:
                self.pause()
            elif self.state == self.STATE_PAUSED:
                self.resume()
        except Exception as e:
            logger.error("Critical error in toggle: %s", e)
            self.session = None
            self.state   = self.STATE_IDLE



# Hotkey message loop (background thread)
def hotkey_loop(manager: SessionManager, stop_event: threading.Event):
    user32 = ctypes.windll.user32

    if not user32.RegisterHotKey(None, HOTKEY_ID, MOD_ALT | MOD_SHIFT, VK_A):
        logger.error("Failed to register hotkey Alt+Shift+A.")
        return

    logger.info("Hotkey Alt+Shift+A registered.")
    msg = ctypes.wintypes.MSG()

    while not stop_event.is_set():
        ret = user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1)
        if ret:
            if msg.message == win32con.WM_HOTKEY and msg.wParam == HOTKEY_ID:
                manager.toggle()
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        else:
            time.sleep(0.02)

    user32.UnregisterHotKey(None, HOTKEY_ID)
    logger.info("Hotkey unregistered.")

# Safety: resume if we exit while paused
    if manager.state == SessionManager.STATE_PAUSED:
        logger.info("Auto-resuming before shutdown…")
        manager.resume()



# Tray icon(2×2 blue-black checkerboard)
def _make_icon_image(size: int = 64) -> Image.Image:
    img  = Image.new("RGB", (size, size))
    draw = ImageDraw.Draw(img)
    half = size // 2
    blue  = "#3B3BF5"
    black = "#000000"
    draw.rectangle([0,    0,    half, half], fill=black)
    draw.rectangle([half, 0,    size, half], fill=blue)
    draw.rectangle([0,    half, half, size], fill=blue)
    draw.rectangle([half, half, size, size], fill=black)
    return img


# Build system tray interface
def build_tray(manager: SessionManager, stop_event: threading.Event) -> pystray.Icon:
    def on_exit(icon, item):
        logger.info("Exit requested from tray.")
        stop_event.set()
        icon.stop()

    def on_resume(icon, item):
        if manager.state == SessionManager.STATE_PAUSED:
            threading.Thread(target=manager.resume, daemon=True,
                             name="TrayResume").start()

    def resume_enabled(item):
        return manager.state == SessionManager.STATE_PAUSED

    def exit_enabled(item):
        return manager.state != SessionManager.STATE_PAUSED

    menu = pystray.Menu(
        pystray.MenuItem("SnapNap", None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Resume", on_resume, enabled=resume_enabled),
        pystray.MenuItem("Exit", on_exit, enabled=exit_enabled),
    )

    icon = pystray.Icon(
        APP_NAME,
        _make_icon_image(64),
        "SnapNap — Alt+Shift+A",
        menu,
    )

# Make left-click also show the context menu.
    from pystray._util import win32 as _pw32
    _WM_LBUTTONUP = 0x0202
    _WM_RBUTTONUP = 0x0205
    _original_handler = icon._message_handlers[_pw32.WM_NOTIFY]

    def _patched_notify(wparam, lparam):
        if lparam == _WM_LBUTTONUP:
            lparam = _WM_RBUTTONUP
        return _original_handler(wparam, lparam)

    icon._message_handlers[_pw32.WM_NOTIFY] = _patched_notify

    return icon



# Entry point
def main():
#Elevate if not admin
    if not is_admin():
        logger.info("Requesting UAC elevation.")
        elevate_self()

# Single-instance guard (after elevation so the elevated copy is the one that holds it)
    _mutex = ensure_single_instance()

# Register Task Scheduler auto-start
    register_task_scheduler()

# Core objects
    manager    = SessionManager()
    stop_event = threading.Event()

# Hotkey thread
    ht = threading.Thread(target=hotkey_loop, args=(manager, stop_event),
                          daemon=True, name="HotkeyThread")
    ht.start()

# Tray icon (main thread — required by pystray on Windows)
    tray = build_tray(manager, stop_event)
    manager.tray_icon = tray
    logger.info("Tray icon starting.")

# First-launch welcome prompt (shown once, after tray is visible)
    _FIRST_RUN_MARKER = os.path.join(_LOG_DIR, ".launched")
    is_first_run = not os.path.exists(_FIRST_RUN_MARKER)

    def _on_tray_ready(icon):
        icon.visible = True
        if is_first_run:
            try:
                open(_FIRST_RUN_MARKER, "w").close()
            except OSError:
                pass
            MB_OK = 0x00000000
            MB_ICONINFORMATION = 0x00000040
            MB_SYSTEMMODAL = 0x00001000
            ctypes.windll.user32.MessageBoxW(
                None,
                "Successfully launched.\nPress Alt+Shift+A to pause/resume.",
                "SnapNap",
                MB_OK | MB_ICONINFORMATION | MB_SYSTEMMODAL,
            )

    tray.run(setup=_on_tray_ready)

# Cleanup
    stop_event.set()
    ht.join(timeout=3)
    logger.info("Application exited.")


if __name__ == "__main__":
    main()