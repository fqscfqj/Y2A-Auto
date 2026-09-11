'use strict';
/*
 * 在「最小 DOM 桩」里真实执行设置页的字段级搜索脚本。
 *
 * 为什么需要它：tests/test_settings_template_layout.py 全是静态字符串断言，
 * 抓不到渲染后 JS 的运行时行为。历史上出现过这类回归 —— renderResults() 开头
 * 会把 activeIndex 重置为 -1，而 runSearch() 在它之后才读取该下标，于是
 * 「方向键选中某项后按 Enter」恒跳到第一条（选中态画得出来，但 Enter 不认）。
 *
 * 用法：node tests/js/settings_search_keyboard.js <提取出的JS文件>
 *   输入文件由 tests/test_settings_search_keyboard.py 从渲染后的 /settings 页面提取，
 *   内容为「共享标签辅助函数 + 字段级搜索脚本」两段真实源码。
 * 输出：单行 JSON（stdout）。夹具与期望值都放在本文件里，便于与断言对照。
 */

const fs = require('fs');
const vm = require('vm');

const RESULT_LIMIT = 20; // 与模板中的 SEARCH_RESULT_LIMIT 保持一致

/* ------------------------------------------------------------------ DOM 桩 */

class ClassList {
    constructor() { this.items = new Set(); }
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

// 只实现脚本用到的选择器语法：#id / .class / tag / [attr] / [attr="v"]，
// 以及以空格分隔的两层后代选择器。遇到不支持的语法直接抛错，
// 避免桩「静默匹配不上」把测试变成永远通过。
function matchesSimple(el, selector) {
    const re = /([#.]?[\w-]+)|(\[[^\]]+\])/g;
    let match;
    let ok = true;
    let consumed = '';
    while ((match = re.exec(selector)) !== null) {
        consumed += match[0];
        if (match[2]) {
            const body = match[2].slice(1, -1);
            const eq = body.indexOf('=');
            if (eq === -1) {
                if (el.getAttribute(body) === null) { ok = false; }
            } else {
                const key = body.slice(0, eq);
                const value = body.slice(eq + 1).replace(/^["']|["']$/g, '');
                if (el.getAttribute(key) !== value) { ok = false; }
            }
        } else {
            const token = match[1];
            if (token.startsWith('#')) {
                if (el.id !== token.slice(1)) { ok = false; }
            } else if (token.startsWith('.')) {
                if (!el.classList.contains(token.slice(1))) { ok = false; }
            } else if (el.tagName !== token.toUpperCase()) {
                ok = false;
            }
        }
    }
    if (consumed !== selector.replace(/\s+/g, '')) {
        throw new Error('DOM 桩不支持的简单选择器: ' + selector);
    }
    return ok;
}

function matchesAny(el, selector) {
    return String(selector).split(',').some((part) => matchesSimple(el, part.trim()));
}

function queryAll(root, selector) {
    // 先去掉逗号后的空格，否则 "a, b, c" 会被空格拆成多个「后代层级」
    const normalized = String(selector).trim().replace(/,\s*/g, ',');
    const parts = normalized.split(/\s+/).filter(Boolean);
    if (parts.length > 2) {
        throw new Error('DOM 桩最多支持两层后代选择器: ' + selector);
    }
    const last = parts[parts.length - 1];
    const hits = root.descendants.filter((el) => matchesAny(el, last));
    if (parts.length === 1) { return hits; }
    const ancestor = parts[0];
    return hits.filter((el) => {
        let node = el.parentNode;
        while (node) {
            if (matchesAny(node, ancestor)) { return true; }
            node = node.parentNode;
        }
        return false;
    });
}

class El {
    constructor(tag) {
        this.tagName = String(tag).toUpperCase();
        this.children = [];
        this.parentNode = null;
        this.attrs = new Map();
        this.classList = new ClassList();
        this.listeners = new Map();
        this.id = '';
        this.value = '';
        this.style = {};
        this._text = '';
        this._html = '';
    }
    get className() { return this.classList.toString(); }
    set className(value) {
        this.classList.items = new Set(String(value).split(/\s+/).filter(Boolean));
    }
    get textContent() {
        if (this.children.length) {
            return this.children.map((child) => child.textContent).join('');
        }
        return this._text;
    }
    set textContent(value) { this._text = String(value); this.children = []; }
    get innerHTML() { return this._html; }
    set innerHTML(value) {
        this._html = String(value);
        if (this._html === '') { this.children = []; }
    }
    get nextSibling() {
        if (!this.parentNode) { return null; }
        const index = this.parentNode.children.indexOf(this);
        return index >= 0 && index + 1 < this.parentNode.children.length
            ? this.parentNode.children[index + 1] : null;
    }
    get descendants() {
        const out = [];
        const walk = (node) => node.children.forEach((child) => { out.push(child); walk(child); });
        walk(this);
        return out;
    }
    setAttribute(key, value) { this.attrs.set(key, String(value)); }
    getAttribute(key) { return this.attrs.has(key) ? this.attrs.get(key) : null; }
    removeAttribute(key) { this.attrs.delete(key); }
    appendChild(child) { child.parentNode = this; this.children.push(child); return child; }
    insertBefore(child, ref) {
        child.parentNode = this;
        const index = ref ? this.children.indexOf(ref) : -1;
        if (index >= 0) { this.children.splice(index, 0, child); } else { this.children.push(child); }
        return child;
    }
    addEventListener(type, fn) {
        if (!this.listeners.has(type)) { this.listeners.set(type, []); }
        this.listeners.get(type).push(fn);
    }
    dispatch(type, event) {
        const ev = Object.assign({ type, preventDefault() {}, stopPropagation() {} }, event || {});
        (this.listeners.get(type) || []).slice().forEach((fn) => fn(ev));
    }
    closest(selector) {
        let node = this.parentNode;
        while (node) {
            if (matchesAny(node, selector)) { return node; }
            node = node.parentNode;
        }
        return null;
    }
    querySelector(selector) { return queryAll(this, selector)[0] || null; }
    querySelectorAll(selector) { return queryAll(this, selector); }
    focus() {} blur() {} scrollIntoView() {} remove() {}
}

/* ------------------------------------------------------- 夹具（合成设置页） */

function buildWorld(fieldNames) {
    const root = new El('div');
    root.id = 'settings-tabContent';

    const chrome = new El('div');
    root.appendChild(chrome);
    const searchInput = new El('input');
    searchInput.id = 'settings-search-input';
    const searchHint = new El('div');
    searchHint.id = 'settings-search-hint';
    const searchClear = new El('button');
    searchClear.id = 'settings-search-clear';
    [searchInput, searchHint, searchClear].forEach((el) => chrome.appendChild(el));

    fieldNames.forEach((name, index) => {
        const pane = new El('div');
        pane.className = 'tab-pane';
        pane.id = 'vtab-fixture-' + index;
        root.appendChild(pane);

        const sectionHeader = new El('div');
        sectionHeader.className = 'settings-section-header';
        const h3 = new El('h3');
        h3.textContent = '合成分组' + index;
        sectionHeader.appendChild(h3);
        pane.appendChild(sectionHeader);

        const card = new El('article');
        card.className = 'settings-card';
        pane.appendChild(card);
        const cardHeader = new El('div');
        cardHeader.className = 'settings-card-header';
        const h4 = new El('h4');
        h4.textContent = '合成卡片' + index;
        cardHeader.appendChild(h4);
        card.appendChild(cardHeader);

        const box = new El('div');
        box.className = 'form-group';
        box.setAttribute('data-fixture-name', name);
        card.appendChild(box);
        const input = new El('input');
        input.name = name;
        input.id = 'fixture-' + index;
        input.setAttribute('name', name);
        box.appendChild(input);
        const label = new El('label');
        label.setAttribute('for', 'fixture-' + index);
        label.textContent = '设置项 ' + name;
        box.appendChild(label);
    });

    const docListeners = [];
    const timers = new Map();
    let timerSeq = 0;

    const document = {
        createElement: (tag) => new El(tag),
        getElementById: (id) => root.descendants.find((el) => el.id === id) || null,
        querySelector: (selector) => queryAll(root, selector)[0] || null,
        querySelectorAll: (selector) => queryAll(root, selector),
        addEventListener: (type, fn) => docListeners.push({ type, fn }),
    };

    const context = {
        document,
        window: {},
        console,
        Set, Map, RegExp, Math, JSON, String, Number, Object, Array, Error,
        setTimeout: (fn) => { timerSeq += 1; timers.set(timerSeq, fn); return timerSeq; },
        clearTimeout: (id) => timers.delete(id),
    };

    return {
        context,
        root,
        searchInput,
        searchHint,
        get resultsPanel() { return document.getElementById('settings-search-results'); },
        fireDomContentLoaded() {
            docListeners.filter((entry) => entry.type === 'DOMContentLoaded')
                .forEach((entry) => entry.fn());
        },
        flushTimers() {
            const pending = Array.from(timers.values());
            timers.clear();
            pending.forEach((fn) => fn());
        },
        highlightedName() {
            const hit = queryAll(root, '.settings-field-hit');
            return hit.length ? hit[0].getAttribute('data-fixture-name') : null;
        },
    };
}

/* ---------------------------------------------------------------- 场景驱动 */

function fixtureNames(count) {
    return Array.from({ length: count }, (unused, index) =>
        'FIXTURE_' + String(index).padStart(2, '0'));
}

function runScenario(source, scenario) {
    const world = buildWorld(fixtureNames(scenario.count));
    vm.createContext(world.context);
    vm.runInContext(source, world.context, { filename: 'settings-search.js' });
    world.fireDomContentLoaded();

    // 输入查询串并让 debounce 真正跑一次（输入阶段不跳转，只列结果）
    world.searchInput.value = scenario.query;
    world.searchInput.dispatch('input', {});
    world.flushTimers();

    const renderedCount = world.resultsPanel
        ? world.resultsPanel.querySelectorAll('.settings-search-result').length : 0;

    // 可选：换一个查询串，但 debounce 尚未触发就直接按 Enter
    if (scenario.thenQuery) {
        world.searchInput.value = scenario.thenQuery;
        world.searchInput.dispatch('input', {});
    }

    scenario.keys.forEach((key) => world.searchInput.dispatch('keydown', { key }));
    const activeDescendant = world.searchInput.getAttribute('aria-activedescendant');
    const expandedBeforeEnter = world.searchInput.getAttribute('aria-expanded');

    world.searchInput.dispatch('keydown', { key: 'Enter' });

    return {
        name: scenario.name,
        renderedCount,
        activeDescendant,
        expandedBeforeEnter,
        expandedAfterEnter: world.searchInput.getAttribute('aria-expanded'),
        panelHiddenAfterEnter: world.resultsPanel
            ? world.resultsPanel.classList.contains('d-none') : null,
        hint: world.searchHint.textContent,
        highlighted: world.highlightedName(),
    };
}

const SCENARIOS = [
    { name: '未按方向键时 Enter 跳第一条', count: 3, query: 'fixture', keys: [],
      expect: 'FIXTURE_00' },
    // 回归用例：renderResults() 重置 activeIndex 后 runSearch() 才读取它，
    // 会导致这两条都跳回 FIXTURE_00。
    { name: '下移一次后 Enter 跳第一项', count: 3, query: 'fixture', keys: ['ArrowDown'],
      expect: 'FIXTURE_00' },
    { name: '下移两次后 Enter 跳第二项', count: 3, query: 'fixture',
      keys: ['ArrowDown', 'ArrowDown'], expect: 'FIXTURE_01' },
    { name: '下移三次后 Enter 跳第三项', count: 3, query: 'fixture',
      keys: ['ArrowDown', 'ArrowDown', 'ArrowDown'], expect: 'FIXTURE_02' },
    { name: '上移一次回绕到最后一项', count: 3, query: 'fixture', keys: ['ArrowUp'],
      expect: 'FIXTURE_02' },
    { name: '下移后上移回绕到最后一项', count: 3, query: 'fixture',
      keys: ['ArrowDown', 'ArrowUp'], expect: 'FIXTURE_02' },
    { name: '下移次数超过结果数时在结果内回绕', count: 3, query: 'fixture',
      keys: ['ArrowDown', 'ArrowDown', 'ArrowDown', 'ArrowDown', 'ArrowDown'],
      expect: 'FIXTURE_01' },
    { name: '结果超过上限时仍在已渲染项内回绕', count: 25, query: 'fixture',
      keys: Array.from({ length: RESULT_LIMIT + 1 }, () => 'ArrowDown'), expect: 'FIXTURE_00' },
    { name: '结果数不足上限时按下移不越界', count: 25, query: 'fixture_0',
      keys: ['ArrowDown', 'ArrowDown'], expect: 'FIXTURE_01' },
    // 换了查询串时旧选中态应作废（Enter 复用同一次查询时才保留）。
    { name: '改查询后 Enter 跳新结果第一项', count: 12, query: 'fixture',
      keys: ['ArrowDown', 'ArrowDown'], thenQuery: 'fixture_0', expect: 'FIXTURE_00' },
];

module.exports = { runScenario, SCENARIOS };

if (require.main === module) {
    const sourcePath = process.argv[2];
    if (!sourcePath) {
        process.stderr.write('用法: node settings_search_keyboard.js <提取出的JS文件>\n');
        process.exit(2);
    }
    const source = fs.readFileSync(sourcePath, 'utf8');
    const results = SCENARIOS.map((scenario) => {
        try {
            const observation = runScenario(source, scenario);
            observation.expect = scenario.expect;
            observation.ok = observation.highlighted === scenario.expect;
            return observation;
        } catch (err) {
            return { name: scenario.name, expect: scenario.expect, ok: false,
                     error: String(err && err.stack ? err.stack : err) };
        }
    });
    process.stdout.write(JSON.stringify({
        scenarios: results,
        failed: results.filter((item) => !item.ok).map((item) => item.name),
    }, null, 2));
}
