# Kritischer Zahlungsprozess – 4. September 2026

Geprüft mit isolierten Daten, gesperrtem Live-HTTP, simulierten Lexware-Antworten
und einer Kopie des aktuellen Recovery-Bestands. Bestehende Live-Daten,
RE0089, Zahlungsbestätigungen und Belege wurden nicht verändert.

## Behobene Blocker

- Neue Partnerpositionen nach einer schon freigegebenen, noch unbezahlten
  Rechnung wurden beim nächsten Belegabgleich zusammen mit bereits belegten
  Positionen erwartet. Der nächste Beleg und Download berücksichtigen jetzt nur
  noch nicht freigegebene Positionen. Bestehende Originalbelege bleiben zugänglich.
  Nach Prüfung aller offenen Positionen bleibt der gemeinsame Zahlungsabschluss
  möglich. Dies gilt auch für die getrennte Gutschriftenprüfung.
- Die UI schloss Positionen mit seit einer Bestätigung geänderten Quellen aus,
  der Payload konnte sie dennoch enthalten. Der Payload berücksichtigt jetzt
  dieselben Quellen- und Abschlusssperren, zusätzlich zu den vorhandenen API-Holds.
- Mehrere separat freigegebene Partnerbelege dürfen beim Zahlungsabschluss nicht
  neu zusammen gerundet werden. Anzeige und Zahlungsdialog verwenden deren
  gespeicherte Positionsbeträge. Regression: zwei Belege zu je 118,41 € ergeben
  236,82 €, nicht die neu berechneten 236,81 € einer anderen Sammelabrechnung.

## Nachgewiesener Ablauf

Gruppe A: alle vier Partner bis UI-Freigabe, Zahlung und Abschluss getestet.
Nachträglich hinzugekommene Positionen können separat geprüft werden. Abgeschlossene
Positionen verschwinden aus dem offenen Bestand; Belegzuordnung und Historie bleiben.

Gruppe B: bestehender Entwurf wird ausgeschlossen, neuer Entwurf enthält nur
freigegebene neue Positionen. Genau ein simulierter Erstellungsaufruf, kein
Finalisieren; Wiederholung bleibt gesperrt. Evelyn-Zahlung und Partnerzahlung
werden getrennt gespeichert. Abschluss erst nach den bestehenden Voraussetzungen.

Gutschriften: BA und MH mit Verkauf und Teil-Erstattung getestet. Positive Rechnung
und negative Gutschrift bleiben getrennt; automatischer Belegabgleich und UI-Abschluss
funktionieren. Wiederholter Abschluss wird abgelehnt. Gutschriften müssen im
Bereich „Offene Positionen → Gutschriften & Erstattungen“ separat bearbeitet werden;
sie werden nicht automatisch gegen einen positiven Evelyn-Entwurf verrechnet.

Gesamte Suite: 144 Tests, 136 bestanden, 8 wegen fehlender ursprünglicher
CSV-Testdateien übersprungen. Vorhandene Original-Masterdaten wurden mitgetestet.

## Offene Belegklärung vor dem echten Einsatz

Eine uneingeschränkte Produktivfreigabe ist noch nicht möglich:

- Das Register kennt RE0089 als erstellten Entwurf mit 37 Positionen. Eine externe
  Finalisierung wird nicht automatisch nachgeführt. Patrick muss in Lexware
  den tatsächlichen Zustand und bestehende Belege prüfen; es wurde nicht live gelesen.
- RE0089 enthält den geschützten nachträglichen Hold zu `08-15103-42438` (48,99 €).
  Korrektur/Weiterverwendung muss mit eindeutigem Belegbezug geklärt werden.
  Nicht unverändert finalisieren und keinesfalls die lokale Sperre manuell entfernen.
  Eine Löschung oder Freigabe des Teststands wurde nicht vorgenommen.
- Sieben offene Erstattungen sind separat abzugleichen und mit dem tatsächlichen
  Zahlungs-/Gutschriftenvorgang zu erledigen. Keine automatische Evelyn-Gutschrift.

Aktueller zusätzlicher Evelyn-Entwurfsumfang: 20 Positionen aus Payout `7714928937`,
3.195,22 € brutto. Die 37 bisherigen Positionen und alle 13 API-Hold-Positionen
werden nicht neu übertragen. Dieser Umfang ist kein erstellter Beleg und keine
Bestätigung eines echten Zahlungseingangs.

Vor dem ersten echten Vorgang: RE0089/Belegstatus und Hold-Korrektur klären,
offene Erstattungen getrennt abstimmen, Bankeingang und tatsächlichen neuen
Positionsumfang kontrollieren. Partnerrechnung hochladen und prüfen; Zahlungen
erst nach tatsächlichem Eingang/Ausgang verbindlich bestätigen.
