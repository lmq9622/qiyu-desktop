/* =============================================================================
 * 栖语 · 设置页「知识与记忆」合并标签（独立组件，不改 index.html）
 * -----------------------------------------------------------------------------
 * 目标：把原来并列的「知识库(rag)」标签与右栏「记忆(memorySidebar)」入口，
 *       在设置页里合并成一个标签：左侧知识库（文档级）、右侧记忆（条目级），
 *       顶部一个搜索框同时搜两边，结果分两栏并标注「来自知识库 / 来自记忆」。
 *
 * 硬约束：
 *   1) 原生 JS、零依赖；所有用户数据只走 textContent，不拼 innerHTML（防 XSS）；
 *   2) 配色/圆角/阴影全部复用 index.html :root 里的设计变量，自动跟随深色主题；
 *   3) 右侧 #memorySidebar 完全不碰（另一个同事在改），本文件只读 app.state；
 *   4) 旧 rag 标签「隐藏 + 别名重定向」到本标签，老 DOM 区块原样留在页面里，
 *      这样 app.loadRagDocuments() / app.renderRagDocuments() 等旧函数不会空指针。
 *
 * 对外接口：window.QiyuKnowledgeMemory = { mount(el?), refresh(), version }
 * 自测：见 docs/知识与记忆_接入说明.md 第 6 节。
 * ========================================================================== */
