/**
 * Dealaris campaign leads in Google Sheets (Web Summit 2026 and the job boards), kept in step with the campaign
 * (jb-leads-queue, crm.py), with a Dashboard tab: reply rate, positive / negative replies, meetings, and charts,
 * for one campaign or all of them.
 *
 * Every 10 minutes, and on every edit of Status / Reply type / Next step / Notes, the sheet sends what was changed by
 * hand to /api/sheet and rewrites the rows from Postgres: invite, acceptance, the last message in the LinkedIn chat,
 * the status and the reply type. Status is worked out from the campaign until someone sets it by hand; Reply type
 * (Positive / Neutral / Negative) comes from gpt-5-mini until someone sets it. Lost or Not relevant on someone not
 * invited yet stops the campaign for them.
 *
 * Install as a standalone project on the account that owns the sheet (script.google.com -> New project; Extensions ->
 * Apps Script on the sheet fails while several Google accounts are signed in): paste this file and appsscript.json,
 * then run setup() once and allow access. Requests are signed with the account's Google ID token, which the server
 * checks (fortwiseai@gmail.com), so there is no secret to paste.
 */
const SHEET_ID = '11KwJMd6abswTQMo1hk85XUjEwTv1rduBkWPDaYzVW9c';
const URL = 'https://jb-leads-queue-production.up.railway.app/api/sheet?campaign=all';
const TAB = 'Leads';
const DASH = 'Dashboard';
const LAYOUT = '2';  // bump when the columns change: the next sync lays the sheet out again
// [header, row field, width]; Status, Reply type, Next step and Notes are edited by hand
const COLS = [
  ['Status', 'status', 140], ['Campaign', 'campaign', 120], ['Name', 'name', 170], ['Company', 'company', 160],
  ['Role', 'role', 170], ['Country', 'country', 100], ['Owner', 'owner', 75], ['Pipeline', 'pipeline', 130],
  ['Reply type', 'reply_type', 95], ['Last message', 'last_message', 320], ['Last activity', 'last_activity', 125],
  ['Next step', 'next_step', 160], ['Notes', 'notes', 260], ['Invited', 'invited', 125],
  ['Accepted', 'accepted', 125], ['Replied', 'replied', 125], ['Score', 'score', 60], ['Niche', 'niche', 200],
  ['Automation note', 'auto_note', 160],
  ['key', 'key', 60], ['_status', 'status', 60], ['_reply', 'reply_type', 60], ['_next', 'next_step', 60],
  ['_notes', 'notes', 60],
];
const C = Object.fromEntries(COLS.map((c, i) => [c[0], i + 1]));
const HIDDEN = C['key'];  // key and the copies of the hand-edited cells, from here to the end
const EDITABLE = {[C['Status']]: ['status', C['_status']], [C['Reply type']]: ['reply_type', C['_reply']],
                  [C['Next step']]: ['next_step', C['_next']], [C['Notes']]: ['notes', C['_notes']]};
// the Web Summit-only layout before LAYOUT 2, so edits made in it aren't lost when the sheet is laid out again
const OLD = {tab: 'Web Summit 2026', key: 18, cols: {1: ['status', 19], 10: ['next_step', 20], 11: ['notes', 21]}};
const COLORS = {
  'To review': '#f4c7c3', 'Needs follow-up': '#fce8b2', 'Meeting booked': '#b7e1cd', 'Won': '#57bb8a',
  'Waiting for reply': '#c9daf8', 'Accepted': '#d9ead3', 'Invite sent': '#eeeeee', 'Lost': '#cccccc',
  'Not relevant': '#cccccc', 'Invite not accepted': '#eeeeee', 'Skipped': '#eeeeee',
};
const REPLY_COLORS = {'Positive': '#b7e1cd', 'Neutral': '#eeeeee', 'Negative': '#f4c7c3'};

let SS_ = null;
function ss_() {
  return SS_ || (SS_ = SpreadsheetApp.openById(SHEET_ID));
}

// run once from the editor: adds the triggers, lays the sheet out and syncs
function setup() {
  ScriptApp.getProjectTriggers().forEach(t => ScriptApp.deleteTrigger(t));
  ScriptApp.newTrigger('sync').timeBased().everyMinutes(10).create();
  ScriptApp.newTrigger('edited').forSpreadsheet(SHEET_ID).onEdit().create();
  sync();
  Logger.log('Done: the sheet syncs every 10 minutes and after each hand edit.');
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
    const props = PropertiesService.getScriptProperties();
    let changes;
    if (props.getProperty('LAYOUT') !== LAYOUT) {
      const old = ss_().getSheetByName(OLD.tab);
      changes = old ? changes_(old, OLD.key, OLD.cols) : [];
      layout_();
      props.setProperty('LAYOUT', LAYOUT);
    } else {
      changes = changes_(ss_().getSheetByName(TAB), C['key'], EDITABLE);
    }
    const res = call_(changes);
    write_(ss_().getSheetByName(TAB), res);
    ss_().getSheetByName(DASH).getRange('E1').setValue('Last sync: ' + res.at);
  } finally {
    lock.releaseLock();
  }
}

