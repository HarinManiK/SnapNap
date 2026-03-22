import json, logging, os, re, sys, urllib.request, webbrowser

logger = logging.getLogger("SnapNap")


LOCAL_VERSION = "1.2"
GITHUB_API_URL = (
    "https://api.github.com/repos/HarinManiK/SnapNap/releases/latest"
)
_REQUEST_TIMEOUT = 3



def _parse_version(version_str: str) -> tuple:
    cleaned = version_str.lstrip("v")
    return tuple(int(x) for x in cleaned.split("."))


def _fetch_latest_release() -> dict | None:
    try:
        req = urllib.request.Request(
            GITHUB_API_URL,
            headers={"User-Agent": f"SnapNap/{LOCAL_VERSION}"},
        )
        with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        logger.debug("Update check – fetch failed: %s", exc)
        return None


def _clean_release_notes(body: str) -> str:
    if not body:
        return ""
    lines = body.splitlines()
    cleaned = []
    for line in lines:
        cleaned.append(re.sub(r"^#+\s*", "", line))
    return "\n".join(cleaned)


def _icon_path() -> str | None:
    if getattr(sys, "frozen", False):
        base = sys._MEIPASS  # type: ignore[attr-defined]
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(base, "assets", "SnapNap2.ico")
    return path if os.path.isfile(path) else None


def _show_update_popup(
    latest_version: str,
    release_notes: str,
    html_url: str,
) -> None:
    import tkinter as tk
    from tkinter import messagebox

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)

    ico = _icon_path()
    if ico:
        try:
            root.iconbitmap(ico)
        except Exception:
            pass

    popup = tk.Toplevel(root)
    popup.title("SnapNap Update Available")
    popup.attributes("-topmost", True)
    popup.resizable(False, False)
    popup.protocol("WM_DELETE_WINDOW", lambda: _on_later(root))

    if ico:
        try:
            popup.iconbitmap(ico)
        except Exception:
            pass

    popup.update_idletasks()
    pw, ph = 460, 380
    sx = (popup.winfo_screenwidth() - pw) // 2
    sy = (popup.winfo_screenheight() - ph) // 2
    popup.geometry(f"{pw}x{ph}+{sx}+{sy}")

    tk.Label(
        popup,
        text=f"A new version of SnapNap is available (v{latest_version}).",
        wraplength=420,
        justify="left",
        pady=10,
        padx=10,
    ).pack(anchor="w")

    tk.Label(
        popup,
        text="Release notes:",
        font=("Segoe UI", 9, "bold"),
        padx=10,
    ).pack(anchor="w")

    notes_frame = tk.Frame(popup)
    notes_frame.pack(fill="both", expand=True, padx=10, pady=(0, 5))

    notes_text = tk.Text(
        notes_frame,
        wrap="word",
        height=10,
        borderwidth=1,
        relief="solid",
        font=("Segoe UI", 9),
    )
    scrollbar = tk.Scrollbar(notes_frame, command=notes_text.yview)
    notes_text.configure(yscrollcommand=scrollbar.set)
    scrollbar.pack(side="right", fill="y")
    notes_text.pack(side="left", fill="both", expand=True)
    notes_text.insert("1.0", release_notes)
    notes_text.configure(state="disabled")

    tk.Label(popup, text="Update now?", pady=5).pack()

    btn_frame = tk.Frame(popup)
    btn_frame.pack(pady=(0, 10))

    tk.Button(
        btn_frame,
        text="Later",
        width=10,
        command=lambda: _on_later(root),
    ).pack(side="left", padx=10)

    tk.Button(
        btn_frame,
        text="Yes",
        width=10,
        command=lambda: _on_yes(root, html_url),
    ).pack(side="left", padx=10)

    root.mainloop()


def _on_later(root) -> None:
    root.destroy()


def _on_yes(root, html_url: str) -> None:
    import tkinter.messagebox as mb

    mb.showinfo(
        "SnapNap Update",
        "Please uninstall the current version of SnapNap before "
        "installing the new version.\n\n"
        "Use uninstaller.exe to uninstall the current SnapNap.exe",
        parent=root,
    )
    webbrowser.open(html_url)
    root.destroy()


def run_update_check() -> None:
    try:
        release = _fetch_latest_release()
        if release is None:
            return

        tag = release.get("tag_name", "")
        body = release.get("body", "")
        html_url = release.get("html_url", "")

        if not tag:
            logger.debug("Update check – tag_name missing from response.")
            return

        remote_ver = _parse_version(tag)
        local_ver = _parse_version(LOCAL_VERSION)

        if remote_ver <= local_ver:
            return

        notes = _clean_release_notes(body)
        _show_update_popup(tag.lstrip("v"), notes, html_url)

    except Exception as exc:
        logger.debug("Update check failed: %s", exc)
