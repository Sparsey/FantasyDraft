/**
 * FantasyDraft — on-demand ESPN update for the players sheet.
 *
 * Adds a "Draft Tools" menu to the spreadsheet. Nothing runs automatically:
 * the sheet only changes when you click "Update players from ESPN".
 *
 * What an update does (on the "players" tab, or the first tab if there is none):
 *   - fills/refreshes the ESPN columns: id, name, positions, nba_team,
 *     proj_points, espn_rank, adp  (matched to existing rows by id)
 *   - appends rows for players not in the sheet yet
 *   - never touches any other column (my_proj, ignore, notes, formulas, ...)
 *   - never deletes or reorders rows
 *
 * League id / season / cookies are stored in *your* Apps Script user
 * properties, not in the sheet, so they stay private even though the sheet
 * is shared publicly.
 *
 * Setup: Extensions -> Apps Script, paste this file, save, reload the sheet.
 */

var ESPN_COLUMNS = ['id', 'name', 'positions', 'nba_team', 'proj_points', 'espn_rank', 'adp'];
var DEFAULT_HEADER = ESPN_COLUMNS.concat(['my_proj', 'ignore', 'notes']);

// eligibleSlots ids that are real positions (0=PG ... 4=C).
var SLOT_POSITIONS = {0: 'PG', 1: 'SG', 2: 'SF', 3: 'PF', 4: 'C'};
var NBA_TEAMS = {
  0: 'FA', 1: 'ATL', 2: 'BOS', 3: 'NOP', 4: 'CHI', 5: 'CLE', 6: 'DAL', 7: 'DEN',
  8: 'DET', 9: 'GSW', 10: 'HOU', 11: 'IND', 12: 'LAC', 13: 'LAL', 14: 'MIA',
  15: 'MIL', 16: 'MIN', 17: 'BKN', 18: 'NYK', 19: 'ORL', 20: 'PHI', 21: 'PHX',
  22: 'POR', 23: 'SAC', 24: 'SAS', 25: 'OKC', 26: 'UTA', 27: 'WAS', 28: 'TOR',
  29: 'MEM', 30: 'CHA'
};

function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu('Draft Tools')
    .addItem('Update players from ESPN', 'updatePlayers')
    .addItem('Settings (league, season, login)…', 'configure')
    .addToUi();
}

function getSettings_() {
  var p = PropertiesService.getUserProperties().getProperties();
  return {
    leagueId: p.leagueId || '',
    season: p.season || '',
    espnS2: p.espnS2 || '',
    swid: p.swid || '',
    rankType: p.rankType || 'STANDARD',
    limit: Number(p.limit || 400)
  };
}

function configure() {
  var ui = SpreadsheetApp.getUi();
  var s = getSettings_();
  var fields = [
    ['leagueId', 'ESPN league id (the number after leagueId= in your league URL)', s.leagueId],
    ['season', 'Season year — the year the season ENDS (2026-27 season = 2027)', s.season],
    ['espnS2', 'espn_s2 cookie — private leagues only, leave blank if public', s.espnS2 ? '(saved)' : ''],
    ['swid', 'SWID cookie, including { } — private leagues only', s.swid ? '(saved)' : ''],
    ['rankType', 'ESPN rank list for espn_rank (usually STANDARD)', s.rankType],
    ['limit', 'How many players to pull', String(s.limit)]
  ];
  var props = PropertiesService.getUserProperties();
  for (var i = 0; i < fields.length; i++) {
    var key = fields[i][0], label = fields[i][1], current = fields[i][2];
    var r = ui.prompt('Draft Tools settings', label + '\nCurrent: ' + (current || '(empty)') +
                      '\n\nLeave blank to keep the current value.', ui.ButtonSet.OK_CANCEL);
    if (r.getSelectedButton() !== ui.Button.OK) return;
    var v = r.getResponseText().trim();
    if (v) props.setProperty(key, v);
  }
  ui.alert('Saved. Run Draft Tools → Update players from ESPN when you are ready.');
}

function fetchEspnPlayers_(s) {
  var url = 'https://lm-api-reads.fantasy.espn.com/apis/v3/games/fba/seasons/' + s.season +
            '/segments/0/leagues/' + s.leagueId + '?view=kona_player_info';
  var filter = {players: {limit: s.limit,
                          sortDraftRanks: {sortPriority: 100, sortAsc: true, value: s.rankType}}};
  var headers = {'X-Fantasy-Filter': JSON.stringify(filter), 'Accept': 'application/json'};
  if (s.espnS2 && s.swid) headers['Cookie'] = 'espn_s2=' + s.espnS2 + '; SWID=' + s.swid;
  var resp = UrlFetchApp.fetch(url, {headers: headers, muteHttpExceptions: true});
  var code = resp.getResponseCode();
  if (code === 401 || code === 403) {
    throw new Error('ESPN refused access (' + code + '). Private league? Set espn_s2 and SWID in Settings ' +
                    '(they expire — copy fresh ones from your browser if this worked before).');
  }
  if (code !== 200) throw new Error('ESPN returned HTTP ' + code + ': ' + resp.getContentText().slice(0, 300));
  return JSON.parse(resp.getContentText()).players || [];
}

