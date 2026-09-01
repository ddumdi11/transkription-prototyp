import os
import unittest
from unittest.mock import Mock, patch

from gui import setting_or_env


class GuiSettingsTest(unittest.TestCase):
    def test_saved_setting_skips_environment_and_glossary_fallback(self):
        fallback = Mock(return_value="glossary")
        with patch.dict(os.environ, {"AUDIOREC_PROMPT": "environment"}):
            value = setting_or_env(
                {"prompt": "saved"}, "prompt", "AUDIOREC_PROMPT", fallback
            )
        self.assertEqual(value, "saved")
        fallback.assert_not_called()

    def test_environment_skips_glossary_fallback(self):
        fallback = Mock(return_value="glossary")
        with patch.dict(os.environ, {"AUDIOREC_PROMPT": ""}):
            value = setting_or_env({}, "prompt", "AUDIOREC_PROMPT", fallback)
        self.assertEqual(value, "")
        fallback.assert_not_called()

    def test_glossary_is_last_fallback(self):
        fallback = Mock(return_value="glossary")
        with patch.dict(os.environ, {}, clear=True):
            value = setting_or_env({}, "prompt", "AUDIOREC_PROMPT", fallback)
        self.assertEqual(value, "glossary")
        fallback.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
