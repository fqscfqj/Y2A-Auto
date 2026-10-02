"""图标与静态资源守卫测试。

背景(Issue #150):设置页「账号与登录」的导航项与汇总卡片图标整块空白。
根因是模板引用了**随包内置的图标集里并不存在**的类名 bi-person-vcard——
图标字体对该类名没有任何 ::before 映射,<i> 标签于是渲染为零宽空白:
既不报错、不占位,服务端也不会有任何日志,只能靠肉眼发现。

内置图标集是 vendored 的 Bootstrap Icons v1.8.0(static/lib/icons,1683 个图标;
woff 与上游 npm 包逐字节一致),而 bi-fire(v1.9.0)、bi-person-vcard(v1.10.0)、
bi-floppy(v1.11.0)都是更高版本才加入的名字。也就是说:只要有人在模板里写下一个
「新版本才有的图标名」,对应图标就会静默消失,而 CI 此前对这类问题完全没有约束
(全仓库扫描发现 bi-person-vcard ×2、bi-floppy、bi-fire 共 4 处属于这种情况)。

判定依据是内置 CSS 的 .bi-xxx::before 映射 —— 浏览器就是靠它把类名转成码位的,
所以「CSS 里没有」等价于「一定渲染成空白」。反之 CSS 里有也基本等价于能显示:
内置字体是未经裁剪的上游文件,唯一的不一致是上游 v1.8.0 自带的 15 个废弃别名
(cloud-haze-1 / envelope-check-1 / envelope-dash-1 / envelope-exclamation-1 /
envelope-slash-1 / envelope-x-1 / mortorboard(-fill) / send-exclamation-1 /
terminal-dash-1 / displayport-1 / ssd(-fill) / filetype-ppt-1 / filetype-xls-1):
CSS 保留了这些类名,但字体里已经没有对应字形(v1.9.0 起才重新补齐)。
这些名字目前没有任何模板在用,也请不要在新代码里使用。

本文件把该约束固化成断言:模板与前端脚本里出现的每个 bi-* 类名,
都必须在内置 CSS 里有定义。失败信息会顺带给出最接近的候选名,
便于把类名换成内置集合里已有的图标。

顺带守一条同类故障:静态资源路径写错时同样是「页面能打开、东西不见了」,
所以 url_for('static', filename=...) 里的字面量路径也必须真实存在。
"""
import difflib
import pathlib
import re
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
ICON_CSS = REPO_ROOT / 'static' / 'lib' / 'icons' / 'bootstrap-icons.css'

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
ICON_DEF_RE = re.compile(r'\.(bi-[a-z0-9-]+)::before')
STATIC_ASSET_RE = re.compile(r"url_for\(\s*'static'\s*,\s*filename\s*=\s*'([^']+)'")
FONT_SRC_RE = re.compile(r'url\(\s*["\']?([^"\')]+\.(?:woff2?|ttf|eot))["\']?\s*\)')

# Issue #150 的直接回归样本:vendored 图标集没有这些名字,必须用内置的等价图标。
# 换成别的图标也可以,但不要再改回这三个名字。
BROKEN_ICON_NAMES = ('bi-person-vcard', 'bi-floppy', 'bi-fire')


def _icon_definition_names():
    """内置 CSS 中定义了 ::before 映射的图标名集合。"""
    return set(ICON_DEF_RE.findall(ICON_CSS.read_text(encoding='utf-8')))


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


class IconAssetsGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.defined = _icon_definition_names()

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

    def test_图标字体文件齐备(self):
        # CSS 引用了不存在的字体文件时,所有图标会一起消失(比单个类名写错更严重)。
        css_text = ICON_CSS.read_text(encoding='utf-8')
        sources = FONT_SRC_RE.findall(css_text)
        self.assertTrue(sources, '内置图标 CSS 里没有解析到任何字体文件引用')
        missing = [
            src for src in sources
            if not (ICON_CSS.parent / src).is_file()
        ]
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

    def test_曾导致图标空白的类名不再出现在模板里(self):
        # Issue #150 的具名回归:这些名字在内置图标集(v1.8.x)里不存在,
        # 一旦有人「顺手改回更贴切的图标名」就会再次空白。
        offenders = [
            f'{relative}:{line} {name}'
            for name, relative, line in _icon_usages()
            if name in BROKEN_ICON_NAMES
        ]
        self.assertEqual(
            offenders, [],
            '这些图标名不在内置图标集里,会让图标静默变成空白(见 Issue #150): '
            + '; '.join(offenders))

    def test_替换后的账号图标确实存在(self):
        # 「账号与登录」是 Issue #150 的现场,单独固化一次,避免整页断言被误改时失去覆盖。
        self.assertIn('bi-person-badge', self.defined)
        settings_source = (REPO_ROOT / 'templates' / 'settings.html').read_text(encoding='utf-8')
        self.assertIn('bi bi-person-badge', settings_source)
        self.assertNotIn('bi-person-vcard', settings_source)


if __name__ == '__main__':
    unittest.main()
