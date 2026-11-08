# Trainings-Dashboard für Triathlon

Selbst gehostetes Dashboard für die Vorbereitung auf einen Triathlon: Kalender für geplante Einheiten, Trainings aus
Garmin Connect, Erholung von WHOOP, Termine aus Google Kalender, Auswertungen und ein Trainer-Chat mit Claude.
Mehrere Benutzer mit getrennten Daten. Oberfläche und Auswertungen sind auf Deutsch.

Jede Installation hat ihre eigenen Daten und Zugänge. Im Repository liegen keine Trainingsdaten, Schlüssel oder Passwörter,
alles davon entsteht erst auf dem eigenen Server im Ordner `data/` (steht in `.gitignore`).

## Aufbau
- `triathlon-web`: Web-App (FastAPI) als Benutzer `triathlon`, nur auf 127.0.0.1:8090 erreichbar. nginx terminiert TLS auf Port 443.
- `triathlon-worker`: wertet die Warteschlange mit `claude -p` aus und beantwortet den Trainer-Chat, ohne Tools und ohne Netzport.
  Planvorschläge kommen erst per Knopf in den Kalender.
- `triathlon-backup.timer`: nächtliche Sicherung der Datenbanken.
- `data/`: SQLite-Datenbanken und Zugangsdaten (nur für `triathlon` lesbar).

## Installation
Getestet auf Ubuntu 24.04 mit Python 3.12, nginx und certbot. Pfad und Dienste gehen von `/opt/triathlon-dashboard` aus.

```sh
# Code und Python-Umgebung
git clone https://github.com/11David1/triathlon-dashboard.git /opt/triathlon-dashboard
cd /opt/triathlon-dashboard
python3 -m venv venv && venv/bin/pip install -r requirements.txt
useradd --system --home-dir /opt/triathlon-dashboard --shell /usr/sbin/nologin triathlon
install -d -o triathlon -g triathlon -m 0750 data
install -d -o triathlon -g triathlon -m 0700 /var/backups/triathlon-dashboard

# Öffentliche Adresse eintragen (ohne sie startet die Web-App nicht)
cp deploy/triathlon-dashboard.env.example /etc/triathlon-dashboard.env
nano /etc/triathlon-dashboard.env

# Dienste
cp deploy/triathlon-*.service deploy/triathlon-backup.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now triathlon-web triathlon-worker triathlon-backup.timer
```

**nginx und Zertifikat:** In `deploy/nginx-acme.conf` und `deploy/nginx-dashboard.conf` `training.example.org` durch die
eigene Domain ersetzen. Zuerst nur die ACME-Datei aktivieren, mit `certbot certonly --webroot -w /var/www/acme -d DOMAIN`
das Zertifikat holen, dann die Dashboard-Datei aktivieren und `nginx -t && systemctl reload nginx`.

**Claude:** Der Worker ruft die [Claude Code](https://claude.com/claude-code) CLI auf (Standard: `/root/.local/bin/claude`,
änderbar mit `TRI_CLAUDE_BIN`). Es gibt zwei Wege, die pro Benutzer festgelegt werden:
- `abo`: Der Aufruf nutzt die Claude-Anmeldung des Servers (einmal als root `claude` starten und anmelden).
  Das läuft über das Konto des Betreibers, also nur für Benutzer freigeben, die das dürfen.
- `api_key` (Standard für neue Benutzer): Der Benutzer trägt unter Einstellungen → Claude einen eigenen Anthropic-API-Key ein
  (`data/users/<name>/anthropic.key`, wird nicht mitgesichert). Der Aufruf läuft mit leerer Umgebung und eigenem Home-Verzeichnis,
  sieht die Server-Anmeldung also nie, und nutzt das Modell `claude-opus-5-5`. Ohne Key bleiben die Aufträge liegen.

**Ersten Benutzer anlegen:** siehe unten, `benutzer anlegen NAME` und bei Bedarf `benutzer claude NAME abo`.

## Bedienung
- Anmeldung mit Benutzername und Passwort. Jeder Benutzer hat eigene, getrennte Daten (Plan, Garmin, WHOOP, Kalender, Trainer).
- Benutzer verwalten (`/opt/triathlon-dashboard/benutzer`, als root oder triathlon):
  - `benutzer liste`
  - `benutzer anlegen anna Anna` zeigt einen einmaligen Startcode (7 Tage gültig, nur gehasht gespeichert).
    Damit meldet sich Anna zum ersten Mal an und muss sofort ein eigenes Passwort setzen (scrypt-Hash).
  - `benutzer zuruecksetzen anna`: neuer Startcode, altes Passwort ungültig, alle Geräte abgemeldet.
  - `benutzer passwort anna`: Passwort direkt setzen (meldet alle Geräte ab).
  - `benutzer claude anna abo` bzw. `benutzer claude anna api_key`: siehe Claude oben.
