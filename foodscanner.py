#!/usr/bin/env python3
"""Überwacht die Betriebe in den eigenen foodsharing-Bezirken und meldet
neue Betriebe sowie Statusänderungen per Telegram.

Aufruf:
  foodscanner.py            normaler Lauf (für Cron)
  foodscanner.py --dry-run  nur anzeigen, nichts senden, State nicht speichern
  foodscanner.py --chat-id  zeigt Chat-IDs, die dem Bot geschrieben haben
  foodscanner.py --test     schickt eine Testnachricht an Telegram
  foodscanner.py --bot      beantwortet ~1 Minute lang Bot-Befehle wie /status (für Cron, jede Minute)
"""
import fcntl
import html
import json
import math
import os
import re
import sys
import time
from pathlib import Path

import requests

BASE = "https://foodsharing.de"  # per FOODSHARING_URL änderbar (z. B. https://foodsharing.at)
DIR = Path(__file__).resolve().parent
STATE_FILE = DIR / "state.json"
BOT_STATE_FILE = DIR / "bot.json"
BOT_LOCK_FILE = DIR / ".bot.lock"
ENV_FILE = DIR / ".env"

# Vorübergehende Fehler (foodsharing down, 502 …) erst melden, wenn sie so lange anhalten.
ERROR_ALERT_AFTER = 60 * 60
# Ab diesem Alter der letzten erfolgreichen Prüfung warnt /status.
STALE_AFTER = 45 * 60

# Bezirks-Typen (Region-Klassifikation), die als "eigener Bezirk" zählen:
# 1 Stadt, 2 Bezirk, 3 Region, 8 Großstadt, 9 Stadtteil.
# Bundesland (5), Land (6), Europa (10) usw. werden ignoriert.
DISTRICT_TYPES = {1, 2, 3, 8, 9}

COOPERATION = {
    0: "unklar",
    1: "kein Kontakt",
    2: "in Verhandlung",
    3: "bereit zu spenden",
    4: "will nicht kooperieren",
    5: "kooperiert",
    6: "spendet an Tafel o. Ä.",
    7: "existiert nicht mehr",
}
TEAM = {
    0: "geschlossen",
    1: "offen",
    2: "sucht Unterstützung",
}
TEAM_ICON = {0: "🔒", 1: "🟢", 2: "🆘"}
# Store-Kategorie: 0 Abholbetrieb, 1 Abgabestelle, 2 Orga-Betrieb (Einarbeitung, Putzdienst, Platzhalter …)
PICKUP_STORE = 0
# Namen, die trotz Kategorie "Abholbetrieb" keine sind (Testbetriebe, falsch einsortierte Abgabestellen)
EXCLUDED_NAME = re.compile(r"\btest|abgabestelle", re.IGNORECASE)


CONFIG_KEYS = (
    "FOODSHARING_EMAIL", "FOODSHARING_PASSWORD", "FOODSHARING_URL",
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
    "REGION_IDS", "MAX_DISTANCE_KM", "HOME_LAT", "HOME_LON",
    "EXCLUDE_IDS", "EXCLUDE_HOME_ONLY",
)


def load_env():
    """Liest .env; gesetzte Umgebungsvariablen haben Vorrang (z. B. für Docker/systemd)."""
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip().strip('"').strip("'")
    env.update({k: os.environ[k] for k in CONFIG_KEYS if os.environ.get(k)})
    return env


