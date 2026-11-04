"""Raspberry Pi gateway: reads N ESP32 motor nodes over USB serial, serves the web portal.
Run:  python3 app.py     (needs: pip install flask pyserial psutil)
"""
import csv, glob, json, os, shutil, subprocess, sys, threading, time
from collections import deque
from datetime import datetime
import serial
from flask import Flask, Response, jsonify, render_template, request, send_file, stream_with_context

try:
    import psutil
except ImportError:
    psutil = None

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
os.makedirs(DATA, exist_ok=True)
BAUD = 115200
OFFLINE_AFTER_S = 10          # no telemetry for this long = link lost
LOG_EVERY_S = 10              # history/CSV sample period
HISTORY_MAX = 8640            # 24 h at 10 s
CSV_FIELDS = ["timestamp", "temperature", "humidity", "smoke", "sound", "vibration",
              "motion", "ir_object", "status", "shutdown", "state"]
THRESHOLD_LIMITS = {          # name: (min, max, type)
    "temperature_max": (0, 80, float), "humidity_max": (0, 100, float),
    "smoke_max": (0, 4095, int), "sound_max": (0, 4095, int), "vibration_max": (1, 1000, int)}
BOOL_CFG = ("vibration_trip", "motion_trip", "auto_shutdown")

app = Flask(__name__)

# ---------------------------------------------------------------- login (password for everyone who opens the site)
import secrets
from datetime import timedelta
from urllib.parse import quote
from flask import session, redirect
from werkzeug.security import generate_password_hash, check_password_hash


def _auth_path(): return os.path.join(DATA, "auth.json")


def _secret():
    p = os.path.join(DATA, "secret.key")
    try:
        return open(p).read().strip()
    except OSError:
        k = secrets.token_hex(32)
        with open(p, "w") as f: f.write(k)
        os.chmod(p, 0o600)
        return k


app.secret_key = _secret()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", PERMANENT_SESSION_LIFETIME=timedelta(days=30))
_fails = {}                    # ip -> [failed attempts, time of last failure]


def _auth():
    try:
        with open(_auth_path()) as f: return json.load(f)
    except Exception:
        return None


def check_pw(pw):
    a = _auth()
    return bool(a) and check_password_hash(a["hash"], pw)


def _locked(ip):
    c = _fails.get(ip)
    return bool(c and c[0] >= 5 and time.time() - c[1] < 300)


def _fail(ip):
    c = _fails.get(ip, [0, 0])
    if time.time() - c[1] > 300: c[0] = 0
    _fails[ip] = [c[0] + 1, time.time()]


@app.before_request
def _gate():
    p = request.path
    if p == "/login" or p.startswith("/static/") or session.get("ok"):
        return None
    if p.startswith("/api/") or p == "/stream":
        return jsonify(error="login required"), 401
    return redirect("/login?next=" + quote(request.full_path.rstrip("?")))


@app.route("/login", methods=["GET", "POST"])
def login():
    ip, msg = request.remote_addr, ""
    if request.method == "POST":
        if _locked(ip):
            msg = "Too many wrong attempts. Wait 5 minutes."
        elif check_pw(request.form.get("password", "")):
            _fails.pop(ip, None)
            session.clear(); session["ok"] = True; session.permanent = bool(request.form.get("stay"))
            nxt = request.args.get("next", "/")
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else "/")
        else:
            _fail(ip); msg = "Wrong password."
    return render_template("login.html", msg=msg, no_password=_auth() is None)


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")

lock = threading.RLock()
motors = {}                   # motor_id -> state dict
links = {}                    # serial path -> Link
notifications = deque(maxlen=200)
names = {}                    # motor_id -> display name (persisted)
BOOT = time.time()


def now_str(fmt="%Y-%m-%d %H:%M:%S"):
    return datetime.now().strftime(fmt)


def notify(motor_id, level, message):
    notifications.append({"time": now_str(), "motor": motor_id, "level": level, "message": message})


# ---------------------------------------------------------------- persistence
def _names_path():
    return os.path.join(DATA, "names.json")


def load_names():
    try:
        with open(_names_path()) as f:
            names.update(json.load(f))
    except Exception:
        pass


def save_names():
    with open(_names_path(), "w") as f:
        json.dump(names, f)


def _csv_path(mid):
    return os.path.join(DATA, f"log_{''.join(c for c in mid if c.isalnum() or c in '_-')}.csv")


def load_history(mid):
    rows = deque(maxlen=HISTORY_MAX)
    try:
        with open(_csv_path(mid), newline="") as f:
            for r in csv.DictReader(f):
                try:
                    rows.append({"ts": datetime.strptime(r["timestamp"], "%Y-%m-%d %H:%M:%S").timestamp(), "timestamp": r["timestamp"], "status": r.get("status", ""), "state": r.get("state") or "RUN", "temperature": float(r["temperature"]),
                                 "humidity": float(r["humidity"]), "smoke": int(float(r["smoke"])),
                                 "sound": int(float(r["sound"])), "vibration": int(float(r["vibration"]))})
                except (KeyError, ValueError):
                    continue
    except FileNotFoundError:
        pass
    return rows


def append_csv(mid, row):
    path = _csv_path(mid)
    if os.path.exists(path): _migrate_csv(path)
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in CSV_FIELDS})


# ---------------------------------------------------------------- settings, run state, normal conditions, offline persistence
_settings_cache = [0, None]


def _settings_path(): return os.path.join(DATA, "settings.json")


def get_settings():
    if time.time() - _settings_cache[0] < 5 and _settings_cache[1] is not None:
        return _settings_cache[1]
    s = {"settle_seconds": 30}
    try:
        with open(_settings_path()) as f: s.update(json.load(f))
    except Exception:
        pass
    _settings_cache[0], _settings_cache[1] = time.time(), s
    return s


def save_settings(s):
    with open(_settings_path(), "w") as f: json.dump(s, f)
    _settings_cache[1] = None


