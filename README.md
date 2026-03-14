# SnapNap
**Xbox-style Quick Resume for Windows** - instantly pause &amp; resume apps or games with **0% CPU/GPU usage while paused**.


SnapNap is a small Windows utility that lets you **pause and resume the currently active application instantly** using a simple hotkey(**Alt+Shift+A**).

Instead of closing an application, SnapNap temporarily pauses it so it stops using CPU resources while keeping everything exactly as it was.

Press the hotkey again and the application continues running normally like nothing happened.

---

## Features

* Pause the active application instantly
* Resume the application with the same hotkey
* Works from anywhere in Windows
* System tray controls
* Avoids important Windows system processes
* Starts automatically when you log in
* Lightweight and fast

---

## Requirements

* Windows 10 or Windows 11
* Administrator permission (needed to pause applications)

---

## Installation

1. Download the latest version from:

https://github.com/<username>/SnapNap/releases

2. Run:

SnapNap.exe

That's it. SnapNap will start automatically the next time you log in.
---

## How to Use

1. Open or focus the application you want to pause.

Examples:

* A game
* A browser
* Any program doing heavy work

2. Press:

**Alt + Shift + A**

The application will be:

* minimized
* temporarily paused
* consumes 0% CPU and GPU

3. To resume the application:

Press **Alt + Shift + A** again.

The application will restore and continue running like nothing happened.

---

**Caution:**

If you pause an online game or an application that is connected to a server (for example, downloading a file), the server may disconnect you after some time.

When the application resumes, it may behave the same way it does when your internet connection drops.

Examples:

- Online games may show a **connection lost** message and ask you to reconnect.
- Downloads may pause or restart.

For situations like watching YouTube in Google Chrome, this usually works fine.

---

## System Tray

SnapNap runs quietly in the Windows system tray.

Available options:

**Resume**
Resumes the paused application.

**Exit**
Closes SnapNap (this option is disabled while an application is paused).

---

## Safety

SnapNap avoids pausing important Windows processes such as:

* explorer.exe
* dwm.exe
* winlogon.exe
* csrss.exe
* svchost.exe

This helps keep Windows running normally.

---

## Logs

SnapNap saves logs here:

```
C:\Users\<user>\AppData\Roaming\SnapNap\suspend_manager.log
```

Logs can help identify problems if something does not work as expected.

---

## Uninstall

To remove SnapNap, download and run:

SnapNapUninstaller.exe

---

## License

SnapNap is distributed under the **SnapNap Personal Use License (SPUL-1.0)**.
