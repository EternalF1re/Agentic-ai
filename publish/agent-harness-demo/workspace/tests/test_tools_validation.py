import unittest
import importlib.util
from pathlib import Path

SPEC_PATH = Path(__file__).parents[1] / '03-tools-validation-demo.py'
spec = importlib.util.spec_from_file_location('tools_demo', str(SPEC_PATH))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class TestToolsValidation(unittest.TestCase):
    def test_sanitize_location_ok(self):
        ok, cleaned = mod.sanitize_location('Chengdu')
        self.assertTrue(ok)
        self.assertEqual(cleaned, 'Chengdu')

    def test_sanitize_location_bad(self):
        ok, msg = mod.sanitize_location('Chengdu; rm -rf /')
        self.assertFalse(ok)
        self.assertIn('disallowed', msg)

    def test_days_invalid_string(self):
        res = mod.get_weather_forecast('Chengdu', 'not-a-number')
        self.assertEqual(res.get('status'), 'error')

    def test_days_out_of_range(self):
        res = mod.get_weather_forecast('Chengdu', 100)
        self.assertEqual(res.get('status'), 'error')


if __name__ == '__main__':
    unittest.main()
