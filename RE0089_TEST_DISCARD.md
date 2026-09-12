# RE0089 – kontrollierte Testbelegkorrektur, 4. September 2026

Ausdrücklicher Nutzerauftrag: RE0089 war ausschließlich ein Testentwurf und wurde
nie als echte Rechnung verwendet. Keine Remote-Prüfung und keine Aussage, dass
der Entwurf in Lexware gelöscht wurde. Keine Lexware-Aufrufe.

Die lokale Vorprüfung ergab keine hochgeladenen Belege, Rechnungszuordnungen,
erfolgreichen Prüfungen, Zahlungsbestätigungen oder Abschlüsse. Der Originalsnapshot
mit 37 Positionen und die Fingerprints waren eindeutig zugeordnet und unverändert.
Vor der Änderung wurde ein vollständiges Abrechnungsbackup angelegt.

Die Korrektur `discard_re0089_test` gilt ausschließlich für die bekannte RE0089-ID.
Sie archiviert die ursprünglichen Registerzeilen und den vollständigen Snapshot in
der vorhandenen Verwerfungshistorie, mit der Kennzeichnung „verworfener Testbeleg“.
Die vorhandene Wiederherstellungslogik verhindert eine Wiederbelebung des Teststatus
aus einer alten Sperrensicherung. Wiederholtes Zurücksetzen wird abgelehnt.

Nur die RE0089-Transfersperren wurden entfernt. Andere Payoutstatus, Original-CSVs,
Partnerbelege, Workflow-Daten sowie manuelle und API-Holds blieben unverändert.

- Wieder im Evelyn-Abrechnungspool: **36 Positionen**.
- Weiterhin gesperrt aus RE0089: **08-15103-42438, MH, 48,99 €**, aktiver API-Hold.
- Die übrigen bestehenden Holds einschließlich **06-15117-56051, 29,99 €** bleiben.
- Neuer tatsächlich übertragbarer Evelyn-Umfang: **56 Positionen** aus
  7700379513, 7710027297, 7712804241 und 7714928937.
- eBay-Bruttobasis: **7.343,57 €**.
- Rabatt 0,5 % netto: **30,89 €**.
- Rechnungsbetrag brutto: **7.306,79 €**.

Noch kein neuer Entwurf wurde erzeugt. Die übrigen Positionen aus Payout 7718008497
werden durch dessen bestehende Sperren nicht zusätzlich freigegeben. Diese Notiz
aktualisiert die RE0089-Belegklärung im vorherigen Zahlungsprozessbericht.

Regressionen: explizite Freigabe erforderlich; Upload, Prüfung, Partnerzahlung,
Evelyn-Zahlung und Abschluss verhindern den Reset; Historie und Hold bleiben
auch bei Wiederherstellung aus alten Registerkopien erhalten.
