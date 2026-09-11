# -*- coding: utf-8 -*-
"""窗口级失败统计的回归测试。

覆盖三类语义：
1. abort 之后被取消/未执行的窗口必须计入失败，否则失败占比被系统性低估；
2. 同一轮转录内的多批窗口（VAD 主路径 + fallback）取较差值，后一批的乐观
   统计不得覆盖先前的降级信号；
3. 各窗口规模下的 abort 容错阈值取值，以及「失败占比低于阈值不应降级」的
   反向守卫。

所有 ASR 调用都被 fake 替换，测试不访问网络。
"""

import os
import tempfile
import threading
import time
import unittest

from modules.asr_api_client import AsrApiClient, AsrConfig
from modules.subtitle_pipeline_types import (
    AsrSegmentTiming,
    AsrTranscriptionResult,
    DetectedSpeechWindow,
)


# 与 modules/speech_recognition.py 保持一致：失败占比达到该值即判为 degraded。
QUALITY_DEGRADED_FAILURE_RATIO = 0.5


def make_windows(count):
    """构造 count 个等长窗口，用于驱动并发批次。"""
    return [
        (
            DetectedSpeechWindow(
                start_s=float(i),
                end_s=float(i) + 1.0,
                ownership_start_s=float(i),
                ownership_end_s=float(i) + 1.0,
            ),
            f'w{i}.wav',
        )
        for i in range(count)
    ]


def failed_result(window):
    return AsrTranscriptionResult(
        provider='whisper',
        response_format='',
        timestamp_mode='none',
        window=window,
        failure_token='asr_failed',
    )


def ok_result(window):
    return AsrTranscriptionResult(
        provider='whisper',
        response_format='verbose_json',
        timestamp_mode='segment',
        text='ok',
        segments=[AsrSegmentTiming(start_s=0.0, end_s=1.0, text='ok')],
        window=window,
    )


def make_client(max_workers):
    """构造已完成能力协商的 client，避免串行探测干扰窗口顺序。"""
    client = AsrApiClient(AsrConfig(api_key='', max_workers=max_workers))
    client._capability_cache.transcription_format = 'verbose_json'
    return client


def install_sequential_transcriber(client, outcomes, per_call_delay=0.0):
    """按调用顺序依次返回 outcomes 中的结局。

    outcomes 中 True 表示该次调用返回失败结果，False 表示成功；超出部分一律成功。
    """
    lock = threading.Lock()
    counter = {'n': 0}

    def fake(wav_path, window=None, segment_info=None):
        with lock:
            index = counter['n']
            counter['n'] += 1
        if per_call_delay:
            # 制造真实的任务在途时间，让 abort 时的取消行为可预期。
            time.sleep(per_call_delay)
        if index < len(outcomes) and outcomes[index]:
            return failed_result(window)
        return ok_result(window)

    client.transcribe_window = fake
    return counter


def install_patterned_transcriber(client, fail_count, total_calls, per_call_delay=0.0):
    """前 fail_count 次调用失败，其余成功；按调用顺序判定而非按窗口号。"""
    outcomes = [True] * fail_count + [False] * max(0, total_calls - fail_count)
    return install_sequential_transcriber(client, outcomes, per_call_delay=per_call_delay)


def count_stubs(results):
    """产物中 asr_failed 结果的总数（真实失败 + 被取消窗口补齐的 stub）。"""
    return sum(1 for item in results if item.failure_token == 'asr_failed' and not item.ok)


def count_placeholders(results, executed_indexes):
    """只统计「未执行窗口」产生的占位结果，用于验证 abort 的取消记账。"""
    return sum(
        1
        for item in results
        if not item.ok and round(float(item.window.start_s)) not in executed_indexes
    )


