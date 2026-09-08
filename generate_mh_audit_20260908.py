# -*- coding: utf-8 -*-
"""Erzeugt das vollstaendige MH-Audit (Kennzahlen, Kommunikationsauswertung,
Falschlieferungen, Detailfaelle, Gesamtfalltabelle) als PDF.

Datengrundlage: Live-Abfrage der Supabase-Tabellen audit_cases und
audit_case_signals fuer Partner MH am 06.09.2026 (120 Bestellpositionen,
139 Nachrichten-Rohsignale, 15 Rueckgaben, 2 Bewertungssignale, 189 Holds).
Alle Aussagen sind an konkrete Bestellnummern gebunden; Systemklassifikations-
Fehler (siehe Abschnitt 6) wurden manuell anhand des Nachrichtenwortlauts
korrigiert, nicht unkritisch uebernommen.
"""
from __future__ import annotations

import io
import shutil

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.graphics.shapes import Drawing
from reportlab.graphics.charts.barcharts import VerticalBarChart
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak,
    KeepTogether,
)

DARK_BLUE = colors.HexColor('#1E3A8A')
SIGNAL_RED = colors.HexColor('#DC2626')
DARK_RED = colors.HexColor('#991B1B')
AMBER = colors.HexColor('#92400E')
LIGHT_AMBER = colors.HexColor('#FEF3C7')
NEUTRAL_GRAY = colors.HexColor('#F3F4F6')
LIGHT_RED = colors.HexColor('#FEE2E2')
ROW_GRAY = colors.HexColor('#F9FAFB')
BORDER_GRAY = colors.HexColor('#D1D5DB')
TEXT_GRAY = colors.HexColor('#1F2937')
WHITE = colors.white

OUTPUT_FILE = 'MH_Audit_vollstaendig_2026-09-08.pdf'
DOWNLOADS_FILE = r'C:\Users\servi\Downloads\MH_Audit_vollstaendig_2026-09-08.pdf'
LEGACY_FILE = 'MH_Audit.pdf'
LEGACY_DOWNLOADS_FILE = r'C:\Users\servi\Downloads\MH_Audit.pdf'

PARTNER_QUOTES = [('MH', 16.13, SIGNAL_RED), ('NB', 9.62, DARK_BLUE), ('PP', 2.86, DARK_BLUE)]
SKU_QUOTES = [('R13B4 (Internetradio)', 42.86, SIGNAL_RED), ('R5B1-B5 (Nespresso)', 28.57, DARK_BLUE),
              ('MH44/PHI (FRITZ!Box)', 22.73, SIGNAL_RED)]

# ---------------------------------------------------------------------------
# Vollstaendige Falltabelle (30 dokumentierte MH-Beschwerde-/Problemfaelle,
# Stand Live-Abfrage 08.09.2026). Jede Zeile = ein Kundenproblem; Mehrfach-
# signale (Rueckgabe + Nachricht + Bewertung + Hold zum selben Vorfall) sind
# in "beleg" zusammengefasst, nicht als separate Faelle gezaehlt.
# ---------------------------------------------------------------------------
ALL_CASES = [
    dict(order='01-15122-89347', sku='R13B4', problem='Defekt (kein Ton)', beleg='Rückgabe, Nachricht, Hold', neu=False),
    dict(order='04-15124-90902', sku='R13B4', problem='Nicht wie beschrieben (Powerbank fehlt)', beleg='Nachricht, Hold', neu=False),
    dict(order='08-15103-42438', sku='R13B4', problem='Nicht wie beschrieben (DAB+/WLAN fehlt, Verpackung offen)', beleg='Rückgabe, Nachricht, Hold', neu=False),
    dict(order='12-15106-02853', sku='R24B5', problem='Sonstige Beschwerde + Negative Bewertung (1)', beleg='Rückgabe, Bewertung', neu=False),
    dict(order='19-15087-98239', sku='R5B1-B5', problem='Sendungsverzug/Kommunikation + Negative Bewertung (2)', beleg='Nachricht, Bewertung, Hold', neu=False),
    dict(order='09-15101-53665', sku='R5B1-B5', problem='Falsche Variante', beleg='Rückgabe, Hold', neu=False),
    dict(order='08-15091-01708', sku='R12B3', problem='Falschlieferung: HP 302 bestellt, HP 303 erhalten', beleg='Nachricht, Hold', neu=True),
    dict(order='02-15097-54828', sku='R12B3', problem='Falschlieferung: HP 303 bestellt, HP 302 erhalten', beleg='Nachricht', neu=False),
    dict(order='14-15110-32481', sku='R12B3', problem='Patrone leer, Verpackung beschädigt + Negative Bewertung (3)', beleg='Rückgabe, Nachricht, Bewertung, Hold', neu=False),
    dict(order='15-15105-94821', sku='R12B3', problem='Verpackung eingerissen, Patronen unvollständig', beleg='Nachricht, Hold', neu=False),
    dict(order='08-15096-66298', sku='R16B3', problem='Verpackung bereits geöffnet, Siegelstreifen fehlen', beleg='Nachricht, Hold', neu=False),
    dict(order='10-15077-72969', sku='MH44/PHI', problem='Falsche Variante', beleg='Rückgabe, Hold', neu=False),
    dict(order='02-15130-45240', sku='MH44/PHI', problem='Nicht wie beschrieben', beleg='Rückgabe', neu=False),
    dict(order='15-15088-16137', sku='MH44/PHI', problem='Nicht wie beschrieben (Vodafone-Inkompatibilität)', beleg='Nachricht, Hold', neu=True),
    dict(order='09-15092-86078', sku='6941565991454', problem='Versandverzug — positiv gelöst, Käufer bestätigt gute Kommunikation', beleg='Nachricht, Hold', neu=False, positiv=True),
    dict(order='21-15067-10713', sku='6941565991454', problem='Versandverzug, Käufer storniert Kauf', beleg='Nachricht', neu=False),
    dict(order='24-15066-72008', sku='R9B4', problem='Paket laut Käufer nicht auffindbar/verloren', beleg='Nachricht', neu=False),
    dict(order='17-15075-26729', sku='R6B2', problem='Gebraucht statt neu (Gebrauchsspuren)', beleg='Nachricht, Hold', neu=False),
    dict(order='06-15117-56051', sku='R6B3', problem='Nicht wie beschrieben (nur Systemgrund, kein Nachrichtentext)', beleg='Rückgabe', neu=False),
    dict(order='22-15092-17559', sku='R20B3', problem='Nicht wie beschrieben (Honor 200 statt 200 Lite)', beleg='Rückgabe, Nachricht, Hold', neu=False),
    dict(order='17-15065-38276', sku='R7B2', problem='Widerruf ("nicht mehr benötigt") — kein Qualitätsmangel', beleg='Rückgabe', neu=False),
    dict(order='12-15088-24787', sku='MH40', problem='Defekt/beschädigt (nur Systemgrund, kein Nachrichtentext)', beleg='Rückgabe', neu=False),
    dict(order='09-15098-82046', sku='MH43/PHI', problem='Sonstige Beschwerde (nur Systemgrund, kein Nachrichtentext)', beleg='Rückgabe', neu=False),
    dict(order='04-15090-66849', sku='R7B1', problem='Gebraucht statt neu (OVP-Siegel offen)', beleg='Rückgabe, Nachricht', neu=False),
    dict(order='14-15091-70890', sku='MH76/BUR', problem='Rückgabe-/Storno-Logistik (keine Qualitätsaussage im Text)', beleg='Nachricht', neu=False),
    dict(order='14-15077-39967', sku='R19B1', problem='Rückgabe abgeschlossen, Grund im Datensatz nicht vermerkt', beleg='Rückgabe (CLOSED)', neu=False),
    dict(order='17-15091-11030', sku='R20B4', problem='Rückgabe abgeschlossen, Grund im Datensatz nicht vermerkt', beleg='Rückgabe (CLOSED)', neu=False),
    dict(order='03-15116-91038', sku='MH44/PHI', problem='INAD: Artikel nicht wie beschrieben (kein Käuferkommentar verfügbar)', beleg='Rückgabe', neu=True),
    dict(order='14-15092-67058', sku='R26B1', problem='Defekt / Funktionsausfall; Return formal als Widerruf', beleg='Nachricht, Rückgabe', neu=True),
    dict(order='18-15105-36957', sku='MH44/PHI', problem='Falsche Variante / Größe (kein Käuferkommentar verfügbar)', beleg='Rückgabe, Hold', neu=True),
]

