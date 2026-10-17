"""Kernlogik für den Cloudflared-LXC.

Ablauf:
  1. Anmeldung bei Cloudflare (cloudflared tunnel login -> cert.pem)
  2. Tunnel anlegen (cloudflared tunnel create)
  3. Ingress-Konfiguration bauen (Hauptdomain, Subdomains, Fangregel)
  4. DNS-Einträge setzen (cloudflared tunnel route dns -f)
  5. Tunnel als systemd-Dienst betreiben

Dazu kommen Status, Logs und Updates (cloudflared per apt, Oberfläche per GitHub).
Es werden nur die Python-Standardbibliotheken benutzt.
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import threading
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
STATE_DIR = Path(os.environ.get("CFWEBUI_STATE", "/var/lib/cloudflared-webui"))
CONF_DIR = Path(os.environ.get("CFWEBUI_CONF", "/etc/cloudflared-webui"))
CLOUDFLARED = os.environ.get("CLOUDFLARED_BIN", "cloudflared")
NO_SYSTEMD = os.environ.get("CFWEBUI_NO_SYSTEMD") == "1"
TUNNEL_SERVICE = "cloudflared-tunnel"
WEBUI_SERVICE = "cloudflared-webui"
METRICS_ADDR = "127.0.0.1:36500"

SETTINGS_FILE = CONF_DIR / "settings.json"
CERT_FILE = STATE_DIR / "cert.pem"
CREDS_FILE = STATE_DIR / "tunnel.json"
CONFIG_FILE = STATE_DIR / "config.json"
CONFIG_LAST_GOOD = STATE_DIR / "config.last-good.json"
ENV_FILE = STATE_DIR / "tunnel.env"

LOG_LEVELS = ["trace", "debug", "info", "notice", "warning", "error", "fatal"]
ALLOWED_RUN_PARAMS = re.compile(
    r"^(--edge-bind-address|--edge-ip-version|--grace-period|--ha-connections|"
    r"--logfile|--loglevel|--pidfile|--protocol|--region|--retries|--tag)=[A-Za-z0-9_.:,/@\-]+$"
)

# Prüfung der Hostnamen
HOST_RE = re.compile(
    r"^(([a-z0-9äöüß]|[a-z0-9äöüß][a-z0-9äöüß\-]*[a-z0-9äöüß])\.)*"
    r"([a-z0-9]|[a-z0-9][a-z0-9\-]*[a-z0-9])$"
)
SERVICE_RE = re.compile(
    r"^(?:(?:https?|tcp|ssh|rdp)://[^\s/:]+(?::\d{1,5})?(?:/\S*)?"
    r"|unix:\S+|http_status:\d{3}|hello_world|bastion)$"
)
BARE_TARGET_RE = re.compile(r"^[A-Za-z0-9.\-]+(?::\d{1,5})?$")
TUNNEL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,62}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+$")

DEFAULTS: dict = {
    "tunnel_name": "proxmox-tunnel",
    # Hauptdomain: bleibt wie sie ist, nur das Ziel (IP) ist einstellbar
    "main": {"hostname": "", "service": "", "no_tls_verify": True, "label": "", "enabled": True},
    # Subdomains: hostname -> service
    "hosts": [],
    # leer = http_status:404
    "catch_all": "",
    "post_quantum": False,
    "log_level": "info",
    "run_parameters": [],
    "github_repo": "Ronny-1979/cloudflared-lxc",
    # True = es gibt gespeicherte Änderungen, die noch nicht auf den Tunnel angewendet wurden
    "pending": False,
}


# --------------------------------------------------------------------------
# Einstellungen
# --------------------------------------------------------------------------
def load_settings() -> dict:
    data: dict = {}
    if SETTINGS_FILE.exists():
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
    merged = json.loads(json.dumps(DEFAULTS))
    for key, value in data.items():
        if key == "main" and isinstance(value, dict):
            merged["main"].update(value)
        else:
            merged[key] = value
    return merged


def save_settings(settings: dict) -> None:
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(settings, indent=2, ensure_ascii=False), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(SETTINGS_FILE)


EXPORT_MAGIC = "cloudflared-lxc-settings"


def export_settings(s: dict) -> dict:
    """Einstellungen für eine Sicherungsdatei. Enthält keine Passwörter oder Cloudflare-Zugangsdaten."""
    data = {k: s[k] for k in ("tunnel_name", "main", "hosts", "catch_all", "post_quantum", "log_level",
                              "run_parameters") if k in s}
    return {"format": EXPORT_MAGIC, "format_version": 1, "app_version": app_version(),
            "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"), "settings": data}


def import_settings(raw: dict, current: dict) -> tuple[dict | None, list[str]]:
    """Liest eine Sicherungsdatei. Nur bekannte Felder werden übernommen und auf den Typ geprüft.
    'github_repo' wird bewusst NICHT importiert (sonst könnte eine fremde Datei die Update-Quelle ändern)."""
    if not isinstance(raw, dict) or raw.get("format") != EXPORT_MAGIC or not isinstance(raw.get("settings"), dict):
        return None, ["Das ist keine gültige Sicherungsdatei dieses Programms."]
    src = raw["settings"]
    s = json.loads(json.dumps(current))

    def text(value, limit=253) -> str:
        return value.strip()[:limit] if isinstance(value, str) else ""

    if "tunnel_name" in src:
        s["tunnel_name"] = text(src["tunnel_name"], 63)
    main = src.get("main")
    if isinstance(main, dict):
        s["main"] = {"hostname": text(main.get("hostname")).lower(), "service": normalize_service(text(main.get("service"), 500)),
                     "no_tls_verify": bool(main.get("no_tls_verify", True)), "label": text(main.get("label"), 60),
                     "enabled": bool(main.get("enabled", True))}
    hosts = src.get("hosts")
    if isinstance(hosts, list):
        s["hosts"] = [
            {"hostname": text(h.get("hostname")).lower(), "service": normalize_service(text(h.get("service"), 500)),
             "label": text(h.get("label"), 60), "no_tls_verify": bool(h.get("no_tls_verify", True)),
             "disable_chunked": bool(h.get("disable_chunked", False)), "enabled": bool(h.get("enabled", True))}
            for h in hosts[:200] if isinstance(h, dict)
        ]
    if "catch_all" in src:
        s["catch_all"] = normalize_service(text(src["catch_all"], 500))
    if "post_quantum" in src:
        s["post_quantum"] = bool(src["post_quantum"])
    if src.get("log_level") in LOG_LEVELS:
        s["log_level"] = src["log_level"]
    if isinstance(src.get("run_parameters"), list):
        s["run_parameters"] = [text(p, 100) for p in src["run_parameters"][:20] if isinstance(p, str) and p.strip()]
    errors = validate_settings(s)
    return (None, errors) if errors else (s, [])


def normalize_service(value: str) -> str:
    """'192.168.1.5:8123' -> 'http://192.168.1.5:8123'"""
    value = (value or "").strip()
    if not value:
        return ""
    if BARE_TARGET_RE.match(value):
        return "http://" + value
    return value


def validate_settings(s: dict) -> list[str]:
    """Gibt eine Liste deutscher Fehlermeldungen zurück (leer = in Ordnung)."""
    errors: list[str] = []

    if not TUNNEL_NAME_RE.match(s.get("tunnel_name", "")):
        errors.append("Tunnelname: nur Buchstaben, Zahlen, '-' und '_' erlaubt.")

    seen: set[str] = set()

    main = s.get("main", {})
    main_host = (main.get("hostname") or "").strip()
    main_service = normalize_service(main.get("service", ""))
    if main_host or main_service:
        if not HOST_RE.match(main_host):
            errors.append(
                f"Hauptdomain '{main_host}' ist ungültig (ohne https:// und ohne Port, nur Kleinbuchstaben)."
            )
        elif not main_service:
            errors.append("Hauptdomain: Bitte ein Ziel eintragen, z. B. 192.168.178.20:8123.")
        elif not SERVICE_RE.match(main_service):
            errors.append(f"Hauptdomain: Ziel '{main_service}' ist ungültig.")
        seen.add(main_host)

    for index, host in enumerate(s.get("hosts", []), start=1):
        name = (host.get("hostname") or "").strip()
        service = normalize_service(host.get("service", ""))
        label = f"Subdomain {index}"
        if not name and not service:
            errors.append(f"{label}: Hostname und Ziel sind leer.")
            continue
        if not name:
            errors.append(f"{label}: Hostname fehlt (Ziel {service}).")
        elif not HOST_RE.match(name):
            errors.append(
                f"{label}: Hostname '{name}' ist ungültig (ohne https:// und ohne Port, nur Kleinbuchstaben)."
            )
        elif name in seen:
            errors.append(f"{label}: Hostname '{name}' ist doppelt vorhanden.")
        else:
            seen.add(name)
        if not service:
            errors.append(f"{label}: Ziel für '{name}' fehlt.")
        elif not SERVICE_RE.match(service):
            errors.append(f"{label}: Ziel '{service}' ist ungültig.")

    catch_all = normalize_service(s.get("catch_all", ""))
    if catch_all and not SERVICE_RE.match(catch_all):
        errors.append(f"Fangregel: Ziel '{catch_all}' ist ungültig.")

    if s.get("log_level") not in LOG_LEVELS:
        errors.append("Protokollstufe ist ungültig.")

    for param in s.get("run_parameters", []):
        if not ALLOWED_RUN_PARAMS.match(param):
            errors.append(f"Zusatzparameter '{param}' ist nicht erlaubt.")

    if not REPO_RE.match(s.get("github_repo", "")):
        errors.append("GitHub-Repo muss die Form 'Benutzer/Repo' haben.")

    if not main_host and not s.get("hosts") and not catch_all:
        errors.append(
            "Es muss mindestens die Hauptdomain, eine Subdomain oder eine Fangregel eingetragen sein."
        )
    return errors


def is_enabled(entry: dict) -> bool:
    return bool(entry.get("enabled", True))


def all_hostnames(s: dict, enabled_only: bool = False) -> list[str]:
    names = []
    main = s.get("main", {})
    main_host = (main.get("hostname") or "").strip()
    if main_host and (not enabled_only or is_enabled(main)):
        names.append(main_host)
    names += [h["hostname"].strip() for h in s.get("hosts", [])
              if h.get("hostname") and (not enabled_only or is_enabled(h))]
    return names


def build_config(s: dict, tunnel_id: str, creds_path: str | Path) -> dict:
    """Baut die cloudflared-Konfiguration."""
    ingress: list[dict] = []

    main = s.get("main", {})
    main_host = (main.get("hostname") or "").strip()
    if main_host and is_enabled(main):
        ingress.append(
            {
                "hostname": main_host,
                "service": normalize_service(main.get("service", "")),
                "originRequest": {"noTLSVerify": bool(main.get("no_tls_verify", True))},
            }
        )

    for host in s.get("hosts", []):
        if not is_enabled(host):
            continue
        rule = {
            "hostname": host["hostname"].strip(),
            "service": normalize_service(host["service"]),
            "originRequest": {"noTLSVerify": bool(host.get("no_tls_verify", True))},
        }
        if host.get("disable_chunked"):
            rule["originRequest"]["disableChunkedEncoding"] = True
        ingress.append(rule)

    catch_all = normalize_service(s.get("catch_all", ""))
    fallback = {"service": catch_all or "http_status:404"}
    if catch_all:
        fallback["originRequest"] = {"noTLSVerify": True}
    ingress.append(fallback)

    return {"tunnel": tunnel_id, "credentials-file": str(creds_path), "ingress": ingress}


# --------------------------------------------------------------------------
# Hilfsfunktionen
# --------------------------------------------------------------------------
def run(cmd: list[str], timeout: int = 120, env: dict | None = None, stdout_only: bool = False) -> tuple[int, str]:
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, env=full_env, check=False
        )
    except FileNotFoundError:
        return 127, f"Programm nicht gefunden: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, f"Zeitüberschreitung nach {timeout}s: {' '.join(cmd)}"
    if stdout_only:
        return proc.returncode, proc.stdout or ""
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def run_retry(cmd: list[str], log, description: str, tries: int = 4, delay: int = 2) -> bool:
    for attempt in range(1, tries + 1):
        rc, out = run(cmd, timeout=90)
        if rc == 0:
            if out.strip():
                log(out.strip())
            return True
        log(f"Fehlgeschlagen: {description} (Versuch {attempt}/{tries}): {out.strip()[-300:]}")
        if attempt < tries:
            time.sleep(delay)
            delay *= 2
    return False


def systemctl(*args: str) -> tuple[int, str]:
    if NO_SYSTEMD:
        return 0, "(systemd übersprungen)"
    return run(["systemctl", *args], timeout=60)


# --------------------------------------------------------------------------
# Hintergrund-Aufträge mit Live-Protokoll
# --------------------------------------------------------------------------
class Job:
    def __init__(self, title: str):
        self.title = title
        self.lines: list[str] = []
        self.state = "running"  # running | ok | error
        self.started = time.time()
        self.lock = threading.Lock()

    def log(self, text: str) -> None:
        with self.lock:
            for line in str(text).splitlines():
                self.lines.append(line)

    def finish(self, ok: bool) -> None:
        self.state = "ok" if ok else "error"

    def snapshot(self) -> dict:
        with self.lock:
            return {"title": self.title, "state": self.state, "lines": list(self.lines)}


_JOBS: dict[str, Job] = {}
_JOB_LOCK = threading.Lock()


def start_job(name: str, title: str, func) -> Job | None:
    """Startet einen Auftrag im Hintergrund. Pro Name läuft nur einer gleichzeitig."""
    with _JOB_LOCK:
        current = _JOBS.get(name)
        if current and current.state == "running":
            return None
        job = Job(title)
        _JOBS[name] = job

    def runner() -> None:
        try:
            ok = func(job)
        except Exception as exc:  # noqa: BLE001 - alles im Protokoll sichtbar machen
            job.log(f"Unerwarteter Fehler: {exc}")
            ok = False
        job.finish(bool(ok))

    threading.Thread(target=runner, daemon=True).start()
    return job


def get_job(name: str) -> Job | None:
    return _JOBS.get(name)


# --------------------------------------------------------------------------
# Cloudflare-Anmeldung
# --------------------------------------------------------------------------
def has_cert() -> bool:
    return CERT_FILE.exists() and CERT_FILE.stat().st_size > 0


def has_tunnel() -> bool:
    return CREDS_FILE.exists()


def tunnel_id() -> str:
    try:
        return json.loads(CREDS_FILE.read_text(encoding="utf-8"))["TunnelID"]
    except (OSError, ValueError, KeyError):
        return ""


_LOGIN = {"state": "idle", "url": "", "message": ""}
_LOGIN_PROC: dict = {"proc": None}
_LOGIN_LOCK = threading.Lock()
URL_RE = re.compile(r"https://\S+")


def login_status() -> dict:
    status = dict(_LOGIN)
    if has_cert() and status["state"] in ("idle", "error"):
        status["state"] = "done"
    return status


def login_start() -> dict:
    """Startet 'cloudflared tunnel login' und liest die Anmelde-Adresse aus."""
    with _LOGIN_LOCK:
        if _LOGIN["state"] == "waiting":
            return dict(_LOGIN)
        _LOGIN.update(state="waiting", url="", message="Warte auf Cloudflare ...")

    home = STATE_DIR / "home"
    home.mkdir(parents=True, exist_ok=True)
    stale = home / ".cloudflared" / "cert.pem"
    if stale.exists():
        stale.unlink()

    def worker() -> None:
        try:
            proc = subprocess.Popen(
                [CLOUDFLARED, "tunnel", "login"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env={**os.environ, "HOME": str(home), "BROWSER": "true"},
            )
        except FileNotFoundError:
            _LOGIN.update(state="error", message="cloudflared ist nicht installiert.")
            return
        _LOGIN_PROC["proc"] = proc
        deadline = time.time() + 600
        assert proc.stdout is not None

        def reader() -> None:
            for line in proc.stdout:  # type: ignore[union-attr]
                match = URL_RE.search(line)
                if match and "argotunnel" in match.group(0) and not _LOGIN["url"]:
                    _LOGIN["url"] = match.group(0)

        threading.Thread(target=reader, daemon=True).start()
        while proc.poll() is None and time.time() < deadline:
            time.sleep(1)
        if proc.poll() is None:
            proc.kill()
            _LOGIN.update(state="error", message="Zeit abgelaufen (10 Minuten). Bitte erneut starten.")
            return
        downloaded = home / ".cloudflared" / "cert.pem"
        if proc.returncode == 0 and downloaded.exists():
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            shutil.move(str(downloaded), str(CERT_FILE))
            os.chmod(CERT_FILE, 0o600)
            _LOGIN.update(state="done", message="Anmeldung erfolgreich.")
        else:
            _LOGIN.update(state="error", message="Anmeldung fehlgeschlagen.")

    threading.Thread(target=worker, daemon=True).start()
    # kurz warten, damit die Adresse sofort angezeigt werden kann
    for _ in range(40):
        if _LOGIN["url"] or _LOGIN["state"] != "waiting":
            break
        time.sleep(0.25)
    return dict(_LOGIN)


def login_cancel() -> None:
    proc = _LOGIN_PROC.get("proc")
    if proc and proc.poll() is None:
        proc.kill()
    _LOGIN.update(state="idle", url="", message="")


def logout() -> None:
    """Entfernt nur die lokale Anmeldung (cert.pem). Der Tunnel bei Cloudflare bleibt."""
    if CERT_FILE.exists():
        CERT_FILE.unlink()
    _LOGIN.update(state="idle", url="", message="")


# --------------------------------------------------------------------------
# Tunnel anlegen, Konfiguration schreiben, DNS setzen, Dienst starten
# --------------------------------------------------------------------------
def existing_tunnel_name() -> str | None:
    rc, out = run(
        [CLOUDFLARED, "--origincert", str(CERT_FILE), "tunnel", "list", "--output", "json", "--id", tunnel_id()],
        timeout=60, stdout_only=True,
    )
    if rc != 0:
        return None
    try:
        start = out.index("[")
        data = json.loads(out[start:])
        return data[0]["name"]
    except (ValueError, IndexError, KeyError, TypeError):
        return None


def forget_tunnel() -> None:
    """Vergisst den lokalen Tunnel (nicht bei Cloudflare). Danach wird ein neuer angelegt."""
    for path in (CREDS_FILE, CONFIG_FILE, CONFIG_LAST_GOOD):
        if path.exists():
            path.unlink()


def write_env(s: dict) -> None:
    args = ["tunnel", "--no-autoupdate", f"--metrics={METRICS_ADDR}"]
    if s.get("post_quantum"):
        args.append("--post-quantum")
    args += list(s.get("run_parameters", []))
    if not any(p.startswith("--loglevel=") for p in args):
        args.append(f"--loglevel={s.get('log_level', 'info')}")
    args += [f"--origincert={CERT_FILE}", f"--config={CONFIG_FILE}", "run", s["tunnel_name"]]
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    ENV_FILE.write_text(f'ARGS="{" ".join(args)}"\n', encoding="utf-8")


def apply_settings(s: dict, job: Job, dry_run: bool = False) -> bool:
    """Übernimmt die Einstellungen. Bei Fehlern bleibt die letzte funktionierende Konfiguration aktiv."""
    log = job.log
    errors = validate_settings(s)
    if errors:
        for err in errors:
            log("FEHLER: " + err)
        return False
    log("Einstellungen sind gültig.")

    if not has_cert():
        log("FEHLER: Noch nicht bei Cloudflare angemeldet (Seite 'Cloudflare').")
        return False

    # Tunnel prüfen / anlegen
    if has_tunnel():
        log(f"Vorhandener Tunnel {tunnel_id()} gefunden, prüfe Namen ...")
        name = existing_tunnel_name()
        if name is None:
            log("FEHLER: Tunnel konnte bei Cloudflare nicht abgefragt werden (Netzwerk oder Anmeldung).")
            return False
        if name != s["tunnel_name"]:
            log(
                f"FEHLER: Der vorhandene Tunnel heißt '{name}', in den Einstellungen steht "
                f"'{s['tunnel_name']}'. Entweder den Namen angleichen oder unter 'Cloudflare' "
                "den lokalen Tunnel zurücksetzen."
            )
            return False
        log("Tunnelname passt.")
    elif dry_run:
        log("Hinweis (Probelauf): Es würde ein neuer Tunnel angelegt.")
    else:
        log(f"Lege neuen Tunnel '{s['tunnel_name']}' an ...")
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        ok = run_retry(
            [
                CLOUDFLARED, "--origincert", str(CERT_FILE), "--cred-file", str(CREDS_FILE),
                "tunnel", "--loglevel", s.get("log_level", "info"), "create", s["tunnel_name"],
            ],
            log, "Tunnel anlegen",
        )
        if not ok or not has_tunnel():
            log(
                "FEHLER: Tunnel konnte nicht angelegt werden. Prüfe im Cloudflare-Dashboard "
                "(Networks -> Tunnels), ob schon ein Tunnel mit diesem Namen existiert."
            )
            return False

    # Konfiguration bauen und mit cloudflared prüfen
    tid = tunnel_id() or "00000000-0000-0000-0000-000000000000"
    config = build_config(s, tid, CREDS_FILE)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    candidate = STATE_DIR / "config.candidate.json"
    candidate.write_text(json.dumps(config, indent=2), encoding="utf-8")
    rc, out = run([CLOUDFLARED, "tunnel", "--config", str(candidate), "ingress", "validate"], timeout=30)
    if rc == 127:
        log("Hinweis: cloudflared nicht gefunden, Prüfung der Konfiguration übersprungen.")
    elif rc != 0:
        log("FEHLER: cloudflared lehnt die Konfiguration ab:\n" + out.strip())
        candidate.unlink(missing_ok=True)
        return False
    else:
        log("Konfiguration von cloudflared geprüft: OK.")

    if dry_run:
        log("Probelauf beendet. Es wurde nichts verändert.")
        candidate.unlink(missing_ok=True)
        return True

    if CONFIG_FILE.exists():
        shutil.copy2(CONFIG_FILE, CONFIG_LAST_GOOD)
    candidate.replace(CONFIG_FILE)
    log("Konfiguration geschrieben.")

    # DNS-Einträge (überschreibt vorhandene CNAMEs)
    for hostname in all_hostnames(s, enabled_only=True):
        log(f"Setze DNS-Eintrag {hostname} ...")
        ok = run_retry(
            [CLOUDFLARED, "--origincert", str(CERT_FILE), "tunnel", "--loglevel", s.get("log_level", "info"),
             "route", "dns", "-f", tunnel_id(), hostname],
            log, f"DNS-Eintrag {hostname}",
        )
        if not ok:
            log(f"FEHLER: DNS-Eintrag für {hostname} konnte nicht gesetzt werden.")
            _rollback(log)
            return False

    write_env(s)
    systemctl("daemon-reload")
    systemctl("enable", TUNNEL_SERVICE)

    # Sauber stoppen, dann neu starten. Ein einfaches "restart" kann hängen bleiben, solange der
    # alte Prozess noch offene Verbindungen abbaut (und seinen Metrik-Port belegt).
    log("Stoppe den laufenden Tunnel ...")
    systemctl("stop", TUNNEL_SERVICE)
    _wait_until(lambda: not _metrics_port_busy(), 20)

    for attempt in (1, 2):
        log("Starte den Tunnel ..." if attempt == 1 else "Tunnel noch nicht verbunden, starte ihn noch einmal neu ...")
        rc, out = systemctl("restart" if attempt == 2 else "start", TUNNEL_SERVICE)
        if rc != 0:
            log("FEHLER: Dienst konnte nicht gestartet werden:\n" + out.strip())
            _rollback(log)
            return False
        log("Dienst gestartet. Warte auf Verbindung zu Cloudflare ...")
        if _wait_until(lambda: tunnel_status()["ready"], 60):
            log(f"Tunnel verbunden ({tunnel_status()['connections']} Verbindungen).")
            log("Fertig.")
            _clear_pending()
            return True
        if NO_SYSTEMD:
            log("Fertig (ohne systemd).")
            _clear_pending()
            return True
        log("Letzte Zeilen aus dem Tunnel-Protokoll:\n" + "\n".join(tunnel_logs(15).splitlines()[-15:]))
        if attempt == 1:
            systemctl("stop", TUNNEL_SERVICE)
            _wait_until(lambda: not _metrics_port_busy(), 20)

    log("WARNUNG: Der Tunnel meldet sich noch nicht verbunden. Prüfe die Protokolle.")
    _clear_pending()
    return True


def _clear_pending() -> None:
    cur = load_settings()
    if cur.get("pending"):
        cur["pending"] = False
        save_settings(cur)


def _wait_until(check, seconds: int) -> bool:
    for _ in range(seconds):
        try:
            if check():
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1)
    return False


def _metrics_port_busy() -> bool:
    host, _, port = METRICS_ADDR.rpartition(":")
    try:
        with socket.create_connection((host or "127.0.0.1", int(port)), timeout=1):
            return True
    except OSError:
        return False


def _rollback(log) -> None:
    if CONFIG_LAST_GOOD.exists():
        shutil.copy2(CONFIG_LAST_GOOD, CONFIG_FILE)
        systemctl("restart", TUNNEL_SERVICE)
        log("Letzte funktionierende Konfiguration wurde wiederhergestellt.")


# --------------------------------------------------------------------------
# Status und Protokolle
# --------------------------------------------------------------------------
def tunnel_status() -> dict:
    active = False
    if not NO_SYSTEMD:
        rc, out = run(["systemctl", "is-active", TUNNEL_SERVICE], timeout=10)
        active = out.strip() == "active"
    ready = False
    connections = 0
    try:
        with urllib.request.urlopen(f"http://{METRICS_ADDR}/ready", timeout=2) as resp:
            data = json.loads(resp.read().decode())
            connections = int(data.get("readyConnections", 0))
            ready = resp.status == 200 and connections > 0
    except (urllib.error.URLError, OSError, ValueError):
        pass
    return {"active": active, "ready": ready, "connections": connections}


def tunnel_logs(lines: int = 200) -> str:
    if NO_SYSTEMD:
        return "(kein systemd)"
    rc, out = run(["journalctl", "-u", TUNNEL_SERVICE, "-n", str(lines), "--no-pager", "-o", "short-iso"], timeout=20)
    return out if out.strip() else "(keine Einträge)"


def webui_logs(lines: int = 100) -> str:
    if NO_SYSTEMD:
        return "(kein systemd)"
    rc, out = run(["journalctl", "-u", WEBUI_SERVICE, "-n", str(lines), "--no-pager", "-o", "short-iso"], timeout=20)
    return out if out.strip() else "(keine Einträge)"


def tunnel_control(action: str) -> tuple[int, str]:
    if action not in ("start", "stop", "restart"):
        return 1, "Unbekannte Aktion"
    return systemctl(action, TUNNEL_SERVICE)


# --------------------------------------------------------------------------
# Versionen und Updates
# --------------------------------------------------------------------------
def app_version() -> str:
    try:
        return (APP_DIR / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "0.0.0"


def cloudflared_version() -> str:
    rc, out = run([CLOUDFLARED, "--version"], timeout=10)
    if rc != 0:
        return "nicht installiert"
    match = re.search(r"version\s+(\S+)", out)
    return match.group(1) if match else out.strip()


def version_tuple(text: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", text.lstrip("vV"))
    return tuple(int(n) for n in nums[:4]) or (0,)


def _http_json(url: str) -> dict | list:
    req = urllib.request.Request(
        url, headers={"User-Agent": "cloudflared-lxc-webui", "Accept": "application/vnd.github+json"}
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def latest_app_release(repo: str) -> dict:
    """Neueste GitHub-Version (Release, sonst Tag). Gibt {'tag','tarball'} zurück."""
    try:
        data = _http_json(f"https://api.github.com/repos/{repo}/releases/latest")
        if isinstance(data, dict) and data.get("tag_name"):
            return {"tag": data["tag_name"], "tarball": data["tarball_url"], "notes": data.get("body") or ""}
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
    tags = _http_json(f"https://api.github.com/repos/{repo}/tags?per_page=30")
    if isinstance(tags, list) and tags:
        best = max(tags, key=lambda t: version_tuple(t["name"]))
        return {"tag": best["name"], "tarball": best["tarball_url"], "notes": ""}
    raise RuntimeError("Im Repository wurde weder ein Release noch ein Tag gefunden.")


def check_app_update(repo: str) -> dict:
    current = app_version()
    try:
        latest = latest_app_release(repo)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "current": current, "error": str(exc)}
    newer = version_tuple(latest["tag"]) > version_tuple(current)
    return {"ok": True, "current": current, "latest": latest["tag"], "newer": newer,
            "tarball": latest["tarball"], "notes": latest["notes"]}


def cloudflared_update_info() -> dict:
    run(["apt-get", "update", "-qq"], timeout=120)
    rc, out = run(["apt-cache", "policy", "cloudflared"], timeout=30)
    installed = re.search(r"Installed:\s*(\S+)", out)
    candidate = re.search(r"Candidate:\s*(\S+)", out)
    inst = installed.group(1) if installed else "(none)"
    cand = candidate.group(1) if candidate else "(none)"
    return {"installed": inst, "candidate": cand, "newer": inst != cand and cand not in ("(none)", "")}


_VERSIONS_CACHE: dict = {"at": 0.0, "data": None}
_VERSIONS_LOCK = threading.Lock()


def versions_cached(max_age: int = 600, fresh: bool = False) -> dict:
    """Versionsstand (10 Minuten zwischengespeichert). fresh=True fragt immer neu ab."""
    with _VERSIONS_LOCK:
        now = time.time()
        if fresh or _VERSIONS_CACHE["data"] is None or now - _VERSIONS_CACHE["at"] > max_age:
            s = load_settings()
            app_info = check_app_update(s["github_repo"])
            cf_info = cloudflared_update_info()
            _VERSIONS_CACHE["data"] = {
                "cloudflared": {"version": cloudflared_version(), "installed": cf_info["installed"],
                                "candidate": cf_info["candidate"], "newer": bool(cf_info["newer"])},
                "app": {"version": app_info.get("current", app_version()), "ok": bool(app_info.get("ok")),
                        "newer": bool(app_info.get("newer")), "latest": app_info.get("latest", ""),
                        "notes": app_info.get("notes", ""), "error": app_info.get("error", "")},
                "repo": s["github_repo"],
            }
            _VERSIONS_CACHE["at"] = time.time()
        return _VERSIONS_CACHE["data"]


def update_cloudflared(job: Job) -> bool:
    job.log("Aktualisiere Paketlisten ...")
    rc, out = run(["apt-get", "update", "-qq"], timeout=180)
    job.log(out.strip())
    job.log("Installiere neueste cloudflared-Version ...")
    rc, out = run(["apt-get", "install", "-y", "--only-upgrade", "cloudflared"], timeout=300)
    job.log(out.strip()[-1500:])
    if rc != 0:
        return False
    job.log("Starte den Tunnel neu ...")
    rc, out = systemctl("restart", TUNNEL_SERVICE)
    job.log(out.strip() or "Neustart ausgeführt.")
    job.log(f"cloudflared-Version jetzt: {cloudflared_version()}")
    return rc == 0


def update_app(job: Job, repo: str) -> bool:
    """Lädt die neueste Version der Oberfläche von GitHub und tauscht den Ordner 'app' aus.
    Einstellungen und Tunnel liegen außerhalb und bleiben erhalten."""
    info = check_app_update(repo)
    if not info["ok"]:
        job.log("FEHLER: " + info["error"])
        return False
    job.log(f"Installiert: {info['current']}  /  Verfügbar: {info['latest']}")
    if not info["newer"]:
        job.log("Die Oberfläche ist bereits aktuell.")
        return True

    job.log("Lade Version herunter ...")
    req = urllib.request.Request(info["tarball"], headers={"User-Agent": "cloudflared-lxc-webui"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        blob = resp.read()
    if len(blob) > 20_000_000:
        job.log("FEHLER: Archiv ist unerwartet groß.")
        return False

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp).resolve()
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
            for member in tar.getmembers():
                target = (base / member.name).resolve()
                inside = target == base or base in target.parents
                if not inside or member.issym() or member.islnk() or member.isdev():
                    job.log("FEHLER: Archiv enthält unzulässige Pfade.")
                    return False
            try:
                tar.extractall(tmp, filter="data")  # Python >= 3.12
            except TypeError:
                tar.extractall(tmp)  # noqa: S202 - Pfade oben geprüft
        roots = [p for p in Path(tmp).iterdir() if p.is_dir()]
        if len(roots) != 1 or not (roots[0] / "app" / "app.py").exists() or not (roots[0] / "app" / "VERSION").exists():
            job.log("FEHLER: Das Archiv enthält keinen gültigen Ordner 'app'.")
            return False
        new_app = roots[0] / "app"
        parent = APP_DIR.parent
        staging = parent / "app.new"
        backup = parent / "app.old"
        if staging.exists():
            shutil.rmtree(staging)
        shutil.copytree(new_app, staging)
        if backup.exists():
            shutil.rmtree(backup)
        APP_DIR.rename(backup)
        staging.rename(APP_DIR)
        job.log("Neue Version eingespielt. Die alte liegt unter 'app.old'.")

    job.log("Die Oberfläche startet in wenigen Sekunden neu. Seite danach neu laden.")
    if not NO_SYSTEMD:
        subprocess.Popen(  # noqa: S603
            ["sh", "-c", f"sleep 3; systemctl restart {WEBUI_SERVICE}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )
    return True