class Foodsharing:
    def __init__(self, email, password):
        self.info_cache = {}
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "foodscanner (private Benachrichtigung)"
        r = self.s.post(f"{BASE}/api/login", json={"email": email, "password": password}, timeout=30)
        if r.status_code == 401:
            raise RuntimeError("Login fehlgeschlagen: E-Mail oder Passwort falsch")
        if r.status_code == 403:
            raise RuntimeError("Login fehlgeschlagen: Konto verlangt einen 2FA-Code")
        r.raise_for_status()

    def get(self, path, **params):
        r = self.s.get(f"{BASE}{path}", params=params, timeout=60)
        r.raise_for_status()
        return r.json()

    def logout(self):
        try:
            self.s.post(f"{BASE}/api/logout", timeout=10)
        except requests.RequestException:
            pass

    def districts(self, region_ids=None):
        """Liefert ({region_id: name}, (lat, lon) der eigenen Adresse)."""
        details = self.get("/api/users/current/details")
        self.user_id = details["id"]
        regions = details.get("regions") or []
        coords = details.get("coordinates") or {}
        home = (coords["lat"], coords["lon"]) if coords.get("lat") and coords.get("lon") else None
        if region_ids:
            names = {r["id"]: r["name"] for r in regions}
            return {rid: names.get(rid, f"Bezirk {rid}") for rid in region_ids}, home
        return {r["id"]: r["name"] for r in regions if r.get("classification") in DISTRICT_TYPES}, home

    def stores_of_region(self, region_id):
        stores, offset = [], 0
        while True:
            page = self.get(f"/api/regions/{region_id}/stores", offset=offset, limit=1000)
            stores += page
            if len(page) < 1000:
                return stores
            offset += 1000

    def team_status(self):
        """Team-Status aller Betriebe über die Kartenfilter (2 Requests statt einer pro Betrieb)."""
        open_ids = {m["id"] for m in self.get("/api/map/markers/stores", help="open")}
        searching_ids = {m["id"] for m in self.get("/api/map/markers/stores", help="searching")}
        if not open_ids:
            raise RuntimeError("Kartenfilter 'offen' lieferte keine Betriebe – API geändert?")
        return open_ids, searching_ids

    def my_store_ids(self):
        """Betriebe, in deren Team man schon ist (inkl. Springer und offener Anfragen)."""
        return {str(st["id"]) for st in self.get(f"/api/users/{self.user_id}/stores")}

    def public_information(self, store_id):
        """Infotext aus der Karten-Sprechblase des Betriebs (None, wenn nicht abrufbar)."""
        if store_id not in self.info_cache:
            try:
                info = self.get(f"/api/map/markers/stores/{store_id}").get("publicInformation")
                self.info_cache[store_id] = (info or "").strip() or None
            except requests.RequestException:
                return None
        return self.info_cache[store_id]


def snapshot(fs, districts):
    open_ids, searching_ids = fs.team_status()
    stores = {}
    for rid, rname in districts.items():
        for st in fs.stores_of_region(rid):
            sid = st["id"]
            team = 2 if sid in searching_ids else 1 if sid in open_ids else 0
            stores[str(sid)] = {
                "name": (st.get("name") or "").strip(),
                "region": rname,
                "address": ", ".join(p for p in [st.get("street"), " ".join(filter(None, [st.get("zipCode"), st.get("city")]))] if p),
                "coop": st.get("cooperationStatus"),
                "team": team,
                "createdAt": st.get("createdAt"),
                "category": st.get("categoryType"),
                "lat": (st.get("location") or {}).get("lat"),
                "lon": (st.get("location") or {}).get("lon"),
            }
    return stores


def distance_km(home, st):
    if not home or st.get("lat") is None or st.get("lon") is None:
        return None
    lat1, lon1, lat2, lon2 = map(math.radians, (home[0], home[1], st["lat"], st["lon"]))
    a = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(a))


def describe(sid, st, fs, home):
    link = f"{BASE}/karte?bid={sid}"
    lines = [
        f'<b><a href="{link}">{html.escape(st["name"])}</a></b> ({html.escape(st["region"])})',
    ]
    parts = [html.escape(st["address"])] if st.get("address") else []
    km = distance_km(home, st)
    if km is not None:
        parts.append(f"📍 {km:.1f} km Luftlinie".replace(".", ","))
    if parts:
        lines.append(" · ".join(parts))
    lines.append(
        f'Status: {COOPERATION.get(st["coop"], st["coop"])} · Team: {TEAM_ICON[st["team"]]} {TEAM[st["team"]]}'
    )
    info = fs.public_information(sid)
    if info:
        if len(info) > 1000:
            info = info[:1000].rstrip() + " …"
        lines.append(f"<blockquote expandable>{html.escape(info)}</blockquote>")
    return "\n".join(lines)


def home_only_pattern(names):
    """Erkennt Sätze wie "Nur Foodsaver mit Stammbezirk Herne" oder "nur Herner Foodsaver"
    (aber nicht "Stammbezirk Herne bevorzugt")."""
    if not names:
        return None
    town = "(?:" + "|".join(re.escape(n) for n in names) + r")\w*"
    return re.compile(
        rf"\b(?:nur|ausschlie(?:ß|ss)lich)\b[^.!?\n]*?(?:stammbez\w*\s+{town}|{town}\s+(?:stammbez|foodsaver|saver))",
        re.IGNORECASE,
    )


