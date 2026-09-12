// Read-only QA and German PNG previews of both sheets; never re-exports XLSX.
// Run in the bundled artifact-tool/Sharp runtime, with verification.json beside the inputs.
import fs from 'node:fs/promises';
import path from 'node:path';
import sharp from 'sharp';
import { FileBlob, SpreadsheetFile } from '@oai/artifact-tool';

const directory = process.argv[2];
const expected = JSON.parse(await fs.readFile(path.join(directory,'verification.json'),'utf8'));
for (const [filename,sheets] of Object.entries(expected)) {
  const wb = await SpreadsheetFile.importXlsx(await FileBlob.load(path.join(directory,filename)));
  // Force a dependency change/restoration so QA checks freshly calculated
  // formulas, not just the cached values embedded in the generated file.
  for (const name of Object.keys(sheets)) {
    const sheet=wb.worksheets.getItem(name);
    const value=sheet.getRange('G15').values[0][0];
    if(typeof value==='number') {
      sheet.getRange('G15').values=[[value+.01]];
      sheet.getRange('J15').values;
      sheet.getRange('G15').values=[[value]];
    }
  }
  const scan = await wb.inspect({kind:'match',searchTerm:'#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!',
    options:{useRegex:true,maxResults:20},summary:'Final formula error scan'});
  console.log(filename,scan.ndjson);
  const images=[];
  let verified=0;
  const snapshots={};
  for (const [name,checks] of Object.entries(sheets)) {
    const sheet=wb.worksheets.getItem(name);
    for (const [cell,value] of Object.entries(checks.formulas)) {
      const actual=sheet.getRange(cell).values[0][0];
      if (typeof actual!=='number' || Math.abs(actual-value)>1e-8) {
        throw new Error(`${filename} ${name}!${cell}: formula=${actual}, independent cached result=${value}`);
      }
      verified++;
    }
    snapshots[name]=sheet.getRange(`A1:K${checks.lastRow}`).values;
  }
  // Freeze a complete snapshot before changing any display values. Otherwise
  // localization can cause still-live formulas to recalculate from text inputs.
  for (const [name,checks] of Object.entries(sheets)) {
    const sheet=wb.worksheets.getItem(name);
    sheet.getRange(`A1:K${checks.lastRow}`).values=snapshots[name];
  }
  for (const [name,checks] of Object.entries(sheets)) {
    const sheet=wb.worksheets.getItem(name);
    // Artifact rendering uses an English display locale. Localize the preview
    // values only; the actual workbook retains typed dates/numbers and formulas.
    const last=checks.lastRow;
    const values=snapshots[name];
    for(let r=0;r<last;r++) for(let c=0;c<11;c++) {
      const value=values[r]?.[c];
      if(typeof value!=='number') continue;
      const cell=sheet.getCell(r,c);
      let text;
      if(r===3 && (c===6||c===8) || r>=14 && (c===7||c===8)) {
        text=(value*100).toLocaleString('de-DE',{minimumFractionDigits:1,maximumFractionDigits:1})+' %';
      } else if(r>=14 && c===0) {
        text=new Date(Date.UTC(1899,11,30)+value*86400000).toLocaleDateString('de-DE',{day:'2-digit',month:'2-digit',year:'numeric',timeZone:'UTC'});
      } else if(r>=14 && (c===6||c===9||c===10)) {
        text=value.toLocaleString('de-DE',{minimumFractionDigits:2,maximumFractionDigits:2})+' €';
        if(value<0) cell.format.font.color='#B42318';
      } else text=String(value);
      cell.values=[[text]];
      cell.setNumberFormat('@');
    }
    const result=await wb.render({sheetName:name,range:`A1:K${last}`,scale:1.25,format:'png'});
    const bytes=Buffer.from(await result.arrayBuffer());
    images.push({bytes,meta:await sharp(bytes).metadata()});
  }
  const width=Math.max(...images.map(i=>i.meta.width));
  const gap=30;
  const height=images.reduce((sum,i)=>sum+i.meta.height,0)+gap;
  let top=0;
  const parts=images.map(i=>{const part={input:i.bytes,top,left:0};top+=i.meta.height+gap;return part;});
  await sharp({create:{width,height,channels:4,background:'#FFFFFF'}}).composite(parts).png()
    .toFile(path.join(directory,filename.replace('.xlsx','.png')));
  console.log(`${filename}: ${verified} formula cells agree; both sheets rendered, ${width} × ${height}.`);
}
