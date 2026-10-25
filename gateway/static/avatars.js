/*!
 * 栖语头像 avatars.js —— 12 枚内联 SVG 头像（currentColor，无第三方依赖）
 *
 * 用法（原生 JS，直接 <script src="/static/avatars.js"></script> 即可）：
 *   QiyuAvatars.list                 // ['a01' ... 'a12']，随机分配 / 选择器用
 *   QiyuAvatars.svg('a03')           // 内联 SVG 字符串，currentColor 跟随父级 color
 *   QiyuAvatars.pick('char_9f2a')    // 由角色 id 稳定映射到一个头像（同 id 永远同一个）
 *   QiyuAvatars.html(char)           // 可直接塞 innerHTML 的字符串（内置 SVG 或自定义 <img>）
 *   QiyuAvatars.render(el, char)     // 把头像画进 el，底色用 char.avatar_color
 *
 * 图形约定：viewBox 32×32，内接圆 r=12（即 4px 内边距），
 * 主色 = currentColor，翡翠绿配件 = currentColor 的 72% 透明度（垫在剪影之下）。
 *
 * 头像清单（二次元风格：脸 + 发型 + 大眼 + 腮红，单色描线，落在彩色圆底上）：
 *   a01  圆头  —  最简基线：圆头 + 颈 + 圆肩，单色剪影
 *   a02  方头  —  圆角方头 + 颈 + 圆肩，带棱角感的角色
 *   a03  猫耳  —  圆头 + 两只翡翠绿三角耳
 *   a04  长耳  —  圆头 + 两只上竖长耳，高领披肩
 *   a05  双马尾  —  圆头 + 两条垂到肩上的翡翠马尾，圆肩
 *   a06  光环  —  圆头 + 头顶翡翠光环
 *   a07  耳机  —  圆头 + 翡翠绿头戴耳机（横梁 + 两只耳罩）
 *   a08  丸子头  —  圆头 + 两侧翡翠发髻，平肩
 *   a09  团子  —  小圆头 + 大圆身 + 两只小手，一只圆滚滚的小生物
 *   a10  星冠  —  圆头 + 五枚放射尖冠
 *   a11  叶芽  —  圆头 + 头顶翡翠嫩芽（两片小叶），高领披肩
 *   a12  尖帽  —  圆锥尖帽 + 帽檐 + 圆头 + 低圆肩，帽尖一颗翡翠小球
 */
