# SnapNap
# Copyright (c) 2026 Harin Mani Karri
# Licensed under the SnapNap Personal Use License (SPUL-1.0)
# See LICENSE file in the repository for full license text.

import os, sys, time, ctypes, ctypes.wintypes, logging, logging.handlers, threading, subprocess, psutil, platform, win32gui, win32process, win32api, win32con, pystray
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from PIL import Image


# Logs setup

_APPDATA = os.environ.get("APPDATA", os.path.expanduser("~"))
_LOG_DIR = os.path.join(_APPDATA, APP_NAME := "SnapNap")
os.makedirs(_LOG_DIR, exist_ok=True)
LOG_PATH = os.path.join(_LOG_DIR, "suspend_manager.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(message)s",
    handlers=[logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=20*1024*1024, backupCount=5, encoding="utf-8")],
)
logger = logging.getLogger("SnapNap")


# Hotkey

HOTKEY_ID  = 1
MOD_ALT    = 0x0001
VK_J       = 0x4A


# Protected processes(let's not mess with OS lol)

PROTECTED_PROCESSES = {
    "explorer.exe", "dwm.exe", "csrss.exe", "winlogon.exe",
    "services.exe", "lsass.exe", "smss.exe", "svchost.exe",
    "wininit.exe", "system", "idle",
}
PROTECTED_PID_CEIL = 1000

DESKTOP_CLASSES = {"Shell_TrayWnd", "Progman", "WorkerW", "Shell_SecondaryTrayWnd"}


# NT/kernel32 function pointers

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


# Single instance guard

_ERROR_ALREADY_EXISTS = 183

def ensure_single_instance():
    mutex = _k32.CreateMutexW(None, False, f"Global\\{APP_NAME}_Mutex")
    err   = ctypes.GetLastError()
    if err == _ERROR_ALREADY_EXISTS:
        logger.info("Another instance is already running. Exiting.")
        sys.exit(0)
    if not mutex:
        logger.error(
            "CreateMutexW returned NULL (error %d) — could not create single-instance guard. "
            "Continuing without mutex protection.", err,
        )
    else:
        logger.info("Single-instance mutex acquired (error code at creation: %d).", err)
    return mutex


# Admin/UAC setup

def is_admin() -> bool:
    try:
        result = bool(ctypes.windll.shell32.IsUserAnAdmin())
        logger.info("Admin check: %s.", "elevated" if result else "not elevated")
        return result
    except Exception as e:
        logger.warning("Admin check raised an exception: %s", e)
        return False

def elevate_self():
    logger.info("Requesting UAC elevation via ShellExecuteW.")
    if getattr(sys, "frozen", False):
        exe    = sys.executable
        params = ""
    else:
        exe    = sys.executable
        params = f'"{os.path.abspath(__file__)}"'
    ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 0)
    sys.exit(0)


# Task Scheduler setup

def register_task_scheduler():
    if getattr(sys, "frozen", False):
        exe_path = sys.executable
        cmd = f'"{exe_path}"'
    else:
        exe_path = sys.executable
        script   = os.path.abspath(__file__)
        cmd = f'"{exe_path}" "{script}"'

    logger.info("Registering Task Scheduler entry → %s", cmd)
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
            logger.info("Task Scheduler entry created/updated successfully.")

            ps_result = subprocess.run([
                "powershell",
                "-WindowStyle", "Hidden",
                "-Command",
                f"$t = Get-ScheduledTask -TaskName '{APP_NAME}'; "
                "$t.Settings.DisallowStartIfOnBatteries=$false; "
                "$t.Settings.StopIfGoingOnBatteries=$false; "
                "Set-ScheduledTask $t"
            ], capture_output=True, timeout=10,
               creationflags=subprocess.CREATE_NO_WINDOW)

            if ps_result.returncode == 0:
                logger.info("Task Scheduler battery-run settings patched.")
            else:
                logger.warning("Battery-run patch failed (non-fatal): %s", ps_result.stderr.strip())
        else:
            logger.warning("schtasks /Create failed (rc=%d): %s", result.returncode, result.stderr.strip())
    except subprocess.TimeoutExpired:
        logger.warning("schtasks timed out — skipping Task Scheduler registration.")
    except Exception as e:
        logger.warning("Could not register Task Scheduler entry: %s", e)


