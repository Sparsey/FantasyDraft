// ==UserScript==
// @name         FantasyDraft — ESPN draft room capture
// @namespace    fantasydraft
// @version      0.1
// @description  Forwards ESPN draft-room traffic and page text to the local FantasyDraft capture server.
// @match        https://*.espn.com/*
// @run-at       document-start
// @grant        GM_xmlhttpRequest
// @grant        unsafeWindow
// @connect      127.0.0.1
// @connect      localhost
// ==/UserScript==

(function () {
  'use strict';
  // Only act on draft pages (the draft room and mock drafts).
  if (!/draft/i.test(location.href)) return;

  var SERVER = 'http://127.0.0.1:8765/log';
  var queue = [];
  var page = unsafeWindow;

  function record(kind, extra) {
    var e = {kind: kind, t: Date.now(), page: location.href};
    for (var k in extra) e[k] = extra[k];
    queue.push(e);
  }

  function flush() {
    if (!queue.length) return;
    var batch = queue;
    queue = [];
    GM_xmlhttpRequest({
      method: 'POST', url: SERVER, data: JSON.stringify(batch),
      headers: {'Content-Type': 'application/json'},
      onerror: function () { queue = batch.concat(queue).slice(-5000); }  // server not running yet: keep, retry
    });
  }
  setInterval(flush, 1000);

  // 1. WebSocket traffic: the live draft feed (picks, clock, autodraft changes).
  var NativeWS = page.WebSocket;
  function WrappedWS(url, protocols) {
    var ws = protocols === undefined ? new NativeWS(url) : new NativeWS(url, protocols);
    record('ws-open', {url: String(url)});
    ws.addEventListener('message', function (ev) {
      record('ws-in', {url: String(url), data: typeof ev.data === 'string' ? ev.data : '[binary ' + (ev.data.size || ev.data.byteLength) + ' bytes]'});
    });
    ws.addEventListener('close', function (ev) { record('ws-close', {url: String(url), code: ev.code}); });
    var nativeSend = ws.send;
    ws.send = function (data) {
      record('ws-out', {url: String(url), data: typeof data === 'string' ? data : '[binary]'});
      return nativeSend.apply(ws, arguments);
    };
    return ws;
  }
  WrappedWS.prototype = NativeWS.prototype;
  ['CONNECTING', 'OPEN', 'CLOSING', 'CLOSED'].forEach(function (k) { WrappedWS[k] = NativeWS[k]; });
  page.WebSocket = WrappedWS;

  // 2. ESPN API responses the draft page loads (players, league, draft state).
  var nativeFetch = page.fetch;
  page.fetch = function (input) {
    var url = typeof input === 'string' ? input : (input && input.url) || '';
    return nativeFetch.apply(this, arguments).then(function (resp) {
      if (/fantasy\.espn\.com|lm-api/.test(url)) {
        resp.clone().text().then(function (body) {
          record('fetch', {url: url, status: resp.status, data: body.slice(0, 2000000)});
        }).catch(function () {});
      }
      return resp;
    });
  };

  // 3. Page text every 10 s, as a fallback source for picks and autodraft status.
  setInterval(function () {
    if (document.body) record('dom-text', {data: document.body.innerText.slice(0, 300000)});
  }, 10000);

  record('hello', {url: location.href, ua: navigator.userAgent});
})();
