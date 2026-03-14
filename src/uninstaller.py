import os
import sys
import ctypes
import shutil
import subprocess
import string
import threading
import time
import tkinter as tk
from tkinter import ttk

import psutil

APP_NAME = "SnapNap"
EXE_NAME = "SnapNap.exe"
APPDATA_FOLDER = os.path.join(
    os.environ.get("APPDATA", os.path.expanduser("~")), APP_NAME
)

BG_DARK      = "#1a1a2e"
BG_CARD      = "#16213e"
ACCENT_BLUE  = "#3B3BF5"
ACCENT_LIGHT = "#5c5cff"
TEXT_WHITE    = "#e8e8e8"
TEXT_DIM      = "#8891a4"
TEXT_WARN     = "#ff6b6b"
TEXT_SUCCESS  = "#51cf66"
PROGRESS_BG  = "#2a2a4a"



def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def elevate_self():
    if getattr(sys, "frozen", False):
        exe, params = sys.executable, ""
    else:
        exe = sys.executable
        params = f'"{os.path.abspath(__file__)}"'
    ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
    sys.exit(0)



def _own_exe_path() -> str:
    if getattr(sys, "frozen", False):
        return os.path.abspath(sys.executable)
    return os.path.abspath(__file__)


def is_snapnap_running() -> bool:
    my_pid = os.getpid()
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            if (proc.info["name"] and
                    proc.info["name"].lower() == EXE_NAME.lower() and
                    proc.info["pid"] != my_pid):
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False



def step_delete_appdata() -> bool:
    if os.path.isdir(APPDATA_FOLDER):
        try:
            shutil.rmtree(APPDATA_FOLDER, ignore_errors=True)
        except Exception:
            pass
    return not os.path.isdir(APPDATA_FOLDER)


