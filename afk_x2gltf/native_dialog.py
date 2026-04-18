from __future__ import annotations

import queue
import sys
import threading


def pick_folder(title: str = "选择目录") -> str | None:
    if not sys.platform.startswith("win"):
        return _tk_pick_folder(title)
    return _tk_pick_folder(title)


def _tk_pick_folder(title: str) -> str | None:
    result: queue.Queue[str | None] = queue.Queue(maxsize=1)

    def worker() -> None:
        try:
            import tkinter as tk
            from tkinter import filedialog

            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            root.update_idletasks()
            path = filedialog.askdirectory(title=title, parent=root)
            root.destroy()
            result.put(path or None)
        except Exception:
            result.put(None)

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout=120)
    if t.is_alive():
        return None
    try:
        return result.get_nowait()
    except queue.Empty:
        return None
