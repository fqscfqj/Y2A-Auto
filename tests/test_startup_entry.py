import importlib.util
import types
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock, call


ROOT = Path(__file__).resolve().parents[1]
SETUP_APP = ROOT / "build-tools" / "setup_app.py"


def load_setup_app():
    spec = importlib.util.spec_from_file_location("setup_app_under_test", SETUP_APP)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class StartupEntryTests(TestCase):
    def test_frozen_entry_delegates_to_shared_app_lifecycle(self):
        setup_app = load_setup_app()
        runtime = object()
        fake_app = types.SimpleNamespace(
            initialize_application=Mock(return_value=runtime),
            shutdown_application=Mock(),
        )

        self.assertIs(setup_app.initialize_application(fake_app), runtime)
        setup_app.cleanup_application(runtime, fake_app)

        fake_app.initialize_application.assert_called_once_with()
        fake_app.shutdown_application.assert_called_once_with(runtime)

    def test_source_entry_uses_shared_lifecycle(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn("runtime = initialize_application()", source)
        self.assertIn("shutdown_application(runtime)", source)


if __name__ == "__main__":
    import unittest

    unittest.main()