# Low-level suspend/resume functions via NT APIs

def _nt_suspend(pid: int) -> bool:
    handle = OpenProcess(PROCESS_SUSPEND_RESUME, False, pid)
    if not handle:
        logger.warning("OpenProcess failed for PID %d (error %d).", pid, ctypes.GetLastError())
        return False
    try:
        status = NtSuspendProcess(handle)
        if status != 0:
            logger.warning("NtSuspendProcess failed for PID %d (NTSTATUS 0x%08X).", pid, status & 0xFFFFFFFF)
            return False
        logger.debug("NtSuspendProcess succeeded for PID %d.", pid)
        return True
    finally:
        CloseHandle(handle)


def _nt_resume(pid: int) -> bool:
    handle = OpenProcess(PROCESS_SUSPEND_RESUME, False, pid)
    if not handle:
        logger.warning("OpenProcess failed for PID %d (error %d).", pid, ctypes.GetLastError())
        return False
    try:
        status = NtResumeProcess(handle)
        if status != 0:
            logger.warning("NtResumeProcess failed for PID %d (NTSTATUS 0x%08X).", pid, status & 0xFFFFFFFF)
            return False
        logger.debug("NtResumeProcess succeeded for PID %d.", pid)
        return True
    finally:
        CloseHandle(handle)


# RAM trimming helper

PROCESS_SET_QUOTA           = 0x0100
PROCESS_QUERY_INFORMATION   = 0x0400

def trim_process_memory(pid: int):
    try:
        rss_before = psutil.Process(pid).memory_info().rss
    except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
        logger.warning("trim_process_memory: cannot read RSS for PID %d: %s", pid, e)
        return (0, 0)

    if rss_before < 50 * 1024 * 1024:
        logger.info("trim_process_memory: PID %d RSS is %.1f MB (< 50 MB) — skipping trim.", pid, rss_before / (1024 * 1024))
        return (0, 0)

    handle = OpenProcess(PROCESS_SET_QUOTA | PROCESS_QUERY_INFORMATION, False, pid)
    if not handle:
        logger.warning("trim_process_memory: OpenProcess failed for PID %d (error %d).", pid, ctypes.GetLastError())
        return (0, 0)
    try:
        ret = ctypes.windll.psapi.EmptyWorkingSet(handle)
        if ret == 0:
            logger.warning("trim_process_memory: EmptyWorkingSet failed for PID %d (error %d).", pid, ctypes.GetLastError())

        ret2 = ctypes.windll.kernel32.SetProcessWorkingSetSize(
            handle,
            ctypes.c_size_t(-1),
            ctypes.c_size_t(-1),
        )
        if ret2 == 0:
            logger.warning("trim_process_memory: SetProcessWorkingSetSize failed for PID %d (error %d).", pid, ctypes.GetLastError())

        try:
            rss_after = psutil.Process(pid).memory_info().rss
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            rss_after = rss_before

        return (rss_before, rss_after)
    except Exception as e:
        logger.warning("trim_process_memory: unexpected error for PID %d: %s", pid, e)
        return (0, 0)
    finally:
        CloseHandle(handle)

# Data model

@dataclass
class AppSession:
    hwnd: int
    pid: int
    # Store (pid, name) tuples
    suspended_pids: List[Tuple[int, str]] = field(default_factory=list)


# Window detection

class WindowDetector:

    @staticmethod
    def get_foreground_window() -> Optional[int]:
        hwnd = win32gui.GetForegroundWindow()

        if not hwnd:
            logger.debug("GetForegroundWindow returned NULL.")
            return None

        if not win32gui.IsWindowVisible(hwnd):
            logger.debug("HWND 0x%X is not visible — skipping.", hwnd)
            return None


# Reject desktop/taskbar

        try:
            cls_name = win32gui.GetClassName(hwnd)
            if cls_name in DESKTOP_CLASSES:
                logger.info("HWND 0x%X belongs to desktop/taskbar class '%s' — skipping.", hwnd, cls_name)
                return None
        except Exception as e:
            logger.warning("GetClassName failed for HWND 0x%X: %s", hwnd, e)
            return None


