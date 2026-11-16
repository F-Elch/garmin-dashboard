# Garmin-Dashboard

Persönliches Gesundheits- und Trainings-Dashboard als **eine HTML-Datei**, die offline auf PC und Handy läuft.
Ein Python-Skript holt deine Garmin-Daten, rechnet die Modelle und schreibt alles als JSON in `garmin-dashboard.html`.

```
garmin-dashboard/
├── update.py          Daten holen (inkrementell), Modelle rechnen, HTML bauen
├── template.html      Oberfläche (HTML/CSS/JS, eigene SVG-Diagramme, keine externen Abhängigkeiten)
├── update.bat         Doppelklick-Start unter Windows
├── .env.example       Vorlage für die Zugangsdaten
├── .env               deine Zugangsdaten (selbst anlegen, nie weitergeben)
├── .garmin_tokens/    Login-Tokens (entsteht beim ersten Login)
└── cache/             lokale Kopie deiner Garmin-Daten (JSON)
```

## Einrichtung (einmalig)

1. Python 3.10 oder neuer installieren (python.org, Haken bei „Add to PATH“).
2. Bibliothek installieren:
   ```
   python -m pip install garminconnect
   ```
3. `.env.example` nach `.env` kopieren und E-Mail und Passwort deines Garmin-Connect-Kontos eintragen.
4. Erster Lauf: `update.bat` doppelklicken (oder `python update.py`).
   - Ist bei Garmin die Zwei-Faktor-Anmeldung aktiv, fragt das Skript nach dem Code.
   - Der erste Lauf lädt alle Tage seit 1. Juni 2024 (drei Abrufe pro Tag) und dauert etwa 15–30 Minuten.
     Abbrechen mit Strg+C ist kein Problem: Bereits geladene Tage bleiben im Cache.

Zum Ausprobieren ohne Login: `python update.py --demo` erzeugt `garmin-dashboard-demo.html` mit erfundenen Daten.

## Aktualisieren

`update.bat` doppelklicken oder `python update.py` ausführen. Es werden nur Tage geladen, die noch fehlen, plus die letzten 3 Tage
(Garmin rechnet Schlaf und HRV oft nach). Danach öffnest du `garmin-dashboard.html` im Browser.

- `python update.py --offline` baut die HTML nur aus dem Cache neu, ohne Garmin abzufragen (z. B. nach Änderungen an `template.html`).
- Zielgewicht und Klausurtermine stehen in `personal.json` (Vorlage: `personal.example.json`, wird nicht hochgeladen).
  Startdatum und Ferien stehen oben in `update.py`.

## Automatisch aktualisieren

In der Windows-Aufgabenplanung gibt es den Task **„Garmin-Dashboard aktualisieren“**. Er startet `update.py --auto` täglich
um 7:30 und 19:00 Uhr. War der PC zu dieser Zeit aus, läuft er nach, sobald der PC wieder an ist und Internet hat.

- Die fertige Datei wird zusätzlich nach `OneDrive\Privat\Garmin\garmin-dashboard.html` kopiert (änderbar in `.env`).
  Damit ist sie auf Handy und anderen Geräten über die OneDrive-App bzw. onedrive.com erreichbar.
- Jeder Lauf schreibt in `update.log`. Bei Problemen steht dort die Ursache.
- Den ersten Login mit Bestätigungscode musst du einmal von Hand machen (`update.bat`). Danach reichen die gespeicherten Tokens.
  Verlangt Garmin später erneut einen Code, steht das im Log, und du startest `update.bat` wieder einmal von Hand.
- Task ändern oder löschen: Aufgabenplanung öffnen (Start → „Aufgabenplanung“) → „Garmin-Dashboard aktualisieren“.
  Oder in PowerShell: `Unregister-ScheduledTask -TaskName "Garmin-Dashboard aktualisieren"`.
- Wird der Projektordner verschoben, muss der Task neu angelegt werden, weil er den Pfad zu `update.py` fest enthält.

## Ohne PC: GitHub Actions + verschlüsselte Seite

GitHub führt `update.py --cloud` zweimal täglich aus (06:15 und 19:00 Uhr Winterzeit) und veröffentlicht die Seite auf
GitHub Pages, z. B. `https://<dein-name>.github.io/garmin-dashboard/`. Der PC muss dafür nicht laufen.

**Was wo liegt:**
- **Repository (öffentlich):** nur Code (`update.py`, `template.html`, Workflow). Keine Daten, keine Zugangsdaten,
  keine persönlichen Einstellungen.
- **GitHub-Secrets** (nur für den Workflow lesbar): `GARMIN_TOKENS` (eigene Garmin-Sitzung für die Cloud) und `DASHBOARD_PASSWORD`.
- **Actions-Cache:** `state.enc` = Datencache und aktuelle Garmin-Tokens, AES-256-verschlüsselt mit deinem Dashboard-Passwort.
- **GitHub Pages:** `index.html`. Die Daten darin sind AES-256-GCM-verschlüsselt und werden erst im Browser
  mit dem Passwort entschlüsselt (PBKDF2, 600.000 Runden). Ohne Passwort sieht man nur ein Eingabefeld.

