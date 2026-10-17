#!/usr/bin/env bash
# Läuft auf dem PROXMOX-HOST. Legt einen Debian-LXC an und installiert darin cloudflared + Weboberfläche.
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/Ronny-1979/cloudflared-lxc/main/ct/cloudflared-lxc.sh)"
# Alle Werte lassen sich auch vorab per Umgebungsvariable setzen (dann ohne Auswahlmenü):
#   CTID CT_HOSTNAME CORES RAM DISK BRIDGE IPCFG GW VLAN STORAGE TEMPLATE_STORAGE
set -euo pipefail

REPO="${CFWEBUI_REPO:-Ronny-1979/cloudflared-lxc}"
REF="${CFWEBUI_REF:-main}"
TITLE="Cloudflared-LXC"

command -v pct >/dev/null || { echo "Dieses Skript muss auf dem Proxmox-Host laufen."; exit 1; }
[ "$(id -u)" -eq 0 ] || { echo "Bitte als root ausführen."; exit 1; }
command -v whiptail >/dev/null || { echo "whiptail fehlt (apt install whiptail)."; exit 1; }
[ -t 0 ] || INTERACTIVE=0

ui_input() { # ui_input "Frage" default  -> Ausgabe auf stdout
  whiptail --backtitle "$TITLE" --title "$TITLE" --inputbox "$1" 10 64 "$2" 3>&1 1>&2 2>&3
}
cancel() { echo "Abgebrochen."; exit 0; }

# Speicher-Auswahl: pick_storage "Titel" content-typ
pick_storage() {
  local content="$1" text="$2" items=() name type avail
  while read -r name type _ _ _ avail _; do
    items+=("$name" "$type, frei: $((avail / 1024 / 1024)) GB" "OFF")
  done < <(pvesm status -content "$content" | awk 'NR>1')
  [ "${#items[@]}" -gt 0 ] || { echo "Kein Speicher für '$content' gefunden." >&2; exit 1; }
  if [ "${#items[@]}" -eq 3 ]; then echo "${items[0]}"; return; fi
  items[2]="ON"
  whiptail --backtitle "$TITLE" --title "$TITLE" --radiolist "$text" 16 64 6 "${items[@]}" 3>&1 1>&2 2>&3
}

NEXT_ID="$(pvesh get /cluster/nextid)"
DEF_STORAGE_SET="${STORAGE:+1}"

if [ -z "${CTID:-}${CT_HOSTNAME:-}${STORAGE:-}${IPCFG:-}" ] && [ "${INTERACTIVE:-1}" = 1 ]; then
  whiptail --backtitle "$TITLE" --title "$TITLE" --yesno \
"Cloudflare-Tunnel mit Weboberfläche in einem eigenen Debian-LXC.

Es wird ein Container angelegt und alles darin installiert.
Danach stellst du Domains und Subdomains im Browser ein.

Fortfahren?" 14 64 || cancel

  MODE="$(whiptail --backtitle "$TITLE" --title "Einstellungen" --menu "Wie soll der Container angelegt werden?" 14 64 3 \
    "1" "Standard (empfohlen: 1 Kern, 512 MB, 2 GB, DHCP)" \
    "2" "Erweitert (alles selbst festlegen)" \
    "3" "Abbrechen" 3>&1 1>&2 2>&3)" || cancel
  [ "$MODE" = 3 ] && cancel

  CTID="$NEXT_ID"; CT_HOSTNAME="cloudflared"; CORES=1; RAM=512; DISK=2
  BRIDGE=vmbr0; IPCFG=dhcp; GW=""; VLAN=""

  if [ "$MODE" = 2 ]; then
    CTID="$(ui_input "Container-ID" "$CTID")" || cancel
    CT_HOSTNAME="$(ui_input "Hostname" "$CT_HOSTNAME")" || cancel
    CORES="$(ui_input "CPU-Kerne" "$CORES")" || cancel
    RAM="$(ui_input "Arbeitsspeicher in MB" "$RAM")" || cancel
    DISK="$(ui_input "Festplatte in GB" "$DISK")" || cancel
    BRIDGE="$(ui_input "Netzwerk-Bridge" "$BRIDGE")" || cancel
    NETMODE="$(whiptail --backtitle "$TITLE" --title "Netzwerk" --menu "IP-Adresse" 12 64 2 \
      "dhcp" "Automatisch per DHCP" "static" "Feste IP-Adresse" 3>&1 1>&2 2>&3)" || cancel
    if [ "$NETMODE" = static ]; then
      IPCFG="$(ui_input "IP mit Netzmaske, z. B. 192.168.178.40/24" "192.168.178.40/24")" || cancel
      GW="$(ui_input "Gateway" "192.168.178.1")" || cancel
    fi
    VLAN="$(ui_input "VLAN-Tag (leer = keins)" "")" || cancel
  fi

  STORAGE="$(pick_storage rootdir "Speicher für den Container:")" || cancel
  TEMPLATE_STORAGE="$(pick_storage vztmpl "Speicher für die Debian-Vorlage:")" || cancel

  whiptail --backtitle "$TITLE" --title "Zusammenfassung" --yesno \
