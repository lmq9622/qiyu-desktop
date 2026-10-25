/*!
 * 栖语 · 记忆面板（memory_panel.js）
 * ---------------------------------------------------------------------------
 * 独立文件、原生 JS、零依赖。由 index.html 用一个空容器挂载：
 *
 *   QiyuMemoryPanel.mount(document.getElementById('memoryPanelRoot'), {
 *       getCharId: () => app.state.currentCharacter?.id || '',
 *       getUserId: () => app.state.sessionUserId || 'web_user',
 *       onExport:  () => app.exportCurrentQy3(),   // 可选：复用主程序导出
 *       onClear:   () => app.clearMemories(),      // 可选：复用主程序清空弹窗
 *       onSummary: () => app.generateSummary(),    // 可选：复用主程序摘要
 *       onToast:   (msg, type) => app.toast(msg, type)   // 可选
 *   });
 *
 * 对外 API：
 *   QiyuMemoryPanel.mount(el, opts)   挂载（可重复调用，会重建 DOM）
 *   QiyuMemoryPanel.refresh()         重新拉取并渲染，返回 Promise<boolean>
 *   QiyuMemoryPanel.setChar(charId)   切换角色记忆库并刷新，返回 Promise<boolean>
 *   QiyuMemoryPanel.getState()        只读快照（自测/联调用）
 *   QiyuMemoryPanel.unmount()         卸载
 *
 * 安全约定：所有用户数据一律走 textContent / createTextNode / createElement，
 *          任何地方都不会把用户字符串当成 HTML 解析 —— 天然免疫 XSS。
 * ---------------------------------------------------------------------------
 */