REPEAT_FOLLOWUP = [
    ('02-15097-54828', 'HP-Patronen-Verwechslung', '3 Nachrichten über 2 Tage, letzte Antwort im Datensatz nur automatisiert, Käufer wartet weiter auf Klärung.'),
    ('19-15087-98239', 'Nespresso-Sendungsverzug', '4 Nachrichten über 4 Tage, endet mit negativer Bewertung.'),
    ('21-15067-10713', 'DJI Mic Mini-Sendungsverzug', '2 Nachrichten, danach Stornierung durch Käufer wegen Fristdrucks.'),
    ('22-15092-17559', 'Honor 200 vs. 200 Lite', '4 Nachrichten von Käufer und einer weiteren eBay-Nutzerin zur Modellklärung, keine dokumentierte Antwort.'),
    ('15-15088-16137', 'Vodafone-Inkompatibilität Fritzbox', '2 Nachrichten, Käufer widerspricht der Kostenübernahme für Rückversand.'),
    ('01-15122-89347', 'Internetradio-Defekt', '2 Nachrichten (Rückgabe-Anleitung erfragt, dann Defektmeldung).'),
]

SKU_PATTERNS = [
    ('R13B4', 'AudioAffairs IR 010 Internetradio', 3, '01-15122-89347, 04-15124-90902, 08-15103-42438',
     'Defekt, fehlendes Zubehör, fehlende Kernfunktion — 3 unterschiedliche Mängel, gleiches Produkt.'),
    ('R12B3', 'Druckerpatronen-Listinggruppe (HP/Canon/Brother)', 4, '08-15091-01708, 02-15097-54828, 14-15110-32481, 15-15105-94821',
     'Zwei spiegelbildliche Falschlieferungen (HP 302↔303) plus zwei Fälle mit geöffneter/unvollständiger Verpackung.'),
    ('R16B3 (verwandt zu R12B3)', 'Epson 18XL Multipack', 1, '08-15096-66298',
     'Fünfter Patronen-Fall außerhalb der R12B3-Gruppe, gleiches Fehlerbild (Verpackung geöffnet).'),
    ('R5B1-B5', 'DeLonghi Nespresso Vertuo Pop', 2, '09-15101-53665, 19-15087-98239',
     'Falsche Variante sowie Sendungsverzug mit negativer Bewertung.'),
    ('MH44/PHI', 'AVM FRITZ!Box 6660 Cable (Refurbished)', 5, '02-15130-45240, 03-15116-91038, 10-15077-72969, 15-15088-16137, 18-15105-36957',
     'Zwei neue Returns verschärfen das Muster: nochmals INAD sowie falsche Größe/Variante; insgesamt fünf Fälle derselben Refurbished-Serie.'),
    ('6941565991454', 'DJI Mic Mini ("im Zulauf" gelistet)', 2, '09-15092-86078, 21-15067-10713',
     'Beide Fälle durch Versandverzug, da Artikel laut SKU-Zusatz "im Zulauf" bei Bestellannahme nicht vorrätig war.'),
]

WRONG_DELIVERY = [
    dict(order='08-15091-01708', sku='R12B3', item='HP 302 Schwarz Tintenpatrone F6U66AE', bestellt='HP 302',
         geliefert='HP 303 (laut Käufer)', beleg='„HALLO. Ich habe bei Ihnen eine Druckerpatrone HP 302 bestellt, geschickt haben Sie mir HP 303. Möchte gerne die Patrone umtauschen, kostenlos."'),
    dict(order='02-15097-54828', sku='R12B3', item='HP 303 Tintenpatrone Schwarz T6N02AE', bestellt='HP 303',
         geliefert='HP 302 (laut Käufer)', beleg='„Hallo. HP 303 bei Ihnen erworben und heute 302 erhalten. Was machen wir jetzt?"'),
]

WRONG_VARIANT = [
    dict(order='09-15101-53665', sku='R5B1-B5', item='DeLonghi Nespresso Vertuo Pop ENV90.B', beleg='Rückgabegrund im System: „Falsche Variante / Größe / Ausführung" (kein Nachrichtentext verfügbar).'),
    dict(order='10-15077-72969', sku='MH44/PHI', item='AVM FRITZ!Box 6660 Cable (Refurbished)', beleg='Rückgabegrund im System: „Falsche Variante / Größe / Ausführung" (kein Nachrichtentext verfügbar).'),
    dict(order='18-15105-36957', sku='MH44/PHI', item='AVM FRITZ!Box 6660 Cable (Refurbished)', beleg='Neuer Return vom 08.09.2026: eBay-Code WRONG_SIZE; kein Käuferkommentar verfügbar.'),
]

NEGATIVE_FEEDBACK = [
    dict(order='12-15106-02853', date='05.09.2026', sku='R24B5 (Razer Thresher 7.1 Headset)',
         text='„Der Artikel wurde als neu angeboten, die Verpackung war bereits geöffnet, der Artikel im Inneren war '
              'schrottreif (beschädigt, zerkratzt, zerbrochen). Sowas als neu zu inserieren und mich 8 Euro für den '
              'Rückversand bezahlen zu lassen ist eine Frechheit."',
         quelle='In Supabase (audit_case_signals) dokumentiert.'),
    dict(order='19-15087-98239', date='06.09.2026', sku='R5B1-B5 (DeLonghi Nespresso Vertuo Pop)',
         text='„Kommunikation mangelhaft. Seit Tagen steht das Paket auf \u201edie Sendung wurde elektronisch '
              'angekündigt\u201c. Verkäufer antwortet zögerlich und mit dem Hinweis, dass er sich kümmern würde. '
              'Seitdem keine weitere Information. eBay verständigt."',
         quelle='In Supabase (audit_case_signals) dokumentiert.'),
    dict(order='14-15110-32481', date='06.09.2026', sku='R12B3 (Canon CLI-521 C Druckerpatrone Cyan, Art.-Nr. 820045600774)',
         text='„Kam leer und zerrissen an. Paket war äußerlich in Ordnung." (Käufer: mimilotta93)',
         quelle='Jetzt über die eBay Trading API in Supabase synchronisiert und über Artikelnummer, Transaktionsnummer '
                'und Bestellnummer eindeutig demselben bereits dokumentierten Rückgabe-/Nachrichtenfall zugeordnet.'),
]

