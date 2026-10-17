# Cloudflared-LXC mit Weboberfläche

**Deine Heimnetz-Dienste sicher aus dem Internet erreichbar machen: ohne Port im Router, ohne Kommandozeile, mit einer Weboberfläche.**

Ein Proxmox-LXC-Container, in dem ein [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/) läuft.
Domains, Subdomains und Ziele trägst du im Browser ein. Anmeldung bei Cloudflare, Tunnel, DNS-Einträge und Updates erledigt das Programm.

**Installation** (als `root` in der Shell des Proxmox-Servers einfügen):
```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/Ronny-1979/cloudflared-lxc/main/ct/cloudflared-lxc.sh)"
```

## Was kann es?
- **Installation mit einem Befehl** auf dem Proxmox-Host, mit Auswahlmenü (Standard oder Erweitert)
- **Cloudflare-Anmeldung im Browser**, Tunnel und DNS-Einträge legt das Programm selbst an
- **Hauptdomain und beliebig viele Subdomains** mit Ziel-IP, frei wählbarem Namen und Ein/Aus-Haken
- **Status, Neustart und Protokoll** des Tunnels im Browser
- **Updates per Klick** für cloudflared und die Oberfläche
- **Sicherung:** Einstellungen exportieren und importieren
- **Dunkelmodus**

## In 5 Schritten zum Ziel
1. **Container anlegen:** Einen Befehl auf dem Proxmox-Server einfügen (Abschnitt 2).
2. **Weboberfläche öffnen:** `http://<LXC-IP>:8099` aufrufen und ein Passwort festlegen.
3. **Bei Cloudflare anmelden:** Knopf klicken, Link öffnen, die eigene Domain anklicken.
4. **Domains eintragen:** Pro Dienst eine Karte mit Domain und IP des Geräts.
5. **„Speichern & übernehmen“ klicken.** Fertig.

## Die Menüs im Überblick
| Reiter | Was du dort machst |
|---|---|
| **Übersicht** | Tunnelstatus („verbunden (4 Verbindungen)“), Neustart/Stopp/Start, Einrichtungsstand, Versionen mit Update-Hinweis, Liste deiner Domains mit **Haken zum Ein-/Ausschalten** und anklickbaren Links |
| **Cloudflare** | Bei Cloudflare anmelden oder abmelden, lokalen Tunnel zurücksetzen |
| **Domains** | Hauptdomain, Subdomains, erweiterte Optionen; „Speichern & übernehmen“, „Probelauf“, „Nur speichern“ |
| **Sicherung** | Einstellungen als Datei exportieren und wieder importieren |
| **Protokolle** | Die letzten 300 Zeilen des Tunnel-Protokolls |
| **Updates** | Neue Versionen von cloudflared und der Oberfläche prüfen und einspielen |
| **Passwort** | Passwort der Weboberfläche ändern |

## Begriffe einfach erklärt
| Begriff | Bedeutung |
|---|---|
| **Subdomain** | Zusatz vor deiner Domain, z. B. `nas.beispiel.de`. Jeder Dienst bekommt eine eigene. |
| **Tunnel** | Verschlüsselte Verbindung von deinem Heimnetz zu Cloudflare. Besucher erreichen deine Geräte, ohne dass du einen Router-Port öffnest. |
| **LXC / Container** | Ein kleiner, abgetrennter Rechner in Proxmox. Hier läuft der Tunnel. |
| **IP-Adresse und Port** | Die „Hausnummer“ und „Tür“ eines Geräts, z. B. `192.168.178.20:8123`. |
| **DNS-Eintrag** | Das „Telefonbuch“ des Internets: sagt, zu welchem Tunnel eine Domain gehört. Das Programm setzt ihn für dich. |

**Platzhalter:** Alles in spitzen Klammern ersetzt du durch deine Werte: `<LXC-IP>` (IP des neuen Containers), `<HA-IP>` (Home Assistant), `<PROXMOX-IP>`, `<DEINE-DOMAIN>` (z. B. `beispiel.de`), `<ID>` (Container-ID in Proxmox, z. B. `114`).