(function (global) {
    'use strict';

    var VERSION = '1.0.0';

    /* ======================= 常量 ======================= */

    // 超过 200 条才分页，每页 50 条（需求阈值；200 条以内整表渲染不卡）
    var PAGE_THRESHOLD = 200;
    var PAGE_SIZE = 50;
    var SEARCH_DEBOUNCE = 300;
    var LS_COLLAPSE_KEY = 'qiyu.memory.collapsed.v1';

    // 分组定义：顺序即渲染顺序
    var GROUPS = [
        { key: 'day', label: '当天', hint: '今天的总结与事件' },
        { key: 'short', label: '短期', hint: '会随时间淡忘' },
        { key: 'long', label: '长期', hint: '永久保留' },
        { key: 'summary', label: '摘要', hint: '阶段性归纳' },
        { key: 'knowledge', label: '知识库', hint: '来自文档资料' },
        // 兜底分组：只会在出现未知 memory_type（如 user_profile）时才渲染
        { key: 'other', label: '其他', hint: '未归类类型' }
    ];

    // memory_type -> 分组。后端 MemoryEntry.memory_type ∈ short_term|long_term|summary|knowledge
    var TYPE_GROUP = {
        short_term: 'short',
        short: 'short',
        long_term: 'long',
        long: 'long',
        summary: 'summary',
        knowledge: 'knowledge',
        day_summary: 'day',
        user_profile: 'other'
    };

    var TYPE_LABEL = {
        short_term: '短期记忆',
        long_term: '长期记忆',
        summary: '摘要',
        knowledge: '知识库',
        day_summary: '当天总结',
        user_profile: '用户画像'
    };

    /* ======================= 外部脚本自身地址（用于自动补 CSS） ======================= */

    var scriptEl = document.currentScript;
    var SCRIPT_SRC = (scriptEl && scriptEl.src) || '';

    /* ======================= 状态 ======================= */

    var S = {
        mounted: false,
        root: null,
        opts: {},
        refs: {},

        charId: '',          // setChar() 显式设定的角色
        charIdUsed: '',      // 最近一次 refresh 实际使用的角色
        entries: [],         // 归一化后的全部条目
        byKey: {},
        query: '',
        filtered: [],
        page: 0,
        pageCount: 1,

        selected: {},        // selKey -> true
        collapsed: {},       // groupKey -> true（持久化到 localStorage）
        expanded: {},        // selKey -> true（正文展开）
        open: {},            // selKey -> true（详情展开）

        loading: false,
        error: '',
        seq: 0,              // 请求序号，丢弃过期响应
        rendered: false,
        lastSig: null,       // 上一次渲染的数据签名；null = 还没渲染过（空结果必须照常重绘）
        clampCandidates: null,
        searchTimer: null,
        tipTimer: null
    };

    /* ======================= 工具函数 ======================= */

    function make(tag, cls, text) {
        var el = document.createElement(tag);
        if (cls) el.className = cls;
        if (text !== undefined && text !== null) el.textContent = String(text);
        return el;
    }

    function pad2(n) { return n < 10 ? '0' + n : '' + n; }

    function toDate(v) {
        if (v === null || v === undefined || v === '') return null;
        if (v instanceof Date) return isNaN(v.getTime()) ? null : v;
        if (typeof v === 'number') return new Date(v < 1e12 ? v * 1000 : v); // 秒/毫秒都兼容
        var d = new Date(v);
        return isNaN(d.getTime()) ? null : d;
    }

    function fmtShort(d) {
        if (!d) return '';
        return pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()) + ' ' + pad2(d.getHours()) + ':' + pad2(d.getMinutes());
    }

    function fmtFull(d) {
        if (!d) return '—';
        return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()) + ' ' +
            pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ':' + pad2(d.getSeconds());
    }

    function num(v) {
        if (v === null || v === undefined || v === '') return null;
        var n = parseFloat(v);
        return isFinite(n) ? n : null;
    }

    function truncate(s, n) {
        s = String(s == null ? '' : s);
        return s.length > n ? s.slice(0, n) + '…' : s;
    }

    function weightClass(w) {
        if (w >= 0.75) return 'is-high';
        if (w >= 0.4) return 'is-mid';
        return 'is-low';
    }

    /**
     * 高亮：把 text 按 query 切片，命中段包 <mark>。
     * 全程 createTextNode / textContent —— 用户数据永远不会被当成 HTML 解析。
     */
    function appendHighlighted(node, text, query) {
        text = String(text == null ? '' : text);
        if (!query) { node.appendChild(document.createTextNode(text)); return; }

        var lowerText = text.toLowerCase();
        var lowerQuery = query.toLowerCase();
        var from = 0;
        var idx = lowerText.indexOf(lowerQuery, from);

        if (idx < 0) { node.appendChild(document.createTextNode(text)); return; }

        while (idx >= 0) {
            if (idx > from) node.appendChild(document.createTextNode(text.slice(from, idx)));
            var mark = make('mark', 'mp-mark');
            mark.textContent = text.substr(idx, query.length);
            node.appendChild(mark);
            from = idx + query.length;
            idx = lowerText.indexOf(lowerQuery, from);
        }
        if (from < text.length) node.appendChild(document.createTextNode(text.slice(from)));
    }

    function kv(label, value) {
        var row = make('div', 'mp-kv');
        row.appendChild(make('span', 'mp-kv-key', label));
        var val = make('span', 'mp-kv-val');
        if (value && value.nodeType === 1) val.appendChild(value);
        else val.textContent = (value === null || value === undefined || value === '') ? '—' : String(value);
        row.appendChild(val);
        return row;
    }

    /* ======================= 样式自动补齐 ======================= */

    // 如果 index.html 忘了写 <link>，就从脚本自身路径推出同目录的 css 自动补一张
    function ensureStyles() {
        if (document.querySelector('link[data-qiyu-memory-panel]')) return;
        if (document.querySelector('link[href$="memory_panel.css"]')) return;
        if (!SCRIPT_SRC) return;
        var href = SCRIPT_SRC.replace(/[^\/]*$/, 'memory_panel.css');
        var link = document.createElement('link');
        link.rel = 'stylesheet';
        link.href = href;
        link.setAttribute('data-qiyu-memory-panel', '1');
        document.head.appendChild(link);
    }

    /* ======================= 宿主对接 ======================= */

    function hostApp() { return global.app || null; }

    function resolveUserId() {
        if (typeof S.opts.getUserId === 'function') {
            try { var v = S.opts.getUserId(); if (v) return String(v); } catch (e) { /* 宿主异常不影响面板 */ }
        }
        var app = hostApp();
        var u = app && app.state && app.state.sessionUserId;
        return u ? String(u) : 'web_user';
    }

    function resolveCharId() {
        // 优先级：opts.getCharId（活的） > setChar() 设定 > app.state.currentCharacter
        if (typeof S.opts.getCharId === 'function') {
            try {
                var v = S.opts.getCharId();
                if (v) return String(v);
            } catch (e) { /* 宿主异常不影响面板 */ }
        }
        if (S.charId) return String(S.charId);
        var app = hostApp();
        var c = app && app.state && app.state.currentCharacter;
        return c && c.id ? String(c.id) : '';
    }

    function toast(msg, type) {
        type = type || 'info';
        if (typeof S.opts.onToast === 'function') {
            try { S.opts.onToast(msg, type); return; } catch (e) { /* 降级 */ }
        }
        var app = hostApp();
        if (app && typeof app.toast === 'function') {
            try { app.toast(msg, type); return; } catch (e) { /* 降级 */ }
        }
        showTip(msg, type);
    }

    // 没有宿主 toast 时的内联提示（自测页 / 独立使用）
    function showTip(msg, type) {
        if (!S.root) return;
        var old = S.root.querySelector('.mp-tip');
        if (old) old.remove();
        var cls = 'mp-tip' + (type === 'error' ? ' is-error' : type === 'success' ? ' is-success' : '');
        var tip = make('div', cls, msg);
        S.root.appendChild(tip);
        clearTimeout(S.tipTimer);
        S.tipTimer = setTimeout(function () { if (tip.parentNode) tip.remove(); }, 3200);
    }

    /* ======================= 接口请求 ======================= */

    function apiBase() {
        return (S.opts.apiBase !== undefined && S.opts.apiBase !== null) ? String(S.opts.apiBase) : '';
    }

    function request(method, path, body) {
        var opt = { method: method, headers: {} };
        if (body !== undefined) {
            opt.headers['Content-Type'] = 'application/json';
            opt.body = JSON.stringify(body);
        }
        return fetch(apiBase() + path, opt).then(function (resp) {
            if (!resp.ok) {
                return resp.text().then(function (t) {
                    throw new Error('HTTP ' + resp.status + (t ? '：' + truncate(t, 150) : ''));
                });
            }
            if (resp.status === 204) return null;
            return resp.json();
        }, function (err) {
            throw new Error('网络错误：' + ((err && err.message) || '无法连接后端'));
        });
    }

    /* ======================= 数据归一化 ======================= */

    function tagsOf(m) {
        var meta = m.metadata || {};
        var t = m.tags || meta.tags || [];
        if (typeof t === 'string') t = [t];
        if (!Array.isArray(t)) return [];
        return t.filter(function (x) {
            return x !== null && x !== undefined && String(x).trim() !== '';
        }).map(String);
    }

    function groupOf(type) {
        var t = String(type || '').toLowerCase();
        return TYPE_GROUP[t] || 'other';
    }

    function baseEntry(over) {
        var e = {
            selKey: '', id: '', group: 'other', kind: 'entry', memoryType: '',
            text: '', ts: null, weight: null, source: '', tags: [],
            mentionCount: null, lastSeen: null, metadata: {}, raw: null
        };
        for (var k in over) { if (Object.prototype.hasOwnProperty.call(over, k)) e[k] = over[k]; }
        return e;
    }

    /**
     * 把 /v1/memory/{user} 的响应压平成统一条目列表。
     * 响应结构（demo.py 实测）：
     *   { data:[全量条目], day:[当天总结], events:[当天事件], short:[], long:[] }
     * data 里已经带 memory_type，所以 short/long 只作为旧版响应兜底。
     */
    function collectEntries(payload) {
        payload = payload || {};
        var out = [];
        var seen = {};
        function push(entry) {
            if (!entry || !entry.text) return;
            if (seen[entry.selKey]) return;
            seen[entry.selKey] = 1;
            out.push(entry);
        }

        // 1) 当天总结（role=system 的一条概述，没有 id，不可删除）
        (Array.isArray(payload.day) ? payload.day : []).forEach(function (item, i) {
            var text = String(item.text || item.content || '').trim();
            if (!text) return;
            push(baseEntry({
                selKey: 'day-note:' + i,
                group: 'day',
                kind: 'note',
                text: text,
                ts: toDate(item.timestamp),
                source: item.type === 'day_summary' ? '当天总结' : '当天',
                metadata: item,
                raw: item
            }));
        });

        // 2) 当天事件（带权重，无 id，不可删除）
        (Array.isArray(payload.events) ? payload.events : []).forEach(function (item, i) {
            var text = String(item.text || '').trim();
            if (!text) return;
            push(baseEntry({
                selKey: 'day-event:' + i,
                group: 'day',
                kind: 'event',
                text: text,
                ts: toDate(item.ts || item.timestamp),
                weight: num(item.weight),
                source: item.topic ? ('事件 · ' + item.topic) : '当天事件',
                tags: item.topic ? [String(item.topic)] : [],
                metadata: item,
                raw: item
            }));
        });

        // 3) 记忆库条目：优先 data（全量、带 memory_type），否则退回 short/long
        var list = (Array.isArray(payload.data) && payload.data.length)
            ? payload.data
            : [].concat(payload.short || [], payload.long || []);

        list.forEach(function (m, i) {
            if (!m) return;
            var meta = m.metadata || {};
            var id = m.id !== undefined && m.id !== null ? String(m.id) : '';
            var type = String(m.memory_type || m.type || '').toLowerCase();
            var text = String(m.text || m.content || '').trim();
            if (!text) return;
            push(baseEntry({
                selKey: id ? ('id:' + id) : ('raw:' + type + ':' + i),
                id: id,
                group: groupOf(type),
                kind: 'entry',
                memoryType: type,
                text: text,
                ts: toDate(m.created_at || m.timestamp),
                weight: num(meta.weight),
                source: meta.source ? String(meta.source) : '',
                tags: tagsOf(m),
                mentionCount: (meta.mention_count !== undefined && meta.mention_count !== null) ? meta.mention_count : null,
                lastSeen: toDate(meta.last_seen),
                metadata: meta,
                raw: m
            }));
        });

        return out;
    }

    // 组内排序：当天总结置顶，其余按时间倒序（无时间的排最后）
    function sortEntries(arr) {
        return arr.sort(function (a, b) {
            if (a.kind === 'note' && b.kind !== 'note') return -1;
            if (b.kind === 'note' && a.kind !== 'note') return 1;
            var ta = a.ts ? a.ts.getTime() : -Infinity;
            var tb = b.ts ? b.ts.getTime() : -Infinity;
            if (ta !== tb) return tb - ta;
            return 0;
        });
    }

    function indexByKey(entries) {
        var map = {};
        entries.forEach(function (e) { map[e.selKey] = e; });
        return map;
    }

    function signatureOf(entries) {
        // 只用来判断“内容有没有变”，避免每次刷新都重绘打断 hover / 滚动
        return entries.map(function (e) {
            return e.selKey + '~' + e.text.length + '~' + (e.ts ? e.ts.getTime() : 0) + '~' + (e.weight === null ? '-' : e.weight);
        }).join('|');
    }

    /* ======================= 折叠状态持久化 ======================= */

    function loadCollapse() {
        try {
            var raw = global.localStorage ? global.localStorage.getItem(LS_COLLAPSE_KEY) : null;
            var obj = raw ? JSON.parse(raw) : null;
            S.collapsed = (obj && typeof obj === 'object') ? obj : {};
        } catch (e) {
            S.collapsed = {};
        }
    }

    function saveCollapse() {
        try {
            if (global.localStorage) global.localStorage.setItem(LS_COLLAPSE_KEY, JSON.stringify(S.collapsed));
        } catch (e) { /* 隐身模式 / 配额满：忽略即可 */ }
    }

    /* ======================= 选择状态 ======================= */

    function selectedCount() {
        var n = 0;
        for (var k in S.selected) { if (S.selected[k]) n++; }
        return n;
    }

    function selectedEntries() {
        return S.entries.filter(function (e) { return !!S.selected[e.selKey]; });
    }

    function pruneSelection() {
        var live = {};
        S.entries.forEach(function (e) { live[e.selKey] = 1; });
        Object.keys(S.selected).forEach(function (k) { if (!live[k]) delete S.selected[k]; });
    }

    /* ======================= 骨架 ======================= */

    function buildShell() {
        var root = S.root;
        root.textContent = '';
        root.classList.add('mp-root');

        var refs = S.refs = {};

        /* ---- 头部 ---- */
        var head = make('div', 'mp-head');
        var titleWrap = make('div', 'mp-title-wrap');
        titleWrap.appendChild(make('span', 'mp-title', '记忆'));
        var total = make('span', 'mp-total mp-hidden');
        total.title = '已加载的记忆条数';
        titleWrap.appendChild(total);
        head.appendChild(titleWrap);

        var actions = make('div', 'mp-actions');

        var btnRefresh = make('button', 'mp-icon-btn', '↻');
        btnRefresh.type = 'button';
        btnRefresh.title = '刷新记忆';
        btnRefresh.setAttribute('aria-label', '刷新记忆');

        var btnExport = make('button', 'mp-icon-btn', '导出角色');
        btnExport.type = 'button';
        btnExport.title = '把该角色（人设 + 全部记忆）导出为 .qy3f（不加密）';
        btnExport.setAttribute('aria-label', '导出角色');

        var btnClear = make('button', 'mp-icon-btn is-danger', '清空');
        btnClear.type = 'button';
        btnClear.title = '清空记忆（复用主程序的清空弹窗）';
        btnClear.setAttribute('aria-label', '清空记忆');

        actions.appendChild(btnRefresh);
        actions.appendChild(btnExport);
        actions.appendChild(btnClear);
        head.appendChild(actions);

        /* ---- 搜索 ---- */
        var searchWrap = make('div', 'mp-search-wrap');
        var search = make('input', 'mp-search');
        search.type = 'text';
        search.placeholder = '搜索记忆（输入即搜）';
        search.setAttribute('aria-label', '搜索记忆');
        var searchClear = make('button', 'mp-search-clear mp-hidden', '×');
        searchClear.type = 'button';
        searchClear.title = '清空搜索';
        searchClear.setAttribute('aria-label', '清空搜索');
        searchWrap.appendChild(search);
        searchWrap.appendChild(searchClear);

        /* ---- 分页条（顶部常驻，列表底部还有一条） ---- */
        var pagerTop = make('div', 'mp-pager mp-pager-top mp-hidden');
        pagerTop.setAttribute('data-role', 'pager-top');

        /* ---- 列表（唯一滚动容器） ---- */
        var list = make('div', 'mp-list');
        list.setAttribute('role', 'list');
        list.setAttribute('aria-label', '记忆列表');
        /* ---- 新增 / 摘要 ---- */
        var add = make('div', 'mp-add');
        var addInput = make('textarea', 'mp-add-input');
        addInput.rows = 2;
        addInput.placeholder = '添加一条记忆…（Ctrl+Enter 提交）';
        addInput.setAttribute('aria-label', '新记忆内容');
        var addRow = make('div', 'mp-add-row');
        var btnAdd = make('button', 'btn btn-secondary btn-sm', '添加记忆');
        btnAdd.type = 'button';
        var btnSummary = make('button', 'btn btn-ghost btn-sm', '生成摘要');
        btnSummary.type = 'button';
        addRow.appendChild(btnAdd);
        addRow.appendChild(btnSummary);
        add.appendChild(addInput);
        add.appendChild(addRow);

        /* ---- 底部多选导出条 ---- */
        var selbar = make('div', 'mp-selbar mp-hidden');
        var selTop = make('div', 'mp-selbar-top');
        var selCount = make('span', 'mp-sel-count');
        var btnSelExport = make('button', 'btn btn-primary btn-sm', '导出角色');
        btnSelExport.type = 'button';
        var btnSelClear = make('button', 'mp-icon-btn', '取消');
        btnSelClear.type = 'button';
        btnSelClear.title = '取消全部勾选';
        selTop.appendChild(selCount);
        selTop.appendChild(btnSelExport);
        selTop.appendChild(btnSelClear);

        var selHint = make('div', 'mp-sel-hint');
        selHint.appendChild(document.createTextNode('整角色导出：包含该角色的人设与'));
        selHint.appendChild(make('b', '', '全部记忆'));
        selHint.appendChild(document.createTextNode('；后端暂不支持只导出勾选条目（勾选仅作标记）。'));

        selbar.appendChild(selTop);
        selbar.appendChild(selHint);

        root.appendChild(head);
        root.appendChild(searchWrap);
        root.appendChild(pagerTop);
        root.appendChild(list);
        root.appendChild(add);
        root.appendChild(selbar);

        refs.total = total;
        refs.btnRefresh = btnRefresh;
        refs.btnExport = btnExport;
        refs.btnClear = btnClear;
        refs.search = search;
        refs.searchClear = searchClear;
        refs.pagerTop = pagerTop;
        refs.list = list;
        refs.add = add;
        refs.addInput = addInput;
        refs.btnAdd = btnAdd;
        refs.btnSummary = btnSummary;
        refs.selbar = selbar;
        refs.selCount = selCount;
        refs.btnSelExport = btnSelExport;
        refs.btnSelClear = btnSelClear;

        // 没有 onClear 钩子时不给一个点了没反应的按钮
        if (typeof S.opts.onClear !== 'function') btnClear.classList.add('mp-hidden');

        bindShellControls();
        updateChrome();
    }

    function bindShellControls() {
        var r = S.refs;

        r.btnRefresh.addEventListener('click', function () { refresh(); });

        r.btnExport.addEventListener('click', function () { exportQy3(); });

        r.btnClear.addEventListener('click', function () {
            if (typeof S.opts.onClear === 'function') { S.opts.onClear(); return; }
            toast('清空入口未接入（宿主未提供 onClear）', 'info');
        });

        r.search.addEventListener('input', function () {
            var has = !!r.search.value;
            r.searchClear.classList.toggle('mp-hidden', !has);
            clearTimeout(S.searchTimer);
            S.searchTimer = setTimeout(function () {
                S.query = (r.search.value || '').trim();
                S.page = 0;
                paint();
            }, SEARCH_DEBOUNCE);
        });

        r.search.addEventListener('keydown', function (ev) {
            if (ev.key === 'Escape') {
                ev.preventDefault();
                clearSearch();
            } else if (ev.key === 'Enter') {
                // 输入即搜已在防抖里，回车只是立刻生效
                ev.preventDefault();
                clearTimeout(S.searchTimer);
                S.query = (r.search.value || '').trim();
                S.page = 0;
                paint();
            }
        });

        r.searchClear.addEventListener('click', function () { clearSearch(); });

        r.btnAdd.addEventListener('click', function () { addMemory(); });

        r.addInput.addEventListener('keydown', function (ev) {
            if (ev.key === 'Enter' && (ev.ctrlKey || ev.metaKey)) {
                ev.preventDefault();
                addMemory();
            }
        });

        r.btnSummary.addEventListener('click', function () { generateSummary(); });

        r.btnSelExport.addEventListener('click', function () { exportQy3(); });

        r.btnSelClear.addEventListener('click', function () {
            S.selected = {};
            paint();
        });
    }

    /* ======================= 根节点事件委托（只绑一次） ======================= */

    function bindRootDelegation() {
        var root = S.root;
        if (!root || root.getAttribute('data-mp-bound') === '1') return;
        root.setAttribute('data-mp-bound', '1');
        root.addEventListener('click', onRootClick);
        root.addEventListener('change', onRootChange);
    }

    function closestRow(node) {
        while (node && node !== S.root) {
            if (node.classList && node.classList.contains('mp-item')) return node;
            node = node.parentNode;
        }
        return null;
    }

    function onRootClick(ev) {
        var t = ev.target;
        if (!t || !t.closest) return;

        // 状态块里的动作按钮
        var actionEl = t.closest('[data-mp-action]');
        if (actionEl) {
            var action = actionEl.getAttribute('data-mp-action');
            if (action === 'retry') refresh();
            else if (action === 'clear-search') clearSearch();
            return;
        }

        // 分页
        var pageEl = t.closest('[data-mp-page]');
        if (pageEl) {
            var next = parseInt(pageEl.getAttribute('data-mp-page'), 10);
            if (isFinite(next)) {
                S.page = Math.max(0, Math.min(S.pageCount - 1, next));
                paint();
                if (S.refs.list) S.refs.list.scrollTop = 0;
            }
            return;
        }

        // 分组折叠
        var groupHead = t.closest('.mp-group-head');
        if (groupHead) {
            toggleGroup(groupHead.parentNode);
            return;
        }

        // “展开 / 收起”（在 mp-main 内，必须比 mp-main 先判断）
        var more = t.closest('.mp-more');
        if (more) {
            toggleText(more);
            return;
        }

        // 删除
        var del = t.closest('.mp-del');
        if (del) {
            var delRow = closestRow(del);
            var delEntry = delRow ? S.byKey[delRow.getAttribute('data-key')] : null;
            if (delEntry) deleteEntry(delEntry);
            return;
        }

        // 点正文任意处：展开/收起 metadata 详情
        var main = t.closest('.mp-main');
        if (main) {
            toggleDetail(main.parentNode);
        }
    }

    function onRootChange(ev) {
        var t = ev.target;
        if (!t || !t.classList || !t.classList.contains('mp-check')) return;
        var row = closestRow(t);
        if (!row) return;
        var key = row.getAttribute('data-key');
        if (t.checked) S.selected[key] = true;
        else delete S.selected[key];
        row.classList.toggle('is-selected', !!t.checked);
        updateChrome();
    }

    /* ======================= 交互实现 ======================= */

    function clearSearch() {
        clearTimeout(S.searchTimer);
        S.query = '';
        S.page = 0;
        if (S.refs.search) S.refs.search.value = '';
        if (S.refs.searchClear) S.refs.searchClear.classList.add('mp-hidden');
        paint();
    }

    function toggleGroup(groupEl) {
        if (!groupEl) return;
        var collapsed = groupEl.classList.toggle('is-collapsed');
        var head = groupEl.querySelector('.mp-group-head');
        if (head) head.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
        var key = groupEl.getAttribute('data-group');
        // 搜索态下分组被强制展开，此时不写持久化，免得退出搜索后组突然全折叠
        if (key && !S.query) {
            if (collapsed) S.collapsed[key] = true;
            else delete S.collapsed[key];
            saveCollapse();
        }
    }

    function toggleText(moreBtn) {
        var row = closestRow(moreBtn);
        if (!row) return;
        var key = row.getAttribute('data-key');
        var textEl = row.querySelector('.mp-text');
        if (!textEl) return;
        var expanded = !S.expanded[key];
        if (expanded) S.expanded[key] = true;
        else delete S.expanded[key];
        textEl.classList.toggle('is-clamped', !expanded);
        moreBtn.textContent = expanded ? '收起' : '展开';
    }

    function toggleDetail(row) {
        if (!row) return;
        var open = row.classList.toggle('is-open');
        var key = row.getAttribute('data-key');
        if (key) {
            if (open) S.open[key] = true;
            else delete S.open[key];
        }
    }

    /* ======================= 删除 / 新增 / 摘要 / 导出 ======================= */

    function deleteEntry(entry) {
        if (!entry || !entry.id) return;
        var preview = truncate(entry.text.replace(/\s+/g, ' '), 42);
        if (!global.confirm('删除这条记忆？\n\n' + preview)) return;

        var url = '/v1/memory/' + encodeURIComponent(resolveUserId()) + '/' +
            encodeURIComponent(entry.id) + '?char_id=' + encodeURIComponent(S.charIdUsed);

        request('DELETE', url).then(function () {
            // 后端已删：本地摘掉这条，避免整表重拉导致闪烁
            S.entries = S.entries.filter(function (x) { return x.selKey !== entry.selKey; });
            S.byKey = indexByKey(S.entries);
            delete S.selected[entry.selKey];
            delete S.open[entry.selKey];
            delete S.expanded[entry.selKey];
            S.lastSig = signatureOf(S.entries);
            toast('已删除 1 条记忆', 'success');
            paint();
        }).catch(function (err) {
            toast('删除失败：' + err.message, 'error');
        });
    }

    function addMemory() {
        var input = S.refs.addInput;
        if (!input) return;
        var text = (input.value || '').trim();
        if (!text) { toast('请先输入记忆内容', 'error'); return; }
        var charId = S.charIdUsed || resolveCharId();
        if (!charId) { toast('先选一个角色再添加记忆', 'error'); return; }

        var btn = S.refs.btnAdd;
        if (btn) btn.disabled = true;

        var url = '/v1/memory/' + encodeURIComponent(resolveUserId()) + '?char_id=' + encodeURIComponent(charId);
        request('POST', url, { text: text, tags: [] }).then(function () {
            input.value = '';
            toast('记忆已添加', 'success');
            return refresh();
        }).catch(function (err) {
            toast('添加失败：' + err.message, 'error');
        }).then(function () {
            if (S.refs.btnAdd) S.refs.btnAdd.disabled = false;
        });
    }

    function generateSummary() {
        if (typeof S.opts.onSummary === 'function') {
            try { S.opts.onSummary(); return; } catch (e) { /* 走内置实现 */ }
        }
        var charId = S.charIdUsed || resolveCharId();
        if (!charId) { toast('先选一个角色再生成摘要', 'error'); return; }

        var btn = S.refs.btnSummary;
        if (btn) btn.disabled = true;

        var url = '/v1/memory/' + encodeURIComponent(resolveUserId()) + '/summary?char_id=' + encodeURIComponent(charId);
        request('POST', url).then(function (d) {
            if (d && d.summary) toast('摘要已生成', 'success');
            else toast((d && d.message) || '摘要生成完成', 'info');
            return refresh();
        }).catch(function (err) {
            toast('生成摘要失败：' + err.message, 'error');
        }).then(function () {
            if (S.refs.btnSummary) S.refs.btnSummary.disabled = false;
        });
    }

    /**
     * 导出 qy3。
     * 注意：后端只有 /v1/characters/{id}/export.qy3，按「整角色」导出（人设 + 全部记忆），
     * 没有按条导出的接口，所以这里的勾选只作为标记，不改变导出内容。
     */
    function exportQy3() {
        var charId = S.charIdUsed || resolveCharId();
        var count = selectedCount();

        if (typeof S.opts.onExport === 'function') {
            if (count) toast('勾选仅作标记：导出会包含该角色的全部记忆', 'info');
            try { S.opts.onExport({ charId: charId, selected: selectedEntries() }); return; } catch (e) { /* 走内置实现 */ }
        }

        if (!charId) { toast('先选一个角色再导出', 'error'); return; }

        var url = '/v1/characters/' + encodeURIComponent(charId) + '/export.qy3';
        var a = document.createElement('a');
        a.href = url;
        a.download = charId + '.qy3f';
        a.style.display = 'none';
        document.body.appendChild(a);
        a.click();
        setTimeout(function () { a.remove(); }, 1000);

        toast(count
            ? ('已开始导出整角色 .qy3f（含全部记忆；勾选的 ' + count + ' 条仅作标记）')
            : '已开始导出整角色 .qy3f（人设 + 全部记忆，不加密）', 'success');
    }

    /* ======================= 渲染 ======================= */

    function applyFilter(entries) {
        var q = S.query;
        if (!q) return entries.slice();
        var lower = q.toLowerCase();
        return entries.filter(function (e) {
            if (e.text.toLowerCase().indexOf(lower) !== -1) return true;
            if (e.source && String(e.source).toLowerCase().indexOf(lower) !== -1) return true;
            for (var i = 0; i < e.tags.length; i++) {
                if (String(e.tags[i]).toLowerCase().indexOf(lower) !== -1) return true;
            }
            return false;
        });
    }

    function paginate(arr) {
        if (arr.length > PAGE_THRESHOLD) {
            S.pageCount = Math.ceil(arr.length / PAGE_SIZE);
            if (S.page >= S.pageCount) S.page = S.pageCount - 1;
            if (S.page < 0) S.page = 0;
            var start = S.page * PAGE_SIZE;
            return arr.slice(start, start + PAGE_SIZE);
        }
        S.pageCount = 1;
        S.page = 0;
        return arr.slice();
    }

    function stateKind() {
        if (S.loading && !S.entries.length) return 'loading';
        if (S.error && !S.entries.length) return 'error';
        if (!S.charIdUsed) return 'nochar';
        if (!S.entries.length) return 'empty';
        if (!S.filtered.length) return 'nomatch';
        return 'list';
    }

    function buildState(kind) {
        var box = make('div', 'mp-state');

        if (kind === 'loading') {
            box.appendChild(make('div', 'mp-spinner'));
            var sk = make('div', 'mp-skeleton');
            for (var i = 0; i < 3; i++) sk.appendChild(make('div', 'mp-skeleton-row'));
            box.appendChild(sk);
            box.appendChild(make('div', 'mp-state-hint', '正在读取记忆…'));
            return box;
        }

        if (kind === 'error') {
            box.appendChild(make('div', 'mp-state-title', '记忆加载失败'));
            box.appendChild(make('div', 'mp-state-hint', truncate(S.error || '未知错误', 180)));
            var act = make('div', 'mp-state-actions');
            var retry = make('button', 'btn btn-secondary btn-sm', '重试');
            retry.type = 'button';
            retry.setAttribute('data-mp-action', 'retry');
            act.appendChild(retry);
            box.appendChild(act);
            return box;
        }

        if (kind === 'nochar') {
            box.appendChild(make('div', 'mp-state-title', '先选一个角色'));
            box.appendChild(make('div', 'mp-state-hint', '记忆按角色隔离存放；选好角色后，这里会显示 TA 记住的事。'));
            return box;
        }

        if (kind === 'empty') {
            box.appendChild(make('div', 'mp-state-title', '还没有记忆'));
            box.appendChild(make('div', 'mp-state-hint', '和 TA 多聊几句，或在下面手动添加一条。'));
            return box;
        }

        // nomatch
        box.appendChild(make('div', 'mp-state-title', '没有匹配的记忆'));
        box.appendChild(make('div', 'mp-state-hint', '关键词「' + truncate(S.query, 24) + '」没有命中，换个词试试。'));
        var act2 = make('div', 'mp-state-actions');
        var clearBtn = make('button', 'btn btn-ghost btn-sm', '清空搜索');
        clearBtn.type = 'button';
        clearBtn.setAttribute('data-mp-action', 'clear-search');
        act2.appendChild(clearBtn);
        box.appendChild(act2);
        return box;
    }

    function buildMeta(entry) {
        var meta = make('div', 'mp-meta');

        if (entry.ts) meta.appendChild(make('span', '', fmtShort(entry.ts)));
        else if (entry.kind === 'note') meta.appendChild(make('span', '', '当天'));
        else meta.appendChild(make('span', '', '时间未知'));

        if (entry.weight !== null) {
            var w = make('span', 'mp-weight ' + weightClass(entry.weight), Math.round(entry.weight * 100) + '%');
            w.title = '权重（越高越不容易被淡忘）';
            meta.appendChild(w);
        }

        entry.tags.slice(0, 2).forEach(function (tag) {
            var chip = make('span', 'mp-tag');
            appendHighlighted(chip, tag, S.query);
            meta.appendChild(chip);
        });
        if (entry.tags.length > 2) meta.appendChild(make('span', '', '+' + (entry.tags.length - 2)));
        return meta;
    }

    // 详情里把 metadata 剩余字段压成一行，方便排查
    function extraMetadata(entry) {
        var meta = entry.metadata || {};
        var skip = { weight: 1, source: 1, tags: 1, mention_count: 1, last_seen: 1 };
        var parts = [];
        Object.keys(meta).forEach(function (k) {
            if (skip[k]) return;
            var v = meta[k];
            if (v === null || v === undefined || v === '') return;
            if (typeof v === 'object') {
                try { v = JSON.stringify(v); } catch (e) { v = '[对象]'; }
            }
            parts.push(k + '=' + truncate(String(v), 60));
        });
        return parts.length ? parts.join(' · ') : '';
    }

    function buildDetail(entry) {
        var box = make('div', 'mp-detail');

        box.appendChild(kv('类型', TYPE_LABEL[entry.memoryType] || (entry.kind === 'note' ? '当天总结' : entry.kind === 'event' ? '当天事件' : '—')));
        box.appendChild(kv('时间', entry.ts ? fmtFull(entry.ts) : '—'));
        if (entry.weight !== null) box.appendChild(kv('权重', Math.round(entry.weight * 100) + '%'));
        if (entry.source) box.appendChild(kv('来源', entry.source));
        if (entry.mentionCount !== null && entry.mentionCount !== undefined) box.appendChild(kv('提及', entry.mentionCount + ' 次'));
        if (entry.lastSeen) box.appendChild(kv('最近', fmtFull(entry.lastSeen)));

        if (entry.tags.length) {
            var wrap = make('span');
            entry.tags.forEach(function (tag) {
                var chip = make('span', 'mp-tag');
                appendHighlighted(chip, tag, S.query);
                wrap.appendChild(chip);
            });
            box.appendChild(kv('标签', wrap));
        }

        if (entry.id) box.appendChild(kv('ID', entry.id));

        var extra = extraMetadata(entry);
        if (extra) box.appendChild(kv('元数据', extra));

        return box;
    }

    function buildItem(entry, candidates) {
        var row = make('div', 'mp-item');
        row.setAttribute('data-key', entry.selKey);
        row.setAttribute('role', 'listitem');
        if (entry.kind === 'note') row.classList.add('is-note');
        if (S.selected[entry.selKey]) row.classList.add('is-selected');
        if (S.open[entry.selKey]) row.classList.add('is-open');

        // 勾选框
        var cb = make('input', 'mp-check');
        cb.type = 'checkbox';
        cb.checked = !!S.selected[entry.selKey];
        cb.setAttribute('aria-label', '选择这条记忆');
        row.appendChild(cb);

        var main = make('div', 'mp-main');

        // 正文
        var text = make('div', 'mp-text');
        var expanded = !!S.expanded[entry.selKey];
        var maybeLong = entry.text.length > 20 || entry.text.indexOf('\n') !== -1;
        if (maybeLong && !expanded) text.classList.add('is-clamped');
        appendHighlighted(text, entry.text, S.query);
        main.appendChild(text);

        // “展开”按钮先建好（隐藏），等布局完成后按实际是否溢出决定是否显示
        var more = null;
        if (maybeLong) {
            more = make('button', 'mp-more mp-hidden', expanded ? '收起' : '展开');
            more.type = 'button';
            main.appendChild(more);
        }

        main.appendChild(buildMeta(entry));
        main.appendChild(buildDetail(entry));
        row.appendChild(main);

        // 只有带 id 的条目才能删（当天总结/事件是即时生成的，没有 id）
        if (entry.id) {
            var del = make('button', 'mp-del', '×');
            del.type = 'button';
            del.title = '删除这条记忆';
            del.setAttribute('aria-label', '删除这条记忆');
            row.appendChild(del);
        }

        if (maybeLong && candidates) {
            candidates.push({
                textEl: text,
                more: more,
                entry: entry,
                // 折叠分组里量不到高度时的兜底判断
                longGuess: entry.text.length > 40 || entry.text.indexOf('\n') !== -1
            });
        }

        return row;
    }

    function buildGroup(group, arr, candidates) {
        var box = make('div', 'mp-group');
        box.setAttribute('data-group', group.key);
        var collapsed = S.query ? false : !!S.collapsed[group.key];
        if (collapsed) box.classList.add('is-collapsed');

        var head = make('button', 'mp-group-head');
        head.type = 'button';
        head.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
        head.appendChild(make('span', 'mp-chev', '▾'));
        head.appendChild(make('span', 'mp-group-label', group.label));
        head.appendChild(make('span', 'mp-group-hint', group.hint));
        head.appendChild(make('span', 'mp-badge', String(arr.length)));
        box.appendChild(head);

        var body = make('div', 'mp-group-body');
        body.setAttribute('role', 'list');
        body.setAttribute('aria-label', group.label + '记忆');
        arr.forEach(function (entry) { body.appendChild(buildItem(entry, candidates)); });
        box.appendChild(body);
        return box;
    }

    // 分页控件内容（顶部常驻条与列表底部共用同一份结构）
    function fillPager(container) {
        container.textContent = '';

        var prev = make('button', 'mp-icon-btn', '‹ 上一页');
        prev.type = 'button';
        prev.setAttribute('data-mp-page', String(S.page - 1));
        if (S.page <= 0) prev.disabled = true;

        var label = make('span', '', '第 ' + (S.page + 1) + '/' + S.pageCount + ' 页 · 共 ' + S.filtered.length + ' 条');

        var next = make('button', 'mp-icon-btn', '下一页 ›');
        next.type = 'button';
        next.setAttribute('data-mp-page', String(S.page + 1));
        if (S.page >= S.pageCount - 1) next.disabled = true;

        container.appendChild(prev);
        container.appendChild(label);
        container.appendChild(next);
        return container;
    }

    function buildPager() {
        return fillPager(make('div', 'mp-pager mp-pager-bottom'));
    }

    function hideTopPager() {
        if (!S.refs.pagerTop) return;
        S.refs.pagerTop.classList.add('mp-hidden');
        S.refs.pagerTop.textContent = '';
    }

    // 布局后统一测量：一次读、一次写，避免逐条强制重排
    function finalizeClamp() {
        var list = S.clampCandidates;
        S.clampCandidates = null;
        if (!list || !list.length) return;

        var measured = [];
        for (var i = 0; i < list.length; i++) {
            var c = list[i];
            var visible = c.textEl.offsetParent !== null;   // 折叠分组里为 null，量不到
            var clipped;
            if (!visible) clipped = c.longGuess;
            else clipped = c.textEl.scrollHeight > c.textEl.clientHeight + 1;
            measured.push({ c: c, clipped: clipped });
        }

        for (var j = 0; j < measured.length; j++) {
            var item = measured[j];
            var entry = item.c.entry;
            var expanded = !!S.expanded[entry.selKey];
            // 测量不可靠时，超长文本一律给展开按钮
            var needToggle = expanded || item.clipped || entry.text.length > 160;
            if (!item.clipped && !expanded) item.c.textEl.classList.remove('is-clamped');
            if (item.c.more) {
                item.c.more.classList.toggle('mp-hidden', !needToggle);
                item.c.more.textContent = expanded ? '收起' : '展开';
            }
        }
    }

    function paint() {
        if (!S.mounted || !S.refs.list) return;

        S.filtered = applyFilter(S.entries);
        var kind = stateKind();
        updateChrome();

        var list = S.refs.list;
        var keepTop = list.scrollTop;
        list.textContent = '';

        if (kind !== 'list') {
            S.clampCandidates = null;
            hideTopPager();
            list.appendChild(buildState(kind));
            S.rendered = true;
            return;
        }

        var pageItems = paginate(S.filtered);
        var candidates = [];

        var buckets = {};
        GROUPS.forEach(function (g) { buckets[g.key] = []; });
        pageItems.forEach(function (e) {
            (buckets[e.group] || buckets.other).push(e);
        });

        GROUPS.forEach(function (g) {
            var arr = buckets[g.key] || [];
            if (!arr.length && !(S.opts.alwaysShowGroups && !S.query)) return;
            list.appendChild(buildGroup(g, sortEntries(arr), candidates));
        });

        // 分页：顶部常驻条（不用滚到底就能翻页）+ 列表底部一条
        if (S.pageCount > 1) {
            fillPager(S.refs.pagerTop);
            S.refs.pagerTop.classList.remove('mp-hidden');
            list.appendChild(buildPager());
        } else {
            hideTopPager();
        }

        S.clampCandidates = candidates;
        finalizeClamp();

        list.scrollTop = keepTop;
        S.rendered = true;
    }

    function updateChrome() {
        var r = S.refs;
        if (!r || !r.list) return;

        // 条数徽标
        if (r.total) {
            var shown = S.query ? S.filtered.length : S.entries.length;
            if (shown > 0) {
                r.total.textContent = S.query ? (shown + '/' + S.entries.length) : String(shown);
                r.total.classList.remove('mp-hidden');
                r.total.title = S.query ? ('命中 ' + shown + ' 条，共 ' + S.entries.length + ' 条') : ('共 ' + shown + ' 条记忆');
            } else {
                r.total.classList.add('mp-hidden');
            }
        }

        // 刷新中状态
        if (r.btnRefresh) r.btnRefresh.classList.toggle('is-spinning', !!S.loading);

        var hasChar = !!(S.charIdUsed || resolveCharId());
        if (r.btnExport) r.btnExport.disabled = !hasChar;
        if (r.add) r.add.classList.toggle('mp-hidden', !hasChar);

        // 多选导出条
        var count = selectedCount();
        if (r.selbar) r.selbar.classList.toggle('mp-hidden', count === 0);
        if (r.selCount && count) {
            r.selCount.textContent = '';
            r.selCount.appendChild(document.createTextNode('已选 '));
            r.selCount.appendChild(make('b', '', String(count)));
            r.selCount.appendChild(document.createTextNode(' 条'));
        }
    }

    /* ======================= 数据加载 ======================= */

    function refresh() {
        if (!S.mounted) return Promise.resolve(false);

        var charId = resolveCharId();
        S.charIdUsed = charId;

        if (!charId) {
            S.loading = false;
            S.error = '';
            S.entries = [];
            S.byKey = {};
            S.filtered = [];
            S.lastSig = null;
            paint();
            return Promise.resolve(true);
        }

        var seq = ++S.seq;
        S.loading = true;
        if (!S.entries.length) S.error = '';
        updateChrome();
        if (!S.entries.length) paint();   // 首次加载显示骨架；已有数据时静默刷新，不闪

        var url = '/v1/memory/' + encodeURIComponent(resolveUserId()) +
            '?char_id=' + encodeURIComponent(charId);

        return request('GET', url).then(function (payload) {
            if (seq !== S.seq) return false;
            S.loading = false;
            S.error = '';

            var entries = collectEntries(payload);
            var sig = signatureOf(entries);

            S.entries = entries;
            S.byKey = indexByKey(entries);
            S.filtered = applyFilter(entries);
            pruneSelection();

            // 内容没变就不重绘：后台每轮回复后都会刷新一次，避免打断 hover / 滚动。
            // 注意必须用 lastSig !== null 判断「渲染过」，否则空结果会和初始值相等而被跳过。
            if (S.lastSig !== null && sig === S.lastSig && S.rendered) {
                updateChrome();
                return true;
            }
            S.lastSig = sig;
            paint();
            return true;
        }).catch(function (err) {
            if (seq !== S.seq) return false;
            S.loading = false;
            S.error = (err && err.message) || String(err);
            if (S.entries.length) {
                // 已有旧数据：保留列表，只提示
                updateChrome();
                toast('刷新记忆失败，当前显示的是上次结果', 'error');
            } else {
                paint();
            }
            return false;
        });
    }

    /* ======================= 对外 API ======================= */

    function mount(el, opts) {
        if (typeof el === 'string') el = document.querySelector(el);
        if (!el) {
            console.warn('[记忆面板] mount 失败：容器元素不存在');
            return false;
        }
        S.opts = opts || {};
        S.root = el;
        S.mounted = true;
        S.lastSig = null;      // 重新构建 DOM，必须重绘一次

        ensureStyles();
        loadCollapse();
        buildShell();
        bindRootDelegation();

        refresh();
        return true;
    }

    function setChar(charId) {
        S.charId = charId ? String(charId) : '';
        S.selected = {};
        S.page = 0;
        S.lastSig = null;
        if (!S.mounted) return Promise.resolve(false);
        return refresh();
    }

    function unmount() {
        if (S.root) {
            S.root.textContent = '';
            S.root.removeAttribute('data-mp-bound');
        }
        clearTimeout(S.searchTimer);
        clearTimeout(S.tipTimer);
        S.mounted = false;
        S.root = null;
        S.refs = {};
        S.entries = [];
        S.byKey = {};
        S.filtered = [];
        S.selected = {};
        S.lastSig = null;
    }

    function getState() {
        var counts = {};
        GROUPS.forEach(function (g) {
            counts[g.key] = S.filtered.filter(function (e) { return e.group === g.key; }).length;
        });
        return {
            version: VERSION,
            mounted: S.mounted,
            charId: resolveCharId(),
            userId: resolveUserId(),
            total: S.entries.length,
            filtered: S.filtered.length,
            query: S.query,
            page: S.page + 1,
            pageCount: S.pageCount,
            selected: selectedCount(),
            selectedKeys: Object.keys(S.selected),
            loading: S.loading,
            error: S.error,
            groups: counts
        };
    }

    global.QiyuMemoryPanel = {
        version: VERSION,
        mount: mount,
        refresh: refresh,
        setChar: setChar,
        getState: getState,
        unmount: unmount
    };

    // 便于自测：window.QiyuMemoryPanel.__internals
    global.QiyuMemoryPanel.__internals = { constants: { PAGE_THRESHOLD: PAGE_THRESHOLD, PAGE_SIZE: PAGE_SIZE, SEARCH_DEBOUNCE: SEARCH_DEBOUNCE }, groups: GROUPS };

})(window);
