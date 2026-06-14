import unittest

from modules.prompt_manager import (
    MODE_APPEND,
    MODE_BUILTIN,
    MODE_OVERRIDE,
    VALID_MODES,
    config_key_for_mode,
    config_key_for_text,
    get_default_config_entries,
    get_final_system_prompt,
    get_metadata_desc_retry_prompt,
    get_metadata_translate_prompt,
    get_prompt_ids,
    get_prompt_info,
    get_subtitle_strict_system_prompt,
    get_subtitle_system_prompt,
    normalize_mode,
    normalize_text,
    read_prompt_config_from_app_config,
)


class NormalizeModeTests(unittest.TestCase):
    def test_valid_modes(self):
        for mode in VALID_MODES:
            self.assertEqual(normalize_mode(mode), mode)

    def test_case_insensitive(self):
        self.assertEqual(normalize_mode("APPEND"), MODE_APPEND)
        self.assertEqual(normalize_mode(" Override "), MODE_OVERRIDE)

    def test_invalid_fallback(self):
        self.assertEqual(normalize_mode("bad"), MODE_BUILTIN)
        self.assertEqual(normalize_mode(""), MODE_BUILTIN)
        self.assertEqual(normalize_mode(None), MODE_BUILTIN)


class NormalizeTextTests(unittest.TestCase):
    def test_strip(self):
        self.assertEqual(normalize_text("  hello  "), "hello")

    def test_crlf(self):
        self.assertEqual(normalize_text("a\r\nb"), "a\nb")

    def test_max_length(self):
        result = normalize_text("a" * 5000, max_length=100)
        self.assertEqual(len(result), 100)

    def test_empty(self):
        self.assertEqual(normalize_text(""), "")
        self.assertEqual(normalize_text(None), "")


class PromptRegistryTests(unittest.TestCase):
    def test_ids(self):
        ids = get_prompt_ids()
        self.assertIn("SUBTITLE_TRANSLATE", ids)
        self.assertIn("SUBTITLE_TRANSLATE_STRICT", ids)
        self.assertIn("METADATA_TRANSLATE", ids)
        self.assertIn("METADATA_DESC_RETRY", ids)

    def test_info(self):
        info = get_prompt_info("SUBTITLE_TRANSLATE")
        self.assertIsNotNone(info)
        self.assertEqual(info["label"], "字幕翻译主提示词")
        self.assertFalse(info["is_advanced"])

    def test_strict_is_advanced(self):
        info = get_prompt_info("SUBTITLE_TRANSLATE_STRICT")
        self.assertTrue(info["is_advanced"])

    def test_unknown_returns_none(self):
        self.assertIsNone(get_prompt_info("NONEXISTENT"))


class ConfigKeyTests(unittest.TestCase):
    def test_mode_key(self):
        self.assertEqual(config_key_for_mode("SUBTITLE_TRANSLATE"), "SUBTITLE_TRANSLATE_MODE")

    def test_text_key(self):
        self.assertEqual(config_key_for_text("SUBTITLE_TRANSLATE"), "SUBTITLE_TRANSLATE_TEXT")

    def test_default_entries(self):
        entries = get_default_config_entries()
        self.assertIn("SUBTITLE_TRANSLATE_MODE", entries)
        self.assertEqual(entries["SUBTITLE_TRANSLATE_MODE"], MODE_BUILTIN)
        self.assertIn("SUBTITLE_TRANSLATE_TEXT", entries)
        self.assertEqual(entries["SUBTITLE_TRANSLATE_TEXT"], "")
        # 每个 Prompt 注册 2 个键：_MODE + _TEXT
        self.assertEqual(len(entries), len(get_prompt_ids()) * 2)


