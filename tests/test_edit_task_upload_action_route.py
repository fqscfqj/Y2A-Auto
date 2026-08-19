"""
回归测试：编辑任务页「上传到 Bilibili」按钮在提交多值同名 action
（action=save_metadata&action=force_upload，旧缓存页面会出现）时，必须真正
触发上传，而非被 save_metadata 抢占只走保存。

这是 app.py 内 `_resolve_edit_task_action` 路由级行为的覆盖：直接打到
/tasks/<id>/edit 端点，用重复的表单字段模拟旧页面的多值提交，断言 force_upload
优先被选中（重定向到 manual_review 并触发后台上传），而非回退到 save_metadata
（重定向回编辑页）。

CI 环境已安装 Flask 等依赖，可直接运行；本地若缺少依赖则自动跳过。
"""
import unittest
from unittest.mock import patch, MagicMock

try:
    import app as web_app
    from werkzeug.datastructures import MultiDict
    _HAS_APP = True
except ImportError:
    # 仅依赖缺失（如本地未装 Flask / 项目依赖）时跳过；
    # 应用自身语法 / 初始化等其它错误应让测试失败，避免“绿了但没覆盖”的假阳性。
    _HAS_APP = False


@unittest.skipUnless(_HAS_APP, "app 依赖未安装，CI 环境可运行")
class EditTaskForceUploadMultiValueRouteTests(unittest.TestCase):
    def setUp(self):
        web_app.app.config['TESTING'] = True
        web_app.app.config['WTF_CSRF_ENABLED'] = False
        self.client = web_app.app.test_client()

    def _fake_task(self, status):
        return {
            'task_id': '1',
            'status': status,
            'upload_target': 'bilibili',
            'video_title_translated': '',
            'description_translated': '',
            'tags_generated': '[]',
            'selected_partition_id_bilibili': '123',
        }

    def _patch_stack(self, task_status):
        """统一打桩：绕过登录（load_config 返回空，password_protection 不启用）、
        任务读取与后台上传，使路由可走到 force_upload 分支。"""
        return (
            patch.object(web_app, 'get_task', return_value=self._fake_task(task_status)),
            patch.object(web_app, '_start_background_force_upload', lambda *a, **k: None),
            patch.object(web_app, 'update_task', MagicMock()),
            patch.object(web_app, 'load_config', return_value={}),
            patch.object(web_app, '_missing_upload_partition_labels', lambda *a, **k: []),
        )

    def test_multivalue_action_prioritizes_force_upload(self):
        # 旧缓存页面会同时提交两个 action，按 DOM 顺序第一个是 save_metadata
        patchers = self._patch_stack(web_app.TASK_STATES['DOWNLOADED'])
        for p in patchers:
            p.start()
        try:
            resp = self.client.post(
                '/tasks/1/edit',
                # Werkzeug 3.1 的 _iter_data() 要求 data.items() 存在，
                # plain list 会在请求发出前抛 AttributeError → 必须用 MultiDict
                # 保留 action 的 DOM 顺序（save_metadata 在前，force_upload 在后）。
                data=MultiDict([('action', 'save_metadata'), ('action', 'force_upload')]),
            )
        finally:
            for p in patchers:
                p.stop()

        # force_upload 分支重定向到 manual_review 并启动后台上传
        self.assertIn(resp.status_code, (302, 303))
        location = resp.headers.get('Location', '')
        self.assertIn('manual_review', location,
                      "多值 action 含 force_upload 时应走上传分支（重定向 manual_review），"
                      f"实际 Location={location}")

    def test_single_save_metadata_redirects_back_to_edit(self):
        # 单独提交 save_metadata 时应只保存，重定向回编辑页（非 manual_review）
        patchers = self._patch_stack(web_app.TASK_STATES['DOWNLOADED'])
        for p in patchers:
            p.start()
        try:
            resp = self.client.post(
                '/tasks/1/edit',
                data=MultiDict([('action', 'save_metadata')]),
            )
        finally:
            for p in patchers:
                p.stop()

        self.assertIn(resp.status_code, (302, 303))
        location = resp.headers.get('Location', '')
        self.assertNotIn('manual_review', location,
                         "仅提交 save_metadata 不应触发上传分支")
        self.assertIn('/tasks/1/edit', location,
                      "仅保存时应重定向回编辑页")


if __name__ == '__main__':
    unittest.main()
