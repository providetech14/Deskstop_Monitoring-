"""
WorkLog - Attendance & Work Monitoring Server
---------------------------------------------
Runs on one office PC/laptop (the "server"). Employees open it from any laptop,
computer or phone browser on the office network, and the WorkLog Agent on
each work PC uses it to clock in/out, report activity and upload screenshots.

Start (with console):   python app.py
Start (hidden, auto-restart, used at Windows logon):  pythonw run_server.pyw
"""
import csv
import io
import logging
import logging.handlers
import os
import secrets
import socket
import sqlite3
import threading
import time
from datetime import datetime, date, timedelta
from functools import wraps

from flask import (Flask, abort, flash, g, jsonify, redirect, render_template,
                   request, send_file, session, url_for, Response)
from markupsafe import Markup
from werkzeug.security import check_password_hash, generate_password_hash

APP_VERSION = "1.1"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("WORKLOG_DATA") or os.path.join(BASE_DIR, "data")
SHOT_DIR = os.path.join(DATA_DIR, "screenshots")
BACKUP_DIR = os.path.join(DATA_DIR, "backups")
LOG_DIR = os.path.join(DATA_DIR, "logs")
DB_PATH = os.path.join(DATA_DIR, "worklog.db")
SECRET_PATH = os.path.join(DATA_DIR, "secret.key")
for _d in (SHOT_DIR, BACKUP_DIR, LOG_DIR):
    os.makedirs(_d, exist_ok=True)

PORT = int(os.environ.get("WORKLOG_PORT", "5000"))
DISCOVERY_PORT = int(os.environ.get("WORKLOG_DISCOVERY_PORT", "5001"))
TS_FMT = "%Y-%m-%d %H:%M:%S"
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

log = logging.getLogger("worklog")
if not log.handlers:
    _h = logging.handlers.RotatingFileHandler(os.path.join(LOG_DIR, "server.log"), maxBytes=2_000_000,
                                              backupCount=5, encoding="utf-8")
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    for _name in ("worklog", "waitress"):
        logging.getLogger(_name).addHandler(_h)
        logging.getLogger(_name).setLevel(logging.INFO)

# Punch types and what the employee status becomes after each.
ACTIONS = {
    "time_in":     {"label": "Time In",     "from": ("out",),     "to": "working"},
    "break_start": {"label": "Start Break", "from": ("working",), "to": "break"},
    "break_end":   {"label": "End Break",   "from": ("break",),   "to": "working"},
    "time_out":    {"label": "Time Out",    "from": ("working", "break"), "to": "out"},
}

DEFAULT_SETTINGS = {
    "company_name": "My Company",
    "work_start": "08:00",
    "work_end": "17:00",
    "work_days": "0,1,2,3,4",          # Mon-Fri (0 = Monday)
    "grace_minutes": "15",
    "break_allowance_minutes": "60",
    "screenshot_interval_minutes": "5",
    "screenshot_retention_days": "30",
    "idle_alert_minutes": "1",          # voice reminder after this many idle minutes (0 = off)
    "idle_alert_message": ("Hello {name}. You have not used your computer for {minutes} minutes. "
                           "Please get back to work, or click Start Break if you are on a break."),
}


def load_secret():
    if not os.path.exists(SECRET_PATH):
        with open(SECRET_PATH, "w") as f:
            f.write(secrets.token_hex(32))
    with open(SECRET_PATH) as f:
        return f.read().strip()


app = Flask(__name__)
app.secret_key = load_secret()
app.config.update(
    MAX_CONTENT_LENGTH=20 * 1024 * 1024,
    PERMANENT_SESSION_LIFETIME=timedelta(days=14),
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_HTTPONLY=True,
)


