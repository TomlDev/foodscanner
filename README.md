<p align="center"><img src="assets/logo.svg" alt="foodscanner" width="420"></p>

# foodscanner

Telegram-Benachrichtigung, sobald in deinen [foodsharing](https://foodsharing.de)-Bezirken ein **neuer Betrieb** auftaucht oder ein **Team wieder aufmacht**.

In vielen Bezirken haben die beliebten Betriebe Einlassstopp, und der übliche Rat lautet: „Schau regelmäßig auf die Karte.“ foodscanner übernimmt das Nachschauen. Er prüft alle 15 Minuten die Betriebe in deinen Bezirken und schreibt dir nur, wenn sich etwas ändert.

```
🔔 Team öffnet!
GE Musterbäckerei (Gelsenkirchen)
Musterstraße 1, 45879 Gelsenkirchen · 📍 3,2 km Luftlinie
Status: kooperiert · Team: 🆘 sucht Unterstützung
┃ Abholung Mo–Fr abends. Bitte nur mit Hygieneschulung bewerben.
Team: 🔒 geschlossen → 🆘 sucht Unterstützung
```

## Was gemeldet wird

- 🆕 neue Betriebe
- 🔔 ein Team öffnet sich (geschlossen → offen / sucht Unterstützung)
- ✏️ andere Änderungen am Team- oder Kooperationsstatus
- 🗑 Betriebe, die nicht mehr gelistet sind

Jede Meldung enthält Adresse, Entfernung, den Infotext des Betriebs und einen Link zur Karte, über den du dich direkt bewerben kannst.

Beim ersten Lauf bekommst du eine Übersicht aller Betriebe, die gerade offen sind. Danach meldet foodscanner nur noch Änderungen.

## Filter

Standardmäßig werden **nur Abholbetriebe** gemeldet. Nicht gemeldet werden:

- Abgabestellen und Orga-Betriebe wie Einarbeitung oder Putzdienst
- Betriebe mit „Test“ oder „Abgabestelle“ im Namen
- Betriebe, in deren Team du schon bist

Optional kannst du in `.env` zusätzlich filtern:

| Einstellung | Wirkung |
|---|---|
| `MAX_DISTANCE_KM=8` | nur Betriebe im Umkreis (Luftlinie ab deiner Profiladresse) |
| `HOME_LAT` / `HOME_LON` | anderer Startpunkt für die Entfernung |
| `REGION_IDS=284,100` | nur diese Bezirke statt aller, in denen du Mitglied bist |
| `EXCLUDE_IDS=12345` | einzelne Betriebe ausblenden (ID aus `…/karte?bid=12345`) |
| `EXCLUDE_HOME_ONLY=Herne` | Betriebe ausblenden, deren Infotext **nur** Foodsaver mit diesem Stammbezirk zulässt („Stammbezirk Herne bevorzugt“ bleibt drin) |

Der Zustand wird immer vollständig gespeichert. Wenn du einen Filter später änderst, bekommst du deshalb keine Flut alter Meldungen.

## Einrichtung

Du brauchst einen Rechner, der dauerhaft läuft (Server, Raspberry Pi, NAS …), mit Python 3.8+.

```bash
git clone https://github.com/<user>/foodscanner.git
cd foodscanner
pip install -r requirements.txt
cp .env.example .env && chmod 600 .env
```

1. Trag in `.env` deine foodsharing-Zugangsdaten ein.
2. Schreib in Telegram an [@BotFather](https://t.me/BotFather), lege mit `/newbot` einen Bot an und trag das Token als `TELEGRAM_BOT_TOKEN` ein.
3. Schreib deinem neuen Bot eine beliebige Nachricht und hol dir dann deine Chat-ID:
   ```bash
   ./foodscanner.py --chat-id
   ```
   Trag die ausgegebene `TELEGRAM_CHAT_ID` ein.
4. Teste die Verbindung:
   ```bash
   ./foodscanner.py --test      # Testnachricht an Telegram
   ./foodscanner.py --dry-run   # zeigt die Meldungen nur an, sendet und speichert nichts
   ```
5. Richte den regelmäßigen Lauf ein (`crontab -e`):
   ```
   */15 * * * * /usr/bin/python3 /pfad/zu/foodscanner/foodscanner.py >> /pfad/zu/foodscanner/foodscanner.log 2>&1
   ```

Alle Einstellungen können statt in `.env` auch als Umgebungsvariablen gesetzt werden, z. B. für Docker oder systemd.

## Gut zu wissen

- **Inoffiziell:** foodscanner nutzt die interne REST-API von foodsharing ([Doku](https://foodsharing.de/api/doc/)). Sie kann sich jederzeit ändern. Pro Lauf sind es etwa 8 Anfragen, bitte nicht öfter als alle 15 Minuten laufen lassen.
- **Zugangsdaten:** Dein foodsharing-Passwort liegt im Klartext in `.env` auf dem Rechner. Schütze die Datei (`chmod 600`) und nutze foodscanner nur auf Geräten, denen du vertraust.
- **2FA:** Konten mit Zwei-Faktor-Anmeldung werden derzeit nicht unterstützt.
- **Fairness:** Eine schnelle Meldung ist kein Freifahrtschein. Lies den Infotext, halte dich an die Regeln der Betriebsverantwortlichen und bewirb dich nur, wenn du die Abholungen auch wirklich übernehmen kannst.
- **Fehler:** Wenn der Login oder die API nicht funktioniert, schickt foodscanner eine ⚠️-Nachricht, einmal pro Fehlerart.

Dies ist ein privates Projekt und steht in keiner Verbindung zu foodsharing e. V.

Tipp: `assets/icon.png` eignet sich als Profilbild für deinen Bot (bei @BotFather mit `/setuserpic`).

## Lizenz

[MIT](LICENSE)
