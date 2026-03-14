# SnapNap — Developer Notes

## Overview

SnapNap is a Windows background utility that allows the user to **pause and resume the currently focused application** using a global hotkey.

It works by suspending and resuming the **entire process tree** of the target application using Windows native APIs.

The design prioritizes:

* safety (avoiding critical system processes)
* responsiveness
* minimal CPU usage
* simple user interaction

---

# Core Concept

When the user presses the hotkey:

```
Alt + Shift + A
```

SnapNap performs the following steps:

1. Detect the current foreground window.
2. Identify the owning process.
3. Build the full process tree of that process.
4. Suspend every process in the tree.
5. Save the session state.

When the hotkey is pressed again:

1. Resume all suspended processes.
2. Restore the window.
3. Bring the application back to the foreground.

---

# Architecture

The program is organized into several logical components.

```
WindowDetector
        │
        ▼
ProcessController
        │
        ▼
SessionManager
        │
        ▼
Hotkey Loop + Tray Interface
```

---

# Components

## WindowDetector

Responsible for identifying the **current foreground window**.

Responsibilities:

* Get active window handle
* Ignore desktop/taskbar windows
* Ignore SnapNap's own window

Important APIs used:

```
win32gui.GetForegroundWindow
win32gui.GetClassName
win32process.GetWindowThreadProcessId
```

Protected desktop classes:

```
Shell_TrayWnd
Progman
WorkerW
Shell_SecondaryTrayWnd
```

These represent the desktop or taskbar and should never be suspended.

---

# ProcessController

Responsible for:

* validating safe processes
* building the process tree
* suspending and resuming processes

## Process Safety

The application avoids suspending critical Windows processes.

Protected list includes:

```
explorer.exe
dwm.exe
csrss.exe
winlogon.exe
services.exe
lsass.exe
smss.exe
svchost.exe
wininit.exe
system
idle
```

Additionally, any process with:

```
PID < 1000
```

is treated as protected.

This prevents accidental suspension of core system processes.

---

## Process Tree Construction

SnapNap suspends **entire process trees**, not just the main process.

Example (Google Chrome):

```
chrome.exe
 ├ renderer.exe
 ├ gpu-process.exe
 └ utility.exe
```

If only the root process were suspended, child processes might continue running.

Process trees are built using:

```
psutil.Process.children(recursive=True)
```

The tree is stored as:

```
[(pid, process_name)]
```

---

# Suspend / Resume Implementation

SnapNap uses undocumented but stable Windows NT APIs:

```
NtSuspendProcess
NtResumeProcess
```

These functions suspend **all threads in a process**.

Workflow:

```
OpenProcess(PROCESS_SUSPEND_RESUME)
      │
      ▼
NtSuspendProcess
```

Processes are suspended **from deepest child → root**.

This avoids situations where parent processes spawn new children during suspension.

Resume order is reversed.

---

# Session Manager

The `SessionManager` controls application state.

States:

```
IDLE
PAUSED
```

Only **one paused session** is supported at a time.

Stored session data:

```
AppSession
 ├ hwnd
 ├ pid
 └ suspended_pids
```

This allows SnapNap to restore:

* process execution
* window state
* application focus

---

# Window Restore Logic

Windows restricts applications from stealing focus.

SnapNap bypasses this restriction by:

1. Simulating an `Alt` key press.
2. Using `AttachThreadInput`.
3. Calling `SetForegroundWindow`.

Sequence:

```
keybd_event(VK_MENU)
AttachThreadInput
SetForegroundWindow
```

This ensures the resumed application becomes the foreground window.

---

# Hotkey System

Global hotkeys are registered using:

```
RegisterHotKey
```

Configuration:

```
Alt + Shift + A
```

The hotkey is processed inside a **background message loop thread** using:

```
PeekMessageW
```

This avoids blocking the main UI thread.

---

# System Tray Interface

SnapNap uses the library:

```
pystray
```

The tray menu provides:

```
Resume
Exit
```

Exit is disabled while an application is paused to avoid leaving suspended processes.

The tray icon also supports **left-click menu opening** via a patched message handler.

---

# Startup Behavior

SnapNap registers itself with **Windows Task Scheduler**.

Command used:

```
schtasks /Create
```

Settings:

```
Trigger: ONLOGON
Run Level: HIGHEST
Interactive: yes
```

Additional configuration ensures the task runs even when the device is on battery.

---

# Single Instance Protection

To prevent multiple SnapNap instances, a global mutex is used.

```
CreateMutexW("Global\\SnapNap_Mutex")
```

If another instance exists, the program exits immediately.

---

# Logging System

Logs are stored at:

```
%APPDATA%\SnapNap\suspend_manager.log
```

The logging system records:

* suspension events
* resume events
* process safety checks
* errors

---

# First Launch Experience

On first launch, SnapNap shows a message box explaining the hotkey.

A marker file is created:

```
%APPDATA%\SnapNap\.launched
```

This ensures the message appears **only once**.

---

# Safety Behavior

To prevent leaving applications frozen:

If SnapNap exits while an application is paused:

```
manager.resume()
```

is automatically executed.

This ensures processes are restored before shutdown.

---

# Dependencies

SnapNap depends on the following Python packages:

```
psutil
pywin32
pystray
Pillow
```

Core Windows APIs are accessed via:

```
ctypes
```

---

# Design Goals

SnapNap was designed with the following goals:

* Minimal resource usage
* Safe process handling
* Simple user interaction
* Fast suspend/resume operations
* Stable behavior on Windows 10 and 11

---

# Limitations

Some applications may disconnect from servers while paused.

Examples:

* online games
* file downloads
* network sessions

This occurs because the application stops responding to network activity while suspended.

---

# Future Improvements

Potential improvements include:

* multiple paused sessions
* configurable hotkeys
* custom tray icons
* pause indicators
* better handling of GPU-intensive applications

---

# Summary

SnapNap provides a lightweight implementation of **application-level quick resume for Windows** by leveraging process suspension techniques and safe process tree management.