# --------------------------------------------------------------------------- DB
def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=30)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
    PRAGMA journal_mode = WAL;
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL COLLATE NOCASE,
        password_hash TEXT NOT NULL,
        full_name TEXT NOT NULL,
        position TEXT DEFAULT '',
        role TEXT NOT NULL CHECK (role IN ('admin','employee')),
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        last_seen TEXT,
        last_seen_host TEXT
    );
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        type TEXT NOT NULL,
        ts TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT 'web',
        note TEXT DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_events_user_ts ON events(user_id, ts);
    CREATE TABLE IF NOT EXISTS screenshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        ts TEXT NOT NULL,
        path TEXT NOT NULL,
        host TEXT DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_shots_user_ts ON screenshots(user_id, ts);
    CREATE TABLE IF NOT EXISTS tokens (
        token TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        created_at TEXT NOT NULL,
        host TEXT DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS activity (
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        day TEXT NOT NULL,
        total_min INTEGER NOT NULL DEFAULT 0,
        active_min INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (user_id, day)
    );
    """)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
    if "idle_seconds" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN idle_seconds INTEGER")
    if "idle_alerts" not in {r[1] for r in conn.execute("PRAGMA table_info(activity)")}:
        conn.execute("ALTER TABLE activity ADD COLUMN idle_alerts INTEGER NOT NULL DEFAULT 0")
    for k, v in DEFAULT_SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES (?,?)", (k, v))
    conn.commit()
    conn.close()


def now():
    return datetime.now().replace(microsecond=0)


def now_s():
    return now().strftime(TS_FMT)


def parse_ts(s):
    return datetime.strptime(s, TS_FMT)


def settings():
    if "settings" in g:
        return g.settings
    rows = db().execute("SELECT key, value FROM settings").fetchall()
    s = dict(DEFAULT_SETTINGS)
    s.update({r["key"]: r["value"] for r in rows})
    g.settings = s
    return s


def setting_int(s, key):
    try:
        return int(s[key])
    except (KeyError, ValueError):
        return int(DEFAULT_SETTINGS[key])


def work_days(s):
    try:
        return {int(x) for x in s.get("work_days", "").split(",") if x.strip() != ""}
    except ValueError:
        return {0, 1, 2, 3, 4}


def clock_time(day, hhmm, fallback):
    try:
        hh, mm = [int(x) for x in hhmm.split(":")]
    except ValueError:
        hh, mm = [int(x) for x in fallback.split(":")]
    return datetime.combine(day, datetime.min.time()).replace(hour=hh, minute=mm)


# ------------------------------------------------------------- security bits
def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(24)
    return session["csrf"]


@app.template_global()
def csrf():
    return Markup(f'<input type="hidden" name="csrf" value="{csrf_token()}">')


@app.before_request
def check_csrf():
    if request.method == "POST" and not request.path.startswith("/api/"):
        sent = request.form.get("csrf") or request.headers.get("X-CSRF", "")
        if not sent or not secrets.compare_digest(sent, session.get("csrf", "")):
            flash("Your page was out of date. Please try again.", "error")
            ref = request.referrer or ""
            host = request.host_url.rstrip("/")
            return redirect(ref if ref.startswith(host + "/") else url_for("home"))


_fails = {}
_fails_lock = threading.Lock()
MAX_FAILS, FAIL_WINDOW = 8, 600


def too_many_fails(key):
    with _fails_lock:
        recent = [t for t in _fails.get(key, []) if time.time() - t < FAIL_WINDOW]
        _fails[key] = recent
        return len(recent) >= MAX_FAILS


def record_fail(key):
    with _fails_lock:
        _fails.setdefault(key, []).append(time.time())
    log.warning("Failed login for %s", key)


def check_login(username, password):
    """Returns (user, error)."""
    key = f"{request.remote_addr}|{username.lower()}"
    if too_many_fails(key):
        return None, "Too many wrong attempts. Please wait 10 minutes and try again."
    u = db().execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if u and u["active"] and check_password_hash(u["password_hash"], password):
        with _fails_lock:
            _fails.pop(key, None)
        return u, None
    record_fail(key)
    return None, "Wrong username or password."


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    if request.endpoint not in ("static", "screenshot_file"):
        resp.headers.setdefault("Cache-Control", "no-store")
    return resp


@app.context_processor
def inject_globals():
    user = current_user()
    return {"me": user, "cfg": settings(), "ACTIONS": ACTIONS, "DAY_NAMES": DAY_NAMES,
            "fmt_dur": fmt_dur, "today": date.today().isoformat(), "version": APP_VERSION}


# ------------------------------------------------------------------------- auth
def current_user():
    if "cu" in g:
        return g.cu
    uid = session.get("uid")
    g.cu = None
    if uid:
        g.cu = db().execute("SELECT * FROM users WHERE id=? AND active=1", (uid,)).fetchone()
    return g.cu


def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not current_user():
            return redirect(url_for("login", next=request.path))
        return fn(*a, **kw)
    return wrapper


def admin_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        u = current_user()
        if not u:
            return redirect(url_for("login", next=request.path))
        if u["role"] != "admin":
            abort(403)
        return fn(*a, **kw)
    return wrapper


def token_user():
    """User for API calls: Bearer token (agent)."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return db().execute(
            "SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id "
            "WHERE t.token=? AND u.active=1", (auth[7:].strip(),)).fetchone()
    return None


