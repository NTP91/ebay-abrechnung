# Recovery-Version für Patricks Wochenlauf

## Aktuelle Oberfläche

Helle Oberfläche mit Übersicht, Gruppe A, Gruppe B, Offene Positionen und Historie.
Der Import liegt kompakt links; API-Key und Backup sind eingeklappt. Fachliche
Tabellen zeigen nur relevante Spalten. Test-Payloads und Debug-Ausgaben wurden
entfernt; Entwurfs-IDs stehen ausschließlich unter „Technische Details“.

Gruppe A enthält neue positive Positionen vollständig zugeordneter, ungesperrter
Payouts. Gruppe B trennt „Partner → Patrick“ (3,5 %) von „Patrick → Evelyn“ (0,5 %,
19 % Umsatzsteuer). Die Beträge stammen aus dem unveränderten Export-Rechenweg.
Die Gesamtrechnung kann mehrere ausgewählte neue Payouts enthalten. Dabei werden
die bestehenden Einzel-Payloads zusammengeführt und alle Payouts vor einem einzigen
Erstellungsaufruf gemeinsam dauerhaft reserviert. Eine Änderung gegenüber dem
angezeigten Datenstand stoppt die Erstellung. Keine Finalisierung und kein Versand.

Historische Downloads bleiben separat unter Historie zugänglich. Sie fließen nicht
in neue Abrechnungsübersichten ein. Erstattungen/Gutschriften, partnerlose Gebühren
und Transaktionen ohne Payout haben eigene Bereiche. Offene Produkttitel/SKU werden
aus dem zugeordneten Bestellbericht angezeigt, ohne Änderungen am Quelldatenbestand.

Prüfstand beim Umbau: 4 Payouts, 47 offene Transaktionen, 37 bereits übertragene
Lexware-Positionen. Beim Payout 7714928937 fehlt eine eindeutige Bestellzuordnung;
die bestehende Sicherheitssperre bleibt erhalten. 52 lokale Tests bestanden,
einschließlich Originalsummen und ausschließlich simulierten Lexware-Aufrufen.

Überlappende Transaktionsberichte: Jeder Payout wird unabhängig verarbeitet.
Teilmengen bekannter Positionen gelten als bereits vorhanden. Neue Positionen
können zu noch nicht gesperrten Payouts ergänzt werden. Zusätzliche unbekannte
Positionen eines gesperrten/abgerechneten Payouts oder widersprüchliche Beträge
werden für diesen Payout zurückgewiesen und mit Quelldaten dauerhaft zur manuellen
Prüfung protokolliert. Andere Payouts derselben Datei werden weiter importiert.
Der Import verändert keine bestehende Rechnungssperre. 48 lokale Tests bestanden,
einschließlich drei alten plus viertem neuen Payout, Teilberichten, 71 erneut
importierten Bestellpositionen, offenen Transaktionen und Original-Abrechnungssummen.

Nachbesserung offene Transaktionen: Berichte dürfen ausgezahlte und noch offene
Zeilen enthalten. Beide werden atomar im Transaktionsbestand gespeichert. Nur
Zeilen mit Payoutnummer gelangen in die bestehende Abrechnungsverarbeitung.
Eine spätere Payoutnummer ersetzt die passende offene Position anhand der
Transaktionsidentität (ersatzweise Bestell-/Artikelnummer); ältere offene Berichte
setzen ausgezahlte Positionen nicht zurück. Verkäufe und Erstattungen bleiben
getrennt. Importmeldungen unterscheiden neue/bekannte ausgezahlte und neue/weiterhin
offene Positionen. Offene Transaktionen sind kein Fehlerzustand und erscheinen in
einer separaten Übersicht. 44 lokale Tests einschließlich ursprünglicher
Abrechnungssummen bestanden; keine Live-Lexware-Aufrufe.

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
Die Gruppe-B-Gesamtrechnung verwendet nur neue positive Positionen. Die bereits
abgerechneten RE0089-Payouts und alle reservierten/unklaren Versuche bleiben ausgeschlossen.

## Prüfung

39 lokale Tests bestanden, einschließlich der vorhandenen importierten Originaldaten:
50 Payoutpositionen, 180 Bestellpositionen; BA/NB/MH- und Evelyn-Summen unverändert,
Evelyn weiterhin 37 positive Positionen und 4.160,31 € brutto. Neue Tests decken
Registerverlust/-neuaufbau, doppelte Imports, Bestellidentität, `NB /`, unbekannte
Partner und Lückenhinweise ab. API-Workflowtests verwenden ausschließlich Mocks.
Die laufende Oberfläche wurde mit dem bestehenden Recovery-Datenbestand geprüft.
Keine weiteren Lexware-Testentwürfe und keine Live-API-Aufrufe durch Codex in diesem Schritt.
