/* status-badge.js v2 — GPU / CPU 资源指示按钮(悬停弹窗详情)
 * 用法:<script src="/static/status-badge.js?v=2" data-src="/api/host/status" data-kind="gpu|cpu|both"></script>
 * 交互:按钮常显概要(GPU x% · CPU x%),鼠标悬停弹出详情卡(各 GPU 利用率/显存、CPU 负载),移开即消失。
 * 每 10s 刷新;接口失败时按钮降级为「资源状态未知」。暖色样式。
 */
(function () {
  'use strict';
  var me = document.currentScript;
  if (!me) return;
  var src = me.getAttribute('data-src');
  if (!src) return;
  var kind = me.getAttribute('data-kind') || 'both';

  var pill = document.createElement('span');
  pill.style.cssText =
    'display:inline-block;padding:1px 10px;border-radius:999px;cursor:default;white-space:nowrap;' +
    'background:rgba(224,190,142,.13);border:1px solid rgba(224,190,142,.35);' +
    'color:var(--warn, var(--amber, #e8b04b));transition:background .2s;';
  pill.textContent = '资源…';

  var card = document.createElement('div');
  card.style.cssText =
    'position:fixed;z-index:9999;display:none;min-width:250px;max-width:330px;padding:9px 12px;' +
    'border-radius:10px;font-size:12px;line-height:1.8;text-align:left;' +
    'background:rgba(42,34,22,.96);border:1px solid rgba(224,190,142,.4);' +
    'box-shadow:0 6px 24px rgba(0,0,0,.35);pointer-events:none;color:var(--ink, #f5efe4);';
  document.body.appendChild(card);

  var hideTimer = null;
  function showCard() {
    var r = pill.getBoundingClientRect();
    card.style.display = 'block';
    var top = r.bottom + 8;
    if (top + card.offsetHeight > innerHeight - 8) top = r.top - card.offsetHeight - 8;
    card.style.left = Math.max(8, Math.min(r.left, innerWidth - card.offsetWidth - 8)) + 'px';
    card.style.top = Math.max(8, top) + 'px';
    tick();
  }
  pill.addEventListener('mouseenter', function () {
    clearTimeout(hideTimer);
    showCard();
  });
  pill.addEventListener('mouseleave', function () {
    hideTimer = setTimeout(function () { card.style.display = 'none'; }, 120);
  });

  var host = document.querySelector('.banner-meta');
  if (host) {
    host.appendChild(document.createTextNode(' · '));
    host.appendChild(pill);
  } else {
    pill.style.cssText += 'position:fixed;top:10px;right:14px;z-index:9998;';
    document.body.appendChild(pill);
  }

  function gb(n) { n = n / 1024; return n >= 10 ? n.toFixed(0) : n.toFixed(1); }
  var lastData = null, lastTs = '';

  function render(d) {
    lastData = d;
    lastTs = new Date().toTimeString().slice(0, 8);
    var compact = [], lines = [], hot = false;
    if (d.gpus && d.gpus.length) {
      var u = 0, mu = 0, mt = 0;
      d.gpus.forEach(function (g) {
        u = Math.max(u, g.util || 0);
        mu += g.mem_used || 0; mt += g.mem_total || 0;
        lines.push('<b>GPU' + g.idx + '</b> ' + esc(g.name || '') +
          '<br>　利用率 ' + Math.round(g.util || 0) + '% · 显存 ' +
          gb(g.mem_used || 0) + '/' + gb(g.mem_total || 0) + 'G');
      });
      compact.push('GPU ' + Math.round(u) + '%');
      if (u > 85 || (mt && mu / mt > 0.9)) hot = true;
    }
    if (d.cpu && typeof d.cpu.percent === 'number') {
      compact.push('CPU ' + Math.round(d.cpu.percent) + '%');
      lines.push('<b>CPU</b> 负载 ' + d.cpu.load1 + ' / ' + d.cpu.cores + ' 核 (' + d.cpu.percent + '%)');
      if (d.cpu.percent > 85) hot = true;
    }
    pill.textContent = compact.length ? compact.join(' · ') : '资源状态未知';
    pill.style.background = hot ? 'rgba(232,120,60,.22)' : 'rgba(224,190,142,.13)';
    if (d.updated !== undefined || lines.length) {
      lines.push('<span style="opacity:.55">更新于 ' + lastTs + '</span>');
    }
    card.innerHTML = lines.length ? lines.join('<br>') : '暂时拿不到资源数据。';
  }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }

  function tick() {
    fetch(src, { cache: 'no-store' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { render(d || {}); })
      .catch(function () {
        pill.textContent = '资源状态未知';
        card.innerHTML = '暂时拿不到资源数据(接口无响应)。';
      });
  }
  tick();
  setInterval(tick, 10000);
})();
