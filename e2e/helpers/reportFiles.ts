import fs from 'fs';
import AdmZip from 'adm-zip';
// pdf-parse has no ESM/TS default export shape that plays well with
// esModuleInterop for a dynamic Buffer call - required as CJS directly.
// eslint-disable-next-line @typescript-eslint/no-var-requires
const pdfParse = require('pdf-parse');

/** The 12 spec-named sheets src/reporting.py's write_excel_report always
 * writes, in order (src/reporting.py::SHEET_NAMES). */
export const EXPECTED_XLSX_SHEETS = [
  'Executive Summary', 'KPIs', 'Regions', 'Categories', 'Products',
  'Customers', 'Anomalies', 'Root Causes', 'Impact', 'Recommendations',
  'Forecast', 'Data Quality',
];

export interface XlsxCheck {
  valid: boolean;
  sizeBytes: number;
  sheetNames: string[];
  missingSheets: string[];
}

/**
 * A real, well-formed .xlsx is a ZIP/OOXML archive. Opens it as one and
 * reads the workbook's declared sheet names from xl/workbook.xml, rather
 * than trusting the file extension.
 */
export function assertValidXlsx(filePath: string): XlsxCheck {
  const sizeBytes = fs.statSync(filePath).size;
  if (sizeBytes === 0) return { valid: false, sizeBytes, sheetNames: [], missingSheets: EXPECTED_XLSX_SHEETS };

  const zip = new AdmZip(filePath);
  const workbookEntry = zip.getEntry('xl/workbook.xml');
  if (!workbookEntry) return { valid: false, sizeBytes, sheetNames: [], missingSheets: EXPECTED_XLSX_SHEETS };

  const xml = zip.readAsText(workbookEntry);
  const sheetNames = Array.from(xml.matchAll(/<sheet[^>]*name="([^"]+)"/g)).map((m) => m[1]);
  const missingSheets = EXPECTED_XLSX_SHEETS.filter((name) => !sheetNames.includes(name));

  return { valid: missingSheets.length === 0, sizeBytes, sheetNames, missingSheets };
}

export interface PdfCheck {
  valid: boolean;
  sizeBytes: number;
  numPages: number;
}

export async function assertValidPdf(filePath: string): Promise<PdfCheck> {
  const buf = fs.readFileSync(filePath);
  const sizeBytes = buf.length;
  const hasMagic = buf.subarray(0, 5).toString('ascii') === '%PDF-';
  if (!hasMagic) return { valid: false, sizeBytes, numPages: 0 };

  const parsed = await pdfParse(buf);
  return { valid: parsed.numpages >= 1, sizeBytes, numPages: parsed.numpages };
}
