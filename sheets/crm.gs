/**
 * Web Summit 2026 leads in Google Sheets, kept in step with the Dealaris campaign (jb-leads-queue, crm.py).
 *
 * Every 10 minutes, and on every edit of Status / Next step / Notes, the sheet sends what was changed by hand to
 * /api/sheet and rewrites the rows from Postgres: invite, acceptance, the last message in the LinkedIn chat, and the
 * status. Status is worked out from the campaign until someone sets it by hand. Lost or Not relevant on someone not
 * invited yet stops the campaign for them.
 *
 * Install as a standalone project on the account that owns the sheet (script.google.com -> New project; Extensions ->
 * Apps Script on the sheet fails while several Google accounts are signed in): paste this file, add the script
 * property TOKEN = SHEET_TOKEN (Project settings -> Script properties), then run setup() once and allow access.
 */
const SHEET_ID = '11KwJMd6abswTQMo1hk85XUjEwTv1rduBkWPDaYzVW9c';
const URL = 'https://jb-leads-queue-production.up.railway.app/api/sheet?campaign=websummit';
const TAB = 'Web Summit 2026';
const SUMMARY = 'Summary';
// [header, row field, width]; Status, Next step and Notes are edited by hand
const COLS = [
  ['Status', 'status', 140], ['Name', 'name', 170], ['Company', 'company', 160], ['Role', 'role', 170],
  ['Country', 'country', 100], ['Owner', 'owner', 80], ['Pipeline', 'pipeline', 130],
  ['Last message', 'last_message', 320], ['Last activity', 'last_activity', 125], ['Next step', 'next_step', 160],
  ['Notes', 'notes', 260], ['Invited', 'invited', 125], ['Accepted', 'accepted', 125], ['Replied', 'replied', 125],
  ['Score', 'score', 60], ['Niche', 'niche', 200], ['Automation note', 'auto_note', 160],
  ['key', 'key', 60], ['_status', 'status', 60], ['_next', 'next_step', 60], ['_notes', 'notes', 60],
];
const C = Object.fromEntries(COLS.map((c, i) => [c[0], i + 1]));
const EDITABLE = {[C['Status']]: ['status', C['_status']], [C['Next step']]: ['next_step', C['_next']],
                  [C['Notes']]: ['notes', C['_notes']]};
const COLORS = {
  'To review': '#f4c7c3', 'Needs follow-up': '#fce8b2', 'Meeting booked': '#b7e1cd', 'Won': '#57bb8a',
  'Waiting for reply': '#c9daf8', 'Accepted': '#d9ead3', 'Invite sent': '#eeeeee', 'Lost': '#cccccc',
  'Not relevant': '#cccccc', 'Invite not accepted': '#eeeeee', 'Skipped': '#eeeeee',
};

let SS_ = null;
function ss_() {
  return SS_ || (SS_ = SpreadsheetApp.openById(SHEET_ID));
}

// run once from the editor: adds the triggers, lays the sheet out and syncs
function setup() {
  if (!PropertiesService.getScriptProperties().getProperty('TOKEN')) throw new Error('Add the script property TOKEN first.');
  ScriptApp.getProjectTriggers().forEach(t => ScriptApp.deleteTrigger(t));
  ScriptApp.newTrigger('sync').timeBased().everyMinutes(10).create();
  ScriptApp.newTrigger('edited').forSpreadsheet(SHEET_ID).onEdit().create();
  layout_();
  sync();
  Logger.log('Done: the sheet syncs every 10 minutes and after each edit of Status, Next step or Notes.');
}

function edited(e) {
  if (!e || e.range.getSheet().getName() !== TAB || e.range.getRow() < 2) return;
  const first = e.range.getColumn(), last = e.range.getLastColumn();
  if (Object.keys(EDITABLE).some(c => c >= first && c <= last)) sync();
}

function sync() {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(30000)) return;  // the next run picks the edits up: they differ from the hidden copies
  try {
    const sh = sheet_();
    const res = call_(changes_(sh));
    write_(sh, res);
    ss_().getSheetByName(SUMMARY).getRange('B1').setValue(res.at);
  } finally {
    lock.releaseLock();
  }
}

function sheet_() {
  const ss = ss_();
  let sh = ss.getSheetByName(TAB);
  if (!sh) {
    const only = ss.getSheets().length === 1 && ss.getSheets()[0].getLastRow() === 0 ? ss.getSheets()[0] : null;
    sh = only ? only.setName(TAB) : ss.insertSheet(TAB, 0);
    layout_();
  }
  return sh;
}

function text_(v) {
  if (v instanceof Date) return Utilities.formatDate(v, ss_().getSpreadsheetTimeZone(), 'yyyy-MM-dd');
  return String(v === null || v === undefined ? '' : v).trim();
}

