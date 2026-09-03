# Recovery-Version für Patricks Wochenlauf

Branch: `codex/recover-payout-settlement`. `main` und die produktive App bleiben unverändert.

## Öffnen und starten

Die aktualisierte lokale App läuft unter http://127.0.0.1:8511/.
Nach einem Rechnerneustart im Repository PowerShell öffnen und ausführen:

```powershell
.\Start-Recovery.ps1
```

Das Skript prüft Branch und Port und verwendet den bestehenden isolierten Datenbestand:
`%LOCALAPPDATA%\PaymentTool-Test\recover-6e82d0d-20260903\test-data`.
Es startet die App im Hintergrund. Einen bereits belegten Port verändert es nicht.
Die frühere, vollständig für HTTP gesperrte Kopie wird nicht mehr gestartet.
In dieser Version kann ausschließlich der Nutzer über die vorhandenen Bestätigungen
und seine API-Key-Eingabe einen echten Entwurf auslösen. Kein Schlüssel wird gespeichert.

## Manueller Ablauf

1. Datenstand prüfen: aktuell Bestelldaten bis 31.08.2026, letzter bekannter Payout 7712804241 vom 02.09.2026, keine offenen Zuordnungen.
2. Neuen Payout und zusätzlichen Bestellbericht etwa für 01.–03.09.2026 auswählen und „Dateien sicher importieren“ drücken. Bestellberichte werden zuerst verarbeitet, jeder Dateiimport separat protokolliert.
3. Pro Datei erkannte, neue, vorhandene und unvollständige/nicht zuordenbare Positionen sowie Fehler kontrollieren. Erfolgreiche andere Dateien bleiben bei einem Fehler einer Datei übernommen und werden einzeln ausgewiesen.
4. Bekannte Dateien können erneut hochgeladen werden. Bekannte Payoutnummern werden nicht erneut angelegt. Abweichende Kopien werden mit konkretem Fehler abgewiesen. Ein identischer Upload ändert die Fachdaten nicht; der Importversuch erscheint lediglich im Protokoll.
5. In Gruppe B den neuen Payout wählen, konkrete Sperrgründe prüfen, Geldeingang bestätigen und Partner-Excel herunterladen. Negative Positionen bleiben separat.
6. Nach eigener Prüfung des Lexware-Altbestands und ausdrücklicher Bestätigung einen Entwurf erzeugen. Einmalige Reservierung erfolgt dauerhaft vor dem API-Aufruf. Ein unklarer Versuch bleibt gesperrt und darf nicht wiederholt werden.
7. Entwurf selbst in Lexware prüfen. Bestehende manuelle Folgestatus verwenden. Die drei RE0089-Payouts sind weiterhin gesperrt.

## Ergänzt

- Importbelege pro Datei, Dublettenhinweise mit Payoutstatus und bestehender Rechnungssperre.
- Stabilere Bestellpositionsidentität bei ergänzter Artikel-/Transaktionsnummer; widersprüchliche Identitäts-, Titel- oder SKU-Angaben werden nicht still überschrieben.
- Datenstand, kompakte Payout- und Bestellberichtshistorie, beobachtete Berichtszeiträume und vorsichtige Hinweise auf mögliche Lücken zwischen diesen Zeiträumen.
- Verständliche SKU-/Partnersperren: `NB /` ist gültig. Der unveränderte SKU-Text bleibt sichtbar. MH-Varianten werden weiterhin MH zugeordnet. Weitere bekannte Gruppe-B-Präfixe können in `partners.json` eingetragen werden; unbekannte Präfixe werden nicht automatisch abgerechnet.
- Separate dauerhafte `Settlement_Locks.json`. Ein fehlendes oder neu aufgebautes SQLite-Register übernimmt vorhandene Sperren wieder. Fehlen beide Registerdateien bei vorhandenen Payoutdaten, wird die Verarbeitung gesperrt und ein vollständiges Backup verlangt.
- Vollständiges Backup enthält jetzt CSV-Dateien, SQLite-Register und Sperrensicherung. Sicherung vor Bereitstellung: `%LOCALAPPDATA%\PaymentTool-Test\Weekly-Run-Before-20260903.zip`.

## Bereits vorhanden und beibehalten

Payout als Betragsquelle, Bestellbericht als Titel-/SKU-Quelle, Gruppe-A-/Gruppe-B-Sätze,
Menge 1 je Transaktion, Partner-Excellayout, separate Erstattungen und partnerlose Gebühren,
Geldeingangsprüfung, persistente Reservierung vor Entwurfserstellung, kein automatischer
erneuter Erstellungsversuch sowie der allgemeine Payout-Freitext aus dem erfolgreichen RE0089-Test.

## Grenzen

Ein Bestelldatum ist kein Nachweis einer lückenlosen Berichtabdeckung. Verkaufsfreie Tage
können wie Lücken aussehen; Hinweise lösen keine Reparatur aus. Fehlende historische
Importdaten werden als unbekannt ausgewiesen. Widersprüchliche bestehende Stammdaten
benötigen eine fachliche Klärung. Der Verlust des gesamten Datenverzeichnisses erfordert
ein vollständiges Backup; lokale Dateien ersetzen keine externe Sicherung.
Die vorhandene UI erstellt weiterhin einen Gruppe-B-Entwurf je ausgewähltem Payout;
die Gesamtübersicht über mehrere Payouts ist ein Excel-Export.

## Prüfung

39 lokale Tests bestanden, einschließlich der vorhandenen importierten Originaldaten:
50 Payoutpositionen, 180 Bestellpositionen; BA/NB/MH- und Evelyn-Summen unverändert,
Evelyn weiterhin 37 positive Positionen und 4.160,31 € brutto. Neue Tests decken
Registerverlust/-neuaufbau, doppelte Imports, Bestellidentität, `NB /`, unbekannte
Partner und Lückenhinweise ab. API-Workflowtests verwenden ausschließlich Mocks.
Die laufende Oberfläche wurde mit dem bestehenden Recovery-Datenbestand geprüft.
Keine weiteren Lexware-Testentwürfe und keine Live-API-Aufrufe durch Codex in diesem Schritt.
