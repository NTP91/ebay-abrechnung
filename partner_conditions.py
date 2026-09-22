"""Zentrale Konditionen-Quelle: Gruppe, Partnerabzug, Patrick-Vermittlungsprovision.

Die EINZIGE Stelle, an der die Prozentsätze des Abrechnungsmodells stehen.
Jede andere Stelle (partner_export.py, studio_view.py, broker_commission.py,
core.py, round_planner.py) liest hier, statt eigene Decimal-Literale zu führen.

Modell ab Runde 2026-003 ("003+"):

    Gruppe   Partner   Partnerabzug   Patrick-Vermittlungsprovision
    A        PP/BA/MK/001   0,5 %      0,0 %
    B        Standard       3,5 %      3,0 %
    B        PM (Sonder)    2,5 %      2,0 %

Evelyns wirtschaftlicher Anteil bleibt in jedem Fall 0,5 % und wird nirgends
separat parametrisiert - er ergibt sich als Partnerabzug minus
Vermittlungsprovision (3,5-3,0 = 0,5; 2,5-2,0 = 0,5; 0,5-0,0 = 0,5).

Partnerzuordnung ist IMMER der exakte SKU-Präfix vor dem ersten Slash
(core.normalized_partner()), nie ein startswith()-Präfixtreffer: "PM/ABC" -> PM,
"PM/" -> PM, "PMX/ABC" -> PMX (und damit NICHT PM).

GB-2026-001/002 laufen weiter über ihr historisches Modell; die
Vermittlungsprovision als eigener Beleg existiert erst ab FIRST_BROKER_ROUND.
"""
from decimal import Decimal

GROUP_A_PARTNERS = ('PP', 'BA', 'MK', '001')

# (Partnerabzug, Patrick-Vermittlungsprovision)
GROUP_A_RATES = (Decimal('0.005'), Decimal('0'))
GROUP_B_RATES = (Decimal('0.035'), Decimal('0.030'))
SPECIAL_RATES = {'PM': (Decimal('0.025'), Decimal('0.020'))}

# Erste Runde, ab der die Vermittlungsabrechnung Patrick -> Evelyn als eigener
# Beleg existiert. Für PM gilt zusätzlich die Aktivierungsregel in
# broker_commission.pm_effective_round().
FIRST_BROKER_ROUND = '2026-003'


def code(partner):
    """Der normalisierte Partnercode (exakter Vergleich, nie startswith)."""
    return str(partner or '').strip().upper()


def group_for(partner):
    """Gruppe allein aus dem exakten Partnercode - kein Präfix-Heuristik."""
    return 'Gruppe A' if code(partner) in GROUP_A_PARTNERS else 'Gruppe B'


def conditions(partner, group=None):
    """Konditionen eines Partners. `group` übersteuert die Ableitung, damit
    bestehende Daten mit bereits gesetzter Gruppenspalte maßgeblich bleiben."""
    name = code(partner)
    resolved = group or group_for(name)
    if resolved == 'Gruppe A':
        partner_rate, broker_rate = GROUP_A_RATES
    else:
        partner_rate, broker_rate = SPECIAL_RATES.get(name, GROUP_B_RATES)
    return dict(partner=name, group=resolved, partner_rate=partner_rate,
                broker_rate=broker_rate, special=name in SPECIAL_RATES,
                effective_from=FIRST_BROKER_ROUND)


def partner_rate(partner, group=None):
    """Partnerabzug auf Netto - die Quelle für partner_export.calculate_sheet."""
    return conditions(partner, group)['partner_rate']


def broker_rate(partner, group=None):
    """Patricks Vermittlungsprovision. Gruppe A ist immer 0 und darf damit
    strukturell nie in eine Vermittlungsabrechnung geraten."""
    return conditions(partner, group)['broker_rate']


def percent(value):
    return f'{value * 100:.1f}'.replace('.', ',') + ' %'


def label(partner, group=None, broker=False):
    """Kurztext der Kondition.

    broker=False (Default) ist der Text für die Partner-Excel. Er nennt
    bewusst NIE Patricks Vermittlungsprovision: die Partnerabrechnung geht an
    den Partner und darf Patricks Marge nicht offenlegen (dieselbe Regel, die
    test_partner_export.check_workbook seit jeher prüft).

    broker=True ist der interne Text für die Partnerkarte im Studio.
    """
    item = conditions(partner, group)
    text = f"{percent(item['partner_rate'])} Abzug · Rechnung direkt an Evelyn"
    if broker and item['group'] == 'Gruppe B':
        text += f" · Patrick-Provision {percent(item['broker_rate'])}"
    if item['special']:
        text = 'Sonderkondition · ' + text
    return text