# Reject our own process

        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if pid == os.getpid():
                logger.info("HWND 0x%X belongs to SnapNap itself — skipping.", hwnd)
                return None
        except Exception as e:
            logger.warning("GetWindowThreadProcessId failed for HWND 0x%X: %s", hwnd, e)
            return None

        try:
            title = win32gui.GetWindowText(hwnd)
        except Exception:
            title = "<unknown>"

        logger.info("Foreground window detected: HWND=0x%X, PID=%d, title='%s'.", hwnd, pid, title)
        return hwnd


# Process safety and tree operations

class ProcessController:

    @staticmethod
    def is_safe(pid: int, name: str) -> bool:
        if pid < PROTECTED_PID_CEIL or pid == os.getpid():
            return False
        if name.lower() in PROTECTED_PROCESSES:
            return False
        return True


# Returns (pid, name) tuples: root first, children after (BFS order)
# Only includes processes that are safe to suspend

    @staticmethod
    def get_process_tree(root_pid: int) -> List[Tuple[int, str]]:
        logger.info("Building process tree for root PID %d.", root_pid)
        _t0 = time.perf_counter()
        try:
            root      = psutil.Process(root_pid)
            root_name = root.name()
        except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
            logger.warning("Cannot access root PID %d: %s", root_pid, e)
            return []

        if not ProcessController.is_safe(root_pid, root_name):
            logger.warning("Root process '%s' (PID %d) is protected — aborting tree walk.", root_name, root_pid)
            return []

        tree: List[Tuple[int, str]] = [(root_pid, root_name)]

        try:
            children = root.children(recursive=True)
        except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
            logger.warning("Could not enumerate children of PID %d: %s", root_pid, e)
            children = []

        skipped = 0
        for child in children:
            try:
                cname = child.name()
                logger.debug("Examining child: '%s' (PID %d).", cname, child.pid)
                if ProcessController.is_safe(child.pid, cname):
                    tree.append((child.pid, cname))
                else:
                    logger.warning("Skipping protected child: '%s' (PID %d).", cname, child.pid)
                    skipped += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                logger.debug("Child PID %d vanished or denied during tree walk — skipping.", child.pid)
                skipped += 1
                continue

        elapsed = time.perf_counter() - _t0
        logger.info(
            "Process tree built in %.3f s: %d process(es) queued, %d skipped. Tree: %s",
            elapsed, len(tree), skipped,
            [(name, pid) for pid, name in tree],
        )
        return tree


# Suspend deepest children first, root last

    @staticmethod
    def suspend_tree(tree: List[Tuple[int, str]]) -> List[Tuple[int, str]]:
        logger.info("Suspending %d process(es) (children-first order).", len(tree))
        _t0 = time.perf_counter()
        actually_suspended: List[Tuple[int, str]] = []
        failed: List[Tuple[int, str]] = []
        total_freed = 0
        for pid, name in reversed(tree):
            if _nt_suspend(pid):
                logger.info("  ✓ Suspended '%s' (PID %d).", name, pid)
                actually_suspended.append((pid, name))
                before, after = trim_process_memory(pid)
                if before > 0:
                    freed = before - after
                    total_freed += freed
                    logger.info("  RAM trim '%s' (PID %d): %.1f MB → %.1f MB (freed %.1f MB).",
                                name, pid, before / (1024 * 1024), after / (1024 * 1024), freed / (1024 * 1024))
            else:
                logger.warning("  ✗ FINAL FAILURE — could not suspend '%s' (PID %d). It will keep running.", name, pid)
                failed.append((pid, name))
        elapsed = time.perf_counter() - _t0
        logger.info(
            "Suspend pass complete in %.3f s: %d/%d succeeded, %d failed.%s",
            elapsed, len(actually_suspended), len(tree), len(failed),
            f" Still-running: {[(n, p) for p, n in failed]}" if failed else "",
        )
        logger.info("Total RAM trimmed across session: %.1f MB freed.", total_freed / (1024 * 1024))
        return actually_suspended