CASE_CARDS = [
    {
        'title': 'Fall A — Order 08-15091-01708 — Falschlieferung Druckerpatrone (bisher nicht erfasst)',
        'order_id': '08-15091-01708', 'line_item': '10083494340608', 'sku': 'MH / BÜR / R12B3 /',
        'datum': '27.08.–05.09.2026', 'produkt': 'HP 302 Schwarz Tintenpatrone F6U66AE – DeskJet 2130 ENVY 4520',
        'kaeufer': 'hebils-0',
        'vorgang': ('Kein Rückgabeantrag im System, kein is_problem-Flag gesetzt — Fall wurde ausschließlich über '
                    'den Nachrichtentext identifiziert. Kundennachricht: „HALLO. Ich habe bei Ihnen eine '
                    'Druckerpatrone HP 302 bestellt, geschickt haben Sie mir HP 303. Möchte gerne die Patrone '
                    'umtauschen, kostenlos."'),
        'bewertung': None,
        'hold': 'Auszahlungs-Hold vorhanden (Status laut Datensatz: aktiv).',
        'hinweis': ('Spiegelbildlich zu Fall B (Order 02-15097-54828): dort wurde HP 303 bestellt und HP 302 '
                    'geliefert. Beide Fälle stammen aus derselben Listing-Gruppe R12B3 — deutliches Indiz für eine '
                    'Verwechslung der beiden Patronenvarianten im Lager/Versandprozess.'),
    },
    {
        'title': 'Fall B — Order 02-15097-54828 — Falschlieferung Druckerpatrone, Gegenstück zu Fall A',
        'order_id': '02-15097-54828', 'line_item': 'n/a', 'sku': 'MH / BÜR / R12B3 / 190780571026',
        'datum': '31.08.–01.09.2026', 'produkt': 'HP 303 Tintenpatrone Schwarz T6N02AE',
        'kaeufer': 'gino17122008 (Thomas)',
        'vorgang': ('Käufer bittet zunächst um Rücksendeetikett und Rückerstattung, meldet sich am Folgetag erneut, '
                    'da kein Etikett angekommen sei. Erst danach folgt die eigentliche Ursache: „Hallo. HP 303 bei '
                    'Ihnen erworben und heute 302 erhalten. Was machen wir jetzt?" Weitere Nachrichten: „Guten '
                    'Morgen. Ich hoffe, dass ich heute eine Antwort auf mein Anliegen bekomme" / „Ich warte dann '
                    'sehnsüchtig auf baldige Antwort."'),
        'bewertung': None,
        'hold': 'Kein Hold-Signal im Datensatz erfasst.',
        'hinweis': ('Wiederholtes Nachfassen (3 Nachrichten) ohne im Datensatz erkennbare inhaltliche Lösung. Zwei '
                    'automatisierte Systemnachrichten sind zwar unter "seller" verzeichnet, enthalten aber keinen '
                    'auswertbaren Klartext, sodass eine tatsächliche Problemlösung nicht belegt ist.'),
    },
    {
        'title': 'Fall C — Order 14-15110-32481 — Patrone leer + 3. negative Bewertung',
        'order_id': '14-15110-32481', 'line_item': '10084596405714', 'sku': 'MH / BÜR / R12B3 / 4960999577494',
        'datum': '05.–06.09.2026', 'produkt': 'Canon CLI-521 C Druckerpatrone Cyan Original Tinte Einzelpack',
        'kaeufer': 'mimilotta93',
        'vorgang': ('DEFECTIVE_ITEM, Rückerstattung 11,98 EUR. Käuferkommentar (Rückgabe): „verpackung defekt und '
                    'Patrone leer." Kundennachricht: „Patrone leer und Verpackung nicht neu!!"'),
        'bewertung': ('„Kam leer und zerrissen an. Paket war äußerlich in Ordnung." (dritte negative Bewertung, '
                      '06.09.2026, inzwischen über die eBay Trading API in Supabase synchronisiert.)'),
        'hold': 'Auszahlungs-Hold 11,98 EUR (DISPUTE, 05.09.2026).',
        'hinweis': ('Ein Kundenproblem, drei Belegquellen (Rückgabe, Nachricht, Bewertung) — bewusst als EIN Fall '
                    'gezählt, nicht dreifach.'),
    },
    {
        'title': 'Fall D — Order 15-15105-94821 — Verpackung eingerissen, Patronen unvollständig',
        'order_id': '15-15105-94821', 'line_item': '10084585340615', 'sku': 'MH / BÜR / R12B3 / 50140475623965014044156239',
        'datum': '05.09.2026', 'produkt': 'Brother LC-1240 VALBPDR Tintenpatronen Value Pack (4 Farben)',
        'kaeufer': 'mysliman',
        'vorgang': ('Kein Rückgabeantrag im System erfasst. Kundennachricht: „Hallo ich hab heute meine Sendung '
                    'erhalten. Die Verpackung der Druckerpatronen war komplett eingerissen und es sind auch nur '
                    'zwei Patronen im Päckchen."'),
        'bewertung': None,
        'hold': 'Auszahlungs-Hold vorhanden (Status laut Datensatz: aktiv).',
        'hinweis': 'Vierter Fall in der R12B3-Listinggruppe; Value Pack (4 Farben angeboten) kam laut Käufer mit nur 2 Patronen an.',
    },
    {
        'title': 'Fall E — Order 08-15096-66298 — Verpackung bereits geöffnet (5. Patronen-Fall, SKU R16B3)',
        'order_id': '08-15096-66298', 'line_item': 'n/a', 'sku': 'MH / BÜR / R16B3 / 8715946625287',
        'datum': '28.08.2026', 'produkt': 'Epson 18XL Multipack Tintenpatronen (4 Farben)',
        'kaeufer': 'grauerire',
        'vorgang': ('Kundennachricht: „Guten Morgen. Leider musste meine Mutter, nachdem sie das unbeschädigte '
                    'Paket geöffnet hatte, feststellen, dass die Patronen nicht original versendet wurden. Die '
                    'Originalverpackung war geöffnet worden. Teilweise fehlten bei den Patronen die gelben '
                    'Verschlussstreifen [...]. Zum Glück waren die Patronen bis auf die gelbe voll mit Tinte."'),
        'bewertung': None,
        'hold': 'Auszahlungs-Hold vorhanden (Status laut Datensatz: aktiv).',
        'hinweis': ('Eigene SKU außerhalb der R12B3-Gruppe, aber identisches Fehlerbild (bereits geöffnete '
                    'Originalverpackung bei Druckerpatronen) — insgesamt 5. dokumentierter Patronen-Fall.'),
    },
    {
        'title': 'Fall F — Order 15-15088-16137 — Vodafone-Inkompatibilität (bisher nicht erfasst)',
        'order_id': '15-15088-16137', 'line_item': '10084482900015', 'sku': 'MH44 / PHI',
        'datum': '02.–03.09.2026', 'produkt': 'AVM FRITZ!Box 6660 Cable (Refurbished) — Wi-Fi 6',
        'kaeufer': 'thjan_72',
        'vorgang': ('Kein is_problem-Flag im System gesetzt. Kundennachricht: „dieser wird nicht von Vodafone '
                    'unterstützt. ich gebe ihn zurück da die Beschreibung falsch ist. ich werde nicht für den '
                    'Rückversand aufkommen, da dies nicht auf Grund meines Fehlers aufgetreten ist." Folgenachricht: '
                    '„nein ist sie nicht. Vodafone nimmt diese Artikelnummer nicht, mir wurde dies telefonisch von '
                    'Vodafone bestätigt, nachdem das Gerät sich nicht freischalten ließ."'),
        'bewertung': None,
        'hold': 'Auszahlungs-Hold vorhanden (Status laut Datensatz: aktiv).',
        'hinweis': ('Dritter Fall zur MH44/PHI-Listinggruppe (AVM FRITZ!Box 6660 Cable Refurbished), zusätzlich zu '
                    'den bereits über Rückgabegrund dokumentierten Fällen 02-15130-45240 und 10-15077-72969.'),
    },
    {
        'title': 'Fall 1 — Order 12-15106-02853 — Negative Bewertung',
        'order_id': '12-15106-02853', 'line_item': '10084610530812', 'sku': 'MH / BÜR / R24B5 / 8886419371298',
        'datum': '04.09.2026 (Rückgabe/Widerruf), 05.09.2026 (Bewertung)',
        'produkt': 'Razer Thresher 7.1 Gaming Headset PS4 PS5 PC Kabellos Schwarz', 'kaeufer': 'alphahans69',
        'vorgang': 'Widerruf, intern klassifiziert als "Sonstige Beschwerde"; Rückerstattung 119,99 EUR.',
        'bewertung': ('„Der Artikel wurde als neu angeboten, die Verpackung war bereits geöffnet, der Artikel im '
                      'Inneren war schrottreif (beschädigt, zerkratzt, zerbrochen). Sowas als neu zu inserieren '
                      'und mich 8 Euro für den Rückversand bezahlen zu lassen ist eine Frechheit."'),
        'hold': 'Kein separater Auszahlungs-Hold in den Signaldaten erfasst; Rückerstattung über Retourenprozess.',
        'hinweis': ('Materiell handelt es sich trotz formaler Erfassung als Widerruf um einen Sachmangel: bereits '
                    'geöffnete Verpackung, beschädigte Ware bei angeblichem Neuzustand.'),
    },
    {
        'title': 'Fall 2 — Order 19-15087-98239 — Negative Bewertung (Lieferverzug/Kommunikation)',
        'order_id': '19-15087-98239', 'line_item': '10084469924919', 'sku': 'MH / BÜR / R5B1-B5 / 8004399024663',
        'datum': '03.–06.09.2026', 'produkt': 'DeLonghi Nespresso Vertuo Pop ENV90.B 19bar Pad-/Kapselmaschine Black',
        'kaeufer': 'hoermalsmw',
        'vorgang': ('Kein formaler Rückgabeantrag; eBay-INR-Fall Nr. 5386259818 (Artikel nicht erhalten), geöffnet '
                    '05.09., geschlossen 06.09.2026. Kundennachrichten: „Guten Morgen, könnten Sie mal schauen, wo '
                    'das Paket hängt?" (03.09.) · „Gibt es Neuigkeiten?" (04.09.) · „Vielen Dank für die '
                    'Erstattung, vielmehr hätte ich aber gerne die Maschine gehabt." (06.09.) · „Danke, dann warte '
                    'ich mal ab." (06.09.)'),
        'bewertung': ('„Kommunikation mangelhaft. Seit Tagen steht das Paket auf \u201edie Sendung wurde '
                      'elektronisch angekündigt\u201c. Verkäufer antwortet zögerlich [...]. Seitdem keine weitere '
                      'Information. eBay verständigt."'),
        'hold': 'Auszahlungs-Hold 40,99 EUR (DISPUTE, gebucht 05.09.2026).',
        'hinweis': ('Korrektur zur internen Automatik-Klassifikation "geöffnet/benutzt": Der Stichwort-Treffer '
                    'stammt aus "Anfrage geöffnet" (Fallbearbeitung), nicht aus einer geöffneten Verpackung. '
                    'Belegter Sachverhalt: Sendungsverzug in Kombination mit verzögerter Kommunikation.'),
    },
    {
        'title': 'Fall 3 — Order 01-15122-89347 — Wiederholter Defekt (SKU R13B4)',
        'order_id': '01-15122-89347', 'line_item': '10083615494001', 'sku': 'MH / BÜR / R13B4 / 4250772303304',
        'datum': '04.09.2026', 'produkt': 'AudioAffairs IR 010 Internetradio DAB+ Bluetooth WLAN Akku Powerbank weiß',
        'kaeufer': 'plueschrolf',
        'vorgang': ('DEFECTIVE_ITEM (SNAD), Rückerstattung 48,99 EUR. Käuferkommentar: „Radio defekt. Es kommt '
                    'kein Ton." Kundennachrichten: „Wie eröffne ich eine Rückgabe? Ich hab es noch nie gemacht!" / '
                    '„Möchte Radio zurück schicken, da kein Ton."'),
        'bewertung': None,
        'hold': 'Auszahlungs-Hold 48,99 EUR (DISPUTE, 04.09.2026).',
        'hinweis': '1. von 3 dokumentierten Fällen zur SKU R13B4. Fehlerbild: totaler Funktionsausfall (kein Ton).',
    },
    {
        'title': 'Fall 4 — Order 04-15124-90902 — Wiederholt "Artikel entspricht nicht der Beschreibung" (SKU R13B4)',
        'order_id': '04-15124-90902', 'line_item': '10084173877504', 'sku': 'MH / BÜR / R13B4 / 4250772303304',
        'datum': '03.–04.09.2026', 'produkt': 'AudioAffairs IR 010 Internetradio DAB+ Bluetooth WLAN Akku Powerbank weiß',
        'kaeufer': 'hanskel-21',
        'vorgang': ('Kein formaler Rückgabeantrag; über Kundennachricht und Auszahlungs-Hold eskaliert. '
                    'Kundennachricht: „warum wurden die internetradios nicht wie beschrieben mit der powerbank '
                    'geliefert. ich bitte um nachlieferung der beiden powerbanks."'),
        'bewertung': None,
        'hold': 'Auszahlungs-Hold 97,98 EUR (DISPUTE, 03.09.2026 — Wert von zwei Bestellpositionen).',
        'hinweis': '2. von 3 Fällen zur SKU R13B4. Fehlerbild: im Titel beworbenes Zubehör ("Akku Powerbank") fehlt bei Lieferung.',
    },
    {
        'title': 'Fall 5 — Order 08-15103-42438 — Wiederholt "Artikel entspricht nicht der Beschreibung" (SKU R13B4)',
        'order_id': '08-15103-42438', 'line_item': '10083565575708', 'sku': 'MH / BÜR / R13B4 / 4250772303304',
        'datum': '02.09.2026', 'produkt': 'AudioAffairs IR 010 Internetradio DAB+ Bluetooth WLAN Akku Powerbank weiß',
        'kaeufer': 'diethaase',
        'vorgang': ('NOT_AS_DESCRIBED (SNAD), Rückerstattung 48,99 EUR. Käuferkommentar: „Das Gerät ist nicht wie '
                    'angegeben DAB+ fähig und verbindet sich nicht mit keinem Funknetz. Die Verpackung war schon '
                    'einmal geöffnet." Kundennachricht: „das Gerät verbindet sich leider nicht mit dem Internet."'),
        'bewertung': None,
        'hold': 'Auszahlungs-Hold 48,99 EUR (DISPUTE, 02.09.2026).',
        'hinweis': ('3. von 3 Fällen zur SKU R13B4. Fehlerbild: zentrale beworbene Funktionen (DAB+, WLAN) fehlen, '
                    'zusätzlich Hinweis auf bereits geöffnete Verpackung.'),
    },
    {
        'title': 'Neuer Fall G — Order 03-15116-91038 — INAD bei FRITZ!Box 6660 Cable',
        'order_id': '03-15116-91038', 'line_item': '10084432047403', 'sku': 'MH44 / PHI',
        'datum': '07.09.2026', 'produkt': 'AVM FRITZ!Box 6660 Cable (Refurbished) — Wi-Fi 6 — Mesh — frei für alle Anbieter',
        'kaeufer': 'dun.dnbi.u23qldv8cd',
        'vorgang': ('Return 5328622758, eBay-Grund NOT_AS_DESCRIBED (INAD), Status ITEM_READY_TO_SHIP. '
                    'Erstattungsbetrag laut Return-Datensatz: 86,99 EUR. Käuferkommentar und zugehöriger '
                    'Nachrichtentext sind über die API nicht verfügbar.'),
        'bewertung': None,
        'hold': 'Kein Hold- oder Dispute-Signal zu diesem Fall im aktuellen Datenstand.',
        'hinweis': ('Neuer eindeutiger INAD-Fall. Er erhöht das Wiederholungsmuster der SKU MH44/PHI; '
                    'die konkrete Abweichung bleibt mangels Käuferkommentar offen. Belegquelle: eBay Post-Order Return.'),
    },
    {
        'title': 'Neuer Fall H — Order 14-15092-67058 — NERF-Blaster ohne Funktion',
        'order_id': '14-15092-67058', 'line_item': '10084495114814', 'sku': 'MH45 / BÜR / R26B1 / 195166219127',
        'datum': '06.–07.09.2026', 'produkt': 'NERF Fortnite Blaster Blue Shock mit 10 Pfeilen Neu und OVP',
        'kaeufer': 'jolinar0151',
        'vorgang': ('Kundennachricht: „Hallo, der Artikel funktioniert leider nicht. Ich möchte diesen reklamieren. '
                    'Können sie mir eine Ersatzlieferung zuschicken.“ Anschließend Return 5328626474, formal mit '
                    'WITHDRAW_FROM_PURCHASE_CONTRACT, Status ITEM_READY_TO_SHIP. Erstattungsbetrag: 36,99 EUR.'),
        'bewertung': None,
        'hold': 'Kein Hold- oder Dispute-Signal zu diesem Fall im aktuellen Datenstand.',
        'hinweis': ('Nachricht und Return gehören aufgrund identischer Order- und Line-Item-ID zu einem Fall. '
                    'Die Nachricht belegt einen Funktionsdefekt; der formale Return-Code allein wäre unspezifisch.'),
    },
    {
        'title': 'Neuer Fall I — Order 18-15105-36957 — falsche Größe/Variante bei FRITZ!Box 6660',
        'order_id': '18-15105-36957', 'line_item': '10087607518918', 'sku': 'MH44 / PHI',
        'datum': '08.09.2026', 'produkt': 'AVM FRITZ!Box 6660 Cable (Refurbished) — Wi-Fi 6 — Mesh — frei für alle Anbieter',
        'kaeufer': 'marc66621',
        'vorgang': ('Return 5328652711, eBay-Grund WRONG_SIZE, Status ITEM_READY_TO_SHIP. '
                    'Erstattungsbetrag laut Return-Datensatz: 93,99 EUR. Käuferkommentar und zugehöriger '
                    'Nachrichtentext sind über die API nicht verfügbar.'),
        'bewertung': None,
        'hold': 'FUNDS_ON_HOLD über 93,99 EUR (SALE); kein Payment Dispute im aktuellen Datenstand.',
        'hinweis': ('Der API-Code wird als falsche Größe/Variante dokumentiert. Ohne Käuferkommentar ist nicht '
                    'belegt, ob die Ursache in der Beschreibung, Auswahl oder Kompatibilität liegt. '
                    'Return und Hold bleiben ein einziger Fall.'),
    },
]