## 1. Wie funktioniert das, und was brauchst du?
Normalerweise müsste man für den Zugriff von außen Ports im Router freigeben. Hier baut stattdessen `cloudflared` **von innen nach außen** eine Verbindung zu Cloudflare auf.
Besucher sprechen mit Cloudflare, und Cloudflare reicht die Anfrage durch den Tunnel an den Dienst weiter.

```
Besucher ──► Cloudflare ◄──(ausgehende, verschlüsselte Verbindung)── Cloudflared-LXC <LXC-IP>
                                                                      ├─ <DEINE-DOMAIN>         → <HA-IP>:8123
                                                                      ├─ nas.<DEINE-DOMAIN>     → <NAS-IP>
                                                                      └─ proxmox.<DEINE-DOMAIN> → https://<PROXMOX-IP>:8006
```

**Du brauchst:** einen Proxmox-Server (VE 8 oder neuer), eine eigene Domain (kostet beim Anbieter jährlich ein paar Euro) und ein kostenloses Cloudflare-Konto. Das Programm und der Cloudflare-Tunnel selbst sind kostenlos.

**Die Domain muss nicht bei Cloudflare gekauft sein**, sie kann bei jedem Anbieter liegen. Wichtig ist nur, dass Cloudflare ihre DNS-Einträge verwaltet:
1. Im Cloudflare-Konto „Domain hinzufügen“ wählen. Cloudflare nennt dir zwei Nameserver.
2. Diese zwei Nameserver beim Anbieter der Domain eintragen.
3. Bestehende Einträge (z. B. für E-Mail) vorher in Cloudflare übernehmen, damit sie weiter funktionieren.

## 2. Installation (auf dem Proxmox-Host)
In Proxmox auf deinen Server → **Shell** (oder per SSH als `root`) und diesen Befehl einfügen:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/Ronny-1979/cloudflared-lxc/main/ct/cloudflared-lxc.sh)"
```

Ein Menü führt durch die Installation: **Standard** (empfohlen: 1 Kern, 512 MB RAM, 2 GB Platte, DHCP, Hostname `cloudflared`) oder **Erweitert** (ID, Hostname, Kerne, RAM, Platte, Bridge, feste IP mit Gateway, VLAN).
Dazu wählst du den Speicher für Container und Debian-Vorlage. Am Ende zeigt ein Fenster die Adresse `http://<LXC-IP>:8099`.
Das Skript ändert am Host nichts außer dem neuen Container (und dem Laden der Debian-Vorlage).

Ohne Menü, mit vorab gesetzten Werten (`CTID`, `CT_HOSTNAME`, `CORES`, `RAM`, `DISK`, `BRIDGE`, `IPCFG`, `GW`, `VLAN`, `STORAGE`, `TEMPLATE_STORAGE`), z. B. mit fester IP:
```bash
IPCFG=<LXC-IP>/24 GW=<ROUTER-IP> bash -c "$(curl -fsSL https://raw.githubusercontent.com/Ronny-1979/cloudflared-lxc/main/ct/cloudflared-lxc.sh)"
```

## 3. Einrichtung in der Weboberfläche
Öffne `http://<LXC-IP>:8099` und lege ein Passwort fest (mindestens 8 Zeichen). **Tu das gleich nach der Installation:** Solange keins gesetzt ist, kann es jeder im Heimnetz festlegen.

### Schritt 1: Bei Cloudflare anmelden (Reiter „Cloudflare“)
1. Auf **„Bei Cloudflare anmelden“** klicken. Nach ein, zwei Sekunden erscheint ein Link.
2. Link öffnen und bei Cloudflare anmelden, so wie du es sonst auch tust (E-Mail und Passwort, oder Google, Apple, GitHub).
3. In der Domainliste **deine Domain anklicken**. Erscheint danach ein Knopf **„Autorisieren“**, auch den klicken.
4. 10 bis 20 Sekunden warten: Die Oberfläche zeigt „Angemeldet“ (sonst Seite neu laden). Der Link gilt 10 Minuten.