def diff(old, new, fs, home, wanted):
    messages = []
    for sid, st in new.items():
        before = old.get(sid)
        if (before is None or before["coop"] != st["coop"] or before["team"] != st["team"]) and not wanted(sid):
            continue
        if before is None:
            messages.append("🆕 <b>Neuer Betrieb</b>\n" + describe(sid, st, fs, home))
            continue
        changes = []
        if before["coop"] != st["coop"]:
            changes.append(
                f'Kooperation: {COOPERATION.get(before["coop"], before["coop"])} → <b>{COOPERATION.get(st["coop"], st["coop"])}</b>'
            )
        if before["team"] != st["team"]:
            changes.append(
                f'Team: {TEAM_ICON[before["team"]]} {TEAM[before["team"]]} → {TEAM_ICON[st["team"]]} <b>{TEAM[st["team"]]}</b>'
            )
        if changes:
            head = "🔔 <b>Team öffnet!</b>" if before["team"] == 0 and st["team"] > 0 else "✏️ <b>Statusänderung</b>"
            messages.append(head + "\n" + describe(sid, st, fs, home) + "\n" + "\n".join(changes))
    for sid, st in old.items():
        if sid not in new:
            messages.append(
                f'🗑 <b>Betrieb nicht mehr gelistet</b>\n{html.escape(st["name"])} ({html.escape(st["region"])})'
            )
    return messages


def telegram(env, text):
    token, chat = env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        raise RuntimeError("TELEGRAM_BOT_TOKEN oder TELEGRAM_CHAT_ID fehlt in .env")
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
        timeout=30,
    )
    if not r.ok:
        raise RuntimeError(f"Telegram-Fehler {r.status_code}: {r.text[:200]}")


def send_all(env, messages, dry_run):
    # Telegram erlaubt max. 4096 Zeichen pro Nachricht – Meldungen bündeln.
    chunks, current = [], ""
    for m in messages:
        if current and len(current) + len(m) + 2 > 4000:
            chunks.append(current)
            current = ""
        current = f"{current}\n\n{m}" if current else m
    if current:
        chunks.append(current)
    for chunk in chunks:
        if dry_run:
            print(chunk + "\n" + "-" * 40)
        else:
            telegram(env, chunk)
            time.sleep(1)


def load_state(path=None):
    path = path or STATE_FILE
    if path.exists():
        return json.loads(path.read_text())
    return {}


def save_state(state, path=None):
    path = path or STATE_FILE
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1))
    os.replace(tmp, path)


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def log(msg):
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", file=sys.stderr)


def is_transient(e):
    """Netzwerkprobleme und 5xx-Antworten gehen meist von selbst weg."""
    if isinstance(e, (requests.ConnectionError, requests.Timeout)):
        return True
    return isinstance(e, requests.HTTPError) and e.response is not None and e.response.status_code >= 500


def fmt_time(iso):
    return time.strftime("%d.%m. %H:%M", time.strptime(iso, "%Y-%m-%dT%H:%M:%S"))


def age_seconds(iso):
    return time.time() - time.mktime(time.strptime(iso, "%Y-%m-%dT%H:%M:%S"))


def fmt_age(seconds):
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"vor {minutes} Min."
    if minutes < 48 * 60:
        return f"vor {minutes // 60} Std."
    return f"vor {minutes // (24 * 60)} Tagen"


def record_error(env, state, e, dry_run):
    """Merkt sich eine laufende Störung und meldet sie einmal – vorübergehende erst nach ERROR_ALERT_AFTER."""
    log(f"Fehler: {e}")
    if dry_run:
        return
    err = state.get("error") or {"since": now_iso(), "count": 0}
    err["count"] += 1
    err["message"] = str(e)
    if (not is_transient(e) or age_seconds(err["since"]) >= ERROR_ALERT_AFTER) and err.get("alerted") != str(e):
        try:
            telegram(
                env,
                f"⚠️ <b>foodscanner-Fehler</b> seit {fmt_time(err['since'])} ({err['count']} fehlgeschlagene Prüfungen):\n"
                f"{html.escape(str(e))}\n\nDu bekommst eine Nachricht, sobald es wieder läuft.",
            )
            err["alerted"] = str(e)
        except Exception as te:
            log(f"Telegram nicht erreichbar: {te}")
    state["error"] = err
    save_state(state)


