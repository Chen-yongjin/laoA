/* 老牛选股 · 站点脚本（原生 JS，无依赖）
 *
 * 只做一件事：检查 downloads/LaoniuTrader.zip 到底有没有。
 *   有 → 把「下载」按钮变成真正的下载链接，并显示文件大小；
 *   没有 → 按钮变灰、显示「下载准备中，请联系作者微信 q352162」。
 *
 * 为什么要检查而不是写死链接：仓库是私密的，GitHub 的下载地址对外无效；
 * 发布时只要把打包好的 zip 丢进 downloads/，按钮自己就生效 —— 不会出现「点了 404」的死按钮。
 * （用 HEAD 探测；file:// 协议下 fetch 会失败，那时按「没有」处理，也就是显示待发布文案。）
 */
(function () {
  var ZIP = 'downloads/LaoniuTrader.zip';

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

  function markReady(size) {
    document.querySelectorAll('[data-size]').forEach(function (el) {
      el.textContent = size ? '（' + human(size) + '）' : '';
    });
  }

  function run() {
    if (!window.fetch) { markPending(); return; }
    fetch(ZIP, { method: 'HEAD', cache: 'no-store' })
      .then(function (r) {
        if (!r.ok) { markPending(); return; }
        var len = parseInt(r.headers.get('content-length') || '0', 10);
        markReady(len);
      })
      .catch(function () { markPending(); });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', run);
  } else {
    run();
  }
})();