def _cols(fractions, total):
    widths = [round(total * f, 2) for f in fractions[:-1]]
    widths.append(round(total - sum(widths), 2))
    return widths


def _styles():
    base = getSampleStyleSheet()
    return {
        'title': ParagraphStyle('TitleStyle', parent=base['Heading1'], fontName='Helvetica-Bold', fontSize=18,
                                 leading=22, textColor=DARK_BLUE),
        'subtitle': ParagraphStyle('SubTitleStyle', parent=base['Normal'], fontName='Helvetica', fontSize=10,
                                    leading=14, textColor=colors.HexColor('#4B5563')),
        'h2': ParagraphStyle('H2Style', parent=base['Heading2'], fontName='Helvetica-Bold', fontSize=13, leading=17,
                              textColor=DARK_BLUE, spaceBefore=12, spaceAfter=6),
        'h3': ParagraphStyle('H3Style', parent=base['Heading3'], fontName='Helvetica-Bold', fontSize=11, leading=14,
                              textColor=DARK_BLUE, spaceBefore=8, spaceAfter=4),
        'bold_body': ParagraphStyle('BoldBody', parent=base['Normal'], fontName='Helvetica-Bold', fontSize=9,
                                     leading=13, textColor=TEXT_GRAY),
        'body': ParagraphStyle('BodyStyle', parent=base['Normal'], fontName='Helvetica', fontSize=9, leading=13,
                                textColor=TEXT_GRAY),
        'small': ParagraphStyle('SmallStyle', parent=base['Normal'], fontName='Helvetica', fontSize=7.8, leading=10.5,
                                 textColor=TEXT_GRAY),
        'small_bold': ParagraphStyle('SmallBold', parent=base['Normal'], fontName='Helvetica-Bold', fontSize=7.8,
                                      leading=10.5, textColor=TEXT_GRAY),
        'red_box': ParagraphStyle('RedBox', parent=base['Normal'], fontName='Helvetica-Bold', fontSize=9, leading=13,
                                   textColor=DARK_RED),
        'amber_box': ParagraphStyle('AmberBox', parent=base['Normal'], fontName='Helvetica-Bold', fontSize=9,
                                     leading=13, textColor=AMBER),
        'header_white': ParagraphStyle('HeaderWhite', parent=base['Normal'], fontName='Helvetica-Bold', fontSize=9,
                                        leading=13, textColor=WHITE),
        'header_white_small': ParagraphStyle('HeaderWhiteSmall', parent=base['Normal'], fontName='Helvetica-Bold',
                                              fontSize=7.8, leading=10.5, textColor=WHITE),
        'case_title_white': ParagraphStyle('CaseTitleWhite', parent=base['Normal'], fontName='Helvetica-Bold',
                                            fontSize=10, leading=13, textColor=WHITE),
        'caption': ParagraphStyle('Caption', parent=base['Normal'], fontName='Helvetica-Oblique', fontSize=8,
                                   leading=11, textColor=colors.HexColor('#6B7280')),
    }


