/* tab-fill.js — 按 Tab 一键填入输入框的推荐内容(placeholder 示例值)
 * 规则:
 *   · 空输入框(text/number 等)与多行框上按 Tab:若推荐内容可识别 → 填入并停留本框;再按 Tab 正常跳到下一项
 *   · Shift+Tab(后退)、已填内容、纯说明性 placeholder 一律不干预
 *   · 推荐值识别:data-tabfill 属性优先,否则解析「例如:/如/比如 …」形式的 placeholder
 *   · 数字输入框只接受纯数字推荐(如 "10"、"1.5";"0 或 1" 不填)
 *   · 括号注释会被去掉:如 rf,mpnn(跳过验证) → rf,mpnn;多个备选取第一个:A142V 或 ALA142VAL → A142V
 */
(function () {
  'use strict';

  var FILLABLE_TYPES = { text: 1, search: 1, url: 1, tel: 1, email: 1, number: 1 };
  var HINT = '按 Tab 可一键填入推荐内容;Esc 可撤销';

  function tr(s) { return (s || '').replace(/\u3000/g, ' ').trim(); }

  // 从 placeholder 提炼可填入的推荐值;不是示例则返回 null
  function recommend(input) {
    var attr = tr(input.getAttribute('data-tabfill'));
    if (attr) return finalClean(attr, input);
    var p = tr(input.getAttribute('placeholder') || '');
    if (!p) return null;
    if (/^-?\d+(\.\d+)?$/.test(p)) return finalClean(p, input);              // 纯数字推荐(如 10 / 1.5)
    var m = p.match(/^(?:例如|比如|如|e\.?g\.?)[\s:：,，]?\s*(.+)$/);          // 前缀式:例如: xxx
    if (!m) m = p.match(/[，,;；]\s*(?:例如|比如|如)\s*[:：]?\s*(.+)$/);      // 后缀式:xxx,如: yyy
    if (!m) return null;
    return finalClean(m[1], input);
  }

  function finalClean(raw, input) {
    var rec = tr(raw);
    rec = rec.replace(/[（(][^（）()]*[)）]\s*$/, '').trim();   // 去尾部括号注释
    var t = (input.getAttribute('type') || 'text').toLowerCase();
    if (t === 'number') {                                       // 数字框:整体必须是纯数字
      return /^-?\d+(\.\d+)?$/.test(rec) ? rec : null;
    }
    var cut = rec.search(/或(者)?/);                            // 多个备选取第一个
    if (cut > 0) rec = tr(rec.slice(0, cut));
    rec = rec.replace(/[。…\s]+$/, '').trim();
    return rec || null;
  }

  function fillable(input) {
    if (!input) return false;
    var tag = input.tagName;
    if (tag !== 'INPUT' && tag !== 'TEXTAREA') return false;   // textarea 同样支持(其 Tab 不用于缩进)
    var t = (input.getAttribute('type') || 'text').toLowerCase();
    if (!FILLABLE_TYPES[t]) return false;
    return !(input.disabled || input.readOnly);
  }

  // 聚焦可填空框时给个悬停提示
  var prevTitle = null;
  document.addEventListener('focusin', function (e) {
    var el = e.target;
    if (!fillable(el) || tr(el.value) || !recommend(el)) return;
    prevTitle = el.getAttribute('title');
    el.setAttribute('title', HINT);
  });
  document.addEventListener('focusout', function (e) {
    var el = e.target;
    if (el && el.tagName === 'INPUT' && prevTitle !== null) {
      if (el.getAttribute('title') === HINT) {
        if (prevTitle === null) el.removeAttribute('title'); else el.setAttribute('title', prevTitle);
      }
      prevTitle = null;
    }
  });

  // Tab 填入
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Tab' || e.shiftKey || e.ctrlKey || e.altKey || e.metaKey || e.isComposing) return;
    var el = e.target;
    if (!fillable(el) || tr(el.value)) return;   // 有内容 → 保持原生 Tab 跳转
    var rec = recommend(el);
    if (!rec) return;                            // 无推荐 → 保持原生 Tab 跳转
    el.value = rec;
    el.__tabfillVal = rec;
    try { el.dispatchEvent(new Event('input', { bubbles: true })); } catch (err) {}
    el.classList.add('tabfill-flash');
    setTimeout(function () { el.classList.remove('tabfill-flash'); }, 900);
    e.preventDefault();                          // 填入后停留本框;再按 Tab 自然跳下一项
  }, true);

  // Esc 撤销刚填入的推荐
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape' || e.isComposing) return;
    var el = e.target;
    if (!fillable(el) || !el.__tabfillVal) return;
    if (el.value === el.__tabfillVal) {
      el.value = '';
      el.__tabfillVal = null;
      try { el.dispatchEvent(new Event('input', { bubbles: true })); } catch (err) {}
    }
  });

  // 填入时的绿色闪光样式
  var css = document.createElement('style');
  css.textContent =
    '.tabfill-flash{animation:tabfillGlow .9s ease;}@keyframes tabfillGlow{' +
    '0%{box-shadow:0 0 0 3px rgba(64,196,124,.5);}100%{box-shadow:none;}}';
  document.head.appendChild(css);
})();
