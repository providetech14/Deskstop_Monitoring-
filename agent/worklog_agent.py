"""
WorkLog Agent - runs on each employee's work PC (Windows).

* Time In / Start Break / End Break / Time Out from a small window
* Reminds the employee to time in (and to time out / end long breaks)
* Takes a screenshot every N minutes ONLY while timed in and not on break,
  and uploads it to the WorkLog server
* Optional: starts automatically with Windows

Needs only Python 3 (standard library). Run with:  pythonw worklog_agent.py
"""
import ctypes
import json
import os
import queue
import re
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
import zlib
from ctypes import wintypes
from datetime import datetime

import tkinter as tk
from tkinter import messagebox

APP_NAME = "WorkLog Agent"
CONF_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "WorkLog")
CONF_PATH = os.path.join(CONF_DIR, "agent.json")
STARTUP_DIR = os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs\Startup")
STARTUP_FILE = os.path.join(STARTUP_DIR, "WorkLogAgent.vbs")
HOST = socket.gethostname()
POLL_SECONDS = 60
REMIND_EVERY_MIN = 15
DISCOVERY_PORT = 5001
APP_DIR = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))
SERVER_FILE = os.path.join(APP_DIR, "server.txt")  # written by the installer

COLORS = {"bg": "#f5f6fb", "card": "#ffffff", "ink": "#1d2033", "muted": "#6b7084", "brand": "#5b4fe0",
          "in": "#138a52", "brk": "#d98a00", "back": "#2b7de9", "out": "#c62f3b", "off": "#c9ccd8"}
STATUS_TEXT = {"working": ("WORKING", "#138a52"), "break": ("ON BREAK", "#d98a00"),
               "out": ("NOT TIMED IN", "#6b7084")}


# ------------------------------------------------------------------ settings
def load_conf():
    try:
        with open(CONF_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_conf(conf):
    os.makedirs(CONF_DIR, exist_ok=True)
    with open(CONF_PATH, "w", encoding="utf-8") as f:
        json.dump(conf, f, indent=2)


# ------------------------------------------------------------------- network
class ApiError(Exception):
    def __init__(self, msg, code=0, body=None):
        super().__init__(msg)
        self.code, self.body = code, body or {}


def api(server, path, token=None, payload=None, raw=None, content_type="application/json", timeout=20):
    url = server.rstrip("/") + path
    headers = {"X-Host": HOST, "User-Agent": "WorkLogAgent/1.0"}
    data = None
    if token:
        headers["Authorization"] = "Bearer " + token
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    elif raw is not None:
        data = raw
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode())
        except ValueError:
            body = {}
        raise ApiError(body.get("error") or body.get("message") or f"Server error {e.code}", e.code, body)
    except (urllib.error.URLError, OSError) as e:
        raise ApiError(f"Cannot reach the WorkLog server ({getattr(e, 'reason', e)}).")