def _bar_chart(pairs, width, max_value=50, height=140):
    drawing = Drawing(width, height)
    chart = VerticalBarChart()
    chart.x = 40
    chart.y = 30
    chart.width = width - 70
    chart.height = height - 50
    chart.data = [[value for _, value, _ in pairs]]
    chart.categoryAxis.categoryNames = [label for label, _, _ in pairs]
    chart.categoryAxis.labels.fontSize = 8.5
    chart.valueAxis.valueMin = 0
    chart.valueAxis.valueMax = max_value
    chart.valueAxis.labelTextFormat = '%d%%'
    chart.valueAxis.labels.fontSize = 8
    chart.barLabels.fontSize = 8.5
    chart.barLabelFormat = '%.2f%%'
    chart.barLabels.dy = 6
    chart.groupSpacing = 14
    chart.bars.strokeColor = None
    for index, (_, _, color) in enumerate(pairs):
        chart.bars[(0, index)].fillColor = color
    drawing.add(chart)
    return drawing


def _header_row_style(bg, extra=None):
    style = [
        ('BACKGROUND', (0, 0), (-1, 0), bg),
        ('GRID', (0, 0), (-1, -1), 0.5, BORDER_GRAY),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    ]
    if extra:
        style.extend(extra)
    return TableStyle(style)


def _case_card(case, styles, content_width):
    info_widths = _cols([0.14, 0.36, 0.12, 0.38], content_width - 16)
    info_rows = [
        [Paragraph('<b>Order ID</b>', styles['bold_body']), Paragraph(case['order_id'], styles['body']),
         Paragraph('<b>Line Item</b>', styles['bold_body']), Paragraph(case.get('line_item') or 'n/a', styles['body'])],
        [Paragraph('<b>SKU</b>', styles['bold_body']), Paragraph(case['sku'], styles['body']),
         Paragraph('<b>Datum</b>', styles['bold_body']), Paragraph(case['datum'], styles['body'])],
        [Paragraph('<b>Produkt</b>', styles['bold_body']), Paragraph(case['produkt'], styles['body']),
         Paragraph('<b>Käufer</b>', styles['bold_body']), Paragraph(case['kaeufer'], styles['body'])],
    ]
    t_info = Table(info_rows, colWidths=info_widths)
    t_info.setStyle(TableStyle([
        ('GRID', (0, 0), (-1, -1), 0.5, BORDER_GRAY), ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4), ('LEFTPADDING', (0, 0), (-1, -1), 5),
        ('RIGHTPADDING', (0, 0), (-1, -1), 5), ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('BACKGROUND', (0, 0), (-1, -1), WHITE),
    ]))

    outer_rows = [
        [Paragraph(case['title'], styles['case_title_white'])],
        [t_info],
        [Paragraph('<b>Rückgabegrund / Vorgang:</b> ' + case['vorgang'], styles['body'])],
    ]
    row_backgrounds = [DARK_BLUE, WHITE, ROW_GRAY]
    if case.get('bewertung'):
        outer_rows.append([Paragraph('<b>Bewertungstext:</b> ' + case['bewertung'], styles['body'])])
        row_backgrounds.append(ROW_GRAY)
    outer_rows.append([Paragraph('<b>Hold-Information:</b> ' + case['hold'], styles['body'])])
    row_backgrounds.append(ROW_GRAY)
    outer_rows.append([Paragraph('<b>Hinweis:</b> ' + case['hinweis'], styles['red_box'])])
    row_backgrounds.append(LIGHT_RED)

    t_outer = Table(outer_rows, colWidths=[content_width])
    style_cmds = [
        ('BOX', (0, 0), (-1, -1), 1, BORDER_GRAY), ('INNERGRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#E2E8F0')),
        ('TOPPADDING', (0, 0), (-1, -1), 7), ('BOTTOMPADDING', (0, 0), (-1, -1), 7),
        ('LEFTPADDING', (0, 0), (-1, -1), 8), ('RIGHTPADDING', (0, 0), (-1, -1), 8),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
    ]
    for row_index, bg in enumerate(row_backgrounds):
        style_cmds.append(('BACKGROUND', (0, row_index), (0, row_index), bg))
    t_outer.setStyle(TableStyle(style_cmds))
    return t_outer


