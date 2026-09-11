# -*- coding: utf-8 -*-
"""libx265 软编码路径的真实 FFmpeg 冒烟测试。

存在的理由：`tests/test_cpu_codec_x265.py` 只能断言「我们拼出的参数是否符合预期」，
无法发现「拼出来的参数 libx265 根本不接受」。本文件真的跑 ffmpeg。

**判定标准必须是 ffprobe 回读，而不是返回码。** 本仓库已经吃过一次教训：
x264/x265 对不认识的 VUI 枚举名只打印 "Error parsing option ..." 然后**返回 0**，
字段在 VUI 里留空 —— 只看 exit code 会得到完全的假绿。同样地，`-tune film` 这类
真失败才能靠返回码发现。两类都得测，且各自用对判据。

覆盖：
  1. `_VALID_X265_TUNES` 每个取值都能编码成功；
  2. 反向证据：x264 独有的 tune（film / stillimage）确实被 libx265 拒绝 ——
     这正是两张 tune 白名单必须分开的原因；
  3. 三张色彩表的每个取值经 `build_color_vui_params(..., 'x265')` 后都落进 VUI；
  4. `_SOFTWARE_VUI_UNSUPPORTED` 的取值不会产生语义错误的值；
  5. `colormatrix` 的 `rgb` 被统一改写成 `gbr`（不依赖未文档化的宽松解析）；
  6. `-tag:v hvc1` 真正写进容器；
  7. 两条 `-x265-params` 后者覆盖前者 —— 合并成一条的必要性证据；
  8. 完整增强参数串 + VUI 同时生效。

**避免版本敏感断言**：CI 上的 libx265 与本机自带的不是同一个版本。凡涉及
「某个枚举名是否被接受」的地方，判据都写成「不得产生错误语义」而不是
「必须严格等于某值」，否则升级 ffmpeg 会让测试无谓变红。真正必须严格相等的是
我们**自己写进命令**的值（见 tests/test_cpu_codec_x265.py）。

ffmpeg 不可用或该构建不含 libx265 时整类 skip，不失败。
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules import video_encoder_params as vep

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 探针输入：极小尺寸、极短时长。冒烟只关心选项解析与 VUI 写入，不关心画质。
_PROBE_INPUT = ['-f', 'lavfi', '-i', 'color=c=black:s=128x128:d=0.1']
_COMMON_OUT = ['-c:v', 'libx265', '-preset', 'ultrafast',
               '-pix_fmt', 'yuv420p', '-frames:v', '3']

_VUI_KEYS = {
    'colorprim': 'color_primaries',
    'transfer': 'color_transfer',
    'colormatrix': 'color_space',
}


def _find_binary(name):
    """按「仓库 ffmpeg/ 目录 -> PATH」的顺序定位二进制。"""
    local = os.path.join(_REPO_ROOT, 'ffmpeg', name + ('.exe' if os.name == 'nt' else ''))
    if os.path.isfile(local):
        return local
    return shutil.which(name)


FFMPEG = _find_binary('ffmpeg')
FFPROBE = _find_binary('ffprobe')


def _encoder_listed():
    """libx265 必须在当前 FFmpeg 构建里，否则本文件整类 skip。"""
    if not FFMPEG:
        return False
    try:
        proc = subprocess.run(
            [FFMPEG, '-hide_banner', '-encoders'],
            capture_output=True, timeout=30,
        )
    except Exception:
        return False
    return b'libx265' in proc.stdout


def _ffmpeg_usable():
    if not FFMPEG:
        return False
    try:
        return subprocess.run([FFMPEG, '-version'], capture_output=True,
                              timeout=15).returncode == 0
    except Exception:
        return False


def _probe_stream(path, entries='color_space,color_primaries,color_transfer,'
                                'color_range,codec_tag_string,codec_name,profile'):
    """回读输出文件的流字段。"""
    if not FFPROBE:
        return {}
    proc = subprocess.run(
        [FFPROBE, '-hide_banner', '-loglevel', 'error', '-select_streams', 'v:0',
         '-show_entries', f'stream={entries}', '-of', 'json', path],
        capture_output=True, timeout=30,
    )
    try:
        return json.loads(proc.stdout.decode('utf-8', 'replace'))['streams'][0]
    except Exception:
        return {}


@unittest.skipUnless(_ffmpeg_usable() and _encoder_listed(),
                     'ffmpeg 不可用或该构建不含 libx265，跳过 x265 冒烟测试')
class X265EncoderSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.mkdtemp(prefix='vep_x265_smoke_')

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._tmpdir, ignore_errors=True)

    def _run(self, extra, tag, extra_params=None, encode_args=None):
        """跑一次 ffmpeg，返回 (返回码, stderr 尾部, 流信息)。

        extra 追加在编码器选项之后（用于 -tune / -tag:v 之类）；
        extra_params 给出 `-x265-params` 的值时只传一次。
        """
        output = os.path.join(self._tmpdir, f'{tag}.mp4')
        if os.path.exists(output):
            os.remove(output)
        cmd = ([FFMPEG, '-hide_banner', '-nostdin', '-loglevel', 'error']
               + _PROBE_INPUT + (encode_args or _COMMON_OUT))
        if extra_params is not None:
            cmd += ['-x265-params', extra_params]
        cmd += list(extra) + ['-y', output]
        proc = subprocess.run(cmd, capture_output=True, timeout=180)
        stderr = proc.stderr.decode('utf-8', 'replace').strip()
        tail = stderr.splitlines()[-1] if stderr else ''
        info = _probe_stream(output) if proc.returncode == 0 else {}
        return proc.returncode, tail, info

    def _run_vui(self, color_map, tag):
        """按 x265 的 VUI 参数跑一次并回读。"""
        extra = vep.build_color_vui_params('cpu', color_map, None, 'x265')
        return self._run(extra, tag)

    # --- 基础形态 ---------------------------------------------------------

    def test_baseline_encodes_to_hevc(self):
        code, tail, info = self._run([], 'baseline')
        self.assertEqual(code, 0, tail)
        self.assertEqual(info.get('codec_name'), 'hevc')
        self.assertEqual(info.get('profile'), 'Main')

    def test_hvc1_tag_is_written(self):
        """hvc1 是 Safari / QuickTime 识别 HEVC 的前提。"""
        code, tail, info = self._run(['-tag:v', 'hvc1'], 'tag_hvc1')
        self.assertEqual(code, 0, tail)
        self.assertEqual(info.get('codec_tag_string'), 'hvc1')

    # --- tune 白名单 ------------------------------------------------------

    def test_every_x265_tune_is_accepted(self):
        self.assertTrue(vep._VALID_X265_TUNES, 'tune 白名单不能是空集')
        failures = []
        for tune in sorted(vep._VALID_X265_TUNES):
            code, tail, info = self._run(['-tune', tune], f'tune_{tune}')
            if code != 0 or not info:
                failures.append(f'-tune {tune}: rc={code} {tail[:120]}')
        self.assertEqual(failures, [], f'白名单里的 tune 被 libx265 拒绝：{failures}')

    def test_x264_only_tunes_really_fail_on_libx265(self):
        """反向证据：这就是两张 tune 白名单必须分开的原因。

        若哪天 libx265 开始接受 film / stillimage，本测试会失败，提示可以把
        两张表合并 —— 而不是让那条分流逻辑变成无人验证的死代码。
        """
        accepted = []
        for tune in ('film', 'stillimage'):
            self.assertNotIn(
                tune, vep._VALID_X265_TUNES,
                f'{tune} 不该出现在 x265 白名单里',
            )
            code, _, _ = self._run(['-tune', tune], f'bad_tune_{tune}')
            if code == 0:
                accepted.append(tune)
        self.assertEqual(
            accepted, [],
            f'libx265 竟然接受了 {accepted}，两张 tune 白名单可以合并了',
        )

    def test_shared_tunes_are_accepted_by_both_tables(self):
        for tune in ('animation', 'grain', 'psnr', 'ssim', 'fastdecode', 'zerolatency'):
            self.assertIn(tune, vep._VALID_X264_TUNES)
            self.assertIn(tune, vep._VALID_X265_TUNES)

    # --- VUI 回读 ---------------------------------------------------------

    def test_every_mapped_value_lands_in_the_vui(self):
        """三张表的每个取值都经 build_color_vui_params 走一遍并回读 VUI。

        期望值 = 该取值经别名表改名后的结果；_X264_PARAMS_UNSUPPORTED 里的取值
        在本编码器上允许 VUI 为空（我们主动跳过该键），但**绝不能**回读出一个与
        目标语义不符的值。
        """
        mismatches = []
        cases = (
            [('colorspace', v) for v in sorted(vep._COLORSPACE_VALUES)]
            + [('color_primaries', v) for v in sorted(vep._PRIMARIES_VALUES)]
            + [('color_trc', v) for v in sorted(vep._TRC_VALUES)]
        )
        for field, value in cases:
            code, tail, info = self._run_vui({field: value}, f'{field}_{value}')
            if code != 0:
                mismatches.append(f'{field}={value}: rc={code} {tail[:100]}')
                continue
            key = _VUI_KEYS[{'colorspace': 'colormatrix',
                             'color_primaries': 'colorprim',
                             'color_trc': 'transfer'}[field]]
            got = info.get(key, '')
            expected = vep._SOFTWARE_VUI_ALIASES.get(field, {}).get(value, value)
            if value in vep._SOFTWARE_VUI_UNSUPPORTED.get(field, ()):
                # 我们主动跳过该键 -> 写入的本就是空；这里用「空或语义等价」判定，
                # 而不是硬要求为空：libx265 未来若支持该枚举名，跳过只是不最优，
                # 并不产生错误语义，测试不该因此变红。真正要守的是「不得写错值」。
                if got not in ('', expected):
                    mismatches.append(
                        f'{field}={value}: 无等价名却回读出 {got!r}（期望空或 {expected!r}）'
                    )
                continue
            if got != expected:
                mismatches.append(
                    f'{field}={value}: 回读={got!r} 期望={expected!r}'
                )
        self.assertEqual(mismatches, [], f'x265-params 未真正写入 VUI：{mismatches}')

    def test_unsupported_values_never_produce_a_wrong_value(self):
        """反证：直接把这些取值写进 -x265-params 时，libx265 不会给出错误语义。

        实测当前 libx265 是静默丢弃（返回码 0，VUI 留空），这正是我们跳过该键的
        依据；断言写成「返回码 0 且回读为空或语义等价」，从而不受 libx265 版本
        差异影响 —— 版本变化时该测试仍能守住「不得写错值」这条底线。
        """
        problems = []
        for field, entry_key, values in (
            ('color_primaries', 'colorprim', vep._SOFTWARE_VUI_UNSUPPORTED['color_primaries']),
            ('color_trc', 'transfer', vep._SOFTWARE_VUI_UNSUPPORTED['color_trc']),
        ):
            for value in sorted(values):
                code, _, info = self._run(
                    [], f'raw_drop_{entry_key}_{value}',
                    extra_params=f'{entry_key}={value}',
                )
                got = info.get(_VUI_KEYS[entry_key], '')
                if code != 0:
                    # 直接报错也可以接受：说明该编码器拒绝了这个取值，
                    # 同样证明「不能透传」，且不会写错值。
                    continue
                if got not in ('', value):
                    problems.append(
                        f'{entry_key}={value}: 回读出语义不符的 {got!r}'
                    )
        self.assertEqual(problems, [], f'预期不会产生错误语义：{problems}')

    def test_colormatrix_is_normalized_to_gbr_for_x265(self):
        """x265 会接受 ffmpeg 规范名 rgb，但我们仍然统一改写成 gbr。

        不依赖 `rgb` 这种未文档化的宽松解析（各版本 libx265 未必一致），
        改走所有版本都支持的正式枚举名。因此参数表里 rgb 必须被改名。
        """
        params = vep.build_color_vui_params(
            'cpu', {'colorspace': 'rgb'}, None, 'x265'
        )
        self.assertEqual(params, ['-x265-params', 'colormatrix=gbr'])
        code, tail, info = self._run(
            list(params), 'normalized_rgb'
        )
        self.assertEqual(code, 0, tail)
        self.assertEqual(info.get('color_space'), 'gbr')

    def test_combined_vui_lands_together(self):
        code, tail, info = self._run_vui(
            {'colorspace': 'bt2020nc', 'color_primaries': 'bt2020',
             'color_trc': 'smpte2084'},
            'combined_hdr',
        )
        self.assertEqual(code, 0, tail)
        self.assertEqual(info.get('color_space'), 'bt2020nc')
        self.assertEqual(info.get('color_primaries'), 'bt2020')
        self.assertEqual(info.get('color_transfer'), 'smpte2084')

    def test_entries_skipped_for_unsupported_values_keep_the_rest(self):
        code, tail, info = self._run_vui(
            {'color_primaries': 'jedec-p22', 'colorspace': 'bt709',
             'color_trc': 'bt709'},
            'partial_jedec',
        )
        self.assertEqual(code, 0, tail)
        self.assertFalse(info.get('color_primaries', ''))
        self.assertEqual(info.get('color_space'), 'bt709')
        self.assertEqual(info.get('color_transfer'), 'bt709')

    # --- -x265-params 的合并语义 -------------------------------------------

    def test_two_x265_params_options_override_instead_of_merging(self):
        """这条是「必须合并成一条」的**直接证据**。

        给两次 -x265-params 时，后一次完全覆盖前一次：第一次写的 colorprim 消失。
        所以 build_encoder_params 必须把质量增强与 VUI 拼进同一条字符串，
        否则 x265 路径会静默丢掉一半参数（返回码仍是 0）。
        """
        output = os.path.join(self._tmpdir, 'two_opts.mp4')
        if os.path.exists(output):
            os.remove(output)
        proc = subprocess.run(
            [FFMPEG, '-hide_banner', '-nostdin', '-loglevel', 'error']
            + _PROBE_INPUT + _COMMON_OUT
            + ['-x265-params', 'colorprim=bt709',
               '-x265-params', 'transfer=bt709',
               '-y', output],
            capture_output=True, timeout=180,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr.decode('utf-8', 'replace'))
        info = _probe_stream(output)
        self.assertFalse(
            info.get('color_primaries', ''),
            '两次 -x265-params 竟然合并了；合并逻辑的必要性需要复核',
        )
        self.assertEqual(
            info.get('color_transfer'), 'bt709',
            '后一次 -x265-params 未生效，覆盖语义与预期不符',
        )

    def test_merged_boost_and_vui_are_both_effective(self):
        """build_encoder_params 的完整输出必须同时让增强项与 VUI 生效。"""
        params = vep.build_encoder_params('cpu', {
            'height': 1080, 'cpu_codec': 'x265', 'hw_quality_boost': True,
            'color_map': {'colorspace': 'bt709', 'color_primaries': 'bt709',
                          'color_trc': 'bt709'},
        })
        self.assertEqual(params[0:2], ['-c:v', 'libx265'])
        code, tail, info = self._run(params, 'merged_full', encode_args=[
            '-f', 'lavfi', '-i', 'color=c=black:s=128x128:d=0.1', '-frames:v', '3',
        ])
        self.assertEqual(code, 0, tail)
        self.assertEqual(info.get('codec_name'), 'hevc')
        self.assertEqual(info.get('color_space'), 'bt709')
        self.assertEqual(info.get('color_primaries'), 'bt709')
        self.assertEqual(info.get('color_transfer'), 'bt709')
        self.assertEqual(info.get('codec_tag_string'), 'hvc1')

    def test_every_x265_quality_boost_key_is_accepted(self):
        """增强项逐个送进 libx265，确认没有拼错键名或值。"""
        failures = []
        for key, value in vep._X265_QUALITY_PAIRS:
            code, tail, _ = self._run(
                [], f'boost_{key}', extra_params=f'{key}={value}'
            )
            if code != 0:
                failures.append(f'{key}={value}: rc={code} {tail[:120]}')
        self.assertEqual(failures, [], f'增强参数被 libx265 拒绝：{failures}')

    def test_psy_rd_colon_form_is_rejected_by_libx265(self):
        """反证：x264 的 `psy-rd=1.0:0.0` 写法在 -x265-params 里会失败。

        冒号是 -x265-params 的键值分隔符，照搬 x264 的写法会把参数串切成
        「psy-rd=1.0」与「0.0」两段。这条证明拆成 psy-rd + psy-rdoq 不是多余。
        """
        code, _, _ = self._run(
            [], 'psy_rd_colon', extra_params='psy-rd=1.0:0.0'
        )
        self.assertNotEqual(
            code, 0,
            'libx265 竟然接受了 psy-rd=1.0:0.0；拆项处理可以简化了',
        )


if __name__ == '__main__':
    unittest.main()