def api_auth(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        u = token_user()
        if not u:
            return jsonify(error="Not logged in"), 401
        g.api_user = u
        return fn(*a, **kw)
    return wrapper


def has_admin():
    return db().execute("SELECT 1 FROM users WHERE role='admin' LIMIT 1").fetchone() is not None


@app.before_request
def first_run_redirect():
    if request.endpoint in ("setup", "static") or request.path.startswith("/api/"):
        return
    if not has_admin():
        return redirect(url_for("setup"))


# ------------------------------------------------------------ attendance logic
def day_events(user_id, day):
    d = day.isoformat()
    return db().execute(
        "SELECT * FROM events WHERE user_id=? AND ts >= ? AND ts < ? ORDER BY ts, id",
        (user_id, d + " 00:00:00", (day + timedelta(days=1)).isoformat() + " 00:00:00"),
    ).fetchall()


def current_status(user_id):
    """Status from the most recent punch. A session left open on a previous day
    (forgot to time out) does not carry over: the employee starts today as 'out'
    and HR sees 'No time out' on that day's record."""
    row = db().execute(
        "SELECT * FROM events WHERE user_id=? ORDER BY ts DESC, id DESC LIMIT 1",
        (user_id,)).fetchone()
    if not row:
        return "out", None
    if row["ts"][:10] < date.today().isoformat():
        return "out", None
    return {"time_in": "working", "break_end": "working",
            "break_start": "break", "time_out": "out"}.get(row["type"], "out"), row


def summarize_day(events, day, s, until=None):
    """Compute worked/break seconds, first in, last out, late flag."""
    work = brk = 0
    state, mark = "out", None
    first_in = last_out = None
    for e in events:
        t = parse_ts(e["ts"])
        typ = e["type"]
        if typ == "time_in":
            if state == "out":
                state, mark = "working", t
                first_in = first_in or t
        elif typ == "break_start" and state == "working":
            work += (t - mark).total_seconds()
            state, mark = "break", t
        elif typ == "break_end" and state == "break":
            brk += (t - mark).total_seconds()
            state, mark = "working", t
        elif typ == "time_out" and state in ("working", "break"):
            if state == "working":
                work += (t - mark).total_seconds()
            else:
                brk += (t - mark).total_seconds()
            state, mark, last_out = "out", None, t
    open_session = state != "out"
    if open_session:
        end = until or now()
        if end.date() != day:
            # Forgot to time out on a past day: count only up to the official end of work.
            end = max(mark, clock_time(day, s["work_end"], DEFAULT_SETTINGS["work_end"]))
        if state == "working":
            work += max(0, (end - mark).total_seconds())
        else:
            brk += max(0, (end - mark).total_seconds())
    late_min = 0
    if first_in:
        start = clock_time(day, s["work_start"], DEFAULT_SETTINGS["work_start"])
        diff = (first_in - start).total_seconds() / 60
        if diff > setting_int(s, "grace_minutes"):
            late_min = int(diff)
    over_break = max(0, int(brk / 60) - setting_int(s, "break_allowance_minutes"))
    return {
        "work": int(work), "break": int(brk), "first_in": first_in, "last_out": last_out,
        "open": open_session, "state": state, "late_min": late_min, "over_break": over_break,
    }


def activity_pct(user_id, day):
    r = db().execute("SELECT total_min, active_min FROM activity WHERE user_id=? AND day=?",
                     (user_id, day.isoformat())).fetchone()
    if not r or not r["total_min"]:
        return None
    return round(100 * r["active_min"] / r["total_min"])


def idle_alerts(user_id, day):
    r = db().execute("SELECT idle_alerts FROM activity WHERE user_id=? AND day=?",
                     (user_id, day.isoformat())).fetchone()
    return r["idle_alerts"] if r else 0


@app.template_filter("mins")
def fmt_mins(m):
    m = int(m or 0)
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m}m"


def fmt_dur(seconds):
    seconds = int(seconds or 0)
    h, rem = divmod(seconds, 3600)
    return f"{h}h {rem // 60:02d}m"


def do_punch(user, action, source, note=""):
    if action not in ACTIONS:
        return False, "Unknown action."
    status, _ = current_status(user["id"])
    if status not in ACTIONS[action]["from"]:
        nice = {"out": "not timed in", "working": "working", "break": "on break"}[status]
        return False, f"You can't {ACTIONS[action]['label'].lower()} right now (you are {nice})."
    db().execute("INSERT INTO events(user_id,type,ts,source,note) VALUES (?,?,?,?,?)",
                 (user["id"], action, now_s(), source[:60], note[:200]))
    db().commit()
    log.info("%s: %s via %s", user["username"], action, source)
    return True, f"{ACTIONS[action]['label']} recorded at {now().strftime('%I:%M %p')}."


def touch_seen(user_id, host="", idle=None):
    db().execute("UPDATE users SET last_seen=?, last_seen_host=COALESCE(NULLIF(?,''),last_seen_host), "
                 "idle_seconds=COALESCE(?, idle_seconds) WHERE id=?", (now_s(), host, idle, user_id))
    db().commit()