"ID:          $CTID
Hostname:    $CT_HOSTNAME
Kerne / RAM: $CORES / ${RAM} MB
Festplatte:  ${DISK} GB auf $STORAGE
Netzwerk:    $BRIDGE, $IPCFG${GW:+ (Gateway $GW)}${VLAN:+, VLAN $VLAN}
Vorlage auf: $TEMPLATE_STORAGE

Container jetzt anlegen?" 16 64 || cancel
else
  CTID="${CTID:-$NEXT_ID}"; CT_HOSTNAME="${CT_HOSTNAME:-cloudflared}"; CORES="${CORES:-1}"
  RAM="${RAM:-512}"; DISK="${DISK:-2}"; BRIDGE="${BRIDGE:-vmbr0}"; IPCFG="${IPCFG:-dhcp}"
  GW="${GW:-}"; VLAN="${VLAN:-}"
  STORAGE="${STORAGE:-$(pvesm status -content rootdir | awk 'NR==2{print $1}')}"
  TEMPLATE_STORAGE="${TEMPLATE_STORAGE:-$(pvesm status -content vztmpl | awk 'NR==2{print $1}')}"
fi

if [ "$IPCFG" != dhcp ] && [ -z "$GW" ]; then echo "Für eine feste IP wird ein Gateway benötigt (GW=...)."; exit 1; fi
pct status "$CTID" >/dev/null 2>&1 && { echo "Container $CTID existiert schon."; exit 1; }

echo ">> Debian-Vorlage suchen"
pveam update >/dev/null
ARCH="$(dpkg --print-architecture)"
TEMPLATE=""
for rel in debian-13-standard debian-12-standard; do
  TEMPLATE="$(pveam available --section system | awk -v r="$rel" -v a="_${ARCH}.tar" '$2 ~ r && index($2, a) {print $2}' | sort -V | tail -n1)"
  [ -n "$TEMPLATE" ] && break
done
[ -n "$TEMPLATE" ] || { echo "Keine Debian-Vorlage für $ARCH gefunden."; exit 1; }
echo "   Vorlage: $TEMPLATE"
pveam list "$TEMPLATE_STORAGE" | grep -q "$TEMPLATE" || pveam download "$TEMPLATE_STORAGE" "$TEMPLATE"

NET="name=eth0,bridge=$BRIDGE,firewall=0"
if [ "$IPCFG" = dhcp ]; then NET="$NET,ip=dhcp"; else NET="$NET,ip=$IPCFG,gw=$GW"; fi
[ -z "$VLAN" ] || NET="$NET,tag=$VLAN"

echo ">> Container $CTID anlegen"
pct create "$CTID" "$TEMPLATE_STORAGE:vztmpl/$TEMPLATE" \
  --hostname "$CT_HOSTNAME" --cores "$CORES" --memory "$RAM" --swap 256 \
  --rootfs "$STORAGE:$DISK" --net0 "$NET" \
  --unprivileged 1 --features nesting=1 --onboot 1 \
  --tags cloudflared --description "Cloudflare Tunnel mit Weboberfläche (https://github.com/$REPO)"
pct start "$CTID"

echo ">> Warte auf Netzwerk"
ok=0
for _ in $(seq 1 60); do
  if pct exec "$CTID" -- sh -c 'getent hosts deb.debian.org >/dev/null 2>&1'; then ok=1; break; fi
  sleep 2
done
[ "$ok" = 1 ] || { echo "Container hat kein Netzwerk/DNS. Einstellungen prüfen (Container $CTID bleibt bestehen)."; exit 1; }

echo ">> Installation im Container"
pct exec "$CTID" -- bash -c "apt-get update -qq && apt-get install -y -qq curl ca-certificates >/dev/null"
pct exec "$CTID" -- bash -c "CFWEBUI_REPO='$REPO' CFWEBUI_REF='${CFWEBUI_APP_REF:-}' bash <(curl -fsSL https://raw.githubusercontent.com/$REPO/$REF/install/install.sh)"

IP="$(pct exec "$CTID" -- hostname -I | awk '{print $1}')"
whiptail --backtitle "$TITLE" --title "Fertig" --msgbox \
"Installation abgeschlossen.

Weboberfläche:  http://$IP:8099

Beim ersten Öffnen legst du ein Passwort fest.
Home Assistant: IP $IP bei http: trusted_proxies eintragen." 14 64 || true
echo "Weboberfläche: http://$IP:8099"
