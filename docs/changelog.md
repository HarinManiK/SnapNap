---
# Changelog
All notable changes to this project will be documented in this file.
**---**

## version 1.1

Integrated an update checker that fetches the latest release from GitHub and notifies the user if a newer version is available.

---


## version 1.0 - Initial Release

Initial release of SnapNap.

Implements a global hotkey (Alt + Shift + A) to pause and resume the currently active application. Suspension is done using native NT APIs and applies to the full process tree, including child processes, with correct ordering to avoid instability.

Includes safety checks to prevent suspending critical system processes and low-level OS tasks. Protected processes are skipped automatically.

Detects the current foreground window while ignoring desktop, taskbar, and self-process cases. On pause, the window is minimized before suspension. On resume, the process tree is restored and the window is brought back to the foreground.

Runs as a single instance using a global mutex. Automatically registers itself to run at login via Task Scheduler with elevated privileges.

Provides a system tray icon with minimal controls (resume and exit). Exit is disabled while a session is active to avoid leaving processes suspended.

Logs all activity to:
%APPDATA%/SnapNap/suspend_manager.log

Requires administrator privileges and relaunches itself with elevation if needed.

Known limitations:
- Windows only
- Some applications (especially those with anti-cheat or protection mechanisms) may not suspend correctly
- No configuration interface yet