# --------------------------------------------------------------- web: general
@app.route("/setup", methods=["GET", "POST"])
def setup():
    if has_admin():
        return redirect(url_for("login"))
    if request.method == "POST":
        f = request.form
        err = validate_user_form(f, new=True)
        if err:
            flash(err, "error")
        else:
            db().execute("UPDATE settings SET value=? WHERE key='company_name'",
                         (f.get("company_name", "").strip() or "My Company",))
            create_user(f, role="admin")
            flash("HR Admin account created. Please log in.", "ok")
            return redirect(url_for("login"))
    return render_template("setup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u, err = check_login(request.form.get("username", "").strip(), request.form.get("password", ""))
        if u:
            session.clear()
            session.permanent = True
            session["uid"] = u["id"]
            nxt = request.args.get("next", "")
            if not nxt.startswith("/") or nxt.startswith("//"):
                nxt = url_for("home")
            return redirect(nxt)
        flash(err, "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def home():
    if current_user()["role"] == "admin":
        return redirect(url_for("admin_dashboard"))
    return redirect(url_for("my_day"))


@app.route("/me", methods=["GET"])
@login_required
def my_day():
    u = current_user()
    s = settings()
    status, last = current_status(u["id"])
    today = date.today()
    evs = day_events(u["id"], today)
    summ = summarize_day(evs, today, s)
    hist = []
    for i in range(1, 8):
        d = today - timedelta(days=i)
        e = day_events(u["id"], d)
        if e:
            hist.append((d, summarize_day(e, d, s)))
    return render_template("me.html", status=status, last=last, events=evs, summ=summ, hist=hist)


@app.route("/me/punch", methods=["POST"])
@login_required
def my_punch():
    src = "phone" if is_mobile() else "web"
    ok, msg = do_punch(current_user(), request.form.get("action", ""), src, request.form.get("note", ""))
    flash(msg, "ok" if ok else "error")
    return redirect(url_for("my_day"))


def is_mobile():
    ua = request.headers.get("User-Agent", "").lower()
    return any(k in ua for k in ("android", "iphone", "ipad", "mobile"))


@app.route("/account", methods=["GET", "POST"])
@login_required
def account():
    if request.method == "POST":
        u = current_user()
        if not check_password_hash(u["password_hash"], request.form.get("current", "")):
            flash("Current password is wrong.", "error")
        elif len(request.form.get("new", "")) < 6:
            flash("New password must be at least 6 characters.", "error")
        elif request.form.get("new") != request.form.get("confirm"):
            flash("New passwords do not match.", "error")
        else:
            db().execute("UPDATE users SET password_hash=? WHERE id=?",
                         (generate_password_hash(request.form["new"]), u["id"]))
            db().commit()
            flash("Password changed.", "ok")
            return redirect(url_for("home"))
    return render_template("account.html")


# ----------------------------------------------------------------- web: admin
@app.route("/admin")
@admin_required
def admin_dashboard():
    s = settings()
    today = date.today()
    rows = []
    counts = {"working": 0, "break": 0, "out": 0, "absent": 0}
    for u in db().execute("SELECT * FROM users WHERE role='employee' AND active=1 ORDER BY full_name"):
        status, last = current_status(u["id"])
        evs = day_events(u["id"], today)
        summ = summarize_day(evs, today, s)
        shot = db().execute("SELECT * FROM screenshots WHERE user_id=? ORDER BY ts DESC LIMIT 1",
                            (u["id"],)).fetchone()
        display = status
        if status == "out" and not evs:
            display = "absent"
        counts[display] += 1
        agent_online = False
        if u["last_seen"]:
            agent_online = (now() - parse_ts(u["last_seen"])).total_seconds() < 180
        idle_min = None
        if agent_online and status == "working" and (u["idle_seconds"] or 0) >= 300:
            idle_min = u["idle_seconds"] // 60
        rows.append({"u": u, "status": display, "last": last, "summ": summ, "shot": shot,
                     "agent_online": agent_online, "idle_min": idle_min,
                     "activity": activity_pct(u["id"], today), "alerts": idle_alerts(u["id"], today)})
    return render_template("admin_dashboard.html", rows=rows, counts=counts,
                           workday=today.weekday() in work_days(s))


@app.route("/admin/employees", methods=["GET", "POST"])
@admin_required
def admin_employees():
    if request.method == "POST":
        f = request.form
        err = validate_user_form(f, new=True)
        if err:
            flash(err, "error")
        else:
            create_user(f, role=f.get("role", "employee"))
            flash(f"Account for {f['full_name'].strip()} created. Username: {f['username'].strip()}", "ok")
            return redirect(url_for("admin_employees"))
    users = db().execute("SELECT * FROM users ORDER BY active DESC, role, full_name").fetchall()
    return render_template("admin_employees.html", users=users, form=request.form)


@app.route("/admin/employees/<int:uid>", methods=["GET", "POST"])
@admin_required
def admin_edit_employee(uid):
    u = db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone() or abort(404)
    if request.method == "POST":
        f = request.form
        act = f.get("do")
        if act == "save":
            err = validate_user_form(f, new=False, uid=uid)
            if err:
                flash(err, "error")
            else:
                role = f.get("role", u["role"])
                if u["id"] == current_user()["id"]:
                    role = "admin"  # can't demote yourself
                db().execute("UPDATE users SET full_name=?, position=?, username=?, role=? WHERE id=?",
                             (f["full_name"].strip(), f.get("position", "").strip(),
                              f["username"].strip(), role, uid))
                if f.get("password"):
                    db().execute("UPDATE users SET password_hash=? WHERE id=?",
                                 (generate_password_hash(f["password"]), uid))
                    db().execute("DELETE FROM tokens WHERE user_id=?", (uid,))
                db().commit()
                flash("Saved.", "ok")
                return redirect(url_for("admin_employees"))
        elif act == "toggle":
            if uid == current_user()["id"]:
                flash("You can't deactivate your own account.", "error")
            else:
                db().execute("UPDATE users SET active = 1 - active WHERE id=?", (uid,))
                db().execute("DELETE FROM tokens WHERE user_id=?", (uid,))
                db().commit()
                flash("Account status changed.", "ok")
            return redirect(url_for("admin_employees"))
    return render_template("admin_edit_employee.html", u=u)


def validate_user_form(f, new, uid=None):
    if not f.get("full_name", "").strip():
        return "Full name is required."
    un = f.get("username", "").strip()
    if not un or " " in un:
        return "Username is required and cannot contain spaces."
    exists = db().execute("SELECT id FROM users WHERE username=?", (un,)).fetchone()
    if exists and exists["id"] != uid:
        return f"Username '{un}' is already taken."
    pw = f.get("password", "")
    if new or pw:
        if len(pw) < 6:
            return "Password must be at least 6 characters."
        if "confirm" in f and pw != f.get("confirm"):
            return "Passwords do not match."
    return None


def create_user(f, role):
    role = role if role in ("admin", "employee") else "employee"
    db().execute(
        "INSERT INTO users(username,password_hash,full_name,position,role,created_at) VALUES (?,?,?,?,?,?)",
        (f["username"].strip(), generate_password_hash(f["password"]), f["full_name"].strip(),
         f.get("position", "").strip(), role, now_s()))
    db().commit()
    log.info("Account created: %s (%s)", f["username"].strip(), role)


def parse_date(s, default):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return default


def build_report(d1, d2, uid=None):
    s = settings()
    q = "SELECT * FROM users WHERE role='employee'"
    args = []
    if uid:
        q += " AND id=?"
        args.append(uid)
    users = db().execute(q + " ORDER BY full_name", args).fetchall()
    out = []
    d = d1
    while d <= d2:
        for u in users:
            evs = day_events(u["id"], d)
            if not evs:
                continue
            out.append({"day": d, "u": u, "events": evs, "summ": summarize_day(evs, d, s),
                        "activity": activity_pct(u["id"], d), "alerts": idle_alerts(u["id"], d)})
        d += timedelta(days=1)
    return out, users


def count_workdays(d1, d2, s, since=None):
    days = work_days(s)
    end = min(d2, date.today())
    d, n = max(d1, since or d1), 0
    while d <= end:
        n += d.weekday() in days
        d += timedelta(days=1)
    return n


@app.route("/admin/attendance")
@admin_required
def admin_attendance():
    today = date.today()
    s = settings()
    d1 = parse_date(request.args.get("from"), today)
    d2 = parse_date(request.args.get("to"), today)
    if d2 < d1:
        d1, d2 = d2, d1
    uid = request.args.get("user", type=int)
    rows, report_users = build_report(d1, d2, uid)
    totals = {}
    for u in report_users:
        if u["active"]:
            totals[u["id"]] = {"u": u, "days": 0, "work": 0, "break": 0, "late": 0, "missing_out": 0,
                               "workdays": count_workdays(d1, d2, s, parse_ts(u["created_at"]).date())}
    for r in rows:
        t = totals.setdefault(r["u"]["id"], {"u": r["u"], "days": 0, "work": 0, "break": 0, "late": 0,
                                             "missing_out": 0, "workdays": 0})
        t["days"] += 1
        t["work"] += r["summ"]["work"]
        t["break"] += r["summ"]["break"]
        t["late"] += 1 if r["summ"]["late_min"] else 0
        t["missing_out"] += 1 if (r["summ"]["open"] and r["day"] < today) else 0
    for t in totals.values():
        t["absent"] = max(0, t["workdays"] - t["days"])
    users = db().execute("SELECT * FROM users WHERE role='employee' ORDER BY full_name").fetchall()
    return render_template("admin_attendance.html", rows=rows, totals=list(totals.values()),
                           users=users, d1=d1, d2=d2, uid=uid)


@app.route("/admin/attendance.csv")
@admin_required
def admin_attendance_csv():
    today = date.today()
    d1 = parse_date(request.args.get("from"), today)
    d2 = parse_date(request.args.get("to"), today)
    if d2 < d1:
        d1, d2 = d2, d1
    uid = request.args.get("user", type=int)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Date", "Employee", "Position", "Time In", "Time Out", "Worked (hours)",
                "Break (minutes)", "Late (minutes)", "Over break (minutes)", "Activity %", "Idle alerts", "Punches"])
    rows, _ = build_report(d1, d2, uid)
    for r in rows:
        sm = r["summ"]
        w.writerow([
            r["day"].isoformat(), r["u"]["full_name"], r["u"]["position"],
            sm["first_in"].strftime("%I:%M %p") if sm["first_in"] else "",
            sm["last_out"].strftime("%I:%M %p") if sm["last_out"] and not sm["open"] else ("(no time out)" if sm["open"] else ""),
            round(sm["work"] / 3600, 2), round(sm["break"] / 60), sm["late_min"], sm["over_break"],
            "" if r["activity"] is None else r["activity"], r["alerts"],
            "; ".join(f"{ACTIONS.get(e['type'], {'label': e['type']})['label']} {e['ts'][11:16]}" for e in r["events"]),
        ])
    name = f"attendance_{d1.isoformat()}_to_{d2.isoformat()}.csv"
    return Response("﻿" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={name}"})


@app.route("/admin/events", methods=["POST"])
@admin_required
def admin_events():
    """Manual corrections: add a punch for an employee, or delete one."""
    f = request.form
    back = f.get("back") or url_for("admin_attendance")
    if not back.startswith("/") or back.startswith("//"):
        back = url_for("admin_attendance")
    if f.get("do") == "delete":
        ev = db().execute("SELECT * FROM events WHERE id=?", (f.get("event_id", type=int),)).fetchone()
        if ev:
            db().execute("DELETE FROM events WHERE id=?", (ev["id"],))
            db().commit()
            log.info("%s deleted event %s (%s %s for user %s)", current_user()["username"],
                     ev["id"], ev["type"], ev["ts"], ev["user_id"])
        flash("Entry deleted.", "ok")
    else:
        uid = f.get("user_id", type=int)
        typ = f.get("type")
        try:
            ts = datetime.strptime(f"{f.get('date')} {f.get('time')}", "%Y-%m-%d %H:%M")
        except ValueError:
            flash("Please enter a valid date and time.", "error")
            return redirect(back)
        if typ not in ACTIONS or not uid:
            flash("Please choose an employee and an entry type.", "error")
            return redirect(back)
        db().execute("INSERT INTO events(user_id,type,ts,source,note) VALUES (?,?,?,?,?)",
                     (uid, typ, ts.strftime(TS_FMT), "hr-correction",
                      f"Added by {current_user()['full_name']}: {f.get('note', '')}"[:200]))
        db().commit()
        log.info("%s added %s %s for user %s", current_user()["username"], typ, ts, uid)
        flash("Entry added.", "ok")
    return redirect(back)


@app.route("/admin/screenshots")
@admin_required
def admin_screenshots():
    day = parse_date(request.args.get("date"), date.today())
    uid = request.args.get("user", type=int)
    q = "SELECT s.*, u.full_name FROM screenshots s JOIN users u ON u.id=s.user_id WHERE s.ts >= ? AND s.ts < ?"
    args = [day.isoformat() + " 00:00:00", (day + timedelta(days=1)).isoformat() + " 00:00:00"]
    if uid:
        q += " AND s.user_id=?"
        args.append(uid)
    shots = db().execute(q + " ORDER BY s.ts DESC", args).fetchall()
    users = db().execute("SELECT * FROM users WHERE role='employee' ORDER BY full_name").fetchall()
    return render_template("admin_screenshots.html", shots=shots, users=users, day=day, uid=uid)


@app.route("/admin/screenshots/<int:sid>")
@admin_required
def screenshot_file(sid):
    row = db().execute("SELECT * FROM screenshots WHERE id=?", (sid,)).fetchone() or abort(404)
    full = os.path.normpath(os.path.join(SHOT_DIR, row["path"]))
    if not full.startswith(os.path.normpath(SHOT_DIR)) or not os.path.exists(full):
        abort(404)
    return send_file(full, mimetype="image/png", max_age=86400)


@app.route("/admin/settings", methods=["GET", "POST"])
@admin_required
def admin_settings():
    if request.method == "POST":
        for k in DEFAULT_SETTINGS:
            if k == "work_days":
                v = ",".join(sorted(x for x in set(request.form.getlist("work_days")) if x in list("0123456")))
                if not v:
                    flash("Please tick at least one work day.", "error")
                    return redirect(url_for("admin_settings"))
                db().execute("UPDATE settings SET value=? WHERE key=?", (v, k))
                continue
            if k not in request.form:
                continue
            v = request.form[k].strip()
            if k == "idle_alert_message":
                v = " ".join(v.split())[:300] or DEFAULT_SETTINGS[k]
            elif k.endswith(("_minutes", "_days")):
                try:
                    v = str(max(1 if k == "screenshot_interval_minutes" else 0, int(v)))
                except ValueError:
                    flash(f"'{k}' must be a number.", "error")
                    return redirect(url_for("admin_settings"))
            elif k in ("work_start", "work_end"):
                try:
                    datetime.strptime(v, "%H:%M")
                except ValueError:
                    flash("Please enter valid work start/end times.", "error")
                    return redirect(url_for("admin_settings"))
            db().execute("UPDATE settings SET value=? WHERE key=?", (v, k))
        db().commit()
        flash("Settings saved.", "ok")
        return redirect(url_for("admin_settings"))
    urls = server_urls()
    return render_template("admin_settings.html", server_urls=urls, qr=qr_svg(urls[0]),
                           backups=list_backups(), data_dir=DATA_DIR)


def qr_svg(text):
    try:
        import qrcode
        import qrcode.image.svg
        svg = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, border=2).to_string(encoding="unicode")
        return Markup(svg.replace('width="', 'data-w="', 1).replace('height="', 'data-h="', 1))
    except Exception:  # noqa: BLE001 - QR is optional
        return None