Die Anmeldung dient nur dem Anlegen des Tunnels und der DNS-Einträge. Dein Cloudflare-Passwort sieht das Programm nie.

### Schritt 2: Domains eintragen (Reiter „Domains“)
Pro Domain: **Name** (frei wählbar, nur zur Anzeige in der Übersicht), **Domain**, **Ziel**, Haken **Aktiv**.

| Name | Domain | Ziel | TLS-Check aus | Chunked aus |
|---|---|---|---|---|
| Home Assistant | `<DEINE-DOMAIN>` (Hauptdomain) | `<HA-IP>:8123` | ja | nein |
| Proxmox | `proxmox.<DEINE-DOMAIN>` | `https://<PROXMOX-IP>:8006` | **ja (nötig)** | **ja** |
| NAS | `nas.<DEINE-DOMAIN>` | `<NAS-IP>` | ja | nein |

- **Ziel = echte IP im Heimnetz mit Port.** `localhost` oder interne Namen gehen nicht, der Container ist ein eigener Rechner. `http://` wird ergänzt, HTTPS-Ziele schreibst du mit `https://`.
- **Hostname** ohne `https://` und ohne Port, nur Kleinbuchstaben.
- **Die Hauptdomain ist optional.** Du kannst sie leer lassen und alles als Subdomain eintragen, auch Home Assistant (`ha.<DEINE-DOMAIN>`). Mindestens ein Eintrag muss da sein.
- **„TLS-Zertifikat nicht prüfen“:** nur bei `https://`-Zielen mit selbst ausgestelltem Zertifikat (Proxmox, Router) nötig, sonst Fehler 502. Überall vorbelegt.
- **„Chunked Encoding aus“:** nur bei Bedarf, z. B. für die Proxmox-Konsole.
- **Erweitert:** Tunnelname (Standard `proxmox-tunnel`), Protokollstufe, Fangregel (leer = nicht eingetragene Domains bekommen „404“), Post-Quantum, Zusatzparameter, GitHub-Repo für Updates.

**Domains ein- und ausschalten:** Der Haken vor jeder Domain (Übersicht oder Reiter „Domains“). Eine deaktivierte Domain bleibt gespeichert, kommt aber nicht in die Tunnel-Konfiguration und antwortet mit „404“.
Beim Umschalten wird sofort gespeichert und gefragt, ob der Tunnel neu starten soll (wenige Sekunden Unterbrechung für alle Domains). Bei „Später“ bleibt oben ein Hinweis mit dem Knopf „Jetzt neu starten“.

### Schritt 3: Übernehmen und testen
1. **„Probelauf“** prüft alles, ohne etwas zu ändern. Im Protokoll darf nichts Rotes stehen.
2. **„Speichern & übernehmen“** legt den Tunnel an, setzt die DNS-Einträge, schreibt die Konfiguration und startet den Dienst. Bei einem Fehler bleibt die letzte funktionierende Konfiguration aktiv.
3. Am Ende steht „Tunnel verbunden“, und die Übersicht zeigt „verbunden (4 Verbindungen)“. Vier sind normal: cloudflared verbindet sich zur Ausfallsicherheit immer mehrfach mit Cloudflare.
4. Teste deine Domains mit dem **Handy im Mobilfunknetz** (WLAN aus), damit du wirklich „von außen“ prüfst.

> **„Nur speichern“** ändert den laufenden Tunnel nicht. Erst „Speichern & übernehmen“ macht die Änderung wirksam.

