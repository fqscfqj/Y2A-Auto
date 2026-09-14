'use strict';
/*
 * 在「最小 DOM 桩」里真实执行设置页主脚本，并模拟点击「保存设置」。
 *
 * 为什么需要它：tests/test_settings_template_layout.py 全是静态字符串断言，
 * 抓不到渲染后 JS 的运行时行为；模板能正常渲染、脚本语法也合法，但提交处理器
 * 可以在发请求之前就抛异常，表现就是「点保存没反应」。
 *
 * 历史缺陷（本次回归的由来）：9 分组重构删掉了全局 #reset-settings-btn 及其
 * `const resetBtn` 声明，却漏改了 toggleSettingsSaveBusy 里的
 * `[saveSettingsBtn, resetBtn].forEach(...)`。该引用指向一个不存在的变量，
 * 于是 submit 处理器在 new FormData / fetch 之前就抛
 * `ReferenceError: resetBtn is not defined`，保存请求根本发不出去。
 * 这类错误既不会让页面渲染失败，也不会让 `node --check` 报语法错误，只有真正
 * 执行到那行代码才会暴露——所以必须在 DOM 桩里跑一遍提交流程。
 *
 * 用法：node tests/js/settings_save_submit.js <提取出的JS文件>
 *   输入文件由 tests/test_settings_save_submit.py 从渲染后的 /settings 页面提取，
 *   内容是页面里那段真实的主 <script> 源码。
 * 输出：单行 JSON（stdout）。
 */

const fs = require('fs');
const vm = require('vm');

/* ------------------------------------------------------------------ DOM 桩 */

class ClassList {
    constructor(owner) { this.owner = owner; this.items = new Set(); }
    add(...names) { names.forEach((n) => { if (n) { this.items.add(n); } }); }
    remove(...names) { names.forEach((n) => this.items.delete(n)); }
    contains(name) { return this.items.has(name); }
    toggle(name, force) {
        const on = force === undefined ? !this.items.has(name) : !!force;
        if (on) { this.items.add(name); } else { this.items.delete(name); }
        return on;
    }
    toString() { return Array.from(this.items).join(' '); }
}

class El {
    constructor(tag) {
        this.tagName = String(tag).toUpperCase();
        this.classList = new ClassList(this);
        this.attrs = new Map();
        this.listeners = new Map();
        this.id = '';
        this.name = '';
        this.value = '';
        this.checked = false;
        this.disabled = false;
        this.style = {};
        this.children = [];
        this._text = '';
    }
    get className() { return this.classList.toString(); }
    set className(value) {
        this.classList.items = new Set(String(value).split(/\s+/).filter(Boolean));
    }
    get textContent() { return this._text; }
    set textContent(value) { this._text = String(value); }
    setAttribute(key, value) {
        this.attrs.set(key, String(value));
        if (key === 'id') { this.id = String(value); }
        if (key === 'class') { this.className = value; }
    }
    getAttribute(key) { return this.attrs.has(key) ? this.attrs.get(key) : null; }
    removeAttribute(key) { this.attrs.delete(key); }
    appendChild(child) { this.children.push(child); return child; }
    addEventListener(type, fn) {
        if (!this.listeners.has(type)) { this.listeners.set(type, []); }
        this.listeners.get(type).push(fn);
    }
    dispatch(type, event) {
        const ev = Object.assign({ type, preventDefault() {}, stopPropagation() {} }, event || {});
        (this.listeners.get(type) || []).slice().forEach((fn) => fn(ev));
    }
    focus() {} blur() {} select() {} remove() {} scrollIntoView() {} closest() { return null; }
    querySelector() { return null; }
    querySelectorAll() { return []; }
}

