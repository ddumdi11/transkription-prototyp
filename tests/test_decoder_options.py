"""CLI- und GUI-Schalter für VAD und Kontextkonditionierung (faster-whisper)."""
import json
import sys
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import gui
import transcribe
from gui import bool_setting, local_decoder_args
from providers.local_provider import LocalWhisperProvider

DECODER_KEYS = {"vad_filter", "condition_on_previous_text"}


def run_cli(*extra: str, provider: str = "local") -> Mock:
    """transcribe.main() bis zur Provider-Erzeugung ausführen.

    get_provider wird gemockt und bricht den Lauf danach kontrolliert ab, damit
    weder ein Modell geladen noch eine API aufgerufen wird.
    """
    get_provider = Mock(side_effect=ValueError("Test-Abbruch nach Provider-Wahl"))
    argv = ["transcribe.py", "input", "--provider", provider, *extra]
    with patch.object(sys, "argv", argv), \
            patch("transcribe.get_provider", get_provider), \
            patch("transcribe.load_dotenv"), \
            patch("builtins.print"):
        with unittest.TestCase().assertRaises(SystemExit) as raised:
            transcribe.main()
    get_provider.exit_code = raised.exception.code
    return get_provider


class CliDecoderOptionsTest(unittest.TestCase):
    """CLI-Schalter --vad/--condition-on-previous-text und ihre Weitergabe."""
    def test_without_options_provider_defaults_apply(self):
        """Ohne Schalter erhält get_provider keine Decoder-Parameter."""
        get_provider = run_cli("--model", "medium")
        get_provider.assert_called_once_with("local", model="medium")
        self.assertEqual(get_provider.exit_code, 1)  # kontrollierter Test-Abbruch

    def test_each_option_reaches_local_provider(self):
        """Jede Schalterform kommt mit dem richtigen Wert beim lokalen Provider an."""
        cases = {
            "--vad": {"vad_filter": True},
            "--no-vad": {"vad_filter": False},
            "--condition-on-previous-text": {"condition_on_previous_text": True},
            "--no-condition-on-previous-text": {"condition_on_previous_text": False},
        }
        for flag, expected in cases.items():
            with self.subTest(flag=flag):
                get_provider = run_cli(flag)
                get_provider.assert_called_once_with("local", model=None, **expected)

    def test_options_can_be_combined(self):
        """Beide Schalter lassen sich unabhängig kombinieren."""
        get_provider = run_cli("--no-vad", "--no-condition-on-previous-text")
        get_provider.assert_called_once_with(
            "local", model=None, vad_filter=False, condition_on_previous_text=False)

        get_provider = run_cli("--no-vad", "--condition-on-previous-text")
        get_provider.assert_called_once_with(
            "local", model=None, vad_filter=False, condition_on_previous_text=True)

    def test_last_given_form_wins(self):
        """Bei widersprüchlichen Angaben gilt die zuletzt genannte Form."""
        get_provider = run_cli("--no-vad", "--vad")
        get_provider.assert_called_once_with("local", model=None, vad_filter=True)

    def test_openai_without_options_gets_no_decoder_parameters(self):
        """OpenAI ohne Schalter bekommt keine lokalen Decoder-Parameter."""
        get_provider = run_cli("--model", "whisper-1", provider="openai")
        get_provider.assert_called_once_with("openai", model="whisper-1")

    def test_openai_rejects_explicit_local_decoder_options(self):
        """OpenAI mit lokalem Schalter bricht per parser.error (Exit 2) ab."""
        for flag in ("--vad", "--no-vad", "--condition-on-previous-text",
                     "--no-condition-on-previous-text"):
            with self.subTest(flag=flag):
                with patch("sys.stderr"):
                    get_provider = run_cli(flag, provider="openai")
                get_provider.assert_not_called()
                self.assertEqual(get_provider.exit_code, 2)


class LocalProviderDecoderOptionsTest(unittest.TestCase):
    """LocalWhisperProvider reicht die Decoder-Optionen an faster-whisper weiter."""
    @patch("providers.local_provider.WhisperModel")
    def test_values_reach_faster_whisper(self, model_class):
        """Alle vier Kombinationen landen unverändert in WhisperModel.transcribe."""
        model_class.return_value.transcribe.return_value = ([], Mock())
        for vad in (True, False):
            for context in (True, False):
                with self.subTest(vad=vad, context=context):
                    model_class.return_value.transcribe.reset_mock()
                    with patch("builtins.print"):
                        provider = LocalWhisperProvider(
                            model_size="medium", vad_filter=vad,
                            condition_on_previous_text=context)
                    provider.transcribe(Path("a.wav"), language="de")
                    kwargs = model_class.return_value.transcribe.call_args.kwargs
                    self.assertIs(kwargs["vad_filter"], vad)
                    self.assertIs(kwargs["condition_on_previous_text"], context)