(function () {
    'use strict';

    // 防重复注入（热重载 / 页面里误引两次时不会出现两个面板）
    if (window.QiyuKnowledgeMemory && window.QiyuKnowledgeMemory.__km) return;

    var VERSION = '1.0.0';

    // ---- 标签 / 挂载点常量 -------------------------------------------------
    var NEW_TAB = 'knowledge';        // 新标签的 data-tab 值
    var LEGACY_TAB = 'rag';           // 旧「知识库」标签的 data-tab 值
    var MOUNT_ID = 'kmRoot';          // 面板根节点 id
    var WIDE_CLASS = 'km-host-wide';  // 给 .settings-layout 加宽用的类名

    // ---- 后端接口（已按 demo.py 实际路由核对过，详见接入说明第 2 节）--------
    // 注意：任务书里写的 "POST /v1/rag/search" 实际是 GET；"POST /v1/rag" 不存在。
    var EP = {
        docs:      '/v1/rag/documents',                       // GET  -> {data:[{source, chunks}]}
        indexAll:  '/v1/rag/index',                           // POST -> {success, total_chunks}
        indexOne:  '/v1/rag/index?path=',                     // POST + 路径（仅 data 目录内）
        kbSearch:  '/v1/rag/search',                          // GET  ?query=&top_k=
        mem:       function (u) { return '/v1/memory/' + encodeURIComponent(u); },
        memItem:   function (u, id) {
            return '/v1/memory/' + encodeURIComponent(u) + '/' + encodeURIComponent(id);
        },
        memSummary: function (u) { return '/v1/memory/' + encodeURIComponent(u) + '/summary'; },
        memClear:   function (u) { return '/v1/memory/' + encodeURIComponent(u) + '/clear'; }
    };

    // 知识库「删除文档」：后端起没起这条路由，先探一次再决定按钮长相（见 probeDocDelete）
    var DOC_DELETE_PROBED = false;    // 是否已探测
    var DOC_DELETE_SUPPORTED = false; // 后端是否支持按 source 删除文档

    // =========================================================================
    // 0. 极小的工具函数
    // =========================================================================

    /** 创建元素：className + textContent（用户数据只走 textContent） */
    function el(tag, cls, text) {
        var n = document.createElement(tag);
        if (cls) n.className = cls;
        if (text !== undefined && text !== null && text !== '') n.textContent = String(text);
        return n;
    }

    /** 清空子节点 */
    function clear(node) {
        if (!node) return node;
        while (node.firstChild) node.removeChild(node.firstChild);
        return node;
    }

    /** 取宿主的 app 对象：index.html 里是 `const app = {...}`（全局词法作用域，
     *  不是 window.app），所以只能用裸标识符 + typeof 探测。 */
    function getApp() {
        try {
            /* eslint-disable no-undef */
            if (typeof app === 'object' && app) return app;
            /* eslint-enable no-undef */
        } catch (e) { /* 未定义时直接忽略 */ }
        return null;
    }

    /** 统一走宿主的 app.api（沿用它的 API_BASE 与错误格式）；宿主不可用时退化成 fetch */
    function api(method, path, body) {
        var A = getApp();
        if (A && typeof A.api === 'function') return A.api(method, path, body || null);
        var opts = { method: method, headers: { 'Content-Type': 'application/json' } };
        if (body) opts.body = JSON.stringify(body);
        return fetch(path, opts).then(function (r) {
            if (!r.ok) return r.text().then(function (t) { throw new Error('HTTP ' + r.status + ': ' + t); });
            if (r.status === 204) return null;
            return r.json();
        });
    }

    /** 复用宿主的 toast；没有就退化成 console */
    function toast(msg, type) {
        var A = getApp();
        if (A && typeof A.toast === 'function') { A.toast(msg, type || 'info'); return; }
        if (type === 'error') console.warn('[知识与记忆]', msg); else console.log('[知识与记忆]', msg);
    }

    /** 当前记忆作用域：会话用户 + 角色（与右栏 #memorySidebar 用的是同一套 state） */
    function scope() {
        var A = getApp();
        var st = (A && A.state) || {};
        var ch = st.currentCharacter || {};
        return {
            user: st.sessionUserId || 'web_user',
            charId: ch.id || '',
            charName: ch.name || ''
        };
    }

    /** char_id 查询串 */
    function charQuery() {
        return '?char_id=' + encodeURIComponent(scope().charId);
    }

    /** 时间显示：沿用宿主的 时:分 风格，超过今天则带上月-日 */
    function fmtWhen(ts) {
        if (!ts) return '';
        var d = new Date(ts);
        if (isNaN(d.getTime())) return '';
        var now = new Date();
        var sameDay = d.getFullYear() === now.getFullYear() &&
                      d.getMonth() === now.getMonth() &&
                      d.getDate() === now.getDate();
        var hhmm = d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
        if (sameDay) return hhmm;
        return (d.getMonth() + 1) + '-' + d.getDate() + ' ' + hhmm;
    }

    /** 知识库 source 是服务器上的完整路径，界面只显示文件名，全路径留在 title 里 */
    function baseName(p) {
        var s = String(p || '');
        if (!s) return '未知文档';
        var i = Math.max(s.lastIndexOf('/'), s.lastIndexOf('\\'));
        return i >= 0 ? s.slice(i + 1) : s;
    }

    /** 当天记忆占位文案（后端没总结出来时的提示）识别：
     *  "今天聊了 0 条，还不足以总结" / "今天已聊 5 条，正在整理当天话题总结…" */
    function isDayPlaceholder(text) {
        return /^今天(已)?聊了?\s*\d+\s*条/.test(String(text || '').trim());
    }

    // =========================================================================
    // 1. 数据层：一次拉齐两边数据，渲染只读这份快照
    // =========================================================================
    var store = {
        docs: [],                       // [{source, chunks}]
        mem: { day: [], events: [], short: [], long: [] },
        hits: { kb: [], mem: [] },      // 联合搜索的两栏结果
        query: '',
        loading: { docs: false, mem: false },
        errors: { docs: '', mem: '' },
        firstLoad: { docs: true, mem: true }
    };

    var dom = {};   // 骨架里会用到的节点引用，mount() 时填好

    // =========================================================================
    // 2. 骨架：只建一次，之后 refresh() 只重画列表
    // =========================================================================
    function buildSkeleton(root) {
        clear(root);
        root.classList.add('km-wrap');

        // ---- 顶部一行说明（产品稿要求的那句） ----
        var lede = el('div', 'km-lede');
        lede.appendChild(el('strong', null, '知识库')); 
        lede.appendChild(el('span', null, ' = 你上传的资料；'));
        lede.appendChild(el('strong', null, '记忆'));
        lede.appendChild(el('span', null, ' = ta 自己攒下来的。'));
        lede.appendChild(el('span', 'km-lede-tail', '一个搜索框，两边一起搜。'));
        root.appendChild(lede);

        // ---- 顶部搜索条（跨知识库 + 记忆） ----
        var bar = el('div', 'km-search');
        dom.input = el('input', 'form-input km-input');
        dom.input.type = 'text';
        dom.input.id = 'kmQueryInput';
        dom.input.placeholder = '搜索知识库和记忆…（回车）';
        dom.input.setAttribute('aria-label', '搜索知识库和记忆');
        dom.input.addEventListener('keydown', function (ev) {
            if (ev.key === 'Enter') { ev.preventDefault(); runSearch(); }
            if (ev.key === 'Escape') { resetSearch(); }
        });

        dom.searchBtn = el('button', 'btn btn-primary', '搜索');
        dom.searchBtn.type = 'button';
        dom.searchBtn.addEventListener('click', runSearch);

        dom.resetBtn = el('button', 'btn btn-ghost btn-sm', '清除');
        dom.resetBtn.type = 'button';
        dom.resetBtn.title = '清空搜索，回到两栏完整列表';
        dom.resetBtn.addEventListener('click', resetSearch);

        dom.searchState = el('span', 'km-search-state');

        bar.appendChild(dom.input);
        bar.appendChild(dom.searchBtn);
        bar.appendChild(dom.resetBtn);
        bar.appendChild(dom.searchState);
        root.appendChild(bar);

        // ---- 搜索结果（分两栏，各自标注来源） ----
        dom.hits = el('div', 'km-hits');
        dom.hits.hidden = true;

        var hitKbCol = el('section', 'km-hit-col');
        var hitKbHead = el('h4', 'km-hit-title');
        hitKbHead.appendChild(el('span', 'km-src-badge km-src-kb', '来自知识库'));
        dom.hitKbCount = el('span', 'km-count');
        hitKbHead.appendChild(dom.hitKbCount);
        dom.hitKb = el('div', 'km-list');
        hitKbCol.appendChild(hitKbHead);
        hitKbCol.appendChild(dom.hitKb);

        var hitMemCol = el('section', 'km-hit-col');
        var hitMemHead = el('h4', 'km-hit-title');
        hitMemHead.appendChild(el('span', 'km-src-badge km-src-mem', '来自记忆'));
        dom.hitMemCount = el('span', 'km-count');
        hitMemHead.appendChild(dom.hitMemCount);
        dom.hitMem = el('div', 'km-list');
        hitMemCol.appendChild(hitMemHead);
        hitMemCol.appendChild(dom.hitMem);

        dom.hits.appendChild(hitKbCol);
        dom.hits.appendChild(hitMemCol);
        root.appendChild(dom.hits);

        // ---- 两栏主体 ----
        var cols = el('div', 'km-columns');

        // ==== 左：知识库（文档级） ====
        var kbPanel = el('section', 'km-panel km-panel-kb');
        var kbHead = el('header', 'km-panel-head');
        var kbTitleWrap = el('div', 'km-panel-titlewrap');
        var kbTitle = el('h3', 'km-panel-title', '知识库');
        kbTitleWrap.appendChild(kbTitle);
        kbTitleWrap.appendChild(el('span', 'km-badge', '文档级'));
        dom.docCount = el('span', 'km-count');
        kbTitleWrap.appendChild(dom.docCount);

        var kbTools = el('div', 'km-panel-tools');

        // 角色切换：知识与记忆是**按角色隔离**的，不是全局
        dom.charSel = el('select', 'form-select km-char');
        dom.charSel.style.cssText = 'font-size:.78rem;padding:4px 8px;max-width:150px;';
        dom.charSel.title = '切换角色：知识库与记忆都按角色隔离';
        dom.charSel.addEventListener('change', function () {
            var id = dom.charSel.value;
            if (id && window.app && app.state && app.state.characters) {
                var ch = app.state.characters.filter(function (c) { return c.id === id; })[0];
                if (ch) app.state.currentCharacter = ch;
            }
            if (typeof refresh === 'function') refresh();
        });
        kbTools.appendChild(dom.charSel);

        // 只支持本项目的 .qy3f 角色文件（不支持 txt/md/pdf 等其他格式）。
        var impBtn = el('button', 'btn btn-ghost btn-sm', '导入角色');
        impBtn.type = 'button';
        impBtn.title = '导入 .qy3f 角色文件（角色卡 + 记忆 + 知识库 + 世界状态）；其他格式不支持';
        impBtn.addEventListener('click', importQy3f);
        kbTools.appendChild(impBtn);

        var expBtn = el('button', 'btn btn-secondary btn-sm', '导出角色');
        expBtn.type = 'button';
        expBtn.title = '把当前角色导出为 .qy3f（不加密，可带走）';
        expBtn.addEventListener('click', exportQy3f);
        kbTools.appendChild(expBtn);

        kbHead.appendChild(kbTitleWrap);
        kbHead.appendChild(kbTools);
        kbPanel.appendChild(kbHead);

        dom.docList = el('div', 'km-list km-doclist');
        kbPanel.appendChild(dom.docList);
        cols.appendChild(kbPanel);

        // ==== 右：记忆（条目级） ====
        var memPanel = el('section', 'km-panel km-panel-mem');
        var memHead = el('header', 'km-panel-head');
        var memTitleWrap = el('div', 'km-panel-titlewrap');
        memTitleWrap.appendChild(el('h3', 'km-panel-title', '记忆'));
        memTitleWrap.appendChild(el('span', 'km-badge', '条目级'));
        dom.memCount = el('span', 'km-count');
        memTitleWrap.appendChild(dom.memCount);
        dom.memScope = el('span', 'km-scope');
        memTitleWrap.appendChild(dom.memScope);

        var memTools = el('div', 'km-panel-tools');
        dom.summaryBtn = el('button', 'btn btn-secondary btn-sm', '生成摘要');
        dom.summaryBtn.type = 'button';
        dom.summaryBtn.title = '把最近的对话压成摘要写进长期记忆（与右栏「生成摘要」同一条接口）';
        dom.summaryBtn.addEventListener('click', generateSummary);
        memTools.appendChild(dom.summaryBtn);
        memHead.appendChild(memTitleWrap);
        memHead.appendChild(memTools);
        memPanel.appendChild(memHead);

        dom.memList = el('div', 'km-list km-memlist');
        memPanel.appendChild(dom.memList);

        // 手动加一条记忆
        var memAdd = el('div', 'km-memadd');
        dom.memText = el('textarea', 'form-textarea km-memtext');
        dom.memText.rows = 2;
        dom.memText.placeholder = '手动给 ta 记一条（例如：我养了一只叫豆豆的猫）';
        dom.memText.setAttribute('aria-label', '新增记忆内容');
        dom.memText.addEventListener('keydown', function (ev) {
            if (ev.key === 'Enter' && (ev.ctrlKey || ev.metaKey)) { ev.preventDefault(); addMemory(); }
        });
        dom.memAddBtn = el('button', 'btn btn-primary btn-sm', '添加记忆');
        dom.memAddBtn.type = 'button';
        dom.memAddBtn.addEventListener('click', addMemory);
        memAdd.appendChild(dom.memText);
        memAdd.appendChild(dom.memAddBtn);
        memPanel.appendChild(memAdd);

        cols.appendChild(memPanel);
        root.appendChild(cols);

        // 给关键控件补稳定 id：方便自测脚本/CDP 定位，也方便同事在控制台里点
        var KM_IDS = {
            input: 'kmQueryInput', searchBtn: 'kmSearchBtn', resetBtn: 'kmResetBtn',
            searchState: 'kmSearchState', hits: 'kmSearchHits',
            hitKb: 'kmHitKb', hitMem: 'kmHitMem',
            docCount: 'kmDocCount', indexBtn: 'kmIndexBtn',
            addToggle: 'kmAddToggle', addBody: 'kmAddBody',
            pathInput: 'kmPathInput', pathBtn: 'kmPathIndexBtn', docList: 'kmDocList',
            memCount: 'kmMemCount', memScope: 'kmMemScope', summaryBtn: 'kmSummaryBtn',
            memList: 'kmMemList', memText: 'kmMemText', memAddBtn: 'kmMemAddBtn'
        };
        Object.keys(KM_IDS).forEach(function (k) {
            if (dom[k] && !dom[k].id) dom[k].id = KM_IDS[k];
        });

        // 底部一行：作用域/口径说明（谁写进来的、怎么删）
        var foot = el('p', 'km-foot',
            '知识库按文档切片存，检索走 /v1/rag/search；记忆按条目存，分当天 / 短期 / 长期三档，' +
            '随角色隔离（当前角色的记忆只有这个角色看得见）。');
        root.appendChild(foot);
    }

    // =========================================================================
    // 3. 渲染：知识库文档列表
    // =========================================================================
    function renderDocs() {
        if (!dom.docList) return;
        clear(dom.docList);
        dom.docCount.textContent = store.docs.length ? String(store.docs.length) + ' 份' : '';

        if (store.errors.docs) {
            var err = el('div', 'km-empty km-empty-err');
            err.appendChild(el('div', 'km-empty-title', '知识库没加载出来'));
            err.appendChild(el('div', 'km-empty-sub', store.errors.docs));
            var retry = el('button', 'btn btn-secondary btn-sm', '重试');
            retry.type = 'button';
            retry.addEventListener('click', function () { loadDocs(); });
            err.appendChild(retry);
            dom.docList.appendChild(err);
            return;
        }

        if (!store.docs.length) {
            // 空态（知识库）：和记忆的空态文案明确区分
            var empty = el('div', 'km-empty');
            empty.appendChild(el('div', 'km-empty-ico', '📄'));
            empty.appendChild(el('div', 'km-empty-title', store.loading.docs ? '正在读取…' : '还没上传任何资料'));
            empty.appendChild(el('div', 'km-empty-sub',
                store.loading.docs ? '' : '把文件放进 ./data/uploads 后点右上角「重新索引」，ta 就能查到了'));
            dom.docList.appendChild(empty);
            return;
        }

        // 按来源稳定排序，避免每次刷新顺序乱跳
        var docs = store.docs.slice().sort(function (a, b) {
            return String(a.source || '').localeCompare(String(b.source || ''));
        });

        docs.forEach(function (doc) {
            var card = el('div', 'km-card km-card-doc');
            var main = el('div', 'km-card-main');
            var title = el('div', 'km-card-title', baseName(doc.source));   // textContent
            title.title = String(doc.source || '');                        // 全路径放 title
            main.appendChild(title);
            var meta = el('div', 'km-card-meta');
            meta.appendChild(el('span', null, (Number(doc.chunks) || 0) + ' 个切片'));
            meta.appendChild(el('span', 'km-doc-open', '点开看内容 ›'));
            main.appendChild(meta);
            // 点标题/正文展开该文档的全部切片（之前点了没反应）
            main.style.cursor = 'pointer';
            main.addEventListener('click', function () { toggleDocChunks(card, doc.source); });
            card.appendChild(main);

            var acts = el('div', 'km-card-acts');
            var del = el('button', 'km-x', '×');
            del.type = 'button';
            del.title = DOC_DELETE_SUPPORTED
                ? '删除这份文档的索引'
                : '后端暂无删除文档的接口（见接入说明附录 B）；现在请删掉 data/uploads 里的文件后重新索引';
            del.addEventListener('click', function (e) {
                if (e && e.stopPropagation) e.stopPropagation();
                deleteDoc(doc.source);
            });
            acts.appendChild(del);
            card.appendChild(acts);
            dom.docList.appendChild(card);
        });
    }

    /** 展开/收起某份文档的切片（再点一次收起） */
    function toggleDocChunks(card, source) {
        var old = card.querySelector('.km-doc-chunks');
        if (old) { old.parentNode.removeChild(old); return; }

        var box = el('div', 'km-doc-chunks');
        box.appendChild(el('div', 'km-doc-loading', '正在读取…'));
        card.appendChild(box);

        api('GET', '/v1/rag/document?source=' + encodeURIComponent(source)).then(function (d) {
            box.textContent = '';
            var rows = (d && d.data) || [];
            if (!rows.length) {
                box.appendChild(el('div', 'km-doc-loading', '（没有切片内容）'));
                return;
            }
            var head = el('div', 'km-doc-chunks-head',
                          '共 ' + rows.length + ' 个切片（按顺序）');
            box.appendChild(head);
            rows.forEach(function (r) {
                var item = el('div', 'km-doc-chunk');
                item.appendChild(el('div', 'km-doc-chunk-idx', '#' + (r.chunk_index != null ? r.chunk_index : '?')));
                item.appendChild(el('div', 'km-doc-chunk-text', String(r.text || '')));
                box.appendChild(item);
            });
        }).catch(function (e) {
            box.textContent = '';
            box.appendChild(el('div', 'km-doc-loading',
                               '读取失败：' + (e && e.message ? e.message : e)));
        });
    }

    // =========================================================================
    // 4. 渲染：记忆三档
    // =========================================================================
    function groupTitle(text, sub) {
        var g = el('div', 'km-group-title');
        g.appendChild(el('span', 'km-group-name', text));
        if (sub) g.appendChild(el('span', 'km-group-sub', sub));
        return g;
    }

    /** 单条记忆卡片（短期 / 长期 / 命中结果通用） */
    function memCard(item, tierLabel) {
        var card = el('div', 'km-card km-card-mem');
        var main = el('div', 'km-card-main');
        var text = el('div', 'km-card-text', String(item.text || ''));
        main.appendChild(text);

        var meta = el('div', 'km-card-meta');
        if (tierLabel) meta.appendChild(el('span', 'km-tier', tierLabel));
        var when = fmtWhen(item.created_at);
        if (when) meta.appendChild(el('span', null, when));
        var w = item.metadata && item.metadata.weight;
        if (w !== undefined && w !== null && w !== '') {
            var wnum = parseFloat(w);
            if (!isNaN(wnum)) meta.appendChild(el('span', null, '权重 ' + Math.round(wnum * 100)));
        }
        if (typeof item.similarity === 'number') {
            meta.appendChild(el('span', null, '相似度 ' + item.similarity.toFixed(3)));
        }
        (item.tags || []).forEach(function (t) {
            meta.appendChild(el('span', 'km-tag', String(t)));
        });
        main.appendChild(meta);
        card.appendChild(main);

        // 没有 id 的条目（比如当天的对话总结）不给删除按钮——后端删不了，别放无效按钮
        if (item && item.id) {
            var acts = el('div', 'km-card-acts');
            var del = el('button', 'km-x', '×');
            del.type = 'button';
            del.title = '删除这条记忆';
            del.addEventListener('click', function () { deleteMemory(item); });
            acts.appendChild(del);
            card.appendChild(acts);
        }
        return card;
    }

    function renderMemory() {
        if (!dom.memList) return;
        clear(dom.memList);

        var s = scope();
        dom.memScope.textContent = s.charName ? '角色：' + s.charName : '全局（未选角色）';

        var day = store.mem.day || [];
        var events = store.mem.events || [];
        var short = store.mem.short || [];
        var long = store.mem.long || [];

        // 后端总会给一条「今天聊了 N 条…」占位，别把它当记忆条目
        var dayReal = [];
        var dayHold = '';
        day.forEach(function (it) {
            var t = String((it && (it.text || it.content)) || '');
            if (!t) return;
            if (isDayPlaceholder(t)) { if (!dayHold) dayHold = t; }
            else dayReal.push({ text: t, created_at: it.timestamp || null, _tier: '当天' });
        });

        var total = dayReal.length + events.length + short.length + long.length;
        dom.memCount.textContent = total ? String(total) + ' 条' : '';

        if (store.errors.mem) {
            var err = el('div', 'km-empty km-empty-err');
            err.appendChild(el('div', 'km-empty-title', '记忆没加载出来'));
            err.appendChild(el('div', 'km-empty-sub', store.errors.mem));
            var retry = el('button', 'btn btn-secondary btn-sm', '重试');
            retry.type = 'button';
            retry.addEventListener('click', function () { loadMemory(); });
            err.appendChild(retry);
            dom.memList.appendChild(err);
            return;
        }

        if (!total) {
            // 空态（记忆）：与「还没上传任何资料」区分开
            var empty = el('div', 'km-empty');
            empty.appendChild(el('div', 'km-empty-title', store.loading.mem ? '正在读取…' : 'ta 还什么都没记住'));
            empty.appendChild(el('div', 'km-empty-sub',
                store.loading.mem ? '' : '等 ta 在聊天里攒下东西就会出现在这里；也可以下面手动加一条'));
            if (dayHold) empty.appendChild(el('div', 'km-empty-note', dayHold));
            dom.memList.appendChild(empty);
            return;
        }

        // 「当天」：有真正的总结才起分组标题；只有后端占位文案时，当一条灰色提示挂着，
        // 免得出现「当天」标题下面空荡荡的观感
        if (dayReal.length) {
            dom.memList.appendChild(groupTitle('当天', dayReal.length + ' 条'));
            dayReal.forEach(function (it) { dom.memList.appendChild(memCard(it, '当天')); });
        } else if (dayHold) {
            dom.memList.appendChild(el('div', 'km-hold', dayHold));
        }

        if (events.length) {
            dom.memList.appendChild(groupTitle('当天事件', '带权重，影响后续归纳'));
            events.forEach(function (ev) {
                var card = el('div', 'km-card km-card-mem');
                var main = el('div', 'km-card-main');
                main.appendChild(el('div', 'km-card-text', String(ev.text || '')));
                var meta = el('div', 'km-card-meta');
                var w = parseFloat(ev.weight);
                if (!isNaN(w)) {
                    var pct = Math.round(w * 100);
                    meta.appendChild(el('span', 'km-tier km-tier-w' + (pct >= 70 ? 'high' : pct >= 50 ? 'mid' : 'low'), pct + '%'));
                }
                main.appendChild(meta);
                card.appendChild(main);
                dom.memList.appendChild(card);
            });
        }

        if (short.length) {
            dom.memList.appendChild(groupTitle('短期记忆', '会随时间淡忘'));
            short.forEach(function (m) { dom.memList.appendChild(memCard(m, '短期')); });
        }

        if (long.length) {
            dom.memList.appendChild(groupTitle('长期记忆', '永久保留'));
            long.forEach(function (m) { dom.memList.appendChild(memCard(m, '长期')); });
        }
    }

    // =========================================================================
    // 5. 联合搜索：一个框搜两边，结果分两栏
    // =========================================================================
    function setSearchState(text) {
        if (dom.searchState) dom.searchState.textContent = text || '';
    }

    function renderHits() {
        if (!dom.hits) return;
        var q = store.query;
        if (!q) {
            dom.hits.hidden = true;
            clear(dom.hitKb); clear(dom.hitMem);
            return;
        }
        dom.hits.hidden = false;

        // ---- 左栏：知识库命中 ----
        clear(dom.hitKb);
        dom.hitKbCount.textContent = String(store.hits.kb.length);
        if (!store.hits.kb.length) {
            dom.hitKb.appendChild(el('div', 'km-empty-sm', '知识库里没有匹配「' + q + '」的内容'));
        } else {
            store.hits.kb.forEach(function (r) {
                var card = el('div', 'km-card km-card-hit');
                var main = el('div', 'km-card-main');
                main.appendChild(el('div', 'km-card-text', String(r.text || '')));
                var meta = el('div', 'km-card-meta');
                var src = (r.metadata && r.metadata.source) || r.file_name || '未知来源';
                var srcEl = el('span', null, baseName(src));
                srcEl.title = String(src);
                meta.appendChild(srcEl);
                if (typeof r.similarity === 'number') meta.appendChild(el('span', null, '相似度 ' + r.similarity.toFixed(3)));
                else if (typeof r.score === 'number') meta.appendChild(el('span', null, '得分 ' + r.score.toFixed(3)));
                main.appendChild(meta);
                card.appendChild(main);
                dom.hitKb.appendChild(card);
            });
        }

        // ---- 右栏：记忆命中（本地过滤已拉取的快照，这样能保住每条的分层标签） ----
        clear(dom.hitMem);
        dom.hitMemCount.textContent = String(store.hits.mem.length);
        if (!store.hits.mem.length) {
            dom.hitMem.appendChild(el('div', 'km-empty-sm', '记忆里没有匹配「' + q + '」的内容'));
        } else {
            store.hits.mem.forEach(function (m) {
                dom.hitMem.appendChild(memCard(m, m._tier));
            });
        }
    }

    /** 记忆侧过滤：在已拉取的快照里按子串匹配，命中项带上分层标签 */
    function filterMemory(q) {
        var needle = String(q || '').toLowerCase();
        var out = [];
        var push = function (arr, tier, pick) {
            (arr || []).forEach(function (it) {
                var text = pick ? pick(it) : String(it.text || '');
                if (!text) return;
                if (text.toLowerCase().indexOf(needle) < 0) return;
                var copy = {};
                for (var k in it) if (Object.prototype.hasOwnProperty.call(it, k)) copy[k] = it[k];
                copy.text = text;
                copy._tier = tier;
                out.push(copy);
            });
        };
        push(store.mem.day, '当天', function (it) {
            var t = String((it && (it.text || it.content)) || '');
            return isDayPlaceholder(t) ? '' : t;
        });
        push(store.mem.events, '当天事件');
        push(store.mem.short, '短期');
        push(store.mem.long, '长期');
        return out;
    }

    function runSearch() {
        var q = (dom.input.value || '').trim();
        if (!q) { resetSearch(); return; }
        store.query = q;
        setSearchState('搜索中…');

        var kbJob = api('GET', EP.kbSearch + '?query=' + encodeURIComponent(q) + '&top_k=8')
            .then(function (d) {
                store.hits.kb = (d && d.data) || [];
            })
            .catch(function (e) {
                store.hits.kb = [];
                toast('知识库搜索失败：' + (e && e.message ? e.message : e), 'error');
            });

        // 记忆侧：若快照为空先补拉一次，再本地过滤（避免和右栏搜索口径不一致）
        var memJob = (store.mem.short.length || store.mem.long.length || (store.mem.day || []).length)
            ? Promise.resolve()
            : loadMemory().catch(function () {});
        memJob = memJob.then(function () {
            store.hits.mem = filterMemory(q);
        });

        Promise.all([kbJob, memJob]).then(function () {
            setSearchState('知识库 ' + store.hits.kb.length + ' 条 · 记忆 ' + store.hits.mem.length + ' 条');
            renderHits();
        });
    }

    function resetSearch() {
        store.query = '';
        store.hits.kb = [];
        store.hits.mem = [];
        if (dom.input) dom.input.value = '';
        setSearchState('');
        renderHits();
    }

    // =========================================================================
    // 6. 动作：知识库
    // =========================================================================
    function loadDocs() {
        store.loading.docs = true;
        if (store.firstLoad.docs) renderDocs();
        return api('GET', EP.docs).then(function (d) {
            store.docs = (d && d.data) || [];
            store.errors.docs = '';
        }).catch(function (e) {
            store.docs = [];
            store.errors.docs = (e && e.message) ? e.message : String(e);
        }).then(function () {
            store.loading.docs = false;
            store.firstLoad.docs = false;
            renderDocs();
        });
    }

    function reindexAll() {
        dom.indexBtn.disabled = true;
        dom.indexBtn.textContent = '索引中…';
        api('POST', EP.indexAll).then(function (d) {
            var n = (d && (d.total_chunks || d.chunks)) || 0;
            toast('索引完成，共 ' + n + ' 个切片', 'success');
        }).catch(function (e) {
            toast('索引失败：' + (e && e.message ? e.message : e), 'error');
        }).then(function () {
            dom.indexBtn.disabled = false;
            dom.indexBtn.textContent = '重新索引';
            return loadDocs();
        });
    }

    function toggleAdd() {
        dom.addBody.hidden = !dom.addBody.hidden;
        dom.addToggle.textContent = dom.addBody.hidden ? '＋ 添加资料' : '－ 收起';
        if (!dom.addBody.hidden && dom.pathInput) dom.pathInput.focus();
    }

    function indexOne() {
        var p = (dom.pathInput.value || '').trim();
        if (!p) { toast('请先填一个文件路径', 'warning'); return; }
        dom.pathBtn.disabled = true;
        api('POST', EP.indexOne + encodeURIComponent(p)).then(function (d) {
            var n = (d && (d.chunks || d.total_chunks)) || 0;
            toast(n ? ('已索引 ' + n + ' 个切片') : '这个文件没有可索引内容（或已是最新）', n ? 'success' : 'warning');
            dom.pathInput.value = '';
            return loadDocs();
        }).catch(function (e) {
            var msg = (e && e.message) ? e.message : String(e);
            // 后端只允许索引 data 目录内的路径（403），把这条规则说清楚
            if (msg.indexOf('403') >= 0) toast('只能索引 data 目录内的文件；放到 ./data/uploads 再试', 'error');
            else toast('索引失败：' + msg, 'error');
        }).then(function () {
            dom.pathBtn.disabled = false;
        });
    }

    // =========================================================================
    // 人格文件（qy3f）导入 / 导出 + 角色切换
    // =========================================================================
    function currentCharId() {
        if (dom.charSel && dom.charSel.value) return dom.charSel.value;
        var ch = (window.app && app.state && app.state.currentCharacter) || null;
        return ch ? ch.id : '';
    }

    function renderCharSelect() {
        if (!dom.charSel) return;
        var chars = (window.app && app.state && app.state.characters) || [];
        if (!chars.length) {
            // 角色列表还没加载完（面板比 app.init 先跑到）→ 自己拉一次补上
            api('GET', '/v1/characters').then(function (d) {
                var list = (d && d.data) || [];
                if (list.length && window.app && app.state) app.state.characters = list;
                _fillCharSelect(list);
            }).catch(function () { _fillCharSelect([]); });
            return;
        }
        _fillCharSelect(chars);
    }

    function _fillCharSelect(chars) {
        if (!dom.charSel) return;
        var cur = (window.app && app.state && app.state.currentCharacter) || null;
        var curId = cur ? cur.id : '';
        dom.charSel.textContent = '';
        var all = document.createElement('option');
        all.value = '';
        all.textContent = '全部角色';
        dom.charSel.appendChild(all);
        (chars || []).forEach(function (c) {
            var o = document.createElement('option');
            o.value = c.id;
            o.textContent = c.name || c.id;
            if (c.id === curId) o.selected = true;
            dom.charSel.appendChild(o);
        });
    }

    function importQy3f() {
        var input = document.createElement('input');
        input.type = 'file';
        input.accept = '.qy3f';
        input.onchange = function () {
            var f = input.files && input.files[0];
            if (!f) return;
            var reader = new FileReader();
            reader.onload = function () {
                var b64 = String(reader.result || '').split(',')[1] || '';
                api('POST', '/v1/characters/qy3/import', { data_base64: b64, replace: false })
                    .then(function (d) {
                        toast('已导入：' + (d && d.name ? d.name : '角色'), 'success');
                        if (window.app && app.loadCharacters) app.loadCharacters();
                        loadDocs();
                        loadMemory();
                    })
                    .catch(function (e) {
                        toast('导入失败：' + (e && e.message ? e.message : e), 'error');
                    });
            };
            reader.readAsDataURL(f);
        };
        input.click();
    }

    function exportQy3f() {
        var id = currentCharId();
        if (!id) { toast('先在右上角选一个角色再导出', 'warning'); return; }
        var a = document.createElement('a');
        a.href = '/v1/characters/' + encodeURIComponent(id) + '/export.qy3';
        a.download = id + '.qy3f';
        a.style.display = 'none';
        document.body.appendChild(a);
        a.click();
        setTimeout(function () { if (a.parentNode) a.remove(); }, 800);
        toast('正在导出 .qy3f（人设 + 记忆 + 知识库）', 'success');
    }

    function deleteDoc(source) {
        var name = baseName(source);
        // 用自家对话框（window.QyuDialog），没有就退回原生 confirm
        var ask = (window.QyuDialog && window.QyuDialog.confirm)
            ? window.QyuDialog.confirm('删除「' + name + '」的索引？（文件本身不会被删）',
                  { title: '删除索引', danger: true, okText: '删除' })
            : Promise.resolve(window.confirm('删除「' + name + '」的索引？（文件本身不会被删）'));

        ask.then(function (ok) {
            if (!ok) return;
            // 后端目前没有这条路由（demo.py 只有 /v1/rag/documents 的 GET），先试一次再给准确提示：
            // 后端补上 DELETE /v1/rag/documents?source= 之后，这里无需再改就能用。
            api('DELETE', EP.docs + '?source=' + encodeURIComponent(source)).then(function () {
                toast('已删除「' + name + '」的索引', 'success');
                return loadDocs();
            }).catch(function (e) {
                var msg = (e && e.message) ? e.message : String(e);
                if (msg.indexOf('405') >= 0 || msg.indexOf('404') >= 0) {
                    toast('后端还没有删除文档的接口，请删掉 ./data/uploads 里的文件后重新索引', 'warning');
                } else {
                    toast('删除失败：' + msg, 'error');
                }
            });
        });
    }

    // =========================================================================
    // 7. 动作：记忆
    // =========================================================================
    function loadMemory() {
        var s = scope();
        store.loading.mem = true;
        if (store.firstLoad.mem) renderMemory();
        return api('GET', EP.mem(s.user) + charQuery()).then(function (d) {
            store.mem.day = (d && d.day) || [];
            store.mem.events = (d && d.events) || [];
            store.mem.short = (d && d.short) || [];
            store.mem.long = (d && d.long) || [];
            store.errors.mem = '';
        }).catch(function (e) {
            store.mem = { day: [], events: [], short: [], long: [] };
            store.errors.mem = (e && e.message) ? e.message : String(e);
        }).then(function () {
            store.loading.mem = false;
            store.firstLoad.mem = false;
            renderMemory();
        });
    }

    function addMemory() {
        var text = (dom.memText.value || '').trim();
        if (!text) { toast('先写点什么再添加', 'warning'); return; }
        var s = scope();
        dom.memAddBtn.disabled = true;
        api('POST', EP.mem(s.user) + charQuery(), { text: text, tags: [] }).then(function () {
            dom.memText.value = '';
            toast('记忆已添加', 'success');
            return loadMemory();
        }).catch(function (e) {
            toast('添加失败：' + (e && e.message ? e.message : e), 'error');
        }).then(function () {
            dom.memAddBtn.disabled = false;
        });
    }

    function deleteMemory(item) {
        var id = item && item.id;
        if (!id) { toast('这条记忆没有 id，无法删除', 'warning'); return; }
        var preview = String(item.text || '').slice(0, 30);
        var msg = '删除这条记忆？\n\n' + preview + (String(item.text || '').length > 30 ? '…' : '');
        var ask = (window.QyuDialog && window.QyuDialog.confirm)
            ? window.QyuDialog.confirm(msg, { title: '删除记忆', danger: true, okText: '删除' })
            : Promise.resolve(window.confirm(msg));

        ask.then(function (ok) {
            if (!ok) return;
            var s = scope();
            api('DELETE', EP.memItem(s.user, id) + charQuery()).then(function () {
                toast('记忆已删除', 'success');
                return loadMemory();
            }).catch(function (e) {
                toast('删除失败：' + (e && e.message ? e.message : e), 'error');
            });
        });
    }

    function generateSummary() {
        var s = scope();
        dom.summaryBtn.disabled = true;
        dom.summaryBtn.textContent = '生成中…';
        api('POST', EP.memSummary(s.user) + charQuery()).then(function (d) {
            if (d && d.success === false) toast(d.message || '历史对话还不够，生成不了摘要', 'warning');
            else toast(d && d.summary ? '摘要已生成并写入长期记忆' : '摘要已生成', 'success');
            return loadMemory();
        }).catch(function (e) {
            toast('生成摘要失败：' + (e && e.message ? e.message : e), 'error');
        }).then(function () {
            dom.summaryBtn.disabled = false;
            dom.summaryBtn.textContent = '生成摘要';
        });
    }

    // =========================================================================
    // 8. 宿主接入：建标签、建内容区、隐藏旧 rag 标签、别名重定向
    // =========================================================================
    function findSettingsTabs() {
        return document.querySelector('#view-settings .settings-tabs') || document.querySelector('.settings-tabs');
    }

    function findLegacyContent() {
        return document.querySelector('.settings-tab-content[data-tab-content="' + LEGACY_TAB + '"]');
    }

    /** 1) 在 .settings-tabs 里插入「知识与记忆」按钮（幂等） */
    function ensureTabButton() {
        var tabs = findSettingsTabs();
        if (!tabs) return null;
        var exists = tabs.querySelector('.settings-tab[data-tab="' + NEW_TAB + '"]');
        if (exists) return exists;

        var btn = el('button', 'settings-tab', '知识与记忆');
        btn.type = 'button';
        btn.dataset.tab = NEW_TAB;
        // 只用 addEventListener（不再写 inline onclick）：一个按钮只触发一次切换，
        // 也不依赖宿主把 app 暴露到全局属性上
        btn.addEventListener('click', function () { switchTab(NEW_TAB); });

        // 插在旧「知识库」按钮的位置（原位替换，信息架构不乱跳）
        var legacy = tabs.querySelector('.settings-tab[data-tab="' + LEGACY_TAB + '"]');
        if (legacy && legacy.parentNode === tabs) tabs.insertBefore(btn, legacy);
        else tabs.appendChild(btn);
        return btn;
    }

    /** 2) 旧的「知识库」按钮隐藏（但不删 DOM：深链/老脚本仍可能引用它） */
    function hideLegacyTabButton() {
        var tabs = findSettingsTabs();
        if (!tabs) return;
        var legacy = tabs.querySelector('.settings-tab[data-tab="' + LEGACY_TAB + '"]');
        if (!legacy) return;
        legacy.hidden = true;
        legacy.style.display = 'none';
        legacy.setAttribute('aria-hidden', 'true');
        legacy.tabIndex = -1;
        legacy.title = '已合并到「知识与记忆」';
    }

    /** 3) 内容区：优先用宿主手写的 #kmRoot，没有就自己造一个 .settings-tab-content */
    function ensureContentHost() {
        var explicit = document.getElementById(MOUNT_ID);
        if (explicit) return explicit;

        var exists = document.querySelector('.settings-tab-content[data-tab-content="' + NEW_TAB + '"]');
        if (exists) {
            var inner = exists.querySelector('#' + MOUNT_ID);
            if (inner) return inner;
            var holder = el('div', 'setting-section glass-strong');
            var root = el('div');
            root.id = MOUNT_ID;
            holder.appendChild(root);
            exists.appendChild(holder);
            return root;
        }

        var wrap = el('div', 'settings-tab-content');
        wrap.dataset.tabContent = NEW_TAB;
        var section = el('div', 'setting-section glass-strong');
        var root2 = el('div');
        root2.id = MOUNT_ID;
        section.appendChild(root2);
        wrap.appendChild(section);

        // 插在旧 rag 内容区后面（位置与原标签一致）
        var legacyContent = findLegacyContent();
        if (legacyContent && legacyContent.parentNode) {
            legacyContent.parentNode.insertBefore(wrap, legacyContent.nextSibling);
        } else {
            var tabs = findSettingsTabs();
            if (tabs && tabs.parentNode) tabs.parentNode.appendChild(wrap);
        }
        return root2;
    }

    /** 4) 调宿主原生切换（保留它的淡出动画与其它 tab 的副作用） */
    function switchTab(tab) {
        var A = getApp();
        if (A && typeof A.switchSettingsTab === 'function') { A.switchSettingsTab(tab); return; }
        // 宿主不可用时退化成纯 CSS 切换
        document.querySelectorAll('.settings-tab').forEach(function (b) {
            b.classList.toggle('active', b.dataset.tab === tab);
        });
        document.querySelectorAll('.settings-tab-content').forEach(function (c) {
            c.classList.toggle('active', c.dataset.tabContent === tab);
        });
    }

    /** 5) 给 .settings-layout 加宽（720px 放不下两栏），离开本标签时还原 */
    function setWide(on) {
        var layout = document.querySelector('#view-settings .settings-layout') ||
                     document.querySelector('.settings-layout');
        if (!layout) return;
        layout.classList.toggle(WIDE_CLASS, !!on);
    }

    /** 6) 打补丁：switchSettingsTab('rag') → 'knowledge'；切到本标签时刷新数据 */
    function patchSwitchSettingsTab() {
        var A = getApp();
        if (!A || typeof A.switchSettingsTab !== 'function' || A.switchSettingsTab.__kmPatched) return;
        var orig = A.switchSettingsTab;
        var patched = function (tab) {
            // 旧深链 / 老 onclick / 老代码里写死的 'rag' 一律转到新标签（唯一真实入口）
            if (tab === LEGACY_TAB) tab = NEW_TAB;
            var ret = orig.call(this, tab);
            setWide(tab === NEW_TAB);
            if (tab === NEW_TAB) { refresh(); }
            return ret;
        };
        patched.__kmPatched = true;
        patched.__kmOrig = orig;
        A.switchSettingsTab = patched;
    }

    /** 7) 深链：index.html 本身不解析 ?tab=，这里补一个最小实现，
     *  让 ?tab=rag / ?tab=knowledge 都能落到新标签（?tab=memory 仍是「记忆标签」，不抢） */
    function maybeOpenFromUrl() {
        var q = '';
        try { q = String(window.location.search || '').replace(/^\?/, ''); } catch (e) { q = ''; }
        if (!q) return false;
        var params = {};
        q.split('&').forEach(function (kv) {
            var i = kv.indexOf('=');
            var k = i < 0 ? kv : kv.slice(0, i);
            var v = i < 0 ? '' : kv.slice(i + 1);
            try { k = decodeURIComponent(k); v = decodeURIComponent(v); } catch (e) { /* 原样使用 */ }
            if (k) params[k] = v;
        });
        var tab = params.tab;
        if (!tab) return false;
        if (tab !== NEW_TAB && tab !== LEGACY_TAB) return false;   // 其它标签交给用户自己点

        var A = getApp();
        if (A && typeof A.switchView === 'function') {
            if (A.state && A.state.view !== 'settings') A.switchView('settings');
        }
        // switchView 有 210ms 过渡，稍等再切标签
        setTimeout(function () { switchTab(NEW_TAB); }, 260);
        return true;
    }

    /** 8) 样式表：自己把 knowledge_memory.css 挂上，
     *  这样 index.html 里只需要加一个 <script>，不用再改 <head>（少一处并行冲突） */
    function ensureStyles() {
        if (document.getElementById('kmStyles')) return;
        if (document.querySelector('link[data-km-style="1"]')) return;
        // 宿主自己写了 <link rel="stylesheet" href=".../knowledge_memory.css"> 的话就别再插一个
        if (document.querySelector('link[href*="knowledge_memory.css"]')) return;
        var link = document.createElement('link');
        link.id = 'kmStyles';
        link.rel = 'stylesheet';
        link.setAttribute('data-km-style', '1');
        // 页面由网关根路径提供，静态目录挂在 /static（demo.py 里 StaticFiles(directory=STATIC_DIR)）
        link.href = '/static/knowledge_memory.css';
        (document.head || document.documentElement).appendChild(link);
    }

    /** 9) 兜底：宿主换角色 / 换会话后，记忆作用域变了，重新拉一次 */
    function watchScope() {
        var last = scope().charId + '|' + scope().user;
        setInterval(function () {
            var now = scope().charId + '|' + scope().user;
            if (now === last) return;
            last = now;
            if (isTabActive()) refresh();
        }, 2000);
    }

    function isTabActive() {
        var A = getApp();
        if (A && A.state && A.state.settingsTab) return A.state.settingsTab === NEW_TAB;
        var c = document.querySelector('.settings-tab-content[data-tab-content="' + NEW_TAB + '"]');
        return !!(c && c.classList.contains('active'));
    }

    // =========================================================================
    // 9. 对外 API
    // =========================================================================
    function mount(target) {
        var root = null;
        if (target) {
            root = (typeof target === 'string') ? document.querySelector(target) : target;
        }
        if (!root) root = document.getElementById(MOUNT_ID);
        if (!root) root = ensureContentHost();

        // 建标签按钮 / 隐藏旧按钮 / 打补丁（重复调用都安全）
        ensureStyles();
        ensureTabButton();
        hideLegacyTabButton();
        patchSwitchSettingsTab();
        if (isTabActive()) setWide(true);
        if (root.dataset.kmMounted === '1') { refresh(); return root; }

        buildSkeleton(root);
        root.dataset.kmMounted = '1';
        refresh();
        return root;
    }

    /** 刷新两栏数据（tab 切过来、外部调用、宿主切角色时都会走到） */
    function refresh() {
        try { renderCharSelect(); } catch (e) { /* 角色列表还没就绪时忽略 */ }
        if (!dom.docList) {
            // 还没建骨架（比如宿主在 init 之前就调用了）→ 补一次 mount
            if (!document.getElementById(MOUNT_ID) && !document.querySelector('.settings-tabs')) return Promise.resolve();
            mount();
            return Promise.resolve();
        }
        return Promise.all([loadDocs(), loadMemory()]).then(function () {
            // 详情面板开着的时候，搜索结果跟着新数据对齐
            if (store.query) {
                store.hits.mem = filterMemory(store.query);
                renderHits();
            }
        });
    }

    window.QiyuKnowledgeMemory = {
        __km: true,
        version: VERSION,
        mount: mount,
        refresh: refresh,
        // 便于自测/排查：直接看数据快照
        _store: store,
        _endpoints: EP
    };

    // =========================================================================
    // 10. 自启动：等宿主 app.init() 跑完再挂（DOMContentLoaded 监听按注册顺序触发）
    // =========================================================================
    function boot() {
        try {
            mount();
            maybeOpenFromUrl();
            watchScope();
        } catch (e) {
            console.error('[知识与记忆] 初始化失败', e);
        }
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', boot);
    } else {
        boot();
    }
})();