# Resume root first, children last (reverse of suspend order)

    @staticmethod
    def resume_tree(suspended_pids: List[Tuple[int, str]]):
        logger.info("Resuming %d process(es) (root-first order).", len(suspended_pids))
        _t0 = time.perf_counter()
        success = 0
        failed: List[Tuple[int, str]] = []
        for pid, name in reversed(suspended_pids):
            if _nt_resume(pid):
                logger.info("  ✓ Resumed '%s' (PID %d).", name, pid)
                success += 1
            else:
                logger.warning("  ✗ FINAL FAILURE — could not resume '%s' (PID %d). Process may remain frozen.", name, pid)
                failed.append((pid, name))
        elapsed = time.perf_counter() - _t0
        logger.info(
            "Resume pass complete in %.3f s: %d/%d succeeded, %d failed.%s",
            elapsed, success, len(suspended_pids), len(failed),
            f" Still-frozen: {[(n, p) for p, n in failed]}" if failed else "",
        )


# Session Manager(idle to paused and vice versa state machine)

class SessionManager:

    STATE_IDLE   = "IDLE"
    STATE_PAUSED = "PAUSED"

    def __init__(self):
        self.state   = self.STATE_IDLE
        self.session: Optional[AppSession] = None
        self._lock   = threading.Lock()
        self.tray_icon: Optional[pystray.Icon] = None
        logger.info("SessionManager initialised (state: %s).", self.STATE_IDLE)


# Tell pystray to re-evaluate menu item enabled/checked states

    def _notify_tray(self):
        try:
            if self.tray_icon is not None:
                self.tray_icon.update_menu()
        except Exception:
            pass


# Pause function

    def pause(self):
        with self._lock:
            if self.state != self.STATE_IDLE:
                logger.info("Pause requested but state is '%s' — ignoring.", self.state)
                return

            _op_start = time.perf_counter()
            logger.info("Pause initiated — probing foreground window.")


# Poll briefly for a valid window

            hwnd = None
            for attempt in range(20):
                logger.debug("Window probe attempt %d/20.", attempt + 1)
                hwnd = WindowDetector.get_foreground_window()
                if hwnd:
                    logger.info("Foreground window found on attempt %d/20.", attempt + 1)
                    break
                time.sleep(0.05)

            if not hwnd:
                logger.info("No suspendable foreground window found after 20 attempts — aborting pause.")
                return

            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            try:
                proc_name = psutil.Process(pid).name()
            except Exception:
                proc_name = "Unknown"

            if not ProcessController.is_safe(pid, proc_name):
                logger.warning("Safety block: '%s' (PID %d) is a protected process.", proc_name, pid)
                return

            logger.info("Target confirmed: '%s' (PID %d, HWND 0x%X).", proc_name, pid, hwnd)


# Force the game out of fullscreen before suspending
# SW_FORCEMINIMIZE crosses thread boundaries and works on exclusive DX windows

            logger.info("Force-minimising window (SW_FORCEMINIMIZE) and waiting for IsIconic confirmation.")
            time.sleep(0.01)
            _user32 = ctypes.windll.user32
            _user32.ShowWindow(hwnd, 11)  # SW_FORCEMINIMIZE


# Steal foreground focus away so exclusive fullscreen games release the display
# GetShellWindow() (taskbar/Explorer) is a more reliable steal target than
# raw desktop on DX11/DX12 titles — always present, always in user32

            _shell = _user32.GetShellWindow()
            if _shell:
                _user32.SetForegroundWindow(_shell)
                logger.info("Foreground stolen to shell window (HWND=0x%X).", _shell)
            else:
                logger.warning("GetShellWindow returned NULL — skipping foreground steal.")


# Step-1: wait for OS to mark the window as minimized (up to 3 s)

            _minimised = False
            for _tick in range(60):
                if _user32.IsIconic(hwnd):
                    _minimised = True
                    logger.info("IsIconic confirmed after %d ms.", (_tick + 1) * 50)
                    break
                time.sleep(0.050)

            if not _minimised:
                logger.warning(
                    "Window HWND=0x%X did not minimise within 3 s — "
                    "game may still hold the display. Proceeding anyway.", hwnd,
                )
            else:


