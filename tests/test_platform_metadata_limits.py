import ast
import pathlib
import re
import unittest


def _load_acfun_helpers():
    module_path = pathlib.Path(__file__).resolve().parents[1] / "modules" / "acfun_uploader.py"
    source = module_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(module_path))

    selected = []
    function_names = {"compact_text", "build_upload_description"}
    variable_names = {"ACFUN_TITLE_LIMIT", "ACFUN_DESCRIPTION_LIMIT"}

    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in function_names:
            selected.append(node)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in variable_names:
                    selected.append(node)

    isolated = ast.Module(body=selected, type_ignores=[])
    namespace = {"re": re}
    exec(compile(isolated, str(module_path), "exec"), namespace)
    return namespace


def _load_bilibili_helpers():
    module_path = pathlib.Path(__file__).resolve().parents[1] / "modules" / "bilibili_uploader.py"
    source = module_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(module_path))

    selected = []
    function_names = {
        "_normalize_multiline_text",
        "_truncate_multiline_text",
        "_remove_redundant_original_url",
        "format_bilibili_description",
    }
    variable_names = {"BILIBILI_TITLE_LIMIT", "BILIBILI_DESCRIPTION_LIMIT"}

    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in function_names:
            selected.append(node)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in variable_names:
                    selected.append(node)

    isolated = ast.Module(body=selected, type_ignores=[])
    namespace = {"re": re}
    exec(compile(isolated, str(module_path), "exec"), namespace)
    return namespace


def _load_ai_output_limits():
    module_path = pathlib.Path(__file__).resolve().parents[1] / "modules" / "ai_enhancer.py"
    source = module_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(module_path))

    selected = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_apply_output_limits"
    ]
    isolated = ast.Module(body=selected, type_ignores=[])
    namespace = {}
    exec(compile(isolated, str(module_path), "exec"), namespace)
    return namespace["_apply_output_limits"]


def _load_task_manager_limit_helper():
    module_path = pathlib.Path(__file__).resolve().parents[1] / "modules" / "task_manager.py"
    source = module_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(module_path))

    selected = []
    function_names = {"normalize_upload_target", "_get_effective_metadata_limits"}
    variable_names = {"UPLOAD_TARGET_ACFUN", "UPLOAD_TARGET_BILIBILI", "UPLOAD_TARGET_BOTH", "VALID_UPLOAD_TARGETS"}

    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in function_names:
            selected.append(node)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in variable_names:
                    selected.append(node)

    isolated = ast.Module(body=selected, type_ignores=[])
    namespace = {}
    exec(compile(isolated, str(module_path), "exec"), namespace)
    return namespace["_get_effective_metadata_limits"]


class PlatformMetadataLimitTests(unittest.TestCase):
    def test_acfun_limits_and_description_budget(self):
        ns = _load_acfun_helpers()

        self.assertEqual(ns["ACFUN_TITLE_LIMIT"], 50)
        self.assertEqual(ns["ACFUN_DESCRIPTION_LIMIT"], 1000)

        result = ns["build_upload_description"]("a" * 1200)
        self.assertEqual(len(result), 1000)
        self.assertTrue(result.endswith("..."))

    def test_bilibili_limits_and_description_budget(self):
        ns = _load_bilibili_helpers()

        self.assertEqual(ns["BILIBILI_TITLE_LIMIT"], 80)
        self.assertEqual(ns["BILIBILI_DESCRIPTION_LIMIT"], 2000)

        result = ns["format_bilibili_description"]("b" * 2300)
        self.assertEqual(len(result), 2000)
        self.assertTrue(result.endswith("..."))

        shared_result = ns["format_bilibili_description"]("c" * 1300, max_len=1000)
        self.assertEqual(len(shared_result), 1000)
        self.assertTrue(shared_result.endswith("..."))

    def test_ai_output_limits_accept_bilibili_sized_metadata(self):
        apply_output_limits = _load_ai_output_limits()

        title = apply_output_limits("t" * 90, "title", title_limit=80, description_limit=2000)
        description = apply_output_limits("d" * 2300, "description", title_limit=80, description_limit=2000)

        self.assertEqual(len(title), 80)
        self.assertEqual(len(description), 2000)
        self.assertTrue(description.endswith("..."))

    def test_effective_limits_follow_upload_target(self):
        get_effective_metadata_limits = _load_task_manager_limit_helper()

        self.assertEqual(
            get_effective_metadata_limits("both"),
            {"title_limit": 50, "description_limit": 1000},
        )
        self.assertEqual(
            get_effective_metadata_limits("acfun"),
            {"title_limit": 50, "description_limit": 1000},
        )
        self.assertEqual(
            get_effective_metadata_limits("bilibili"),
            {"title_limit": 80, "description_limit": 2000},
        )


if __name__ == "__main__":
    unittest.main()
