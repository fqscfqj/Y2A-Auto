"""设置页「保存设置」提交流程的运行时行为测试。

为什么单独有这个文件：tests/test_settings_template_layout.py 全部是静态字符串断言，
它在结构上抓不到渲染后 JS 的运行时逻辑；`node --check` 只能发现语法错误，发现不了
「引用了不存在的变量」。实际发生过这类回归 —— 9 分组重构删掉了全局
``#reset-settings-btn`` 及其 ``const resetBtn`` 声明，却漏改了
``toggleSettingsSaveBusy`` 里的 ``[saveSettingsBtn, resetBtn].forEach(...)``。
submit 处理器因此在 ``new FormData`` / ``fetch`` 之前就抛
``ReferenceError: resetBtn is not defined``：页面能正常渲染、按钮能点、
控制台之外没有任何提示，但保存请求根本发不出去 —— 表现就是「点保存没反应」。

本测试从渲染后的 /settings 页面提取**真实**主脚本，放进 Node + 最小 DOM 桩里执行
（tests/js/settings_save_submit.js），然后模拟一次 submit，覆盖：
请求真的发出去了（POST /settings 且带 save_operation_id）、提交期间卡片级「重置」
按钮被一并禁用、以及提交路径不再抛异常。

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
HARNESS = REPO_ROOT / 'tests' / 'js' / 'settings_save_submit.js'

# 提取锚点：模板里真实的主脚本起点，改动设置页脚本时该锚点应保持稳定。
SCRIPT_START = 'const settingsEscapeSelector = function (value) {'
SCRIPT_END = '</script>'


def extract_main_script(page: str) -> str:
    """从渲染后的页面里取出主 <script> 的源码（含顶层共享辅助函数）。"""
    try:
        start = page.index(SCRIPT_START)
        end = page.index(SCRIPT_END, start)
    except ValueError as exc:  # pragma: no cover - 只在模板被大改时触发
        raise AssertionError(
            f'无法从渲染页面提取设置页脚本（模板结构可能已变）：{exc}') from exc
    return page[start:end]


@unittest.skipUnless(shutil.which('node'), '需要 node 运行时（不在 PATH 上时跳过）')
class SettingsSaveSubmitTests(unittest.TestCase):
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

        cls.source = extract_main_script(response.get_data(as_text=True))
        with tempfile.TemporaryDirectory() as tmp:
            js_path = pathlib.Path(tmp) / 'settings_page.js'
            js_path.write_text(cls.source, encoding='utf-8')
            proc = subprocess.run(
                [shutil.which('node'), str(HARNESS), str(js_path)],
                capture_output=True, text=True, encoding='utf-8',
                cwd=str(REPO_ROOT), timeout=120)
        try:
            cls.report = json.loads(proc.stdout)
        except ValueError as exc:
            raise AssertionError(
                'DOM 桩脚本没有输出可解析的 JSON:\n'
                f'--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}') from exc
        cls.proc = proc

    def test_提交处理器没有抛异常(self):
        # 这是本文件存在的理由：修复前这里会是
        # `ReferenceError: resetBtn is not defined`，请求发不出去。
        self.assertIsNone(
            self.report.get('submitError'),
            '设置页提交处理器执行时抛异常，保存请求发不出去：\n'
            + str(self.report.get('submitError')))
        self.assertIsNone(self.report.get('error'))

    def test_点击保存会发出POST请求(self):
        self.assertEqual(
            self.report.get('fetchCount'), 1,
            '点击保存后应恰好发出一次请求（丢失请求即「点保存没反应」）')
        self.assertEqual(self.report.get('fetchMethod'), 'POST')
        self.assertTrue(
            str(self.report.get('fetchUrl') or '').endswith('/settings'),
            f"保存请求应提交到 /settings，实际为 {self.report.get('fetchUrl')!r}")
        self.assertTrue(
            self.report.get('operationId'),
            'POST 表单里必须带上 save_operation_id（进度轮询依赖它）')

    def test_保存期间卡片重置按钮被一并禁用(self):
        # 全局重置入口在 9 分组重构后下沉为卡片级 .settings-card-reset，
        # 保存进行中仍应把它们禁用，避免保存与重置并发写配置。
        self.assertIs(
            self.report.get('cardResetDisabled'), True,
            '保存期间卡片级「重置」按钮没有被禁用')

    def test_桩脚本执行成功(self):
        # 防止桩脚本自身失败（例如选择器不支持）被当成「行为正确」而空跑通过。
        self.assertEqual(
            self.proc.returncode, 0,
            'DOM 桩脚本以非零状态退出:\n'
            f'--- stdout ---\n{self.proc.stdout}\n--- stderr ---\n{self.proc.stderr}')


if __name__ == '__main__':
    unittest.main()