# Step-2: wait for foreground to leave the game window
# IsIconic only means the OS window is minimized
# GetForegroundWindow() != hwnd means DXGI has actually release
# the exclusive swap chain — the compositor has taken back the display

                _fg_released = False
                for _tick in range(60):  # up to 3 more seconds
                    if _user32.GetForegroundWindow() != hwnd:
                        _fg_released = True
                        logger.info("Foreground released from game after %d ms — DXGI swap chain free.", (_tick + 1) * 50)
                        break
                    time.sleep(0.050)

                if not _fg_released:
                    logger.warning(
                        "Game HWND=0x%X still holds foreground after 3 s — "
                        "DXGI may not have released. Black screen risk remains.", hwnd,
                    )


# Step-3: settling sleep so the GPU framebuffer fully drains

                time.sleep(1.000)

            tree = ProcessController.get_process_tree(pid)
            if not tree:
                logger.warning("Empty process tree — aborting pause.")
                return

            logger.info(
                "STATE SNAPSHOT (pre-suspend): state=%s, target='%s' PID=%d, HWND=0x%X, tree_size=%d.",
                self.state, proc_name, pid, hwnd, len(tree),
            )
            actually_suspended = ProcessController.suspend_tree(tree)
            if not actually_suspended:
                logger.warning("No processes were successfully suspended — session not created.")
                return

            self.session = AppSession(hwnd=hwnd, pid=pid,
                                      suspended_pids=actually_suspended)
            self.state = self.STATE_PAUSED
            logger.info(
                "State → PAUSED. Session: root='%s' PID=%d, total suspended=%d. "
                "Total pause op took %.3f s.",
                proc_name, pid, len(actually_suspended),
                time.perf_counter() - _op_start,
            )
            self._notify_tray()


# Resume function

    def resume(self):
        with self._lock:
            if self.state != self.STATE_PAUSED or not self.session:
                logger.warning(
                    "Resume called but state is '%s' (session=%s) — resetting to IDLE.",
                    self.state, "present" if self.session else "absent",
                )
                self.state   = self.STATE_IDLE
                self.session = None
                return

            _op_start = time.perf_counter()
            logger.info(
                "STATE SNAPSHOT (pre-resume): state=%s, root PID=%d, HWND=0x%X, suspended_count=%d, pids=%s.",
                self.state, self.session.pid, self.session.hwnd,
                len(self.session.suspended_pids),
                [(n, p) for p, n in self.session.suspended_pids],
            )
            logger.info(
                "Resume initiated for session: root PID=%d, %d process(es) to resume.",
                self.session.pid, len(self.session.suspended_pids),
            )


            avail_ram = psutil.virtual_memory().available
            avail_gb = avail_ram / (1024 ** 3)
            logger.info("Available RAM before resume: %.1f GB.", avail_gb)
            if avail_gb < 2.0:
                logger.warning("Low system RAM detected (%.1f GB free) — resume may cause slowdown.", avail_gb)
                threading.Thread(
                    target=lambda: ctypes.windll.user32.MessageBoxW(
                        None,
                        f"Low system RAM available ({avail_gb:.1f} GB free).\n"
                        "Resuming may cause system slowdown.\n"
                        "The app will still resume.",
                        "SnapNap",
                        0x00000000 | 0x00000030,  # MB_OK | MB_ICONWARNING
                    ),
                    daemon=True,
                    name="LowRAMWarning",
                ).start()

            ProcessController.resume_tree(self.session.suspended_pids)
            _root_pid = self.session.pid

            _t_wake = time.perf_counter()
            while (time.perf_counter() - _t_wake) < 0.500:
                try:
                    if psutil.Process(_root_pid).status() != psutil.STATUS_STOPPED:
                        break
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    break
                time.sleep(0.005)
            time.sleep(0.010)


# Restore the window and return focus

            try:
                hwnd = self.session.hwnd
                if win32gui.IsWindow(hwnd):
                    logger.info("Restoring window HWND=0x%X.", hwnd)

                    win32gui.PostMessage(hwnd, win32con.WM_SYSCOMMAND,
                                        win32con.SC_RESTORE, 0)
                    _t_restore = time.perf_counter()
                    while (time.perf_counter() - _t_restore) < 0.300:
                        try:
                            if not ctypes.windll.user32.IsIconic(hwnd):
                                break
                        except Exception:
                            break
                        time.sleep(0.005)
                    time.sleep(0.010)

