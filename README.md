# Trainings-Dashboard für den Wettkampf

Kalender für geplante Einheiten, Trainings aus Garmin Connect und Auswertungen durch Claude.
Adresse: https://training.example.org

## Aufbau
- `triathlon-web`: Web-App als Benutzer `triathlon`, nur auf 127.0.0.1:8090 erreichbar. nginx terminiert TLS auf Port 443.
- `triathlon-worker`: wertet die Warteschlange mit `claude -p` aus und beantwortet den Trainer-Chat, ohne Tools und ohne Netzport. Planvorschläge kommen erst per Knopf in den Kalender. Nutzt die Claude-Anmeldung von root.
- `data/`: SQLite-Datenbank und Garmin-Key (nur für `triathlon` lesbar).

## Bedienung
- Passwort setzen oder ändern (meldet alle Geräte ab): `/opt/triathlon-dashboard/set-password`
- Garmin-Key eintragen: im Dashboard unter Einstellungen. Garmin-Keys laufen je nach Wahl nach 30 bis 365 Tagen ab.
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
Die Adresse liegt in `data/calendar.urls` und wird nicht mitgesichert, die zuletzt gelesenen Termine schon.

## WHOOP
Liefert Recovery, HRV, Ruhepuls, Schlaf und Tagesbelastung (nur lesend, WHOOP Developer API v2). Einmalig im
[WHOOP Developer Dashboard](https://developer-dashboard.whoop.com) eine App anlegen: Redirect URL `https://training.example.org/api/whoop/callback`,
Berechtigungen read:recovery, read:cycles, read:sleep. Client ID und Secret unter Einstellungen → WHOOP eintragen und verbinden.
Abgleich zusammen mit Garmin alle 30 Minuten, beim Verbinden 60 Tage rückwirkend. Werte zählen für den Tag des Aufwachens.
Sobald WHOOP-Daten da sind, zeigen „Heute“ und die Erholungskurven WHOOP statt Garmin, der Trainer sieht beide.
Zugangsdaten liegen in `data/whoop.json` und werden nicht mitgesichert. „Verbindung trennen“ entzieht den Zugriff auch bei WHOOP.

## Sicherung
Jede Nacht um 03:30 Uhr deutscher Zeit (`triathlon-backup.timer`) landet eine geprüfte Kopie der Datenbank in
`/var/backups/triathlon-dashboard`. Die letzten 30 bleiben erhalten. Sie liegen nur auf diesem Server und helfen
daher nicht, wenn der Server selbst ausfällt. Der Garmin-Key wird nicht mitgesichert.

Von Hand sichern: `systemctl start triathlon-backup`

Wiederherstellen (DATEI durch den gewünschten Namen ersetzen):
```
systemctl stop triathlon-web triathlon-worker
cd /opt/triathlon-dashboard/data
cp triathlon.db vor-wiederherstellung.db
rm -f triathlon.db-wal triathlon.db-shm
gunzip -c /var/backups/triathlon-dashboard/DATEI > triathlon.db
chown triathlon:triathlon triathlon.db
systemctl start triathlon-web triathlon-worker
```

## Abgleich
Alle 30 Minuten und per Knopf. Der erste Abgleich holt 180 Tage Trainings und 60 Tage Erholungsdaten.
Neue Einheiten der letzten 3 Tage werden automatisch ausgewertet, ältere nur per Knopf.
Standort- und Kontodaten werden aus den Daten für die Auswertung entfernt.
