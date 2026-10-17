"""Webinterface für den Cloudflared-LXC (Flask)."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import time
from functools import wraps

from flask import (Flask, Response, abort, flash, jsonify, redirect, render_template,
                   request, session, url_for)

import cfmanager as cf

AUTH_FILE = cf.CONF_DIR / "auth.json"
SECRET_FILE = cf.CONF_DIR / "secret.key"

app = Flask(__name__)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  PERMANENT_SESSION_LIFETIME=60 * 60 * 12)


def _write_private(path, data: bytes) -> None:
    """Schreibt eine Datei von Anfang an nur für den Besitzer lesbar (kein kurzes Zeitfenster mit 644)."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


def _secret() -> bytes:
    cf.CONF_DIR.mkdir(parents=True, exist_ok=True)
    if not SECRET_FILE.exists():
        _write_private(SECRET_FILE, secrets.token_bytes(32))
    return SECRET_FILE.read_bytes()


app.secret_key = _secret()


# ----------------------------------------------------------------- Passwort
def _hash(password: str, salt: bytes | None = None) -> dict:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 240_000)
    return {"salt": salt.hex(), "hash": digest.hex()}


def _load_auth() -> dict | None:
    try:
        return json.loads(AUTH_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _save_password(password: str) -> None:
    cf.CONF_DIR.mkdir(parents=True, exist_ok=True)
    _write_private(AUTH_FILE, json.dumps(_hash(password)).encode("utf-8"))


def _check_password(password: str) -> bool:
    auth = _load_auth()
    if not auth:
        return False
    test = _hash(password, bytes.fromhex(auth["salt"]))["hash"]
    return hmac.compare_digest(test, auth["hash"])


_FAILS: dict[str, list[float]] = {}


def _rate_limited(ip: str) -> bool:
    now = time.time()
    recent = [t for t in _FAILS.get(ip, []) if now - t < 300]
    _FAILS[ip] = recent
    return len(recent) >= 8


# ------------------------------------------------------------ Absicherung
def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not _load_auth():
            return redirect(url_for("setup"))
        if not session.get("auth"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def csrf_token() -> str:
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


@app.before_request
def _csrf_check():
    if request.method == "POST":
        sent = request.form.get("csrf") or request.headers.get("X-CSRF", "")
        if not sent or not hmac.compare_digest(sent, session.get("csrf", "")):
            abort(400, "Ungültiges Formular-Token. Seite neu laden.")


# Kennung dieses Prozesses: ändert sich bei jedem Neustart des Webinterface
BOOT_ID = f"{os.getpid()}-{time.time_ns()}"
RUNNING_VERSION = cf.app_version()  # Version des laufenden Codes (nicht der Datei auf der Platte)


@app.context_processor
def _inject():
    return {"csrf": csrf_token(), "app_version": RUNNING_VERSION, "boot": BOOT_ID}


@app.after_request
def _no_stale_cache(resp):
    # Seiten nie aus dem Browser-Zwischenspeicher zeigen; Dateien (CSS, Icons) immer prüfen lassen
    resp.headers["Cache-Control"] = "no-cache" if request.path.startswith("/static/") else "no-store"
    return resp


# ----------------------------------------------------------------- Anmeldung
@app.route("/setup", methods=["GET", "POST"])
def setup():
    if _load_auth():
        return redirect(url_for("login"))
    if request.method == "POST":
        pw, pw2 = request.form.get("password", ""), request.form.get("password2", "")
        if len(pw) < 8:
            flash("Das Passwort muss mindestens 8 Zeichen haben.", "error")
        elif pw != pw2:
            flash("Die Passwörter stimmen nicht überein.", "error")
        else:
            _save_password(pw)
            session["auth"] = True
            return redirect(url_for("index"))
    return render_template("setup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if not _load_auth():
        return redirect(url_for("setup"))
    if request.method == "POST":
        ip = request.remote_addr or "?"
        if _rate_limited(ip):
            flash("Zu viele Fehlversuche. Bitte 5 Minuten warten.", "error")
        elif _check_password(request.form.get("password", "")):
            session.clear()
            session["auth"] = True
            session.permanent = True
            nxt = request.args.get("next", "")
            safe = nxt.startswith("/") and not nxt.startswith("//") and "\\" not in nxt and "\n" not in nxt and "\r" not in nxt
            return redirect(nxt if safe else url_for("index"))
        else:
            _FAILS.setdefault(ip, []).append(time.time())
            flash("Falsches Passwort.", "error")
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/account", methods=["GET", "POST"])
@login_required
def account():
    if request.method == "POST":
        old = request.form.get("old", "")
        new, new2 = request.form.get("password", ""), request.form.get("password2", "")
        if not _check_password(old):
            flash("Das aktuelle Passwort ist falsch.", "error")
        elif len(new) < 8:
            flash("Das neue Passwort muss mindestens 8 Zeichen haben.", "error")
        elif new != new2:
            flash("Die neuen Passwörter stimmen nicht überein.", "error")
        else:
            _save_password(new)
            flash("Passwort geändert.", "ok")
            return redirect(url_for("account"))
    return render_template("account.html")


# --------------------------------------------------------------------- Seiten
@app.route("/")
@login_required
def index():
    s = cf.load_settings()
    return render_template(
        "index.html", s=s, status=cf.tunnel_status(), has_cert=cf.has_cert(),
        has_tunnel=cf.has_tunnel(), tunnel_id=cf.tunnel_id(),
        cloudflared=cf.cloudflared_version(), hostnames=cf.all_hostnames(s),
    )


@app.route("/api/status")
@login_required
def api_status():
    return jsonify({**cf.tunnel_status(), "app_version": RUNNING_VERSION, "boot": BOOT_ID})


@app.route("/tunnel/<action>", methods=["POST"])
@login_required
def tunnel_action(action):
    rc, out = cf.tunnel_control(action)
    flash(("Ausgeführt: " + action) if rc == 0 else f"Fehler: {out.strip()}", "ok" if rc == 0 else "error")
    return redirect(url_for("index"))


# ----------------------------------------------------------------- Cloudflare
@app.route("/cloudflare")
@login_required
def cloudflare():
    return render_template("cloudflare.html", has_cert=cf.has_cert(), has_tunnel=cf.has_tunnel(),
                           tunnel_id=cf.tunnel_id(), login=cf.login_status())


@app.route("/cloudflare/login/start", methods=["POST"])
@login_required
def cloudflare_login_start():
    return jsonify(cf.login_start())


@app.route("/cloudflare/login/status")
@login_required
def cloudflare_login_status():
    return jsonify(cf.login_status())


@app.route("/cloudflare/login/cancel", methods=["POST"])
@login_required
def cloudflare_login_cancel():
    cf.login_cancel()
    return redirect(url_for("cloudflare"))


@app.route("/cloudflare/logout", methods=["POST"])
@login_required
def cloudflare_logout():
    cf.logout()
    flash("Lokale Cloudflare-Anmeldung entfernt. Der Tunnel läuft weiter.", "ok")
    return redirect(url_for("cloudflare"))


@app.route("/cloudflare/forget", methods=["POST"])
@login_required
def cloudflare_forget():
    cf.systemctl("stop", cf.TUNNEL_SERVICE)
    cf.systemctl("disable", cf.TUNNEL_SERVICE)  # kein Neustart-Schleife nach Reboot ohne Tunnel
    cf.forget_tunnel()
    flash("Lokaler Tunnel vergessen. Beim nächsten Übernehmen wird ein neuer angelegt.", "ok")
    return redirect(url_for("cloudflare"))


# ----------------------------------------------------------------- Einstellungen
def _checkbox(name: str) -> bool:
    return request.form.get(name) == "on"


def settings_from_form() -> dict:
    s = cf.load_settings()
    s["tunnel_name"] = request.form.get("tunnel_name", "").strip()
    s["main"] = {
        "hostname": request.form.get("main_hostname", "").strip().lower(),
        "service": cf.normalize_service(request.form.get("main_service", "")),
        "no_tls_verify": _checkbox("main_notls"),
        "label": request.form.get("main_label", "").strip()[:60],
        "enabled": _checkbox("main_enabled"),
    }
    indexes = sorted({int(m.group(1)) for k in request.form if (m := re.match(r"hosts-([0-9]{1,4})-", k))})
    hosts = []
    for i in indexes:
        name = request.form.get(f"hosts-{i}-hostname", "").strip().lower()
        service = cf.normalize_service(request.form.get(f"hosts-{i}-service", ""))
        if not name and not service:
            continue  # leere Zeilen ignorieren
        hosts.append({"hostname": name, "service": service,
                      "label": request.form.get(f"hosts-{i}-label", "").strip()[:60],
                      "enabled": _checkbox(f"hosts-{i}-enabled"),
                      "no_tls_verify": _checkbox(f"hosts-{i}-notls"),
                      "disable_chunked": _checkbox(f"hosts-{i}-chunked")})
    s["hosts"] = hosts
    s["catch_all"] = cf.normalize_service(request.form.get("catch_all", ""))
    s["post_quantum"] = _checkbox("post_quantum")
    s["log_level"] = request.form.get("log_level", "info")
    s["run_parameters"] = [p.strip() for p in request.form.get("run_parameters", "").splitlines() if p.strip()]
    s["github_repo"] = request.form.get("github_repo", "").strip()
    return s


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    if request.method == "POST":
        s = settings_from_form()
        action = request.form.get("action", "save")
        errors = cf.validate_settings(s)
        if errors and action != "save_draft":
            for e in errors:
                flash(e, "error")
            return render_template("settings.html", s=s, log_levels=cf.LOG_LEVELS)
        if action == "save":
            s["pending"] = True
        cf.save_settings(s)
        if action == "save":
            flash("Gespeichert. Noch nicht auf den Tunnel angewendet.", "ok")
            return redirect(url_for("settings"))
        dry = action == "dry"
        cf.start_job("apply", "Probelauf" if dry else "Übernehmen",
                     lambda job: cf.apply_settings(s, job, dry_run=dry))
        return redirect(url_for("job_page", name="apply"))
    return render_template("settings.html", s=cf.load_settings(), log_levels=cf.LOG_LEVELS)


@app.route("/favicon.ico")
def favicon():
    return app.send_static_file("favicon.svg")


@app.route("/domains/toggle", methods=["POST"])
@login_required
def domain_toggle():
    """Domain aktivieren/deaktivieren (Haken auf der Übersicht). Speichert nur, wirkt erst nach dem Neustart."""
    data = request.get_json(silent=True) or {}
    key, enabled = data.get("key"), bool(data.get("enabled"))
    s = cf.load_settings()
    if key == "main" and s["main"].get("hostname"):
        target = s["main"]
    elif isinstance(key, int) and not isinstance(key, bool) and 0 <= key < len(s["hosts"]):
        target = s["hosts"][key]
    else:
        return jsonify({"ok": False, "error": "Eintrag nicht gefunden"}), 404
    target["enabled"] = enabled
    s["pending"] = True
    cf.save_settings(s)
    name = target.get("label") or target.get("hostname")
    return jsonify({"ok": True, "name": name, "enabled": enabled})


@app.route("/settings/apply-saved", methods=["POST"])
@login_required
def apply_saved():
    """Wendet die gespeicherten Einstellungen an (Konfiguration schreiben, Tunnel neu starten)."""
    s = cf.load_settings()
    cf.start_job("apply", "Übernehmen", lambda job: cf.apply_settings(s, job))
    return redirect(url_for("job_page", name="apply"))


@app.route("/backup")
@login_required
def backup():
    return render_template("backup.html")


@app.route("/settings/export")
@login_required
def settings_export():
    payload = json.dumps(cf.export_settings(cf.load_settings()), indent=2, ensure_ascii=False)
    name = "cloudflared-einstellungen-" + time.strftime("%Y%m%d") + ".json"
    return Response(payload, mimetype="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.route("/settings/import", methods=["POST"])
@login_required
def settings_import():
    upload = request.files.get("file")
    if not upload or not upload.filename:
        flash("Bitte eine Sicherungsdatei auswählen.", "error")
        return redirect(url_for("backup"))
    blob = upload.read(200_001)
    if len(blob) > 200_000:
        flash("Die Datei ist zu groß für eine Sicherungsdatei.", "error")
        return redirect(url_for("backup"))
    try:
        raw = json.loads(blob.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        flash("Die Datei konnte nicht gelesen werden (kein gültiges JSON).", "error")
        return redirect(url_for("backup"))
    new, errors = cf.import_settings(raw, cf.load_settings())
    if new is None:
        for e in errors:
            flash("Import abgebrochen: " + e, "error")
        return redirect(url_for("backup"))
    cf.save_settings(new)
    flash("Einstellungen importiert und gespeichert. Prüfe sie und klicke dann auf „Speichern & übernehmen“, "
          "damit sie am Tunnel wirken.", "ok")
    return redirect(url_for("settings"))


# ------------------------------------------------------------------- Aufträge
JOB_NAMES = ("apply", "update")

@app.route("/job/<name>")
@login_required
def job_page(name):
    if name not in JOB_NAMES:
        abort(404)
    return render_template("job.html", name=name)


@app.route("/api/job/<name>")
@login_required
def api_job(name):
    if name not in JOB_NAMES:
        abort(404)
    job = cf.get_job(name)
    return jsonify(job.snapshot() if job else {"title": "", "state": "none", "lines": []})


# ---------------------------------------------------------------------- Logs
@app.route("/logs")
@login_required
def logs():
    return render_template("logs.html", tunnel=cf.tunnel_logs(300))


# ------------------------------------------------------------------- Updates
@app.route("/updates")
@login_required
def updates():
    # Seite sofort anzeigen; die Versionsprüfung läuft danach im Browser (/api/versions?fresh=1)
    return render_template("updates.html", repo=cf.load_settings()["github_repo"],
                           cf_version=cf.cloudflared_version())


@app.route("/api/versions")
@login_required
def api_versions():
    return jsonify(cf.versions_cached(fresh=request.args.get("fresh") == "1"))


@app.route("/updates/cloudflared", methods=["POST"])
@login_required
def update_cloudflared():
    cf._VERSIONS_CACHE["data"] = None
    cf.start_job("update", "cloudflared aktualisieren", cf.update_cloudflared)
    return redirect(url_for("job_page", name="update"))


@app.route("/updates/app", methods=["POST"])
@login_required
def update_app():
    repo = cf.load_settings()["github_repo"]
    cf._VERSIONS_CACHE["data"] = None
    cf.start_job("update", "Webinterface aktualisieren", lambda job: cf.update_app(job, repo))
    return redirect(url_for("job_page", name="update"))


if __name__ == "__main__":
    app.run(host=os.environ.get("CFWEBUI_HOST", "0.0.0.0"), port=int(os.environ.get("CFWEBUI_PORT", "8099")))
