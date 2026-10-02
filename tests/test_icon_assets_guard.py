"""图标与静态资源守卫测试。

背景(Issue #150):设置页「账号与登录」的导航项与汇总卡片图标整块空白。
根因是模板引用了**随包内置的图标集里并不存在**的类名 bi-person-vcard——
图标字体对该类名没有任何 ::before 映射,<i> 标签于是渲染为零宽空白:
既不报错、不占位,服务端也不会有任何日志,只能靠肉眼发现。

当时内置的是 Bootstrap Icons v1.8.0(vendored 于 static/lib/icons)。全仓库
扫描发现 bi-person-vcard、bi-floppy、bi-fire 共 4 处在用它没有的名字,这三个
名字分别要到上游 v1.10.0 / v1.11.0 / v1.9.0 才出现。修复时先把它们换成
v1.8.0 里已有的等价图标,随后内置图标集整体升级到 v1.13.1(CSS 顶部带版本
注释)。升级后原来的三个名字本身已经可用,但**「模板引用了内置集里没有的图标名」
这类故障必须由 CI 拦住**——本文件就是那条约束。

判定依据是内置 CSS 的 .bi-xxx::before 映射:浏览器靠它把类名转成码位,所以
「CSS 里没有」等价于「一定渲染成空白」。为了让这条等价关系在两个方向上都成立,
本文件另外校验 CSS 定义的每个码位在内置字体里都有字形——v1.8.0 就存在 15 个
只写在 CSS 里、字体里没有字形的废弃别名(ssd / mortorboard / filetype-ppt-1 …),
那种「半一致」状态会让图标空白更难排查,所以换图标集时必须一并检查。

顺带守一条同类故障:静态资源路径写错时同样是「页面能打开、东西不见了」,
所以 url_for('static', filename=...) 里的字面量路径也必须真实存在。
"""
import difflib
import pathlib
import re
import struct
import unittest
import zlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
ICON_CSS = REPO_ROOT / 'static' / 'lib' / 'icons' / 'bootstrap-icons.css'
FONT_DIR = ICON_CSS.parent / 'fonts'

# 图标类名的来源:后端模板 + (未经过打包的)前端脚本。
# 这些文件里的图标名全部是字面量,因此可以静态扫描——若将来出现
# 动态拼接的类名(如 `'bi-' + name`),扫描会漏掉它们,届时需要另行处理。
SOURCE_DIRS = (
    (REPO_ROOT / 'templates', '*.html'),
    (REPO_ROOT / 'static' / 'js', '*.js'),
)

# 同时匹配 <i class="bi bi-x"> 与 class="... bi bi-x ..." 两种写法;
# 不匹配 `'bi-' + name` 这类拼接:引号/加号不在字符集内。
ICON_USE_RE = re.compile(r'\bbi\s+(bi-[a-z0-9-]+)')
ICON_DEF_RE = re.compile(r'\.(bi-[a-z0-9-]+)::before\s*\{\s*content:\s*"\\([0-9a-fA-F]+)"')
STATIC_ASSET_RE = re.compile(r"url_for\(\s*'static'\s*,\s*filename\s*=\s*'([^']+)'")
# 上游 CSS 用 `fonts/bootstrap-icons.woff2?<hash>` 这种带查询串的写法做缓存失效,
# 取路径时要把它剪掉,否则会误判成文件不存在。
FONT_SRC_RE = re.compile(r'url\(\s*["\']?([^"\')?]+\.(?:woff2?|ttf|eot))(?:[?#][^"\')]*)?["\']?\s*\)')


def _icon_codepoints():
    """内置 CSS 里 类名 -> 码位 的映射。"""
    text = ICON_CSS.read_text(encoding='utf-8')
    return {name: int(code, 16) for name, code in ICON_DEF_RE.findall(text)}


def _source_files():
    for directory, pattern in SOURCE_DIRS:
        if directory.is_dir():
            yield from sorted(directory.rglob(pattern))