def default_server():
    try:
        with open(SERVER_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("http"):
                    return line.rstrip("/")
    except OSError:
        pass
    return ""


def discover(timeout=2.5):
    """Broadcast on the local network to find the WorkLog server. Returns its URL or None."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.settimeout(timeout)
        s.sendto(b"WORKLOG_DISCOVER", ("255.255.255.255", DISCOVERY_PORT))
        end = time.time() + timeout
        while time.time() < end:
            data, addr = s.recvfrom(256)
            parts = data.split()
            if len(parts) == 2 and parts[0] == b"WORKLOG_HERE":
                return f"http://{addr[0]}:{int(parts[1])}"
    except (OSError, ValueError):
        return None
    finally:
        s.close()
    return None


# --------------------------------------------------------------------- voice
DEFAULT_IDLE_MESSAGE = ("Hello {name}. You have not used your computer for {minutes} minutes. "
                        "Please get back to work, or click Start Break if you are on a break.")
_speech_proc = None


def idle_message(template, first_name, minutes):
    """Fill in {name} and {minutes}; says '1 minute' (not '1 minutes')."""
    text = (template or DEFAULT_IDLE_MESSAGE).replace("{name}", first_name).replace("{minutes}", str(minutes))
    return re.sub(r"\b1 minutes\b", "1 minute", text)


def speak(text, wav_file=None):
    """Say text aloud with the voice built into Windows (no extra software).
    With wav_file, saves the speech to that file instead (used for testing)."""
    global _speech_proc
    if _speech_proc is not None and _speech_proc.poll() is None:
        return _speech_proc  # still speaking the previous reminder
    safe = text.replace("'", "''")  # PowerShell single-quoted string: no code can run from the text
    out = f"$s.SetOutputToWaveFile('{wav_file}'); " if wav_file else ""
    cmd = ("Add-Type -AssemblyName System.Speech; "
           "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
           f"$s.Volume = 100; $s.Rate = -1; {out}$s.Speak('{safe}'); $s.Dispose()")
    _speech_proc = subprocess.Popen(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", cmd],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return _speech_proc


# ---------------------------------------------------------------- screenshot
user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


user32.GetLastInputInfo.argtypes = [ctypes.POINTER(LASTINPUTINFO)]
kernel32.GetTickCount.restype = wintypes.DWORD


def idle_seconds():
    """Seconds since the last keyboard/mouse input (no keystrokes are recorded)."""
    lii = LASTINPUTINFO(cbSize=ctypes.sizeof(LASTINPUTINFO))
    if not user32.GetLastInputInfo(ctypes.byref(lii)):
        return 0
    return ((kernel32.GetTickCount() - lii.dwTime) & 0xFFFFFFFF) / 1000.0

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.SetStretchBltMode.argtypes = [wintypes.HDC, ctypes.c_int]
gdi32.StretchBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                             wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT, ctypes.c_void_p,
                            ctypes.POINTER(BITMAPINFO), wintypes.UINT]
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]

MAX_WIDTH = 1920  # downscale very wide (multi-monitor) captures to keep files small


def capture_png():
    """Capture all monitors and return PNG bytes (no extra libraries needed)."""
    x, y = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
    w, h = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)
    if w <= 0 or h <= 0:
        raise RuntimeError("No screen found")
    w2 = min(w, MAX_WIDTH)
    h2 = max(1, int(h * w2 / w))
    screen = user32.GetDC(None)
    mem = gdi32.CreateCompatibleDC(screen)
    bmp = gdi32.CreateCompatibleBitmap(screen, w2, h2)
    old = gdi32.SelectObject(mem, bmp)
    try:
        gdi32.SetStretchBltMode(mem, 4)  # HALFTONE = good quality scaling
        if not gdi32.StretchBlt(mem, 0, 0, w2, h2, screen, x, y, w, h, 0x00CC0020 | 0x40000000):
            raise RuntimeError("Screen capture failed (screen locked?)")
        bi = BITMAPINFO()
        bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bi.bmiHeader.biWidth, bi.bmiHeader.biHeight = w2, -h2  # top-down
        bi.bmiHeader.biPlanes, bi.bmiHeader.biBitCount = 1, 32
        buf = ctypes.create_string_buffer(w2 * h2 * 4)
        if not gdi32.GetDIBits(mem, bmp, 0, h2, buf, ctypes.byref(bi), 0):
            raise RuntimeError("Could not read screen pixels")
    finally:
        gdi32.SelectObject(mem, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(None, screen)

    bgra = buf.raw
    rgb = bytearray(w2 * h2 * 3)
    rgb[0::3], rgb[1::3], rgb[2::3] = bgra[2::4], bgra[1::4], bgra[0::4]
    stride = w2 * 3
    raw = b"".join(b"\x00" + bytes(rgb[i:i + stride]) for i in range(0, len(rgb), stride))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w2, h2, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


# ------------------------------------------------------------------ startup
def agent_command():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    return f'"{pyw if os.path.exists(pyw) else exe}" "{os.path.abspath(__file__)}"'


def set_autostart(enabled):
    if enabled:
        os.makedirs(STARTUP_DIR, exist_ok=True)
        cmd = agent_command().replace('"', '""')
        with open(STARTUP_FILE, "w", encoding="utf-8") as f:
            f.write(f'CreateObject("WScript.Shell").Run "{cmd}", 0, False\n')
    elif os.path.exists(STARTUP_FILE):
        os.remove(STARTUP_FILE)


# ------------------------------------------------------------------------ UI
class Agent:
    def __init__(self):
        self.conf = load_conf()
        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.configure(bg=COLORS["bg"])
        self.root.resizable(False, False)
        icon = os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))), "worklog.ico")
        if os.path.exists(icon):
            try:
                self.root.iconbitmap(default=icon)
            except tk.TclError:
                pass
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.state = None            # last /api/status payload
        self.state_at = time.time()  # when it was received
        self.last_shot = 0.0
        self.last_remind = 0.0
        self.busy = False
        self.frame = None
        self.results = queue.Queue()
        self.root.after(100, self.drain)
        self.cur_min = int(time.time() // 60)   # activity: minute currently being observed
        self.act_total = self.act_active = 0    # minutes not yet reported to the server
        self.last_discover = 0.0
        self.polling = False
        self.idle_alert_at = 0.0   # when the last idle (voice) reminder was given
        self.idle_win = None
        if self.conf.get("token") and self.conf.get("server"):
            self.show_main()
            self.refresh()
        else:
            self.show_login()
        self.root.after(1000, self.tick)

    # ---- helpers
    def clear(self):
        if self.frame:
            self.frame.destroy()
        self.frame = tk.Frame(self.root, bg=COLORS["bg"], padx=22, pady=18)
        self.frame.pack(fill="both", expand=True)
        return self.frame

    def label(self, parent, text, size=10, bold=False, fg=None, **kw):
        return tk.Label(parent, text=text, bg=kw.pop("bg", COLORS["bg"]), fg=fg or COLORS["ink"],
                        font=("Segoe UI", size, "bold" if bold else "normal"), **kw)

    def button(self, parent, text, color, cmd):
        return tk.Button(parent, text=text, command=cmd, bg=color, fg="white", activebackground=color,
                         activeforeground="white", disabledforeground="#f2f2f2", relief="flat",
                         font=("Segoe UI", 11, "bold"), cursor="hand2", width=12, pady=10, bd=0)

    def in_background(self, work, done):
        """Run network work off the UI thread, then call done(result, error) on it."""
        def run():
            try:
                res, err = work(), None
            except Exception as e:  # noqa: BLE001 - show any problem to the user
                res, err = None, e
            self.results.put((done, res, err))  # handled on the UI thread by drain()
        threading.Thread(target=run, daemon=True).start()

    def drain(self):
        try:
            while True:
                done, res, err = self.results.get_nowait()
                try:
                    done(res, err)
                except tk.TclError:
                    pass  # window changed while the request was running
        except queue.Empty:
            pass
        self.root.after(100, self.drain)

    # ---- login screen
    def show_login(self, error=""):
        f = self.clear()
        self.label(f, "WorkLog", 18, True, COLORS["brand"]).pack(anchor="w")
        self.label(f, "Log in with the account HR gave you.", 10, fg=COLORS["muted"]).pack(anchor="w", pady=(0, 12))
        entries = {}
        for key, text, show in (("server", "Server address (from HR)", ""), ("username", "Username", ""),
                                ("password", "Password", "•")):
            self.label(f, text, 9, True, COLORS["muted"]).pack(anchor="w")
            e = tk.Entry(f, show=show, font=("Segoe UI", 11), width=32, relief="solid", bd=1)
            e.pack(fill="x", pady=(2, 10), ipady=4)
            entries[key] = e
        known = self.conf.get("server") or default_server()
        entries["server"].insert(0, known or "http://")
        entries["username"].insert(0, self.conf.get("username", ""))
        err = self.label(f, error, 9, fg=COLORS["out"], wraplength=300, justify="left")
        err.pack(anchor="w")

        def fill_found(res, _e):
            if res and entries["server"].winfo_exists() and entries["server"].get().strip() in ("", "http://"):
                entries["server"].delete(0, "end")
                entries["server"].insert(0, res)
        if not known:
            self.in_background(discover, fill_found)

        def attempt_login(server, user, pw):
            """Log in; if the server can't be reached, look for it on the network and retry once."""
            try:
                return server, api(server, "/api/login", payload={"username": user, "password": pw, "host": HOST})
            except ApiError as e:
                if e.code:
                    raise
                found = discover()
                if not found or found == server:
                    raise
                return found, api(found, "/api/login", payload={"username": user, "password": pw, "host": HOST})

        def submit(_evt=None):
            server = entries["server"].get().strip().rstrip("/")
            if server and not server.startswith("http"):
                server = "http://" + server
            user, pw = entries["username"].get().strip(), entries["password"].get()
            if not (server and user and pw) or server == "http:/":
                err.config(text="Please fill in all fields.")
                return
            btn.config(state="disabled", text="Logging in…")

            def done(out, e):
                if e:
                    err.config(text=str(e))
                    btn.config(state="normal", text="Log in")
                    return
                server_used, res = out
                self.conf.update(server=server_used, username=user, token=res["token"], name=res["full_name"])
                save_conf(self.conf)
                if "autostart" not in self.conf:
                    self.conf["autostart"] = True
                    try:
                        set_autostart(True)
                    except OSError:
                        pass
                    save_conf(self.conf)
                self.show_main()
                self.refresh()
            self.in_background(lambda: attempt_login(server, user, pw), done)

        btn = self.button(f, "Log in", COLORS["brand"], submit)
        btn.config(width=30)
        btn.pack(fill="x", pady=(8, 0))
        entries["password"].bind("<Return>", submit)
        (entries["username"] if self.conf.get("server") else entries["server"]).focus_set()

    # ---- main screen
    def show_main(self):
        f = self.clear()
        top = tk.Frame(f, bg=COLORS["bg"])
        top.pack(fill="x")
        self.lbl_company = self.label(top, "WorkLog", 9, True, COLORS["brand"])
        self.lbl_company.pack(anchor="w")
        self.label(top, self.conf.get("name", ""), 14, True).pack(anchor="w")

        card = tk.Frame(f, bg=COLORS["card"], padx=16, pady=14, highlightthickness=1, highlightbackground="#e5e7f0")
        card.pack(fill="x", pady=12)
        self.lbl_status = self.label(card, "Connecting…", 11, True, COLORS["muted"], bg=COLORS["card"])
        self.lbl_status.pack()
        self.lbl_clock = self.label(card, "--:--:--", 26, True, bg=COLORS["card"])
        self.lbl_clock.pack()
        self.lbl_totals = self.label(card, "", 9, fg=COLORS["muted"], bg=COLORS["card"])
        self.lbl_totals.pack()

        grid = tk.Frame(f, bg=COLORS["bg"])
        grid.pack()
        self.btns = {
            "time_in": self.button(grid, "Time In", COLORS["in"], lambda: self.punch("time_in")),
            "time_out": self.button(grid, "Time Out", COLORS["out"], lambda: self.punch("time_out")),
            "break_start": self.button(grid, "Start Break", COLORS["brk"], lambda: self.punch("break_start")),
            "break_end": self.button(grid, "End Break", COLORS["back"], lambda: self.punch("break_end")),
        }
        for i, k in enumerate(("time_in", "time_out", "break_start", "break_end")):
            self.btns[k].grid(row=i // 2, column=i % 2, padx=5, pady=5)

        self.lbl_msg = self.label(f, "", 9, fg=COLORS["muted"], wraplength=300, justify="center")
        self.lbl_msg.pack(pady=(6, 0))
        self.label(f, "Notice: while you are timed in (not on break), this PC sends\nscreenshots and "
                      "active/idle time (not what you type) to HR,\nand speaks a reminder "
                      "if the PC is not used for a while.", 8, fg=COLORS["muted"],
                   justify="center").pack(pady=(8, 4))

        bottom = tk.Frame(f, bg=COLORS["bg"])
        bottom.pack(fill="x", pady=(6, 0))
        self.var_auto = tk.BooleanVar(value=os.path.exists(STARTUP_FILE))
        tk.Checkbutton(bottom, text="Start with Windows", variable=self.var_auto, command=self.toggle_autostart,
                       bg=COLORS["bg"], font=("Segoe UI", 9), activebackground=COLORS["bg"]).pack(side="left")
        for text, cmd in (("Log out", self.logout), ("Website", self.open_site)):
            lk = self.label(bottom, text, 9, fg=COLORS["brand"], cursor="hand2")
            lk.pack(side="right", padx=4)
            lk.bind("<Button-1>", lambda _e, c=cmd: c())
        self.render()

    def render(self):
        if not self.state or not hasattr(self, "btns"):
            return
        st = self.state["status"]
        text, color = STATUS_TEXT.get(st, ("", COLORS["muted"]))
        since = self.state.get("since")
        if since and st != "out":
            text += f"  since {datetime.strptime(since, '%Y-%m-%d %H:%M:%S').strftime('%I:%M %p')}"
        self.lbl_status.config(text=text, fg=color)
        self.lbl_company.config(text=self.state.get("company_name", "WorkLog"))
        allowed = {"time_in": st == "out", "time_out": st != "out",
                   "break_start": st == "working", "break_end": st == "break"}
        for k, b in self.btns.items():
            b.config(state="normal" if allowed[k] and not self.busy else "disabled",
                     bg=b.cget("activebackground") if allowed[k] else COLORS["off"])

    def tick(self):
        """Every second: update the clock; every minute: heartbeat; screenshots; reminders."""
        try:
            if self.state and hasattr(self, "lbl_clock") and self.lbl_clock.winfo_exists():
                el = int(time.time() - self.state_at)
                work = self.state["worked_seconds"] + (el if self.state["status"] == "working" else 0)
                brk = self.state["break_seconds"] + (el if self.state["status"] == "break" else 0)
                self.lbl_clock.config(text=time.strftime("%I:%M:%S %p"))
                self.lbl_totals.config(text=f"Worked today {work // 3600}h {work % 3600 // 60:02d}m   ·   "
                                            f"Break {brk // 3600}h {brk % 3600 // 60:02d}m")
            self.track_activity()
            if self.conf.get("token"):
                if time.time() - self.state_at >= POLL_SECONDS and not self.polling:
                    self.refresh()
                self.maybe_screenshot()
                self.maybe_remind()
                self.maybe_idle_alert()
        except Exception:  # noqa: BLE001 - never let the timer die
            pass
        finally:
            self.root.after(1000, self.tick)

    def track_activity(self):
        """At the end of each minute while working: was the keyboard/mouse used in that minute?"""
        m = int(time.time() // 60)
        if m == self.cur_min:
            return
        gap = m - self.cur_min
        self.cur_min = m
        if gap > 2:  # PC was asleep/hibernating: don't count those minutes
            return
        if self.state and self.state.get("status") == "working":
            self.act_total = min(30, self.act_total + 1)
            if idle_seconds() < 60:
                self.act_active = min(self.act_total, self.act_active + 1)

    def refresh(self):
        """Heartbeat: report activity, get the latest status (every minute)."""
        server, token = self.conf.get("server"), self.conf.get("token")
        total, active = self.act_total, self.act_active
        self.polling = True

        def work():
            return api(server, "/api/heartbeat", token, payload={
                "idle_seconds": int(idle_seconds()), "minutes_total": total, "minutes_active": active})

        def done(res, err):
            self.polling = False
            if err:
                self.state_at = time.time() - POLL_SECONDS + 15  # try again in 15 s
                if isinstance(err, ApiError) and err.code == 401:
                    self.force_login("Your session ended. Please log in again.")
                    return
                if hasattr(self, "lbl_msg") and self.lbl_msg.winfo_exists():
                    self.lbl_msg.config(text=str(err), fg=COLORS["out"])
                if isinstance(err, ApiError) and not err.code:
                    self.try_rediscover(server)
                return
            self.act_total -= total
            self.act_active = max(0, self.act_active - active)
            self.set_state(res)
            if hasattr(self, "lbl_msg") and self.lbl_msg.winfo_exists() and self.lbl_msg.cget("fg") == COLORS["out"]:
                self.lbl_msg.config(text="", fg=COLORS["muted"])
        self.in_background(work, done)

    def try_rediscover(self, current):
        """Server unreachable: maybe its IP changed. Look for it on the network (at most once a minute)."""
        if time.time() - self.last_discover < 60:
            return
        self.last_discover = time.time()

        def done(found, _err):
            if found and found != current and self.conf.get("token"):
                self.conf["server"] = found
                save_conf(self.conf)
                self.refresh()
        self.in_background(discover, done)

    def set_state(self, res):
        self.state, self.state_at = res, time.time()
        self.render()

    def punch(self, action):
        if action == "time_out" and not messagebox.askyesno(APP_NAME, "Time out for today?", parent=self.root):
            return
        self.busy = True
        self.render()

        def done(res, err):
            self.busy = False
            if err:
                if isinstance(err, ApiError) and err.body.get("status"):
                    self.set_state(err.body)
                if isinstance(err, ApiError) and err.code == 401:
                    return self.force_login("Your session ended. Please log in again.")
                if isinstance(err, ApiError) and not err.code:
                    self.last_discover = 0
                    self.try_rediscover(self.conf.get("server"))
                    err = "Cannot reach the WorkLog server. Searching the network… please try again in a moment."
                self.lbl_msg.config(text=str(err), fg=COLORS["out"])
                self.render()
                return
            self.set_state(res)
            self.lbl_msg.config(text=res.get("message", ""), fg=COLORS["in"])
            if action in ("time_in", "break_end"):
                self.last_shot = time.time() - self.interval() + 60  # first screenshot ~1 min later
        self.in_background(lambda: api(self.conf["server"], "/api/punch", self.conf["token"],
                                       payload={"action": action, "source": f"agent ({HOST})"}), done)

    def interval(self):
        return max(1, int((self.state or {}).get("screenshot_interval_minutes", 10))) * 60

    def maybe_screenshot(self):
        if not self.state or self.state["status"] != "working":
            return
        if time.time() - self.last_shot < self.interval():
            return
        self.last_shot = time.time()
        server, token = self.conf["server"], self.conf["token"]

        def work():
            return api(server, "/api/screenshot", token, raw=capture_png(), content_type="image/png", timeout=60)

        def done(_res, err):
            if err and hasattr(self, "lbl_msg") and self.lbl_msg.winfo_exists():
                if isinstance(err, ApiError) and err.code == 409:
                    self.refresh()  # status changed on another device
                else:
                    self.last_shot = time.time() - self.interval() + 120  # retry in 2 minutes
        self.in_background(work, done)

    def maybe_remind(self):
        if not self.state or time.time() - self.last_remind < REMIND_EVERY_MIN * 60:
            return
        now = datetime.now()
        hm = now.strftime("%H:%M")
        st = self.state["status"]
        msg = None
        if now.weekday() in self.state.get("work_days", [0, 1, 2, 3, 4]):
            start, end = self.state.get("work_start", "08:00"), self.state.get("work_end", "17:00")
            if st == "out" and start <= hm < end and not self.state.get("timed_in_today"):
                msg = "Good day! Please TIME IN to start your work day."
        if st == "working" and hm >= self.state.get("work_end", "17:00"):
            msg = "Work hours are over. Don't forget to TIME OUT before you leave."
        if st == "break" and self.state.get("since"):
            mins = (now - datetime.strptime(self.state["since"], "%Y-%m-%d %H:%M:%S")).total_seconds() / 60
            if mins >= max(15, int(self.state.get("break_allowance_minutes", 60))):
                msg = f"You have been on break for {int(mins)} minutes. Click END BREAK when you are back."
        if msg:
            self.last_remind = time.time()
            self.root.deiconify()
            self.root.lift()
            self.root.attributes("-topmost", True)
            self.root.after(500, lambda: self.root.attributes("-topmost", False))
            # shown from a separate callback so the 1-second timer (heartbeat, screenshots) keeps running
            self.root.after(10, lambda: messagebox.showinfo(APP_NAME, msg, parent=self.root))

    def maybe_idle_alert(self):
        """Timed in but no keyboard/mouse use for N minutes: speak a reminder and show it on
        screen; repeat every N minutes until the employee is back."""
        st = self.state
        if not st or st.get("status") != "working":
            self.idle_alert_at = 0
            self.close_idle_win()
            return
        limit = int(st.get("idle_alert_minutes", 5) or 0)
        idle = idle_seconds()
        if limit <= 0 or idle < 30:  # feature off, or the employee is using the PC again
            self.idle_alert_at = 0
            self.close_idle_win()
            return
        if idle < limit * 60 or (self.idle_alert_at and time.time() - self.idle_alert_at < limit * 60):
            return
        self.idle_alert_at = time.time()
        first = (self.conf.get("name") or "").split(" ")[0]
        text = idle_message(st.get("idle_alert_message"), first, int(idle // 60))
        try:
            speak(text)
        except OSError:
            pass
        self.show_idle_win(text)
        server, token = self.conf.get("server"), self.conf.get("token")
        self.in_background(lambda: api(server, "/api/idle_alert", token, payload={}), lambda _r, _e: None)

    def show_idle_win(self, text):
        self.close_idle_win()
        w = tk.Toplevel(self.root, bg="#fff3dc", padx=24, pady=20)
        w.title("WorkLog reminder")
        w.attributes("-topmost", True)
        w.resizable(False, False)
        tk.Label(w, text="🔊  Are you still there?", bg="#fff3dc", fg="#b26b00",
                 font=("Segoe UI", 16, "bold")).pack(anchor="w")
        tk.Label(w, text=text, bg="#fff3dc", fg=COLORS["ink"], font=("Segoe UI", 11),
                 wraplength=380, justify="left").pack(anchor="w", pady=(8, 14))
        row = tk.Frame(w, bg="#fff3dc")
        row.pack(fill="x")
        tk.Button(row, text="I'm back", command=self.close_idle_win, bg=COLORS["in"], fg="white",
                  relief="flat", font=("Segoe UI", 11, "bold"), padx=16, pady=6, bd=0,
                  cursor="hand2").pack(side="left")

        def go_break():
            self.close_idle_win()
            self.root.deiconify()
            self.punch("break_start")
        tk.Button(row, text="Start Break", command=go_break, bg=COLORS["brk"], fg="white",
                  relief="flat", font=("Segoe UI", 11, "bold"), padx=16, pady=6, bd=0,
                  cursor="hand2").pack(side="left", padx=8)
        w.update_idletasks()
        x = (w.winfo_screenwidth() - w.winfo_reqwidth()) // 2
        y = (w.winfo_screenheight() - w.winfo_reqheight()) // 3
        w.geometry(f"+{x}+{y}")
        w.protocol("WM_DELETE_WINDOW", self.close_idle_win)
        self.idle_win = w

    def close_idle_win(self):
        if self.idle_win is not None:
            try:
                self.idle_win.destroy()
            except tk.TclError:
                pass
            self.idle_win = None

    def toggle_autostart(self):
        try:
            set_autostart(self.var_auto.get())
            self.conf["autostart"] = self.var_auto.get()
            save_conf(self.conf)
        except OSError as e:
            messagebox.showerror(APP_NAME, f"Could not change startup setting: {e}", parent=self.root)

    def open_site(self):
        webbrowser.open(self.conf.get("server", ""))

    def force_login(self, msg):
        self.conf.pop("token", None)
        save_conf(self.conf)
        self.state = None
        self.show_login(msg)

    def logout(self):
        if self.state and self.state["status"] != "out":
            if not messagebox.askyesno(APP_NAME, "You are still timed in. Log out of the agent anyway?\n"
                                       "(Your time keeps running until you Time Out.)", parent=self.root):
                return
        server, token = self.conf.get("server"), self.conf.get("token")
        threading.Thread(target=lambda: self._quiet(lambda: api(server, "/api/logout", token, payload={})),
                         daemon=True).start()
        self.force_login("")

    @staticmethod
    def _quiet(fn):
        try:
            fn()
        except Exception:  # noqa: BLE001
            pass

    def on_close(self):
        if self.conf.get("token"):
            # keep running in the background (taskbar) so reminders and screenshots continue
            self.root.iconify()
        else:
            self.root.destroy()


def single_instance():
    """Prevent two copies of the agent running at once."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateMutexW(None, False, "Global\\WorkLogAgentSingleInstance")
    return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS


if __name__ == "__main__":
    if "--test-screenshot" in sys.argv:
        out = os.path.abspath("test_screenshot.png")
        with open(out, "wb") as fh:
            fh.write(capture_png())
        print("Saved", out)
        sys.exit(0)
    if not single_instance():
        ctypes.windll.user32.MessageBoxW(None, "WorkLog Agent is already running (check the taskbar).", APP_NAME, 0x40)
        sys.exit(0)
    Agent().root.mainloop()
