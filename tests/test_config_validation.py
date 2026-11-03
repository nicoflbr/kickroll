import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kickroll.config import ConfigError, load_config  # noqa: E402


class ConfigValidationTest(unittest.TestCase):
    def _load(self, text):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "kickroll.toml"
        path.write_text(text, encoding="utf-8")
        return load_config(path)

    def test_string_for_int_rejected(self):
        with self.assertRaises(ConfigError):
            self._load('[worker]\nmax_parallel = "2"\n')

    def test_bool_for_int_rejected(self):
        with self.assertRaises(ConfigError):
            self._load("[budget]\nmax_calls = true\n")

    def test_zero_parallel_rejected(self):
        with self.assertRaises(ConfigError):
            self._load("[worker]\nmax_parallel = 0\n")

    def test_negative_budget_rejected(self):
        with self.assertRaises(ConfigError):
            self._load("[budget]\nmax_opus = -1\n")

    def test_tier_timeout_type(self):
        with self.assertRaises(ConfigError):
            self._load('[timeouts]\nsonnet = "x"\n')

    def test_weight_non_positive(self):
        with self.assertRaises(ConfigError):
            self._load("[weights]\nopus = 0\n")

    def test_test_command_must_be_list(self):
        with self.assertRaises(ConfigError):
            self._load('[checks]\ntest_command = "python -m unittest"\n')

    def test_global_checks_must_be_list_of_lists(self):
        with self.assertRaises(ConfigError):
            self._load('[checks]\nglobal_checks = ["python"]\n')

    def test_error_names_key(self):
        with self.assertRaises(ConfigError) as ctx:
            self._load('[worker]\nmax_parallel = "2"\n')
        self.assertIn("max_parallel", str(ctx.exception))

    def test_int_for_float_ok(self):
        cfg = self._load("[budget]\nmax_minutes = 30\n")
        self.assertEqual(cfg.max_minutes, 30)
        self.assertIsInstance(cfg.max_minutes, float)

    def test_zero_reviewer_calls_allowed(self):
        cfg = self._load("[budget]\nmax_reviewer_calls = 0\n")
        self.assertEqual(cfg.max_reviewer_calls, 0)

    def test_shipped_configs_load(self):
        for name in ("kickroll.example.toml", "kickroll.toml"):
            path = ROOT / name
            if path.is_file():
                load_config(path)


if __name__ == "__main__":
    unittest.main()