# Alt key nudge(helps Windows reassign foreground rights)

                    user32  = ctypes.windll.user32
                    VK_MENU = 0x12
                    KEYEVENTF_KEYUP = 0x0002
                    user32.keybd_event(VK_MENU, 0, 0, 0)
                    user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)

                    cur = win32api.GetCurrentThreadId()
                    fg, _ = win32process.GetWindowThreadProcessId(hwnd)
                    if cur != fg:
                        attached = ctypes.windll.user32.AttachThreadInput(cur, fg, True)
                        if not attached:
                            logger.warning(
                                "AttachThreadInput(attach) failed for thread pair (%d, %d) — "
                                "SetForegroundWindow may not work.", cur, fg,
                            )
                        win32gui.SetForegroundWindow(hwnd)
                        if attached:
                            ctypes.windll.user32.AttachThreadInput(cur, fg, False)
                    else:
                        win32gui.SetForegroundWindow(hwnd)

                    logger.info("Focus returned to HWND=0x%X.", hwnd)
                else:
                    logger.warning("HWND=0x%X is no longer a valid window — skipping restore.", hwnd)
            except Exception as e:
                logger.warning("Could not restore window focus: %s", e)

            self.session = None
            self.state   = self.STATE_IDLE
            logger.info("State → IDLE. Total resume op took %.3f s.", time.perf_counter() - _op_start)
            self._notify_tray()


# Toggle setup

    def toggle(self):
        logger.info("[Hotkey] Toggle pressed — current state: %s.", self.state)
        try:
            if self.state == self.STATE_IDLE:
                self.pause()
            elif self.state == self.STATE_PAUSED:
                self.resume()
        except Exception as e:
            logger.error("Unhandled exception in toggle: %s", e, exc_info=True)
            self.session = None
            self.state   = self.STATE_IDLE
            logger.info("State force-reset → IDLE after error.")


# Hotkey listener loop

def hotkey_loop(manager: SessionManager, stop_event: threading.Event):
    user32 = ctypes.windll.user32

    logger.info("Hotkey thread started.")

    if not user32.RegisterHotKey(None, HOTKEY_ID, MOD_ALT, VK_J):
        logger.error(
            "Failed to register hotkey Alt+J (error %d). "
            "Another application may be using the same combination.",
            ctypes.GetLastError(),
        )
        return

    logger.info("Hotkey Alt+J registered successfully (ID=%d).", HOTKEY_ID)

    msg = ctypes.wintypes.MSG()
    while not stop_event.is_set():
        ret = user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1)
        if ret:
            if msg.message == win32con.WM_HOTKEY and msg.wParam == HOTKEY_ID:
                logger.info("[Hotkey] WM_HOTKEY received.")
                manager.toggle()
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        else:
            time.sleep(0.02)

    user32.UnregisterHotKey(None, HOTKEY_ID)
    logger.info("Hotkey Alt+J unregistered.")

    if manager.state == SessionManager.STATE_PAUSED:
        logger.info("Shutdown detected with active session — auto-resuming before exit.")
        manager.resume()

    logger.info("Hotkey thread exiting.")


# System tray icon

def _get_icon_path() -> str | None:
    if getattr(sys, "frozen", False):
        base = sys._MEIPASS
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(base, "assets", "SnapNap2.ico")
    found = os.path.isfile(path)
    logger.info("Icon asset %s: %s", "found" if found else "not found", path)
    return path if found else None

def _make_icon_image() -> Image.Image:
    ico_path = _get_icon_path()
    if not ico_path:
        raise FileNotFoundError(
            "SnapNap2.ico not found in assets/ — cannot start without icon."
        )
    img = Image.open(ico_path)
    logger.info("Loaded tray icon from: %s", ico_path)
    return img