function text_(v) {
  if (v instanceof Date) return Utilities.formatDate(v, ss_().getSpreadsheetTimeZone(), 'yyyy-MM-dd');
  return String(v === null || v === undefined ? '' : v).trim();
}

// what was edited by hand since the last sync: cells that differ from their hidden copies
function changes_(sh, keyCol, cols) {
  const n = sh.getLastRow() - 1;
  if (n < 1) return [];
  const vals = sh.getRange(2, 1, n, sh.getLastColumn()).getValues();
  const out = [];
  vals.forEach(v => {
    const key = text_(v[keyCol - 1]);
    if (!key) return;
    const ch = {key};
    Object.keys(cols).forEach(col => {
      const [field, copy] = cols[col];
      if (text_(v[col - 1]) !== text_(v[copy - 1])) ch[field] = text_(v[col - 1]);
    });
    if (Object.keys(ch).length > 1) out.push(ch);
  });
  return out;
}

function call_(changes) {
  const r = UrlFetchApp.fetch(URL, {
    method: 'post', contentType: 'application/json', muteHttpExceptions: true,
    headers: {Authorization: 'Bearer ' + ScriptApp.getIdentityToken()}, payload: JSON.stringify({changes}),
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
  const list = values => SpreadsheetApp.newDataValidation().requireValueInList(values, true).setAllowInvalid(true).build();
  sh.getRange(2, C['Status'], rows.length, 1).setDataValidation(list(res.statuses));
  sh.getRange(2, C['Reply type'], rows.length, 1).setDataValidation(list(res.reply_types));
  ss_().getSheetByName(DASH).getRange('B1').setDataValidation(list(['All'].concat(res.campaigns)));
}

function letter_(col) {
  let s = '';
  for (; col > 0; col = Math.floor((col - 1) / 26)) s = String.fromCharCode(65 + (col - 1) % 26) + s;
  return s;
}

function layout_() {
  const ss = ss_();
  const old = ss.getSheetByName(OLD.tab);
  let sh = ss.getSheetByName(TAB);
  if (!sh && old) sh = old.setName(TAB);
  if (!sh) sh = ss.insertSheet(TAB, 0);
  if (sh.getFilter()) sh.getFilter().remove();
  sh.clear();
  sh.clearConditionalFormatRules();
  sh.getDataRange().clearDataValidations();
  sh.showColumns(1, sh.getMaxColumns());
  if (sh.getMaxColumns() < COLS.length) sh.insertColumnsAfter(sh.getMaxColumns(), COLS.length - sh.getMaxColumns());
  sh.getRange(1, 1, 1, COLS.length).setValues([COLS.map(c => c[0])])
    .setFontWeight('bold').setBackground('#1f2937').setFontColor('#ffffff');
  sh.setFrozenRows(1);
  sh.setFrozenColumns(3);
  COLS.forEach((c, i) => sh.setColumnWidth(i + 1, c[2]));
  [C['Status'], C['Reply type'], C['Next step'], C['Notes']].forEach(c => sh.getRange(1, c).setBackground('#2a78d6'));
  sh.hideColumns(HIDDEN, COLS.length - HIDDEN + 1);
  const body = (col) => sh.getRange(2, col, sh.getMaxRows() - 1, 1);
  sh.getRange(2, 1, sh.getMaxRows() - 1, HIDDEN - 1).setVerticalAlignment('top');
  body(C['Last message']).setWrap(true);
  body(C['Notes']).setWrap(true);
  const rule = (range, text, color) =>
    SpreadsheetApp.newConditionalFormatRule().whenTextEqualTo(text).setBackground(color).setRanges([range]).build();
  sh.setConditionalFormatRules(
    Object.entries(COLORS).map(([s, color]) => rule(body(C['Status']), s, color))
      .concat(Object.entries(REPLY_COLORS).map(([s, color]) => rule(body(C['Reply type']), s, color))));
  sh.getRange(1, 1, sh.getMaxRows(), HIDDEN - 1).createFilter();
  const summary = ss.getSheetByName('Summary');
  if (summary) ss.deleteSheet(summary);
  dashboard_();
}

function dashboard_() {
  const ss = ss_();
  const d = ss.getSheetByName(DASH) || ss.insertSheet(DASH, 0);
  d.getCharts().forEach(ch => d.removeChart(ch));
  d.clear();
  const col = h => `'${TAB}'!${letter_(C[h])}2:${letter_(C[h])}`;
  const crit = `IF($B$1="All","*",$B$1)`;
  const count = (...pairs) => `COUNTIFS(${col('Campaign')},${crit}${pairs.map(([h, v]) => `,${col(h)},${v}`).join('')})`;
  const pct = (a, b) => `=IFERROR(${a}/${b},0)`;

  d.getRange('A1:B1').setValues([['Campaign', 'All']]).setFontWeight('bold').setFontSize(12);
  d.getRange('B1').setBackground('#e8f0fe').setDataValidation(SpreadsheetApp.newDataValidation()
    .requireValueInList(['All', 'Web Summit 2026', 'Job boards'], true).build());
  d.getRange('C1').setValue('← pick a campaign').setFontColor('#666666');
  d.getRange('E1').setFontColor('#666666');

  // metrics: [label, formula, is a rate]
  const M = [
    ['Leads', `=${count()}`],
    ['Invites sent', `=${count(['Invited', '"<>"'])}`],
    ['Accepted', `=${count(['Accepted', '"<>"'])}`],
    ['Acceptance rate', pct('B6', 'B5'), true],
    ['Replied', `=${count(['Replied', '"<>"'])}`],
    ['Reply rate (of accepted)', pct('B8', 'B6'), true],
    ['Positive replies', `=${count(['Reply type', '"Positive"'])}`],
    ['Neutral replies', `=${count(['Reply type', '"Neutral"'])}`],
    ['Negative replies', `=${count(['Reply type', '"Negative"'])}`],
    ['Positive reply rate (of replies)', pct('B10', 'B8'), true],
    ['Negative reply rate (of replies)', pct('B12', 'B8'), true],
    ['Meetings booked', `=${count(['Status', '"Meeting booked"'])}+${count(['Status', '"Won"'])}`],
    ['Meeting rate (of accepted)', pct('B15', 'B6'), true],
    ['Won', `=${count(['Status', '"Won"'])}`],
    ['Lost / not relevant', `=${count(['Status', '"Lost"'])}+${count(['Status', '"Not relevant"'])}`],
  ];
  d.getRange('A3:B3').setValues([['Metric', 'Value']]).setFontWeight('bold').setBackground('#1f2937').setFontColor('#ffffff');
  d.getRange(4, 1, M.length, 2).setValues(M.map(m => [m[0], m[1]]));
  M.forEach((m, i) => d.getRange(4 + i, 2).setNumberFormat(m[2] ? '0.0%' : '0'));
  M.forEach((m, i) => { if (m[2]) d.getRange(4 + i, 1, 1, 2).setBackground('#f1f5f9').setFontWeight('bold'); });

  // funnel and reply mix, for the charts
  d.getRange('D3:E3').setValues([['Funnel', 'People']]).setFontWeight('bold').setBackground('#1f2937').setFontColor('#ffffff');
  d.getRange('D4:E8').setValues([['Invited', '=B5'], ['Accepted', '=B6'], ['Replied', '=B8'], ['Positive', '=B10'],
                                 ['Meeting booked', '=B15']]);
  d.getRange('D11:E11').setValues([['Reply type', 'Replies']]).setFontWeight('bold').setBackground('#1f2937').setFontColor('#ffffff');
  d.getRange('D12:E14').setValues([['Positive', '=B10'], ['Neutral', '=B11'], ['Negative', '=B12']]);

  // the last 8 weeks, Monday to Sunday
  d.getRange('G3:J3').setValues([['Week of', 'Invites', 'Accepted', 'Replies']]).setFontWeight('bold')
    .setBackground('#1f2937').setFontColor('#ffffff');
  const weeks = [];
  for (let k = 7; k >= 0; k--) {
    const r = 11 - k;
    const span = h => [[h, `">="&$G${r}`], [h, `"<"&($G${r}+7)`]];
    weeks.push([`=TODAY()-WEEKDAY(TODAY(),3)-${7 * k}`, `=${count(...span('Invited'))}`,
                `=${count(...span('Accepted'))}`, `=${count(...span('Replied'))}`]);
  }
  d.getRange('G4:J11').setValues(weeks);
  d.getRange('G4:G11').setNumberFormat('dd MMM');
  [[1, 230], [2, 80], [3, 20], [4, 120], [5, 70], [6, 20], [7, 80], [8, 70], [9, 75], [10, 70]]
    .forEach(([c, w]) => d.setColumnWidth(c, w));

  const chart = (type, range, row, colNum, title, opts) => {
    let b = d.newChart().setChartType(type).addRange(d.getRange(range)).setNumHeaders(1).setPosition(row, colNum, 0, 0)
      .setOption('title', title).setOption('width', 470).setOption('height', 290).setOption('legend', {position: 'none'});
    Object.entries(opts || {}).forEach(([k, v]) => { b = b.setOption(k, v); });
    d.insertChart(b.build());
  };
  chart(Charts.ChartType.BAR, 'D3:E8', 20, 1, 'Funnel', {colors: ['#2a78d6']});
  chart(Charts.ChartType.PIE, 'D11:E14', 20, 5, 'Reply type',
        {legend: {position: 'right'}, colors: ['#57bb8a', '#bdbdbd', '#e06666'], pieHole: 0.45});
  chart(Charts.ChartType.COLUMN, 'G3:J11', 36, 1, 'Weekly: invites, accepted, replies',
        {legend: {position: 'bottom'}, colors: ['#9aa9bd', '#2a78d6', '#57bb8a'], width: 960});
  d.setFrozenRows(1);
}
