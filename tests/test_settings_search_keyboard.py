"""设置页「字段级搜索」的运行时行为测试。

为什么单独有这个文件：tests/test_settings_template_layout.py 全部是静态字符串断言，
它在结构上抓不到渲染后 JS 的运行时逻辑。实际发生过这类回归 ——
``renderResults()`` 开头把 ``activeIndex`` 重置为 -1，而 ``runSearch()`` 在它之后
才读取该下标，于是「方向键选中某项后按 Enter」恒跳第一条：选中态画得出来、
``aria-activedescendant`` 也对，但 Enter 不认。

本测试从渲染后的 /settings 页面提取**真实**脚本（共享标签辅助函数 + 搜索脚本），
放进 Node + 最小 DOM 桩里执行，覆盖：方向键选中 → Enter 的落点、结果数不足/超出
上限时的回绕、换查询串后旧选中态作废、面板收起与 aria 状态。

Node 不在 PATH 上时自动跳过（不新增 CI 依赖；仓库本身没有 package.json）。
"""
import json
import pathlib
import shutil
import subprocess
import tempfile
import unittest

import app as web_app


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
HARNESS = REPO_ROOT / 'tests' / 'js' / 'settings_search_keyboard.js'

# 提取锚点：模板里的真实注释与定义，改动搜索脚本时这两处锚点应保持稳定。
SEARCH_MARKER = '/* 设置项搜索：字段级索引 + 结果面板 + 定位高亮'
HELPERS_START = 'const settingsEscapeSelector = function (value) {'
HELPERS_LABEL = 'const settingsFieldLabel'
DOM_READY = "document.addEventListener('DOMContentLoaded'"


def extract_search_script(page: str) -> str:
    """从渲染后的页面里取出「共享辅助函数 + 搜索脚本」两段源码。

    两段原本是两个独立的 <script> 顶层声明，这里拼接成一个脚本执行，
    与浏览器里两个 script 标签共享顶层词法作用域的效果一致。
    """
    try:
        helpers_start = page.index(HELPERS_START)
        label_start = page.index(HELPERS_LABEL, helpers_start)
        helpers_end = page.index('\n};\n', label_start) + len('\n};\n')
        helpers = page[helpers_start:helpers_end]

        marker = page.index(SEARCH_MARKER)
        block_start = page.index(DOM_READY, marker)
        block_end = page.index('\n    });\n', block_start) + len('\n    });\n')
        block = page[block_start:block_end]
    except ValueError as exc:  # pragma: no cover - 只在模板被大改时触发
        raise AssertionError(
            f'无法从渲染页面提取搜索脚本（模板结构可能已变）：{exc}') from exc

    return helpers + '\n' + block


@unittest.skipUnless(shutil.which('node'), '需要 node 运行时（不在 PATH 上时跳过）')
class SettingsSearchKeyboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not HARNESS.exists():  # pragma: no cover - 仓库完整性检查
            raise AssertionError(f'缺少 DOM 桩脚本: {HARNESS}')
        web_app.app.config.update(TESTING=True)
        client = web_app.app.test_client()
        with client.session_transaction() as session:
            session['logged_in'] = True
        response = client.get('/settings')
        if response.status_code != 200:
            raise AssertionError(f'设置页渲染失败: {response.status_code}')

        cls.source = extract_search_script(response.get_data(as_text=True))
        with tempfile.TemporaryDirectory() as tmp:
            js_path = pathlib.Path(tmp) / 'settings_search.js'
            js_path.write_text(cls.source, encoding='utf-8')
            proc = subprocess.run(
                [shutil.which('node'), str(HARNESS), str(js_path)],
                capture_output=True, text=True, encoding='utf-8',
                cwd=str(REPO_ROOT), timeout=120)
        if proc.returncode != 0:
            raise AssertionError(
                'DOM 桩脚本执行失败:\n'
                f'--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}')
        cls.report = json.loads(proc.stdout)
        cls.by_name = {item['name']: item for item in cls.report['scenarios']}

    def test_所有键盘导航场景都通过(self):
        failures = []
        for item in self.report['scenarios']:
            if item.get('ok'):
                continue
            if 'error' in item:
                failures.append(f"{item['name']}: 执行异常 {item['error']}")
            else:
                failures.append(
                    f"{item['name']}: Enter 落到 {item['highlighted']}，"
                    f"期望 {item['expect']}")
        self.assertEqual(failures, [], '键盘导航行为不符合预期')

    def test_场景覆盖了方向键与回车(self):
        # 防止有人把场景表删空导致上面那条「空跑通过」
        self.assertGreaterEqual(len(self.report['scenarios']), 8)
        self.assertIn('未按方向键时 Enter 跳第一条', self.by_name)
        self.assertIn('下移两次后 Enter 跳第二项', self.by_name)

    def test_方向键确实改变了回车落点(self):
        # 这条断言是本文件存在的理由：修复前两个场景都落到 FIXTURE_00。
        without_arrow = self.by_name['未按方向键时 Enter 跳第一条']
        with_arrow = self.by_name['下移两次后 Enter 跳第二项']
        self.assertEqual(without_arrow['highlighted'], 'FIXTURE_00')
        self.assertEqual(with_arrow['highlighted'], 'FIXTURE_01')
        self.assertNotEqual(
            without_arrow['highlighted'], with_arrow['highlighted'],
            '方向键选中的项没有影响 Enter 的落点（Enter 忽略了选中态）')
        # 选中态本身必须同步到 aria-activedescendant
        self.assertEqual(with_arrow['activeDescendant'], 'settings-search-result-1')

    def test_输入阶段不跳转且提示可回车(self):
        scenario = self.by_name['未按方向键时 Enter 跳第一条']
        self.assertEqual(scenario['renderedCount'], 3, '输入后应列出全部匹配项')
        self.assertEqual(scenario['expandedBeforeEnter'], 'true')
        self.assertIn('按 Enter 跳转', scenario['hint'])

    def test_回车后结果面板收起(self):
        for name in ('未按方向键时 Enter 跳第一条', '下移两次后 Enter 跳第二项'):
            scenario = self.by_name[name]
            self.assertIs(scenario['panelHiddenAfterEnter'], True,
                          f'{name}: 回车跳转后结果面板应收起')
            self.assertEqual(scenario['expandedAfterEnter'], 'false',
                             f'{name}: 回车后 aria-expanded 应回到 false')

    def test_结果超出上限时只渲染上限条数(self):
        scenario = self.by_name['结果超过上限时仍在已渲染项内回绕']
        self.assertEqual(scenario['renderedCount'], 20)


if __name__ == '__main__':
    unittest.main()
