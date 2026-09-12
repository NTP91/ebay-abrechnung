// Development-only layout builder; no Node dependency in the Streamlit app.
import { Workbook, SpreadsheetFile } from '@oai/artifact-tool';

const workbook = Workbook.create();
const euro = '[$-407]#,##0.00 "€";[Red]-#,##0.00 "€"';
const percent = '[$-407]0.0%';
for (const name of ['Rechnung', 'Gutschriften']) {
  const sheet = workbook.worksheets.add(name);
  sheet.showGridLines = false;
  const area = sheet.getRange('A1:K27');
  area.format = {font:{name:'Calibri',size:11,color:'#233648'},rowHeight:23,verticalAlignment:'center'};
  [16,23,48,42,9,11,24,13,16,27,28].forEach((width,i) => {
    sheet.getRange(`${'ABCDEFGHIJK'[i]}1:${'ABCDEFGHIJK'[i]}27`).format.columnWidth=width;
  });
  for (const range of ['A1:K1','A2:K2','A3:B3','A4:B4','C3:D3','C4:D4','E3:F3','E4:F4',
    'G3:H3','G4:H4','I3:K3','I4:K4','A5:D5','A6:D7','E5:K5','E6:K6','E7:K7',
    'A9:K9','A10:F10','G10:K10','A11:F11','G11:K11','A12:F12','G12:K12','A13:K13','A27:K27']) {
    sheet.getRange(range).merge();
  }
  sheet.getRange('A1').values=[[name.toUpperCase()+' · ABRECHNUNG']];
  sheet.getRange('A1:K1').format={fill:'#EAF2F8',font:{size:23,bold:true,color:'#173B53'},rowHeight:42};
  sheet.getRange('A2').values=[['Partner-Einzelabrechnung']];
  sheet.getRange('A2:K2').format={font:{size:12,color:'#52687A'},rowHeight:25};
  sheet.getRange('A3:K7').format={fill:'#F5F8FB',horizontalAlignment:'left',wrapText:true};
  for (const [cell,label] of Object.entries({A3:'PARTNER',C3:'GRUPPE',E3:'RECHNUNGSEMPFÄNGER',G3:'RABATT',I3:'UMSATZSTEUER',
    A5:'RECHNUNGSADRESSE',E5:'EBAY-AUSZAHLUNGSNUMMERN'})) sheet.getRange(cell).values=[[label]];
  sheet.getRange('A3:K3').format.font={size:10,bold:true,color:'#52687A'};
  sheet.getRange('A5:K5').format.font={size:10,bold:true,color:'#52687A'};
  sheet.getRange('A4:K4').format={font:{size:13,bold:true},rowHeight:30};
  sheet.getRange('G4').values=[[.005]];
  sheet.getRange('I4').values=[[.19]];
  sheet.getRange('G4').setNumberFormat(percent);
  sheet.getRange('I4').setNumberFormat(percent);
  sheet.getRange('A6').values=[['Rechnungsadresse noch nicht hinterlegt']];
  sheet.getRange('A6:D7').format.font.color='#8B4A18';
  sheet.getRange('A9').values=[['Wichtig: Die oben aufgeführte(n) eBay-Auszahlungsnummer(n) müssen in Lexoffice als Freitext auf der Rechnung eingetragen werden.']];
  sheet.getRange('A9:K9').format={fill:'#FFF0F0',font:{bold:true,color:'#B42318'},rowHeight:38,wrapText:true};
  sheet.getRange('A10').values=[['HELLGRÜN · Diese Spalten in Lexoffice eintragen.']];
  sheet.getRange('G10').values=[['HELLGRAU · Diese Spalten dienen nur zur Kontrolle.']];
  sheet.getRange('A10:F10').format={fill:'#DDF0DF',font:{bold:true,color:'#245B32'},rowHeight:27};
  sheet.getRange('G10:K10').format={fill:'#EAEDF0',font:{bold:true,color:'#475569'},rowHeight:27};
  sheet.getRange('A11').values=[['Artikelname → oberes Lexoffice-Feld „Artikel“. Zusatztext → vollständig in das Feld darunter.']];
  sheet.getRange('G11').values=[['VK netto → exakt in „VK (Netto)“ übernehmen.']];
  sheet.getRange('A12').values=[['Menge immer 1 · Einheit „Stück“ · Rabatt und Umsatzsteuer je Position übernehmen.']];
  sheet.getRange('G12').values=[['Nettorechnung verwenden; keinen zusätzlichen Gesamtrabatt setzen.']];
  sheet.getRange('A11:K12').format={wrapText:true,rowHeight:29,font:{size:11,color:'#42576A'}};
  sheet.getRange('A13').values=[['0 Positionen']];
  sheet.getRange('A13:K13').format={font:{size:10,color:'#52687A'},rowHeight:27};
  sheet.getRange('A14:K14').values=[['Bestelldatum','Bestellnummer','Artikelname','Zusatztext','Menge','Einheit',
    'VK netto – in Lexoffice\neintragen','Rabatt','Umsatzsteuer','Rechnungsbetrag brutto\nnach Rabatt','eBay-Auszahlungsbetrag\nbrutto inklusive Versand']];
  sheet.getRange('A14:K14').format={font:{bold:true},wrapText:true,rowHeight:58,horizontalAlignment:'left'};
  for(const row of [14,15,16]) {
    sheet.getRange(`A${row}:B${row}`).format.fill=row===14?'#E3E7EB':'#F2F4F6';
    sheet.getRange(`C${row}:I${row}`).format.fill=row===14?'#CBE6CF':row===15?'#EFF8EF':'#E6F2E8';
    sheet.getRange(`J${row}:K${row}`).format.fill=row===14?'#E3E7EB':'#F2F4F6';
  }
  for(const row of [15,16]) {
    sheet.getRange(`A${row}:K${row}`).values=[[null,'','','',1,'Stück',0,.005,.19,0,0]];
    sheet.getRange(`A${row}:K${row}`).format={wrapText:true,rowHeight:60,horizontalAlignment:'left'};
    sheet.getRange(`A${row}`).setNumberFormat('dd"."mm"."yyyy');
    sheet.getRange(`B${row}:D${row}`).setNumberFormat('@');
    sheet.getRange(`E${row}`).setNumberFormat('0');
    sheet.getRange(`E${row}`).format.horizontalAlignment='center';
    for(const col of ['G','J','K']) {
      sheet.getRange(`${col}${row}`).setNumberFormat(euro);
      sheet.getRange(`${col}${row}`).format.horizontalAlignment='right';
    }
    sheet.getRange(`H${row}:I${row}`).setNumberFormat(percent);
    sheet.getRange(`H${row}:I${row}`).format.horizontalAlignment='right';
  }
  sheet.getRange('E14').format.horizontalAlignment='center';
  for(const col of ['G','H','I','J','K']) sheet.getRange(`${col}14`).format.horizontalAlignment='right';
  const labels=['Summe VK netto vor Rabatt','Rabatt netto in Euro','Netto nach Rabatt','Umsatzsteuer 19 %',
    'Rechnungsbetrag brutto nach Rabatt','eBay-Auszahlungsbetrag brutto','Rabatt brutto in Euro'];
  labels.forEach((label,i)=>{
    const row=19+i;
    sheet.getRange(`A${row}:J${row}`).merge();
    sheet.getRange(`A${row}`).values=[[label]];
    sheet.getRange(`K${row}`).values=[[0]];
    sheet.getRange(`K${row}`).setNumberFormat(euro);
    sheet.getRange(`K${row}`).format.horizontalAlignment='right';
    sheet.getRange(`A${row}:K${row}`).format={fill:i===4?'#D5EADB':'#F2F5F7',rowHeight:i===4?33:26,font:{bold:i===4,color:'#233648'}};
  });
  sheet.getRange('A27:K27').format={wrapText:true,rowHeight:42,font:{size:10,color:'#52687A'}};
  sheet.freezePanes.freezeRows(14);
}
await (await SpreadsheetFile.exportXlsx(workbook)).save(process.argv[2]);
console.log('Partner template exported.');