- Benutzer und Sitzungen liegen in `data/users.db`, die Daten jedes Benutzers in `data/users/<name>/`,
  seine Sicherungen in `/var/backups/triathlon-dashboard/users/<name>/`.
  Wer von der früheren Einzelbenutzer-Version umsteigt, wird zum Benutzer `admin` mit den Daten direkt in `data/` und dem Abo.
- Wettkampf (Name, Datum), Anzeigename und Notizen für den Trainer: Einstellungen.
- Garmin-Key eintragen: Einstellungen. Garmin-Keys laufen je nach Wahl nach 30 bis 365 Tagen ab.
- Trainings ohne Uhr: In „Letzte Trainings“ über „+ Training hinzufügen“. Sie haben negative IDs und werden vom Garmin-Abgleich nicht angefasst.
- Beschwerden: Karte auf der Startseite. „Beschwerde ist weg“ behält den Verlauf für Trainer und Auswertung.
- Warnungen (Garmin-Key, Abgleich, Claude-Anmeldung, Dienst) erscheinen oben im Dashboard.
- Logs: `journalctl -u triathlon-web -u triathlon-worker -f`
- Tests: `cd /opt/triathlon-dashboard && PYTHONPATH=.:tests venv/bin/pytest -q tests`

## Google Kalender
Unter Einstellungen die geheime iCal-Adresse eintragen. Der Server liest die Termine alle 30 Minuten und vor Chat-Nachrichten,
wenn der Stand älter als 5 Minuten ist. Der Trainer sieht nur Titel und Uhrzeiten und kann nichts in Google ändern.
Einträge, die am Titel als Training erkannt werden (`classify_title` in `app/plan.py`, 180 Tage zurück bis 60 Tage voraus), werden zu Plan-Einträgen.
Vergangene ohne Garmin-Partner zählen als „laut Kalender“ trainiert, „Nicht gemacht“ blendet sie aus. Alle anderen Termine sieht nur der Trainer,
sie werden nur ab heute gespeichert. Erkennung korrigieren: Einstellungen → „Was zählt aus Google als Training?“.
Die Adresse liegt in `calendar.urls` im Datenordner des Benutzers und wird nicht mitgesichert, die zuletzt gelesenen Termine schon.

## WHOOP
Liefert Recovery, HRV, Ruhepuls, Schlaf und Tagesbelastung (nur lesend, WHOOP Developer API v2). Einmalig im
[WHOOP Developer Dashboard](https://developer-dashboard.whoop.com) eine App anlegen: Redirect URL `<TRI_ORIGIN>/api/whoop/callback`
(steht auch unter Einstellungen → WHOOP), Berechtigungen read:recovery, read:cycles, read:sleep.
Client ID und Secret unter Einstellungen → WHOOP eintragen und verbinden.
Abgleich zusammen mit Garmin alle 30 Minuten, beim Verbinden 60 Tage rückwirkend. Werte zählen für den Tag des Aufwachens.
Sobald WHOOP-Daten da sind, zeigen „Heute“ und die Erholungskurven WHOOP statt Garmin, der Trainer sieht beide.
Zugangsdaten liegen in `whoop.json` im Datenordner und werden nicht mitgesichert. „Verbindung trennen“ entzieht den Zugriff auch bei WHOOP.

## Sicherung
Jede Nacht um 03:30 Uhr deutscher Zeit (`triathlon-backup.timer`) landet eine geprüfte Kopie jeder Datenbank in
`/var/backups/triathlon-dashboard`. Die letzten 30 bleiben erhalten. Sie liegen nur auf diesem Server und helfen
daher nicht, wenn der Server selbst ausfällt. Zugangsdaten (Garmin, WHOOP, Claude, Kalender) werden nicht mitgesichert.

Von Hand sichern: `systemctl start triathlon-backup`

Wiederherstellen (NAME und DATEI ersetzen):
```
systemctl stop triathlon-web triathlon-worker
cd /opt/triathlon-dashboard/data/users/NAME
cp triathlon.db vor-wiederherstellung.db
rm -f triathlon.db-wal triathlon.db-shm
gunzip -c /var/backups/triathlon-dashboard/users/NAME/DATEI > triathlon.db
chown triathlon:triathlon triathlon.db
systemctl start triathlon-web triathlon-worker
```

## Abgleich
Alle 30 Minuten und per Knopf. Der erste Abgleich holt 180 Tage Trainings und 60 Tage Erholungsdaten.
Neue Einheiten der letzten 3 Tage werden automatisch ausgewertet, ältere nur per Knopf.
Standort- und Kontodaten werden aus den Daten für die Auswertung entfernt.

## Lizenz
MIT, siehe [LICENSE](LICENSE).