### Sicherung (Reiter „Sicherung“)
„Einstellungen exportieren“ speichert Domains, Namen, Ziele und Optionen als `.json`-Datei. **Passwort und Cloudflare-Zugangsdaten sind nicht enthalten.**
Ein Import speichert nur; danach prüfen und „Speichern & übernehmen“. Die Update-Quelle (GitHub-Repo) wird nie importiert.
Bei einem neuen Container meldest du dich nach dem Import bei Cloudflare an. Existiert der Tunnel gleichen Namens dort noch, nimm einen neuen Tunnelnamen oder lösche den alten im Cloudflare-Dashboard.

## 4. Home Assistant einstellen
*Nur nötig, wenn Home Assistant hinter dem Tunnel steht.* Der Container muss dort als **vertrauenswürdiger Proxy** eingetragen sein. Sonst kommt „400 Bad Request“, und die IP-Sperre von HA würde im Ernstfall den Container und damit alle Zugriffe von außen sperren.

**Per Oberfläche:** Einstellungen → System → Netzwerk → *HTTP-Server* → *Reverse-Proxy*: „X-Forwarded-For vertrauen“ einschalten, `<LXC-IP>` bei den vertrauenswürdigen Proxys eintragen (HA macht `/32` daraus), speichern. HA startet dabei neu.

**Oder in der `configuration.yaml`:**
```yaml
http:
  use_x_forwarded_for: true
  trusted_proxies:
    - <LXC-IP>      # nur diese eine IP (oder /32), nicht /24 mit Host-Adresse
```
Die Internet-URL in HA (Einstellungen → System → Netzwerk) sollte die Adresse sein, die du wirklich nutzt, z. B. `https://ha.<DEINE-DOMAIN>`. Ob HA auf der Hauptdomain oder einer Subdomain liegt, ist für den Tunnel und für HA dasselbe.


## 5. Updates
Es wird nichts automatisch aktualisiert, du entscheidest per Klick. Der Reiter **Updates** zeigt den Stand; die Übersicht weist auf neue Versionen hin.

| Was | Quelle | Einspielen | Wirkung |
|---|---|---|---|
| **cloudflared** | Paketquelle von Cloudflare | „cloudflared aktualisieren“ | Tunnel startet kurz neu |
| **Weboberfläche** | neues Release auf GitHub | „Oberfläche aktualisieren“ | Oberfläche startet neu, der **Tunnel läuft weiter**; danach wirst du automatisch zur Übersicht geleitet |
| **Debian im Container** | Debian | in der Container-Konsole `apt update && apt upgrade` | Betriebssystem-Updates |

Bei einem Update der Oberfläche wird nur der Ordner `app` ausgetauscht. **Einstellungen, Passwort, Cloudflare-Anmeldung und Tunnel bleiben erhalten.** Die alte Version liegt als `/opt/cloudflared-webui/app.old` bereit.
Zurück zur alten Version (Container-Konsole oder `pct exec <ID> -- …`):
```bash
rm -rf /opt/cloudflared-webui/app && mv /opt/cloudflared-webui/app.old /opt/cloudflared-webui/app && systemctl restart cloudflared-webui
```
Die installierte Version steht unten auf jeder Seite. Eine eigene Version (Fork) trägst du unter *Domains → Erweitert → GitHub-Repo* ein.
**Für Entwickler:** `app/VERSION` erhöhen und nach `main` pushen; eine GitHub-Action legt Tag und Release an.

## 6. Fehlersuche
Protokoll: Reiter **Protokolle**, oder vom Proxmox-Host `pct exec <ID> -- journalctl -u cloudflared-tunnel -n 30 --no-pager`.

