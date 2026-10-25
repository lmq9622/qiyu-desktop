/**
 * Qyu Dialog —— 用自家 UI 替换浏览器原生 confirm/alert/prompt
 *
 * 原生弹窗（"127.0.0.1:8765 显示 …"）和界面风格完全不搭，而且不可定制文案/按钮。
 * 这里给一套 Promise 版：
 *
 *   await QyuDialog.confirm('删除这个角色？')        // → true / false
 *   await QyuDialog.alert('已保存')                  // → undefined
 *   await QyuDialog.prompt('新名字', '默认值')        // → string / null
 *
 * 交互：Esc = 取消，Enter = 确定，点遮罩 = 取消；确认类默认聚焦"取消"（防误点）。
 * 样式全部取 index.html 的 :root 变量，深浅主题自动跟随。
 */
(function () {
    'use strict';

    var CSS_ID = 'qyu-dialog-css';
    var CSS = [
        '.qdlg-mask{position:fixed;inset:0;z-index:9999;display:flex;align-items:center;justify-content:center;',
        'background:rgba(20,20,24,.38);backdrop-filter:blur(6px);-webkit-backdrop-filter:blur(6px);',
        'opacity:0;transition:opacity .16s ease}',
        '.qdlg-mask.qdlg-in{opacity:1}',
        '.qdlg-card{width:min(420px,calc(100vw - 40px));background:var(--bg-surface,#fff);',
        'border:1px solid var(--border,rgba(0,0,0,.08));border-radius:var(--radius-xl,18px);',
        'box-shadow:0 24px 64px rgba(0,0,0,.22);padding:22px 22px 16px;',
        'transform:translateY(6px) scale(.985);transition:transform .18s cubic-bezier(.22,.61,.36,1)}',
        '.qdlg-mask.qdlg-in .qdlg-card{transform:none}',
        '.qdlg-title{font-size:1rem;font-weight:700;color:var(--text-primary,#111);margin-bottom:8px}',
        '.qdlg-body{font-size:.88rem;line-height:1.6;color:var(--text-secondary,#444);white-space:pre-wrap;word-break:break-word}',
        '.qdlg-input{width:100%;margin-top:12px;padding:10px 12px;font-size:.88rem;border-radius:var(--radius-md,10px);',
        'border:1px solid var(--border,rgba(0,0,0,.12));background:var(--bg-elevated,#fff);color:var(--text-primary,#111);outline:none}',
        '.qdlg-input:focus{border-color:var(--primary,#111)}',
        '.qdlg-actions{display:flex;justify-content:flex-end;gap:10px;margin-top:20px}',
        '.qdlg-btn{padding:9px 18px;border-radius:999px;font-size:.86rem;font-weight:600;cursor:pointer;border:1px solid transparent;transition:all .15s}',
        '.qdlg-cancel{background:transparent;color:var(--text-secondary,#444);border-color:var(--border,rgba(0,0,0,.14))}',
        '.qdlg-cancel:hover{background:var(--bg-hover,rgba(0,0,0,.04))}',
        '.qdlg-ok{background:var(--primary,#111);color:#fff}',
        '.qdlg-ok:hover{filter:brightness(1.08)}',
        '.qdlg-ok.qdlg-danger{background:var(--danger,#dc2626)}',
        '@media (prefers-color-scheme: dark){.qdlg-mask{background:rgba(0,0,0,.55)}}'
    ].join('');

    function ensureCss() {
        if (document.getElementById(CSS_ID)) return;
        var style = document.createElement('style');
        style.id = CSS_ID;
        style.textContent = CSS;
        document.head.appendChild(style);
    }

    function el(tag, cls, text) {
        var n = document.createElement(tag);
        if (cls) n.className = cls;
        if (text != null) n.textContent = String(text);
        return n;
    }

    /**
     * 核心：弹出自定义对话框
     * @returns Promise<{ok:boolean, value:string}>
     */
    function open(opts) {
        ensureCss();
        opts = opts || {};

        return new Promise(function (resolve) {
            var mask = el('div', 'qdlg-mask');
            var card = el('div', 'qdlg-card');
            card.setAttribute('role', 'dialog');
            card.setAttribute('aria-modal', 'true');

            if (opts.title) card.appendChild(el('div', 'qdlg-title', opts.title));
            var body = el('div', 'qdlg-body', opts.message || '');
            card.appendChild(body);

            var input = null;
            if (opts.mode === 'prompt') {
                input = el('input', 'qdlg-input');
                input.type = 'text';
                input.value = opts.defaultValue == null ? '' : String(opts.defaultValue);
                if (opts.placeholder) input.placeholder = opts.placeholder;
                card.appendChild(input);
            }

            var actions = el('div', 'qdlg-actions');
            var cancelBtn = el('button', 'qdlg-btn qdlg-cancel', opts.cancelText || '取消');
            var okBtn = el('button', 'qdlg-btn qdlg-ok' + (opts.danger ? ' qdlg-danger' : ''),
                           opts.okText || '确定');
            actions.appendChild(cancelBtn);
            actions.appendChild(okBtn);
            card.appendChild(actions);
            mask.appendChild(card);

            var done = false;
            function finish(ok) {
                if (done) return;
                done = true;
                document.removeEventListener('keydown', onKey, true);
                mask.classList.remove('qdlg-in');
                setTimeout(function () {
                    if (mask.parentNode) mask.parentNode.removeChild(mask);
                }, 160);
                resolve({ ok: ok, value: input ? input.value : '' });
            }

            function onKey(e) {
                if (e.key === 'Escape') { e.preventDefault(); finish(false); }
                else if (e.key === 'Enter' && opts.mode !== 'alert') {
                    if (document.activeElement === cancelBtn) return;   // 聚焦在取消上，回车不误确定
                    e.preventDefault(); finish(true);
                }
            }

            cancelBtn.addEventListener('click', function () { finish(false); });
            okBtn.addEventListener('click', function () { finish(true); });
            mask.addEventListener('mousedown', function (e) {
                if (e.target === mask) finish(false);
            });
            document.addEventListener('keydown', onKey, true);

            document.body.appendChild(mask);
            requestAnimationFrame(function () { mask.classList.add('qdlg-in'); });

            setTimeout(function () {
                if (input) { input.focus(); input.select(); }
                else if (opts.mode === 'alert') okBtn.focus();
                else cancelBtn.focus();     // 确认类默认落在"取消"，防误删
            }, 30);
        });
    }

    var QyuDialog = {
        version: '1.0.0',

        /** 确认框 → Promise<boolean> */
        confirm: function (message, opts) {
            opts = Object.assign({}, opts, { mode: 'confirm', message: message });
            return open(opts).then(function (r) { return !!r.ok; });
        },

        /** 提示框 → Promise<void> */
        alert: function (message, opts) {
            opts = Object.assign({}, opts, { mode: 'alert', message: message, okText: '知道了' });
            return open(opts).then(function () { /* void */ });
        },

        /** 输入框 → Promise<string|null>（取消为 null） */
        prompt: function (message, defaultValue, opts) {
            opts = Object.assign({}, opts, { mode: 'prompt', message: message, defaultValue: defaultValue });
            return open(opts).then(function (r) { return r.ok ? r.value : null; });
        }
    };

    window.QyuDialog = QyuDialog;
})();
