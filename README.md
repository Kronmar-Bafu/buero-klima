# Büroklima

Zwei Govee H5179 messen Temperatur und Luftfeuchtigkeit. Ein Laptop holt den
gespeicherten Verlauf per Bluetooth ab, sobald er in Reichweite ist, und legt
ihn als CSV in diesem Repo ab. `index.html` zeigt die Werte als statische Seite
(GitHub Pages). Es gibt keine Cloud, kein WLAN am Sensor und kein Backend.

## Aufbau

| Datei | Zweck |
|---|---|
| `govee_sync.py` | Verlauf abholen, CSV schreiben, optional committen und pushen |
| `data/<Sensor>/<JJJJ-MM>.csv` | Minutenwerte, Zeit in UTC |
| `data/index.json` | Dateiliste und Batteriestand für das Dashboard |
| `index.html`, `vendor/` | Dashboard (uPlot ist mit eingecheckt, keine externen Abhängigkeiten) |
| `config.json` | Titel, Sensornamen, Komfortbereich, Arbeitszeit |
| `systemd/` | stündlicher Timer |

## Einrichten

1. Repo auf GitHub anlegen, diesen Ordner als `~/buero-klima` klonen bzw. pushen.
2. Erster Abruf (holt bis zu 20 Tage, dauert einige Minuten pro Sensor):

   ```bash
   uv run govee_sync.py -v
   ```

3. Dashboard lokal ansehen:

   ```bash
   python3 -m http.server 8000     # dann http://localhost:8000
   ```

   Direkt als Datei geöffnet (`file://`) lädt die Seite keine Daten.
4. In `config.json` den Sensoren Namen geben, z. B. `"label": "Fensterplatz"`.
5. GitHub Pages aktivieren: Settings → Pages → Branch `main`, Ordner `/ (root)`.

## Stündlicher Abgleich

```bash
mkdir -p ~/.config/systemd/user
cp systemd/buero-klima.* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now buero-klima.timer
```

Die Unit erwartet das Repo unter `~/buero-klima` und uv unter `~/.local/bin/uv`
(`which uv` prüfen und sonst in `buero-klima.service` anpassen).

Nachsehen, was passiert ist:

```bash
systemctl --user list-timers buero-klima.timer
journalctl --user -u buero-klima.service -n 30
```

`Persistent=true` holt einen verpassten Lauf nach, sobald der Laptop wieder
läuft. Ist kein Sensor in Reichweite, endet der Lauf ohne Änderung.

### Push ohne Passwortabfrage

Der Timer läuft ohne Terminal. `git push` muss deshalb ohne Rückfrage
funktionieren, zum Beispiel mit einem SSH-Schlüssel ohne Passphrase, der als
Deploy Key (mit Schreibrecht) nur für dieses Repo hinterlegt ist. Schlägt ein
Push fehl, bleibt der Commit lokal und geht beim nächsten Lauf mit hoch.

## Gut zu wissen

- Der Sensor speichert rund 20 Tage. Wer seltener in Reichweite ist, bekommt Lücken.
- Die Govee-App muss geschlossen sein, der Sensor erlaubt nur eine Verbindung.
- Ein abgebrochener Download ist unkritisch: Die Daten kommen älteste zuerst,
  der nächste Lauf setzt dort fort.
- `uv run govee_sync.py --max-minutes 30 --dry-run -v` zeigt, was abgeholt
  würde, ohne etwas zu schreiben.

Das Bluetooth-Protokoll stammt aus
[GoveeBTTempLogger](https://github.com/wcbonner/GoveeBTTempLogger) (MIT).
