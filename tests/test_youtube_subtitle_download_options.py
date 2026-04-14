import ast
import pathlib
import unittest


def _load_function(name):
    module_path = pathlib.Path(__file__).resolve().parents[1] / "modules" / "youtube_handler.py"
    source = module_path.read_text(encoding="utf-8")
    module_ast = ast.parse(source, filename=str(module_path))
    selected = [
        node for node in module_ast.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    ]
    isolated_module = ast.Module(body=selected, type_ignores=[])
    namespace = {"Any": object}
    exec(compile(isolated_module, str(module_path), "exec"), namespace)
    return namespace[name]


class YouTubeSubtitleDownloadOptionsTests(unittest.TestCase):
    def test_returns_no_write_subs_when_subtitles_disabled(self):
        build_args = _load_function("_build_subtitle_download_args")

        args = build_args({}, include_subtitles=False)

        self.assertEqual(args, ["--no-write-subs"])

    def test_returns_manual_subtitle_flags_when_enabled(self):
        build_args = _load_function("_build_subtitle_download_args")

        args = build_args({}, include_subtitles=True)

        self.assertEqual(
            args,
            ["--write-subs", "--all-subs", "--convert-subs", "srt"],
        )

    def test_includes_auto_generated_subtitle_flag_when_enabled(self):
        build_args = _load_function("_build_subtitle_download_args")

        args = build_args(
            {"YOUTUBE_AUTO_GENERATED_SUBTITLES_ENABLED": True},
            include_subtitles=True,
        )

        self.assertIn("--write-auto-subs", args)
        self.assertEqual(args[:-1], ["--write-subs", "--all-subs", "--convert-subs", "srt"])


if __name__ == "__main__":
    unittest.main()
