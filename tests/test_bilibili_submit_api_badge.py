# -*- coding: utf-8 -*-
"""投稿接口徽标的渲染链路测试。

复审提醒：`BILIBILI_SUBMIT_API` 的结果会写进
`bilibili_upload_response`（JSON）→ 任务列表/卡片读取 `submit_api` 渲染徽标，
这条链路此前没有任何用例。这里直接渲染两个 partial，覆盖：

* 必剪通道 → 「必剪」徽标；
* Web 通道 → 「Web」徽标；
* 未登记的取值（例如历史数据里的 auto）→ 不渲染徽标，而不是错误地显示成 Web。
"""
import json
import unittest

import app as web_app


class _TaskStub:
    """占位任务对象：模板里没显式赋值的字段一律返回 None。"""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

    def __getattr__(self, name):
        return None


class SubmitApiBadgeRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        web_app.app.config['TESTING'] = True

    def _render(self, template_name, upload_response, target='bilibili'):
        task = _TaskStub(
            # 模板对 task.id 做了 task.id[:8] 截断，必须是字符串
            id='abcdef1234567890',
            status='completed',
            upload_target=target,
            video_title_original='标题',
            bilibili_upload_response=json.dumps(upload_response, ensure_ascii=False),
            acfun_upload_response=json.dumps({'ac_number': 1001}),
        )
        with web_app.app.test_request_context('/'):
            return web_app.app.jinja_env.get_template(
                'partials/{}'.format(template_name)
            ).render(task=task, config={})

    def test_bcut_pc_renders_required_badge(self):
        for template in ('task_row.html', 'task_card.html'):
            for target in ('bilibili', 'both'):
                with self.subTest(template=template, target=target):
                    html = self._render(
                        template,
                        {'bvid': 'BV1bcut', 'aid': 1, 'submit_api': 'bcut_pc'},
                        target=target,
                    )
                    self.assertIn('BV1bcut', html)
                    self.assertIn('>必剪</span>', html)
                    self.assertNotIn('>Web</span>', html)

    def test_web_renders_web_badge(self):
        for template in ('task_row.html', 'task_card.html'):
            with self.subTest(template=template):
                html = self._render(
                    template, {'bvid': 'BV1web', 'aid': 2, 'submit_api': 'web'}
                )
                self.assertIn('>Web</span>', html)
                self.assertNotIn('>必剪</span>', html)

    def test_unknown_value_renders_no_badge(self):
        for value in ('auto', '', 'bcut'):
            with self.subTest(value=value):
                html = self._render(
                    'task_row.html', {'bvid': 'BV1old', 'aid': 3, 'submit_api': value}
                )
                self.assertIn('BV1old', html)
                self.assertNotIn('投稿接口', html)


if __name__ == '__main__':
    unittest.main()
