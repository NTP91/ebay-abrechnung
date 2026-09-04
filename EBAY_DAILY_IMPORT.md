# Täglicher Finances-Import

Vorher gab es weder einen täglichen Trigger noch eine Übernahme neuer
API-Payouts in den CSV-Abrechnungsbestand. Der manuelle Trust/Risk-Abruf speicherte
nur API-Snapshots und Hold-Nachweise.

## Betrieb

Aktive Codex-Automation `payout-studio-t-glicher-ebay-import`: täglich 08:00 Uhr
in der lokalen Zeitzone Europe/Berlin. Rechner und Codex müssen verfügbar sein.
Es handelt sich nicht um einen Cloud-Dienst oder einen Windows-Systemdienst.
Verpasste Daten werden beim nächsten erfolgreichen Lauf nachgeholt.

Die Automation führt im Repository aus:

```powershell
python ebay_sync.py --data-dir "$env:LOCALAPPDATA\PaymentTool-Test\recover-6e82d0d-20260903\test-data" --trigger automatic
```

Der CLI-Einstieg verweigert andere Branches als `codex/recover-payout-settlement`.
Manuell führt „eBay-Daten aktualisieren“ dieselbe Funktion `ebay_sync.run` mit
`trigger=manual` aus. Es gibt keine zweite Import-/Freigabelogik und keine
Lexware-Aufrufe. Die Automation darf keine Codeänderungen oder Statuskorrekturen
vornehmen. Alle API-Aufrufe verwenden den bestehenden signierten GET-Client;
OAuth-Refresh ist weiterhin der einzige POST gegen eBay.

## Daten und Fortschritt

- `getPayouts` nach Payoutdatum; danach `getPayout` und vollständige, paginierte
  `getTransactions`-Bewegungen pro Payout.
- Transaktionsfenster ab dem letzten erfolgreichen Abruf minus sieben Tage.
  Beim ersten Lauf: letzter importierter Payouttag minus sieben Tage; ohne
  Ausgangsbestand 90 Tage. Lange Ausfälle werden in Teilfenstern nachgeholt.
- Bereits importierte Payouts und API-seitig noch ausstehende Payouts werden erneut
  geprüft. Offene Transaktionen und aktive Holds werden zusätzlich bestellbezogen
  abgefragt, damit alte Statuswechsel nicht aus dem Zeitfenster fallen.
- Verkäufe, Erstattungen und partnerlose Gebühren werden in das vorhandene
  Transaktionsschema überführt und durch `core.import_reports` validiert.
  Offene Verkäufe bleiben ohne Payout; spätere Auszahlung aktualisiert dieselbe
  Position. Bestellnummer und Artikelreferenzen verknüpfen den bestehenden
  Bestellbericht. Dessen Upload bleibt für fehlende Produkt-/SKU-Daten erforderlich.
- Holds, Releases und weitere Bankbewegungen bleiben im vollständigen API-Nachweis.
  Sie werden weder als neue Verkäufe noch als erfundene Gutschriften verbucht.
- Parent-Verkäufe erhalten genau eine Finanzzeile; sämtliche Artikelreferenzen
  bleiben gespeichert. Eine mehrdeutige Partnerzuordnung bleibt prüfpflichtig.
- Verschiedene Teil-Erstattungen desselben Artikels behalten separate Referenzen
  und Positionsidentitäten. API- und CSV-Reimporte erkennen gemeinsame Referenzen.

Jeder Payout muss anhand von `bookingEntry`, EUR-Beträgen, Transaktionsanzahl und
Payout-ID vollständig mit dem offiziellen API-Auszahlungsbetrag übereinstimmen.
Keine Bank-Summen aus Sales ein zweites Mal buchen. Der manuelle Bankabgleich
zeigt diesen Kontrollwert und lehnt einen abweichenden Kontrollbetrag ab.

`Settlement_Ebay_Sync.json` und ein SQLite-Spiegel speichern Checkpoint, vollständige
Finanzbewegungen, Payoutkontrollen und Laufhistorie. Beide sind im Backup enthalten.
Beschädigte/widersprüchliche Spiegel werden nicht still ignoriert. Ein Prozesslock
verhindert parallele API-Importe. Unterbrochene Läufe werden als solche markiert;
Fehler oder Teilimporte verschieben den letzten erfolgreichen Checkpoint nicht.
Bereits angenommene Positionen werden bei Wiederholung dedupliziert. Importwarnungen
für gesperrte Payouts bleiben zur Prüfung erhalten. Bestehende Holds, Rechnungs- und
Abschlusssperren werden nicht aufgehoben.

## Anzeige

Sidebar: API-Datenstand, letzter erfolgreicher automatischer Abruf und letzter
manueller Abruf. Historie → Payouts → „eBay API · Abrufhistorie“ enthält Zeitraum,
Quelle, Auslösung, neue Payouts, neue Finanzpositionen, bekannte Datensätze,
Bewegungen ausschließlich im API-Nachweis sowie Fehler-/Erfolgsstatus. Dort stehen
auch die offiziellen Bank-Kontrollbeträge. Andere Trust/Risk-Daten werden nicht
fälschlich mit einem neuen Abrufzeitpunkt versehen.

## Nachweis vom 4. September 2026

Erster Live-Lauf erfolgreich: 0 neue Payouts, 142 neue offene Finanzpositionen,
122 bekannte Datensätze und 14 weitere Bewegungen im API-Nachweis. Die fünf
vorhandenen Payouts sind vollständig geprüft. Bestellbericht, RE0089-Verwerfung,
Partnerbelege, Workflow und bestehende Sperrensicherung blieben unverändert.

Originaldaten-Replay und Wiederholung: keine doppelte Anlage; vorhandene
RE0089-Historie und Sperren identisch. Regressionen prüfen API-/CSV-Wechsel,
Parent-Summen, Teil-Erstattungen, offene Positionen, Catch-up nach Fehlern,
unpassende Bankbeträge, parallele Läufe sowie den echten manuellen UI-Einstieg.

Gesamte Testsuite: 163 Tests, 155 bestanden, 8 mangels ursprünglicher CSV-Dateien
übersprungen. Die Import-Randfälle wurden anschließend nochmals geprüft; die
Recovery-Oberfläche mit dem tatsächlich importierten API-Datenstand lief ebenfalls
ohne Ausnahme. Kein Test rief Lexware auf.

API-Verträge: [Payout-Abfragen](https://developer.ebay.com/api-docs/sell/static/finances/payout-info.html)
und [Finanztransaktionen](https://developer.ebay.com/api-docs/sell/static/finances/transaction-info.html).
