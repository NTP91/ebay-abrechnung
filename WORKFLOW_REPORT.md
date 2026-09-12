# Abschlussprüfung: Bestellbericht → Payout → Lexoffice-Entwurf

Stand: 02.09.2026, Branch `codex/recover-payout-settlement`.
Dieser Bericht ersetzt die offenen PDF-/API-Prüfpunkte der früheren Berichte.
RE0088 ist gemäß Benutzerbestätigung kein benötigtes PDF-Abnahmedokument.

## Verbindliche Produktquelle und Realtest

Produkttitel und SKU werden ausschließlich aus dem eindeutig zugeordneten
Bestellbericht übernommen. Matching: Transaktionsnummer, danach Bestellung und
Artikelnummer, danach nur eine eindeutige Bestellposition. Widersprüchliche
Bestell-/Artikelnummern oder Mehrfachtreffer sperren die Zuordnung.
Ein Payout-Titel kann niemals eine fehlende Zuordnung ersetzen; er bleibt nur
als separate Kontrollspalte sichtbar. Der gesamte betreffende Payout wird bei
einer fehlenden Zuordnung für die Rechnungsanlage blockiert.

Originaldaten erneut geprüft:

| Prüfung | Tatsächlich | Abweichung zum Soll |
|---|---:|---:|
| Payout-Dateien / eindeutige Payouts | 4 / 3 | 0 |
| Eindeutige Zeilen | 50 | 0 |
| Gesamtsumme | 4.427,83 EUR | 0,00 EUR |
| Bestellungen / Erstattungen / Gebühr | 43 / 6 / 1 | 0 |
| Bestellbezogene Positionen mit eindeutigem Bestellbericht-Titel | 49 | 0 |
| Offene Zuordnungen | 0 | 0 |
| Bestellartikel nach wiederholtem Import | 180 | 0 zusätzliche |

Für jede der 49 Zeilen wird im Test nicht nur die Quelle markiert, sondern Titel
und SKU ausdrücklich mit dem tatsächlichen passenden Bestellbericht-Datensatz
verglichen. Alle MH-Varianten bleiben MH, NB bleibt eigener Gruppe-B-Partner.
Die Gebühr -1,78 EUR bleibt ohne Partner und außerhalb der Gutschriften.

## Rechnungsdaten und API

Historische Referenz f803b06: quantity=1, netAmount=round(Payout-Betrag/1.19,2),
discountPercentage=0.5 und taxRatePercentage=19. Diese Felder sind unverändert.
Name ist der vollständige Bestellbericht-Titel; darunter enthält description
SKU, eBay-Bestellnummer und ergänzend die Payoutnummer.
Jeder Payout erhält seinen eigenen Gruppe-B-Verkaufsentwurf. Sechs Erstattungen
werden weiterhin separat in einer Gutschriftenübersicht geprüft/exportiert;
keine automatische Gutschrift oder negative Verkaufsposition.

API wieder angebunden: https://api.lexware.io/v1 (Nachfolger der historischen
api.lexoffice.io-Domain). Kundennummer 16335 wird als Kundenrolle eindeutig
aufgelöst. POST /invoices sendet ausschließlich finalize=false. Kein Versand,
keine Finalisierung. Offizielle Referenz: https://developers.lexware.io/docs/

**Alle Schreibaufrufe wurden ausschließlich mit einem simulierten HTTP-Client
getestet. Keine echte Rechnung und kein echter Entwurf wurden angelegt.**
Der reale Account-/Layout-/Berechtigungstest steht als kontrollierter manueller
Entwurfstest aus; er wurde nicht als erfolgreich behauptet.

## Dauerhafte Sperren und Status

Bestehende CSV-Masters bleiben erhalten. Neu ist Settlement_State.sqlite3 im
gleichen Datenverzeichnis mit eindeutigem Schlüssel pro Payout und Auditprotokoll.

- Status: importiert → vollständig zugeordnet bzw. Prüfung erforderlich →
  Geld eingegangen → Lexoffice-Entwurf erstellt → Partnerrechnung geprüft →
  Partner ausgezahlt → abgeschlossen.
