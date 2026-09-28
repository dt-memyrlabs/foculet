#!/usr/bin/env python3
"""
Foculet - a Windows focus-parking app.

The loop:
  * every time you switch to a new window on a screen, that screen's
    previous window is "parked": Foculet snaps a thumbnail picture of it,
    minimizes the real window, and pins the picture to the board on
    the dump monitor - a giant taskbar of pictures of what you were doing
  * click a picture -> the real window restores to the monitor it came from
  * right-click a picture -> the real window is closed
  * close a parked window -> its picture vanishes from the board

Usage:
  py -3 foculet.py [--dump rightmost] [--config foculet.json]

  --dump: rightmost | primary | <0-based index left-to-right> | <device name>
"""

import argparse
import ctypes
import json
import math
import os
import shutil
import sys
import threading
import time
import traceback
import tkinter as tk

from PIL import Image, ImageTk, ImageDraw

import win32api
import win32con
import win32gui
import win32process
import win32console
import win32ui

# PrintWindow is not wrapped by this pywin32 build - call it via ctypes.
# PW_RENDERFULLCONTENT (2) asks the app to render its full content.
_print_window = ctypes.windll.user32.PrintWindow
_print_window.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
_print_window.restype = ctypes.c_bool


def print_window(hwnd, hdc):
    for flag in (2, 0):
        try:
            if _print_window(hwnd, hdc, flag):
                return True
        except Exception:
            pass
    return False

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(HERE, "foculet.log")
THUMB_DIR = os.path.join(HERE, "thumbs")

POLL_SECS = 0.5
CLOSE_UNDO_SECS = 20  # right-click arms a close; undo window
GRID_COLS = 3            # default board grid; Daniel can change it in
GRID_ROWS = 2            # foculet.json (grid_cols/grid_rows) or the setup picker
GRID_TIERS = [(3, 2), (3, 3), (4, 3)]  # auto-grow: board expands as it
                                      # fills (Daniel: >6 -> 3x3, >9 -> 4x3)
NEVER_PARK_DEFAULT = set()  # Daniel picks these himself in
                         # foculet.json -> never_park; no preselected apps
THUMB_MAX = (560, 400)   # fallback; the real size is computed from the
                         # dump monitor's work area and the grid

OWN_CONSOLE = win32console.GetConsoleWindow()


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    try:
        if sys.stdout.isatty():
            print(line, flush=True)
    except Exception:
        pass


CRASH_PATH = os.path.join(HERE, "foculet-crash.log")
_crash_fh = None  # held open for faulthandler; see _install_crash_handlers