def _run_state(m, t, prev):
    """STOPPED while the relay is cut; SETTLING for the first seconds after a (re)start; RUN afterwards."""
    now = time.time()
    if t.get("shutdown"):
        m["run_since"] = None
        return "STOPPED"
    if m.get("run_since") is None or (prev and t["uptime_ms"] < prev["uptime_ms"]):
        m["run_since"] = now
    return "SETTLING" if now - m["run_since"] < get_settings()["settle_seconds"] else "RUN"


PROFILE_DEFAULT = {"temperature": [15.0, 45.0], "humidity": [30.0, 70.0], "smoke": [0, 600], "sound": [0, 800], "vibration": [0, 5]}
PROFILE_LABELS = {"temperature": "Temperature", "humidity": "Humidity", "smoke": "Smoke", "sound": "Sound", "vibration": "Vibration"}


def _profiles_path(): return os.path.join(DATA, "profiles.json")


def get_profile(mid):
    try:
        with open(_profiles_path()) as f: return json.load(f).get(mid)
    except Exception:
        return None


def save_profile(mid, prof):
    try:
        with open(_profiles_path()) as f: allp = json.load(f)
    except Exception:
        allp = {}
    allp[mid] = prof
    with open(_profiles_path(), "w") as f: json.dump(allp, f)


def _pctl(v, p):
    v = sorted(v)
    return v[min(len(v) - 1, int(p / 100 * len(v)))]


def learn_profile(m):
    """Normal range = 5th to 95th percentile of the last 24 h of settled, healthy (SAFE) running data."""
    out, n = {}, 0
    for k in PROFILE_DEFAULT:
        vals = [r[k] for r in m["history"] if r.get("state", "RUN") == "RUN" and r.get("status") == "SAFE"
                and not (k in ("temperature", "humidity") and r[k] == 0)]
        n = max(n, len(vals))
        if len(vals) >= 180:
            out[k] = [round(_pctl(vals, 5), 1), round(_pctl(vals, 95), 1)]
    return out, n


def outside_normal(m):
    prof, t = get_profile(m["id"]), m["t"]
    if not prof or not t or m.get("state") != "RUN":
        return []
    rows = [r for r in list(m["history"])[-6:] if r.get("state", "RUN") == "RUN"]
    res = []
    if len(rows) >= 5:
        for k, (lo, hi) in prof.items():
            if k not in PROFILE_LABELS: continue
            vals = [r[k] for r in rows if not (k in ("temperature", "humidity") and r[k] == 0)]
            if len(vals) >= 5 and sum(1 for v in vals if v < lo or v > hi) >= 5:
                res.append({"sensor": k, "label": PROFILE_LABELS[k], "value": round(vals[-1], 1), "low": lo, "high": hi})
    return res


def _state_path(mid): return os.path.join(DATA, f"state_{''.join(c for c in mid if c.isalnum() or c in '_-')}.json")


def save_state(m):
    """Last known state of a motor, so it is still listed (with its data) after the Pi restarts while the ESP32 is off."""
    try:
        tmp = _state_path(m["id"]) + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"id": m["id"], "t": m["t"], "last_seen": m["last_seen"], "state": m.get("state"), "cond": m.get("cond")}, f)
        os.replace(tmp, _state_path(m["id"]))
    except OSError as e:
        print("[state]", e)


def load_known_motors():
    for p in glob.glob(os.path.join(DATA, "state_*.json")):
        try:
            with open(p) as f: s = json.load(f)
            m = get_motor(str(s["id"]))
            m.update(t=s.get("t"), last_seen=s.get("last_seen", 0), state="OFFLINE", cond=None)
        except Exception:
            continue
    for p in glob.glob(os.path.join(DATA, "log_*.csv")):
        mid = os.path.basename(p)[4:-4]
        if mid and not any(k == mid for k in motors):
            get_motor(mid)