class GuiDecoderHelpersTest(unittest.TestCase):
    """Reine Hilfsfunktionen der GUI ohne Tk."""
    def test_bool_setting_defaults_for_missing_or_invalid_values(self):
        """Fehlende oder nicht-boolesche Werte fallen auf den Default zurück."""
        self.assertTrue(bool_setting({}, "local_vad_filter", True))
        self.assertTrue(bool_setting({"local_vad_filter": "nein"}, "local_vad_filter", True))
        self.assertFalse(bool_setting({"local_vad_filter": False}, "local_vad_filter", True))

    def test_local_args_are_always_explicit(self):
        """Für local werden beide Schalter immer ausdrücklich gesetzt."""
        self.assertEqual(local_decoder_args("local", True, True),
                         ["--vad", "--condition-on-previous-text"])
        self.assertEqual(local_decoder_args("local", False, False),
                         ["--no-vad", "--no-condition-on-previous-text"])
        self.assertEqual(local_decoder_args("local", False, True),
                         ["--no-vad", "--condition-on-previous-text"])

    def test_openai_gets_no_local_args(self):
        """Für OpenAI werden keine lokalen Schalter erzeugt."""
        self.assertEqual(local_decoder_args("openai", False, False), [])


def _tk_available() -> bool:
    """Prüfen, ob ein Tk-Fenster erzeugt werden kann (Display vorhanden)."""
    try:
        root = tk.Tk()
    except tk.TclError:
        return False
    root.destroy()
    return True


@unittest.skipUnless(_tk_available(), "Tk/Display nicht verfügbar")
class GuiDecoderSettingsTest(unittest.TestCase):
    """GUI mit echtem Tk: Laden, Speichern, Befehlsaufbau und Sichtbarkeit."""
    def setUp(self):
        """Einstellungsdatei in ein temporäres Verzeichnis umleiten."""
        self.temp = tempfile.TemporaryDirectory()
        self.settings_file = Path(self.temp.name) / "gui_settings.json"
        patcher = patch.object(gui, "SETTINGS_FILE", self.settings_file)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.temp.cleanup)

    def _open(self, settings: dict | None):
        """GUI mit den gegebenen gespeicherten Einstellungen öffnen."""
        if settings is not None:
            self.settings_file.write_text(json.dumps(settings), encoding="utf-8")
        root = tk.Tk()
        root.withdraw()
        self.addCleanup(root.destroy)
        return gui.TranscriptionGUI(root)

    def _command(self, app) -> list[str]:
        """Den von _transcribe gebauten Befehl abfangen, ohne ihn auszuführen."""
        app.input_folder.set(self.temp.name)
        with patch.object(app, "_run_command") as run:
            app._transcribe()
        return run.call_args.args[0]

    def test_old_settings_without_fields_get_safe_defaults(self):
        """Alte Einstellungen ohne die neuen Felder ergeben VAD an, Kontext an."""
        app = self._open({"provider": "local"})
        self.assertTrue(app.local_vad_filter.get())
        self.assertTrue(app.local_condition_on_previous_text.get())

    def test_loads_and_saves_both_values(self):
        """Beide Werte werden geladen und als bool gespeichert."""
        app = self._open({"provider": "local", "local_vad_filter": False,
                          "local_condition_on_previous_text": False})
        self.assertFalse(app.local_vad_filter.get())
        self.assertFalse(app.local_condition_on_previous_text.get())

        app.local_vad_filter.set(True)
        app._save_settings()
        saved = json.loads(self.settings_file.read_text(encoding="utf-8"))
        self.assertIs(saved["local_vad_filter"], True)
        self.assertIs(saved["local_condition_on_previous_text"], False)

    def test_local_command_contains_selected_values(self):
        """Der lokale Befehl enthält die gewählten Schalter."""
        app = self._open({"provider": "local", "local_vad_filter": False})
        cmd = self._command(app)
        self.assertIn("--no-vad", cmd)
        self.assertIn("--condition-on-previous-text", cmd)
        self.assertNotIn("--vad", cmd)

    def test_openai_command_has_no_local_decoder_options(self):
        """Der OpenAI-Befehl enthält keine lokalen Decoder-Schalter."""
        app = self._open({"provider": "openai", "local_vad_filter": False,
                          "local_condition_on_previous_text": False})
        cmd = self._command(app)
        self.assertFalse({"--vad", "--no-vad", "--condition-on-previous-text",
                          "--no-condition-on-previous-text"} & set(cmd))

    def test_local_options_only_visible_for_local_engine(self):
        """Die Checkboxen sind nur bei der lokalen Engine sichtbar."""
        app = self._open({"provider": "openai"})
        self.assertFalse(app.local_options_frame.winfo_manager())
        app.engine.set(gui.ENGINE_VALUES["local"])
        app._on_engine_change()
        self.assertEqual(app.local_options_frame.winfo_manager(), "grid")


if __name__ == "__main__":
    unittest.main()
