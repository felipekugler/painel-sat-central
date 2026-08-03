// Utilitários compartilhados do Painel SAT Central.

function showToast(msg, tipo) {
    tipo = tipo || 'info';
    var box = document.getElementById('toast-container');
    if (!box) return;
    var cls = { success: 'alert-success', error: 'alert-error', info: 'alert-info' }[tipo] || 'alert-info';
    var el = document.createElement('div');
    el.className = 'alert ' + cls + ' shadow-lg text-sm py-2 px-3';
    el.style.maxWidth = '360px';
    el.innerHTML = '<span>' + msg + '</span>';
    box.appendChild(el);
    setTimeout(function () {
        el.style.transition = 'opacity 0.3s ease';
        el.style.opacity = '0';
        setTimeout(function () { el.remove(); }, 300);
    }, 4000);
}
window.showToast = showToast;