def build_tray(manager: SessionManager, stop_event: threading.Event) -> pystray.Icon:
    logger.info("Building tray icon.")

    def on_exit(icon, item):
        logger.info("[Tray] Exit clicked.")
        stop_event.set()
        icon.stop()

    def on_resume(icon, item):
        if manager.state == SessionManager.STATE_PAUSED:
            logger.info("[Tray] Resume clicked.")
            threading.Thread(target=manager.resume, daemon=True,
                             name="TrayResume").start()
        else:
            logger.info("[Tray] Resume clicked but state is '%s' — ignoring.", manager.state)

    def resume_enabled(item):
        return manager.state == SessionManager.STATE_PAUSED

    def exit_enabled(item):
        return manager.state != SessionManager.STATE_PAUSED

    menu = pystray.Menu(
        pystray.MenuItem("SnapNap", None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Resume", on_resume, enabled=resume_enabled),
        pystray.MenuItem("Exit",   on_exit,   enabled=exit_enabled),
    )

    icon = pystray.Icon(
        APP_NAME,
        _make_icon_image(),
        "SnapNap — Alt+J",
        menu,
    )

# Patch: treat left-click as right-click
    from pystray._util import win32 as _pw32
    _WM_LBUTTONUP = 0x0202
    _WM_RBUTTONUP = 0x0205
    _original_handler = icon._message_handlers[_pw32.WM_NOTIFY]

    def _patched_notify(wparam, lparam):
        if lparam == _WM_LBUTTONUP:
            lparam = _WM_RBUTTONUP
        return _original_handler(wparam, lparam)

    icon._message_handlers[_pw32.WM_NOTIFY] = _patched_notify
    logger.info("Tray icon built. Left-click → right-click patch applied.")

    return icon


def main():
    logger.info("=" * 60)
    logger.info("SnapNap starting up (PID %d).", os.getpid())
    logger.info("Log file: %s", LOG_PATH)
    logger.info("Python : %s | Frozen: %s", sys.version.split()[0], getattr(sys, "frozen", False))
    logger.info("OS     : %s", platform.platform())
    logger.info("CPU    : %d logical core(s)", os.cpu_count() or 0)
    logger.info("User   : %s", os.environ.get("USERNAME", "<unknown>"))
    logger.info("=" * 60)

    if not is_admin():
        elevate_self()

    _mutex = ensure_single_instance()

    register_task_scheduler()

    logger.info("Running update check.")
    from updater import run_update_check
    run_update_check()
    logger.info("Update check returned.")

    manager    = SessionManager()
    stop_event = threading.Event()

    logger.info("Starting hotkey listener thread.")
    ht = threading.Thread(target=hotkey_loop, args=(manager, stop_event),
                          daemon=True, name="HotkeyThread")
    ht.start()

    tray = build_tray(manager, stop_event)
    manager.tray_icon = tray

    _FIRST_RUN_MARKER = os.path.join(_LOG_DIR, ".launched")
    is_first_run      = not os.path.exists(_FIRST_RUN_MARKER)
    logger.info("First-run marker %s.", "absent — showing welcome dialog" if is_first_run else "present — skipping welcome dialog")

    def _on_tray_ready(icon):
        icon.visible = True
        logger.info("Tray icon is now visible.")
        if is_first_run:
            try:
                open(_FIRST_RUN_MARKER, "w").close()
                logger.info("First-run marker created at %s.", _FIRST_RUN_MARKER)
            except OSError as e:
                logger.warning("Could not create first-run marker: %s", e)
            MB_OK             = 0x00000000
            MB_ICONINFORMATION = 0x00000040
            MB_SYSTEMMODAL    = 0x00001000
            ctypes.windll.user32.MessageBoxW(
                None,
                "Successfully launched.\nPress Alt+J to pause/resume.",
                "SnapNap",
                MB_OK | MB_ICONINFORMATION | MB_SYSTEMMODAL,
            )
            logger.info("Welcome dialog dismissed.")

    logger.info("Handing control to pystray run loop.")
    tray.run(setup=_on_tray_ready)

    logger.info("Tray run loop exited — signalling hotkey thread to stop.")
    stop_event.set()
    ht.join(timeout=3)

    if ht.is_alive():
        logger.warning("Hotkey thread did not exit within 3 s — continuing anyway.")
    else:
        logger.info("Hotkey thread joined cleanly.")

    logger.info("SnapNap exited cleanly.")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
