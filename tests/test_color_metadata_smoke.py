# -*- coding: utf-8 -*-
"""色彩元数据白名单的真实 FFmpeg 冒烟测试。

存在的理由：`tests/test_video_encoder_params.py` 只能断言「白名单里的值是否被
保留」，无法发现「保留的值 ffmpeg 根本不接受」。本文件对三张白名单
（_COLORSPACE_VALUES / _PRIMARIES_VALUES / _TRC_VALUES）的**每个取值**真的跑
一次 ffmpeg，用 `exit code == 0` 判定。

两处关键细节，弄错会让测试假绿：

1. `-colorspace` / `-color_primaries` / `-color_trc` 是**输出**选项，必须写在
   `-i` 之后；写在前面会得到 "Option not found"，与取值合法性无关。
2. x264 私有参数（`-x264-params colorprim=/transfer=/colormatrix=`）对不认识的
   枚举名只打印 "Error parsing option ..." 并**继续返回 0**，字段在 VUI 里留空。
   所以该路径不能只看返回码，必须再用 ffprobe 回读 VUI —— 三张表里
   `rgb` / `smpte428_1` / `log` / `log_sqrt` / `iec61966_2_4` / `iec61966_2_1` /
   `bt1361` / `jedec-p22` / `ebu3213` / `gamma22` / `gamma28` 都属于「ffmpeg 通用
   选项收，x264 不收」的取值，靠 build_color_vui_params 的改名映射才能落进 VUI。

ffmpeg 不可用时整类 skip，不失败。
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

# 探针输入：极小尺寸、极短时长，冒烟只关心选项解析与 VUI 写入，不关心画质。
_PROBE_INPUT = ['-f', 'lavfi', '-i', 'color=c=black:s=128x128:d=0.1']
_COMMON_OUT = ['-c:v', 'libx264', '-preset', 'ultrafast',
               '-pix_fmt', 'yuv420p', '-fps_mode', 'cfr', '-frames:v', '3']

_VUI_KEYS = {
    'colorspace': 'color_space',
    'color_primaries': 'color_primaries',
    'color_trc': 'color_transfer',
}


def _find_binary(name):
    """按「仓库 ffmpeg/ 目录 -> PATH」的顺序定位二进制。"""
    local = os.path.join(_REPO_ROOT, 'ffmpeg', name + ('.exe' if os.name == 'nt' else ''))
    if os.path.isfile(local):
        return local
    return shutil.which(name)


FFMPEG = _find_binary('ffmpeg')
FFPROBE = _find_binary('ffprobe')


def _ffmpeg_usable():
    if not FFMPEG:
        return False
    try:
        return subprocess.run([FFMPEG, '-version'], capture_output=True,
                              timeout=15).returncode == 0
    except Exception:
        return False


def _probe_vui(path):
    """回读输出文件的色彩元数据字段。"""
    if not FFPROBE:
        return {}
    proc = subprocess.run(
        [FFPROBE, '-hide_banner', '-loglevel', 'error', '-select_streams', 'v:0',
         '-show_entries', 'stream=color_space,color_primaries,color_transfer',
         '-of', 'json', path],
        capture_output=True, timeout=30,
    )
    try:
        stream = json.loads(proc.stdout.decode('utf-8', 'replace'))['streams'][0]
    except Exception:
        return {}
    return {key: stream.get(key, '') for key in _VUI_KEYS.values()}


@unittest.skipUnless(_ffmpeg_usable(), 'ffmpeg 不可用，跳过色彩元数据冒烟测试')
class ColorMetadataSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.mkdtemp(prefix='vep_color_smoke_')

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._tmpdir, ignore_errors=True)

    def _run(self, extra, tag):
        """跑一次 ffmpeg，返回 (返回码, stderr 尾部, VUI 回读)。"""
        output = os.path.join(self._tmpdir, f'{tag}.mkv')
        if os.path.exists(output):
            os.remove(output)
        proc = subprocess.run(
            [FFMPEG, '-hide_banner', '-nostdin', '-loglevel', 'error']
            + _PROBE_INPUT + _COMMON_OUT + extra + ['-y', output],
            capture_output=True, timeout=120,
        )
        stderr = proc.stderr.decode('utf-8', 'replace').strip()
        tail = stderr.splitlines()[-1] if stderr else ''
        vui = _probe_vui(output) if proc.returncode == 0 else {}
        return proc.returncode, tail, vui

    def _assert_all_accepted(self, option, values, key):
        """通用输出选项：每个取值都必须能让 ffmpeg 成功产出文件。"""
        self.assertTrue(values, f'{option} 的取值表为空，白名单不能是空集')
        failures = []
        for value in sorted(values):
            code, tail, _ = self._run([option, value], f'{key}_{value}')
            if code != 0:
                failures.append(f'{option} {value}: rc={code} {tail[:120]}')
        self.assertEqual(failures, [], f'{option} 存在 ffmpeg 拒绝的取值：{failures}')

    def test_colorspace_whitelist_is_accepted_by_ffmpeg(self):
        self._assert_all_accepted(
            '-colorspace', vep._COLORSPACE_VALUES, 'colorspace'
        )

    def test_color_primaries_whitelist_is_accepted_by_ffmpeg(self):
        self._assert_all_accepted(
            '-color_primaries', vep._PRIMARIES_VALUES, 'primaries'
        )

    def test_color_trc_whitelist_is_accepted_by_ffmpeg(self):
        self._assert_all_accepted('-color_trc', vep._TRC_VALUES, 'trc')

    def test_values_outside_the_tables_are_actually_rejected(self):
        # 反向证据：白名单不是「什么都收」的摆设。这些取值在 ffmpeg 侧不可用，
        # 正是原缺陷把三者合表时漏出去的取值。
        for option, value in (
            ('-colorspace', 'bt2020'),
            ('-colorspace', 'film'),
            ('-colorspace', 'bt2020c'),
            ('-color_primaries', 'bt2020nc'),
            ('-color_primaries', 'ycgco'),
            ('-color_trc', 'bt2020_10'),
            ('-color_trc', 'bt2020_12'),
        ):
            code, _, _ = self._run([option, value], f'reject_{value}')
            self.assertNotEqual(
                code, 0, f'{option} {value} 竟然被 ffmpeg 接受了，白名单需要复核'
            )

    def test_colorspace_round_trips_through_the_generic_option(self):
        """`-colorspace` 会被 libx264 转发进 VUI，回读必须与写入值语义一致。

        `rgb` 是特例：ffmpeg 规范名是 rgb，VUI 里编码为 gbr（同一矩阵）。
        """
        mismatches = []
        for value in sorted(vep._COLORSPACE_VALUES):
            code, _, vui = self._run(['-colorspace', value], f'vui_space_{value}')
            if code != 0:
                mismatches.append(f'-colorspace {value}: rc={code}')
                continue
            expected = 'gbr' if value == 'rgb' else value
            got = vui.get('color_space', '')
            if got != expected:
                mismatches.append(f'-colorspace {value}: 回读={got!r} 期望={expected!r}')
        self.assertEqual(mismatches, [], f'-colorspace 未真正写入 VUI：{mismatches}')

    def test_primaries_and_trc_are_silently_dropped_by_libx264(self):
        """通用选项写 primaries/trc 时 libx264 静默丢弃 —— 这是本模块的必要性证据。

        返回码是 0，VUI 里却什么都没有：只看命令成功与否根本发现不了。
        因此软件编码路径必须靠 build_color_vui_params 的 `-x264-params` 补写
        （对应 X264ColorVuiSmokeTests 的断言）。
        """
        dropped = []
        for option, key, values in (
            ('-color_primaries', 'color_primaries', vep._PRIMARIES_VALUES),
            ('-color_trc', 'color_transfer', vep._TRC_VALUES),
        ):
            for value in sorted(values):
                code, _, vui = self._run([option, value], f'drop_{key}_{value}')
                if code != 0:
                    dropped.append(f'{option} {value}: rc={code}（应被接受）')
                    continue
                got = vui.get(key, '')
                if got:
                    dropped.append(
                        f'{option} {value}: 意外回读到 {got!r}，'
                        f'build_color_vui_params 的必要性需要复核'
                    )
        self.assertEqual(dropped, [], f'通用选项未被丢弃：{dropped}')


@unittest.skipUnless(_ffmpeg_usable(), 'ffmpeg 不可用，跳过色彩元数据冒烟测试')
class X264ColorVuiSmokeTests(unittest.TestCase):
    """build_color_vui_params 的产物必须在 x264 里真正生效（回读 VUI 判定）。"""

    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.mkdtemp(prefix='vep_x264_vui_')

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._tmpdir, ignore_errors=True)

    def _run_vui(self, color_map, tag):
        extra = vep.build_color_vui_params('cpu', color_map)
        output = os.path.join(self._tmpdir, f'{tag}.mkv')
        if os.path.exists(output):
            os.remove(output)
        proc = subprocess.run(
            [FFMPEG, '-hide_banner', '-nostdin', '-loglevel', 'error']
            + _PROBE_INPUT + _COMMON_OUT + extra + ['-y', output],
            capture_output=True, timeout=120,
        )
        stderr = proc.stderr.decode('utf-8', 'replace').strip()
        tail = stderr.splitlines()[-1] if stderr else ''
        vui = _probe_vui(output) if proc.returncode == 0 else {}
        return proc.returncode, tail, vui

    def test_every_mapped_value_lands_in_the_vui(self):
        """三张表的每个取值都经 build_color_vui_params 走一遍并回读 VUI。

        期望值 = 该取值在 x264 里的枚举名（_X264_PARAMS_ALIASES 的右值），
        没有等价名的取值（_X264_PARAMS_UNSUPPORTED）允许 VUI 为空，
        但**绝不能**回读出一个与目标语义不符的值。
        """
        mismatches = []
        for field, key, values in (
            ('colorspace', 'color_space', vep._COLORSPACE_VALUES),
            ('color_primaries', 'color_primaries', vep._PRIMARIES_VALUES),
            ('color_trc', 'color_transfer', vep._TRC_VALUES),
        ):
            for value in sorted(values):
                code, tail, vui = self._run_vui(
                    {field: value}, f'{field}_{value}'
                )
                if code != 0:
                    mismatches.append(f'{field}={value}: rc={code} {tail[:100]}')
                    continue
                got = vui.get(key, '')
                if value in vep._X264_PARAMS_UNSUPPORTED.get(field, ()):
                    # x264 无对应枚举：跳过该键，VUI 必须留空而不是写错值。
                    if got:
                        mismatches.append(
                            f'{field}={value}: x264 无对应名，却回读出 {got!r}'
                        )
                    continue
                expected = vep._X264_PARAMS_ALIASES.get(field, {}).get(value, value)
                if got != expected:
                    mismatches.append(
                        f'{field}={value}: 回读={got!r} 期望={expected!r}'
                    )
        self.assertEqual(mismatches, [], f'x264-params 未真正写入 VUI：{mismatches}')

    def test_unmapped_ffmpeg_name_would_be_silently_dropped(self):
        """反证：直接写 ffmpeg 规范名时 x264 静默丢弃，说明映射不是多余的。

        `colormatrix=rgb` 返回码是 0（x264 只警告），但 VUI 里没有色域 ——
        这正是只看返回码会漏掉的失败模式。
        """
        output = os.path.join(self._tmpdir, 'raw_rgb.mkv')
        if os.path.exists(output):
            os.remove(output)
        proc = subprocess.run(
            [FFMPEG, '-hide_banner', '-nostdin', '-loglevel', 'error']
            + _PROBE_INPUT + _COMMON_OUT
            + ['-x264-params', 'colormatrix=rgb', '-y', output],
            capture_output=True, timeout=120,
        )
        self.assertEqual(proc.returncode, 0, 'x264 对未知枚举名不应改变返回码')
        self.assertFalse(
            _probe_vui(output).get('color_space', ''),
            'x264 竟然接受了 colormatrix=rgb；映射表需要复核',
        )

    def test_mapped_output_is_accepted_and_effective_together(self):
        """三键同时输出的典型场景：全部落进 VUI。"""
        color_map = {
            'colorspace': 'bt2020nc',
            'color_primaries': 'bt2020',
            'color_trc': 'smpte2084',
        }
        code, tail, vui = self._run_vui(color_map, 'combined_hdr')
        self.assertEqual(code, 0, tail)
        self.assertEqual(vui.get('color_space'), 'bt2020nc')
        self.assertEqual(vui.get('color_primaries'), 'bt2020')
        self.assertEqual(vui.get('color_transfer'), 'smpte2084')

    def test_entries_skipped_for_unsupported_values_keep_the_rest(self):
        """一个字段无 x264 等价名时，其余字段仍必须写进 VUI。"""
        code, tail, vui = self._run_vui(
            {'color_primaries': 'jedec-p22', 'colorspace': 'bt709',
             'color_trc': 'bt709'},
            'partial_jedec',
        )
        self.assertEqual(code, 0, tail)
        self.assertFalse(vui.get('color_primaries', ''))
        self.assertEqual(vui.get('color_space'), 'bt709')
        self.assertEqual(vui.get('color_transfer'), 'bt709')


if __name__ == '__main__':
    unittest.main()
