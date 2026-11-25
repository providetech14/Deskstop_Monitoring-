"""Runs the WorkLog server hidden in the background (no window) and restarts it
automatically if it ever stops unexpectedly. Started at Windows logon."""
import os
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("WORKLOG_PORT", "5000"))
LOG_DIR = os.path.join(os.environ.get("WORKLOG_DATA") or os.path.join(HERE, "data"), "logs")
PID_FILE = os.path.join(os.path.dirname(LOG_DIR), "server.pid")
os.makedirs(LOG_DIR, exist_ok=True)


def port_in_use():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def note(msg):
    with open(os.path.join(LOG_DIR, "runner.log"), "a", encoding="utf-8") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")


if port_in_use():
    note(f"Port {PORT} already in use - WorkLog is probably already running. Exiting.")
    sys.exit(0)

with open(PID_FILE, "w") as f:
    f.write(str(os.getpid()))

python = sys.executable  # pythonw.exe
fast_crashes = 0
while True:
    started = time.time()
    note("Starting WorkLog server")
    with open(os.path.join(LOG_DIR, "server-output.log"), "a", encoding="utf-8") as out:
        proc = subprocess.Popen([python, "-c", "import app; app.serve_forever()"], cwd=HERE,
                                stdout=out, stderr=subprocess.STDOUT,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        code = proc.wait()
    note(f"Server stopped with exit code {code}")
    fast_crashes = fast_crashes + 1 if time.time() - started < 30 else 0
    time.sleep(3 if fast_crashes <= 1 else min(300, 5 * 2 ** min(fast_crashes, 6)))  # back off if it keeps failing
