#!/usr/bin/env bash
# Läuft INNERHALB des LXC (Debian/Ubuntu). Installiert cloudflared + Webinterface.
# Kann auch später erneut ausgeführt werden (Reparatur / Neuinstallation des Webinterface).
set -euo pipefail

REPO="${CFWEBUI_REPO:-Ronny-1979/cloudflared-lxc}"
REF="${CFWEBUI_REF:-}"          # leer = neuestes Release, sonst Tag/Branch
APP_ROOT=/opt/cloudflared-webui

[ "$(id -u)" -eq 0 ] || { echo "Bitte als root ausführen."; exit 1; }
export DEBIAN_FRONTEND=noninteractive

echo ">> Pakete installieren"
apt-get update -qq
apt-get install -y -qq python3 python3-flask curl gnupg ca-certificates tar

echo ">> Cloudflare-Paketquelle einrichten"
install -d -m 0755 /usr/share/keyrings
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg -o /usr/share/keyrings/cloudflare-main.gpg
echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main' \
  > /etc/apt/sources.list.d/cloudflared.list
apt-get update -qq
apt-get install -y -qq cloudflared

echo ">> Webinterface von GitHub laden ($REPO)"
if [ -z "$REF" ]; then
  REF="$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("tag_name",""))' 2>/dev/null || true)"
  [ -n "$REF" ] || REF="main"
fi
echo "   Version/Branch: $REF"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
curl -fsSL "https://codeload.github.com/$REPO/tar.gz/$REF" -o "$TMP/app.tgz"
tar -xzf "$TMP/app.tgz" -C "$TMP"
SRC="$(find "$TMP" -mindepth 1 -maxdepth 1 -type d | head -n1)"
[ -f "$SRC/app/app.py" ] || { echo "FEHLER: Archiv enthält keinen Ordner 'app'."; exit 1; }

install -d -m 0755 "$APP_ROOT"
rm -rf "$APP_ROOT/app"
cp -r "$SRC/app" "$APP_ROOT/app"
install -m 0644 "$SRC/systemd/cloudflared-tunnel.service" /etc/systemd/system/cloudflared-tunnel.service
install -m 0644 "$SRC/systemd/cloudflared-webui.service"  /etc/systemd/system/cloudflared-webui.service
install -d -m 0700 /var/lib/cloudflared-webui /etc/cloudflared-webui

systemctl daemon-reload
systemctl enable cloudflared-webui.service
systemctl restart cloudflared-webui.service   # auch bei erneutem Ausführen: neuen Code laden
# Der Tunnel-Dienst startet erst, wenn im Webinterface etwas übernommen wurde.
# (Bei einer Reparatur eines schon eingerichteten Tunnels bleibt er aktiviert.)
if [ ! -f /var/lib/cloudflared-webui/tunnel.env ]; then
  systemctl disable cloudflared-tunnel.service >/dev/null 2>&1 || true
fi

IP="$(hostname -I | awk '{print $1}')"
echo
echo "Fertig. Webinterface: http://$IP:8099"
