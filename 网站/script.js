/* 财神助手 · 站点脚本（原生 JS，无依赖）
 *
 * 做两件事：
 *   1) 读 downloads/latest.json（当前版本 + 包文件名），把下载按钮指向**带版本号**的包；
 *      读不到就退回老约定 downloads/CaishenTrader.zip（老站点、老包名照样能用）。
 *   2) HEAD 探测该文件在不在：在 → 按钮生效，并显示文件大小与版本号；
 *      不在 → 按钮置灰、显示「下载准备中，请联系作者微信 q352162」。
 *
 * 为什么要探测而不是写死链接：仓库是私密的，GitHub 的下载地址对外无效；
 * 发布时只要把打包好的 zip 丢进 downloads/、把 latest.json 改一行，按钮自己就生效，
 * 不会出现「点了 404」的死按钮。
 */
(function () {
  var MANIFEST = 'downloads/latest.json';
  var FALLBACK = 'downloads/CaishenTrader.zip';

  function human(bytes) {
    if (!bytes || bytes < 1024) return bytes + ' B';
    var mb = bytes / 1048576;
    if (mb >= 1) return mb.toFixed(1) + ' MB';
    return (bytes / 1024).toFixed(0) + ' KB';
  }

  function markPending() {
    document.querySelectorAll('[data-download]').forEach(function (a) {
      a.classList.add('is-pending');
      a.removeAttribute('href');
      a.setAttribute('aria-disabled', 'true');
      a.textContent = '下载准备中';
      a.addEventListener('click', function (e) { e.preventDefault(); });
    });
    document.querySelectorAll('[data-pending]').forEach(function (el) { el.hidden = false; });
  }

  function markReady(size, version) {
    document.querySelectorAll('[data-size]').forEach(function (el) {
      el.textContent = size ? '（' + human(size) + '）' : '';
    });
    if (version) {
      document.querySelectorAll('[data-version]').forEach(function (el) {
        el.textContent = 'v' + version;
      });
    }
  }

  function applyHref(url) {
    document.querySelectorAll('[data-download]').forEach(function (a) { a.setAttribute('href', url); });
  }

  function probe(url, version) {
    return fetch(url, { method: 'HEAD', cache: 'no-store' }).then(function (r) {
      if (!r.ok) return false;
      var len = parseInt(r.headers.get('content-length') || '0', 10);
      markReady(len, version);
      return true;
    });
  }

  function run() {
    if (!window.fetch) { markPending(); return; }
    fetch(MANIFEST, { cache: 'no-store' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .catch(function () { return null; })
      .then(function (man) {
        var url = FALLBACK, version = '';
        if (man && man.file) { url = 'downloads/' + man.file; version = man.version || ''; }
        applyHref(url);
        return probe(url, version).then(function (ok) { if (!ok) markPending(); });
      })
      .catch(function () { markPending(); });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', run);
  } else {
    run();
  }
})();