def status_text(env):
    state = load_state()
    updated, err = state.get("updated"), state.get("error")
    if err:
        head = "⚠️ <b>foodscanner hat gerade Probleme</b>"
    elif not updated or age_seconds(updated) > STALE_AFTER:
        head = "⚠️ <b>foodscanner prüft nicht mehr</b> – läuft der Cronjob?"
    else:
        head = "✅ <b>foodscanner läuft</b>"
    lines = [head]
    if updated:
        lines.append(f"Letzte erfolgreiche Prüfung: {fmt_time(updated)} ({fmt_age(age_seconds(updated))})")
    if err:
        lines.append(
            f"Fehler seit {fmt_time(err['since'])} ({err['count']} fehlgeschlagene Prüfungen):\n{html.escape(err['message'])}"
        )
    if state.get("districts"):
        lines.append(f"Bezirke: {html.escape(', '.join(state['districts'].values()))}")
    if state.get("stores") is not None:
        radius = f" im Umkreis von {env['MAX_DISTANCE_KM']} km" if env.get("MAX_DISTANCE_KM") else ""
        watched = f"{len(state['stores'])} Betriebe"
        if "matching" in state:
            watched += f", davon {state['matching']} passende Abholbetriebe{radius}"
        lines.append(f"Überwacht: {watched}")
    last = state.get("last_message")
    lines.append(f"Letzte Meldung: {fmt_time(last) + ' (' + fmt_age(age_seconds(last)) + ')' if last else 'noch keine'}")
    return "\n".join(lines)


HELP_TEXT = (
    "🔎 <b>foodscanner</b> meldet sich automatisch, wenn in deinen Bezirken ein Betrieb neu dazukommt "
    "oder ein Team aufmacht.\n\n/status – läuft alles? Wann wurde zuletzt geprüft?"
)
BOT_COMMANDS = [{"command": "status", "description": "Läuft alles? Wann wurde zuletzt geprüft?"}]


def run_bot(env, seconds=55):
    """Beantwortet Befehle per Long-Polling. Läuft knapp eine Minute; Cron startet es jede Minute neu."""
    lock = open(BOT_LOCK_FILE, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return  # vorheriger Lauf ist noch aktiv
    token, chat = env.get("TELEGRAM_BOT_TOKEN"), str(env.get("TELEGRAM_CHAT_ID", ""))
    if not token or not chat:
        sys.exit("TELEGRAM_BOT_TOKEN oder TELEGRAM_CHAT_ID fehlt in .env")
    api = f"https://api.telegram.org/bot{token}"
    bot = load_state(BOT_STATE_FILE)
    try:
        if bot.get("commands") != BOT_COMMANDS:
            requests.post(f"{api}/setMyCommands", json={"commands": BOT_COMMANDS}, timeout=30).raise_for_status()
            bot["commands"] = BOT_COMMANDS
            save_state(bot, BOT_STATE_FILE)
        deadline = time.time() + seconds
        while (wait := int(deadline - time.time())) > 1:
            r = requests.get(
                f"{api}/getUpdates",
                params={"offset": bot.get("offset", 0), "timeout": wait, "allowed_updates": '["message"]'},
                timeout=wait + 15,
            )
            r.raise_for_status()
            for update in r.json().get("result", []):
                bot["offset"] = update["update_id"] + 1
                msg = update.get("message") or {}
                if str(msg.get("chat", {}).get("id")) != chat:
                    continue  # nur der eingetragene Chat darf den Bot abfragen
                command = (msg.get("text") or "").split(maxsplit=1)[0:1]
                command = command[0].split("@")[0].lower() if command else ""
                telegram(env, status_text(env) if command == "/status" else HELP_TEXT)
            save_state(bot, BOT_STATE_FILE)
    except (requests.RequestException, RuntimeError) as e:
        log(f"Bot-Fehler: {e}")


def show_chat_ids(env):
    token = env.get("TELEGRAM_BOT_TOKEN")
    if not token:
        sys.exit("TELEGRAM_BOT_TOKEN fehlt in .env")
    updates = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=30).json()
    chats = {}
    for u in updates.get("result", []):
        msg = u.get("message") or u.get("channel_post") or {}
        chat = msg.get("chat")
        if chat:
            chats[chat["id"]] = chat.get("username") or chat.get("title") or chat.get("first_name")
    if not chats:
        print("Keine Nachrichten gefunden – schreib dem Bot zuerst etwas (z. B. /start) und versuch es erneut.")
    for cid, name in chats.items():
        print(f"TELEGRAM_CHAT_ID={cid}   ({name})")