def _install_crash_handlers():
    """Log every otherwise-silent death to foculet-crash.log.

    Under pythonw there is no console, so an unhandled exception in the
    watcher thread, the tray thread, or a Tk callback would vanish
    without a trace and the app would just look "crashed". This routes
    all of them (plus faulthandler for hard crashes) into a file.
    """
    global _crash_fh
    import faulthandler
    _crash_fh = open(CRASH_PATH, "a", encoding="utf-8", buffering=1)
    _crash_fh.write(f"\n=== foculet started pid={os.getpid()} "
                    f"at {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
    faulthandler.enable(file=_crash_fh)

    def _write(kind, exc):
        try:
            _crash_fh.write(f"[{time.strftime('%H:%M:%S')}] {kind}: "
                            f"{exc!r}\n")
            traceback.print_exception(type(exc), exc, exc.__traceback__,
                                      file=_crash_fh)
            _crash_fh.write("\n")
        except Exception:
            pass
        log(f"CRASH ({kind}): {exc!r} -- full traceback in foculet-crash.log")

    def _excepthook(typ, val, tb):
        _write("main thread", val)

    def _thread_excepthook(args):
        name = args.thread.name if args.thread else "?"
        _write(f"thread {name}", args.exc_value)

    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook


# ---------------------------------------------------------------- monitors

def list_monitors():
    mons = []
    for hmon, _hdc, _rect in win32api.EnumDisplayMonitors():
        info = win32api.GetMonitorInfo(hmon)
        mons.append({
            "handle": hmon,
            "device": info["Device"],
            "rect": info["Monitor"],   # (left, top, right, bottom)
            "work": info["Work"],      # work area (excludes taskbar)
            "primary": bool(info["Flags"] & win32con.MONITORINFOF_PRIMARY),
        })
    mons.sort(key=lambda m: m["rect"][0])  # left to right
    return mons


def monitor_from_rect(rect):
    try:
        hmon = win32api.MonitorFromRect(rect, win32con.MONITOR_DEFAULTTONEAREST)
        info = win32api.GetMonitorInfo(hmon)
        return {
            "handle": hmon,
            "device": info["Device"],
            "rect": info["Monitor"],
            "work": info["Work"],
            "primary": bool(info["Flags"] & win32con.MONITORINFOF_PRIMARY),
        }
    except Exception:
        return None


def safe_rect(hwnd):
    try:
        return win32gui.GetWindowRect(hwnd)
    except Exception:
        return None


class _Onboarding:
    """3-step setup wizard: welcome -> dump monitor -> done.
    Runs on the Tk thread. Closing the window keeps the current
    values (same contract as the old first-run picker)."""

    def __init__(self, monitors, cfg, master=None):
        self.monitors = monitors
        self.cfg = cfg
        devices = {m["device"] for m in monitors}
        rightmost = max(monitors, key=lambda m: m["rect"][0])["device"]
        saved = cfg.get("dump_monitor")
        self._dump_default = saved if saved in devices else rightmost
        if master is None:
            self.win = tk.Tk()
            self._own_root = True
        else:
            self.win = tk.Toplevel(master)
            self._own_root = False
        w = self.win
        w.title("Foculet setup")
        w.configure(bg="#1e1e1e")
        w.resizable(False, False)
        prim = next(m for m in monitors if m["primary"])
        pl, pt, pr, pb = prim["rect"]
        ww, wh = 540, 460
        w.geometry(f"{ww}x{wh}+{pl + (pr - pl) // 2 - ww // 2}"
                   f"+{pt + (pb - pt) // 2 - wh // 2}")
        self.frames = []
        self.step = 0
        body = tk.Frame(w, bg="#1e1e1e")
        body.pack(fill="both", expand=True, padx=24, pady=(20, 4))
        self.body = body
        self._build_welcome()
        self._build_monitor()
        self._build_done()
        w.protocol("WM_DELETE_WINDOW", self._finish)
        self._show(0)
        if self._own_root:
            w.mainloop()
        else:
            w.transient(master)
            w.grab_set()
            master.wait_window(w)

    # -- frame plumbing ------------------------------------------------

    def _frame(self):
        f = tk.Frame(self.body, bg="#1e1e1e")
        self.frames.append(f)
        return f

    def _title(self, parent, text):
        tk.Label(parent, text=text, bg="#1e1e1e", fg="#ffffff",
                 font=("Segoe UI", 14, "bold")).pack(anchor="w", pady=(0, 12))

    def _body(self, parent, text):
        tk.Label(parent, text=text, bg="#1e1e1e", fg="#bbbbbb",
                 font=("Segoe UI", 10), wraplength=480,
                 justify="left").pack(anchor="w", pady=3)

    def _nav(self, f, back, next_text, on_next=None):
        row = tk.Frame(f, bg="#1e1e1e")
        row.pack(side="bottom", fill="x", pady=(20, 0))
        if back:
            tk.Button(row, text="< Back",
                      command=lambda: self._show(self.step - 1),
                      bg="#2a2a2a", fg="#bbbbbb",
                      activebackground="#3a3a3a", activeforeground="#ffffff",
                      bd=0, padx=12, pady=5, cursor="hand2").pack(side="left")
        tk.Button(row, text=next_text,
                  command=on_next or (lambda: self._show(self.step + 1)),
                  bg="#d99a2b", fg="#1a1a1a", activebackground="#e8ab3f",
                  bd=0, padx=18, pady=5, cursor="hand2",
                  font=("Segoe UI", 10, "bold")).pack(side="right")

    def _show(self, i):
        self.step = i
        for n, f in enumerate(self.frames):
            if n == i:
                f.pack(fill="both", expand=True)
            else:
                f.pack_forget()
        if i == 2:
            self._done_summary.configure(
                text=f"Dump monitor: {self._dump_var.get()}\n"
                     f"Board: {self._cols_var.get()} x {self._rows_var.get()} "
                     f"(size applies after a restart)")

    # -- steps ----------------------------------------------------------

    def _build_welcome(self):
        f = self._frame()
        self._title(f, "Foculet watches your windows.")
        self._body(f, "\u2022  Every time you switch windows, the one you "
                      "left is parked as a picture on your dump monitor.")
        self._body(f, "\u2022  Minimizing a window parks it too.")
        self._body(f, "\u2022  Click a picture to bring the window back "
                      "where it came from. Right-click a picture to close "
                      "that window after a 20-second countdown.")
        self._body(f, "\u2022  Fullscreen apps and games are never touched.")
        self._nav(f, back=False, next_text="Next >")

    def _build_monitor(self):
        f = self._frame()
        self._title(f, "Pick your dump monitor")
        self._body(f, "Foculet covers it with the parking picture board, "
                      "so pick one you can dedicate to it.")
        self._dump_var = tk.StringVar(value=self._dump_default)
        for i, m in enumerate(self.monitors):
            l, t, r, b = m["rect"]
            pos = "left" if i == 0 else ("right" if i == len(self.monitors) - 1
                                        else "middle")
            label = f"Monitor {i + 1}: {r - l}x{b - t} at ({l},{t}) \u2014 " \
                    f"{pos}" + (" \u2014 PRIMARY" if m["primary"] else "")
            tk.Radiobutton(f, text=label, variable=self._dump_var,
                           value=m["device"], anchor="w", justify="left",
                           bg="#1e1e1e", fg="#bbbbbb",
                           selectcolor="#2a2a2a",
                           activebackground="#1e1e1e",
                           activeforeground="#ffffff").pack(anchor="w", pady=1)
        grow = tk.Frame(f, bg="#1e1e1e")
        grow.pack(anchor="w", pady=(14, 0))
        tk.Label(grow, text="Board grid:", bg="#1e1e1e", fg="#bbbbbb",
                 font=("Segoe UI", 10)).pack(side="left")
        self._cols_var = tk.IntVar(value=self.cfg.get("grid_cols",
                                                      GRID_COLS))
        self._rows_var = tk.IntVar(value=self.cfg.get("grid_rows",
                                                      GRID_ROWS))
        tk.Spinbox(grow, from_=1, to=8, width=3,
                   textvariable=self._cols_var).pack(side="left", padx=(8, 2))
        tk.Label(grow, text="columns  x", bg="#1e1e1e", fg="#bbbbbb",
                 font=("Segoe UI", 10)).pack(side="left")
        tk.Spinbox(grow, from_=1, to=8, width=3,
                   textvariable=self._rows_var).pack(side="left", padx=(2, 2))
        tk.Label(grow, text="rows", bg="#1e1e1e", fg="#bbbbbb",
                 font=("Segoe UI", 10)).pack(side="left")
        self._body(f, "Board size applies after a restart. "
                      "The monitor itself switches immediately.")
        self._nav(f, back=True, next_text="Next >")

    def _build_done(self):
        f = self._frame()
        self._title(f, "You're set.")
        self._done_summary = tk.Label(f, bg="#1e1e1e", fg="#bbbbbb",
                                      font=("Segoe UI", 10), justify="left")
        self._done_summary.pack(anchor="w", pady=(0, 8))
        self._body(f, "Right-click the tray icon any time to re-run this "
                      "setup, pause parking, or exit Foculet.")
        self._nav(f, back=True, next_text="Finish", on_next=self._finish)

    # -- actions ---------------------------------------------------------

    def _finish(self):
        try:
            cols = max(1, min(8, int(self._cols_var.get())))
            rows = max(1, min(8, int(self._rows_var.get())))
        except Exception:
            cols, rows = GRID_COLS, GRID_ROWS
        self.cfg["dump_monitor"] = self._dump_var.get()
        self.cfg["grid_cols"] = cols
        self.cfg["grid_rows"] = rows
        try:
            self.win.destroy()
        except Exception:
            pass

    def result(self):
        return self.cfg


def onboarding_wizard(monitors, cfg, master=None):
    """3-step setup wizard (welcome, dump monitor, done).
    Returns the updated cfg dict. Closing the window keeps the current
    values."""
    cfg.setdefault("grid_cols", GRID_COLS)
    cfg.setdefault("grid_rows", GRID_ROWS)
    return _Onboarding(monitors, cfg, master).result()


def pick_dump(monitors, spec):
    if spec == "rightmost":
        return max(monitors, key=lambda m: m["rect"][0])
    if spec == "primary":
        return next(m for m in monitors if m["primary"])
    if spec.isdigit():
        return monitors[int(spec) % len(monitors)]
    for m in monitors:
        if m["device"].lower() == spec.lower():
            return m
    raise ValueError(f"unknown --dump spec: {spec!r}")


# ---------------------------------------------------------------- windows

def exe_of(hwnd):
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        h = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        try:
            path = win32process.GetModuleFileNameEx(h, 0)
        finally:
            win32api.CloseHandle(h)
        return os.path.basename(path).lower()
    except Exception:
        return ""


def safe_title(hwnd):
    try:
        return win32gui.GetWindowText(hwnd).strip()
    except Exception:
        return ""


def is_fullscreen(hwnd, rect):
    mon = monitor_from_rect(rect)
    return bool(mon) and tuple(rect) == tuple(mon["rect"])


def is_owned(hwnd):
    """True if the window is owned by another window (dialogs, popups,
    dropdowns). Owned windows are never real switches and never parked."""
    try:
        return bool(win32gui.GetWindow(hwnd, win32con.GW_OWNER))
    except Exception:
        return False


# Hosts of transient system UI: tray overflow, Start menu, search,
# notification center, quick settings, credential/PIN prompts
# (Windows Hello), touch keyboard, emoji picker / clipboard history,
# UAC, OOBE, lock screen. These pop OVER your work; focusing one is
# never a real task switch, so the window behind it must not be parked.
_SHELL_HOSTS = {"explorer.exe", "startmenuexperiencehost.exe",
                "searchhost.exe", "shellexperiencehost.exe",
                "credentialuibroker.exe", "tabtip.exe",
                "textinputhost.exe", "consent.exe",
                "useroobebroker.exe", "lockapp.exe", "sihost.exe"}


def _class_name(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    try:
        if ctypes.windll.user32.GetClassNameW(hwnd, buf, 256):
            return buf.value
    except Exception:
        pass
    return ""


def is_shell_transient(hwnd):
    """True for transient shell UI (tray overflow flyout, Start menu,
    volume/network flyouts). Never a real switch, never parked.

    Explorer *file* windows (CabinetWClass) are real windows and are
    left alone by this check."""
    try:
        ex = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        if ex & win32con.WS_EX_TOOLWINDOW:
            return True
    except Exception:
        pass
    try:
        if exe_of(hwnd) in _SHELL_HOSTS \
                and _class_name(hwnd) != "CabinetWClass":
            return True
    except Exception:
        pass
    return False


def age_badge(age_s):
    """(label, color) showing how long a window has been parked."""
    m = int(age_s // 60)
    if m < 1:
        return "<1m", "#888888"
    txt = f"{m}m" if m < 60 else (f"{m // 60}h" if m < 1440
                                  else f"{m // 1440}d")
    if m < 15:
        return txt, "#888888"   # fresh
    if m < 60:
        return txt, "#d99a2b"   # waiting a while
    return txt, "#d95f2b"       # waiting long


def parkable(hwnd, dump_device, excluded_exes, never_park=frozenset(),
             quiet=False, allow_iconic=False):
    """True if this window is eligible to be parked right now.

    quiet: don't log the reason for a skip (for high-frequency checks).
    allow_iconic: the minimize hook fires after the window is already
    iconic - eligibility without the minimized check."""
    def no(why):
        if not quiet:
            log(f"skip '{safe_title(hwnd)}': {why}")
        return False
    if hwnd == OWN_CONSOLE:
        return False
    try:
        if not win32gui.IsWindow(hwnd) or not win32gui.IsWindowVisible(hwnd):
            return no("not a live/visible window")
        if win32gui.IsIconic(hwnd) and not allow_iconic:
            return no("already minimized")
        if win32gui.GetParent(hwnd):          # not a top-level window
            return no("not top-level")
        if is_owned(hwnd):                    # a dialog/popup: belongs to
            return no("owned dialog/popup")   # its owner, never parked alone
        if is_shell_transient(hwnd):          # tray overflow, Start menu,
            return no("transient shell UI")   # flyouts: not real windows
    except Exception:
        return False
    title = safe_title(hwnd)
    if not title or title == "Program Manager":
        return no("no title")
    if exe_of(hwnd) in excluded_exes:
        return no("in excluded_exes")
    if exe_of(hwnd).lower() in never_park:
        return no("key app - never parked")
    try:
        rect = win32gui.GetWindowRect(hwnd)
    except Exception:
        return no("no rect")
    if is_fullscreen(hwnd, rect):
        return no("fullscreen (never yank a fullscreen app/game)")
    mon = monitor_from_rect(rect)
    if mon and mon["device"] == dump_device:
        return no("already on the dump monitor")
    return True


def is_alive(hwnd):
    try:
        return bool(win32gui.IsWindow(hwnd))
    except Exception:
        return False


def is_maximized(hwnd):
    try:
        return win32gui.GetWindowPlacement(hwnd)[1] == win32con.SW_SHOWMAXIMIZED
    except Exception:
        return False


def place(hwnd, x, y, w, h):
    """Restore-if-maximized, then move without stealing focus.
    Returns True only if the window actually moved."""
    try:
        if is_maximized(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetWindowPos(hwnd, 0, x, y, w, h,
                              win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE)
        return True
    except Exception as e:
        log(f"move failed for hwnd={hwnd}: {e}")
        return False


# ---------------------------------------------------------------- thumbnails

def _finish_thumb(tmp, path, maxsize):
    """Shared PIL tail for both capture paths: keep the recognizable
    top ~62%, scale to cover the cell anchored top-left, save the PNG."""
    try:
        img = Image.open(tmp).convert("RGB")
        if img.getbbox() is None:
            return False  # captured nothing but black
        tw, th = maxsize
        # the recognizable part lives at the top: crop there first
        img = img.crop((0, 0, img.width, int(img.height * 0.62)))
        # scale to cover the cell, anchored top-left (keeps tabs/title)
        scale = max(tw / img.width, th / img.height)
        img = img.resize((max(1, int(img.width * scale + 0.5)),
                          max(1, int(img.height * scale + 0.5))),
                         Image.LANCZOS)
        img = img.crop((0, 0, tw, th))
        img.save(path, "PNG")
        return True
    except Exception as e:
        log(f"thumbnail failed: {e}")
        return False
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass


def _bitblt_window(hwnd, tmp):
    """Copy the window's on-screen pixels straight off the display.

    ~10ms and the target app is never asked to render, so it never
    blocks. Only valid while the window is fully visible (foreground);
    anywhere else the pixels would be some other window's."""
    l, t, r, b = win32gui.GetWindowRect(hwnd)
    w, h = r - l, b - t
    if w <= 0 or h <= 0:
        return False
    screen_dc = win32gui.GetDC(0)
    try:
        mfc_dc = win32ui.CreateDCFromHandle(screen_dc)
        save_dc = mfc_dc.CreateCompatibleDC()
        try:
            bmp = win32ui.CreateBitmap()
            bmp.CreateCompatibleBitmap(mfc_dc, w, h)
            save_dc.SelectObject(bmp)
            save_dc.BitBlt((0, 0), (w, h), mfc_dc, (l, t),
                           win32con.SRCCOPY)
            bmp.SaveBitmapFile(save_dc, tmp)
            return True
        finally:
            save_dc.DeleteDC()
            mfc_dc.DeleteDC()
    finally:
        win32gui.ReleaseDC(0, screen_dc)


def capture_thumbnail(hwnd, path, maxsize, fast=False):
    """Snap the window to a PNG thumbnail that fills a board cell.

    Keeps the top ~62% of the window - title bar, tabs, toolbar, top of
    the content: the part you actually recognize - then scales it to
    cover maxsize, anchored top-left, so the picture fills its cell
    with no empty bands.
    fast=True takes the window straight off the screen (BitBlt): ~10ms
    and the app is never involved, so it can't stutter - but it's only
    correct while the window is the foreground (fully visible) window.
    Otherwise the app is asked to render via PrintWindow.
    Returns True on success."""
    tmp = path + ".bmp"
    try:
        if fast:
            if not _bitblt_window(hwnd, tmp):
                return False
        else:
            l, t, r, b = win32gui.GetWindowRect(hwnd)
            w, h = r - l, b - t
            if w <= 0 or h <= 0:
                return False
            hwnd_dc = win32gui.GetWindowDC(hwnd)
            try:
                mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
                save_dc = mfc_dc.CreateCompatibleDC()
                try:
                    bmp = win32ui.CreateBitmap()
                    bmp.CreateCompatibleBitmap(mfc_dc, w, h)
                    save_dc.SelectObject(bmp)
                    if not print_window(hwnd, save_dc.GetSafeHdc()):
                        return False
                    bmp.SaveBitmapFile(save_dc, tmp)
                finally:
                    save_dc.DeleteDC()
                    mfc_dc.DeleteDC()
            finally:
                win32gui.ReleaseDC(hwnd, hwnd_dc)
        return _finish_thumb(tmp, path, maxsize)
    except Exception as e:
        log(f"thumbnail failed: {e}")
        return False
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass


def make_placeholder_thumb(path, title, size=(296, 200)):
    """Fallback picture when a window refuses to be screenshotted."""
    try:
        img = Image.new("RGB", size, "#242424")
        d = ImageDraw.Draw(img)
        txt = (title[:40] + "…") if len(title) > 40 else title
        d.text((16, size[1] // 2 - 8), txt or "(no title)", fill="#bbbbbb")
        img.save(path, "PNG")
    except Exception as e:
        log(f"placeholder failed: {e}")


class Foculet:
    def __init__(self, dump_spec, excluded_exes,
                 grid_cols=GRID_COLS, grid_rows=GRID_ROWS,
                 never_park=(), config_path=None):
        self.excluded = set(excluded_exes)
        # key apps (chat etc.) that are never parked, even on a switch
        self.never_park = {e.lower() for e in never_park} | \
            {e.lower() for e in NEVER_PARK_DEFAULT}
        self.monitors = list_monitors()
        self.by_device = {m["device"]: m for m in self.monitors}
        self.dump = pick_dump(self.monitors, dump_spec)
        try:
            grid_cols = max(1, min(8, int(grid_cols)))
            grid_rows = max(1, min(8, int(grid_rows)))
        except Exception:
            grid_cols, grid_rows = GRID_COLS, GRID_ROWS
        self.grid_cols = grid_cols
        self.grid_rows = grid_rows
        self.max_parked = grid_cols * grid_rows  # board slots = grid cells
        # thumbnail size that fills a board cell: dump work area minus
        # the board/cell padding, minus room for the title label
        wl, wt, wr, wb = self.dump["work"]
        cell_w = (wr - wl - 24) // grid_cols - 12
        cell_h = (wb - wt - 24) // grid_rows - 12
        self.thumb_max = (max(240, cell_w - 8), max(160, cell_h - 48))
        self.lock = threading.Lock()
        self._quiet_until = 0.0  # focus changes before this are ours
                                 # (minimize/restore); the watcher absorbs
                                 # them instead of treating them as switches
        self._last_fg_cache = 0.0  # last refresh of the focused window's
                                   # snapshot (backs every park)
        self._paused = threading.Event()  # set from the tray menu: parking
                                          # halts until resumed
        self._tray_hwnd = None  # tray message window (tray thread)
        self._config_path = config_path  # foculet.json, for setup re-runs
        self._parking = set()  # hwnds currently inside park(): the minimize
                               # hook must not re-park our own minimize
        self._notify_queue = []  # pending tray balloon tips (tray thread)
        self._thumb_cache = {}  # hwnd -> png path: fresh snapshot of the
                                # focused window, so a later minimize can
                                # park it with a real picture (an iconic
                                # window can't be captured)
        self.parked = {}  # hwnd -> {"origin_rect", "origin_device", "exe",
                          #          "title", "thumb", "was_maximized"}
        self.current = {}  # monitor device -> hwnd currently owning that screen
        self._thumb_seq = 0
        os.makedirs(THUMB_DIR, exist_ok=True)
        # clear stale thumbnails from a previous run
        for f in os.listdir(THUMB_DIR):
            if f.endswith(".png"):
                try:
                    os.remove(os.path.join(THUMB_DIR, f))
                except Exception:
                    pass
        log(f"monitors: {[(m['device'], m['rect'], 'PRIMARY' if m['primary'] else '') for m in self.monitors]}")
        log(f"dump monitor: {self.dump['device']} {self.dump['rect']}")
        log(f"mode: park on every switch, board "
            f"{self.grid_cols}x{self.grid_rows}, "
            f"excluded: {sorted(self.excluded) or 'none'}, "
            f"never-park: {sorted(self.never_park) or 'none'}")

    # -- parking ----------------------------------------------------

    def _grow_grid(self):
        """Grow the board to the next GRID_TIERS step. True if it grew.

        Called from park() (watcher thread): only touches plain
        attributes. The Tk grid reconfigure happens in rebuild_lot,
        which runs on the main thread."""
        cur_cells = self.grid_cols * self.grid_rows
        nxt = None
        for cols, rows in GRID_TIERS:
            if cols * rows > cur_cells:
                nxt = (cols, rows)
                break
        if not nxt:
            return False
        cols, rows = nxt
        self.grid_cols, self.grid_rows = cols, rows
        self.max_parked = cols * rows
        wl, wt, wr, wb = self.dump["work"]
        cell_w = (wr - wl - 24) // cols - 12
        cell_h = (wb - wt - 24) // rows - 12
        self.thumb_max = (max(240, cell_w - 8), max(160, cell_h - 48))
        self._lot_key = None  # force a full rebuild on the next tick
        log(f"board grew to {cols}x{rows} ({self.max_parked} slots)")
        return True

    def _warm_cache(self):
        """Snapshot every eligible window once at startup, so windows
        that were already open (never focused since we started) still
        park with a real picture when minimized."""
        time.sleep(2)  # let the tray/board settle first
        try:
            hwnds = []
            win32gui.EnumWindows(lambda h, _: hwnds.append(h) or True, None)
        except Exception:
            return
        n = 0
        for hwnd in hwnds:
            if self._paused.is_set():
                return
            before = len(self._thumb_cache)
            self._cache_thumb(hwnd)
            if len(self._thumb_cache) > before:
                n += 1
        log(f"thumbnail cache warmed: {n} window(s)")

    def _cache_thumb(self, hwnd):
        """Snapshot the newly focused window. If the user minimizes it
        later, the minimize hook fires too late to capture (the window is
        already iconic) - this cache is the picture it parks with.
        The foreground window is taken straight off the screen (BitBlt):
        ~10ms and the app never blocks. Anything else falls back to
        PrintWindow."""
        if not hwnd or hwnd in self.parked_keys():
            return
        try:
            if win32process.GetWindowThreadProcessId(hwnd)[1] == os.getpid():
                return  # our own window (board, setup wizard)
        except Exception:
            return
        if not parkable(hwnd, self.dump["device"], self.excluded,
                        self.never_park, quiet=True):
            return
        with self.lock:
            self._thumb_seq += 1
            seq = self._thumb_seq
            while len(self._thumb_cache) >= 24:  # bound the cache
                oldest = next(iter(self._thumb_cache))
                try:
                    os.remove(self._thumb_cache.pop(oldest))
                except Exception:
                    pass
        path = os.path.join(THUMB_DIR, f"cache_{hwnd}_{seq}.png")
        try:
            is_fg = win32gui.GetForegroundWindow() == hwnd
        except Exception:
            is_fg = False
        ok = capture_thumbnail(hwnd, path, self.thumb_max, fast=is_fg)
        if not ok and is_fg:
            # screen copy failed on a foreground window (odd) - fall
            # back to asking the app to render
            ok = capture_thumbnail(hwnd, path, self.thumb_max)
        if ok:
            with self.lock:
                prev = self._thumb_cache.pop(hwnd, None)
                self._thumb_cache[hwnd] = path
            if prev:
                try:
                    os.remove(prev)
                except Exception:
                    pass
        else:
            try:
                os.remove(path)
            except Exception:
                pass

    def _cache_thumb_later(self, hwnd):
        """Refresh the focus-time snapshot off the critical path: let
        the switch animation settle, then grab the foreground window
        straight off the screen (~10ms, the app never blocks). If focus
        already moved on, that switch's own call handles it."""
        def _go():
            time.sleep(0.25)
            try:
                if win32gui.GetForegroundWindow() != hwnd:
                    return
                if hwnd in self.parked_keys():
                    return
            except Exception:
                return
            self._cache_thumb(hwnd)
        threading.Thread(target=_go, daemon=True,
                         name="foculet-cache").start()

    def _drop_cache(self, hwnd):
        with self.lock:
            path = self._thumb_cache.pop(hwnd, None)
        if path:
            try:
                os.remove(path)
            except Exception:
                pass

    def park(self, hwnd, thumb_src=None):
        # The minimize hook can fire while we are already parking this
        # window (our own minimize); never park a window twice.
        with self.lock:
            if hwnd in self._parking:
                return
            self._parking.add(hwnd)
        try:
            self._park_inner(hwnd, thumb_src)
        finally:
            with self.lock:
                self._parking.discard(hwnd)

    def _park_inner(self, hwnd, thumb_src=None):
        with self.lock:
            # board full? grow through the tiers before giving up
            while len(self.parked) >= self.max_parked and self._grow_grid():
                pass
            if len(self.parked) >= self.max_parked:
                log(f"board full ({self.max_parked}); "
                    f"not parking '{safe_title(hwnd)}'")
                return
        try:
            placement = win32gui.GetWindowPlacement(hwnd)
        except Exception:
            placement = None
        if placement and placement[1] == win32con.SW_SHOWMINIMIZED:
            # parked from the minimize hook: GetWindowRect is garbage for
            # an iconic window - restore from the placement's normal rect
            rect = placement[4]
            was_max = bool(placement[0] & getattr(
                win32con, "WPF_RESTORETOMAXIMIZED", 2))
        else:
            try:
                rect = win32gui.GetWindowRect(hwnd)
            except Exception:
                return
            was_max = is_maximized(hwnd)
        mon = monitor_from_rect(rect)
        title = safe_title(hwnd)
        with self.lock:
            self._thumb_seq += 1
            seq = self._thumb_seq
        thumb = os.path.join(THUMB_DIR, f"{hwnd}_{seq}.png")
        if thumb_src and os.path.exists(thumb_src):
            # a fresh snapshot from when the window was focused (used by
            # the minimize path - an iconic window can't be captured)
            try:
                shutil.copy(thumb_src, thumb)
            except Exception:
                thumb_src = None
        if not thumb_src or not os.path.exists(thumb):
            if not capture_thumbnail(hwnd, thumb, self.thumb_max):
                make_placeholder_thumb(thumb, title, self.thumb_max)
        try:
            win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
        except Exception as e:
            log(f"could not minimize '{title}': {e}")
            try:
                os.remove(thumb)
            except Exception:
                pass
            return
        # minimizing moves focus; don't read that as the user switching
        with self.lock:
            self._quiet_until = time.time() + 1.5
        with self.lock:
            self.parked[hwnd] = {
                "origin_rect": rect,
                "origin_device": mon["device"] if mon else None,
                "exe": exe_of(hwnd),
                "title": title,
                "thumb": thumb,
                "was_maximized": was_max,
                "parked_at": time.time(),  # board sorts oldest-first
            }
            for dev, h in list(self.current.items()):
                if h == hwnd:
                    del self.current[dev]  # no longer any screen's current window
        self._drop_cache(hwnd)  # its focus-time snapshot is now the picture
        log(f"PARKED '{title}' -> {self.dump['device']}")

    def unpark(self, hwnd):
        if hwnd in self._pending_close:
            self._cancel_close(hwnd, "restored")
        with self.lock:
            p = self.parked.pop(hwnd, None)
        if not p:
            return
        mon = self.by_device.get(p["origin_device"]) or \
            next(m for m in self.monitors if m["primary"])
        l, t, r, b = p["origin_rect"]
        wl, wt, wr, wb = mon["work"]
        w, h = min(r - l, wr - wl), min(b - t, wb - wt)
        x, y = min(max(l, wl), wr - w), min(max(t, wt), wb - h)
        try:
            # un-minimize first, then move home without stealing focus
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            if not place(hwnd, x, y, w, h):
                raise RuntimeError("window would not move")
            if p.get("was_maximized"):
                win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)
            win32gui.SetForegroundWindow(hwnd)
            # restoring moves focus on purpose; absorb the echo
            with self.lock:
                self._quiet_until = time.time() + 1.5
        except Exception as e:
            log(f"could not unpark '{p['title']}': {e}")
            with self.lock:
                self.parked[hwnd] = p  # keep it parked
            return
        try:
            os.remove(p["thumb"])
        except Exception:
            pass
        with self.lock:
            self.current[mon["device"]] = hwnd  # it owns its home screen again
        log(f"UNPARKED '{p['title']}' -> {mon['device']}")

    def close_parked(self, hwnd):
        """Arm/cancel a delayed close (right-click a board picture).

        Gmail-style: the window only really closes after CLOSE_UNDO_SECS.
        Until then the cell shows a red countdown — right-click again or
        left-click (restore) to cancel."""
        with self.lock:
            p = self.parked.get(hwnd)
        if not p:
            return
        if hwnd in self._pending_close:
            self._cancel_close(hwnd, "toggled off")
        else:
            self._pending_close[hwnd] = {
                "deadline": time.time() + CLOSE_UNDO_SECS,
                "title": p["title"],
            }
            cell = self._cell_by_hwnd.get(hwnd)
            if cell:
                try:
                    cell.configure(highlightbackground="#aa3333",
                                   highlightthickness=2)
                except Exception:
                    pass
            log(f"close ARMED for '{p['title']}' - "
                f"undo within {CLOSE_UNDO_SECS}s (right-click / left-click)")

    def _cancel_close(self, hwnd, why):
        pend = self._pending_close.pop(hwnd, None)
        if not pend:
            return
        cell = self._cell_by_hwnd.get(hwnd)
        if cell:
            try:
                cell.configure(highlightbackground="#333333",
                               highlightthickness=1)
            except Exception:
                pass
        # restore the badge: without this the red countdown stays frozen,
        # because _tick_badges only runs while a close is pending
        for h, badge, pa in getattr(self, "_badge_widgets", []):
            if h == hwnd:
                try:
                    txt, fg = age_badge(time.time() - pa)
                    badge.configure(text=txt, fg=fg)
                except Exception:
                    pass
                break
        log(f"close CANCELLED for '{pend['title']}' ({why})")

    def _fire_close(self, hwnd):
        pend = self._pending_close.pop(hwnd, None)
        if not pend:
            return
        with self.lock:
            p = self.parked.get(hwnd)
        # hwnd reuse guard: only close it if it's still the same window
        if not p or p["title"] != pend["title"] or not is_alive(hwnd):
            log(f"close for '{pend['title']}' skipped - window already gone")
            return
        try:
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
            log(f"CLOSED '{pend['title']}' (undo expired)")
        except Exception as e:
            log(f"could not close '{pend['title']}': {e}")

    def sweep_dead(self):
        """Forget parked windows the user closed; delete their pictures."""
        for hwnd in [h for h in self._pending_close if not is_alive(h)]:
            self._pending_close.pop(hwnd, None)
            log("close disarmed - window died on its own")
        gone = []
        with self.lock:
            for h in list(self.parked):
                if not is_alive(h):
                    gone.append(self.parked.pop(h))
        for p in gone:
            log(f"forgot closed window '{p['title']}'")
            try:
                os.remove(p["thumb"])
            except Exception:
                pass
        with self.lock:
            dead_cached = [h for h in self._thumb_cache if not is_alive(h)]
        for h in dead_cached:
            self._drop_cache(h)

    def parked_keys(self):
        with self.lock:
            return set(self.parked)

    # -- system tray: raw win32, no extra libraries --------------------
    # A dedicated thread owns a hidden message window and registers the
    # icon with Shell_NotifyIcon - the same API every native Windows app
    # uses. Left- or right-click opens the menu.

    _TRAY_ID_PAUSE = 1001
    _TRAY_ID_EXIT = 1002
    _TRAY_ID_SETUP = 1003
    _TRAY_NOTIFY = win32con.WM_USER + 21  # posted when a balloon tip waits

    def _make_tray_hicon(self):
        """Build the amber icon directly as an HICON (32-bit color +
        1-bit mask). No .ico file involved, so Windows can't misread it."""
        import struct
        from PIL import ImageFont
        try:
            size = 32
            letter = (self._app_name or "V")[0].upper()
            img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.rounded_rectangle([2, 2, size - 3, size - 3], radius=7,
                                fill=(217, 154, 43, 255))
            try:
                font = ImageFont.truetype("segoeui.ttf", 20)
            except Exception:
                font = ImageFont.load_default()
            d.text((size // 2, size // 2 + 1), letter,
                   fill=(20, 20, 20, 255), font=font, anchor="mm")
            px = img.load()
            xor = bytearray()
            mask = bytearray()
            for y in range(size - 1, -1, -1):  # BMP rows are bottom-up
                dword = 0
                for x in range(size):
                    r, g, b, a = px[x, y]
                    xor += bytes((b, g, r, a))
                    if a < 128:  # AND mask: 1 bit = transparent
                        dword |= (1 << (31 - x))
                mask += struct.pack("<I", dword)
            gdi32 = ctypes.windll.gdi32
            hxor = gdi32.CreateBitmap(size, size, 1, 32, bytes(xor))
            hmask = gdi32.CreateBitmap(size, size, 1, 1, bytes(mask))
            hicon = win32gui.CreateIconIndirect((True, 0, 0, hxor, hmask))
            log(f"tray icon built: hicon={hicon}")
            return hicon
        except Exception as e:
            log(f"tray icon build failed ({e}); using stock icon")
            return win32gui.LoadIcon(0, win32con.IDI_APPLICATION)

    def _show_tray_menu(self, hwnd):
        name = self._app_name
        with self.lock:
            n = len(self.parked)
        menu = win32gui.CreatePopupMenu()
        win32gui.AppendMenu(menu,
                            win32con.MF_STRING | win32con.MF_DISABLED,
                            0, name)
        win32gui.AppendMenu(menu,
                            win32con.MF_STRING | win32con.MF_DISABLED,
                            0, "1 parked" if n == 1 else f"{n} parked")
        win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, "")
        win32gui.AppendMenu(menu, win32con.MF_STRING, self._TRAY_ID_SETUP,
                            "Setup\u2026")
        win32gui.AppendMenu(menu, win32con.MF_STRING, self._TRAY_ID_PAUSE,
                            "Resume parking" if self._paused.is_set()
                            else "Pause parking")
        win32gui.AppendMenu(menu, win32con.MF_STRING, self._TRAY_ID_EXIT,
                            "Exit")
        x, y = win32gui.GetCursorPos()
        win32gui.SetForegroundWindow(hwnd)
        win32gui.TrackPopupMenu(menu, win32con.TPM_LEFTALIGN,
                                x, y, 0, hwnd, None)
        win32gui.PostMessage(hwnd, win32con.WM_NULL, 0, 0)
        win32gui.DestroyMenu(menu)

    def _tray_wndproc(self, hwnd, msg, wparam, lparam):
        WM_TRAY = win32con.WM_USER + 20
        if msg == self._wm_taskbarcreated:
            # Explorer was restarted: our icon registration died with it.
            # Re-add the icon so it comes back on its own.
            try:
                win32gui.Shell_NotifyIcon(
                    win32gui.NIM_ADD,
                    (hwnd, 0,
                     win32gui.NIF_ICON | win32gui.NIF_MESSAGE
                     | win32gui.NIF_TIP,
                     WM_TRAY, self._tray_hicon, self._app_name))
                log("tray icon re-registered after explorer restart")
            except Exception as e:
                log(f"tray icon re-register failed: {e}")
            return 0
        if msg == WM_TRAY:
            if lparam in (win32con.WM_LBUTTONUP, win32con.WM_RBUTTONUP):
                self._show_tray_menu(hwnd)
            return 0
        if msg == self._TRAY_NOTIFY:
            self._flush_notifications(hwnd)
            return 0
        if msg == win32con.WM_COMMAND:
            wid = win32api.LOWORD(wparam)
            if wid == self._TRAY_ID_SETUP:
                # the wizard must run on the Tk thread, not the tray thread
                self.root.after(0, self.open_setup)
            elif wid == self._TRAY_ID_PAUSE:
                if self._paused.is_set():
                    self._paused.clear()
                    log("parking resumed from tray")
                else:
                    self._paused.set()
                    log("parking paused from tray")
            elif wid == self._TRAY_ID_EXIT:
                log("exit requested from tray")
                try:
                    win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, (hwnd, 0))
                finally:
                    win32gui.DestroyWindow(hwnd)
                    # Tk must die on its own thread
                    self.root.after(0, self.root.destroy)
            return 0
        if msg == win32con.WM_DESTROY:
            try:
                win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, (hwnd, 0))
            except Exception:
                pass
            win32gui.PostQuitMessage(0)
            return 0
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    def _tray_main(self):
        WM_TRAY = win32con.WM_USER + 20
        name = self._app_name
        hicon = self._make_tray_hicon()
        self._tray_hicon = hicon
        self._wm_taskbarcreated = win32gui.RegisterWindowMessage(
            "TaskbarCreated")
        wc = win32gui.WNDCLASS()
        wc.lpfnWndProc = self._tray_wndproc
        wc.lpszClassName = "FoculetTrayWindow"
        try:
            win32gui.RegisterClass(wc)
        except Exception:
            pass  # already registered
        hwnd = win32gui.CreateWindow(wc.lpszClassName, name,
                                     0, 0, 0, 0, 0, 0, 0, 0, None)
        self._tray_hwnd = hwnd
        nid = (hwnd, 0,
               win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP,
               WM_TRAY, hicon, name)
        try:
            win32gui.Shell_NotifyIcon(win32gui.NIM_ADD, nid)
        except Exception as e:
            log(f"tray icon registration failed: {e}")
            self._tray_hwnd = None
            return
        log("tray icon running")
        win32gui.PumpMessages()
        self._tray_hwnd = None

    def start_tray(self):
        self._app_name = self.root.title() or "Foculet"
        t = threading.Thread(target=self._tray_main, daemon=True,
                             name="foculet-tray")
        t.start()

    def notify(self, title, msg):
        """Balloon tip on the tray icon. Thread-safe: the tip itself is
        shown on the tray thread."""
        with self.lock:
            self._notify_queue.append((title, msg))
        hwnd = self._tray_hwnd
        if hwnd:
            try:
                win32gui.PostMessage(hwnd, self._TRAY_NOTIFY, 0, 0)
            except Exception:
                pass

    def _flush_notifications(self, hwnd):
        with self.lock:
            items = self._notify_queue
            self._notify_queue = []
        nif_info = getattr(win32gui, "NIF_INFO", 0x10)
        niif_info = getattr(win32con, "NIIF_INFO", 1)
        for title, msg in items:
            try:
                win32gui.Shell_NotifyIcon(
                    win32gui.NIM_MODIFY,
                    (hwnd, 0, nif_info, 0, 0, "",
                     msg, 0, title, niif_info))
            except Exception as e:
                log(f"tray notify failed: {e}")

    # -- setup (re-runnable from the tray) -----------------------------

    def open_setup(self):
        """Re-run the setup wizard. Called on the Tk thread."""
        if not self._config_path:
            return
        try:
            cfg = load_config(self._config_path)
        except Exception:
            cfg = {}
        cfg.setdefault("excluded_exes", sorted(self.excluded))
        cfg.setdefault("never_park", sorted(self.never_park))
        new = onboarding_wizard(self.monitors, cfg, master=self.root)
        save_config(self._config_path, new)
        self.apply_setup(new)
        self.notify("Foculet", "Setup complete.")

    def apply_setup(self, cfg):
        """Apply wizard choices live. The dump monitor moves immediately;
        the board grid size takes effect on the next start."""
        new_dump = cfg.get("dump_monitor")
        if new_dump and new_dump != self.dump["device"]:
            self.monitors = list_monitors()
            self.by_device = {m["device"]: m for m in self.monitors}
            try:
                self.dump = pick_dump(self.monitors, new_dump)
            except ValueError as e:
                log(f"dump switch failed: {e}")
                return
            wl, wt, wr, wb = self.dump["work"]
            try:
                self.root.geometry(f"{wr - wl}x{wb - wt}+{wl}+{wt}")
            except Exception as e:
                log(f"board move failed: {e}")
            log(f"dump monitor switched to {self.dump['device']}")
            self.notify("Foculet",
                        f"Dump monitor is now {self.dump['device']}.")

    # -- picture board (runs on the main/Tk thread) ------------------

    def _toggle_pause(self):
        if self._paused.is_set():
            self._paused.clear()
            log("parking resumed from board bar")
        else:
            self._paused.set()
            log("parking paused from board bar")
        self._sync_topbar()

    def _sync_topbar(self, n=None):
        try:
            if n is None:
                with self.lock:
                    n = len(self.parked)
            self._top_label.configure(
                text=f"Foculet · {n} parked" if n != 1 else "Foculet · 1 parked")
            self._pause_btn.configure(
                text="Resume" if self._paused.is_set() else "Pause")
        except Exception:
            pass

    def build_lot(self):
        self.root = tk.Tk()
        self.root.title("Foculet")
        self.root.configure(bg="#141414")
        self.root.overrideredirect(True)  # borderless: just the board
        wl, wt, wr, wb = self.dump["work"]
        self.root.geometry(f"{wr - wl}x{wb - wt}+{wl}+{wt}")
        # slim top bar: board identity, parked count, pause/resume
        self.topbar = tk.Frame(self.root, bg="#1e1e1e", height=30)
        self.topbar.pack(side="top", fill="x")
        self.topbar.pack_propagate(False)
        self._top_label = tk.Label(self.topbar, text="Foculet",
                                   fg="#999999", bg="#1e1e1e",
                                   font=("Segoe UI", 9))
        self._top_label.pack(side="left", padx=10)
        self._pause_btn = tk.Button(
            self.topbar, text="Pause", command=self._toggle_pause,
            fg="#bbbbbb", bg="#2a2a2a", activeforeground="#ffffff",
            activebackground="#3a3a3a", font=("Segoe UI", 9),
            bd=0, padx=10, pady=2, cursor="hand2")
        self._pause_btn.pack(side="right", padx=8, pady=3)
        self.grid_frame = tk.Frame(self.root, bg="#141414")
        self.grid_frame.pack(expand=True, fill="both", padx=12, pady=12)
        for c in range(self.grid_cols):
            self.grid_frame.grid_columnconfigure(c, weight=1, uniform="cell")
        for r in range(self.grid_rows):
            self.grid_frame.grid_rowconfigure(r, weight=1, uniform="cell")
        self._lot_key = None
        self._lot_age_key = None
        self._photos = []
        self._badge_widgets = []  # (hwnd, badge label, parked_at)
        self._cell_by_hwnd = {}   # hwnd -> cell frame (for close-arm tint)
        self._pending_close = {}  # hwnd -> {"deadline", "title"}
        self.root.withdraw()  # hidden until the first window parks
        try:
            # the board must never steal focus: deiconify() can activate
            # a window on Windows, and an activated board would read as
            # a "switch" and cascade into parks
            bh = self.root.winfo_id()
            ex = win32gui.GetWindowLong(bh, win32con.GWL_EXSTYLE)
            win32gui.SetWindowLong(bh, win32con.GWL_EXSTYLE,
                                   ex | win32con.WS_EX_NOACTIVATE)
        except Exception as e:
            log(f"board noactivate failed: {e}")
        self.root.after(500, self.refresh_lot)

    def rebuild_lot(self, items):
        # the grid may have grown since the last build (auto-grow tiers):
        # reconfigure columns/rows here, on the main thread
        for c in range(self.grid_cols):
            self.grid_frame.grid_columnconfigure(c, weight=1, uniform="cell")
        for r in range(self.grid_rows):
            self.grid_frame.grid_rowconfigure(r, weight=1, uniform="cell")
        for w in self.grid_frame.winfo_children():
            w.destroy()
        self._photos = []
        self._badge_widgets = []  # (hwnd, badge label, parked_at): ticked in place
        self._cell_by_hwnd = {}
        for i in range(self.max_parked):
            cell = tk.Frame(self.grid_frame, bg="#1e1e1e",
                            highlightbackground="#333333", highlightthickness=1)
            cell.grid(row=i // self.grid_cols, column=i % self.grid_cols,
                      padx=6, pady=6, sticky="nsew")
            if i < len(items):
                hwnd, title, thumb, parked_at = items[i]
                age_txt, age_fg = age_badge(time.time() - parked_at)
                photo = None
                try:
                    if os.path.exists(thumb):
                        photo = ImageTk.PhotoImage(Image.open(thumb))
                    else:
                        log(f"thumb gone for '{title}' (raced an unpark)")
                except Exception as e:
                    log(f"thumb load failed for '{title}': {e}")
                if photo:
                    self._photos.append(photo)  # keep a reference
                    pic = tk.Label(cell, image=photo, bg="#1e1e1e",
                                   cursor="hand2")
                    pic.pack(expand=True)
                    pic.bind("<Button-1>", lambda e, h=hwnd: self.unpark(h))
                    pic.bind("<Button-3>",
                             lambda e, h=hwnd: self.close_parked(h))
                # age badge: how long this window has been waiting
                badge = tk.Label(cell, text=age_txt, fg=age_fg, bg="#101010",
                                 font=("Segoe UI", 8), padx=4, pady=1)
                badge.place(relx=1.0, rely=0.0, anchor="ne", x=-4, y=4)
                self._badge_widgets.append((hwnd, badge, parked_at))
                self._cell_by_hwnd[hwnd] = cell
                if hwnd in self._pending_close:
                    # rebuild kept an armed close: re-tint the new cell
                    cell.configure(highlightbackground="#aa3333",
                                   highlightthickness=2)
                name = (title[:34] + "…") if len(title) > 34 else title
                tl = tk.Label(cell, text=name or "(no title)", fg="#bbbbbb",
                              bg="#1e1e1e", font=("Segoe UI", 9),
                              cursor="hand2")
                tl.pack(pady=(0, 6))
                tl.bind("<Button-1>", lambda e, h=hwnd: self.unpark(h))
                tl.bind("<Button-3>", lambda e, h=hwnd: self.close_parked(h))
                # right-click anywhere on the picture: close that window
                cell.bind("<Button-1>", lambda e, h=hwnd: self.unpark(h))
                cell.bind("<Button-3>",
                          lambda e, h=hwnd: self.close_parked(h))

    def _tick_badges(self, now):
        expired = []
        for hwnd, badge, pa in getattr(self, "_badge_widgets", []):
            try:
                pend = self._pending_close.get(hwnd)
                if pend:
                    left = pend["deadline"] - now
                    if left <= 0:
                        expired.append(hwnd)
                    else:
                        badge.configure(text=f"\u2715 {math.ceil(left)}s",
                                        fg="#ff6666")
                else:
                    txt, fg = age_badge(now - pa)
                    badge.configure(text=txt, fg=fg)
            except Exception:
                pass
        for hwnd in expired:
            self._fire_close(hwnd)

    def refresh_lot(self):
        try:
            with self.lock:
                # oldest parked first: the board reads like a timeline
                now = time.time()
                ordered = sorted(self.parked.items(),
                                 key=lambda kv: kv[1].get("parked_at", 0))
                items = [(h, p["title"], p["thumb"], p.get("parked_at", now))
                         for h, p in ordered if is_alive(h)]
            # identity key: full rebuild only when the parked SET
            # changes. Age badges tick in place (no rebuild, no blink).
            id_key = tuple(h for h, _, _, _ in items)
            age_key = tuple(int((now - pa) // 60) for _, _, _, pa in items)
            if id_key != self._lot_key:
                if items:
                    self.root.deiconify()
                    self.root.lower()  # stay at the bottom of the z-order
                else:
                    self.root.withdraw()
                self.rebuild_lot(items)
                # advance the key only after a successful build, so a
                # failed render retries on the next tick instead of
                # freezing the board on stale/empty cells
                self._lot_key = id_key
                self._lot_age_key = age_key
                log(f"board: {len(items)} picture(s)")
            elif age_key != self._lot_age_key or self._pending_close:
                self._tick_badges(now)
                self._lot_age_key = age_key
            hwnd = self._tray_hwnd
            if hwnd:
                try:
                    n = len(items)
                    tip = f"{self.root.title()} - 1 parked" if n == 1 else \
                        f"{self.root.title()} - {n} parked"
                    win32gui.Shell_NotifyIcon(
                        win32gui.NIM_MODIFY,
                        (hwnd, 0, win32gui.NIF_TIP, 0, 0, tip))
                except Exception:
                    pass
            self._sync_topbar(len(items))
        except Exception as e:
            log(f"lot refresh failed: {e}")
        self.root.after(500, self.refresh_lot)

    # -- focus watcher (runs on a background thread) -----------------

    def watcher(self):
        log("foculet running - every switch parks the screen's previous window.")
        prev = None
        while True:
            time.sleep(POLL_SECS)
            try:
                fg = win32gui.GetForegroundWindow()
            except Exception:
                continue
            self.sweep_dead()

            if fg != prev:
                # a switch happened: prev -> fg. right after our own
                # minimize/restore, absorb the focus echo instead of
                # reading it as another switch (that echo is what used
                # to cascade: park -> focus jump -> park...)
                with self.lock:
                    quiet = time.time() < self._quiet_until
                paused = self._paused.is_set()
                if not quiet and not paused:
                    self.on_fg_change(fg)
                prev = fg
                with self.lock:
                    self._last_fg_cache = time.time()
            elif fg and not self._paused.is_set():
                # no switch: the focused window's snapshot backs every
                # later park(), so refresh it while it's cheap (the
                # foreground BitBlt path) instead of re-capturing at
                # switch time
                with self.lock:
                    due = time.time() - self._last_fg_cache > 30
                    if due:
                        self._last_fg_cache = time.time()
                if due:
                    self._cache_thumb(fg)

    def on_fg_change(self, fg):
        # each screen remembers its current window; the moment you
        # focus a new window on a screen, that screen's previous
        # window gets its picture taken and is parked on the board.
        if fg and (is_owned(fg) or is_shell_transient(fg)
                   or fg == getattr(self, "_tray_hwnd", None)):
            return  # a dialog/popup opened (file picker, save dialog),
                    # transient shell UI (tray overflow, Start menu),
                    # or our own tray window (its menu steals focus
                    # via SetForegroundWindow when you click the icon):
                    # not a real switch - leave everything alone
        if fg in self.parked_keys():
            # A parked window became foreground. That can be a deliberate
            # restore (taskbar click, board is separate) - or just a
            # passing glance: Alt+Tab preview, taskbar hover peek, or an
            # app raising itself for a moment. Only unpark it if it
            # stays put; a glance leaves it parked.
            time.sleep(1.0)
            try:
                still_there = win32gui.GetForegroundWindow() == fg
            except Exception:
                still_there = False
            if still_there and fg in self.parked_keys():
                self.unpark(fg)
            return
        fg_rect = safe_rect(fg) if fg else None
        fg_mon = monitor_from_rect(fg_rect) if fg_rect else None
        if fg and fg_mon and fg_mon["device"] != self.dump["device"]:
            dev = fg_mon["device"]
            with self.lock:
                prev_current = self.current.get(dev)
            if (prev_current and prev_current != fg
                    and prev_current not in self.parked_keys()
                    and parkable(prev_current, self.dump["device"],
                                 self.excluded, self.never_park)):
                # the snapshot taken when this window was focused is the
                # picture: re-capturing here would make the app being
                # left behind re-render for no visible gain
                with self.lock:
                    thumb_src = self._thumb_cache.get(prev_current)
                # park() enforces the board cap
                self.park(prev_current, thumb_src=thumb_src)
            if fg != OWN_CONSOLE:
                with self.lock:
                    self.current[dev] = fg
        # keep a fresh snapshot of the newly focused window: if the user
        # minimizes it later, the minimize hook fires too late to capture
        # (the window is already iconic) - the cache is the picture.
        # off the critical path, so the switch itself never stutters.
        if fg and not self._paused.is_set():
            self._cache_thumb_later(fg)

    # -- minimize parking (its own thread with a message pump) ---------

    def _minimize_hook_main(self):
        """Catch the user's minimize via EVENT_SYSTEM_MINIMIZESTART and
        park the window exactly like a focus-switch park. NOTE: the event
        is delivered out-of-context, after the window is already iconic
        (verified by probe) - so the picture comes from the focus-time
        snapshot cache, not a fresh capture."""
        import ctypes.wintypes as wt
        EVENT_SYSTEM_MINIMIZESTART = 0x0016
        WINEVENT_OUTOFCONTEXT = 0x0000

        @ctypes.WINFUNCTYPE(None, wt.HANDLE, wt.DWORD, wt.HWND, wt.LONG,
                           wt.LONG, wt.DWORD, wt.DWORD)
        def _cb(hhook, event, hwnd, id_obj, id_child, tid, ts):
            try:
                self.on_minimize_start(hwnd)
            except Exception as e:
                log(f"minimize hook: {e}")

        self._min_hook_cb = _cb  # keep alive: the hook holds no reference
        hook = ctypes.windll.user32.SetWinEventHook(
            EVENT_SYSTEM_MINIMIZESTART, EVENT_SYSTEM_MINIMIZESTART,
            None, _cb, 0, 0, WINEVENT_OUTOFCONTEXT)
        if not hook:
            log("minimize hook failed to install; minimize-parking is off")
            return
        self._min_hook = hook
        log("minimize hook installed - minimizing a window parks it too")
        msg = wt.MSG()
        while ctypes.windll.user32.GetMessageW(ctypes.byref(msg), None, 0, 0):
            ctypes.windll.user32.TranslateMessage(ctypes.byref(msg))
            ctypes.windll.user32.DispatchMessageW(ctypes.byref(msg))

    def on_minimize_start(self, hwnd):
        """The user minimized a window: park it the same way a switch
        would. Runs on the hook thread. The hook fires after the window
        is already iconic (verified by probe), so the picture comes from
        the focus-time snapshot cache - an iconic window can't be
        captured."""
        if not hwnd or hwnd in self.parked_keys():
            return
        with self.lock:
            if (self._paused.is_set() or hwnd in self._parking
                    or time.time() < self._quiet_until):
                return
            thumb_src = self._thumb_cache.get(hwnd)
        if hwnd == getattr(self, "_tray_hwnd", None):
            return
        try:
            if win32process.GetWindowThreadProcessId(hwnd)[1] == os.getpid():
                return  # our own window (board, setup wizard): never park it
        except Exception:
            return
        try:
            placement = win32gui.GetWindowPlacement(hwnd)
        except Exception:
            return
        normal = placement[4] if placement else None
        if not normal:
            return
        # NOTE: GetWindowRect is garbage for an iconic window - the two
        # rect-dependent checks must use the placement's normal rect.
        if is_fullscreen(hwnd, normal):
            return  # never yank a fullscreen app/game
        mon = monitor_from_rect(normal)
        if mon and mon["device"] == self.dump["device"]:
            return  # minimizing a window that's already on the dump board
        if not parkable(hwnd, self.dump["device"], self.excluded,
                        self.never_park, quiet=True, allow_iconic=True):
            return
        log(f"minimized by user - parking '{safe_title(hwnd)}'")
        self.park(hwnd, thumb_src=thumb_src)

    def _tk_crash(self, exc, val, tb):
        try:
            with open(CRASH_PATH, "a", encoding="utf-8") as f:
                f.write(f"[{time.strftime('%H:%M:%S')}] tk callback: "
                        f"{val!r}\n")
                traceback.print_exception(exc, val, tb, file=f)
                f.write("\n")
            log(f"CRASH (tk callback): {val!r} -- see foculet-crash.log")
        except Exception:
            pass

    def run(self):
        self.build_lot()
        self.root.report_callback_exception = self._tk_crash
        self.start_tray()  # needs root (for the title); runs its own thread
        t = threading.Thread(target=self.watcher, daemon=True,
                             name="foculet-watcher")
        t.start()
        m = threading.Thread(target=self._minimize_hook_main, daemon=True,
                             name="foculet-minimize")
        m.start()
        w = threading.Thread(target=self._warm_cache, daemon=True,
                             name="foculet-warmcache")
        w.start()
        if getattr(self, "_fresh_setup", False):
            # first run went through the setup wizard: say hello on the
            # tray once everything is up
            def _hello():
                time.sleep(6)
                self.notify(
                    "Foculet",
                    "Setup complete - Foculet is watching. Right-click "
                    "the tray icon any time to re-run setup.")
            threading.Thread(target=_hello, daemon=True,
                             name="foculet-hello").start()
        self.root.mainloop()


def load_config(path):
    cfg = {"excluded_exes": [], "never_park": []}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cfg.update(json.load(f))
    return cfg


def save_config(path, cfg):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def main():
    _install_crash_handlers()
    ap = argparse.ArgumentParser(
        description="Foculet - parks your previous window on the dump monitor")
    ap.add_argument("--dump", default=None,
                    help="rightmost | primary | <index> | <device name>")
    ap.add_argument("--config", default=os.path.join(HERE, "foculet.json"))
    args = ap.parse_args()

    cfg = load_config(args.config)
    dump = args.dump or cfg.get("dump_monitor")
    grid_cols = cfg.get("grid_cols", GRID_COLS)
    grid_rows = cfg.get("grid_rows", GRID_ROWS)
    fresh_setup = False
    if not dump:
        # first run: the setup wizard (welcome, dump monitor, done),
        # then remember the choices
        cfg = onboarding_wizard(list_monitors(), cfg)
        save_config(args.config, cfg)
        dump = cfg.get("dump_monitor")
        grid_cols = cfg.get("grid_cols", GRID_COLS)
        grid_rows = cfg.get("grid_rows", GRID_ROWS)
        fresh_setup = True
        log(f"setup complete: dump={dump} (board {grid_cols}x{grid_rows})")

    foculet = Foculet(dump, cfg["excluded_exes"], grid_cols, grid_rows,
                  cfg.get("never_park", []), config_path=args.config)
    foculet._fresh_setup = fresh_setup
    try:
        foculet.run()
    except KeyboardInterrupt:
        log("stopped.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        try:
            with open(CRASH_PATH, "a", encoding="utf-8") as f:
                f.write(f"[{time.strftime('%H:%M:%S')}] startup/main "
                        f"crashed\n")
                traceback.print_exc(file=f)
        except Exception:
            pass
        try:
            log("CRASH (startup): see foculet-crash.log")
        except Exception:
            pass
        sys.exit(1)