// what was edited by hand since the last sync: cells that differ from their hidden copies
function changes_(sh) {
  const n = sh.getLastRow() - 1;
  if (n < 1) return [];
  const vals = sh.getRange(2, 1, n, COLS.length).getValues();
  const out = [];
  vals.forEach(v => {
    const key = text_(v[C['key'] - 1]);
    if (!key) return;
    const ch = {key};
    Object.keys(EDITABLE).forEach(col => {
      const [field, copy] = EDITABLE[col];
      if (text_(v[col - 1]) !== text_(v[copy - 1])) ch[field] = text_(v[col - 1]);
    });
    if (Object.keys(ch).length > 1) out.push(ch);
  });
  return out;
}

function call_(changes) {
  const token = PropertiesService.getScriptProperties().getProperty('TOKEN');
  if (!token) throw new Error('Add the script property TOKEN, then run setup().');
  const r = UrlFetchApp.fetch(URL, {
    method: 'post', contentType: 'application/json', muteHttpExceptions: true,
    headers: {Authorization: 'Bearer ' + token}, payload: JSON.stringify({changes}),
  });
  if (r.getResponseCode() !== 200) throw new Error('Dealaris ' + r.getResponseCode() + ': ' + r.getContentText().slice(0, 200));
  return JSON.parse(r.getContentText());
}

// a value Sheets would read as a formula stays text
function safe_(v) {
  const s = String(v === null || v === undefined ? '' : v);
  return /^[=+\-@]/.test(s) ? "'" + s : s;
}

function write_(sh, res) {
  const rows = res.rows.map(r => COLS.map(([h, f]) => h === 'Score' ? (Number(r[f]) || '') : safe_(r[f])));
  const old = Math.max(sh.getLastRow() - 1, 0);
  if (old > rows.length) sh.getRange(rows.length + 2, 1, old - rows.length, COLS.length).clearContent();
  if (!rows.length) return;
  sh.getRange(2, 1, rows.length, COLS.length).setValues(rows);
  const links = res.rows.map(r => [SpreadsheetApp.newRichTextValue().setText(r.name || '—').setLinkUrl(r.url || null).build()]);
  sh.getRange(2, C['Name'], rows.length, 1).setRichTextValues(links);
  sh.getRange(2, C['Status'], Math.max(rows.length, 1), 1).setDataValidation(
    SpreadsheetApp.newDataValidation().requireValueInList(res.statuses, true).setAllowInvalid(true).build());
}

function layout_() {
  const ss = ss_();
  const sh = ss.getSheetByName(TAB) || ss.insertSheet(TAB, 0);
  sh.getRange(1, 1, 1, COLS.length).setValues([COLS.map(c => c[0])])
    .setFontWeight('bold').setBackground('#1f2937').setFontColor('#ffffff');
  sh.setFrozenRows(1);
  sh.setFrozenColumns(2);
  COLS.forEach((c, i) => sh.setColumnWidth(i + 1, c[2]));
  [C['Status'], C['Next step'], C['Notes']].forEach(c => sh.getRange(1, c).setBackground('#2a78d6'));
  sh.hideColumns(C['key'], 4);
  sh.getRange('A2:Q').setVerticalAlignment('top');
  sh.getRange(2, C['Last message'], sh.getMaxRows() - 1, 1).setWrap(true);
  sh.getRange(2, C['Notes'], sh.getMaxRows() - 1, 1).setWrap(true);
  const status = sh.getRange(2, C['Status'], sh.getMaxRows() - 1, 1);
  sh.setConditionalFormatRules(Object.entries(COLORS).map(([s, color]) =>
    SpreadsheetApp.newConditionalFormatRule().whenTextEqualTo(s).setBackground(color).setRanges([status]).build()));
  if (!sh.getFilter()) sh.getRange(1, 1, sh.getMaxRows(), C['Automation note']).createFilter();

  const sm = ss.getSheetByName(SUMMARY) || ss.insertSheet(SUMMARY);
  sm.clear();
  const order = ['To review', 'Needs follow-up', 'Meeting booked', 'Won', 'Waiting for reply', 'Accepted', 'In sequence',
                 'Invite sent', 'Queued', 'Invite not accepted', 'Lost', 'Not relevant', 'Skipped'];
  sm.getRange(1, 1, 1, 2).setValues([['Last sync', '']]);
  sm.getRange(3, 1, 1, 2).setValues([['Status', 'People']]).setFontWeight('bold');
  sm.getRange(4, 1, order.length, 2).setValues(order.map((s, i) =>
    [s, `=COUNTIF('${TAB}'!A:A, A${i + 4})`]));
  sm.getRange(4 + order.length, 1, 1, 2).setValues([['Total', `=COUNTA('${TAB}'!B2:B)`]]).setFontWeight('bold');
  sm.setColumnWidth(1, 170);
}