- Geldeingang erfordert eBay-Quellstatus `Betrag überwiesen` und manuelle Bestätigung.
- Vor der ersten Erstellung muss der Benutzer bestehende Rechnungen einschließlich
  älterer Testentwürfe prüfen. Frühere, außerhalb dieser App angelegte Rechnungen
  können nicht aus einem neuen leeren Register erkannt werden.
- Vor jedem POST werden Versuchssperre, Daten-Fingerabdruck und Payload-Snapshot
  transaktional gespeichert. Parallele Klicks und Neustarts erzeugen keinen
  zweiten Versuch. Keine automatischen POST-Retries.
- Timeout, HTTP-Fehler oder fehlende Antwort-ID lassen den Payout gesperrt.
  Auch Prozessabbruch nach Reservierung bleibt gesperrt. In Lexoffice manuell
  abgleichen; es gibt absichtlich keinen ungesicherten Reset-Knopf.
- Veränderte gesperrte Abrechnungsdaten führen sichtbar zu Prüfung erforderlich;
  gespeicherter Snapshot und Entwurfs-ID bleiben erhalten.
- Folgestatus sind manuelle, protokollierte Bestätigungen, keine automatische
  Rechnungsprüfung und keine Banküberweisung. Erstattungen müssen dabei separat
  berücksichtigt werden.
- Vollbackup umfasst beide CSV-Masters und einen konsistenten SQLite-Snapshot.
  Datenlöschung/Archivierung aus dem Dashboard ist zum Schutz der Sperren deaktiviert.

## Dashboard

Navy-Oberfläche, Kennzahlenkarten, klare Import-/Prüf-/Entwurfsschritte,
vier Tabs, sichtbare Prüf-/Sperrstatus, Payout-Auswahl und Partnerdownloads.
Gruppe A hat keinen Lexoffice-Upload. Excel-Exporte verwenden weiterhin openpyxl.
Browserprüfung bei 1280px: Kennzahlen und Navigation lesbar, keine abgeschnittenen
Geldbeträge. Zusätzliche Streamlit-AppTests prüfen alle Original-Payout-Vorschauen.

## Tests

```powershell
$env:EBAY_REAL_TEST_DIR='C:\Users\servi\Downloads'
python -B -m unittest test_recovery test_real_reports test_api_workflow -q
```

**33 Tests bestanden**: 15 bestehende Regressionstests, 8 Originaldaten-Tests,
10 API-/Status-/Sperrtests. Enthalten: verbindliche Titelquelle, fehlende Zuordnung,
Originalsummen, Doppelimporte, getrennte Refunds, unveränderte Nettopreise,
alle drei Payouts mit simulierten Entwürfen, paralleler Doppelklick, Neustart,
Timeout, Prozessabbruch, HTTP-Fehler, Abschlussstatus, Änderungserkennung und Backup.
Originaldateien ausschließlich lesend verwendet. Kein echter API-Key verwendet.

## Betrieb und verbleibende Grenzen

1. `PAYMENT_DATA_DIR` auf ein bestehendes persistentes Datenverzeichnis setzen;
   ohne Angabe liegen Daten neben app.py. Dieses Verzeichnis muss mit CSVs UND
   SQLite-Datei gemeinsam gesichert/wiederhergestellt werden. Ein flüchtiges
   Streamlit-Cloud-Dateisystem allein garantiert keine dauerhaften Sperren.
2. Betrieb auf einem Server mit zuverlässigen lokalen Dateisperren/SQLite; mehrere
   Instanzen mit jeweils eigenen Dateisystemen teilen keine Rechnungssperren.
3. Deployment vor Zugriff Unbefugter schützen. Backups enthalten Finanzdaten.
   API-Key wird nur im Sitzungsspeicher verwendet, nicht im Register/Backup gespeichert.
4. Unklare API-Versuche und bereits vor dieser Version erstellte Rechnungen
   benötigen manuellen Lexoffice-Abgleich. Kein automatisches Entsperren.
5. Partnerrechnungs-Upload/automatischer Belegvergleich ist weiterhin die separate
   zweite Ausbaustufe. Gutschriften werden als Übersicht, nicht automatisch per API erstellt.
6. Streamlit meldet noch Deprecation-Hinweise für use_container_width, keine Laufzeitfehler.

Kein Merge nach main. Der kontrollierte Live-Entwurfstest ist eine separate
bewusste Benutzeraktion, nicht Teil der automatisierten Tests.