(function (global) {
    'use strict';

    /* ── 常量白名单：只有这 12 个字符串会被当作 HTML 输出，别处一律不拼用户数据 ── */
    var LIST = ['a01', 'a02', 'a03', 'a04', 'a05', 'a06', 'a07', 'a08', 'a09', 'a10', 'a11', 'a12',
                'a13', 'a14', 'a15', 'a16', 'a17', 'a18', 'a19', 'a20', 'a21', 'a22', 'a23', 'a24'];

    var SVG = Object.freeze({
    a01: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" width=\"100%\" height=\"100%\" aria-hidden=\"true\" focusable=\"false\"><g>\n    <circle cx=\"16\" cy=\"17.4\" r=\"7.4\" fill=\"currentColor\"/>\n    <ellipse cx=\"10.5\" cy=\"20.3\" rx=\"1.5\" ry=\".85\" fill=\"#FB7185\" opacity=\".5\"/>\n    <ellipse cx=\"21.5\" cy=\"20.3\" rx=\"1.5\" ry=\".85\" fill=\"#FB7185\" opacity=\".5\"/>\n    <ellipse cx=\"12.8\" cy=\"17.8\" rx=\"1.55\" ry=\"2.05\" fill=\"#1b1b1b\"/>\n            <ellipse cx=\"19.2\" cy=\"17.8\" rx=\"1.55\" ry=\"2.05\" fill=\"#1b1b1b\"/>\n            <circle cx=\"13.35\" cy=\"17\" r=\".52\" fill=\"#fff\"/>\n            <circle cx=\"19.75\" cy=\"17\" r=\".52\" fill=\"#fff\"/><path d=\"M15.1 21.5c.5.65 1.3.65 1.8 0\" stroke=\"#1b1b1b\" stroke-width=\".8\" stroke-linecap=\"round\" fill=\"none\"/>\n  <path d=\"M8.2 17.6c0-4.6 3.4-8.2 7.8-8.2s7.8 3.6 7.8 8.2c0 .9-.1 1.7-.3 2.4-.2-2.6-1.2-4.4-2.9-5.2-1.6-.8-3.4-.3-4.6.6-1.3-1.1-3.2-1.5-4.8-.6-1.7.9-2.6 2.7-2.8 5.2-.2-.7-.2-1.5-.2-2.4z\" fill=\"rgba(0,0,0,.62)\"/>\n  <path d=\"M15.2 9.2c.3-2 1.1-3.2 2.2-3.6-.4 1.4-.4 2.6.2 3.5\" fill=\"rgba(0,0,0,.62)\"/></g></svg>",
    a02: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" width=\"100%\" height=\"100%\" aria-hidden=\"true\" focusable=\"false\"><g>\n    <circle cx=\"16\" cy=\"17.4\" r=\"7.4\" fill=\"currentColor\"/>\n    <ellipse cx=\"10.5\" cy=\"20.3\" rx=\"1.5\" ry=\".85\" fill=\"#FB7185\" opacity=\".5\"/>\n    <ellipse cx=\"21.5\" cy=\"20.3\" rx=\"1.5\" ry=\".85\" fill=\"#FB7185\" opacity=\".5\"/>\n    <ellipse cx=\"12.8\" cy=\"17.8\" rx=\"1.55\" ry=\"2.05\" fill=\"#1b1b1b\"/>\n            <ellipse cx=\"19.2\" cy=\"17.8\" rx=\"1.55\" ry=\"2.05\" fill=\"#1b1b1b\"/>\n            <circle cx=\"13.35\" cy=\"17\" r=\".52\" fill=\"#fff\"/>\n            <circle cx=\"19.75\" cy=\"17\" r=\".52\" fill=\"#fff\"/><path d=\"M15.1 21.5c.5.65 1.3.65 1.8 0\" stroke=\"#1b1b1b\" stroke-width=\".8\" stroke-linecap=\"round\" fill=\"none\"/>\n  <ellipse cx=\"6.6\" cy=\"19\" rx=\"2.5\" ry=\"4.4\" fill=\"rgba(0,0,0,.62)\" transform=\"rotate(-18 6.6 19)\"/>\n  <ellipse cx=\"25.4\" cy=\"19\" rx=\"2.5\" ry=\"4.4\" fill=\"rgba(0,0,0,.62)\" transform=\"rotate(18 25.4 19)\"/>\n  <path d=\"M8.4 17c0-4.4 3.4-7.8 7.6-7.8s7.6 3.4 7.6 7.8c0 .8-.1 1.5-.2 2.2-.3-2.4-1.2-4-2.8-4.7-1.5-.7-3.2-.2-4.4.6-1.2-.9-3-1.3-4.5-.6-1.7.8-2.6 2.4-2.9 4.8-.2-.7-.4-1.5-.4-2.3z\" fill=\"rgba(0,0,0,.62)\"/>\n  <circle cx=\"6.6\" cy=\"15.2\" r=\"1.7\" fill=\"rgba(0,0,0,.42)\"/>\n  <circle cx=\"25.4\" cy=\"15.2\" r=\"1.7\" fill=\"rgba(0,0,0,.42)\"/></g></svg>",
    a03: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" width=\"100%\" height=\"100%\" aria-hidden=\"true\" focusable=\"false\"><g>\n    <circle cx=\"16\" cy=\"17.4\" r=\"7.4\" fill=\"currentColor\"/>\n    <ellipse cx=\"10.5\" cy=\"20.3\" rx=\"1.5\" ry=\".85\" fill=\"#FB7185\" opacity=\".5\"/>\n    <ellipse cx=\"21.5\" cy=\"20.3\" rx=\"1.5\" ry=\".85\" fill=\"#FB7185\" opacity=\".5\"/>\n    <ellipse cx=\"12.8\" cy=\"17.8\" rx=\"1.55\" ry=\"2.05\" fill=\"#1b1b1b\"/>\n            <ellipse cx=\"19.2\" cy=\"17.8\" rx=\"1.55\" ry=\"2.05\" fill=\"#1b1b1b\"/>\n            <circle cx=\"13.35\" cy=\"17\" r=\".52\" fill=\"#fff\"/>\n            <circle cx=\"19.75\" cy=\"17\" r=\".52\" fill=\"#fff\"/><path d=\"M15.1 21.5c.5.65 1.3.65 1.8 0\" stroke=\"#1b1b1b\" stroke-width=\".8\" stroke-linecap=\"round\" fill=\"none\"/>\n  <path d=\"M7.4 24.4c-.5-2.2-.6-4.4-.6-6.6 0-5 4.1-9 9.2-9s9.2 4 9.2 9c0 2.2-.1 4.4-.6 6.6-1.1.5-2.3.7-3.4.6.7-2.4 1-5 .9-7.6-.6 1.4-1.6 2.3-3 2.7-1.4.4-2.9.1-4.1-.7-1.2.8-2.7 1.1-4.1.7-1.4-.4-2.4-1.3-3-2.7-.1 2.6.2 5.2.9 7.6-1.1.1-2.3-.1-3.4-.6z\" fill=\"rgba(0,0,0,.62)\"/></g></svg>",
    a04: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" width=\"100%\" height=\"100%\" aria-hidden=\"true\" focusable=\"false\"><g>\n    <circle cx=\"16\" cy=\"17.4\" r=\"7.4\" fill=\"currentColor\"/>\n    <ellipse cx=\"10.5\" cy=\"20.3\" rx=\"1.5\" ry=\".85\" fill=\"#FB7185\" opacity=\".5\"/>\n    <ellipse cx=\"21.5\" cy=\"20.3\" rx=\"1.5\" ry=\".85\" fill=\"#FB7185\" opacity=\".5\"/>\n    <ellipse cx=\"12.8\" cy=\"17.8\" rx=\"1.55\" ry=\"1.85\" fill=\"#1b1b1b\"/>\n            <ellipse cx=\"19.2\" cy=\"17.8\" rx=\"1.55\" ry=\"1.85\" fill=\"#1b1b1b\"/>\n            <circle cx=\"13.35\" cy=\"17\" r=\".52\" fill=\"#fff\"/>\n            <circle cx=\"19.75\" cy=\"17\" r=\".52\" fill=\"#fff\"/><path d=\"M15.1 21.5c.5.65 1.3.65 1.8 0\" stroke=\"#1b1b1b\" stroke-width=\".8\" stroke-linecap=\"round\" fill=\"none\"/>\n  <path d=\"M8.3 17.2c0-4.4 3.4-8 7.7-8s7.7 3.6 7.7 8c0 .7-.1 1.4-.2 2-1.1-1.6-2.4-2.8-3.8-3.4-1.4 1.1-3.2 1.7-5.1 1.7-1 0-2-.2-2.9-.5.5 1 .8 1.9.9 2.7-1.5-.1-2.9-.9-4-2.2-.2-.6-.3-1.2-.3-1.3z\" fill=\"rgba(0,0,0,.62)\"/>\n  <path d=\"M20.4 10.2l2.1-2.6-.6 3.1 2.4-1.2-1.7 2.5\" fill=\"rgba(0,0,0,.62)\"/></g></svg>",
    a05: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" width=\"100%\" height=\"100%\" aria-hidden=\"true\" focusable=\"false\"><g>\n    <circle cx=\"16\" cy=\"17.4\" r=\"7.4\" fill=\"currentColor\"/>\n    <ellipse cx=\"10.5\" cy=\"20.3\" rx=\"1.5\" ry=\".85\" fill=\"#FB7185\" opacity=\".5\"/>\n    <ellipse cx=\"21.5\" cy=\"20.3\" rx=\"1.5\" ry=\".85\" fill=\"#FB7185\" opacity=\".5\"/>\n    <ellipse cx=\"12.8\" cy=\"17.8\" rx=\"1.55\" ry=\"2.05\" fill=\"#1b1b1b\"/>\n            <ellipse cx=\"19.2\" cy=\"17.8\" rx=\"1.55\" ry=\"2.05\" fill=\"#1b1b1b\"/>\n            <circle cx=\"13.35\" cy=\"17\" r=\".52\" fill=\"#fff\"/>\n            <circle cx=\"19.75\" cy=\"17\" r=\".52\" fill=\"#fff\"/><path d=\"M15.1 21.5c.5.65 1.3.65 1.8 0\" stroke=\"#1b1b1b\" stroke-width=\".8\" stroke-linecap=\"round\" fill=\"none\"/>\n  <path d=\"M8.6 11.4l.7-4.6 3.9 3.1z\" fill=\"rgba(0,0,0,.62)\"/>\n  <path d=\"M23.4 11.4l-.7-4.6-3.9 3.1z\" fill=\"rgba(0,0,0,.62)\"/>\n  <path d=\"M8.4 17c0-4.4 3.4-7.8 7.6-7.8s7.6 3.4 7.6 7.8c0 .8-.1 1.5-.2 2.2-.3-2.4-1.2-4-2.8-4.7-1.5-.7-3.2-.2-4.4.6-1.2-.9-3-1.3-4.5-.6-1.7.8-2.6 2.4-2.9 4.8-.2-.7-.4-1.5-.4-2.3z\" fill=\"rgba(0,0,0,.62)\"/>\n  <path d=\"M6.4 22.6l-2.6 1.2M25.6 22.6l2.6 1.2\" stroke=\"rgba(0,0,0,.62)\" stroke-width=\".9\" stroke-linecap=\"round\"/></g></svg>",
    a06: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" width=\"100%\" height=\"100%\" aria-hidden=\"true\" focusable=\"false\"><g>\n    <circle cx=\"16\" cy=\"17.4\" r=\"7.4\" fill=\"currentColor\"/>\n    <ellipse cx=\"10.5\" cy=\"20.3\" rx=\"1.5\" ry=\".85\" fill=\"#FB7185\" opacity=\".5\"/>\n    <ellipse cx=\"21.5\" cy=\"20.3\" rx=\"1.5\" ry=\".85\" fill=\"#FB7185\" opacity=\".5\"/>\n    <ellipse cx=\"12.8\" cy=\"17.8\" rx=\"1.55\" ry=\"1.7\" fill=\"#1b1b1b\"/>\n            <ellipse cx=\"19.2\" cy=\"17.8\" rx=\"1.55\" ry=\"1.7\" fill=\"#1b1b1b\"/>\n            <circle cx=\"13.35\" cy=\"17\" r=\".52\" fill=\"#fff\"/>\n            <circle cx=\"19.75\" cy=\"17\" r=\".52\" fill=\"#fff\"/><path d=\"M15.1 21.5c.5.65 1.3.65 1.8 0\" stroke=\"#1b1b1b\" stroke-width=\".8\" stroke-linecap=\"round\" fill=\"none\"/>\n  <path d=\"M8.4 17c0-4.4 3.4-7.8 7.6-7.8s7.6 3.4 7.6 7.8c0 .8-.1 1.5-.2 2.2-.3-2.4-1.2-4-2.8-4.7-1.5-.7-3.2-.2-4.4.6-1.2-.9-3-1.3-4.5-.6-1.7.8-2.6 2.4-2.9 4.8-.2-.7-.4-1.5-.4-2.3z\" fill=\"rgba(0,0,0,.62)\"/>\n  <circle cx=\"12.8\" cy=\"17.9\" r=\"2.75\" fill=\"none\" stroke=\"#1b1b1b\" stroke-width=\".7\" opacity=\".75\"/>\n  <circle cx=\"19.2\" cy=\"17.9\" r=\"2.75\" fill=\"none\" stroke=\"#1b1b1b\" stroke-width=\".7\" opacity=\".75\"/>\n  <path d=\"M15.55 17.9h.9\" stroke=\"#1b1b1b\" stroke-width=\".7\" opacity=\".75\"/></g></svg>",
    a07: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" width=\"100%\" height=\"100%\" aria-hidden=\"true\" focusable=\"false\"><g>\n    <circle cx=\"16\" cy=\"17.4\" r=\"7.4\" fill=\"currentColor\"/>\n    <ellipse cx=\"10.5\" cy=\"20.3\" rx=\"1.5\" ry=\".85\" fill=\"#FB7185\" opacity=\".5\"/>\n    <ellipse cx=\"21.5\" cy=\"20.3\" rx=\"1.5\" ry=\".85\" fill=\"#FB7185\" opacity=\".5\"/>\n    <path d=\"M11.2 18.1c.7-.9 1.9-.9 2.6 0\" stroke=\"#1b1b1b\" stroke-width=\".95\" stroke-linecap=\"round\" fill=\"none\"/>\n            <path d=\"M18.2 18.1c.7-.9 1.9-.9 2.6 0\" stroke=\"#1b1b1b\" stroke-width=\".95\" stroke-linecap=\"round\" fill=\"none\"/><path d=\"M15.1 21.5c.5.65 1.3.65 1.8 0\" stroke=\"#1b1b1b\" stroke-width=\".8\" stroke-linecap=\"round\" fill=\"none\"/>\n  <circle cx=\"16\" cy=\"6.6\" r=\"3.1\" fill=\"rgba(0,0,0,.62)\"/>\n  <path d=\"M8.6 17.4c0-4.4 3.4-7.9 7.4-7.9s7.4 3.5 7.4 7.9c0 .7-.1 1.4-.2 2-.3-2.3-1.2-3.8-2.7-4.5-1.5-.7-3.1-.2-4.3.6-1.2-.8-2.9-1.3-4.4-.6-1.5.7-2.4 2.2-2.7 4.5-.2-.6-.3-1.3-.3-2z\" fill=\"rgba(0,0,0,.62)\"/></g></svg>",
    a08: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" width=\"100%\" height=\"100%\" aria-hidden=\"true\" focusable=\"false\"><g>\n    <circle cx=\"16\" cy=\"17.4\" r=\"7.4\" fill=\"currentColor\"/>\n    <ellipse cx=\"10.5\" cy=\"20.3\" rx=\"1.5\" ry=\".85\" fill=\"#FB7185\" opacity=\".5\"/>\n    <ellipse cx=\"21.5\" cy=\"20.3\" rx=\"1.5\" ry=\".85\" fill=\"#FB7185\" opacity=\".5\"/>\n    <ellipse cx=\"12.8\" cy=\"17.8\" rx=\"1.55\" ry=\"2.05\" fill=\"#1b1b1b\"/>\n            <ellipse cx=\"19.2\" cy=\"17.8\" rx=\"1.55\" ry=\"2.05\" fill=\"#1b1b1b\"/>\n            <circle cx=\"13.35\" cy=\"17\" r=\".52\" fill=\"#fff\"/>\n            <circle cx=\"19.75\" cy=\"17\" r=\".52\" fill=\"#fff\"/><path d=\"M15.1 21.5c.5.65 1.3.65 1.8 0\" stroke=\"#1b1b1b\" stroke-width=\".8\" stroke-linecap=\"round\" fill=\"none\"/>\n  <path d=\"M8.6 17.2c0-4.4 3.4-7.9 7.4-7.9s7.4 3.5 7.4 7.9c0 .7-.1 1.4-.2 2-.3-2.3-1.2-3.8-2.7-4.5-1.5-.7-3.1-.2-4.3.6-1.2-.8-2.9-1.3-4.4-.6-1.5.7-2.4 2.2-2.7 4.5-.2-.6-.3-1.3-.3-2z\" fill=\"rgba(0,0,0,.62)\"/>\n  <ellipse cx=\"25.6\" cy=\"18.4\" rx=\"2.2\" ry=\"4.2\" fill=\"rgba(0,0,0,.62)\" transform=\"rotate(14 25.6 18.4)\"/>\n  <circle cx=\"24.4\" cy=\"13.4\" r=\"1.5\" fill=\"rgba(0,0,0,.42)\"/></g></svg>",
    a09: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" width=\"100%\" height=\"100%\" aria-hidden=\"true\" focusable=\"false\"><g>\n    <circle cx=\"16\" cy=\"17.4\" r=\"7.4\" fill=\"currentColor\"/>\n    <ellipse cx=\"10.5\" cy=\"20.3\" rx=\"1.5\" ry=\".85\" fill=\"#FB7185\" opacity=\".5\"/>\n    <ellipse cx=\"21.5\" cy=\"20.3\" rx=\"1.5\" ry=\".85\" fill=\"#FB7185\" opacity=\".5\"/>\n    <ellipse cx=\"12.8\" cy=\"17.8\" rx=\"1.55\" ry=\"2.05\" fill=\"#1b1b1b\"/>\n            <ellipse cx=\"19.2\" cy=\"17.8\" rx=\"1.55\" ry=\"2.05\" fill=\"#1b1b1b\"/>\n            <circle cx=\"13.35\" cy=\"17\" r=\".52\" fill=\"#fff\"/>\n            <circle cx=\"19.75\" cy=\"17\" r=\".52\" fill=\"#fff\"/><ellipse cx=\"16\" cy=\"21.7\" rx=\".85\" ry=\"1.05\" fill=\"#1b1b1b\"/>\n  <path d=\"M7.6 24.2c-.5-2.1-.6-4.2-.6-6.4 0-5 4-9 9-9s9 4 9 9c0 2.2-.1 4.3-.6 6.4-1 .3-2 .4-3 .3.6-2.3.8-4.7.7-7.1-1.1 1.2-2.6 1.9-4.3 1.9-1.6 0-3-.6-4.1-1.6-1.1 1-2.5 1.6-4.1 1.6-1.7 0-3.2-.7-4.3-1.9-.1 2.4.1 4.8.7 7.1-1 .1-2 0-3-.3z\" fill=\"rgba(0,0,0,.62)\"/></g></svg>",
    a10: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" width=\"100%\" height=\"100%\" aria-hidden=\"true\" focusable=\"false\"><g>\n    <circle cx=\"16\" cy=\"17.4\" r=\"7.4\" fill=\"currentColor\"/>\n    <ellipse cx=\"10.5\" cy=\"20.3\" rx=\"1.5\" ry=\".85\" fill=\"#FB7185\" opacity=\".5\"/>\n    <ellipse cx=\"21.5\" cy=\"20.3\" rx=\"1.5\" ry=\".85\" fill=\"#FB7185\" opacity=\".5\"/>\n    <path d=\"M12.8 16.2l.55 1.35 1.45.15-1.1.95.32 1.42-1.22-.72-1.22.72.32-1.42-1.1-.95 1.45-.15z\" fill=\"#1b1b1b\"/>\n            <path d=\"M19.2 16.2l.55 1.35 1.45.15-1.1.95.32 1.42-1.22-.72-1.22.72.32-1.42-1.1-.95 1.45-.15z\" fill=\"#1b1b1b\"/><path d=\"M15.1 21.5c.5.65 1.3.65 1.8 0\" stroke=\"#1b1b1b\" stroke-width=\".8\" stroke-linecap=\"round\" fill=\"none\"/>\n  <path d=\"M9.2 11.8l-.4-5 4.2 2.9z\" fill=\"rgba(0,0,0,.62)\"/>\n  <path d=\"M22.8 11.8l.4-5-4.2 2.9z\" fill=\"rgba(0,0,0,.62)\"/>\n  <path d=\"M8.4 17c0-4.4 3.4-7.8 7.6-7.8s7.6 3.4 7.6 7.8c0 .8-.1 1.5-.2 2.2-.3-2.4-1.2-4-2.8-4.7-1.5-.7-3.2-.2-4.4.6-1.2-.9-3-1.3-4.5-.6-1.7.8-2.6 2.4-2.9 4.8-.2-.7-.4-1.5-.4-2.3z\" fill=\"rgba(0,0,0,.62)\"/></g></svg>",
    a11: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" width=\"100%\" height=\"100%\" aria-hidden=\"true\" focusable=\"false\"><g>\n    <circle cx=\"16\" cy=\"17.4\" r=\"7.4\" fill=\"currentColor\"/>\n    <ellipse cx=\"10.5\" cy=\"20.3\" rx=\"1.5\" ry=\".85\" fill=\"#FB7185\" opacity=\".5\"/>\n    <ellipse cx=\"21.5\" cy=\"20.3\" rx=\"1.5\" ry=\".85\" fill=\"#FB7185\" opacity=\".5\"/>\n    <ellipse cx=\"12.8\" cy=\"17.8\" rx=\"1.55\" ry=\"1.15\" fill=\"#1b1b1b\"/>\n            <ellipse cx=\"19.2\" cy=\"17.8\" rx=\"1.55\" ry=\"1.15\" fill=\"#1b1b1b\"/>\n            <circle cx=\"13.35\" cy=\"17\" r=\".52\" fill=\"#fff\"/>\n            <circle cx=\"19.75\" cy=\"17\" r=\".52\" fill=\"#fff\"/><path d=\"M15.1 21.5c.5.65 1.3.65 1.8 0\" stroke=\"#1b1b1b\" stroke-width=\".8\" stroke-linecap=\"round\" fill=\"none\"/>\n  <path d=\"M8.9 13.2c1.6-2.6 4.2-4.2 7.1-4.2s5.5 1.6 7.1 4.2c-2.1-1.1-4.5-1.7-7.1-1.7s-5 .6-7.1 1.7z\" fill=\"rgba(0,0,0,.62)\"/>\n  <path d=\"M8.6 17.6c0-4.4 3.4-7.9 7.4-7.9s7.4 3.5 7.4 7.9c0 .7-.1 1.4-.2 2-.3-2.3-1.2-3.8-2.7-4.5-1.5-.7-3.1-.2-4.3.6-1.2-.8-2.9-1.3-4.4-.6-1.5.7-2.4 2.2-2.7 4.5-.2-.6-.3-1.3-.3-2z\" fill=\"rgba(0,0,0,.62)\"/>\n  <path d=\"M13.4 21.4c.6.5 1.4.5 2 0\" stroke=\"#0369a1\" stroke-width=\".7\" stroke-linecap=\"round\" fill=\"none\"/></g></svg>",
    a12: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" width=\"100%\" height=\"100%\" aria-hidden=\"true\" focusable=\"false\"><g>\n    <circle cx=\"16\" cy=\"17.4\" r=\"7.4\" fill=\"currentColor\"/>\n    <ellipse cx=\"10.5\" cy=\"20.3\" rx=\"1.5\" ry=\".85\" fill=\"#FB7185\" opacity=\".5\"/>\n    <ellipse cx=\"21.5\" cy=\"20.3\" rx=\"1.5\" ry=\".85\" fill=\"#FB7185\" opacity=\".5\"/>\n    <path d=\"M11.2 18.1c.7-.9 1.9-.9 2.6 0\" stroke=\"#1b1b1b\" stroke-width=\".95\" stroke-linecap=\"round\" fill=\"none\"/>\n            <path d=\"M18.2 18.1c.7-.9 1.9-.9 2.6 0\" stroke=\"#1b1b1b\" stroke-width=\".95\" stroke-linecap=\"round\" fill=\"none\"/><path d=\"M15.1 21.5c.5.65 1.3.65 1.8 0\" stroke=\"#1b1b1b\" stroke-width=\".8\" stroke-linecap=\"round\" fill=\"none\"/>\n  <path d=\"M8.2 17.2c0-4.6 3.5-8.2 7.8-8.2s7.8 3.6 7.8 8.2c0 .9-.1 1.7-.3 2.4-.2-2.6-1.2-4.4-2.9-5.2-1.6-.8-3.4-.3-4.6.6-1.3-1.1-3.2-1.5-4.8-.6-1.7.9-2.6 2.7-2.8 5.2-.2-.7-.2-1.5-.2-2.4z\" fill=\"rgba(0,0,0,.62)\"/>\n  <circle cx=\"26.2\" cy=\"11.4\" r=\"1.3\" fill=\"#FBBF24\"/>\n  <path d=\"M5.4 13.2l.9.4-.4.9-.4-.9z\" fill=\"#FBBF24\"/></g></svg>"
});

    /* 无 avatar_color 时的兜底底色（取自角色卡原有的 getAvatarColor 色板，并补两色） */
    var PALETTE = ['#2563eb', '#059669', '#7c3aed', '#d97706', '#dc2626', '#db2777',
                   '#0891b2', '#65a30d', '#ea580c', '#4f46e5', '#0f766e', '#b45309'];

    /* 老的内置头像文件名（default_01.png 等）：命中就改用 SVG，避免继续显示旧卡通图 */
    var LEGACY = /^default_\d+(?:\.(?:png|jpe?g|webp|gif))?$/i;
    /* 内置 SVG 文件名 / id：a01、a01.svg 都认 */
    var BUILTIN = /^a(0[1-9]|1\d|2[0-4])(?:\.svg)?$/i;

    /* ── 小工具 ── */
    function normId(id) {
        var m = BUILTIN.exec(String(id == null ? '' : id).trim());
        return m ? 'a' + m[1] : null;
    }

    /* 只接受 #RGB / #RRGGBB，其它（rgb()/颜色名/注入串）一律拒绝 → 返回 null */
    function normColor(c) {
        var s = String(c == null ? '' : c).trim();
        if (/^#[0-9a-f]{3}$/i.test(s)) {
            return ('#' + s[1] + s[1] + s[2] + s[2] + s[3] + s[3]).toLowerCase();
        }
        return /^#[0-9a-f]{6}$/i.test(s) ? s.toLowerCase() : null;
    }

    /* 相对亮度（0..1），用来决定剪影该用白还是深色 */
    function luminance(hex) {
        var r = parseInt(hex.substr(1, 2), 16) / 255,
            g = parseInt(hex.substr(3, 2), 16) / 255,
            b = parseInt(hex.substr(5, 2), 16) / 255;
        function lin(v) { return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); }
        return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
    }

    function escHtml(s) {
        return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
            return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
        });
    }

    /* ── 由种子稳定映射：FNV-1a，同一种子永远同一个头像 ── */
    function pick(seed) {
        var s = String(seed == null ? '' : seed), h = 2166136261 >>> 0;
        for (var i = 0; i < s.length; i++) {
            h ^= s.charCodeAt(i);
            h = Math.imul(h, 16777619) >>> 0;
        }
        return LIST[h % LIST.length];
    }

    function colorFor(seed) {
        return PALETTE[LIST.indexOf(pick(seed)) % PALETTE.length];
    }

    /* ── 解析一个角色该用哪枚头像 ── */
    function resolve(char) {
        char = char || {};
        var av = String(char.avatar || '').trim();
        var builtin = normId(av);
        if (builtin) return { kind: 'builtin', id: builtin };          // 已选 a01..a12
        var seed = char.id != null && char.id !== '' ? char.id : (char.name || av);
        if (!av || LEGACY.test(av)) return { kind: 'auto', id: pick(seed) };  // 未选 / 旧内置图
        return { kind: 'custom' };                                     // 用户上传的图片，保持原样
    }

    /* 内置头像 = DiceBear `adventurer` 风格的真·插画 SVG（MIT，可商用），
       直接 <img> 引用；容器给圆底，SVG 透明区域会露出底色。 */
    var AVATAR_BASE = '/static/avatars/';
    function svg(id) {
        var k = normId(id) || 'a01';
        return '<img src="' + AVATAR_BASE + k + '.svg" alt=""'
             + ' style="width:100%;height:100%;display:block;border-radius:50%;object-fit:cover"'
             + ' onerror="this.remove()">';
    }

    /* 可直接塞进 innerHTML：内置头像是 <img>，自定义图片也是 <img>（全部转义） */
    function html(char) {
        char = char || {};
        var r = resolve(char);
        var name = char.name == null ? '' : String(char.name).slice(0, 40);
        if (r.kind === 'custom') {
            return '<img class="qy-avatar-img" src="/avatars/' + encodeURIComponent(String(char.avatar)) + '"'
                 + ' alt="' + escHtml(name) + '"'
                 + ' onerror="this.remove()">';
        }
        return '<img class="qy-avatar-img" src="/static/avatars/' + r.id + '.svg"'
             + ' alt="' + escHtml(name) + '" onerror="this.remove()">';
    }

    /* 把头像画进 el：底色 = character.avatar_color（校验过的 #RRGGBB） */
    function render(el, char) {
        if (!el || !el.style) return false;
        char = char || {};
        var r = resolve(char);
        if (r.kind === 'custom') return false;    // 自定义图片交给调用方走 <img>
        var bg = normColor(char.avatar_color) || colorFor(char.id != null ? char.id : (char.name || r.id));
        el.textContent = '';                      // 先清空（不用 innerHTML 拼任何数据）
        el.style.background = bg;
        el.style.color = luminance(bg) > 0.62 ? '#0b1220' : '#ffffff';
        el.setAttribute('data-avatar', r.id);
        if (el.classList) el.classList.add('qy-avatar');
        el.innerHTML = '<img class="qy-avatar-img" src="/static/avatars/' + r.id + '.svg"'
                     + ' alt="" onerror="this.remove()">';   // 白名单常量，无用户数据
        return true;
    }

    global.QiyuAvatars = Object.freeze({
        version: '1.0.0',
        list: Object.freeze(LIST.slice()),
        SVGS: SVG,
        svg: svg,
        pick: pick,
        colorFor: colorFor,
        resolve: resolve,
        html: html,
        render: render,
        color: normColor,
        escape: escHtml
    });
})(typeof window !== 'undefined' ? window : this);