def step_remove_task() -> bool:
    try:
        result = subprocess.run(
            ["schtasks.exe", "/Delete", "/TN", APP_NAME, "/F"],
            capture_output=True, text=True, timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if result.returncode == 0:
            return True
        stderr = result.stderr.lower()
        if "does not exist" in stderr or "cannot find" in stderr:
            return True
        return False
    except Exception:
        return False


def step_find_and_delete_exe() -> bool:
    own = os.path.normcase(os.path.normpath(_own_exe_path()))
    deleted_all = True

    search_roots: list[str] = []

    for env in ("USERPROFILE", "LOCALAPPDATA", "PROGRAMFILES",
                "PROGRAMFILES(X86)", "APPDATA"):
        p = os.environ.get(env)
        if p and os.path.isdir(p):
            search_roots.append(p)

    user_profile = os.environ.get("USERPROFILE", "")
    for sub in ("Desktop", "Downloads", "Documents"):
        p = os.path.join(user_profile, sub)
        if os.path.isdir(p):
            search_roots.append(p)

    for letter in string.ascii_uppercase:
        drive = f"{letter}:\\"
        try:
            if os.path.isdir(drive):
                dtype = ctypes.windll.kernel32.GetDriveTypeW(drive)
                if dtype == 3:  # DRIVE_FIXED
                    search_roots.append(drive)
        except Exception:
            continue

    seen: set[str] = set()
    unique: list[str] = []
    for r in search_roots:
        norm = os.path.normcase(os.path.normpath(r))
        if norm not in seen:
            seen.add(norm)
            unique.append(r)

    for root in unique:
        for dirpath, _, filenames in os.walk(root, topdown=True):
            for fname in filenames:
                if fname.lower() == EXE_NAME.lower():
                    full = os.path.join(dirpath, fname)
                    norm = os.path.normcase(os.path.normpath(full))
                    if norm == own:
                        continue
                    try:
                        os.remove(full)
                    except Exception:
                        deleted_all = False
    return deleted_all



class UninstallerApp:

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("SnapNap Uninstaller")
        self.root.geometry("480x300")
        self.root.resizable(False, False)
        self.root.configure(bg=BG_DARK)
        self.root.overrideredirect(False)

        self.root.update_idletasks()
        w, h = 480, 300
        x = (self.root.winfo_screenwidth() - w) // 2
        y = (self.root.winfo_screenheight() - h) // 2
        self.root.geometry(f"{w}x{h}+{x}+{y}")

        self.root.attributes("-topmost", True)
        self.root.after(200, lambda: self.root.attributes("-topmost", False))
        self.root.lift()
        self.root.focus_force()

        style = ttk.Style()
        style.theme_use("clam")
        style.configure(
            "Custom.Horizontal.TProgressbar",
            troughcolor=PROGRESS_BG,
            background=ACCENT_BLUE,
            darkcolor=ACCENT_BLUE,
            lightcolor=ACCENT_LIGHT,
            bordercolor=BG_CARD,
            thickness=22,
        )

        self._build_ui()

    def _build_ui(self):
        header = tk.Frame(self.root, bg=BG_DARK, height=70)
        header.pack(fill="x", padx=30, pady=(25, 0))
        header.pack_propagate(False)

        tk.Label(
            header, text="SnapNap", font=("Segoe UI", 22, "bold"),
            fg=ACCENT_BLUE, bg=BG_DARK
        ).pack(anchor="w")
        tk.Label(
            header, text="Uninstaller", font=("Segoe UI", 11),
            fg=TEXT_DIM, bg=BG_DARK
        ).pack(anchor="w")

        sep = tk.Frame(self.root, bg=ACCENT_BLUE, height=2)
        sep.pack(fill="x", padx=30, pady=(10, 15))

        self.status_label = tk.Label(
            self.root, text="Checking...", font=("Segoe UI", 11),
            fg=TEXT_WHITE, bg=BG_DARK, anchor="w"
        )
        self.status_label.pack(fill="x", padx=30)

        self.progress_var = tk.DoubleVar(value=0)
        self.progress = ttk.Progressbar(
            self.root, variable=self.progress_var,
            maximum=100, length=420, mode="determinate",
            style="Custom.Horizontal.TProgressbar"
        )
        self.progress.pack(padx=30, pady=(15, 10))

        self.detail_label = tk.Label(
            self.root, text="", font=("Segoe UI", 9),
            fg=TEXT_DIM, bg=BG_DARK, anchor="w"
        )
        self.detail_label.pack(fill="x", padx=30)

        self.result_label = tk.Label(
            self.root, text="", font=("Segoe UI", 10, "bold"),
            fg=TEXT_SUCCESS, bg=BG_DARK
        )
        self.result_label.pack(pady=(10, 0))

        self.ok_btn = tk.Button(
            self.root, text="OK", font=("Segoe UI", 10, "bold"),
            fg=TEXT_WHITE, bg=ACCENT_BLUE, activebackground=ACCENT_LIGHT,
            activeforeground=TEXT_WHITE, bd=0, padx=30, pady=6,
            command=self.root.destroy, cursor="hand2"
        )

    def set_status(self, text: str, color: str = TEXT_WHITE):
        self.status_label.config(text=text, fg=color)

    def set_detail(self, text: str):
        self.detail_label.config(text=text)

    def set_progress(self, value: float):
        self.progress_var.set(value)

    def show_result(self, text: str, color: str = TEXT_SUCCESS):
        self.result_label.config(text=text, fg=color)
        self.ok_btn.pack(pady=(5, 15))

    def run(self):
        threading.Thread(target=self._run_logic, daemon=True).start()
        self.root.mainloop()

    def _run_logic(self):
        self.root.after(0, self.set_status, "Checking if SnapNap is running...")
        time.sleep(0.5)

        if is_snapnap_running():
            self.root.after(0, self.set_status,
                            "Close SnapNap before uninstalling.", TEXT_WARN)
            self.root.after(0, self.set_detail, "")
            self.root.after(0, self.progress.pack_forget)
            for i in range(5, 0, -1):
                self.root.after(0, self.result_label.config,
                                {"text": f"Closing in {i}s...", "fg": TEXT_WARN})
                self.root.after(0, self.result_label.pack,
                                {"pady": (10, 0)})
                time.sleep(1)
            self.root.after(0, self.root.destroy)
            return

        self.root.after(0, self.set_status, "Uninstalling...")
        self.root.after(0, self.set_detail, "[1/3]  Deleting AppData folder...")
        self.root.after(0, self.set_progress, 5)
        time.sleep(0.4)
        s1 = step_delete_appdata()
        self.root.after(0, self.set_progress, 30)
        time.sleep(0.3)

        self.root.after(0, self.set_detail, "[2/3]  Removing scheduled task...")
        time.sleep(0.3)
        s2 = step_remove_task()
        self.root.after(0, self.set_progress, 55)
        time.sleep(0.3)

        self.root.after(0, self.set_detail,
                        "[3/3]  Searching for SnapNap.exe — this may take a moment...")
        time.sleep(0.3)
        s3 = step_find_and_delete_exe()
        self.root.after(0, self.set_progress, 100)
        time.sleep(0.3)

        errors: list[str] = []
        if not s1:
            errors.append("AppData folder")
        if not s2:
            errors.append("scheduled task")
        if not s3:
            errors.append("SnapNap.exe file(s)")

        if errors:
            msg = f"Completed with issues: could not remove {', '.join(errors)}"
            self.root.after(0, self.set_detail, "")
            self.root.after(0, self.set_status, "Done", TEXT_WARN)
            self.root.after(0, self.show_result, msg, TEXT_WARN)
        else:
            self.root.after(0, self.set_detail, "")
            self.root.after(0, self.set_status, "Done", TEXT_SUCCESS)
            self.root.after(0, self.show_result,
                            "Successfully uninstalled", TEXT_SUCCESS)



def main():
    if not is_admin():
        elevate_self()

    app = UninstallerApp()
    app.run()


if __name__ == "__main__":
    main()
