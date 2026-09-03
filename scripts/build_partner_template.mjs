// Development-only template builder. The Streamlit app needs no Node runtime.
import fs from 'node:fs/promises';
import { Workbook, SpreadsheetFile } from '@oai/artifact-tool';

const workbook = Workbook.create();
const euro = '#,##0.00 "€";[Red]-#,##0.00 "€"';
for (const name of ['Rechnung', 'Gutschriften']) {
  const sheet = workbook.worksheets.add(name);
  sheet.showGridLines = false;
  const area = sheet.getRange('A1:H23');
  area.format.font.name = 'Calibri';
  area.format.font.size = 11;
  area.format.font.color = '#24364B';
  area.format.rowHeight = 23;
  area.format.verticalAlignment = 'center';
  const widths = [16, 23, 62, 9, 27, 22, 29, 31];
  widths.forEach((width, i) => {
    sheet.getRange(`${'ABCDEFGH'[i]}1:${'ABCDEFGH'[i]}23`).format.columnWidth = width;
  });
  sheet.getRange('A1:H1').merge();
  sheet.getRange('A1').values = [[name.toUpperCase() + ' · PARTNERABRECHNUNG']];
  sheet.getRange('A1:H1').format = {
    fill: '#142D45', font: { color: '#FFFFFF', size: 21, bold: true }, rowHeight: 46,
  };
  for (const range of ['A3:B3', 'A4:B4', 'C3:D3', 'C4:D4', 'E3:F3', 'E4:F4',
    'G3:H3', 'A5:C5', 'A6:C6', 'D5:H5', 'D6:H6', 'A8:H8', 'A22:H22', 'A23:H23']) {
    sheet.getRange(range).merge();
  }
  for (const [cell, label] of Object.entries({A3:'PARTNER', C3:'GRUPPE', E3:'UMSATZSTEUER', G3:'PROVISION / RABATT', A5:'EBAY-AUSZAHLUNGSNUMMER', D5:'AUSZAHLUNGSDATUM'})) {
    sheet.getRange(cell).values = [[label]];
  }
  sheet.getRange('A3:H6').format.fill = '#EEF4F8';
  sheet.getRange('A3:H6').format.horizontalAlignment = 'left';
  sheet.getRange('A3:H3').format.font = {bold:true, size:10, color:'#54677A'};
  sheet.getRange('A5:H5').format.font = {bold:true, size:10, color:'#54677A'};
  sheet.getRange('A4:D4').format.font = {bold:true, size:14};
  sheet.getRange('E4').values = [[0.19]];
  sheet.getRange('E4').setNumberFormat('0%');
  sheet.getRange('H4').values = [[0.005]];
  sheet.getRange('H4').setNumberFormat('0.0%');
  sheet.getRange('H4').format.horizontalAlignment = 'left';
  sheet.getRange('A6:H6').format.wrapText = true;
  sheet.getRange('A8').values = [['Jede Zeile entspricht einer eBay-Abrechnungstransaktion und wird in der Rechnung als eine Position mit Menge 1 verarbeitet.']];
  sheet.getRange('A8:H8').format = {wrapText:true, rowHeight:36, font:{size:11, color:'#54677A'}};
  sheet.getRange('A10:H10').values = [[
    'Bestelldatum', 'Bestellnummer', 'Artikelname', 'Stück',
    'Rechnungsbetrag netto\nvor Rabatt', 'Rabatt 0,5 %',
    'Rechnungsbetrag brutto\nnach Rabatt', 'eBay-Abrechnungsbetrag\nbrutto inkl. Versand',
  ]];
  sheet.getRange('A10:H10').format = {
    fill:'#1F5865', font:{bold:true, color:'#FFFFFF'}, wrapText:true, rowHeight:65,
  };
  // Rows 11/12 are the alternating body styles, 15..20 the summary styles.
  for (const row of [11,12]) {
    sheet.getRange(`A${row}:H${row}`).values = [[null, '', '', 1, 0, 0, 0, 0]];
    sheet.getRange(`A${row}:H${row}`).format = {fill:row===11?'#FFFFFF':'#F1F6F8', rowHeight:64, wrapText:true};
    sheet.getRange(`A${row}`).setNumberFormat('dd"."mm"."yyyy');
    sheet.getRange(`A${row}:C${row}`).format.horizontalAlignment = 'left';
    sheet.getRange(`B${row}:C${row}`).setNumberFormat('@');
    sheet.getRange(`D${row}`).setNumberFormat('0');
    sheet.getRange(`D${row}`).format.horizontalAlignment = 'center';
    sheet.getRange(`E${row}:H${row}`).setNumberFormat(euro);
    sheet.getRange(`E${row}:H${row}`).format.horizontalAlignment = 'right';
  }
  const labels = ['Summe Rechnungsbetrag netto vor Rabatt','Rabattbetrag','Netto nach Rabatt',
    '19 % Umsatzsteuer','Rechnungsbetrag brutto nach Rabatt','Summe eBay-Abrechnungsbetrag brutto inklusive Versand'];
  labels.forEach((label,i) => {
    const r=15+i;
    sheet.getRange(`A${r}:G${r}`).merge();
    sheet.getRange(`A${r}`).values=[[label]];
    sheet.getRange(`H${r}`).values=[[0]];
    sheet.getRange(`H${r}`).setNumberFormat(euro);
    sheet.getRange(`A${r}:H${r}`).format.rowHeight=27;
    if (i===4) sheet.getRange(`A${r}:H${r}`).format = {fill:'#142D45',font:{bold:true,color:'#FFFFFF'},rowHeight:34};
    else sheet.getRange(`A${r}:H${r}`).format.fill='#EEF4F8';
  });
  sheet.getRange('A22:H23').format = {wrapText:true, font:{size:10,color:'#54677A'},rowHeight:32};
  sheet.freezePanes.freezeRows(10);
}
const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(process.argv[2]);
console.log('Partner template exported.');