Die Sicherheit hängt an deinem Dashboard-Passwort. Nimm 4–5 zufällige Wörter, z. B. aus einem Passwortmanager, und nirgends sonst verwendet.

**Einrichtung (einmalig):**
1. Auf github.com ein neues **öffentliches** Repository `garmin-dashboard` anlegen (ohne README) und diesen Ordner hochladen.
2. Eigene Garmin-Tokens für die Cloud erzeugen: `python update.py --cloud-login` (fragt ggf. nach dem Bestätigungscode).
   Den Inhalt von `cloud_tokens.json` kopieren, danach die Datei löschen.
3. Im Repository: **Settings → Secrets and variables → Actions → New repository secret**
   - `GARMIN_TOKENS` = Inhalt von `cloud_tokens.json`
   - `DASHBOARD_PASSWORD` = dein Dashboard-Passwort (mind. 12 Zeichen)
   - `DASHBOARD_CONFIG` = Inhalt deiner `personal.json` (Zielgewicht, Klausurtermine; optional)
4. **Settings → Pages → Build and deployment → Source: GitHub Actions.**
5. **Actions → „Dashboard aktualisieren“ → Run workflow.** Der erste Lauf lädt alle Tage (ca. 20–40 Minuten), danach nur noch neue.

**Wenn es hakt:** Schlägt ein Lauf fehl, schickt GitHub dir eine E-Mail. Steht im Log „Keine gültigen Garmin-Tokens“,
`python update.py --cloud-login` wiederholen und `GARMIN_TOKENS` neu setzen. Änderst du `DASHBOARD_PASSWORD`, lädt der
nächste Lauf alles neu, und auf deinen Geräten musst du das neue Passwort eingeben.

Lokal kannst du die Verschlüsselung mit `python update.py --offline --encrypt` ausprobieren.

## Aufs Handy

Die HTML-Datei ist komplett eigenständig (kein Internet nötig). Wege aufs Handy:

- **OneDrive (automatisch, siehe oben)**: In der OneDrive-App `Privat/Garmin/garmin-dashboard.html` antippen →
  „Öffnen in …“ / „Herunterladen“ und im Browser öffnen. Die Vorschau der OneDrive-App führt die Diagramme meist nicht aus.
- **USB-Kabel oder E-Mail an dich selbst**: Datei in „Downloads“ speichern und mit Chrome/Firefox öffnen
  (Android: in Chrome `content://`- bzw. Dateimanager-Link; iPhone: Dateien-App → Datei antippen).

Achtung: Die Datei enthält deine Gesundheitsdaten. Gib sie nur an Personen weiter, die sie sehen dürfen.
Auf manchen Handys speichert der Browser bei `file://` keine Einstellungen. Das Dashboard funktioniert trotzdem,
merkt sich dann aber Zeitraum und Gewichte nicht.

## Datenschutz und Sicherheit

- Zugangsdaten stehen nur in `.env` und die Tokens nur in `.garmin_tokens/`. Beide sind in `.gitignore` eingetragen.
- Das Skript gibt Zugangsdaten nie aus. Nach dem Bauen durchsucht es die HTML nach jedem Wert aus `.env` und den Token-Dateien.
  Wird etwas gefunden, bricht es ab und schreibt die Datei **nicht**.
- Die HTML lädt nichts nach. Eine Content-Security-Policy (`default-src 'none'`) verhindert jede Netzwerkverbindung.

## Was die Seiten zeigen

| Seite | Inhalt |
|---|---|
| Heute | Readiness-Score (Gewichte per Regler), Fazit, Warnhinweise, 7 Tage vs. 90 Tage |
| Erholung | HRV und Ruhepuls mit persönlichem Normalband, Strukturbrüche, Erholungskurve nach harten Tagen, auffällige Tage |
| Schlaf | Dauer und Phasen, Score, Ein- und Aufwachzeit, Schlafschuld, Social Jetlag, Wochentage Schulzeit vs. Ferien |
| Training | TRIMP, ACWR, Fitness/Ermüdung/Form, Pulszonen pro Monat, Krafttrainings pro Woche, Laufökonomie |
| Körper | Gewicht mit Kalman-Trend, Rate, Prognose bis zum Zielgewicht, Muskelmasse, Körperfett |
| Zusammenhänge | frei wählbares Streudiagramm (r, n, p), stärkste Zusammenhänge, Saisonvergleich |
| Prognosen | HRV und Ruhepuls morgen (Ridge, Walk-Forward, Vergleich mit naiver Prognose), Gewicht 14 Tage, Schlafscore 7 Tage |
| Methodik | jede Kennzahl erklärt, Datenstand |

## Bekannte Grenzen

- `garminconnect` ist keine offizielle Garmin-Schnittstelle. Ändert Garmin seine Webseite, kann ein Update der Bibliothek nötig sein:
  `python -m pip install -U garminconnect`.
- Hauttemperatur liefert nicht jede Uhr. Ohne sie läuft die Frühwarnung mit den übrigen Werten.
- Die Ferientermine (NRW, Ferienordnung) sind bis 2027 eingetragen. Spätere Jahre bitte in `update.py` ergänzen.
