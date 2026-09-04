# API-Einbehalte in der Abrechnungsfreigabe

Stand: 4. September 2026, Recovery-Branch.

## Regeln und Integration

`api_holds.py` speichert die relevanten Finances-Beobachtungen kumulativ in
`Settlement_API_Holds.json` und als SQLite-Spiegel. Wiederholte Beobachtungen
werden dedupliziert. Fehlende Zeilen, ein neuer 90-Tage-Zeitraum und fehlgeschlagene
Abrufe entfernen keine vorhandenen Nachweise. Beide Sicherungen gehören zum
Abrechnungsbackup; widersprüchliche Spiegel stoppen die Verarbeitung.

Belegte `RETRO_HOLD-*`-/`DISPUTE_HOLD-*`-Belastungen und Verkäufe mit
`FUNDS_ON_HOLD` sperren die bestehende positive Bestellposition über `orderId`.
Bei mehreren Positionen derselben Bestellung wird konservativ die gesamte
Bestellung gesperrt: ein Hold darf nicht willkürlich auf einzelne Artikel verteilt
werden. Erstattungen bleiben eigene finanzielle Vorgänge. `PAYOUT` allein ist
keine Freigabe.

Eine automatische Aufhebung des API-Holds benötigt entweder die dokumentierte
spätere PAYOUT/CREDIT-Beobachtung derselben zuvor gehaltenen SALE-Transaktion
mit gleichem Bestellbezug und Betrag sowie Payout-ID, oder eine spätere
DISPUTE/CREDIT-Gegenbewegung mit gleichem Betrag und eindeutig gemeinsamem
Fallbezug. Normale neue Verkäufe, Erstattungen, bloßes Verschwinden und unklare
Gegenbewegungen reichen nicht. Bestehende manuelle Sperren werden niemals dadurch
aufgehoben. Eine spätere Freigabe entfernt nur das abgeleitete API-Hindernis;
bestehende Rechnungs-, Quellen-, Payout- und Abschlusssperren bleiben maßgeblich.

`core.load_master_data` ergänzt abgeleitete API-Felder. Quellbeträge, Positionskeys
und Fingerprints der Originaldaten bleiben unverändert. Partnerauswahl,
Händlerrechnungs-Sollbestand, Bestätigungsaktionen und Evelyn-Payload berücksichtigen
die zusätzliche Sperre. Eine Position mit vorhandener Übertragung/Reservierung,
Prüfung, Zahlung oder Abschluss wird zusätzlich als geschützter Korrekturfall
angezeigt. Kein gespeicherter Status oder Rechnungs-Snapshot wird zurückgesetzt.
Die Übertragungsanzeige berücksichtigt den tatsächlichen vorhandenen Snapshot,
damit ein beim Entwurf ausgeschlossener Hold nicht als übertragen erscheint.

CSV-Zeilen vom Typ `Auszahlung` bleiben als Bank-Kontrollzeilen im Rohbestand und
in einer eigenen UI-Tabelle. Sie erzeugen weder Partnererstattungen noch eine
zweite Finanzbuchung und zählen im manuellen Bewegungsabgleich nicht nochmals.

## Oberfläche

Partnerkarten behalten den primären Rechnungsupload; der Partner steht dort fest.
Der globale Einstieg verwendet dasselbe `invoice_panel` und verlangt zuerst eine
Partnerauswahl. Gruppe A erklärt die Reihenfolge Upload, Abgleich, Freigabe und
„Bezahlt / abgeschlossen“ sowie die Sperre bei neuen ungeprüften Positionen.
API-Holds und geschützte Korrekturfälle stehen unter Offene Positionen → Einbehalte.

„eBay-Daten aktualisieren“ speichert künftig auch die Hold-Nachweise und lädt die
Ansicht neu. Der Abruf enthält Bestellabfragen für alle importierten Payouts;
es gibt keine Hintergrundabfragen oder Lexware-Aufrufe. Die Bewertung ist eine
Sicht auf die zuletzt gespeicherten API-Daten, keine Behauptung über noch nicht
abgerufene neue Vorgänge. Bestehende Daten ohne belegten Hold werden nicht pauschal
neu gesperrt. Payoutweite Transfersperren bleiben unverändert und können auch nach
einer belegten Freigabe eine gesonderte Korrekturentscheidung erforderlich machen.

## Prüfung mit dem bestehenden Recovery-Datenstand

Gespeicherter API-Abruf vom 4. September 2026: 299 Transaktionen, ergänzt durch
Payout-/Bestellabfragen. Keine neuen Live-Aufrufe für diese Umsetzung erforderlich.

Zusätzlich gesperrt:

- MH, `06-15117-56051`, 29,99 €: nicht übertragen, jetzt einbehalten.
- MH, `08-15103-42438`, 48,99 €: weiterhin Bestandteil von RE0089;
  geschützter Korrekturfall, keine Löschung oder erneute Übertragung.

Die elf zuvor manuell gesperrten Retro-Hold-Positionen bleiben gesperrt. Insgesamt
13 vorhandene positive Positionen haben einen aktiven API-Hold. Die Bank-Summenzeile
über −3.722,17 € in Payout `7714928937` entfällt ausschließlich aus der abgeleiteten
Partneransicht. Die unveränderten CSVs behalten sämtliche Originalzeilen.

| Offene abrechnungsfähige positive Positionen | Anzahl | Rechnungsbetrag brutto |
| --- | ---: | ---: |
| Gruppe A, 0,5 % | 13 | 1.756,78 € |
| Gruppe B, Partner 3,5 % | 57 | 7.170,46 € |
| Gruppe B, Evelyn-Gesamtübersicht 0,5 % | 57 | 7.393,34 € |

Gruppe A bleibt unverändert. Gruppe B sinkt gegenüber dem bisherigen Stand um
zwei Positionen, 76,22 € Partnerbrutto bzw. 78,57 € Evelyn-Brutto. Die
Evelyn-Gesamtübersicht enthält auch bereits übertragene Positionen; sie ist nicht
der Betrag eines neuen Entwurfs. RE0089 bleibt bei 37 Positionen und 4.160,31 €.

Tests decken positionsbezogene Sperren, Erstattungsabgrenzung, konservative
Release-Prüfung, persistente Nachweise, unveränderte geschützte Stände, Bankzeilen,
Re-Import und alle vier Gruppe-A-Partner bis zum UI-Abschluss ab.