| Problem | Ursache | Lösung |
|---|---|---|
| „Noch nicht bei Cloudflare angemeldet“ | Anmeldung fehlt | Reiter *Cloudflare* → anmelden, Domain anklicken |
| Anmeldung kommt nicht weiter | Link abgelaufen oder Domain nicht angeklickt | Neu starten, Domain anklicken; Anmeldeart wie beim Konto (E-Mail oder Google/Apple/GitHub) |
| **Fehler 1033** | Tunnel nicht verbunden | Übersicht und Protokoll prüfen; ausgehendes UDP 7844 freigeben oder Zusatzparameter `--protocol=http2` |
| **Fehler 502** | Ziel nicht erreichbar oder TLS-Problem | IP und Port prüfen; bei HTTPS-Zielen „TLS-Zertifikat nicht prüfen“ anhaken. Test: `pct exec <ID> -- curl -kI http://<ZIEL-IP>:<PORT>` |
| **400 Bad Request** bei der HA-Domain | Container nicht als Proxy in HA | Abschnitt 4 |
| „has host bits set“ in HA | `/24` bei einer einzelnen IP | Nur die IP oder `/32` eintragen |
| Änderung wirkt nicht | nur „Nur speichern“ geklickt | „Speichern & übernehmen“ oder „Jetzt neu starten“ |
| Proxmox-Konsole bricht ab | Chunked Encoding | Bei der Proxmox-Domain „Chunked Encoding aus“ anhaken |
| Seite „nicht gefunden“ nach der Umstellung | DNS-Zwischenspeicher | Einige Minuten warten, `Strg+F5` |
| Weboberfläche nicht erreichbar | Container aus oder IP geändert | In Proxmox prüfen; IP: `pct exec <ID> -- hostname -I` |

## 7. Sicherheit und Aufräumen
- **Kein Port im Router** nötig, `cloudflared` baut nur ausgehende Verbindungen auf.
- **Weboberfläche nur im Heimnetz** (Port `8099`, HTTP). Nicht über den Tunnel veröffentlichen. Passwortschutz mit Sperre nach Fehlversuchen.
- **Geheime Dateien** (nur `root` lesbar): `/var/lib/cloudflared-webui/` (`tunnel.json`, `cert.pem`) und `/etc/cloudflared-webui/` (Einstellungen). Wer `tunnel.json` hat, kann deinen Tunnel betreiben.
- **Datenverkehr** läuft über Cloudflare, wie bei jedem Cloudflare Tunnel. Sensible Dienste zusätzlich absichern.
- **Feste IP** für den Container (Menü „Erweitert“ oder DHCP-Reservierung), und den Container in die Proxmox-Sicherung aufnehmen.
- **Deinstallieren:** Container stoppen und löschen (`pct stop <ID> && pct destroy <ID>`), im Cloudflare-Dashboard den Tunnel (*Zero Trust → Networks → Tunnels*) und die CNAME-Einträge (*DNS*) löschen.

## 8. Häufige Fragen
**Brauche ich Home Assistant?** Nein, jeder Dienst mit IP und Port im Heimnetz geht.

**Docker statt LXC?** Gebaut ist es für Proxmox-LXC. Die Oberfläche läuft aber auf jedem Debian/Ubuntu: `install/install.sh` als `root` ausführen.

**Mehrere Domains?** Die Anmeldung gilt für die Domain, die du anklickst; ihre Subdomains funktionieren. Für eine zweite Domain nimm einen zweiten Container.

**Cloudflare-Passwort vergessen?** Der laufende Tunnel braucht keins. Neu anmelden musst du dich nur für einen neuen Tunnel.

**Gehen Einstellungen bei Updates verloren?** Nein.

**Fehler oder Wünsche?** Als *Issue* hier im Repository, mit Version (unten auf jeder Seite) und Protokoll, aber **ohne** Domains, IPs oder Tokens.

## Entwicklung und Lizenz
Tests: `python3 -m unittest discover -s tests` (optional `CLOUDFLARED_BIN=/pfad/cloudflared`). Struktur: `ct/` (Skript für den Proxmox-Host), `install/` (Installation im Container), `app/` (Weboberfläche, Python/Flask), `systemd/` (Dienste), `tests/`.
Lizenz: MIT, siehe [LICENSE](LICENSE).