def build_pdf(buffer_or_path):
    styles = _styles()
    doc = SimpleDocTemplate(
        buffer_or_path, pagesize=A4, leftMargin=36, rightMargin=36, topMargin=36, bottomMargin=36,
        title='MH Audit vollstaendig', author='Trust/Risk Audit',
    )
    width = doc.width
    story = []

    # --- Titel ---------------------------------------------------------
    story.append(Paragraph('MH-Audit — vollständige Auswertung', styles['title']))
    story.append(Paragraph(
        'Kennzahlen, Kundenkommunikation, INAD-Schwerpunkt und Detailfälle | Datenstand: 08.09.2026',
        styles['subtitle']))
    story.append(Spacer(1, 12))

    # --- Executive Summary ----------------------------------------------
    story.append(Paragraph('1. Executive Summary', styles['h2']))
    story.append(Paragraph(
        'Für Partner MH wurden 155 Bestellpositionen und 121 signalverknüpfte Fallpositionen sowie sämtliche '
        'verfügbaren Kundennachrichten (149 gespeicherte Nachrichtensignale, 42 Bestellungen mit Nachrichtenverkehr), '
        '18 Rückgaben, 3 negative Bewertungen und 190 Hold-Transaktionen ausgewertet. Daraus ergeben sich '
        '<b>30 dokumentierte, an konkreten Bestellnummern belegte Kundenproblem-/Rückgabefälle</b>. Gegenüber '
        'der unveränderten Baseline vom 06.09.2026 kommen drei Fälle hinzu: ein eindeutiger INAD-Return bei der '
        'FRITZ!Box 6660, ein per Kundennachricht belegter Funktionsdefekt beim NERF-Blaster und ein Return mit '
        'eBay-Grund WRONG_SIZE bei derselben FRITZ!Box-Serie. Die dritte negative Bewertung ist nun auch '
        'API-seitig gespeichert; sie bleibt demselben bereits bekannten Patronenfall zugeordnet und wird nicht '
        'erneut gezählt.', styles['body']))
    story.append(Spacer(1, 10))

    summary_widths = _cols([1/3, 1/3, 1/3], width)
    summary_rows = [
        [Paragraph('<b>30</b><br/>dokumentierte Fälle', styles['bold_body']),
         Paragraph('<b>7</b><br/>INAD-Fälle', styles['red_box']),
         Paragraph('<b>3</b><br/>negative Bewertungen', styles['red_box'])],
        [Paragraph('<b>3</b><br/>neue Fälle seit Baseline', styles['amber_box']),
         Paragraph('<b>5</b><br/>Fälle der SKU MH44/PHI', styles['red_box']),
         Paragraph('<b>0</b><br/>doppelte Fallzählungen', styles['bold_body'])],
    ]
    t_summary = Table(summary_rows, colWidths=summary_widths)
    t_summary.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), NEUTRAL_GRAY), ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'), ('INNERGRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#E5E7EB')),
        ('BOX', (0, 0), (-1, -1), 1, BORDER_GRAY), ('TOPPADDING', (0, 0), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
    ]))
    story.append(t_summary)
    story.append(Spacer(1, 14))

    story.append(Paragraph('1.1 Vergleich 06.09.2026 vs. 08.09.2026', styles['h2']))
    compare_rows = [
        [Paragraph('Kennzahl', styles['header_white']), Paragraph('06.09.', styles['header_white']),
         Paragraph('08.09.', styles['header_white']), Paragraph('Veränderung', styles['header_white'])],
        [Paragraph('Dokumentierte Fälle', styles['body']), Paragraph('27', styles['body']), Paragraph('30', styles['bold_body']), Paragraph('+3', styles['red_box'])],
        [Paragraph('Returns', styles['body']), Paragraph('15', styles['body']), Paragraph('18', styles['bold_body']), Paragraph('+3', styles['red_box'])],
        [Paragraph('INAD / nicht wie beschrieben', styles['body']), Paragraph('6', styles['body']), Paragraph('7', styles['bold_body']), Paragraph('+1', styles['red_box'])],
        [Paragraph('Nachrichtensignale', styles['body']), Paragraph('139', styles['body']), Paragraph('149', styles['bold_body']), Paragraph('+10', styles['body'])],
        [Paragraph('Negative Bewertungen in Supabase', styles['body']), Paragraph('2 + 1 manuell bestätigt', styles['small']), Paragraph('3', styles['bold_body']), Paragraph('dritter Beleg synchronisiert; kein neuer Fall', styles['small'])],
        [Paragraph('Hold-Signale', styles['body']), Paragraph('189', styles['body']), Paragraph('190', styles['bold_body']), Paragraph('+1; Holds allein zählen nicht als Qualitätsfall', styles['small'])],
    ]
    compare = Table(compare_rows, colWidths=_cols([0.34, 0.18, 0.18, 0.30], width), repeatRows=1)
    compare.setStyle(_header_row_style(DARK_BLUE, [('ROWBACKGROUNDS', (0, 1), (-1, -1), [WHITE, ROW_GRAY]),
                                                   ('VALIGN', (0, 0), (-1, -1), 'TOP')]))
    story.append(compare)
    story.append(Spacer(1, 14))

    # --- Partnervergleich -------------------------------------------------
    story.append(Paragraph('2. Partnervergleich', styles['h2']))
    story.append(Paragraph(
        'Das eng definierte Trust/Risk-Reporting weist für MH aktuell <b>25 Qualitätsfälle bei 155 '
        'Bestellpositionen (16,13 %)</b> aus. Die breitere Beweisübersicht umfasst zusätzlich nachvollziehbare '
        'Rückgabe-/Kommunikationsfälle und kommt auf 30 dokumentierte Fälle.', styles['body']))
    story.append(Spacer(1, 8))
    story.append(_bar_chart(PARTNER_QUOTES, width, max_value=16))
    story.append(Spacer(1, 6))
    partner_widths = _cols([0.25, 0.25, 0.25, 0.25], width)
    p_data = [
        [Paragraph('Partner', styles['header_white']), Paragraph('Positionen', styles['header_white']),
         Paragraph('Qualitätsfälle', styles['header_white']), Paragraph('Mängelquote', styles['header_white'])],
        [Paragraph('<b>MH</b>', styles['bold_body']), Paragraph('155', styles['body']),
         Paragraph('25', styles['body']), Paragraph('<b>16,13 %</b>', styles['red_box'])],
        [Paragraph('NB', styles['body']), Paragraph('52', styles['body']), Paragraph('5', styles['body']),
         Paragraph('9,62 %', styles['body'])],
        [Paragraph('PP', styles['body']), Paragraph('35', styles['body']), Paragraph('1', styles['body']),
         Paragraph('2,86 %', styles['body'])],
    ]
    t_partner = Table(p_data, colWidths=partner_widths)
    t_partner.setStyle(_header_row_style(DARK_BLUE, [('ROWBACKGROUNDS', (0, 1), (-1, -1), [WHITE, ROW_GRAY])]))
    story.append(t_partner)
    story.append(Spacer(1, 4))
    story.append(Paragraph(
        'Hinweis: Die Kennzahl "25 Qualitätsfälle" stammt aus dem bestehenden Trust/Risk-Reporting mit '
        'kategorisierten Qualitätssignalen. Die Beweisübersicht führt zusätzlich belegte Rückgabe- und '
        'Kommunikationsfälle transparent auf; neutrale Fälle sind ausdrücklich als solche markiert.', styles['caption']))
    story.append(Spacer(1, 12))

    # --- Top-Problem-SKUs --------------------------------------------------
    story.append(Paragraph('3. Top-Problem-SKUs', styles['h2']))
    story.append(_bar_chart(SKU_QUOTES, width, max_value=50))
    story.append(Spacer(1, 6))
    sku_widths = _cols([0.22, 0.40, 0.19, 0.19], width)
    sku_data = [
        [Paragraph('SKU', styles['header_white']), Paragraph('Produkt', styles['header_white']),
         Paragraph('Verkäufe', styles['header_white']), Paragraph('Fehlerquote', styles['header_white'])],
        [Paragraph('<b>R13B4</b>', styles['bold_body']), Paragraph('AudioAffairs IR 010 Internetradio', styles['body']),
         Paragraph('7', styles['body']), Paragraph('<b>42,86 %</b> (3 Fälle)', styles['red_box'])],
        [Paragraph('<b>R5B1-B5</b>', styles['bold_body']), Paragraph('DeLonghi Nespresso Vertuo Pop', styles['body']),
         Paragraph('7', styles['body']), Paragraph('<b>28,57 %</b> (2 Fälle)', styles['red_box'])],
        [Paragraph('<b>MH44/PHI</b>', styles['bold_body']), Paragraph('AVM FRITZ!Box 6660 Cable (Refurbished)', styles['body']),
         Paragraph('22', styles['body']), Paragraph('<b>22,73 %</b> (5 Fälle)', styles['red_box'])],
    ]
    t_sku = Table(sku_data, colWidths=sku_widths)
    t_sku.setStyle(_header_row_style(DARK_BLUE, [('ROWBACKGROUNDS', (0, 1), (-1, -1), [WHITE, ROW_GRAY])]))
    story.append(t_sku)
    story.append(Spacer(1, 14))
    story.append(Paragraph('3.1 INAD / „Artikel nicht wie beschrieben“', styles['h2']))
    story.append(Paragraph(
        'INAD wird separat ausgewiesen, weil diese eBay-Kategorie für das Kontorisiko besonders relevant ist. '
        'Der aktuelle Datenstand enthält sieben eindeutig zugeordnete INAD-Fälle. Neu seit der Baseline ist '
        '<b>Order 03-15116-91038</b> (FRITZ!Box 6660 Cable, Return 5328622758, 86,99 EUR). Für diesen Return '
        'liefert die API keinen Käuferkommentar; deshalb wird nur der belegte eBay-Grund wiedergegeben.', styles['body']))
    inad_rows = [[Paragraph('Order ID', styles['header_white']), Paragraph('SKU', styles['header_white']), Paragraph('Beleglage', styles['header_white'])]]
    for order, sku, evidence in [
        ('02-15130-45240', 'MH44/PHI', 'Return: NOT_AS_DESCRIBED'),
        ('03-15116-91038', 'MH44/PHI', 'Neu: Return NOT_AS_DESCRIBED; kein Käuferkommentar'),
        ('04-15090-66849', 'R7B1', 'Return + Nachricht'),
        ('04-15124-90902', 'R13B4', 'Nachricht: beworbene Powerbank fehlt'),
        ('06-15117-56051', 'R6B3', 'Return: NOT_AS_DESCRIBED; kein Käuferkommentar'),
        ('08-15103-42438', 'R13B4', 'Return + Nachricht: DAB+/WLAN-Abweichung'),
        ('22-15092-17559', 'R20B3', 'Return + Nachricht: Honor 200 statt 200 Lite'),
    ]:
        inad_rows.append([Paragraph(order, styles['small_bold']), Paragraph(sku, styles['small']), Paragraph(evidence, styles['small'])])
    inad = Table(inad_rows, colWidths=_cols([0.20, 0.20, 0.60], width), repeatRows=1)
    inad.setStyle(_header_row_style(SIGNAL_RED, [('ROWBACKGROUNDS', (0, 1), (-1, -1), [WHITE, ROW_GRAY]),
                                                 ('VALIGN', (0, 0), (-1, -1), 'TOP')]))
    story.append(inad)
    story.append(PageBreak())

    # --- 3 negative Bewertungen ---------------------------------------------
    story.append(Paragraph('4. Negative Bewertungen (3 von 3 bestätigt)', styles['h2']))
    rev_widths = _cols([0.16, 0.11, 0.28, 0.45], width)
    rev_rows = [[
        Paragraph('Order ID', styles['header_white']), Paragraph('Datum', styles['header_white']),
        Paragraph('SKU / Artikel', styles['header_white']), Paragraph('Bewertungstext & Quelle', styles['header_white']),
    ]]
    for fb in NEGATIVE_FEEDBACK:
        rev_rows.append([
            Paragraph(fb['order'], styles['body']), Paragraph(fb['date'], styles['body']),
            Paragraph(fb['sku'], styles['body']),
            Paragraph(fb['text'] + '<br/><i>' + fb['quelle'] + '</i>', styles['small']),
        ])
    t_rev = Table(rev_rows, colWidths=rev_widths)
    t_rev.setStyle(_header_row_style(DARK_BLUE, [('ROWBACKGROUNDS', (0, 1), (-1, -1), [WHITE, ROW_GRAY]),
                                                  ('VALIGN', (0, 0), (-1, -1), 'TOP')]))
    story.append(t_rev)
    story.append(Spacer(1, 14))

    # --- Falschlieferungen ---------------------------------------------------
    story.append(Paragraph('5. Falschlieferungen', styles['h2']))
    story.append(Paragraph(
        '<b>Bestätigtes Wiederholungsmuster:</b> Zwei spiegelbildliche Fälle innerhalb derselben Listing-Gruppe '
        'R12B3, bei denen laut Käuferangabe jeweils die falsche HP-Patronenvariante geliefert wurde.',
        styles['body']))
    story.append(Spacer(1, 6))
    wd_widths = _cols([0.15, 0.30, 0.15, 0.15, 0.25], width)
    wd_rows = [[
        Paragraph('Order ID', styles['header_white']), Paragraph('Artikel', styles['header_white']),
        Paragraph('Bestellt', styles['header_white']), Paragraph('Geliefert', styles['header_white']),
        Paragraph('Käuferzitat', styles['header_white']),
    ]]
    for wd in WRONG_DELIVERY:
        wd_rows.append([
            Paragraph(wd['order'], styles['body']), Paragraph(wd['item'], styles['small']),
            Paragraph(wd['bestellt'], styles['body']), Paragraph(wd['geliefert'], styles['red_box']),
            Paragraph(wd['beleg'], styles['small']),
        ])
    t_wd = Table(wd_rows, colWidths=wd_widths)
    t_wd.setStyle(_header_row_style(DARK_BLUE, [('ROWBACKGROUNDS', (0, 1), (-1, -1), [WHITE, ROW_GRAY]),
                                                 ('VALIGN', (0, 0), (-1, -1), 'TOP')]))
    story.append(t_wd)
    story.append(Spacer(1, 10))
    story.append(Paragraph(
        'Zusätzlich zwei weitere Fälle mit Rückgabegrund „Falsche Variante" im System, ohne auswertbaren '
        'Nachrichtentext:', styles['body']))
    story.append(Spacer(1, 4))
    wv_widths = _cols([0.15, 0.35, 0.50], width)
    wv_rows = [[Paragraph('Order ID', styles['header_white']), Paragraph('Artikel', styles['header_white']),
                Paragraph('Beleg', styles['header_white'])]]
    for wv in WRONG_VARIANT:
        wv_rows.append([Paragraph(wv['order'], styles['body']), Paragraph(wv['item'], styles['small']),
                         Paragraph(wv['beleg'], styles['small'])])
    t_wv = Table(wv_rows, colWidths=wv_widths)
    t_wv.setStyle(_header_row_style(DARK_BLUE, [('ROWBACKGROUNDS', (0, 1), (-1, -1), [WHITE, ROW_GRAY])]))
    story.append(t_wv)
    story.append(Spacer(1, 4))
    story.append(Paragraph(
        'Insgesamt 5 Fälle betreffen Druckerpatronen (2× Falschlieferung, 3× geöffnete/unvollständige '
        'Verpackung — siehe Abschnitt 8 und Detailfälle A–E).', styles['caption']))
    story.append(PageBreak())

    # --- Kommunikationsauswertung -------------------------------------------
    story.append(Paragraph('6. Auswertung der Kundenkommunikation', styles['h2']))
    story.append(Paragraph(
        '42 MH-Bestellungen weisen Kundennachrichten auf. In Supabase liegen 149 nach externer Nachricht-ID '
        'deduplizierte Nachrichtensignale; das sind zehn mehr als in der Baseline. Verkäuferantworten und '
        'eBay-Systemtexte werden nicht als neue Qualitätsfälle gezählt. Eine belastbare, flächendeckende '
        'Aussage zu Antwortzeiten ist weiterhin nicht möglich. Feststellbar aus den vorhandenen Zeitstempeln:',
        styles['body']))
    story.append(Spacer(1, 4))
    bullets = [
        '<b>0 Fälle</b> mit belegter Antwortzeit ≥ 24 Stunden (zu wenige Seller-Antwort-Signale im Datensatz, um dies zu prüfen).',
        'In den wenigen Fällen mit erfasster Seller-Antwort lag die schnellste dokumentierte Reaktion bei rund 3,7 Stunden (Order 02-15097-54828, zweite Antwort) — kein Beleg für generell langsame Reaktion.',
        '<b>6 Fälle</b> mit wiederholtem Nachfassen des Käufers (≥2 eigenständige Nachrichten) ohne im Datensatz erkennbare inhaltliche Lösung (Details in Abschnitt 7).',
        'Datenqualitäts-Hinweis: Die automatische Klassifikation „geöffnet/benutzt" traf in mindestens 4 Fällen '
        '(u.a. Order 19-15087-98239, 21-15067-10713, 24-15066-72008, 14-15091-70890) fälschlich auf die '
        'eBay-Textfloskel „Anfrage geöffnet" (Fallbearbeitung) zu, nicht auf eine tatsächlich geöffnete '
        'Verpackung. Diese Fälle wurden für dieses Audit korrigiert eingeordnet.',
        'Positivbeispiel zur Einordnung: Order 09-15092-86078 (DJI Mic Mini) begann als Versandverzugs-Beschwerde, '
        'endete aber laut Käufer positiv: „alles in Ordnung [...] Ihre Kommunikation war gut."',
    ]
    for b in bullets:
        story.append(Paragraph('• ' + b, styles['body']))
        story.append(Spacer(1, 3))
    story.append(Spacer(1, 8))

    # --- Wiederholtes Nachfassen ---------------------------------------------
    story.append(Paragraph('7. Fälle mit wiederholtem Nachfassen / erkennbar fehlender Lösung', styles['h2']))
    rf_widths = _cols([0.16, 0.28, 0.56], width)
    rf_rows = [[Paragraph('Order ID', styles['header_white']), Paragraph('Thema', styles['header_white']),
                Paragraph('Belegte Beobachtung', styles['header_white'])]]
    for order, thema, note in REPEAT_FOLLOWUP:
        rf_rows.append([Paragraph(order, styles['body']), Paragraph(thema, styles['body']),
                         Paragraph(note, styles['small'])])
    t_rf = Table(rf_rows, colWidths=rf_widths)
    t_rf.setStyle(_header_row_style(DARK_BLUE, [('ROWBACKGROUNDS', (0, 1), (-1, -1), [WHITE, ROW_GRAY]),
                                                 ('VALIGN', (0, 0), (-1, -1), 'TOP')]))
    story.append(t_rf)
    story.append(PageBreak())

    # --- Produkt-/Qualitätsmängel + Wiederholungsmuster ----------------------
    story.append(Paragraph('8. Produkt-/Qualitätsmängel und Wiederholungsmuster derselben SKU', styles['h2']))
    sp_widths = _cols([0.14, 0.24, 0.08, 0.22, 0.32], width)
    sp_rows = [[Paragraph('SKU', styles['header_white']), Paragraph('Produkt', styles['header_white']),
                Paragraph('Fälle', styles['header_white']), Paragraph('Order-IDs', styles['header_white']),
                Paragraph('Muster', styles['header_white'])]]
    for sku, produkt, n, orders, note in SKU_PATTERNS:
        sp_rows.append([Paragraph(sku, styles['bold_body']), Paragraph(produkt, styles['small']),
                         Paragraph(str(n), styles['red_box'] if n >= 3 else styles['body']),
                         Paragraph(orders, styles['small']), Paragraph(note, styles['small'])])
    t_sp = Table(sp_rows, colWidths=sp_widths)
    t_sp.setStyle(_header_row_style(DARK_BLUE, [('ROWBACKGROUNDS', (0, 1), (-1, -1), [WHITE, ROW_GRAY]),
                                                 ('VALIGN', (0, 0), (-1, -1), 'TOP')]))
    story.append(t_sp)
    story.append(Spacer(1, 14))

    # --- Detailfälle ------------------------------------------------------
    story.append(Paragraph('9. Detailfälle mit Originalbelegen', styles['h2']))
    story.append(Paragraph(
        'Fälle A–F und 1–5 stammen aus der Baseline. Fälle G–I sind seitdem neu hinzugekommen. '
        'Mehrere Belegquellen derselben Order-/Line-Item-/SKU-Kombination bleiben jeweils ein Fall.',
        styles['caption']))
    story.append(Spacer(1, 8))
    for case in CASE_CARDS:
        card = _case_card(case, styles, width)
        story.append(KeepTogether([card, Spacer(1, 12)]))

    story.append(PageBreak())

    # --- Gesamtfalltabelle --------------------------------------------------
    story.append(Paragraph('10. Gesamttabelle aller dokumentierten Fälle (30)', styles['h2']))
    tot_widths = _cols([0.15, 0.13, 0.52, 0.20], width)
    tot_rows = [[Paragraph('Order ID', styles['header_white_small']), Paragraph('SKU', styles['header_white_small']),
                 Paragraph('Problemtyp', styles['header_white_small']), Paragraph('Belegquelle', styles['header_white_small'])]]
    for c in ALL_CASES:
        problem_style = styles['small']
        if c.get('neu'):
            problem_style = ParagraphStyle('NewCase', parent=styles['small'], textColor=DARK_RED, fontName='Helvetica-Bold')
        elif c.get('positiv'):
            problem_style = ParagraphStyle('PosCase', parent=styles['small'], textColor=colors.HexColor('#166534'))
        marker = ' [neu]' if c.get('neu') else (' [gelöst]' if c.get('positiv') else '')
        tot_rows.append([
            Paragraph(c['order'], styles['small_bold']), Paragraph(c['sku'], styles['small']),
            Paragraph(c['problem'] + marker, problem_style), Paragraph(c['beleg'], styles['small']),
        ])
    t_tot = Table(tot_rows, colWidths=tot_widths, repeatRows=1)
    t_tot.setStyle(_header_row_style(DARK_BLUE, [
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [WHITE, ROW_GRAY]), ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, -1), 3), ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
    ]))
    story.append(t_tot)
    story.append(Spacer(1, 6))
    story.append(Paragraph(
        '[neu] = gegenüber der Baseline neu oder damals nicht als Problemfall klassifiziert. '
        '[gelöst] = laut Käuferaussage positiv abgeschlossen, trotz anfänglicher Beschwerde.',
        styles['caption']))
    story.append(Spacer(1, 14))

    # --- Fazit (operativ, ohne Kündigungsempfehlung) -------------------------
    story.append(Paragraph('11. Zusammenfassung &amp; Kernbefunde', styles['h2']))
    story.append(Paragraph(
        'Die aktualisierte Vollauswertung von 155 Bestellpositionen, 149 Nachrichtensignalen, 18 Rückgaben und '
        '3 negativen Bewertungen dokumentiert 30 Kundenproblem-/Rückgabefälle. Das engere, kategorisierte '
        'Trust/Risk-Reporting zählt 25 echte Qualitätsfälle. Die Analyse identifiziert folgende zentrale '
        'Schwachstellen:', styles['body']))
    story.append(Spacer(1, 6))
    fazit_points = [
        ('Systematische Falschlieferungen bei Tinte/Toner:', (
            '5 Fälle betreffen Druckerpatronen. Besonders gravierend sind zwei spiegelbildliche Vertauschungen '
            'in der SKU-Gruppe R12B3 (HP 302 bestellt / HP 303 geliefert und umgekehrt) sowie unvollständige '
            'und geöffnete Packungen.')),
        ('Aktivierungsprobleme bei AVM-Routern:', (
            'Die FRITZ!Box 6660 Cable (SKU MH44/PHI) steigt von drei auf fünf dokumentierte Fälle. Neu sind '
            'ein eindeutiger INAD-Return und ein Return mit dem API-Grund WRONG_SIZE. Für beide fehlen '
            'Käuferkommentare, weshalb die konkrete technische Ursache offen bleibt.')),
        ('Neue Funktionsstörung:', (
            'Beim NERF Fortnite Blaster (Order 14-15092-67058) belegt die Kundennachricht ausdrücklich, dass '
            'der Artikel nicht funktioniert. Der spätere Return ist derselben Line-Item-ID zugeordnet und '
            'wird daher nicht als zweiter Fall gezählt.')),
        ('Hohe Dunkelziffer ("Versteckte Mängel"):', (
            'Fälle wie Order 08-15091-01708 und 15-15088-16137 waren systemseitig nicht als Problem geflaggt '
            'und traten erst durch die Analyse der Nachrichtentexte zutage.')),
        ('Kritische Qualitäts-Hotspots:', (
            'Einzelne SKUs zeigen extrem hohe Ausfallraten (Internetradio R13B4 mit 42,86 % Fehlerquote). '
            'Zudem existieren 6 Fälle mit mehrfachem Kunden-Nachfassen ohne dokumentierte Lösung.')),
    ]
    for label, text in fazit_points:
        story.append(Paragraph('• <b>' + label + '</b> ' + text, styles['body']))
        story.append(Spacer(1, 5))

    story.append(Spacer(1, 10))
    story.append(Paragraph('12. Einordnung der Kategorien und Datenlücken', styles['h2']))
    story.append(Paragraph(
        'Die Kategorien können sich überschneiden, wenn ein einzelner Fall mehrere belegte Mängel enthält. '
        'Der Fall wird trotzdem nur einmal gezählt. Im aktuellen Bestand sind insbesondere 7 INAD-Fälle, '
        '2 spiegelbildliche Falschlieferungen, 5 Defekt-/Beschädigt-Fälle einschließlich des neuen '
        'NERF-Funktionsausfalls, mindestens 4 belegte Gebraucht-/Geöffnet-Fälle, 2 Fälle mit fehlenden Teilen '
        'und 3 dokumentierte Fälle mit falscher Variante/Größe vorhanden. Kompatibilitäts- oder '
        'Funktionsabweichungen werden im Falltext ausgewiesen, wenn die Belegquelle dies ausdrücklich trägt.',
        styles['body']))
    story.append(Spacer(1, 6))
    for gap in [
        'Die Post-Order API liefert bei den drei neuen Returns keine Käuferkommentare. Die konkrete Ursache '
        'des neuen INAD- und WRONG_SIZE-Returns darf daher nicht weiter interpretiert werden.',
        'Im aktuellen 90-Tage-Abruf sind keine Payment Disputes verfügbar. Holds werden als Belegquelle '
        'gesichert, zählen allein aber nicht als Kunden-/Qualitätsfall.',
        'Die Message-APIs liefern keine direkten Line-Item-Referenzen in ihrer Abdeckungsstatistik. Eine '
        'Zuordnung erfolgt nur, wenn Order-, Transaktions- oder Artikelreferenz eindeutig aus dem eBay-Datensatz '
        'rekonstruiert werden kann.',
        'Die unveränderte Baseline vom 06.09.2026 bleibt separat erhalten. Diese PDF dokumentiert den '
        'ergänzenden Stand vom 08.09.2026 und ersetzt die historische Datei nicht.',
    ]:
        story.append(Paragraph('• ' + gap, styles['body']))
        story.append(Spacer(1, 4))

    doc.build(story)


def generate_pdf_bytes():
    buffer = io.BytesIO()
    build_pdf(buffer)
    return buffer.getvalue()


def main():
    pdf_bytes = generate_pdf_bytes()
    with open(OUTPUT_FILE, 'wb') as handle:
        handle.write(pdf_bytes)
    shutil.copyfile(OUTPUT_FILE, DOWNLOADS_FILE)
    print(f'PDF erzeugt: {OUTPUT_FILE} ({len(pdf_bytes):,} Bytes)')
    print(f'Kopiert nach: {DOWNLOADS_FILE}')


if __name__ == '__main__':
    main()
