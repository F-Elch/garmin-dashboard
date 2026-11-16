#!/usr/bin/env python3
"""Garmin-Dashboard – Update-Skript.

Holt Garmin-Daten inkrementell (Bibliothek ``garminconnect``), speichert sie in
``cache/``, rechnet die aufwendigen Modelle und erzeugt ``garmin-dashboard.html``.

    python update.py            # normaler Lauf (nur neue Tage laden)
    python update.py --offline  # nichts laden, nur HTML aus dem Cache neu bauen
    python update.py --demo     # synthetische Daten -> garmin-dashboard-demo.html

Zugangsdaten stehen ausschließlich in ``.env`` (siehe ``.env.example``). Sie werden
nie ausgegeben, nie in den Cache und nie in die HTML geschrieben. Nach dem Bauen
durchsucht das Skript die HTML nach allen Werten aus ``.env`` und den Token-Dateien
und löscht die Datei, falls etwas gefunden wird.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import logging
import math
import os
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CACHE_DIR = ROOT / "cache"
TOKEN_DIR = ROOT / ".garmin_tokens"
ENV_FILE = ROOT / ".env"
TEMPLATE = ROOT / "template.html"
OUT = ROOT / "garmin-dashboard.html"
OUT_DEMO = ROOT / "garmin-dashboard-demo.html"

# ---------------------------------------------------------------- Einstellungen
START_DATE = dt.date(2024, 6, 1)
REFRESH_DAYS = 3          # die letzten n Tage immer neu laden (Garmin rechnet nach)
PAUSE_S = 0.25            # Pause zwischen Tagen, um Garmin nicht zu fluten

# Fertige HTML zusätzlich hierhin kopieren (z. B. privater OneDrive-Ordner fürs Handy).
# In .env mit DASHBOARD_COPY_TO=... überschreibbar, leer = keine Kopie.
COPY_TO = Path.home() / "OneDrive" / "Privat" / "Garmin"
LOG_FILE = ROOT / "update.log"

# Cloud-Modus (GitHub Actions): verschlüsselter Zustand + verschlüsselte Seite
STATE_FILE = ROOT / "state.enc"          # Cache + Garmin-Tokens, AES-verschlüsselt
SITE_DIR = ROOT / "site"                 # wird auf GitHub Pages veröffentlicht
CLOUD_TOKENS = ROOT / "cloud_tokens.json"
PAGE_SALT = b"garmin-dashboard/page/v1"  # fest, damit "Passwort merken" über Updates hinweg gilt
PAGE_ITER = 600_000                      # PBKDF2-SHA256 wie im Browser
STATE_ITER = 300_000
MIN_PASSWORD = 12

# Persönliche Einstellungen (Zielgewicht, Klausuren) stehen NICHT im Code, damit das Repository
# öffentlich sein kann: lokal in personal.json (siehe personal.example.json), in GitHub Actions im
# Secret DASHBOARD_CONFIG (gleicher JSON-Inhalt).
PERSONAL_FILE = ROOT / "personal.json"


def _load_personal() -> dict:
    raw = os.environ.get("DASHBOARD_CONFIG", "").strip()
    if not raw and PERSONAL_FILE.exists():
        raw = PERSONAL_FILE.read_text(encoding="utf-8")
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        sys.exit("personal.json bzw. DASHBOARD_CONFIG ist kein gültiges JSON.")


_PERSONAL = _load_personal()
TARGET_KG = float(_PERSONAL["target_kg"]) if _PERSONAL.get("target_kg") else None  # Zielgewicht
EXAMS = sorted(str(d)[:10] for d in _PERSONAL.get("exams", []))                      # Klausurtage

# Schulferien NRW laut Ferienordnung (BASS 12-61 Nr. 1), erster bis letzter Ferientag.
# Wochenenden direkt davor/danach werden im Dashboard mitmarkiert.
HOLIDAYS = [
    ("Osterferien", "2024-03-25", "2024-04-06"),
    ("Pfingstferien", "2024-05-21", "2024-05-21"),
    ("Sommerferien", "2024-07-08", "2024-08-20"),
    ("Herbstferien", "2024-10-14", "2024-10-26"),
    ("Weihnachtsferien", "2024-12-23", "2025-01-06"),
    ("Osterferien", "2025-04-14", "2025-04-26"),
    ("Pfingstferien", "2025-06-10", "2025-06-10"),
    ("Sommerferien", "2025-07-14", "2025-08-26"),
    ("Herbstferien", "2025-10-13", "2025-10-25"),
    ("Weihnachtsferien", "2025-12-22", "2026-01-06"),
    ("Osterferien", "2026-03-30", "2026-04-11"),
    ("Pfingstferien", "2026-05-26", "2026-05-26"),
    ("Sommerferien", "2026-07-20", "2026-09-01"),
    ("Herbstferien", "2026-10-17", "2026-10-31"),
    ("Weihnachtsferien", "2026-12-23", "2027-01-06"),
    ("Osterferien", "2027-03-22", "2027-04-03"),
    ("Pfingstferien", "2027-05-18", "2027-05-18"),
    ("Sommerferien", "2027-07-19", "2027-08-31"),
    ("Herbstferien", "2027-10-23", "2027-11-06"),
    ("Weihnachtsferien", "2027-12-24", "2028-01-08"),
]

log = logging.getLogger("garmin-dashboard")


# ======================================================================= Hilfen
def iso(d: dt.date) -> str:
    return d.isoformat()


def parse(s: str) -> dt.date:
    return dt.date.fromisoformat(s[:10])


def daterange(a: dt.date, b: dt.date):
    d = a
    while d <= b:
        yield d
        d += dt.timedelta(days=1)


def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def sd(xs):
    xs = [x for x in xs if x is not None]
    if len(xs) < 2:
        return None
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def rnd(x, n=2):
    return None if x is None else round(x, n)


def load_env() -> dict:
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    for k in ("GARMIN_EMAIL", "GARMIN_PASSWORD"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def read_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(p: Path, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(p)


# ===================================================================== Abholen
FAIL = object()


def call(fn, *args, tries=3):
    """Ruft die API auf. Gibt FAIL bei Fehler zurück (Tag wird dann nicht gecacht)."""
    for attempt in range(1, tries + 1):
        try:
            return fn(*args)
        except Exception as e:  # noqa: BLE001
            name = type(e).__name__
            if "Authentication" in name:
                raise
            msg = str(e)
            wait = 60 * attempt if ("429" in msg or "Too Many" in msg) else 2 * attempt
            log.warning("  %s(%s): %s – neuer Versuch in %ss", fn.__name__, ",".join(args), name, wait)
            time.sleep(wait)
    return FAIL


def _get(o, *keys):
    for k in keys:
        if not isinstance(o, dict):
            return None
        o = o.get(k)
    return o


def _clock(ms, bedtime=False):
    if not ms:
        return None
    t = dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc)  # *Local-Stempel = Ortszeit
    h = t.hour + t.minute / 60
    if bedtime and h < 15:
        h += 24  # 00:30 -> 24,5, damit Mittelwerte über Mitternacht stimmen
    return round(h, 3)


def norm_day(ds, us, sl, hv) -> dict:
    us = us if isinstance(us, dict) else {}
    sl = sl if isinstance(sl, dict) else {}
    hv = hv if isinstance(hv, dict) else {}
    dto = sl.get("dailySleepDTO") or sl
    r = {"d": ds}
    r["steps"] = us.get("totalSteps")
    r["rhr"] = us.get("restingHeartRate")
    st = us.get("averageStressLevel")
    r["stress"] = st if isinstance(st, (int, float)) and st >= 0 else None
    r["bbMax"] = us.get("bodyBatteryHighestValue")
    r["bbMin"] = us.get("bodyBatteryLowestValue")
    r["bbCh"] = us.get("bodyBatteryChargedValue")
    r["bbDr"] = us.get("bodyBatteryDrainedValue")
    r["kcalA"] = us.get("activeKilocalories")
    mod, vig = us.get("moderateIntensityMinutes"), us.get("vigorousIntensityMinutes")
    r["im"] = (mod or 0) + 2 * (vig or 0) if (mod is not None or vig is not None) else None
    r["spo2"] = us.get("averageSpo2") or dto.get("averageSpO2Value")
    sec = dto.get("sleepTimeSeconds")
    if sec:
        r["slDur"] = rnd(sec / 3600)
        for k, src in (("slDeep", "deepSleepSeconds"), ("slRem", "remSleepSeconds"),
                       ("slLight", "lightSleepSeconds"), ("slAwake", "awakeSleepSeconds")):
            r[k] = rnd((dto.get(src) or 0) / 3600)
        r["slStart"] = _clock(dto.get("sleepStartTimestampLocal"), bedtime=True)
        r["slEnd"] = _clock(dto.get("sleepEndTimestampLocal"))
    r["slScore"] = _get(dto, "sleepScores", "overall", "value")
    need = _get(dto, "sleepNeed", "actual")
    r["slNeed"] = rnd(need / 60) if need else None
    r["resp"] = dto.get("averageRespirationValue") or us.get("avgWakingRespirationValue")
    skin = dto.get("avgSkinTempDeviationC", sl.get("avgSkinTempDeviationC"))
    r["skinT"] = skin
    hs = hv.get("hrvSummary") or {}
    r["hrv"] = hs.get("lastNightAvg") or sl.get("avgOvernightHrv")
    r["hrv7"] = hs.get("weeklyAvg")
    r["hrvLo"] = _get(hs, "baseline", "balancedLow")
    r["hrvHi"] = _get(hs, "baseline", "balancedUpper")
    return {k: v for k, v in r.items() if v is not None}


def norm_act(a: dict) -> dict:
    t = str(a.get("startTimeLocal") or "")
    z = [round((a.get(f"hrTimeInZone_{i}") or 0) / 60, 1) for i in range(1, 6)]
    r = {
        "id": a.get("activityId"),
        "d": t[:10],
        "t": t[11:16],
        "type": _get(a, "activityType", "typeKey") or "other",
        "name": (a.get("activityName") or "").replace("​", ""),
        "dur": rnd((a.get("duration") or 0) / 60, 1),
        "hr": a.get("averageHR"),
        "hrMax": a.get("maxHR"),
        "kcal": a.get("calories"),
        "z": z,
        "dist": rnd((a.get("distance") or 0) / 1000, 3),
        "sets": a.get("activeSets") or a.get("totalSets"),
        "reps": a.get("totalReps"),
    }
    return {k: v for k, v in r.items() if v not in (None, "")}


def norm_weights(resp) -> dict:
    out = {}
    for w in (resp or {}).get("dateWeightList") or []:
        d = w.get("calendarDate")
        if not d and w.get("date"):
            d = iso(dt.datetime.fromtimestamp(w["date"] / 1000, dt.timezone.utc).date())
        if not d or not w.get("weight"):
            continue
        out.setdefault(d, []).append(w)
    res = {}
    for d, ws in out.items():
        kg = mean([x["weight"] / 1000 for x in ws])
        fat = mean([x.get("bodyFat") for x in ws])
        mus = mean([(x.get("muscleMass") or 0) / 1000 or None for x in ws])
        res[d] = {k: v for k, v in {"d": d, "kg": rnd(kg), "fat": rnd(fat, 1), "muscle": rnd(mus)}.items() if v is not None}
    return res


def connect(env, auto=False):
    try:
        from garminconnect import Garmin
    except ImportError:
        sys.exit("Bibliothek fehlt. Bitte einmal ausführen:  python -m pip install garminconnect")
    # Die Bibliothek protokolliert Netzwerkfehler mit kompletten Tracebacks; call() meldet sie knapp selbst.
    logging.getLogger("garminconnect").setLevel(logging.CRITICAL)
    email, pw = env.get("GARMIN_EMAIL") or None, env.get("GARMIN_PASSWORD") or None

    def mfa():
        if auto:  # geplanter Lauf: niemand kann den Code eingeben
            raise RuntimeError("Garmin verlangt einen Bestätigungscode – bitte update.bat einmal von Hand starten.")
        return input("Bestätigungscode (MFA) von Garmin: ").strip()

    g = Garmin(email, pw, prompt_mfa=mfa)
    try:
        g.login(str(TOKEN_DIR))
    except Exception as e:  # noqa: BLE001
        sys.exit(f"Anmeldung bei Garmin fehlgeschlagen ({type(e).__name__}). "
                 "Bitte GARMIN_EMAIL und GARMIN_PASSWORD in .env prüfen.")
    return g


def fetch(g, cache):
    days, acts, weights = cache["days"], cache["acts"], cache["weights"]
    today = dt.date.today()
    todo = [d for d in daterange(START_DATE, today)
            if iso(d) not in days or (today - d).days < REFRESH_DAYS]
    log.info("Tageswerte: %d Tage zu laden (%d im Cache)", len(todo), len(days))
    fails = 0
    for i, d in enumerate(todo, 1):
        ds = iso(d)
        us = call(g.get_user_summary, ds)
        sl = call(g.get_sleep_data, ds)
        hv = call(g.get_hrv_data, ds)
        if FAIL in (us, sl, hv):
            fails += 1
        else:
            days[ds] = norm_day(ds, us, sl, hv)
        if i % 10 == 0 or i == len(todo):
            log.info("  %d/%d  (%s)", i, len(todo), ds)
        if i % 25 == 0:
            save_cache(cache)
        time.sleep(PAUSE_S)
    if fails:
        log.warning("%d Tage konnten nicht geladen werden – werden beim nächsten Lauf erneut versucht.", fails)

    last_act = max((a["d"] for a in acts.values()), default=None)
    a_start = max(START_DATE, parse(last_act) - dt.timedelta(days=REFRESH_DAYS)) if last_act else START_DATE
    log.info("Aktivitäten ab %s …", a_start)
    res = call(g.get_activities_by_date, iso(a_start), iso(today))
    if res is not FAIL:
        for a in res or []:
            n = norm_act(a)
            if n.get("id"):
                acts[str(n["id"])] = n

    last_w = max(weights.keys(), default=None)
    w_start = max(START_DATE, parse(last_w) - dt.timedelta(days=14)) if last_w else START_DATE
    log.info("Gewicht ab %s …", w_start)
    a = w_start
    while a <= today:
        b = min(a + dt.timedelta(days=180), today)
        res = call(g.get_body_composition, iso(a), iso(b))
        if res is not FAIL:
            weights.update(norm_weights(res))
        a = b + dt.timedelta(days=1)


def load_cache():
    return {
        "days": read_json(CACHE_DIR / "days.json", {}),
        "acts": read_json(CACHE_DIR / "activities.json", {}),
        "weights": read_json(CACHE_DIR / "weights.json", {}),
    }


def save_cache(c):
    write_json(CACHE_DIR / "days.json", c["days"])
    write_json(CACHE_DIR / "activities.json", c["acts"])
    write_json(CACHE_DIR / "weights.json", c["weights"])


# ================================================================== Demo-Daten
def demo_cache(seed=7):
    rnd_ = random.Random(seed)
    end = dt.date.today() - dt.timedelta(days=1)
    hol = holiday_set()
    exams = set(EXAMS)
    days, acts, weights = {}, {}, {}
    hrv_state, rhr_state, fit = 0.0, 0.0, 0.0
    kg = 68.0
    aid = 1
    load_prev = 0
    for d in daterange(START_DATE, end):
        ds = iso(d)
        wd = d.weekday()
        holiday = ds in hol
        exam_next = iso(d + dt.timedelta(days=1)) in exams
        # Training
        trimp = 0
        if rnd_.random() < (0.55 if wd in (0, 2, 4, 5) else 0.15):
            z = [rnd_.uniform(25, 50), rnd_.uniform(8, 25), rnd_.uniform(1, 8), rnd_.uniform(0, 3), 0]
            dur = sum(z) + rnd_.uniform(10, 30)
            acts[str(aid)] = {"id": aid, "d": ds, "t": "17:%02d" % rnd_.randint(0, 59), "type": "strength_training",
                              "name": "Krafttraining", "dur": round(dur, 1), "hr": rnd_.randint(100, 120),
                              "hrMax": rnd_.randint(150, 175), "kcal": int(dur * 5.5), "z": [round(x, 1) for x in z],
                              "sets": rnd_.randint(18, 34), "reps": rnd_.randint(150, 300)}
            aid += 1
            trimp += sum((i + 1) * x for i, x in enumerate(z))
        if rnd_.random() < 0.04:
            z = [3, 8, 14, 6, 1]
            km = rnd_.uniform(4, 7)
            acts[str(aid)] = {"id": aid, "d": ds, "t": "08:10", "type": "running", "name": "Laufen",
                              "dur": round(km * 6.2, 1), "hr": rnd_.randint(150, 165), "hrMax": 182,
                              "kcal": int(km * 70), "z": z, "dist": round(km, 2)}
            aid += 1
            trimp += sum((i + 1) * x for i, x in enumerate(z))
        fit = fit * 0.98 + trimp * 0.0008
        hrv_state = 0.75 * hrv_state + rnd_.gauss(0, 3) - 0.05 * load_prev - (6 if exam_next else 0)
        rhr_state = 0.75 * rhr_state + rnd_.gauss(0, 1.2) + 0.02 * load_prev + (2 if exam_next else 0)
        season = 3 * math.sin((d.timetuple().tm_yday / 365) * 2 * math.pi) + (6 if d >= dt.date(2025, 9, 1) else 0)
        late = (wd >= 4 or holiday)
        bed = 23.2 + (1.0 if late else 0) + rnd_.gauss(0, 0.5) + (0.4 if exam_next else 0)
        wake = (6.4 if not late else 8.6) + rnd_.gauss(0, 0.4)
        dur = max(4.5, wake + 24 - bed - rnd_.uniform(0.1, 0.4))
        deep = dur * rnd_.uniform(0.13, 0.22)
        rem = dur * rnd_.uniform(0.12, 0.24)
        awake = rnd_.uniform(0.05, 0.4)
        light = dur - deep - rem
        hrv = 62 + hrv_state + season + fit + 2 * (dur - 7.5)
        rhr = 55 + rhr_state - 0.6 * fit - 0.8 * (dur - 7.5)
        score = max(30, min(98, 75 + 6 * (dur - 7.5) + 0.4 * (hrv - 62) + rnd_.gauss(0, 6)))
        days[ds] = {
            "d": ds, "steps": int(max(1500, rnd_.gauss(7500 if not holiday else 6000, 2500))),
            "rhr": round(rhr), "stress": int(max(10, rnd_.gauss(32 + (10 if exam_next else 0), 7))),
            "bbMax": int(min(100, max(20, 40 + 6 * dur + rnd_.gauss(0, 8)))), "bbMin": rnd_.randint(5, 30),
            "bbCh": rnd_.randint(30, 70), "bbDr": rnd_.randint(30, 75), "kcalA": int(300 + trimp * 2 + rnd_.gauss(0, 120)),
            "im": int(trimp / 6), "spo2": rnd_.randint(93, 97), "slDur": round(dur, 2), "slDeep": round(deep, 2),
            "slRem": round(rem, 2), "slLight": round(light, 2), "slAwake": round(awake, 2),
            "slStart": round(bed, 3), "slEnd": round(wake, 3), "slScore": int(score),
            "slNeed": round(7.75 + rnd_.uniform(0, 0.75), 2), "resp": round(15 + rnd_.gauss(0, 0.6) + (0.8 if exam_next else 0), 1),
            "hrv": round(max(20, hrv)), "hrv7": None, "hrvLo": 52, "hrvHi": 74,
        }
        if rnd_.random() < 0.08:
            del days[ds]["hrv"]
        load_prev = trimp
        kg += 0.012 + rnd_.gauss(0, 0.02)
        if rnd_.random() < 0.35:
            weights[ds] = {"d": ds, "kg": round(kg + rnd_.gauss(0, 0.45), 2),
                           "fat": round(14 + rnd_.gauss(0, 0.8), 1), "muscle": round(kg * 0.47 + rnd_.gauss(0, 0.3), 2)}
    for ds, r in days.items():  # 7-Tage-HRV wie Garmin
        i = parse(ds)
        r["hrv7"] = rnd(mean([days.get(iso(i - dt.timedelta(days=k)), {}).get("hrv") for k in range(7)]), 0)
        days[ds] = {k: v for k, v in r.items() if v is not None}
    return {"days": days, "acts": acts, "weights": weights}


# ================================================================== Kontext
def holiday_set():
    s = set()
    for _, a, b in HOLIDAYS:
        a, b = parse(a), parse(b)
        # Wochenende direkt davor/danach mitzählen
        while (a - dt.timedelta(days=1)).weekday() >= 5:
            a -= dt.timedelta(days=1)
        while (b + dt.timedelta(days=1)).weekday() >= 5:
            b += dt.timedelta(days=1)
        for d in daterange(a, b):
            s.add(iso(d))
    return s


# ======================================================== Daten zusammenführen
def build_days(cache):
    days = cache["days"]
    if not days:
        sys.exit("Keine Tageswerte im Cache. Erst `python update.py` mit Zugang laufen lassen (oder --demo).")
    first = max(START_DATE, parse(min(days)))
    last = parse(max(days))
    trimp, n_act, n_str = {}, {}, {}
    for a in cache["acts"].values():
        d = a.get("d")
        z = a.get("z") or [0] * 5
        trimp[d] = trimp.get(d, 0) + sum((i + 1) * (x or 0) for i, x in enumerate(z))
        n_act[d] = n_act.get(d, 0) + 1
        if "strength" in a.get("type", ""):
            n_str[d] = n_str.get(d, 0) + 1
    hol = holiday_set()
    exams = set(EXAMS)
    out = []
    for d in daterange(first, last):
        ds = iso(d)
        r = dict(days.get(ds) or {"d": ds})
        r["trimp"] = round(trimp.get(ds, 0), 1)
        if n_act.get(ds):
            r["nAct"] = n_act[ds]
        if n_str.get(ds):
            r["nStr"] = n_str[ds]
        if ds in hol:
            r["hol"] = 1
        if ds in exams:
            r["exam"] = 1
        out.append(r)
    return out


# ================================================================ Lineare Algebra
def solve(A, b):
    n = len(A)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(M[r][c]))
        M[c], M[p] = M[p], M[c]
        if abs(M[c][c]) < 1e-12:
            continue
        for r in range(n):
            if r != c:
                f = M[r][c] / M[c][c]
                if f:
                    for k in range(c, n + 1):
                        M[r][k] -= f * M[c][k]
    return [M[i][n] / M[i][i] if abs(M[i][i]) > 1e-12 else 0.0 for i in range(n)]


def ridge_fit(X, y, lam=10.0):
    p = len(X[0])
    mu = [mean([row[j] for row in X]) or 0.0 for j in range(p)]
    sg = [sd([row[j] for row in X]) or 1.0 for j in range(p)]
    Z = [[0.0 if row[j] is None else (row[j] - mu[j]) / sg[j] for j in range(p)] for row in X]
    ym = sum(y) / len(y)
    A = [[sum(z[i] * z[j] for z in Z) + (lam if i == j else 0) for j in range(p)] for i in range(p)]
    c = [sum(z[i] * (yy - ym) for z, yy in zip(Z, y)) for i in range(p)]
    return {"mu": mu, "sd": sg, "w": solve(A, c), "ym": ym}


def ridge_pred(m, x):
    return m["ym"] + sum(w * (0.0 if v is None else (v - mu) / s)
                         for w, v, mu, s in zip(m["w"], x, m["mu"], m["sd"]))


# ===================================================================== Modelle
def back_mean(vals, i, w, minn=None):
    xs = [v for v in vals[max(0, i - w + 1): i + 1] if v is not None]
    return sum(xs) / len(xs) if xs and len(xs) >= (minn or max(1, w // 2)) else None


def next_day_model(days, key):
    other = "rhr" if key == "hrv" else "hrv"
    V = {k: [d.get(k) for d in days] for k in (key, other, "trimp", "slDur", "slScore", "stress", "resp")}
    names = [f"{'HRV' if key == 'hrv' else 'Ruhepuls'} heute", "7-Tage-Mittel", f"{'Ruhepuls' if key == 'hrv' else 'HRV'} heute",
             "Last heute (TRIMP)", "Last 7 Tage", "Schlafdauer letzte Nacht", "Schlafscore", "Ø Stress heute",
             "Atemfrequenz", "Wochenende morgen", "Ferien morgen", "Klausur morgen"]

    def feats(t):
        nd = parse(days[t]["d"]) + dt.timedelta(days=1)
        nds = iso(nd)
        return [V[key][t], back_mean(V[key], t, 7), V[other][t], V["trimp"][t], back_mean(V["trimp"], t, 7, 1),
                V["slDur"][t], V["slScore"][t], V["stress"][t], V["resp"][t],
                1.0 if nd.weekday() >= 5 else 0.0,
                1.0 if (t + 1 < len(days) and days[t + 1].get("hol")) or nds in holiday_set_cached() else 0.0,
                1.0 if nds in EXAMS else 0.0]

    rows = []
    for t in range(len(days) - 1):
        y = V[key][t + 1]
        if y is None or V[key][t] is None:
            continue
        rows.append((days[t + 1]["d"], feats(t), y, V[key][t], back_mean(V[key], t, 7, 3)))
    res = {"names": names, "n": len(rows)}
    if len(rows) < 90:
        res["error"] = "Zu wenige Daten (mind. 90 Nächte nötig)."
        return res
    min_train = 60
    model = None
    preds = []
    for i in range(min_train, len(rows)):
        if model is None or (i - min_train) % 7 == 0:
            model = ridge_fit([r[1] for r in rows[:i]], [r[2] for r in rows[:i]])
        d, x, y, n1, n7 = rows[i]
        preds.append((d, y, ridge_pred(model, x), n1, n7 if n7 is not None else n1))
    mae = lambda k: sum(abs(p[1] - p[k]) for p in preds) / len(preds)  # noqa: E731
    resid = [p[1] - p[2] for p in preds]
    rmse = math.sqrt(sum(r * r for r in resid) / len(resid))
    final = ridge_fit([r[1] for r in rows], [r[2] for r in rows])
    t_last = max(i for i in range(len(days)) if V[key][i] is not None)
    x_last = feats(t_last)
    coefs = sorted(({"name": n, "w": round(w, 3)} for n, w in zip(names, final["w"])), key=lambda c: -abs(c["w"]))
    res.update({
        "target": iso(parse(days[t_last]["d"]) + dt.timedelta(days=1)),
        "pred": round(ridge_pred(final, x_last), 1),
        "pi": round(1.96 * rmse, 1),
        "maeModel": round(mae(2), 2), "maeNaive1": round(mae(3), 2), "maeNaive7": round(mae(4), 2),
        "nTest": len(preds), "testFrom": preds[0][0],
        "coefs": coefs,
        "series": [[p[0], p[1], round(p[2], 1), p[3]] for p in preds[-180:]],
        "lambda": 10,
    })
    return res


_HOL = None


def holiday_set_cached():
    global _HOL
    if _HOL is None:
        _HOL = holiday_set()
    return _HOL


def holt_winters(days, key, horizon=7, m=7):
    y = [d.get(key) for d in days]
    obs = [v for v in y if v is not None]
    if len(obs) < 60:
        return {"error": "Zu wenige Daten."}

    def run(a, b, g, collect=False):
        first = [v for v in y[:28] if v is not None]
        level = sum(first) / len(first)
        trend = 0.0
        season = [0.0] * m
        cnt = [0] * m
        for i, v in enumerate(y[:28]):
            if v is not None:
                season[i % m] += v - level
                cnt[i % m] += 1
        season = [s / c if c else 0.0 for s, c in zip(season, cnt)]
        sse, errs = 0.0, []
        for i, v in enumerate(y):
            f = level + trend + season[i % m]
            if v is None:
                level, trend = level + trend, trend
                continue
            e = v - f
            if i >= 28:
                sse += e * e
                errs.append((i, e, f))
            lv = a * (v - season[i % m]) + (1 - a) * (level + trend)
            trend = b * (lv - level) + (1 - b) * trend
            season[i % m] = g * (v - lv) + (1 - g) * season[i % m]
            level = lv
        return sse, errs, (level, trend, season)

    best = None
    for a in (0.05, 0.1, 0.2, 0.3, 0.5):
        for b in (0.0, 0.01, 0.05):
            for g in (0.0, 0.05, 0.1, 0.2):
                sse = run(a, b, g)[0]
                if best is None or sse < best[0]:
                    best = (sse, a, b, g)
    _, a, b, g = best
    _, errs, (level, trend, season) = run(a, b, g)
    s = math.sqrt(sum(e * e for _, e, _ in errs) / len(errs))
    n = len(y)
    last = parse(days[-1]["d"])
    fc = []
    for h in range(1, horizon + 1):
        f = level + h * trend + season[(n - 1 + h) % m]
        w = 1.96 * s * math.sqrt(1 + (h - 1) * a * a)
        fc.append([iso(last + dt.timedelta(days=h)), round(f, 1), round(f - w, 1), round(f + w, 1)])
    # Vergleich letzte 180 Tage: Modell vs. 7-Tage-Mittel
    recent = [(i, e, f) for i, e, f in errs if i >= n - 180]
    mae_m = sum(abs(e) for _, e, _ in recent) / len(recent)
    naive = []
    for i, e, f in recent:
        nm = back_mean(y, i - 1, 7, 3)
        if nm is not None:
            naive.append(abs(y[i] - nm))
    return {"params": [a, b, g], "fc": fc, "maeModel": round(mae_m, 2),
            "maeNaive7": round(sum(naive) / len(naive), 2) if naive else None, "sd": round(s, 2)}


def kalman_weight(days, weights):
    if len(weights) < 8:
        return {"error": "Zu wenige Wiegungen (mind. 8)."}
    first = parse(min(weights))
    last = max(parse(days[-1]["d"]), parse(max(weights)))
    dates = list(daterange(first, last))
    ys = [weights.get(iso(d), {}).get("kg") for d in dates]

    def run(R, ql, qs, keep=False):
        y0 = next(v for v in ys if v is not None)
        x = [y0, 0.0]
        P = [[1.0, 0.0], [0.0, 0.01]]
        ll = 0.0
        hist = []
        for y in ys:
            # Vorhersage
            xp = [x[0] + x[1], x[1]]
            Pp = [[P[0][0] + 2 * P[0][1] + P[1][1] + ql, P[0][1] + P[1][1]],
                  [P[0][1] + P[1][1], P[1][1] + qs]]
            if y is not None:
                S = Pp[0][0] + R
                v = y - xp[0]
                ll += -0.5 * (math.log(S) + v * v / S)
                K0, K1 = Pp[0][0] / S, Pp[1][0] / S
                x = [xp[0] + K0 * v, xp[1] + K1 * v]
                P = [[(1 - K0) * Pp[0][0], (1 - K0) * Pp[0][1]],
                     [Pp[1][0] - K1 * Pp[0][0], Pp[1][1] - K1 * Pp[0][1]]]
            else:
                x, P = xp, Pp
            if keep:
                hist.append((xp, Pp, x, P, y))
        return ll, hist

    best = None
    for R in (0.1, 0.25, 0.5, 1.0):
        for ql in (1e-4, 1e-3, 1e-2):
            for qs in (1e-7, 1e-6, 1e-5):
                ll = run(R, ql, qs)[0]
                if best is None or ll > best[0]:
                    best = (ll, R, ql, qs)
    _, R, ql, qs = best
    _, hist = run(R, ql, qs, keep=True)
    # Ein-Schritt-Fehler vs. naiv (letzte Wiegung)
    errs_m, errs_n, prev = [], [], None
    for xp, Pp, x, P, y in hist:
        if y is not None:
            if prev is not None:
                errs_m.append(abs(y - xp[0]))
                errs_n.append(abs(y - prev))
            prev = y
    # RTS-Glättung
    n = len(hist)
    xs = [h[2][:] for h in hist]
    Ps = [[r[:] for r in h[3]] for h in hist]
    for t in range(n - 2, -1, -1):
        xf, Pf = hist[t][2], hist[t][3]
        xp, Pp = hist[t + 1][0], hist[t + 1][1]
        FP = [[Pf[0][0] + Pf[1][0], Pf[0][1] + Pf[1][1]], [Pf[1][0], Pf[1][1]]]  # F*Pf
        PfFt = [[FP[0][0], FP[1][0]], [FP[0][1], FP[1][1]]]  # Pf*F^T
        det = Pp[0][0] * Pp[1][1] - Pp[0][1] * Pp[1][0]
        inv = [[Pp[1][1] / det, -Pp[0][1] / det], [-Pp[1][0] / det, Pp[0][0] / det]]
        C = [[sum(PfFt[i][k] * inv[k][j] for k in range(2)) for j in range(2)] for i in range(2)]
        dx = [xs[t + 1][0] - xp[0], xs[t + 1][1] - xp[1]]
        xs[t] = [xf[0] + C[0][0] * dx[0] + C[0][1] * dx[1], xf[1] + C[1][0] * dx[0] + C[1][1] * dx[1]]
        dP = [[Ps[t + 1][i][j] - Pp[i][j] for j in range(2)] for i in range(2)]
        CdP = [[sum(C[i][k] * dP[k][j] for k in range(2)) for j in range(2)] for i in range(2)]
        Ps[t] = [[Pf[i][j] + sum(CdP[i][k] * C[j][k] for k in range(2)) for j in range(2)] for i in range(2)]
    trend = [[iso(d), round(x[0], 2), round(math.sqrt(max(P[0][0], 0)), 2), round(x[1] * 7, 3)]
             for d, x, P in zip(dates, xs, Ps)]
    L, S_ = hist[-1][2]
    P = hist[-1][3]
    fc = []
    for h in range(1, 181):
        mu = L + S_ * h
        var = P[0][0] + 2 * h * P[0][1] + h * h * P[1][1] + h * ql + qs * h * (h + 1) * (2 * h + 1) / 6
        fc.append([iso(last + dt.timedelta(days=h)), round(mu, 2), round(1.96 * math.sqrt(var), 2),
                   round(1.96 * math.sqrt(var + R), 2)])
    s_sd = math.sqrt(max(P[1][1], 0))
    eta = {"level": round(L, 2), "slopeWeek": round(S_ * 7, 3), "slopeWeekCI": round(1.96 * s_sd * 7, 3)}
    if TARGET_KG is None:
        eta["status"] = "kein Ziel"
    elif L >= TARGET_KG:
        eta["status"] = "erreicht"
    else:
        def days_for(s):
            return (TARGET_KG - L) / s if s > 1e-5 else None
        mid, fast, slow = days_for(S_), days_for(S_ + 1.96 * s_sd), days_for(S_ - 1.96 * s_sd)
        eta["status"] = "unterwegs" if mid else "kein Fortschritt"
        for k, v in (("date", mid), ("early", fast), ("late", slow)):
            eta[k] = iso(last + dt.timedelta(days=round(v))) if v and v < 3650 else None
    return {"params": {"R": R, "qLevel": ql, "qSlope": qs}, "trend": trend, "fc": fc, "eta": eta,
            "maeModel": round(mean(errs_m), 2) if errs_m else None,
            "maeNaive": round(mean(errs_n), 2) if errs_n else None, "target": TARGET_KG}


def change_points(days, key, min_weeks=4, thresh=4.5, max_cp=6):
    # Wochenmittel (Mo-So)
    weeks = {}
    for d in days:
        v = d.get(key)
        if v is None:
            continue
        dd = parse(d["d"])
        wk = iso(dd - dt.timedelta(days=dd.weekday()))
        weeks.setdefault(wk, []).append(v)
    wk = sorted((k, sum(v) / len(v)) for k, v in weeks.items() if len(v) >= 3)
    if len(wk) < 3 * min_weeks:
        return []
    found = []

    def seg(a, b, depth):
        if len(found) >= max_cp or b - a < 2 * min_weeks:
            return
        xs = [v for _, v in wk[a:b]]
        best = None
        for k in range(min_weeks, len(xs) - min_weeks + 1):
            l, r = xs[:k], xs[k:]
            m1, m2 = sum(l) / len(l), sum(r) / len(r)
            pooled = math.sqrt((sum((x - m1) ** 2 for x in l) + sum((x - m2) ** 2 for x in r)) / (len(xs) - 2) or 1e-9)
            t = abs(m1 - m2) / (pooled * math.sqrt(1 / len(l) + 1 / len(r)))
            if best is None or t > best[0]:
                best = (t, k, m1, m2)
        if best and best[0] > thresh:
            t, k, m1, m2 = best
            found.append({"d": wk[a + k][0], "before": round(m1, 1), "after": round(m2, 1), "t": round(t, 1)})
            seg(a, a + k, depth + 1)
            seg(a + k, b, depth + 1)

    seg(0, len(wk), 0)
    return sorted(found, key=lambda c: c["d"])


def compute_models(days, weights):
    t0 = time.time()
    m = {
        "run": dt.datetime.now().isoformat(timespec="minutes"),
        "hrv": next_day_model(days, "hrv"),
        "rhr": next_day_model(days, "rhr"),
        "sleepScore": holt_winters(days, "slScore", 7),
        "weight": kalman_weight(days, weights),
        "cp": {"hrv": change_points(days, "hrv"), "rhr": change_points(days, "rhr")},
    }
    log.info("Modelle gerechnet in %.1f s", time.time() - t0)
    return m


# ===================================================================== Ausgabe
def collect_secrets(env) -> list[str]:
    secrets = {v for v in env.values() if v and len(v) >= 4}
    for k in ("DASHBOARD_PASSWORD", "GARMIN_TOKENS"):
        if os.environ.get(k):
            secrets.add(os.environ[k].strip())
    if os.environ.get("GARMIN_TOKENS"):
        try:
            secrets.update(v for v in json.loads(os.environ["GARMIN_TOKENS"]).values() if isinstance(v, str) and len(v) >= 16)
        except (ValueError, AttributeError):
            pass

    def walk(o):
        if isinstance(o, str):
            if len(o) >= 16:
                secrets.add(o)
        elif isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    for base in (TOKEN_DIR, Path.home() / ".config" / "garmin"):
        files = [base] if base.is_file() else (list(base.rglob("*")) if base.is_dir() else [])
        for f in files:
            if not f.is_file() or f.stat().st_size > 1_000_000:
                continue
            try:
                txt = f.read_text(encoding="utf-8", errors="ignore").strip()
            except OSError:
                continue
            try:
                walk(json.loads(txt))
            except ValueError:
                if len(txt) >= 16:
                    secrets.add(txt)
    return sorted(secrets)


# ============================================================ Verschlüsselung
def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _aesgcm():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        sys.exit("Für die Verschlüsselung fehlt eine Bibliothek:  python -m pip install cryptography")
    return AESGCM


def encrypt_blob(data: bytes, password: str, salt: bytes, iters: int) -> dict:
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iters, 32)
    iv = os.urandom(12)
    ct = _aesgcm()(key).encrypt(iv, data, None)  # Ciphertext || Tag, wie WebCrypto
    return {"v": 1, "iter": iters, "salt": _b64(salt), "iv": _b64(iv), "ct": _b64(ct)}


def decrypt_blob(obj: dict, password: str) -> bytes:
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), base64.b64decode(obj["salt"]), obj["iter"], 32)
    return _aesgcm()(key).decrypt(base64.b64decode(obj["iv"]), base64.b64decode(obj["ct"]), None)


def render(days, cache, models, out: Path, env, demo=False, password=None):
    acts = sorted(cache["acts"].values(), key=lambda a: (a.get("d", ""), a.get("t", "")))
    data = {
        "generated": dt.datetime.now().isoformat(timespec="minutes"),
        "demo": demo,
        "start": days[0]["d"], "end": days[-1]["d"],
        "target": TARGET_KG,
        "days": days,
        "acts": [{k: v for k, v in a.items() if k != "id"} for a in acts if a.get("d", "") >= days[0]["d"]],
        "weights": sorted(cache["weights"].values(), key=lambda w: w["d"]),
        "holidays": [{"name": n, "from": a, "to": b} for n, a, b in HOLIDAYS],
        "exams": EXAMS,
        "models": models,
    }
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = TEMPLATE.read_text(encoding="utf-8")
    if "__DATA__" not in html:
        sys.exit("template.html enthält keinen Platzhalter __DATA__.")
    secrets = collect_secrets(env)
    if any(s in payload for s in secrets):
        sys.exit("ABBRUCH: Zugangswerte wären in der HTML gelandet. Datei wurde NICHT geschrieben.")
    if password:
        blob = encrypt_blob(payload.encode("utf-8"), password, PAGE_SALT, PAGE_ITER)
        payload = json.dumps({"enc": blob}, separators=(",", ":"))
    html = html.replace("__DATA__", payload)
    leaks = [s for s in secrets if s in html]
    if leaks:
        sys.exit(f"ABBRUCH: {len(leaks)} Zugangswert(e) wären in der HTML gelandet. Datei wurde NICHT geschrieben.")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    log.info("Fertig: %s (%.0f KB) – Zugangsdaten-Prüfung: sauber", out.name, out.stat().st_size / 1024)
    return out


def copy_out(out: Path, env):
    target = env.get("DASHBOARD_COPY_TO", None)
    target = Path(os.path.expandvars(target)).expanduser() if target is not None else COPY_TO
    if not str(target).strip() or str(target) == ".":
        return
    try:
        target.mkdir(parents=True, exist_ok=True)
        tmp = target / (out.name + ".part")
        tmp.write_bytes(out.read_bytes())
        tmp.replace(target / out.name)
        log.info("Kopie abgelegt: %s", target / out.name)
    except OSError as e:
        log.warning("Kopie nach %s fehlgeschlagen: %s", target, type(e).__name__)


def main():
    ap = argparse.ArgumentParser(description="Garmin-Dashboard aktualisieren")
    ap.add_argument("--offline", action="store_true", help="nichts laden, nur aus dem Cache bauen")
    ap.add_argument("--demo", action="store_true", help="synthetische Daten (ohne Login)")
    ap.add_argument("--auto", action="store_true", help="geplanter Lauf: keine Rückfragen, Protokoll in update.log")
    ap.add_argument("--cloud", action="store_true", help="Lauf in GitHub Actions (verschlüsselter Zustand und Seite)")
    ap.add_argument("--cloud-login", action="store_true", help="eigene Garmin-Tokens für die Cloud erzeugen")
    ap.add_argument("--encrypt", action="store_true", help="HTML mit Passwort verschlüsseln (DASHBOARD_PASSWORD oder Abfrage)")
    args = ap.parse_args()
    if args.cloud_login:
        logging.basicConfig(level=logging.INFO, format="%(message)s")
        return cloud_login()
    if args.cloud:
        logging.basicConfig(level=logging.INFO, format="%(message)s")
        return run_cloud()
    handlers = [logging.StreamHandler()]
    if args.auto:
        handlers.append(logging.FileHandler(LOG_FILE, encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s" if args.auto else "%(message)s",
                        datefmt="%d.%m.%Y %H:%M", handlers=handlers)
    if args.auto:
        _trim_log()
        log.info("—— geplanter Lauf ——")
        try:
            run(args)
        except SystemExit as e:
            if e.code not in (None, 0):
                log.error("FEHLER: %s", e.code)
            raise
        except Exception as e:  # noqa: BLE001
            log.error("FEHLER: %s: %s", type(e).__name__, e)
            raise SystemExit(1)
    else:
        run(args)


def _trim_log(max_lines=2000):
    try:
        lines = LOG_FILE.read_text(encoding="utf-8").splitlines()
        if len(lines) > max_lines:
            LOG_FILE.write_text("\n".join(lines[-max_lines // 2:]) + "\n", encoding="utf-8")
    except OSError:
        pass


def run(args):
    env = load_env()
    if args.demo:
        cache = demo_cache()
        out = OUT_DEMO
    else:
        cache = load_cache()
        if not args.offline:
            if not (env.get("GARMIN_EMAIL") and env.get("GARMIN_PASSWORD")) and not TOKEN_DIR.exists():
                sys.exit("Keine Zugangsdaten: .env.example nach .env kopieren und ausfüllen.")
            g = connect(env, auto=args.auto)
            try:
                fetch(g, cache)
            except KeyboardInterrupt:
                log.warning("Abgebrochen – bisher geladene Tage werden gespeichert.")
            finally:
                save_cache(cache)
        out = OUT
    days = build_days(cache)
    models = compute_models(days, cache["weights"])
    password = ask_password() if args.encrypt else None
    out = render(days, cache, models, out, env, demo=args.demo, password=password)
    if not args.demo:
        copy_out(out, env)


def ask_password(confirm=False) -> str:
    pw = os.environ.get("DASHBOARD_PASSWORD")
    if not pw:
        import getpass
        pw = getpass.getpass("Dashboard-Passwort: ")
        if confirm and getpass.getpass("Wiederholen: ") != pw:
            sys.exit("Passwörter stimmen nicht überein.")
    if len(pw) < MIN_PASSWORD:
        sys.exit(f"Passwort zu kurz (mindestens {MIN_PASSWORD} Zeichen, besser 4–5 zufällige Wörter).")
    return pw


# ================================================================== Cloud
def cloud_login():
    """Eigene Token-Sitzung für GitHub Actions erzeugen (unabhängig von den PC-Tokens)."""
    env = load_env()
    if not (env.get("GARMIN_EMAIL") and env.get("GARMIN_PASSWORD")):
        sys.exit("GARMIN_EMAIL und GARMIN_PASSWORD in .env eintragen.")
    from garminconnect import Garmin
    logging.getLogger("garminconnect").setLevel(logging.CRITICAL)
    g = Garmin(env["GARMIN_EMAIL"], env["GARMIN_PASSWORD"],
               prompt_mfa=lambda: input("Bestätigungscode (MFA) von Garmin: ").strip())
    try:
        g.login()
    except Exception as e:  # noqa: BLE001
        sys.exit(f"Anmeldung fehlgeschlagen ({type(e).__name__}).")
    CLOUD_TOKENS.write_text(g.client.dumps(), encoding="utf-8")
    log.info("Fertig: %s erzeugt.\n"
             "1. Datei öffnen, gesamten Inhalt kopieren.\n"
             "2. GitHub → dein Repository → Settings → Secrets and variables → Actions →\n"
             "   New repository secret, Name: GARMIN_TOKENS, Inhalt einfügen, speichern.\n"
             "3. Danach %s löschen.", CLOUD_TOKENS.name, CLOUD_TOKENS.name)


def run_cloud():
    pw = os.environ.get("DASHBOARD_PASSWORD", "")
    if len(pw) < MIN_PASSWORD:
        sys.exit(f"Secret DASHBOARD_PASSWORD fehlt oder ist kürzer als {MIN_PASSWORD} Zeichen.")
    secret_tokens = os.environ.get("GARMIN_TOKENS", "").strip()
    cache = {"days": {}, "acts": {}, "weights": {}}
    state_tokens = None
    if STATE_FILE.exists():
        try:
            st = json.loads(decrypt_blob(json.loads(STATE_FILE.read_text(encoding="utf-8")), pw))
            cache, state_tokens = st["cache"], st.get("tokens")
            log.info("Gespeicherter Zustand geladen (%d Tage).", len(cache["days"]))
        except Exception:  # noqa: BLE001
            log.warning("Gespeicherter Zustand ließ sich nicht öffnen (Passwort geändert?) – lade alles neu.")
    else:
        log.info("Kein gespeicherter Zustand – erster Lauf lädt alle Tage (dauert ca. 20–40 Minuten).")

    from garminconnect import Garmin
    logging.getLogger("garminconnect").setLevel(logging.CRITICAL)

    def no_mfa():
        raise RuntimeError("MFA verlangt")

    g = None
    TOKEN_DIR.mkdir(exist_ok=True)
    for source, tok in (("gespeicherte", state_tokens), ("Secret-", secret_tokens)):
        if not tok:
            continue
        (TOKEN_DIR / "garmin_tokens.json").write_text(tok, encoding="utf-8")
        try:
            cand = Garmin(prompt_mfa=no_mfa)
            cand.login(str(TOKEN_DIR))
            g = cand
            log.info("Bei Garmin angemeldet (%sTokens).", source)
            break
        except Exception as e:  # noqa: BLE001
            log.warning("Anmeldung mit %sTokens fehlgeschlagen (%s).", source, type(e).__name__)
    if g is None:
        sys.exit("Keine gültigen Garmin-Tokens. Am PC `python update.py --cloud-login` ausführen "
                 "und das Secret GARMIN_TOKENS neu setzen.")
    try:
        fetch(g, cache)
    finally:
        tokens = g.client.dumps()
        state = json.dumps({"cache": cache, "tokens": tokens}, separators=(",", ":")).encode("utf-8")
        STATE_FILE.write_text(json.dumps(encrypt_blob(state, pw, os.urandom(16), STATE_ITER)), encoding="utf-8")
        log.info("Zustand verschlüsselt gespeichert.")
    days = build_days(cache)
    models = compute_models(days, cache["weights"])
    render(days, cache, models, SITE_DIR / "index.html", {}, password=pw)
    (SITE_DIR / ".nojekyll").write_text("", encoding="utf-8")


if __name__ == "__main__":
    main()