def primary_ip():
    """IP of the network card used for the default route (no packets are sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def server_urls():
    ips = []
    p = primary_ip()
    if p and not p.startswith("127."):
        ips.append(p)
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if not ip.startswith(("127.", "169.254.")) and ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    return [f"http://{ip}:{PORT}" for ip in ips] or [f"http://localhost:{PORT}"]


# --------------------------------------------------------- API (agent/mobile)
@app.route("/api/ping")
def api_ping():
    return jsonify(app="worklog", version=APP_VERSION)


@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json(silent=True) or {}
    u, err = check_login(str(data.get("username", "")).strip(), str(data.get("password", "")))
    if not u:
        return jsonify(error=err), 401
    tok = secrets.token_urlsafe(32)
    db().execute("INSERT INTO tokens(token,user_id,created_at,host) VALUES (?,?,?,?)",
                 (tok, u["id"], now_s(), str(data.get("host", ""))[:100]))
    db().commit()
    log.info("Agent login: %s from %s", u["username"], data.get("host", ""))
    return jsonify(token=tok, full_name=u["full_name"], role=u["role"])


@app.route("/api/logout", methods=["POST"])
@api_auth
def api_logout():
    db().execute("DELETE FROM tokens WHERE token=?", (request.headers["Authorization"][7:].strip(),))
    db().commit()
    return jsonify(ok=True)


def status_payload(u):
    s = settings()
    status, last = current_status(u["id"])
    today = date.today()
    summ = summarize_day(day_events(u["id"], today), today, s)
    return {
        "full_name": u["full_name"], "status": status,
        "since": last["ts"] if last else None,
        "worked_seconds": summ["work"], "break_seconds": summ["break"],
        "timed_in_today": summ["first_in"] is not None,
        "company_name": s["company_name"], "work_start": s["work_start"],
        "work_end": s["work_end"], "work_days": sorted(work_days(s)),
        "break_allowance_minutes": setting_int(s, "break_allowance_minutes"),
        "screenshot_interval_minutes": setting_int(s, "screenshot_interval_minutes"),
        "idle_alert_minutes": setting_int(s, "idle_alert_minutes"),
        "idle_alert_message": s.get("idle_alert_message") or DEFAULT_SETTINGS["idle_alert_message"],
        "server_time": now_s(),
    }


@app.route("/api/idle_alert", methods=["POST"])
@api_auth
def api_idle_alert():
    """Agent tells us it just gave the employee an idle (voice) reminder."""
    u = g.api_user
    status, _ = current_status(u["id"])
    if status == "working":
        db().execute("INSERT INTO activity(user_id, day, idle_alerts) VALUES (?,?,1) "
                     "ON CONFLICT(user_id, day) DO UPDATE SET idle_alerts=idle_alerts+1",
                     (u["id"], date.today().isoformat()))
        db().commit()
        log.info("Idle reminder given to %s", u["username"])
    return jsonify(ok=True)


@app.route("/api/status")
@api_auth
def api_status():
    touch_seen(g.api_user["id"], request.headers.get("X-Host", "")[:100])
    return jsonify(status_payload(g.api_user))


@app.route("/api/heartbeat", methods=["POST"])
@api_auth
def api_heartbeat():
    """Agent reports every minute: idle time and how many of the last minutes had keyboard/mouse use."""
    u = g.api_user
    data = request.get_json(silent=True) or {}

    def num(key, lo, hi):
        try:
            return max(lo, min(hi, int(data.get(key, 0))))
        except (TypeError, ValueError):
            return 0
    idle = num("idle_seconds", 0, 10 ** 7)
    total, active = num("minutes_total", 0, 30), num("minutes_active", 0, 30)
    active = min(active, total)
    touch_seen(u["id"], request.headers.get("X-Host", "")[:100], idle)
    status, _ = current_status(u["id"])
    if status == "working" and total:
        db().execute("INSERT INTO activity(user_id, day, total_min, active_min) VALUES (?,?,?,?) "
                     "ON CONFLICT(user_id, day) DO UPDATE SET total_min=total_min+excluded.total_min, "
                     "active_min=active_min+excluded.active_min",
                     (u["id"], date.today().isoformat(), total, active))
        db().commit()
    return jsonify(status_payload(u))


@app.route("/api/punch", methods=["POST"])
@api_auth
def api_punch():
    data = request.get_json(silent=True) or {}
    ok, msg = do_punch(g.api_user, str(data.get("action", "")), str(data.get("source", "agent")),
                       str(data.get("note", "")))
    body = status_payload(g.api_user)
    body["message"] = msg
    return jsonify(body), (200 if ok else 409)


@app.route("/api/screenshot", methods=["POST"])
@api_auth
def api_screenshot():
    u = g.api_user
    status, _ = current_status(u["id"])
    if status != "working":
        return jsonify(error="Screenshots are only stored while timed in and not on break."), 409
    raw = request.get_data()
    if not raw.startswith(b"\x89PNG"):
        return jsonify(error="Expected a PNG image."), 400
    t = now()
    rel = os.path.join(str(u["id"]), t.strftime("%Y-%m-%d"), t.strftime("%H%M%S") + ".png")
    full = os.path.join(SHOT_DIR, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "wb") as f:
        f.write(raw)
    host = request.headers.get("X-Host", "")[:100]
    db().execute("INSERT INTO screenshots(user_id,ts,path,host) VALUES (?,?,?,?)",
                 (u["id"], t.strftime(TS_FMT), rel, host))
    db().commit()
    touch_seen(u["id"], host)
    return jsonify(ok=True)


@app.errorhandler(403)
def forbidden(_e):
    return render_template("message.html", title="Not allowed",
                           text="This page is for HR Admin accounts only."), 403


@app.errorhandler(500)
def server_error(e):
    log.exception("Server error on %s: %s", request.path, e)
    if request.path.startswith("/api/"):
        return jsonify(error="Server error"), 500
    return render_template("message.html", title="Something went wrong",
                           text="Please try again. If it keeps happening, restart WorkLog."), 500


# ------------------------------------------------------ background services
def list_backups():
    try:
        return sorted((f for f in os.listdir(BACKUP_DIR) if f.endswith(".db")), reverse=True)
    except OSError:
        return []


def daily_maintenance():
    """Once per day: back up the database (keep 30) and delete old screenshots."""
    try:
        target = os.path.join(BACKUP_DIR, f"worklog-{date.today().isoformat()}.db")
        if not os.path.exists(target):
            src = sqlite3.connect(DB_PATH)
            dst = sqlite3.connect(target)
            with dst:
                src.backup(dst)
            dst.close()
            src.close()
            log.info("Backup saved: %s", target)
            for old in list_backups()[30:]:
                os.remove(os.path.join(BACKUP_DIR, old))
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        days = int((conn.execute("SELECT value FROM settings WHERE key='screenshot_retention_days'").fetchone()
                    or {"value": "30"})["value"])
        if days > 0:
            cutoff = (now() - timedelta(days=days)).strftime(TS_FMT)
            old = conn.execute("SELECT * FROM screenshots WHERE ts < ?", (cutoff,)).fetchall()
            for r in old:
                try:
                    os.remove(os.path.join(SHOT_DIR, r["path"]))
                except OSError:
                    pass
            conn.execute("DELETE FROM screenshots WHERE ts < ?", (cutoff,))
            conn.commit()
            if old:
                log.info("Deleted %d screenshots older than %d days", len(old), days)
            for root, dirs, _files in os.walk(SHOT_DIR, topdown=False):  # remove empty day folders
                for d in dirs:
                    try:
                        os.rmdir(os.path.join(root, d))
                    except OSError:
                        pass
        conn.close()
    except Exception:  # noqa: BLE001
        log.exception("Daily maintenance failed")


def maintenance_loop():
    while True:
        daily_maintenance()
        time.sleep(3600)


def discovery_loop():
    """Answers 'where is WorkLog?' broadcasts from agents, so they find this
    PC even after its IP address changes."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", DISCOVERY_PORT))
    except OSError:
        log.exception("Discovery service could not start on UDP %s", DISCOVERY_PORT)
        return
    while True:
        try:
            data, addr = sock.recvfrom(256)
            if data.strip() == b"WORKLOG_DISCOVER":
                sock.sendto(f"WORKLOG_HERE {PORT}".encode(), addr)
        except OSError:
            time.sleep(1)


def start_background():
    threading.Thread(target=maintenance_loop, daemon=True, name="maintenance").start()
    threading.Thread(target=discovery_loop, daemon=True, name="discovery").start()


init_db()


def serve_forever():
    start_background()
    log.info("WorkLog %s starting on port %s; data in %s", APP_VERSION, PORT, DATA_DIR)
    try:
        from waitress import serve
        listen = f"0.0.0.0:{PORT}"
        if socket.has_ipv6:
            listen += f" [::]:{PORT}"  # so http://localhost answers instantly (Windows tries IPv6 first)
        try:
            serve(app, listen=listen, threads=12, channel_timeout=60)
        except OSError:
            serve(app, listen=f"0.0.0.0:{PORT}", threads=12, channel_timeout=60)
    except ImportError:
        app.run(host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    print("=" * 60)
    print(f" WorkLog server {APP_VERSION} is running.")
    for u in server_urls():
        print(f"   Open on this network:  {u}")
    print(f"   Open on this PC:       http://localhost:{PORT}")
    print(f"   Data folder:           {DATA_DIR}")
    print(" Keep this window open. Press Ctrl+C to stop.")
    print("=" * 60)
    serve_forever()
