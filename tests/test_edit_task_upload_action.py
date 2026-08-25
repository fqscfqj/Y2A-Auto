"""
回归测试：编辑任务页「上传」按钮必须真正触发上传，而非被隐藏的
save_metadata 字段抢占（同名 action 字段冲突）。

根因：主表单内存在两个同名为 action 的字段——隐藏字段
value="save_metadata"（始终提交）与上传按钮 value="force_upload"。
点击上传时二者同时提交，Werkzeug 的 request.form.get('action') 只取
表单源码中第一个出现的（隐藏的 save_metadata），导致 force_upload 分支
永不触发，点「上传」实际只走保存。

修复：隐藏字段改名 default_action；app.py 改用 getlist('action') 优先取
force_upload，回退到 default_action。本测试看守这个条件不回归。
"""
import pathlib
import re
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
TEMPLATE = REPO_ROOT / "templates" / "edit_task.html"


class EditTaskUploadActionRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = TEMPLATE.read_text(encoding="utf-8")

    def _main_form_block(self):
        # 截取主编辑表单（id=task-edit-form）的片段，用于精确断言
        m = re.search(
            r'<form method="post" action="\{\{ url_for\(.edit_task[^>]*'
            r'id="task-edit-form"[^>]*>.*?</form>',
            self.html, re.DOTALL)
        self.assertIsNotNone(m, "找不到主编辑表单 task-edit-form")
        return m.group(0)

    def test_hidden_save_metadata_field_renamed_to_default_action(self):
        self.assertIn(
            'name="default_action" value="save_metadata"',
            self.html,
            "主表单的隐藏保存字段应改名为 default_action，避免与上传按钮同名冲突",
        )

    def test_no_conflicting_hidden_action_save_metadata_in_main_form(self):
        main_form = self._main_form_block()
        self.assertNotIn(
            'name="action" value="save_metadata"',
            main_form,
            "主表单内不应再存在 name=action value=save_metadata 的隐藏字段"
            "（否则会与上传按钮同名，再次抢占 action）",
        )

    def test_upload_button_still_carries_action_force_upload(self):
        main_form = self._main_form_block()
        self.assertIn(
            'name="action" value="force_upload"',
            main_form,
            "上传按钮必须保留 name=action value=force_upload，否则无法触发上传",
        )

    def test_cover_actions_preserved(self):
        # 封面替换/恢复是各自独立的表单，其 name=action 不应被误改
        self.assertIn('name="action" value="replace_cover"', self.html)
        self.assertIn('name="action" value="restore_cover"', self.html)


if __name__ == "__main__":
    unittest.main()