def _icon_usages():
    """产出 (图标名, 相对路径, 行号),覆盖全部模板与前端脚本。"""
    for path in _source_files():
        text = path.read_text(encoding='utf-8')
        relative = path.relative_to(REPO_ROOT).as_posix()
        for match in ICON_USE_RE.finditer(text):
            yield match.group(1), relative, text.count('\n', 0, match.start()) + 1


def _woff_codepoints(path):
    """读 woff 的 cmap,返回字体**真正有字形**的码位集合。

    只用标准库:woff 的各表是 zlib 压缩的。woff2 需要 brotli(不在
    requirements.txt 里),这里以 woff 为代表——同一次上游发布的 woff 与
    woff2 覆盖一致,v1.13.1 已用 Node 的 brotli 逐一比对确认。
    """
    data = path.read_bytes()
    num_tables = struct.unpack('>H', data[12:14])[0]
    tables = {}
    offset = 44
    for _ in range(num_tables):
        tag, start, compressed, original, _checksum = struct.unpack(
            '>4sIIII', data[offset:offset + 20])
        offset += 20
        tables[tag.decode('latin-1')] = (start, compressed, original)

    start, compressed, original = tables['cmap']
    raw = data[start:start + compressed]
    if compressed != original:
        raw = zlib.decompress(raw)

    codepoints = set()
    for i in range(struct.unpack('>H', raw[2:4])[0]):
        subtable = struct.unpack('>I', raw[8 + i * 8:12 + i * 8])[0]
        fmt = struct.unpack('>H', raw[subtable:subtable + 2])[0]
        if fmt == 4:
            seg_x2 = struct.unpack('>H', raw[subtable + 6:subtable + 8])[0]
            segments = seg_x2 // 2
            ends = struct.unpack(f'>{segments}H', raw[subtable + 14:subtable + 14 + seg_x2])
            starts = struct.unpack(
                f'>{segments}H', raw[subtable + 16 + seg_x2:subtable + 16 + seg_x2 * 2])
            for begin, end in zip(starts, ends):
                if begin != 0xFFFF:
                    codepoints.update(range(begin, end + 1))
        elif fmt == 12:
            groups = struct.unpack('>I', raw[subtable + 12:subtable + 16])[0]
            for group in range(groups):
                base = subtable + 16 + group * 12
                begin, end = struct.unpack('>II', raw[base:base + 8])
                codepoints.update(range(begin, end + 1))
    return codepoints


class IconAssetsGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.codepoints = _icon_codepoints()
        cls.defined = set(cls.codepoints)
        # 字体覆盖只用于校验,解析不了就跳过对应用例,不让环境问题变成红灯
        cls.font_codepoints = None
        woff = FONT_DIR / 'bootstrap-icons.woff'
        if woff.is_file():
            try:
                cls.font_codepoints = _woff_codepoints(woff)
            except Exception:  # noqa: BLE001 - 解析失败按「拿不到」处理
                cls.font_codepoints = None

    def test_内置图标集能被解析且规模合理(self):
        # 防「守卫本身失效」:CSS 路径变动或正则失配时,defined 会退化成空集,
        # 那样后面的断言会以奇怪的方式失败(或全部图标都被报成缺失)。
        self.assertTrue(ICON_CSS.is_file(), f'内置图标 CSS 不存在: {ICON_CSS}')
        self.assertGreater(
            len(self.defined), 1000,
            f'内置图标集只解析出 {len(self.defined)} 个图标,CSS 可能已损坏或路径变动')

    def test_扫描范围非空(self):
        # 防「因为一个文件都没扫到而恒真」:模板目录改名/被移走时必须报错,
        # 而不是安静地通过。
        files = list(_source_files())
        self.assertTrue(files, '未扫描到任何模板或前端脚本,守卫等于失效')
        usages = list(_icon_usages())
        self.assertGreater(
            len(usages), 50,
            f'只扫描到 {len(usages)} 处图标引用,扫描规则可能已失效')

    def test_引用的图标都在内置图标集里(self):
        missing = {}
        for name, relative, line in _icon_usages():
            if name not in self.defined:
                missing.setdefault(name, []).append(f'{relative}:{line}')

        if missing:
            candidates = sorted(self.defined)
            details = []
            for name, places in sorted(missing.items()):
                suggestions = difflib.get_close_matches(name, candidates, n=3, cutoff=0.6)
                hint = ('内置集合中的近似图标: ' + ', '.join(suggestions)) if suggestions \
                    else '内置集合中没有近似的图标名,请从内置图标集里另选一个'
                details.append(f'  {name} ({", ".join(places[:3])}) -> {hint}')
            self.fail(
                '以下图标名不在随包内置的图标集里,渲染时不会报错但会静默变成空白'
                '(见 Issue #150)。请改用内置集合里已有的图标:\n' + '\n'.join(details))

    def test_内置CSS定义的码位在字体里都有字形(self):
        # 「CSS 里有」必须等价于「能渲染」:只有 CSS 有、字体没字形的类名同样是
        # 空白,而且更难排查(v1.8.0 的 15 个废弃别名就是这种)。
        if self.font_codepoints is None:
            self.skipTest('内置 woff 缺失或无法解析,跳过字体字形校验')
        self.assertGreater(
            len(self.font_codepoints), 1000,
            f'woff cmap 只解析出 {len(self.font_codepoints)} 个码位,解析结果不可信')

        names_by_codepoint = {}
        for name, codepoint in self.codepoints.items():
            names_by_codepoint.setdefault(codepoint, []).append(name)
        dead = sorted(cp for cp in names_by_codepoint if cp not in self.font_codepoints)
        self.assertEqual(
            dead, [],
            '这些图标只在 CSS 里有定义、内置字体里没有字形,渲染出来是空白: '
            + ', '.join(
                f'U+{cp:04X} {"/".join(sorted(names_by_codepoint[cp]))}' for cp in dead[:10]))

    def test_图标字体文件齐备(self):
        # CSS 引用了不存在的字体文件时,所有图标会一起消失(比单个类名写错更严重)。
        sources = FONT_SRC_RE.findall(ICON_CSS.read_text(encoding='utf-8'))
        self.assertTrue(sources, '内置图标 CSS 里没有解析到任何字体文件引用')
        missing = [src for src in sources if not (ICON_CSS.parent / src).is_file()]
        self.assertEqual(missing, [], f'图标 CSS 引用了不存在的字体文件: {missing}')
        for src in sources:
            font = ICON_CSS.parent / src
            self.assertGreater(
                font.stat().st_size, 1000,
                f'字体文件疑似为空或损坏: {src}')

    def test_模板静态资源引用都指向真实文件(self):
        missing = []
        for path in _source_files():
            text = path.read_text(encoding='utf-8')
            relative = path.relative_to(REPO_ROOT).as_posix()
            for match in STATIC_ASSET_RE.finditer(text):
                asset = match.group(1)
                if not (REPO_ROOT / 'static' / asset).is_file():
                    line = text.count('\n', 0, match.start()) + 1
                    missing.append(f'{relative}:{line} -> static/{asset}')
        self.assertEqual(
            missing, [],
            '模板引用了不存在的静态资源,页面能打开但资源会 404: ' + '; '.join(missing))

    def test_账号与登录处的图标可渲染(self):
        # Issue #150 的现场。这里不钉死具体图标名(升级图标集后可以自由更换),
        # 只要求「账号与登录」的导航项与汇总卡片各有一个图标,且它真的能渲染出来。
        source = (REPO_ROOT / 'templates' / 'settings.html').read_text(encoding='utf-8')
        for label, anchor in (('导航项', r'id="vtab-accounts-tab"'),
                              ('汇总卡片', r'data-settings-target="#vtab-accounts"')):
            match = re.search(
                anchor + r'[\s\S]{0,600}?<i class="bi (bi-[a-z0-9-]+)"', source)
            self.assertIsNotNone(match, f'「账号与登录」{label}没有图标了(见 Issue #150)')
            name = match.group(1)
            self.assertIn(name, self.defined, f'{name} 不在内置图标集里')
            if self.font_codepoints is not None:
                self.assertIn(
                    self.codepoints[name], self.font_codepoints,
                    f'{name} 在内置字体里没有字形,渲染出来是空白')


if __name__ == '__main__':
    unittest.main()