// 只支持桩需要的最小选择器集合；遇到别的选择器返回空集（宽松），
// 但显式支持 .class 查询，因为待验证的修复正是按 .settings-card-reset 查找按钮。
function buildWorld() {
    const registry = [];

    const register = (el) => { registry.push(el); return el; };

    const form = register(new El('form'));
    form.id = 'settings-form';
    form.setAttribute('id', 'settings-form');
    form.action = '/settings';

    // 卡片级「重置」按钮：9 分组重构后由脚本动态生成，保存期间应被一并禁用。
    const cardReset = register(new El('button'));
    cardReset.className = 'settings-card-reset';
    cardReset.setAttribute('class', 'settings-card-reset');

    const queryAll = (selector) => {
        const sel = String(selector).trim();
        if (sel.startsWith('.') && !/[\s,[\]>#]/.test(sel)) {
            const cls = sel.slice(1);
            return registry.filter((el) => el.classList.contains(cls));
        }
        return [];
    };

    const docListeners = [];
    const document = {
        createElement: (tag) => new El(tag),
        getElementById: (id) => registry.find((el) => el.id === id) || null,
        querySelector: () => null,
        querySelectorAll: (selector) => queryAll(selector),
        addEventListener: (type, fn) => docListeners.push({ type, fn }),
    };

    const fetchCalls = [];
    const bootstrapStub = {
        Tab: { getOrCreateInstance: () => ({ show() {} }) },
        Toast: { getOrCreateInstance: () => ({ show() {} }) },
        Modal: { getOrCreateInstance: () => ({ show() {}, hide() {} }) },
    };

    const context = {
        document,
        bootstrap: bootstrapStub,
        window: {
            location: { hash: '', pathname: '/settings' },
            crypto: { randomUUID: () => 'probe-operation-id' },
            addEventListener() {},
            setInterval: () => 1,
            clearInterval() {},
            setTimeout: (fn) => { return 1; },
            clearTimeout() {},
        },
        // 返回「永不落地」的 promise：只验证提交同步阶段是否真的发出请求，
        // 不让轮询逻辑干扰断言（也避免产生未处理的 rejection）。
        fetch: (url, init) => {
            fetchCalls.push({ url: String(url), init: init || {} });
            return new Promise(() => {});
        },
        FormData: class FormDataStub {
            constructor() { this.entries = new Map(); }
            set(key, value) { this.entries.set(key, value); }
            get(key) { return this.entries.has(key) ? this.entries.get(key) : null; }
            append(key, value) { this.entries.set(key, value); }
        },
        console,
        confirm: () => true,
        alert: () => {},
        Set, Map, RegExp, Math, JSON, String, Number, Object, Array, Error,
        Boolean, Date, Promise, parseInt, parseFloat, isNaN, isFinite,
        setTimeout: (fn) => 1,
        clearTimeout: () => {},
        setInterval: () => 1,
        clearInterval: () => {},
    };
    context.globalThis = context;

    return {
        context,
        document,
        form,
        cardReset,
        fetchCalls,
        fireDomContentLoaded() {
            docListeners.filter((entry) => entry.type === 'DOMContentLoaded')
                .forEach((entry) => entry.fn());
        },
    };
}

/* ---------------------------------------------------------------- 场景驱动 */

function runProbe(source) {
    const world = buildWorld();
    const observation = {
        name: '点击保存设置会发出 POST 请求',
        ok: false,
        submitError: null,
        fetchCount: null,
        fetchUrl: null,
        fetchMethod: null,
        operationId: null,
        cardResetDisabled: null,
    };

    try {
        vm.createContext(world.context);
        vm.runInContext(source, world.context, { filename: 'settings-page.js' });
        world.fireDomContentLoaded();
    } catch (err) {
        // 加载期就抛异常同样属于「设置页脚本坏了」，直接报出来
        observation.submitError = 'DOMContentLoaded 执行异常: '
            + String(err && err.stack ? err.stack : err);
        return observation;
    }

    try {
        world.form.dispatch('submit', { target: world.form });
    } catch (err) {
        observation.submitError = String(err && err.stack ? err.stack : err);
        return observation;
    }

    observation.fetchCount = world.fetchCalls.length;
    if (world.fetchCalls.length) {
        const call = world.fetchCalls[0];
        observation.fetchUrl = call.url;
        observation.fetchMethod = (call.init && call.init.method) || 'GET';
        const body = call.init && call.init.body;
        observation.operationId = body && typeof body.get === 'function'
            ? body.get('save_operation_id') : null;
    }
    observation.cardResetDisabled = world.cardReset.disabled;
    observation.ok = observation.fetchCount === 1
        && observation.fetchMethod === 'POST'
        && !!observation.operationId
        && observation.cardResetDisabled === true;
    return observation;
}

module.exports = { runProbe };

if (require.main === module) {
    const sourcePath = process.argv[2];
    if (!sourcePath) {
        process.stderr.write('用法: node settings_save_submit.js <提取出的JS文件>\n');
        process.exit(2);
    }
    const source = fs.readFileSync(sourcePath, 'utf8');
    let report;
    try {
        report = runProbe(source);
    } catch (err) {
        report = { ok: false, error: String(err && err.stack ? err.stack : err) };
    }
    process.stdout.write(JSON.stringify(report, null, 2));
    process.exitCode = report.ok ? 0 : 1;
}
