"""Conservative local table extraction. Unknown content never implies a match."""
import csv
import io
import re
import unicodedata
from datetime import date, datetime
from decimal import Decimal


def norm(value):
    return re.sub(r'[^a-z0-9]', '', unicodedata.normalize('NFKD',str(value or '')).encode('ascii','ignore').decode().lower())


ALIASES = {
    'order':('Bestellnummer','eBay-Bestellnummer','Order number'),
    'sku':('SKU','Bestandseinheit'),
    'article':('Artikel','Artikelname','Angebotstitel','Produkt','Produkttitel'),
    'extra':('Zusatztext','Beschreibung'),
    'quantity':('Menge','Stück','Anzahl','Quantity'),
    'net':('Netto vor Rabatt','Rechnungsbetrag netto vor Rabatt','VK netto','Einzelpreis netto','VK netto in Lexoffice eintragen'),
    'net_after':('Netto nach Rabatt','Positionsbetrag netto'),
    'gross':('Positionsbetrag brutto','Rechnungsbetrag brutto nach Rabatt','Zeilenbetrag brutto'),
    'rate':('Rabatt %','Rabatt in %','Discount %'),
    'discount':('Rabattbetrag','Rabatt netto'),
    'number':('Rechnungsnummer','Invoice number'),
    'invoice_date':('Rechnungsdatum','Invoice date'),
    'total':('Gesamtbetrag brutto','Rechnungsbetrag brutto','Bruttosumme','Gesamtsumme brutto'),
}
LOOKUP = {norm(alias):field for field,aliases in ALIASES.items() for alias in aliases}


def cell(value):
    if isinstance(value,(date,datetime)):
        return value.strftime('%d.%m.%Y')
    return str(value).strip() if value is not None else ''


