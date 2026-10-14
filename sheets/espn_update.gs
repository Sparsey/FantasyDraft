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
 * Before your league is activated for the new season, the update falls back
 * to ESPN's default league (ranks, ADP, default-scoring projections) and says
 * so; run it again after activation to get your league's scoring.
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

/** Accepts a bare league id or any ESPN league URL ("...leagueId=358709800&seasonId=2027..."). */
function parseLeagueId_(text) {
  text = String(text || '').trim();
  var m = text.match(/leagueId=(\d+)/i);
  if (m) return m[1];
  return /^\d+$/.test(text) ? text : '';
}

function parseSeason_(text) {
  var m = String(text || '').match(/seasonId=(\d{4})/i) || String(text || '').match(/^\s*(\d{4})\s*$/);
  return m ? m[1] : '';
}

function getSettings_() {
  var p = PropertiesService.getUserProperties().getProperties();
  return {
    leagueId: parseLeagueId_(p.leagueId),
    // A pasted league URL also carries the season.
    season: parseSeason_(p.season) || parseSeason_(p.leagueId),
    espnS2: (p.espnS2 || '').trim(),
    swid: (p.swid || '').trim(),
    rankType: p.rankType || 'STANDARD',
    limit: Number(p.limit || 400)
  };
}

function configure() {
  var ui = SpreadsheetApp.getUi();
  var s = getSettings_();
  var fields = [
    ['leagueId', 'ESPN league id — the number after leagueId= in your league URL (pasting the whole URL works too)', s.leagueId],
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
    if (!v) continue;
    if (key === 'leagueId') {
      if (!parseLeagueId_(v)) {
        ui.alert('"' + v + '" is not a league id. It should be a number like 358709800, or your league URL.');
        return;
      }
      if (parseSeason_(v)) props.setProperty('season', parseSeason_(v));
      v = parseLeagueId_(v);
    }
    if (key === 'season' && !parseSeason_(v)) {
      ui.alert('"' + v + '" is not a season. Use the 4-digit year the season ends, e.g. 2027.');
      return;
    }
    props.setProperty(key, v);
  }
  ui.alert('Saved. Run Draft Tools → Update players from ESPN when you are ready.');
}

function espnGet_(path, s, useCookies) {
  var url = 'https://lm-api-reads.fantasy.espn.com/apis/v3/games/fba/seasons/' + s.season + path +
            '?view=kona_player_info';
  var filter = {players: {limit: s.limit,
                          sortDraftRanks: {sortPriority: 100, sortAsc: true, value: s.rankType}}};
  var headers = {'X-Fantasy-Filter': JSON.stringify(filter), 'Accept': 'application/json'};
  if (useCookies && s.espnS2 && s.swid) headers['Cookie'] = 'espn_s2=' + s.espnS2 + '; SWID=' + s.swid;
  return UrlFetchApp.fetch(url, {headers: headers, muteHttpExceptions: true});
}

/**
 * Players with projections scored by your league's settings. If the league
 * can't be read for this season (not activated/renewed yet, or bad cookies),
 * falls back to ESPN's default league so you still get ranks, ADP and
 * default-scoring projections. Returns {players, source, warning}.
 */
function fetchEspnPlayers_(s) {
  var resp = espnGet_('/segments/0/leagues/' + s.leagueId, s, true);
  var code = resp.getResponseCode();
  if (code === 200) {
    return {players: JSON.parse(resp.getContentText()).players || [], source: 'league ' + s.leagueId, warning: ''};
  }
  var why;
  if (code === 401 || code === 403) {
    why = s.espnS2 && s.swid
      ? 'ESPN refused access (' + code + '). The league may not be activated for ' + s.season +
        ' yet, or your espn_s2/SWID cookies have expired (copy fresh ones into Settings).'
      : 'ESPN refused access (' + code + '). Private league: set espn_s2 and SWID in Settings.';
  } else if (code === 404) {
    why = 'League ' + s.leagueId + ' not found for season ' + s.season + ' (not activated/renewed yet?).';
  } else {
    why = 'League request failed with HTTP ' + code + '.';
  }
  var fallback = espnGet_('/segments/0/leaguedefaults/1', s, false);
  if (fallback.getResponseCode() !== 200) {
    throw new Error(why + ' The ESPN default-league fallback also failed (HTTP ' + fallback.getResponseCode() + ').');
  }
  return {
    players: JSON.parse(fallback.getContentText()).players || [],
    source: 'ESPN default scoring',
    warning: why + ' Using ESPN default-league projections for now; run Update again once your league works.'
  };
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
  try {
    updatePlayers_();
  } catch (e) {
    SpreadsheetApp.getUi().alert('ESPN update failed:\n\n' + (e && e.message ? e.message : e));
  }
}

function updatePlayers_() {
  var s = getSettings_();
  if (!s.leagueId || !s.season) {
    SpreadsheetApp.getUi().alert('League id or season missing/invalid (league id: "' + s.leagueId +
                                 '", season: "' + s.season + '"). Fix them in Draft Tools → Settings.');
    return;
  }
  var fetched = fetchEspnPlayers_(s);
  // Which ESPN rank lists exist, and how the chosen one compares with
  // projection order (identical order suggests it's just sorted by projections).
  var rankTypes = {};
  fetched.players.forEach(function (raw) {
    Object.keys((raw.player || raw).draftRanksByRankType || {}).forEach(function (k) { rankTypes[k] = true; });
  });
  var players = fetched.players
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
  var byProj = players.filter(function (p) { return p.proj_points !== '' && p.espn_rank !== ''; })
    .sort(function (a, b) { return b.proj_points - a.proj_points; }).slice(0, 150);
  var byRank = byProj.slice().sort(function (a, b) { return a.espn_rank - b.espn_rank; });
  var moved = 0;
  byProj.forEach(function (p, i) { if (Math.abs(byRank.indexOf(p) - i) >= 5) moved++; });
  var rankNote = 'Rank lists ESPN provides: ' + (Object.keys(rankTypes).join(', ') || 'none') +
                 '\nUsing ' + s.rankType + ': ' + (byProj.length ?
                   moved + ' of the top ' + byProj.length + ' by projection are 5+ spots away from their ' + s.rankType + ' rank' +
                   (moved === 0 ? ' (same order as projections)' : '') : 'no ranks found');
  sheet.getRange(1, col.id + 1).setNote('Last ESPN update: ' + stamp + '\nProjections: ' + fetched.source +
                                        '\n' + rankNote + '\nSeason ' + s.season +
                                        (fetched.warning ? '\n\n' + fetched.warning : ''));
  var summary = updated + ' players updated, ' + added + ' added (projections: ' + fetched.source + ').\n' + rankNote;
  if (fetched.warning) {
    SpreadsheetApp.getUi().alert(summary + '\n\n' + fetched.warning);
  } else {
    SpreadsheetApp.getActiveSpreadsheet().toast(summary, 'ESPN update', 8);
  }
}