function parsePlayer_(raw, season, rankType) {
  var info = raw.player || raw;
  var pos = [];
  (info.eligibleSlots || []).forEach(function (slot) {
    if (SLOT_POSITIONS[slot] && pos.indexOf(SLOT_POSITIONS[slot]) < 0) pos.push(SLOT_POSITIONS[slot]);
  });
  if (!pos.length) return null;
  var proj = '';
  (info.stats || []).forEach(function (st) {
    if (proj === '' && st.statSourceId === 1 && st.statSplitTypeId === 0 && String(st.seasonId) === String(season)) {
      proj = Math.round((st.appliedTotal || 0) * 10) / 10;
    }
  });
  var ranks = info.draftRanksByRankType || {};
  var rank = ranks[rankType] && ranks[rankType].rank;
  var adp = info.ownership && info.ownership.averageDraftPosition;
  return {
    id: String(info.id || raw.id),
    name: info.fullName || '?',
    positions: pos.join('/'),
    nba_team: NBA_TEAMS.hasOwnProperty(info.proTeamId) ? NBA_TEAMS[info.proTeamId] : String(info.proTeamId || ''),
    proj_points: proj,
    espn_rank: rank || '',
    adp: adp ? Math.round(adp * 10) / 10 : ''
  };
}

function playersSheet_() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  return ss.getSheetByName('players') || ss.getSheets()[0];
}

function updatePlayers() {
  var s = getSettings_();
  if (!s.leagueId || !s.season) {
    SpreadsheetApp.getUi().alert('Set your league id and season first: Draft Tools → Settings.');
    return;
  }
  var players = fetchEspnPlayers_(s)
    .map(function (raw) { return parsePlayer_(raw, s.season, s.rankType); })
    .filter(function (p) { return p; });

  var sheet = playersSheet_();
  if (sheet.getLastRow() === 0) {
    sheet.getRange(1, 1, 1, DEFAULT_HEADER.length).setValues([DEFAULT_HEADER]);
    sheet.setFrozenRows(1);
  }

  // Locate (or add) each ESPN column by its header.
  var lastCol = sheet.getLastColumn();
  var header = sheet.getRange(1, 1, 1, lastCol).getValues()[0]
    .map(function (h) { return String(h).trim().toLowerCase(); });
  var col = {};
  ESPN_COLUMNS.forEach(function (name) {
    var idx = header.indexOf(name);
    if (idx < 0) {
      header.push(name);
      idx = header.length - 1;
      sheet.getRange(1, idx + 1).setValue(name);
    }
    col[name] = idx;
  });

  // Existing rows, matched by id.
  var nRows = Math.max(sheet.getLastRow() - 1, 0);
  var columns = {};
  ESPN_COLUMNS.forEach(function (name) {
    columns[name] = nRows ? sheet.getRange(2, col[name] + 1, nRows, 1).getValues().map(function (r) { return r[0]; }) : [];
  });
  var rowById = {};
  columns.id.forEach(function (id, i) { if (id !== '') rowById[String(id)] = i; });

  var updated = 0, added = 0;
  players.forEach(function (p) {
    var i = rowById[p.id];
    if (i === undefined) {
      i = columns.id.length;
      ESPN_COLUMNS.forEach(function (name) { columns[name].push(''); });
      rowById[p.id] = i;
      added++;
    } else {
      updated++;
    }
    ESPN_COLUMNS.forEach(function (name) { columns[name][i] = p[name]; });
  });

  // Write back only the ESPN columns, so your own columns and formulas are untouched.
  var total = columns.id.length;
  if (total) {
    ESPN_COLUMNS.forEach(function (name) {
      sheet.getRange(2, col[name] + 1, total, 1).setValues(columns[name].map(function (v) { return [v]; }));
    });
  }
  var stamp = Utilities.formatDate(new Date(), Session.getScriptTimeZone(), 'yyyy-MM-dd HH:mm');
  sheet.getRange(1, col.id + 1).setNote('Last ESPN update: ' + stamp + ' (' + s.rankType + ' ranks, season ' + s.season + ')');
  SpreadsheetApp.getActiveSpreadsheet().toast(updated + ' players updated, ' + added + ' added.', 'ESPN update', 8);
}
