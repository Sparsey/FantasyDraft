// ==UserScript==
// @name         FantasyDraft — ESPN draft room capture
// @namespace    fantasydraft
// @version      0.3
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

  // Small status badge in the corner so you can see the script is alive.
  var sent = 0, wsCount = 0, badge = null;
  function status(text, ok) {
    if (!document.body) return;
    if (!badge) {
      badge = document.createElement('div');
      badge.style.cssText = 'position:fixed;bottom:6px;left:6px;z-index:2147483647;padding:3px 7px;' +
        'font:12px/1.3 monospace;border-radius:4px;color:#fff;pointer-events:none;opacity:.85';
      document.body.appendChild(badge);
    }
    badge.style.background = ok ? '#1a7f37' : '#b42318';
    badge.textContent = 'FantasyDraft capture: ' + text;
  }

  function flush() {
    if (!queue.length) {
      if (!sent) status('running, nothing sent yet', true);
      return;
    }
    var batch = queue;
    queue = [];
    function fail(why) {
      queue = batch.concat(queue).slice(-5000);  // keep and retry
      status(why + ' — is "python -m fantasydraft capture" running?', false);
    }
    GM_xmlhttpRequest({
      method: 'POST', url: SERVER, data: JSON.stringify(batch),
      headers: {'Content-Type': 'application/json'},
      onload: function (r) {
        if (r.status >= 200 && r.status < 300) {
          sent += batch.length;
          status('sent ' + sent + ' events (' + wsCount + ' live-feed messages)', true);
        } else {
          fail('server replied ' + r.status);
        }
      },
      onerror: function () { fail('cannot reach 127.0.0.1:8765'); },
      ontimeout: function () { fail('timed out'); },
      timeout: 5000
    });
  }
  setInterval(flush, 1000);

  // 1. WebSocket traffic: the live draft feed (picks, clock, autodraft changes).
  var NativeWS = page.WebSocket;
  function WrappedWS(url, protocols) {
    var ws = protocols === undefined ? new NativeWS(url) : new NativeWS(url, protocols);
    record('ws-open', {url: String(url)});
    ws.addEventListener('message', function (ev) {
      wsCount++;
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

  // 3. Page text every 10 s (and soon after load), used to resync when you
  //    join a draft that's already under way.
  function snapshot() {
    if (document.body) record('dom-text', {data: document.body.innerText.slice(0, 300000)});
  }
  setTimeout(snapshot, 4000);
  setInterval(snapshot, 10000);

  record('hello', {url: location.href, ua: navigator.userAgent});
})();
