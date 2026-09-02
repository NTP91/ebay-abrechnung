# Wiederherstellung – Prüfstand vor Änderungen

Branch: `codex/recover-payout-settlement`, Basis: `8c8e60e`.
Der vorherige lokale Stand ist als Commit `e32c6b5` auf
`codex/workspace-backup-20260902` gesichert. `main` wird nicht verändert.

## Nachgewiesene Fehler

- Der aktuelle Upload überschreibt Master_Orders.csv / Master_Payouts.csv:
  bestehende, nicht erneut hochgeladene Datensätze gehen verloren.
- Die aktuelle CSV-Leselogik prüft nur Zeile 0; Parserfehler werden als leere
  DataFrames verschluckt und unbemerkt von der Verarbeitung ausgeschlossen.
- Produktname/SKU werden durch ein einziges Dictionary pro Bestellnummer
  überschrieben. Mehrartikelbestellungen können falsch zugeordnet werden.
- Payout-Produktnamen werden nicht priorisiert; fehlende SKU wird als NB geraten.
- Die Betragswahl nimmt beliebige passende Spalten und setzt Fehler auf 0.
- Gebühren ohne Bestellung werden einem Partner zugeordnet und als Gutschrift
  ausgegeben. MH-Unterpräfixe werden nicht zusammengefasst.
- Der aktuelle Stand enthält keine aktive Rechnungs-API oder persistente
  Rechnungssperre. Ein bloßer Neustart stellt diese Funktionen nicht wieder her.

## Historie

Alle Python-Dateien aller 115 Repository-Commits wurden auf Syntax geprüft.
Der frühere IndentationError liegt in core.py:97 (z.B. f2634fb bis 79d6eb0),
nicht zwingend im angezeigten Importaufruf in app.py.
891bc3e ist der jüngste syntaktisch gültige Stand vor dieser Fehlerphase mit
Rechnungs-API, aber kein nachgewiesener fachlich stabiler Stand.
f803b06 enthält die ältere positionsweise API-Logik: Menge 1, Nettopreis
round(Payout-Betrag / 1.19, 2), 19% Umsatzsteuer, 0,5% Rabatt.
Ein Rücksetzen auf einen dieser Commits ist nicht als sichere Reparatur belegt.

## Fehlende Abnahmeunterlagen

Im übergebenen Anhang liegt ausschließlich das Briefing. Die vier echten
Payout-Dateien, Bestellberichte und das Rechnungsbeispiel fehlen.
Die Sollwerte (3 Payouts, 50 Zeilen, 4.427,83 EUR, 49 Produktnamen) können deshalb
noch nicht gegen Originaldaten geprüft werden. Kein produktiver API-Test.

## Reparierter Zwischenstand (nicht produktionsfreigegeben)

- CSV-/XLSX-Kopfzeilenerkennung, BOM und deutsche Geldbeträge. Ungültige
  Geldwerte oder beschädigte Transaktionszeilen führen zum sichtbaren Abbruch,
  nicht zu 0 EUR oder stillschweigendem Weglassen.
- CSV-Masters bleiben erhalten; Imports ergänzen unter Dateisperre und mit
  atomarem Austausch. Abweichende Versionen bekannter Payouts werden abgelehnt.
- Bestellidentität verwendet Bestell-/Transaktions-/Artikelnummer gemeinsam.
  Widersprüche werden nicht automatisch überschrieben.
- Payout-Produktdaten haben Vorrang. Ergänzung über Transaktion, dann
  Bestellung+Artikel, dann eindeutige Bestellung; Mehrdeutigkeit bleibt sichtbar.
- MH-Unterpräfixe werden MH; fehlende SKU wird nicht länger zu NB geraten.
- Gebühren ohne Bestellbezug bleiben separat. Erstattungen erscheinen nicht
  in Verkaufsrechnungen; Gruppe-A-/Gruppe-B-Provisionen werden gespiegelt.
- Offline-Rechnungsdaten pro Auszahlung: Produktname, SKU, Bestellnummer,
  Payoutnummer; Menge 1, historischer Nettopreis, 19%, Rabatt 0,5%.
- Export weiterhin openpyxl. Löschen wurde durch bestätigtes Archivieren ersetzt.

## Tests

`python -B -m unittest test_recovery -v`: 14 synthetische Regressionstests bestanden.
Zusätzlich Streamlit-AppTest ohne Upload bestanden; lokaler Serverstart und
HTTP-Healthcheck erfolgreich (`ok`). Keine externen API-Aufrufe oder Rechnungen.
Der Test mit Payout-ID 7700379513 nutzt ausdrücklich synthetische Daten und
belegt NICHT die Echtdaten-Sollsumme oder die erwarteten 50 Zeilen.

## Offene Arbeit / Sperren

- Originaldateien und Rechnungsbeispiel fehlen: keine fachliche Vollabnahme,
  keine Aussage, dass alle 49 echten Positionen richtig zugeordnet werden.
- Produktive API-Wiederanbindung, getestete persistente Entwurfssperren,
  Wiederanlauf nach API-Timeout und vollständiger Workflow-Status stehen aus.
  Deshalb wird aktuell ausschließlich ein Offline-Payload erzeugt.
- Historische CSV-Dateien enthalten keine verlässliche Information über bereits
  erstellte Lexoffice-Rechnungen. Vor Freigabe ist ein Abgleich erforderlich.
- Lokale CSV-Dateien benötigen einen persistenten Datenträger. Dauerhaftigkeit
  über einen Streamlit-Cloud-Rebuild ist nicht nachgewiesen. Speicherziel klären.
- Der bisherige Status 'Ausbezahlt' darf nicht als Nachweis des Bankeingangs
  interpretiert werden; Statusmigration und Nachweise stehen aus.
- Neue UI-Gestaltung und zweite Stufe Partnerrechnungsprüfung bewusst noch nicht
  umgesetzt, da die Kernfunktion noch keine Echtdaten-Abnahme hat.
- importer.py bleibt als historisches, von der App nicht mehr genutztes Hilfsskript
  unverändert; nicht separat gegen produktive Masterdateien ausführen.

Keine Freigabe für Merge nach main oder produktives Deployment dieses Zwischenstands.