def extract(content, filename):
    result = dict(items=[], number='', invoice_date='', total='', warnings=[], errors=[], text='')
    tables=[]
    try:
        suffix=filename.lower().rsplit('.',1)[-1]
        if suffix=='csv':
            try: text=content.decode('utf-8-sig')
            except UnicodeDecodeError: text=content.decode('cp1252')
            delimiter=';' if text.count(';')>=text.count(',') else ','
            tables=[list(csv.reader(io.StringIO(text),delimiter=delimiter))]
            result['text']=text
        elif suffix=='xlsx':
            from openpyxl import load_workbook
            workbook=load_workbook(io.BytesIO(content),data_only=True)
            for sheet in workbook:
                if sheet.sheet_state!='visible': continue
                rows=[]
                for row in sheet:
                    if sheet.row_dimensions[row[0].row].hidden: continue
                    values=[]
                    for c in row:
                        value=c.value
                        if isinstance(value,(int,float)) and '%' in c.number_format:
                            value=str(Decimal(str(value))*100)+' %'
                        values.append(cell(value))
                    rows.append(values)
                tables.append(rows)
            workbook.close()
        elif suffix=='pdf':
            import pdfplumber
            with pdfplumber.open(io.BytesIO(content)) as document:
                if len(document.pages)>100:
                    raise ValueError('Mehr als 100 PDF-Seiten; manuelle Prüfung erforderlich.')
                for page in document.pages:
                    text=page.extract_text() or ''
                    result['text']+=text+'\n'
                    found=page.extract_tables()
                    tables.extend(found or [[re.split(r'\s{2,}|\|',line) for line in (page.extract_text(layout=True) or '').splitlines()]])
            if not result['text'].strip():
                result['warnings'].append('PDF enthält keinen auslesbaren Text; Scan/OCR manuell prüfen.')
        else:
            raise ValueError('Unterstützt werden PDF, XLSX und CSV.')
        metadata={'number':set(),'invoice_date':set(),'total':set()}
        for table in tables:
            mapping=None
            header_rate=''
            had_header=False
            ambiguous_rate=False
            table_totals=set()
            item_count=len(result['items'])
            for raw in table:
                row=[cell(value) for value in raw]
                nonempty=[value for value in row if value]
                if not nonempty: continue
                fields=[LOOKUP.get(norm(value)) for value in row]
                if 'order' in fields and len([f for f in fields if f])>=2:
                    had_header=True
                    mapping={index:field for index,field in enumerate(fields) if field}
                    ambiguous_rate=any(norm(value)=='rabatt' and '%' not in value for value in row)
                    if len(mapping.values())!=len(set(mapping.values())):
                        result['warnings'].append('Mehrdeutige Tabellenspalten erkannt.')
                    header_rate=''
                    for index,value in enumerate(row):
                        match=re.search(r'Rabatt\s*(\d+(?:[.,]\d+)?)\s*%',value,re.I)
                        if match:
                            header_rate=match.group(1)
                            mapping[index]='discount'
                    continue
                first=norm(nonempty[0])
                meta_field=LOOKUP.get(first)
                if len(nonempty)>=2 and meta_field in metadata:
                    (table_totals if meta_field=='total' else metadata[meta_field]).add(nonempty[-1]); continue
                if first=='rechnungsbetragbruttonachrabatt' and len(nonempty)>=2:
                    table_totals.add(nonempty[-1]); continue
                if not mapping: continue
                if first.startswith(('summe','rabattbetrag','nettonachrabatt','19umsatzsteuer','rabattnettoineuro','umsatzsteuer19','ebayauszahlungsbetrag','rabattbruttoineuro')):
                    mapping=None
                    continue
                if first.startswith(('ebayauszahlungsnummer','partner','gruppe','auszahlungsdatum','angewendeter','keineerstattungen','keinepositionen','rechenweg')):
                    continue
                item={field:row[index] if index<len(row) else '' for index,field in mapping.items()}
                if not item.get('order'):
                    result['warnings'].append('Nicht eindeutig zuordenbare Tabellenzeile: '+' | '.join(nonempty)[:180]); continue
                for field in metadata:
                    if item.get(field): (table_totals if field=='total' else metadata[field]).add(item[field])
                if header_rate: item['rate']=header_rate
                if ambiguous_rate and item.get('rate') and '%' not in item['rate']:
                    result['warnings'].append('Rabattsatz ohne eindeutig erkannte Prozent-Einheit.')
                article=item.get('article','').replace('\\n','\n')
                extra=item.get('extra','').replace('\\n','\n')
                sku=re.search(r'(?:^|\n)\s*SKU:\s*([^\n]+)',extra or article,re.I)
                if sku:
                    item.setdefault('sku',sku.group(1).strip())
                    if not item.get('sku'): item['sku']=sku.group(1).strip()
                    if not extra: item['article']=article[:sku.start()].strip()
                result['items'].append(item)
            if len(result['items'])>item_count or not had_header:
                metadata['total'].update(table_totals)
        for field,values in metadata.items():
            if len(values)>1:
                label={'number':'Rechnungsnummer','invoice_date':'Rechnungsdatum','total':'Gesamtbetrag brutto'}[field]
                result['errors'].append('Widersprüchliche Angaben für '+label+': '+', '.join(sorted(values)))
            elif values: result[field]=next(iter(values))
        for field,pattern in [('number',r'Rechnungs(?:nummer|[- ]?Nr\.?)(?:\s*:\s*|\s+)([^\s;,]+)'),('invoice_date',r'Rechnungsdatum\s*:?\s*(\d{2}\.\d{2}\.\d{4})'),('total',r'(?im)^\s*(?:Gesamtbetrag brutto|Rechnungsbetrag brutto|Bruttosumme)\s*:?\s*(-?[\d.,]+)\s*(?:EUR|€)?\s*$')]:
            matches=set(re.findall(pattern,result['text'],re.I))
            if not result[field] and len(matches)==1: result[field]=next(iter(matches))
        if not result['items']:
            result['warnings'].append('Keine eindeutig strukturierte Positionstabelle erkannt.')
    except Exception as exc:
        result['warnings'].append('Datei konnte nicht vollständig ausgelesen werden: '+type(exc).__name__)
    return result
