# Wochenabrechnung in der Recovery-App

- Tägliche Importe bleiben möglich. Am Abrechnungstag werden nur noch offene Positionen angeboten.
- Dashboard: vorhandener eBay-Bruttoumsatz aus zugeordneten Payoutpositionen, einschließlich Erstattungen. Evelyn-Provision entspricht dem Netto-Rabatt von 0,5 %. Patrick erhält nur für Gruppe B die Differenz aus 3,5 % Partnerrabatt und 0,5 % Evelyn-Provision, mit der bestehenden Cent-Rundung. Die Kennzahlen sind keine Zahlungsbestätigungen.
- `Einbehalten` bleibt unverändert im Rohdatenarchiv und erscheint separat unter „Einbehalte / Rücksendungen in Klärung“. Der Vorgang ist weder Rechnung noch Gutschrift oder Zuordnungsfehler. Echte spätere Zahlungs-/Erstattungsbewegungen werden weiterhin regulär importiert.
- Pro Partner gibt es eine kumulative offene Sammelabrechnung über alle importierten Payouts. Payoutnummern bleiben Herkunftsnachweis in Export und Historie, bilden aber keine operativen Teilabrechnungen. Prüf- und Zahlungsaktionen gelten für den gesamten noch offenen Partnerbestand; bereits bestätigte Positionen werden dabei nicht doppelt bestätigt. Evelyn-Zahlungen gelten für einen konkreten gespeicherten Entwurf. Ein Bestätigungsdialog zeigt Positionen, Abrechnungsbetrag und das aktuelle Datum. Bei zwischenzeitlich geänderten Quellen wird die Bestätigung abgelehnt.
- Automatischer Belegabgleich folgt später. Das bestehende Positionsmodell hält Prüfdatum, Quelldaten-Snapshot, beide Zahlungswege und Abschluss getrennt; die manuelle Bestätigung ersetzt keinen automatischen Belegvergleich.
- Gruppe A schließt nach Prüfung und Partnerzahlung. Gruppe B schließt erst nach Prüfung, Partnerzahlung und Evelyn-Zahlung. Abgeschlossene Positionen verschwinden aus Arbeitslisten und Downloads; ihre Daten und Zeitpunkte bleiben in der Historie.

## Entwurf kontrolliert verwerfen

Die Aktion führt **keinen** API-Löschaufruf aus. Der Nutzer löscht den Entwurf in Lexware und bestätigt dies ausdrücklich in der App. Die App prüft anschließend lesend den gespeicherten Evelyn-Kontakt (Kundennummer 16335) und erwartet für exakt die gespeicherte Rechnungs-ID HTTP 404. Besteht der Beleg noch, ist der Kontakt falsch oder ist die Antwort unklar, bleibt die Sperre bestehen. Bereits bestätigter Evelyn-Zahlungseingang, abgeschlossene Positionen oder veränderte Quelldaten verhindern diese Korrektur.

Nur die betroffene Transfersperre wird freigegeben. Ursprüngliche Registereinträge und Payload bleiben in `discarded_invoices` erhalten. `Settlement_Corrections.json` spiegelt die Korrektur unabhängig von SQLite und gehört zum vollständigen Backup. Ein alter Sperrenspiegel darf einen nachweislich verworfenen Entwurf nicht wieder aktivieren; eine spätere neue Reservierung bleibt geschützt. Partner- und Zahlungsstatus werden nicht geändert.

## Lokale Validierung

- 79 Tests ausgeführt: 71 bestanden, 8 separate Originaldatei-Tests ohne deren Dateivorlagen übersprungen.
- Ursprüngliche Master-Daten aus dem vorhandenen Backup: BA, NB, MH und Gruppe-B-Gesamtabrechnung inklusive unveränderter 4.160,31 € geprüft.
- Einbehalt neben gültigem Payout, spätere Zahlung/Erstattung, Dashboard-Provisionsabgrenzung, UI-Bestätigungsdialog und Abschluss ohne erneuten Download getestet.
- Verwerfen nur mit simulierten HTTP-Antworten: vorhandener Beleg, falscher Kontakt, Fehler, Timeout und bestätigte Zahlung geben keine Sperre frei. Wiederherstellung aus Sidecars erhält Korrekturhistorie und Partnerstatus.
- Aktuelle Recovery-Daten zusätzlich auf isolierter Kopie geprüft: vier Payouts, ein Einbehalt, eine unabhängige Prüfposition, RE0089 mit 37 Positionen. Die simulierte Korrektur verändert ausschließlich dessen Transfersperren.
- Keine echten Lexware-Aufrufe, kein echtes Verwerfen, kein neuer Entwurf.