class AbortThresholdTests(unittest.TestCase):
    """abort 容错阈值的表驱动断言。"""

    # (窗口数, 期望容错次数)：低于阈值不得 abort，达到阈值才 abort。
    EXPECTED_TOLERANCE = [
        (1, 1),
        (4, 1),
        (5, 1),
        (8, 2),
        (10, 2),
        (11, 2),
        (13, 2),
        (20, 3),
        (40, 5),
    ]

    def test_threshold_never_reaches_old_half_batch_allowance(self):
        """旧的 max(3, n*0.15) 在 n<20 时会容错 3 次，新公式必须收紧。"""
        # n=4：旧公式容忍 3 次失败（75%），新公式只容忍 1 次；
        # n=8：旧公式容忍 3 次（38%），新公式只容忍 2 次（25%）；
        # n=10：旧公式容忍 3 次（30%），新公式只容忍 2 次（20%）。
        # n>=16 时两者都回落到 3，由下一个用例覆盖。
        cases = [(4, 3), (8, 3), (10, 3)]
        for window_count, old_tolerance in cases:
            with self.subTest(window_count=window_count):
                new_tolerance = max(min(3, window_count // 4), int(window_count * 0.15))
                self.assertLess(new_tolerance, old_tolerance)

    def test_threshold_at_large_batches_is_unchanged(self):
        """n>=16 时下限不再主导，公式必须回落到原先的 15% 量级。"""
        for window_count, expected in ((16, 3), (20, 3), (40, 6), (100, 15)):
            with self.subTest(window_count=window_count):
                self.assertEqual(
                    max(min(3, window_count // 4), int(window_count * 0.15)),
                    expected,
                )

    def test_failures_below_threshold_do_not_shrink_batch(self):
        for window_count, tolerance in self.EXPECTED_TOLERANCE:
            if tolerance <= 1:
                # 容错为 1 时「低于阈值」等于零失败，由零失败用例覆盖。
                continue
            with self.subTest(window_count=window_count, tolerance=tolerance):
                failed = tolerance - 1
                client = make_client(max_workers=window_count)
                install_patterned_transcriber(
                    client, fail_count=failed, total_calls=window_count
                )
                results = client.transcribe_windows_concurrent(make_windows(window_count))

                # 未触发 abort：每个窗口都真实执行过，失败数就是注入的失败数。
                self.assertEqual(len(results), window_count)
                self.assertEqual(count_stubs(results), failed)
                # last_failed_count 取本轮最差占比折算值：failed/window_count
                # 乘以本轮最大批次规模，这里与注入值一致。
                self.assertEqual(client.last_failed_count, failed)
                self.assertEqual(client.last_window_count, window_count)
                self.assertLess(
                    client.last_failure_ratio, QUALITY_DEGRADED_FAILURE_RATIO
                )

    def test_failures_reaching_threshold_abort_remaining_windows(self):
        # 全部窗口都失败时，无论 abort 是否来得及取消任务，产物与统计都必须是
        # 「全批失败」——取消只影响耗时，不影响结局。n=1 时没有剩余窗口可取消。
        for window_count, tolerance in self.EXPECTED_TOLERANCE:
            with self.subTest(window_count=window_count, tolerance=tolerance):
                client = make_client(max_workers=window_count)
                install_patterned_transcriber(
                    client, fail_count=window_count, total_calls=window_count
                )
                results = client.transcribe_windows_concurrent(make_windows(window_count))

                self.assertEqual(len(results), window_count)
                self.assertEqual(count_stubs(results), window_count)
                self.assertEqual(client.last_failed_count, window_count)
                self.assertEqual(client.last_window_count, window_count)
                self.assertAlmostEqual(client.last_failure_ratio, 1.0)


class CancelledWindowAccountingTests(unittest.TestCase):
    """abort 之后被取消 / 从未执行的窗口必须计入失败占比。"""

    def test_cancelled_windows_counted_as_failures(self):
        """abort 之后被取消 / 从未执行的窗口必须计入失败占比。

        n=10 的容错阈值是 2：前两个任务各延迟 50ms 失败，abort 在第 2 个失败
        结果被取回时触发，此时队列里仍有窗口没启动，取消必然生效。旧实现里
        last_failed_count 停在 2（占比 0.2 < 0.5），降级信号因此消失。
        """
        client = make_client(max_workers=2)
        gate = threading.Event()
        executed = set()
        lock = threading.Lock()
        call_order = {'n': 0}

        def fake(wav_path, window=None, segment_info=None):
            with lock:
                index = call_order['n']
                call_order['n'] += 1
            executed.add(round(float(window.start_s)))
            if index < 2:
                time.sleep(0.05)
                return failed_result(window)
            # 第 3 个任务负责触发 abort；再往后的任务应当被取消而不会启动。
            if index == 2:
                return failed_result(window)
            # 若个别排队任务在取消生效前被 worker 取走，这里只需短暂等待，
            # 避免线程池 shutdown 阻塞测试。
            gate.wait(timeout=0.5)
            return failed_result(window)

        client.transcribe_window = fake
        try:
            results = client.transcribe_windows_concurrent(make_windows(10))
        finally:
            gate.set()
        # 线程池已随 with 语句退出，executed 此刻稳定，可以安全读取快照。
        with lock:
            executed_snapshot = set(executed)

        self.assertEqual(len(results), 10)
        placeholders = count_placeholders(results, executed_snapshot)
        self.assertGreaterEqual(placeholders, 1)
        # 每个窗口都有结局：已执行的窗口数 + 被取消窗口补齐的占位结果 == 批量规模。
        self.assertEqual(len(executed_snapshot) + placeholders, 10)
        self.assertTrue(all(not item.ok for item in results))
        # 旧实现只把真实失败的窗口计入占比，停在 2/10 = 0.2。
        self.assertEqual(client.last_failed_count, 10)
        self.assertEqual(client.last_window_count, 10)
        self.assertAlmostEqual(client.last_failure_ratio, 1.0)
        self.assertGreaterEqual(client.last_failure_ratio, QUALITY_DEGRADED_FAILURE_RATIO)

    def test_batch_result_length_always_matches_window_count(self):
        """无论是否 abort，产物长度与统计口径都必须覆盖全部窗口。"""
        client = make_client(max_workers=2)
        install_patterned_transcriber(
            client, fail_count=8, total_calls=8, per_call_delay=0.05
        )
        results = client.transcribe_windows_concurrent(make_windows(8))

        self.assertEqual(len(results), 8)
        self.assertTrue(all(item.window is not None for item in results))
        self.assertEqual(client.last_failed_count, 8)
        self.assertEqual(client.last_window_count, 8)

    def test_partial_abort_ratio_reflects_real_damage(self):
        """规模 8、容错 2：第 2 次失败即作废整批，占比必须是 1.0 而不是 0.25。"""
        client = make_client(max_workers=1)
        install_patterned_transcriber(
            client, fail_count=8, total_calls=8, per_call_delay=0.2
        )
        results = client.transcribe_windows_concurrent(make_windows(8))

        self.assertEqual(len(results), 8)
        self.assertTrue(all(not item.ok for item in results))
        self.assertEqual(client.last_failed_count, 8)
        self.assertAlmostEqual(client.last_failure_ratio, 1.0)
        # 旧实现只记 2/8 = 0.25，会漏掉降级。
        self.assertGreater(client.last_failure_ratio, 2 / 8)

    def test_clean_batch_reports_zero(self):
        client = make_client(max_workers=4)
        install_patterned_transcriber(client, fail_count=0, total_calls=20)
        results = client.transcribe_windows_concurrent(make_windows(20))

        self.assertEqual(count_stubs(results), 0)
        self.assertEqual(client.last_failed_count, 0)
        self.assertEqual(client.last_failure_ratio, 0.0)
        self.assertEqual(client.last_window_count, 20)


class RoundAggregationTests(unittest.TestCase):
    """同一轮内多批窗口取较差值，fallback 不得覆盖主路径的失败信号。"""

    def test_fallback_optimistic_batch_does_not_hide_vad_failure(self):
        # 主路径 8 个窗口、容错 2 → 2 次失败即 abort，整批失败（占比 1.0）。
        client = make_client(max_workers=4)
        install_patterned_transcriber(
            client, fail_count=8, total_calls=8, per_call_delay=0.05
        )
        client.transcribe_windows_concurrent(make_windows(8))
        main_ratio = client.last_failure_ratio
        self.assertAlmostEqual(main_ratio, 1.0)

        # fallback 只有 3 个窗口且全部成功，不应把失败占比清零。
        install_patterned_transcriber(client, fail_count=0, total_calls=3)
        client.transcribe_windows_concurrent(make_windows(3))

        self.assertAlmostEqual(client.last_failure_ratio, main_ratio)
        # 最差批次口径：失败占比与窗口规模都沿用主路径批次，fallback 不稀释它。
        self.assertEqual(client.last_failed_count, 8)
        self.assertEqual(client.last_window_count, 8)
        self.assertGreaterEqual(client.last_failure_ratio, QUALITY_DEGRADED_FAILURE_RATIO)

    def test_worse_later_batch_wins(self):
        """后一批更差时同样取较差值（回归：不能被「保留旧值」写成取较小值）。"""
        client = make_client(max_workers=1)
        install_patterned_transcriber(client, fail_count=1, total_calls=20)
        client.transcribe_windows_concurrent(make_windows(20))
        self.assertAlmostEqual(client.last_failure_ratio, 1 / 20)

        install_patterned_transcriber(
            client, fail_count=12, total_calls=12, per_call_delay=0.05
        )
        client.transcribe_windows_concurrent(make_windows(12))

        self.assertAlmostEqual(client.last_failure_ratio, 1.0)
        # 后一批更差：最差占比为 1.0，窗口规模仍是本轮最大的 20。
        self.assertEqual(client.last_failed_count, 20)
        self.assertEqual(client.last_window_count, 20)

    def test_reset_quality_counters_starts_new_round(self):
        client = make_client(max_workers=4)
        install_patterned_transcriber(
            client, fail_count=8, total_calls=8, per_call_delay=0.05
        )
        client.transcribe_windows_concurrent(make_windows(8))
        self.assertGreater(client.last_failure_ratio, 0.0)

        client.reset_quality_counters()
        self.assertEqual(client.last_failure_ratio, 0.0)
        self.assertEqual(client.last_failed_count, 0)
        self.assertEqual(client.last_window_count, 0)

        install_patterned_transcriber(client, fail_count=0, total_calls=6)
        client.transcribe_windows_concurrent(make_windows(6))
        self.assertEqual(client.last_failure_ratio, 0.0)
        self.assertEqual(client.last_window_count, 6)

    def test_empty_batch_keeps_documented_semantics(self):
        client = make_client(max_workers=2)
        install_patterned_transcriber(
            client, fail_count=4, total_calls=4, per_call_delay=0.05
        )
        client.transcribe_windows_concurrent(make_windows(4))
        self.assertGreater(client.last_failure_ratio, 0.0)

        # 空批次代表本轮没有窗口，按既有语义清零。
        self.assertEqual(client.transcribe_windows_concurrent([]), [])
        self.assertEqual(client.last_failure_ratio, 0.0)
        self.assertEqual(client.last_failed_count, 0)
        self.assertEqual(client.last_window_count, 0)


class QualityStateGuardTests(unittest.TestCase):
    """守卫：只有失败占比真正越线时才允许降级。"""

    def _classify(self, failure_ratio, window_count, produced=True):
        """用真实 recognizer 的裁决逻辑给结局分类（不依赖它的构造过程）。"""
        import logging  # noqa: WPS433 - 仅用于装配替身的 logger
        from modules import speech_recognition as sr  # noqa: WPS433 - 延迟导入，隔离副作用

        recognizer = object.__new__(sr.SpeechRecognizer)
        recognizer.logger = logging.getLogger('test.asr_quality_guard')
        recognizer.last_warning_message = ''
        recognizer.last_error_message = ''
        recognizer.last_quality_state = 'ok'
        recognizer.last_degraded_reasons = []
        recognizer._asr = _FakeAsrStats(failure_ratio, window_count)

        handle = None
        output_path = None
        try:
            if produced:
                # produced 判定基于输出文件真实存在，这里落地一个临时文件。
                fd, output_path = tempfile.mkstemp(suffix='.srt')
                os.close(fd)
                handle = output_path
            recognizer._resolve_quality_state(output_path)
        finally:
            if handle:
                os.remove(handle)
        return recognizer.last_quality_state

    def test_ratio_below_threshold_is_not_degraded(self):
        # 4/20 = 0.2，远低于 0.5，必须保持 ok。
        self.assertEqual(self._classify(0.2, 20), 'ok')
        # 边界内侧：9/20 = 0.45 仍不降级。
        self.assertEqual(self._classify(0.45, 20), 'ok')

    def test_ratio_at_or_above_threshold_degrades(self):
        self.assertEqual(self._classify(QUALITY_DEGRADED_FAILURE_RATIO, 20), 'degraded')
        self.assertEqual(self._classify(0.6, 20), 'degraded')

    def test_all_windows_failed_is_failed(self):
        self.assertEqual(self._classify(1.0, 20), 'failed')

    def test_no_subtitle_output_is_failed_regardless_of_ratio(self):
        # 反向守卫的另一半：没有任何窗口失败也不能把「无产物」判成 ok。
        self.assertEqual(self._classify(0.0, 20, produced=False), 'failed')

    def test_guard_uses_the_same_ratio_the_client_reports(self):
        """端到端口径：client 上报的占比直接喂给真实裁决逻辑，不产生降级。

        注入的失败数必须低于容错阈值，否则 abort 会把批次作废、占比升到 1.0。
        """
        for window_count, fail_count in ((20, 2), (40, 5), (100, 14)):
            with self.subTest(window_count=window_count, fail_count=fail_count):
                client = make_client(max_workers=2)
                completed = install_patterned_transcriber(
                    client, fail_count=fail_count, total_calls=window_count
                )
                results = client.transcribe_windows_concurrent(make_windows(window_count))

                # 未触发 abort：没有窗口被作废，失败样本数与注入值一致。
                self.assertEqual(len(results), window_count)
                self.assertEqual(completed['n'], window_count)
                ratio = client.last_failure_ratio
                self.assertLess(ratio, QUALITY_DEGRADED_FAILURE_RATIO)
                self.assertEqual(client.last_window_count, window_count)
                self.assertEqual(
                    self._classify(ratio, client.last_window_count), 'ok'
                )

    def test_guard_degrades_on_client_reported_abort_ratio(self):
        """abort 作废整批后，client 上报的占比必须足以触发真实降级。"""
        client = make_client(max_workers=2)
        completed = install_patterned_transcriber(
            client, fail_count=6, total_calls=6, per_call_delay=0.2
        )
        client.transcribe_windows_concurrent(make_windows(6))

        self.assertLessEqual(completed['n'], 6)
        self.assertEqual(client.last_failure_ratio, 1.0)
        self.assertEqual(
            self._classify(client.last_failure_ratio, client.last_window_count),
            'failed',
        )


class RecognizerIntegrationTests(unittest.TestCase):
    """真实 SpeechRecognizer 消费 asr_api_client 统计口径的集成验证。"""

    def _make_recognizer(self):
        from modules import speech_recognition as sr  # noqa: WPS433 - 延迟导入，隔离副作用

        # api_key 留空，AsrApiClient 不会真正初始化 SDK，测试全程离线。
        config = sr.SpeechRecognitionConfig(api_key='', max_workers=2)
        return sr.SpeechRecognizer(config, task_id='test_failure_stats')

    def test_aborted_batch_degrades_through_real_recognizer(self):
        """abort 导致整批失败时，真实 recognizer 必须判为 degraded 而非 ok。"""
        recognizer = self._make_recognizer()
        client = recognizer._asr
        client._capability_cache.transcription_format = 'verbose_json'
        install_patterned_transcriber(
            client, fail_count=10, total_calls=10, per_call_delay=0.05
        )
        client.transcribe_windows_concurrent(make_windows(10))

        self.assertAlmostEqual(client.last_failure_ratio, 1.0)
        # speech_recognition 直接读 client 的这两个字段，无需为本次修复改它。
        recognizer._resolve_quality_state(None)
        self.assertEqual(recognizer.last_quality_state, 'failed')
        self.assertTrue(
            any('all_windows_failed' in reason for reason in recognizer.last_degraded_reasons)
        )

    def test_few_failures_keep_recognizer_ok(self):
        """少量窗口失败时 recognizer 的输出仍判 ok，不会误触发门控。"""
        recognizer = self._make_recognizer()
        client = recognizer._asr
        client._capability_cache.transcription_format = 'verbose_json'
        install_patterned_transcriber(client, fail_count=2, total_calls=20)
        client.transcribe_windows_concurrent(make_windows(20))

        self.assertLess(client.last_failure_ratio, QUALITY_DEGRADED_FAILURE_RATIO)
        # recognizer 直接消费 client 的统计，无需为本次修复改 speech_recognition。
        fd, output_path = tempfile.mkstemp(suffix='.srt')
        os.close(fd)
        try:
            recognizer._resolve_quality_state(output_path)
        finally:
            os.remove(output_path)
        self.assertEqual(recognizer.last_quality_state, 'ok')
        self.assertEqual(recognizer.last_degraded_reasons, [])


class _FakeAsrStats:
    """只暴露鉴权质量结局所需字段的 ASR 统计替身。"""

    def __init__(self, failure_ratio, window_count):
        self.last_failure_ratio = failure_ratio
        self.last_window_count = window_count
        self.last_failed_count = 0


if __name__ == '__main__':
    unittest.main()