def read_csv_range(mid, minutes):
    """Samples older than the in-memory 24 h, read from the CSV on disk and thinned to about 360 points."""
    cutoff = datetime.fromtimestamp(time.time() - minutes * 60).strftime("%Y-%m-%d %H:%M:%S")
    path, rows = _csv_path(mid), []
    try:
        with open(path, newline="") as f:
            total = sum(1 for r in csv.DictReader(f) if r.get("timestamp", "") >= cutoff)
        step, i = max(1, total // 360), 0
        with open(path, newline="") as f:
            for r in csv.DictReader(f):
                if r.get("timestamp", "") < cutoff: continue
                i += 1
                if (i - 1) % step: continue
                try:
                    rows.append({"timestamp": r["timestamp"], "temperature": float(r["temperature"]), "humidity": float(r["humidity"]),
                                 "smoke": int(float(r["smoke"])), "sound": int(float(r["sound"])), "vibration": int(float(r["vibration"]))})
                except (KeyError, ValueError):
                    continue
    except FileNotFoundError:
        pass
    return rows


_migrated = set()


def _migrate_csv(path):
    """Older logs have no 'state' column: add it (blank = unknown, treated as RUN) so new rows line up."""
    if path in _migrated: return
    _migrated.add(path)
    with open(path, newline="") as f: head = f.readline()
    if "state" in head: return
    tmp = path + ".tmp"
    with open(path, newline="") as src, open(tmp, "w", newline="") as dst:
        for i, line in enumerate(src):
            dst.write(line.rstrip("\r\n") + (",state" if i == 0 else ",") + "\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------- cumulative fault forecast (30 min up to 6 h ahead)
FORECAST_WINDOWS = [(600, 1800, "10 min"), (3600, 10800, "1 h"), (21600, 21600, "6 h")]   # (fit window s, horizon s, label)
TREND_HOLD_S = 60              # a trend must be consistent over at least this much data (longer windows need half their length)
MIN_R2 = 0.6                   # how straight the trend must be (0..1)
FORECAST_SENSORS = {           # key: (label, limit key, minimum rise over 10 min to count as a trend)
    "temperature": ("Temperature", "temperature_max", 0.5), "humidity": ("Humidity", "humidity_max", 1.0),
    "smoke": ("Smoke", "smoke_max", 50), "sound": ("Sound", "sound_max", 50), "vibration": ("Vibration", "vibration_max", 1)}


def fmt_eta(minutes):
    return f"{round(minutes)} min" if minutes < 90 else f"{minutes / 60:.1f} h"


def _fit(pts):
    n = len(pts)
    mx, my = sum(x for x, _ in pts) / n, sum(y for _, y in pts) / n
    sxx = sum((x - mx) ** 2 for x, _ in pts)
    if sxx == 0:
        return 0.0, 0.0
    sxy = sum((x - mx) * (y - my) for x, y in pts)
    syy = sum((y - my) ** 2 for _, y in pts)
    return sxy / sxx, ((sxy * sxy) / (sxx * syy) if syy > 0 else 0.0)


def run_segment(m, max_age):
    """Samples of the current continuous run, oldest first: only settled RUN samples, no stop, no link gap."""
    now, rows = time.time(), []
    for r in reversed(m["history"]):
        ts = r.get("ts")
        if ts is None or now - ts > max_age: break
        if r.get("state", "RUN") != "RUN": break          # a stop or the settling period after a start ends the segment
        if rows and rows[-1]["ts"] - ts > 45: break       # link gap
        rows.append(r)
    rows.reverse()
    return rows


def update_forecast(m):
    """Straight-line trend on the cumulative running data, several window lengths. Warning only - never a shutdown."""
    t, preds = m["t"], []
    seg = run_segment(m, FORECAST_WINDOWS[-1][0])
    for key, (label, limit_key, min_rise) in FORECAST_SENSORS.items():
        limit, best = t.get(limit_key), None
        if limit is None or not seg:
            continue
        for win, horizon, basis in FORECAST_WINDOWS:
            rows = [r for r in seg if seg[-1]["ts"] - r["ts"] <= win]
            pts = [(r["ts"] - rows[0]["ts"], r[key]) for r in rows if not (key in ("temperature", "humidity") and r[key] == 0)]
            need = TREND_HOLD_S if win <= 600 else win * 0.5
            if len(pts) < 6 or pts[-1][0] - pts[0][0] < need or pts[-1][1] >= limit:
                continue
            slope, r2 = _fit(pts)
            if r2 < MIN_R2 or slope < min_rise / 600.0:
                continue
            eta = (limit - pts[-1][1]) / slope
            if eta <= horizon and (best is None or eta < best["eta"]):
                best = {"eta": eta, "slope": slope, "cur": pts[-1][1], "r2": r2, "basis": basis}
        if best:
            preds.append({"sensor": key, "label": label, "eta_min": round(best["eta"] / 60, 1), "rate_per_min": round(best["slope"] * 60, 2),
                          "current": round(best["cur"], 1), "limit": limit, "r2": round(best["r2"], 2), "basis": best["basis"]})
    preds.sort(key=lambda p: p["eta_min"])
    for p in preds:
        if p["sensor"] not in m["pred_keys"]:
            notify(m["id"], "warning", f"{display_name(m['id'])}: {p['label']} rising {p['rate_per_min']}/min - may reach its limit in ~{fmt_eta(p['eta_min'])} ({p['basis']} trend)")
    m["pred_keys"] = {p["sensor"] for p in preds}
    m["pred"] = preds


# ---------------------------------------------------------------- motor state
def get_motor(mid):
    if mid not in motors:
        motors[mid] = {"id": mid, "port": None, "t": None, "last_seen": 0, "connected": False,
                       "last_status": None, "last_pred": "", "last_log": 0, "history": load_history(mid),
                       "pred": [], "since": {}, "pred_keys": set(), "state": "", "run_since": None}
    return motors[mid]


def display_name(mid):
    return names.get(mid) or mid.replace("_", " ").title()


def breaches(t):
    out = []
    if t.get("dht_valid"):
        if t["temperature"] >= t["temperature_max"]: out.append("Temperature")
        if t["humidity"] >= t["humidity_max"]: out.append("Humidity")
    if t["smoke"] >= t["smoke_max"]: out.append("Smoke")
    if t["sound"] >= t["sound_max"]: out.append("Sound")
    if t.get("vibration_trip") and t.get("vibration_detected"): out.append("Vibration")
    if t.get("motion_trip") and t.get("motion"): out.append("Motion")
    if t.get("cover_closed") is False: out.append("Cover open")
    return out


def alerts_for(m):
    t, name, out = m["t"], display_name(m["id"]), []
    if not m["connected"]:
        if m["last_seen"]:
            out.append({"motor": m["id"], "name": name, "level": "danger",
                        "message": "Link lost - no data from the ESP32 (its local protection still runs)"})
        return out
    if not t:
        return out
    if t["status"] == "SHUTDOWN":
        out.append({"motor": m["id"], "name": name, "level": "danger", "message": "Shutdown latched: " + t["shutdown_reason"]})
    elif t["status"] == "DANGER":
        out.append({"motor": m["id"], "name": name, "level": "danger",
                    "message": "Hazard (auto-shutdown disabled): " + t["shutdown_reason"]})
    elif t["status"] == "WARNING":
        b = breaches(t)
        msg = ("Over limit: " + ", ".join(b)) if b else (t["prediction_details"] if t["predicted_hazard"] else "Reading near a limit")
        out.append({"motor": m["id"], "name": name, "level": "warning", "message": msg})
    if not t["dht_valid"]:
        out.append({"motor": m["id"], "name": name, "level": "warning", "message": "DHT22 not responding"})
    for p in m["pred"]:
        out.append({"motor": m["id"], "name": name, "level": "warning",
                    "message": f"Forecast: {p['label']} rising {p['rate_per_min']}/min - may reach its limit in ~{fmt_eta(p['eta_min'])} ({p['basis']} trend)"})
    for o in m.get("outside", []):
        out.append({"motor": m["id"], "name": name, "level": "warning",
                    "message": f"Outside its normal range: {o['label']} {o['value']} (normal {o['low']} to {o['high']})"})
    return out


def motor_view(m):
    online = m["connected"]
    t = m["t"]
    return {"id": m["id"], "name": display_name(m["id"]), "port": m["port"], "connected": online,
            "status": (t["status"] if (online and t) else "OFFLINE"),
            "last_seen_s": round(time.time() - m["last_seen"], 1) if m["last_seen"] else None,
            "t": t, "breaches": breaches(t) if t else [],
            "forecast": m["pred"] if online else [],
            "condition": m.get("cond") if online else None,
            "state": m.get("state") if online else "OFFLINE", "outside_normal": m.get("outside", []) if online else []}


def fleet():
    with lock:
        ms = sorted(motors.values(), key=lambda m: m["id"])
        views = [motor_view(m) for m in ms]
        alerts = [a for m in ms for a in alerts_for(m)]
        return {"timestamp": now_str(), "motors": views, "alerts": alerts,
                "notifications": list(notifications)[-60:],
                "summary": {"total": len(views),
                            "online": sum(v["connected"] for v in views),
                            "offline": sum(not v["connected"] for v in views),
                            "warning": sum(v["status"] == "WARNING" for v in views),
                            "danger": sum(v["status"] in ("DANGER", "SHUTDOWN") for v in views),
                            "alerts": len(alerts)}}


# ---------------------------------------------------------------- serial links
class Link(threading.Thread):
    def __init__(self, path):
        super().__init__(daemon=True)
        self.path, self.ser, self.motor_id, self.alive = path, None, None, True
        self.wlock = threading.Lock()

    def send(self, obj):
        with self.wlock:
            if not self.ser or not self.ser.is_open:
                return False
            try:
                self.ser.write(("@C" + json.dumps(obj, separators=(",", ":")) + "\n").encode())
                return True
            except Exception:
                return False

    def run(self):
        try:
            s = serial.Serial()
            s.port, s.baudrate, s.timeout = self.path, BAUD, 1
            s.dtr = False; s.rts = False     # avoid resetting the ESP32 on open where possible
            s.open()
            self.ser = s
            time.sleep(0.5)
            self.send({"cmd": "hello"})
            while self.alive:
                line = s.readline().decode(errors="ignore").strip()
                if line.startswith("@T"):
                    self.on_telemetry(line[2:])
                elif line.startswith("@E"):
                    self.on_event(line[2:])
        except Exception as e:
            print(f"[link] {self.path} closed: {e}")
        finally:
            self.alive = False
            try:
                if self.ser: self.ser.close()
            except Exception:
                pass

    def on_event(self, raw):
        try:
            d = json.loads(raw)
        except ValueError:
            return
        level = "warning" if "rejected" in d.get("msg", "") else "info"
        with lock:
            notify(d.get("id", self.motor_id), level, f"{display_name(d.get('id', ''))}: {d.get('msg', '')}")

    def on_telemetry(self, raw):
        try:
            t = json.loads(raw)
            mid = str(t["id"])
        except (ValueError, KeyError):
            return
        with lock:
            for other in links.values():
                if other is not self and other.alive and other.motor_id == mid:
                    notify(mid, "danger", f"Duplicate motor ID '{mid}' on {self.path} and {other.path} - give each ESP32 a unique MOTOR_ID")
            self.motor_id = mid
            m = get_motor(mid)
            prev, was_online = m["t"], m["connected"]
            m.update(port=self.path, connected=True, last_seen=time.time())
            name = display_name(mid)
            if not was_online:
                notify(mid, "info", f"{name}: link established on {self.path}")
            if prev and t["uptime_ms"] < prev["uptime_ms"]:       # ESP32 rebooted: restore its non-persistent settings
                notify(mid, "warning", f"{name}: ESP32 restarted")
                self.send({"cfg": {k: prev[k] for k in list(THRESHOLD_LIMITS) + list(BOOL_CFG) if k in prev}})
            if m["last_status"] != t["status"]:
                if m["last_status"] is not None or t["status"] != "SAFE":
                    lvl = {"SAFE": "info", "WARNING": "warning"}.get(t["status"], "danger")
                    extra = f" ({t['shutdown_reason']})" if t["status"] != "SAFE" else ""
                    notify(mid, lvl, f"{name}: status {m['last_status'] or '-'} -> {t['status']}{extra}")
                m["last_status"] = t["status"]
            if t["predicted_hazard"] and t["prediction_details"] != m["last_pred"]:
                notify(mid, "warning", f"{name}: {t['prediction_details']}")
            m["last_pred"] = t["prediction_details"] if t["predicted_hazard"] else ""
            m["t"] = t
            m["state"] = _run_state(m, t, prev)
            if time.time() - m["last_log"] >= LOG_EVERY_S:
                m["last_log"] = time.time()
                row = {"ts": time.time(), "timestamp": now_str(), "temperature": t["temperature"], "humidity": t["humidity"],
                       "smoke": t["smoke"], "sound": t["sound"], "vibration": t["vibration"],
                       "motion": int(t["motion"]), "ir_object": int(t["ir_object"]),
                       "status": t["status"], "shutdown": int(t["shutdown"]), "state": m["state"]}
                m["history"].append(row)
                try: append_csv(mid, row)
                except OSError as e: print("[csv]", e)
                update_forecast(m)
                m["cond"] = cond_now(m)
                m["outside"] = outside_normal(m)
                save_state(m)


def supervisor():
    """Discovers new USB serial ports and flags motors that stopped talking."""
    while True:
        with lock:
            for path in glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*"):
                if path not in links or not links[path].alive:
                    links[path] = Link(path)
                    links[path].start()
            for path in [p for p, l in links.items() if not l.alive]:
                del links[path]
            for m in motors.values():
                if m["connected"] and time.time() - m["last_seen"] > OFFLINE_AFTER_S:
                    m["connected"] = False
                    notify(m["id"], "danger", f"{display_name(m['id'])}: link lost")
        time.sleep(3)


def link_for(mid):
    with lock:
        for l in links.values():
            if l.alive and l.motor_id == mid:
                return l
    return None


# ---------------------------------------------------------------- pages
def page(template, active):
    return render_template(template, active_page=active)

@app.route("/")
def home(): return page("home.html", "home")
@app.route("/live")
def live_monitoring(): return page("live_monitoring.html", "live")
@app.route("/control")
def control(): return page("control.html", "control")
@app.route("/alerts")
def alerts(): return page("alerts.html", "alerts")
@app.route("/data")
def data_analytics(): return page("data_analytics.html", "data")
@app.route("/health")
def system_health(): return page("system_health.html", "health")
@app.route("/settings")
def settings(): return page("settings.html", "settings")


# ---------------------------------------------------------------- API
@app.route("/api/fleet")
def api_fleet(): return jsonify(fleet())


@app.route("/stream")
def stream():
    def gen():
        while True:
            yield "data: " + json.dumps(fleet()) + "\n\n"
            time.sleep(2)
    return Response(stream_with_context(gen()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/motor/<mid>/command", methods=["POST"])
def api_command(mid):
    cmd = (request.get_json(silent=True) or {}).get("cmd")
    if cmd not in ("shutdown", "resume"):
        return jsonify(error="cmd must be 'shutdown' or 'resume'"), 400
    l = link_for(mid)
    if not l or not l.send({"cmd": cmd}):
        return jsonify(sent=False, error="Motor is not connected"), 503
    with lock:
        notify(mid, "info", f"{display_name(mid)}: {cmd} requested from dashboard")
    return jsonify(sent=True)


@app.route("/api/motor/<mid>/config", methods=["POST"])
def api_config(mid):
    body = request.get_json(silent=True) or {}
    cfg = {}
    for k, (lo, hi, typ) in THRESHOLD_LIMITS.items():
        if k in body:
            try:
                cfg[k] = max(lo, min(hi, typ(float(body[k]))))
            except (TypeError, ValueError):
                return jsonify(error=f"Invalid value for {k}"), 400
    for k in BOOL_CFG:
        if k in body:
            cfg[k] = bool(body[k])
    if not cfg:
        return jsonify(error="No settings supplied"), 400
    l = link_for(mid)
    if not l or not l.send({"cfg": cfg}):
        return jsonify(sent=False, error="Motor is not connected"), 503
    with lock:
        notify(mid, "info", f"{display_name(mid)}: settings updated from dashboard")
    return jsonify(sent=True, applied=cfg)


@app.route("/api/motor/<mid>/rename", methods=["POST"])
def api_rename(mid):
    name = str((request.get_json(silent=True) or {}).get("name", "")).strip()[:40]
    with lock:
        if name: names[mid] = name
        else: names.pop(mid, None)
        save_names()
    return jsonify(name=display_name(mid))


# ---------------------------------------------------------------- overall condition index (0-100 % toward each limit)
COND_SENSORS = {   # key: (label, limit key, default "normal" value used until enough healthy data exists)
    "temperature": ("Temperature", "temperature_max", 25.0), "humidity": ("Humidity", "humidity_max", 40.0),
    "smoke": ("Smoke", "smoke_max", 0.0), "sound": ("Sound", "sound_max", 0.0), "vibration": ("Vibration", "vibration_max", 0.0)}
COND_BASE_WINDOW_S = 3600      # "normal" = median of the last hour of SAFE samples
COND_MIN_SAFE = 30             # healthy samples needed before the learned value replaces the default
COND_SMOOTH = 6                # 1-minute moving average (6 samples of 10 s)


def _profile_mid(m, k, default):
    p = get_profile(m["id"])
    return (p[k][0] + p[k][1]) / 2 if p and k in p else default


def _band(v): return "Good" if v < 50 else ("Watch" if v < 80 else "Critical")


def _median(v):
    v = sorted(v); n = len(v)
    return None if not n else (v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2)


def _usable(key, r):           # a DHT11 error is logged as 0, so it must not count as a real reading
    return not (key in ("temperature", "humidity") and r.get(key, 0) == 0)


def _z(v, normal, limit):
    span = limit - normal
    if span <= 1e-9:
        return 100.0 if v >= limit else 0.0
    return max(0.0, min(100.0, (v - normal) / span * 100.0))


def cond_baseline(m):
    now, cached = time.time(), m.get("base")
    if cached and now - cached[0] < 30:
        return cached[1]
    rows = [r for r in m["history"] if r.get("ts") and now - r["ts"] <= COND_BASE_WINDOW_S and r.get("status") == "SAFE" and r.get("state", "RUN") == "RUN"]
    base = {}
    for k, (_, _, default) in COND_SENSORS.items():
        vals = [r[k] for r in rows if _usable(k, r)]
        base[k] = _median(vals) if len(vals) >= COND_MIN_SAFE else _profile_mid(m, k, default)
    m["base"] = (now, base)
    return base


def cond_now(m):
    t, base = m["t"] or {}, cond_baseline(m)
    rows, best = list(m["history"])[-COND_SMOOTH:], None
    for k, (label, limit_key, _) in COND_SENSORS.items():
        lim = t.get(limit_key)
        zs = [_z(r[k], base[k], lim) for r in rows if lim is not None and _usable(k, r) and r.get("state", "RUN") == "RUN"]
        if zs and (best is None or sum(zs) / len(zs) > best[0]):
            best = (sum(zs) / len(zs), label)
    return None if best is None else {"index": round(best[0]), "driver": best[1], "band": _band(best[0])}


def cond_series(m, minutes):
    t, base, now = m["t"] or {}, cond_baseline(m), time.time()
    rows = [r for r in m["history"] if r.get("ts") and now - r["ts"] <= minutes * 60]
    raw = {k: [None if t.get(lk) is None or not _usable(k, r) or r.get("state", "RUN") != "RUN" else _z(r[k], base[k], t[lk]) for r in rows]
           for k, (_, lk, _) in COND_SENSORS.items()}
    sm = {}
    for k, arr in raw.items():
        out = []
        for i in range(len(arr)):
            w = [x for x in arr[max(0, i - COND_SMOOTH + 1): i + 1] if x is not None]
            out.append(round(sum(w) / len(w), 1) if w else None)
        sm[k] = out
    total, driver = [], []
    for i in range(len(rows)):
        c = [(sm[k][i], k) for k in sm if sm[k][i] is not None]
        if c:
            v, k = max(c); total.append(v); driver.append(COND_SENSORS[k][0])
        else:
            total.append(None); driver.append(None)
    step = max(1, len(rows) // 360)
    last = next((i for i in range(len(total) - 1, -1, -1) if total[i] is not None), None)
    return {"labels": [r["timestamp"][11:19] if minutes <= 360 else r["timestamp"][5:16] for r in rows][::step],
            "sensors": {COND_SENSORS[k][0]: v[::step] for k, v in sm.items()},
            "total": total[::step], "driver": driver[::step],
            "now": None if last is None else {"index": round(total[last]), "driver": driver[last], "band": _band(total[last])},
            "normal": {COND_SENSORS[k][0]: round(v, 1) for k, v in base.items()}}


@app.route("/api/motor/<mid>/condition")
def api_condition(mid):
    minutes = max(5, min(1440, request.args.get("minutes", 60, type=int)))
    with lock:
        m = motors.get(mid)
        if not m:
            return jsonify(error="Unknown motor"), 404
        return jsonify(cond_series(m, minutes))


@app.route("/api/motor/<mid>/history")
def api_history(mid):
    minutes = max(5, min(43200, request.args.get("minutes", 60, type=int)))
    with lock:
        m = motors.get(mid)
        if not m:
            return jsonify(error="Unknown motor"), 404
        t = m["t"] or {}
        if minutes > 1440:
            rows = read_csv_range(mid, minutes)
        else:
            cutoff = datetime.fromtimestamp(time.time() - minutes * 60).strftime("%Y-%m-%d %H:%M:%S")
            rows = [r for r in m["history"] if r["timestamp"] >= cutoff]
    step = max(1, len(rows) // 360)
    rows = rows[::step]
    out = {k: [r[k] for r in rows] for k in ("temperature", "humidity", "smoke", "sound", "vibration")}
    out["labels"] = [r["timestamp"][11:19] if minutes <= 360 else r["timestamp"][5:16] for r in rows]
    out["limits"] = {k: t.get(k) for k in ("temperature_max", "humidity_max", "smoke_max", "sound_max", "vibration_max")}
    return jsonify(out)


@app.route("/api/motor/<mid>/profile", methods=["GET", "POST"])
def api_profile(mid):
    if mid not in motors:
        return jsonify(error="Unknown motor"), 404
    if request.method == "POST":
        body, prof = request.get_json(silent=True) or {}, {}
        for k in PROFILE_DEFAULT:
            try:
                lo, hi = float(body[k][0]), float(body[k][1])
            except (KeyError, TypeError, ValueError, IndexError):
                return jsonify(error=f"Enter a low and a high value for {PROFILE_LABELS[k]}"), 400
            if lo > hi:
                return jsonify(error=f"{PROFILE_LABELS[k]}: the low value is above the high value"), 400
            prof[k] = [lo, hi]
        with lock:
            save_profile(mid, prof)
            notify(mid, "info", f"{display_name(mid)}: normal operating conditions saved")
        return jsonify(ok=True)
    saved = get_profile(mid)
    return jsonify(saved=bool(saved), profile=saved or PROFILE_DEFAULT, defaults=PROFILE_DEFAULT)


@app.route("/api/motor/<mid>/profile/learn", methods=["POST"])
def api_profile_learn(mid):
    with lock:
        m = motors.get(mid)
        if not m:
            return jsonify(error="Unknown motor"), 404
        learned, n = learn_profile(m)
    if len(learned) < len(PROFILE_DEFAULT):
        return jsonify(ok=False, error=f"Not enough healthy running data yet: need at least 30 minutes of settled, SAFE running data per sensor (best sensor has {n // 6} minutes)."), 400
    merged = dict(get_profile(mid) or PROFILE_DEFAULT)
    merged.update(learned)
    return jsonify(ok=True, profile=merged, minutes=n // 6)


@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    if request.method == "POST":
        try:
            v = int(float((request.get_json(silent=True) or {}).get("settle_seconds")))
        except (TypeError, ValueError):
            return jsonify(error="settle_seconds must be a number"), 400
        s_ = dict(get_settings()); s_["settle_seconds"] = max(0, min(600, v)); save_settings(s_)
    return jsonify(get_settings())


@app.route("/api/export_csv")
def api_export():
    mid = request.args.get("motor", "")
    path = _csv_path(mid)
    if not os.path.exists(path):
        return jsonify(error="No log for that motor yet"), 404
    return send_file(path, as_attachment=True, download_name=f"{mid}_log.csv")


def _throttled():
    try:
        v = int(subprocess.check_output(["vcgencmd", "get_throttled"], timeout=2).decode().split("=")[1], 16)
    except Exception:
        return None
    b = lambda n: bool(v >> n & 1)
    return {"under_voltage_now": b(0), "freq_capped_now": b(1), "throttled_now": b(2), "soft_temp_limit_now": b(3),
            "under_voltage_ever": b(16), "freq_capped_ever": b(17), "throttled_ever": b(18)}


@app.route("/api/system_health")
def api_system_health():
    temp = None
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            temp = round(int(f.read()) / 1000, 1)
    except Exception:
        pass
    du = shutil.disk_usage("/")
    vm = psutil.virtual_memory() if psutil else None
    up = int(time.time() - (psutil.boot_time() if psutil else BOOT))
    return jsonify({"cpu_percent": psutil.cpu_percent(interval=None) if psutil else 0,
                    "ram": {"used_percent": vm.percent if vm else 0, "total_mb": round(vm.total / 1048576) if vm else 0},
                    "temperature_c": temp, "throttled": _throttled(),
                    "disk": {"used_percent": round(du.used / du.total * 100, 1), "free_gb": round(du.free / 1e9, 1)},
                    "uptime": f"{up // 86400}d {up % 86400 // 3600}h {up % 3600 // 60}m"})


# ---------------------------------------------------------------- Wi-Fi management (NetworkManager / nmcli)
import re

HOTSPOT_NAME = "SmartSafetyHotspot"


def _nm_split(line):
    parts, cur, esc = [], "", False
    for c in line:                      # nmcli -t escapes ':' and '\' with a backslash
        if esc: cur += c; esc = False
        elif c == "\\": esc = True
        elif c == ":": parts.append(cur); cur = ""
        else: cur += c
    parts.append(cur)
    return parts


def _nm(args, timeout=25):
    """Run nmcli; if the OS says we lack permission, retry once through passwordless sudo."""
    r = None
    for use_sudo in (False, True):
        try:
            r = subprocess.run((["sudo", "-n"] if use_sudo else []) + ["nmcli"] + args,
                               capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            return 127, "", "nmcli not found - NetworkManager is not installed"
        except subprocess.TimeoutExpired:
            return 124, "", "Timed out"
        msg = (r.stderr or r.stdout).lower()
        if r.returncode != 0 and not use_sudo and any(w in msg for w in ("privileg", "not authorized", "permission")):
            continue
        break
    return r.returncode, r.stdout, (r.stderr.strip() or r.stdout.strip())


def _wifi_saved():
    rc, out, _ = _nm(["-t", "-f", "NAME,TYPE", "connection", "show"])
    if rc != 0:
        return []
    return [p[0] for p in map(_nm_split, out.splitlines()) if len(p) >= 2 and p[1] == "802-11-wireless" and p[0] != HOTSPOT_NAME]


def _ips():
    ips = {}
    try:
        out = subprocess.check_output(["ip", "-4", "-o", "addr", "show"], text=True, timeout=3)
        for line in out.splitlines():
            mm = re.match(r"\d+:\s+(\S+)\s+inet\s+([\d.]+)", line)
            if mm: ips[mm.group(1)] = mm.group(2)
    except Exception:
        pass
    return ips


@app.route("/api/wifi/status")
def api_wifi_status():
    rc, out, err = _nm(["-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status"])
    wifi, eth = {"state": "unavailable", "ssid": None}, False
    for p in map(_nm_split, out.splitlines()):
        if len(p) < 4: continue
        if p[1] == "wifi": wifi = {"state": p[2], "ssid": p[3] or None}
        if p[1] == "ethernet" and p[2] == "connected": eth = True
    ips = _ips()
    return jsonify(nm_ok=(rc == 0), error=None if rc == 0 else err, wifi=wifi, ethernet=eth,
                   wifi_ip=ips.get("wlan0"), eth_ip=ips.get("eth0"), saved=_wifi_saved())


_scan = {"state": "idle", "results": [], "error": None}
_scan_lock = threading.Lock()


def _wifi_dev_state():
    rc, out, _ = _nm(["-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status"])
    for p in map(_nm_split, out.splitlines()):
        if len(p) >= 4 and p[1] == "wifi":
            return p[2], p[3]
    return "unavailable", ""


def _scan_worker():
    """One Wi-Fi radio can't scan while it runs the hotspot: pause it, scan, then restore it
    (unless the Pi has meanwhile joined a saved network)."""
    on_hotspot = _wifi_dev_state()[1] == HOTSPOT_NAME
    results, error = [], None
    try:
        if on_hotspot:
            _nm(["connection", "down", "id", HOTSPOT_NAME])
            time.sleep(2)
        _nm(["radio", "wifi", "on"])
        rc, out, err = _nm(["-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "device", "wifi", "list", "--rescan", "yes"], timeout=30)
        if rc != 0:
            error = err or "Scan failed"
        else:
            saved, best = set(_wifi_saved()), {}
            for p in map(_nm_split, out.splitlines()):
                if len(p) < 4 or not p[1] or p[1] == "smartsafety": continue
                sig = int(p[2]) if p[2].isdigit() else 0
                n = {"ssid": p[1], "signal": sig, "security": p[3] or "Open", "in_use": p[0] == "*", "saved": p[1] in saved}
                old = best.get(p[1])
                if old is None or n["in_use"] or (sig > old["signal"] and not old["in_use"]):
                    best[p[1]] = n
            results = sorted(best.values(), key=lambda n: (-n["in_use"], -n["signal"]))
    finally:
        if on_hotspot:
            time.sleep(1)
            if _wifi_dev_state()[0] != "connected":
                _nm(["connection", "up", "id", HOTSPOT_NAME], timeout=30)
        with _scan_lock:
            _scan.update(state="error" if error else "done", results=results, error=error)


@app.route("/api/wifi/scan", methods=["GET", "POST"])
def api_wifi_scan():
    if request.method == "POST":
        with _scan_lock:
            if _scan["state"] != "scanning":
                _scan.update(state="scanning", results=[], error=None)
                threading.Thread(target=_scan_worker, daemon=True).start()
    return jsonify(dict(_scan))


@app.route("/api/wifi/connect", methods=["POST"])
def api_wifi_connect():
    b = request.get_json(silent=True) or {}
    ssid, pw, hidden = str(b.get("ssid", "")).strip(), str(b.get("password", "")), bool(b.get("hidden"))
    if not ssid or len(ssid.encode()) > 32:
        return jsonify(error="Network name must be 1-32 characters"), 400
    if ssid.startswith("-"):
        return jsonify(error="Network name cannot start with '-'"), 400
    if pw and not 8 <= len(pw) <= 63:
        return jsonify(error="Password must be 8-63 characters (leave blank for an open network)"), 400
    if not pw and ssid in _wifi_saved():
        rc, out, err = _nm(["connection", "up", "id", ssid], timeout=45)
    else:
        rc, out, err = _nm(["device", "wifi", "connect", ssid] + (["password", pw] if pw else []) + (["hidden", "yes"] if hidden else []), timeout=45)
    with lock:
        notify("pi", "info" if rc == 0 else "warning", f"Wi-Fi {'connected to' if rc == 0 else 'could not connect to'} '{ssid}'")
    if rc != 0:
        return jsonify(ok=False, error=err or "Could not connect"), 502
    return jsonify(ok=True)


@app.route("/api/wifi/hotspot", methods=["POST"])
def api_wifi_hotspot():
    rc, _, err = _nm(["connection", "up", "id", HOTSPOT_NAME], timeout=30)
    with lock:
        notify("pi", "info" if rc == 0 else "warning", "Hotspot " + ("started" if rc == 0 else "could not start"))
    if rc != 0:
        return jsonify(ok=False, error=err or "Hotspot profile missing - run the boot setup script"), 502
    return jsonify(ok=True)


@app.route("/api/wifi/forget", methods=["POST"])
def api_wifi_forget():
    name = str((request.get_json(silent=True) or {}).get("name", ""))
    if name not in _wifi_saved():
        return jsonify(error="Not a saved Wi-Fi network"), 404
    rc, _, err = _nm(["connection", "delete", "id", name])
    if rc != 0:
        return jsonify(error=err or "Failed"), 502
    return jsonify(ok=True)


# ---------------------------------------------------------------- phone access (QR code)
@app.route("/api/access")
def api_access():
    import socket
    port = request.host.rsplit(":", 1)[1] if ":" in request.host else "80"
    urls = [f"http://{ip}:{port}" for ip in _ips().values() if not ip.startswith("127.")]
    urls.append(f"http://{socket.gethostname()}.local:{port}")
    cur = request.host_url.rstrip("/")
    return jsonify(urls=urls, current=cur if cur in urls else None)


@app.route("/api/qr.svg")
def api_qr():
    url = request.args.get("url", "")[:200]
    if not url.startswith(("http://", "https://")):
        return "Bad URL", 400
    try:
        import io, segno
    except ImportError:
        return "Install the QR library: pip install segno", 503
    buf = io.BytesIO()
    segno.make(url, error="m").save(buf, kind="svg", scale=6, border=2, dark="#000000", light="#ffffff", xmldecl=False)
    return Response(buf.getvalue(), mimetype="image/svg+xml", headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------- GitHub backup (upload only after the password is entered)
def _git(*args, timeout=90):
    try:
        r = subprocess.run(["git", "-C", BASE] + list(args), capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except FileNotFoundError:
        return 127, "git is not installed"
    except subprocess.TimeoutExpired:
        return 124, "Timed out"


def git_status():
    rc, _ = _git("rev-parse", "--is-inside-work-tree")
    if rc != 0:
        return {"configured": False}
    rcr, remote = _git("remote", "get-url", "origin")
    rcs, porc = _git("status", "--porcelain")
    _, last = _git("log", "-1", "--format=%cd", "--date=format:%Y-%m-%d %H:%M")
    return {"configured": rcr == 0, "remote": remote if rcr == 0 else None,
            "changes": len([l for l in porc.splitlines() if l.strip()]) if rcs == 0 else None,
            "last_push": last if last and "fatal" not in last else None}


@app.route("/api/github/status")
def api_github_status():
    return jsonify(git_status())


@app.route("/api/github/push", methods=["POST"])
def api_github_push():
    ip = request.remote_addr
    if _locked(ip):
        return jsonify(ok=False, error="Too many wrong attempts. Wait 5 minutes."), 429
    if not check_pw(str((request.get_json(silent=True) or {}).get("password", ""))):
        _fail(ip)
        return jsonify(ok=False, error="Wrong password - nothing was uploaded."), 403
    _fails.pop(ip, None)
    if not git_status().get("configured"):
        return jsonify(ok=False, error="GitHub is not set up yet (run setup_github.sh on the Pi)."), 400
    _git("add", "-A")
    _git("commit", "-m", "Backup " + now_str())            # returns 1 when there is nothing new - not an error
    rc, out = _git("push", "-u", "origin", "HEAD", timeout=120)
    with lock:
        notify("pi", "info" if rc == 0 else "warning", "GitHub backup " + ("uploaded" if rc == 0 else "failed"))
    return jsonify(ok=rc == 0, output=out[-500:], error=None if rc == 0 else "Upload failed: " + out[-200:])


def backup_watcher():
    """Every 10 minutes: tell the user when changes are waiting. Never uploads by itself."""
    last = None
    while True:
        time.sleep(600)
        try:
            s = git_status()
            n = s.get("changes") if s.get("configured") else None
            if n and n != last:
                with lock:
                    notify("pi", "info", f"Backup: {n} changed file(s) waiting. Approve the upload to GitHub in Settings (password needed).")
            last = n or None
        except Exception:
            pass


if __name__ == "__main__":
    if "--set-password" in sys.argv:
        import getpass
        p1 = getpass.getpass("New dashboard password (at least 8 characters): ")
        if p1 != getpass.getpass("Repeat it: ") or len(p1) < 8:
            sys.exit("Passwords differ or are shorter than 8 characters - nothing changed.")
        with open(_auth_path(), "w") as f: json.dump({"hash": generate_password_hash(p1), "set": now_str()}, f)
        os.chmod(_auth_path(), 0o600)
        print("Password saved. It applies immediately.")
        sys.exit(0)
    load_names()
    load_known_motors()
    threading.Thread(target=supervisor, daemon=True).start()
    threading.Thread(target=backup_watcher, daemon=True).start()
    app.run(host="0.0.0.0", port=5000, threaded=True)