def main():
    global BASE
    args = set(sys.argv[1:])
    env = load_env()
    BASE = env.get("FOODSHARING_URL", BASE).rstrip("/")
    dry_run = "--dry-run" in args

    if "--chat-id" in args:
        return show_chat_ids(env)
    if "--test" in args:
        telegram(env, "✅ foodscanner: Telegram-Benachrichtigung funktioniert.")
        return print("Testnachricht gesendet.")
    if "--bot" in args:
        return run_bot(env)

    state = load_state()
    fs = None
    try:
        if not env.get("FOODSHARING_EMAIL") or not env.get("FOODSHARING_PASSWORD"):
            raise RuntimeError("FOODSHARING_EMAIL oder FOODSHARING_PASSWORD fehlt in .env")
        fs = Foodsharing(env["FOODSHARING_EMAIL"], env["FOODSHARING_PASSWORD"])
        region_ids = [int(x) for x in env.get("REGION_IDS", "").split(",") if x.strip()]
        districts, home = fs.districts(region_ids)
        if env.get("HOME_LAT") and env.get("HOME_LON"):
            home = (float(env["HOME_LAT"]), float(env["HOME_LON"]))
        stores = snapshot(fs, districts)
        if not stores:
            raise RuntimeError("Keine Betriebe gefunden – Abbruch, um Fehlalarme zu vermeiden")
    except Exception as e:
        if fs:
            fs.logout()
        record_error(env, state, e, dry_run)
        sys.exit(1)

    # Gemeldet werden nur Abholbetriebe ohne "Test"/"Abgabestelle" im Namen innerhalb von MAX_DISTANCE_KM
    # (Betriebe ohne Koordinaten immer), ohne EXCLUDE_IDS und ohne eigene Teams.
    # Gespeichert wird trotzdem alles, damit ein späteres Ändern der Filter keine Fehlalarme auslöst.
    max_km = float(env["MAX_DISTANCE_KM"].replace(",", ".")) if env.get("MAX_DISTANCE_KM") else None
    exclude_ids = {x.strip() for x in env.get("EXCLUDE_IDS", "").split(",") if x.strip()}
    try:
        exclude_ids |= fs.my_store_ids()
    except requests.RequestException as e:
        print(f"Eigene Betriebe nicht abrufbar: {e}", file=sys.stderr)
    home_only = home_only_pattern([x.strip() for x in env.get("EXCLUDE_HOME_ONLY", "").split(",") if x.strip()])

    def near(sid, st):
        if sid in exclude_ids:
            return False
        if st.get("category", PICKUP_STORE) != PICKUP_STORE or EXCLUDED_NAME.search(st["name"]):
            return False
        km = distance_km(home, st)
        return max_km is None or km is None or km <= max_km

    def wanted(sid):
        # Infotext wird nur für Betriebe geladen, die sonst gemeldet würden.
        return not (home_only and home_only.search(fs.public_information(sid) or ""))

    near_stores = {sid: st for sid, st in stores.items() if near(sid, st)}
    old = state.get("stores")
    if old is None:
        available = [
            (sid, st)
            for sid, st in sorted(near_stores.items(), key=lambda kv: (-kv[1]["team"], distance_km(home, kv[1]) or 0))
            if st["team"] > 0 and wanted(sid)
        ]
        searching = sum(1 for _, st in available if st["team"] == 2)
        radius = f" im Umkreis von {max_km:g} km".replace(".", ",") if max_km is not None else ""
        messages = [
            "👀 <b>foodscanner gestartet</b>\n"
            f"Bezirke: {html.escape(', '.join(districts.values()))}\n"
            f"{len(available)} passende Abholbetriebe{radius} mit offenem Team, davon {searching} suchen Unterstützung:"
        ] + [describe(sid, st, fs, home) for sid, st in available]
    else:
        near_old = {sid: st for sid, st in old.items() if near(sid, stores.get(sid, st))}
        messages = diff(near_old, near_stores, fs, home, wanted)
    fs.logout()

    notified = bool(messages)
    err = state.get("error")
    if err and err.get("alerted"):
        messages.insert(0, f"✅ <b>foodscanner läuft wieder</b> (Störung seit {fmt_time(err['since'])}, {err['count']} fehlgeschlagene Prüfungen)")
    if messages:
        send_all(env, messages, dry_run)
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {len(stores)} Betriebe, {len(messages)} Meldungen")

    if not dry_run:
        save_state({
            "stores": stores,
            "districts": districts,
            "matching": len(near_stores),
            "updated": now_iso(),
            "last_message": now_iso() if notified else state.get("last_message"),
        })


if __name__ == "__main__":
    main()