class ReadPromptConfigTests(unittest.TestCase):
    def test_empty_config(self):
        mode, text = read_prompt_config_from_app_config({}, "SUBTITLE_TRANSLATE")
        self.assertEqual(mode, MODE_BUILTIN)
        self.assertEqual(text, "")

    def test_valid_config(self):
        app_config = {
            "SUBTITLE_TRANSLATE_MODE": "append",
            "SUBTITLE_TRANSLATE_TEXT": "保留术语",
        }
        mode, text = read_prompt_config_from_app_config(app_config, "SUBTITLE_TRANSLATE")
        self.assertEqual(mode, MODE_APPEND)
        self.assertEqual(text, "保留术语")

    def test_invalid_mode_fallback(self):
        app_config = {"SUBTITLE_TRANSLATE_MODE": "bad"}
        mode, _ = read_prompt_config_from_app_config(app_config, "SUBTITLE_TRANSLATE")
        self.assertEqual(mode, MODE_BUILTIN)


class FinalSystemPromptTests(unittest.TestCase):
    def test_builtin_zh(self):
        prompt = get_final_system_prompt("SUBTITLE_TRANSLATE", target_language="zh")
        self.assertIn("简体中文", prompt)
        self.assertNotIn("{target_language_name}", prompt)

    def test_builtin_en(self):
        prompt = get_final_system_prompt("SUBTITLE_TRANSLATE", target_language="en")
        self.assertIn("English", prompt)

    def test_append(self):
        prompt = get_final_system_prompt(
            "SUBTITLE_TRANSLATE",
            mode=MODE_APPEND,
            user_text="保留术语不翻译",
            target_language="zh",
        )
        self.assertIn("简体中文", prompt)
        self.assertIn("保留术语不翻译", prompt)

    def test_override(self):
        prompt = get_final_system_prompt(
            "SUBTITLE_TRANSLATE",
            mode=MODE_OVERRIDE,
            user_text="你是一个特殊的翻译器",
            target_language="zh",
        )
        self.assertEqual(prompt, "你是一个特殊的翻译器")

    def test_override_empty_text_fallback(self):
        builtin = get_final_system_prompt("SUBTITLE_TRANSLATE", target_language="zh")
        override_empty = get_final_system_prompt(
            "SUBTITLE_TRANSLATE",
            mode=MODE_OVERRIDE,
            user_text="",
            target_language="zh",
        )
        self.assertEqual(override_empty, builtin)

    def test_variable_rendering(self):
        prompt = get_final_system_prompt("METADATA_TRANSLATE", target_language="ja")
        self.assertIn("日本語", prompt)
        self.assertNotIn("{target_language_name}", prompt)


class SubtitlePromptTests(unittest.TestCase):
    def test_subtitle_system_prompt_has_json(self):
        prompt = get_subtitle_system_prompt(target_language="zh")
        self.assertIn('"translations"', prompt)
        self.assertIn("一一对应", prompt)

    def test_subtitle_strict_prompt(self):
        prompt = get_subtitle_strict_system_prompt(target_language="zh")
        self.assertIn("严格模式", prompt)
        self.assertIn('"translations"', prompt)

    def test_append_preserves_protocol(self):
        prompt = get_subtitle_system_prompt(
            mode=MODE_APPEND,
            user_text="保留音乐术语",
            target_language="zh",
        )
        self.assertIn("一一对应", prompt)
        self.assertIn('"translations"', prompt)
        self.assertIn("保留音乐术语", prompt)


class MetadataPromptTests(unittest.TestCase):
    def test_metadata_translate_prompt(self):
        prompt = get_metadata_translate_prompt(target_language="zh")
        self.assertIn('"title"', prompt)
        self.assertIn('"description"', prompt)

    def test_metadata_retry_suffix(self):
        prompt = get_metadata_translate_prompt(target_language="zh", retry=True)
        self.assertIn("仅重写本次输入", prompt)

    def test_desc_retry_prompt(self):
        prompt = get_metadata_desc_retry_prompt(target_language="zh")
        self.assertIn('"description"', prompt)


if __name__ == "__main__":
    unittest.main()
