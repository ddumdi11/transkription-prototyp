"""
GUI für den Transkriptions-Prototyp.

Starte mit:
    python gui.py
"""

import json
import os
import re
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk, scrolledtext, filedialog, messagebox
from pathlib import Path

from dotenv import load_dotenv

# .env laden für API-Key Check
load_dotenv()

# Einstellungen werden neben der App gespeichert, damit z. B. die
# Engine-/Modell-Wahl nicht bei jedem Start zurückgesetzt wird.
SETTINGS_FILE = Path(__file__).parent / "gui_settings.json"

# Engine-Auswahl: Anzeigename -> interner Provider-Wert.
ENGINE_LABELS = {
    "OpenAI (Cloud, kostenpflichtig)": "openai",
    "Lokal (faster-whisper, kostenlos)": "local",
}
ENGINE_VALUES = {v: k for k, v in ENGINE_LABELS.items()}

# Modell-Auswahl je Engine (erster Eintrag = Default).
MODELS_BY_PROVIDER = {
    "openai": ["gpt-4o-mini-transcribe", "gpt-4o-transcribe", "whisper-1"],
    "local": ["tiny", "base", "small", "medium", "large-v3"],
}
DEFAULT_MODEL_BY_PROVIDER = {"openai": "gpt-4o-mini-transcribe", "local": "small"}

# LLM-Korrektur: Backend-Anzeigename -> interner Wert (siehe transcribe.py).
CORRECTION_BACKEND_LABELS = {"Ollama": "ollama", "LM Studio": "lmstudio"}
CORRECTION_BACKEND_VALUES = {v: k for k, v in CORRECTION_BACKEND_LABELS.items()}

# Workflow-Modus (BUG-TRANSCRIBE-001): "single" = jede Aufnahme einzeln
# transkribieren (neuer Standard, Metadaten bleiben erhalten);
# "join" = Audios vorher zusammenfügen (alter Ablauf, Sonderfall).
WORKFLOW_TEXT_SINGLE = "▶ Kompletter Workflow (Einzeltranskription)"
WORKFLOW_TEXT_JOIN = "▶ Kompletter Workflow (Zusammenfügen + Transkription)"


class TranscriptionGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Transkriptions-Prototyp")
        # Fenstergröße wird nach dem Aufbau der Widgets gesetzt
        # (Inhaltshöhe messen bzw. gespeicherte Größe wiederherstellen).
        self.root.minsize(640, 560)

        # Gespeicherte Einstellungen laden (oder leeres Dict).
        settings = self._load_settings()

        # Variablen für Optionen
        self.input_folder = tk.StringVar(value=settings.get("input_folder", "input"))
        self.output_folder = tk.StringVar(value=settings.get("output_folder", "output"))
        self.prompt = tk.StringVar(value=settings.get(
            "prompt", "Fachbegriffe: Diktiergerät, Claude, Claude Code, KI"))
        self.move_after_join = tk.BooleanVar(value=settings.get("move_after_join", True))
        self.force_retranscribe = tk.BooleanVar(value=False)

        # Workflow-Modus: Einzeltranskription ist der neue Standard.
        mode = settings.get("workflow_mode", "single")
        if mode not in ("single", "join"):
            mode = "single"
        self.workflow_mode = tk.StringVar(value=mode)
        self.move_after_transcribe = tk.BooleanVar(
            value=settings.get("move_after_transcribe", True))
        self.merge_transcripts = tk.BooleanVar(
            value=settings.get("merge_transcripts", True))
        self.no_replacements = tk.BooleanVar(value=settings.get("no_replacements", False))

        # Engine-/Modell-Wahl. Pro Engine wird die zuletzt gewählte Modellgröße
        # gemerkt, damit ein Engine-Wechsel kein ungültiges Modell durchreicht.
        provider = settings.get("provider", "openai")
        if provider not in ENGINE_VALUES:
            provider = "openai"
        self.engine = tk.StringVar(value=ENGINE_VALUES[provider])
        self._model_per_provider = dict(DEFAULT_MODEL_BY_PROVIDER)
        self._model_per_provider.update(settings.get("model_per_provider", {}))
        self.model = tk.StringVar(value=self._model_per_provider[provider])

        # Optionale LLM-Korrektur (lokal, abschaltbar).
        self.correct_enabled = tk.BooleanVar(value=settings.get("correct_enabled", False))
        corr_backend = settings.get("correct_backend", "ollama")
        if corr_backend not in CORRECTION_BACKEND_VALUES:
            corr_backend = "ollama"
        self.correct_backend = tk.StringVar(value=CORRECTION_BACKEND_VALUES[corr_backend])
        self.correct_model = tk.StringVar(value=settings.get("correct_model", ""))

        # Prozess-Tracking
        self.running_process = None

        self._create_widgets()
        self._init_window_size(settings)
        self._check_api_key()

        # Einstellungen beim Schließen sichern.
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _check_api_key(self):
        """Prüft ob der API-Key gesetzt ist (nur für die Cloud-Engine relevant)."""
        if self._current_provider() == "openai" and not os.getenv("OPENAI_API_KEY"):
            self._log("⚠ WARNUNG: OPENAI_API_KEY ist nicht gesetzt!")
            self._log("   Bitte in der .env-Datei eintragen (oder Engine 'Lokal' waehlen).\n")

    def _current_provider(self) -> str:
        """Interner Provider-Wert (openai/local) der aktuellen Engine-Wahl."""
        return ENGINE_LABELS.get(self.engine.get(), "openai")

    def _refresh_model_choices(self):
        """Füllt das Modell-Dropdown passend zur gewählten Engine."""
        provider = self._current_provider()
        self.model_combo["values"] = MODELS_BY_PROVIDER[provider]
        # Gemerkte Auswahl für diese Engine setzen.
        self.model.set(self._model_per_provider.get(
            provider, DEFAULT_MODEL_BY_PROVIDER[provider]))
        if provider == "local":
            self.engine_hint.config(text="kostenlos, laeuft lokal ohne API-Key/Netz")
        else:
            self.engine_hint.config(text="ca. $0.003/Min (gpt-4o-mini-transcribe)")

    def _on_engine_change(self, event=None):
        """Engine gewechselt -> Modell-Dropdown anpassen."""
        self._refresh_model_choices()

    def _on_model_change(self, event=None):
        """Gewählte Modellgröße für die aktuelle Engine merken."""
        self._model_per_provider[self._current_provider()] = self.model.get()

    def _init_window_size(self, settings: dict):
        """
        Setzt die Start-Fenstergröße: zuletzt gespeicherte Größe, sonst die
        tatsächlich benötigte Inhaltshöhe (gedeckelt auf Bildschirmhöhe).
        Verhindert, dass neue GUI-Elemente unten aus dem Fenster wachsen.
        """
        saved = settings.get("window_size")
        if saved and re.fullmatch(r"\d{3,4}x\d{3,4}", saved):
            self.root.geometry(saved)
            return
        self.root.update_idletasks()
        width = max(self.root.winfo_reqwidth(), 700)
        height = min(self.root.winfo_reqheight() + 20,
                     self.root.winfo_screenheight() - 80)
        self.root.geometry(f"{width}x{height}")

    def _is_single_mode(self) -> bool:
        """True, wenn der Einzeldatei-Workflow (neuer Standard) aktiv ist."""
        return self.workflow_mode.get() == "single"

    def _on_mode_change(self):
        """Workflow-Modus gewechselt -> Buttons/Checkboxen anpassen."""
        single = self._is_single_mode()
        self.join_btn.config(state=tk.DISABLED if single else tk.NORMAL)
        self.move_transcribe_check.config(
            state=tk.NORMAL if single else tk.DISABLED)
        self.merge_check.config(
            state=tk.NORMAL if single else tk.DISABLED)
        self.workflow_btn.config(
            text=WORKFLOW_TEXT_SINGLE if single else WORKFLOW_TEXT_JOIN)

    def _current_correct_backend(self) -> str:
        """Interner Backend-Wert (ollama/lmstudio) der Korrektur-Wahl."""
        return CORRECTION_BACKEND_LABELS.get(self.correct_backend.get(), "ollama")

    def _on_correct_toggle(self):
        """Backend-/Modell-Felder je nach Korrektur-Checkbox aktivieren."""
        state = tk.NORMAL if self.correct_enabled.get() else tk.DISABLED
        # Combobox bleibt readonly, wenn aktiv; sonst disabled.
        self.correct_backend_combo.config(
            state="readonly" if self.correct_enabled.get() else tk.DISABLED)
        self.correct_model_entry.config(state=state)

    def _load_settings(self) -> dict:
        """Lädt gespeicherte Einstellungen (leeres Dict bei Fehler/keine Datei)."""
        try:
            return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_settings(self):
        """Speichert die aktuellen Einstellungen neben der App."""
        # Aktuelle Modellwahl in die Pro-Engine-Map übernehmen.
        self._model_per_provider[self._current_provider()] = self.model.get()
        data = {
            "input_folder": self.input_folder.get(),
            "output_folder": self.output_folder.get(),
            "prompt": self.prompt.get(),
            "move_after_join": self.move_after_join.get(),
            "no_replacements": self.no_replacements.get(),
            "workflow_mode": self.workflow_mode.get(),
            "move_after_transcribe": self.move_after_transcribe.get(),
            "merge_transcripts": self.merge_transcripts.get(),
            "provider": self._current_provider(),
            "model_per_provider": self._model_per_provider,
            "correct_enabled": self.correct_enabled.get(),
            "correct_backend": self._current_correct_backend(),
            "correct_model": self.correct_model.get(),
            # Nur Breite x Höhe (ohne Position) merken — eine gespeicherte
            # Position könnte nach Monitorwechsel außerhalb des Sichtbaren liegen.
            "window_size": self.root.geometry().split("+")[0],
        }
        try:
            SETTINGS_FILE.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    def _on_close(self):
        """Beim Schließen Einstellungen sichern und Fenster beenden."""
        self._save_settings()
        self.root.destroy()

    def _create_widgets(self):
        """Erstellt alle GUI-Elemente."""
        # Hauptcontainer mit Padding
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.pack(fill=tk.BOTH, expand=True)

        # === Ordner-Einstellungen ===
        folder_frame = ttk.LabelFrame(main_frame, text="Ordner", padding="10")
        folder_frame.pack(fill=tk.X, pady=(0, 10))

        # Input-Ordner
        ttk.Label(folder_frame, text="Input:").grid(row=0, column=0, sticky=tk.W, padx=(0, 5))
        ttk.Entry(folder_frame, textvariable=self.input_folder, width=40).grid(row=0, column=1, sticky=tk.EW, padx=(0, 5))
        ttk.Button(folder_frame, text="...", width=3, command=self._browse_input).grid(row=0, column=2)

        # Output-Ordner
        ttk.Label(folder_frame, text="Output:").grid(row=1, column=0, sticky=tk.W, padx=(0, 5), pady=(5, 0))
        ttk.Entry(folder_frame, textvariable=self.output_folder, width=40).grid(row=1, column=1, sticky=tk.EW, padx=(0, 5), pady=(5, 0))
        ttk.Button(folder_frame, text="...", width=3, command=self._browse_output).grid(row=1, column=2, pady=(5, 0))

        folder_frame.columnconfigure(1, weight=1)

        # === Aktionen ===
        action_frame = ttk.LabelFrame(main_frame, text="Aktionen", padding="10")
        action_frame.pack(fill=tk.X, pady=(0, 10))

        # Workflow-Modus (BUG-TRANSCRIBE-001): Einzeltranskription ist Standard.
        mode_frame = ttk.Frame(action_frame)
        mode_frame.pack(fill=tk.X, pady=(0, 8))

        ttk.Radiobutton(
            mode_frame,
            text="Einzeln transkribieren (Standard) — Metadaten je Aufnahme bleiben erhalten",
            value="single",
            variable=self.workflow_mode,
            command=self._on_mode_change,
        ).pack(anchor=tk.W)
        ttk.Radiobutton(
            mode_frame,
            text="Vorher zusammenfügen (Sonderfall) — Aufnahmen bewusst als eine Einheit",
            value="join",
            variable=self.workflow_mode,
            command=self._on_mode_change,
        ).pack(anchor=tk.W)

        ttk.Separator(action_frame, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=(0, 8))

        # Buttons nebeneinander
        button_container = ttk.Frame(action_frame)
        button_container.pack(fill=tk.X)

        # Join Audio Button
        join_frame = ttk.Frame(button_container)
        join_frame.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(0, 5))

        self.join_btn = ttk.Button(
            join_frame,
            text="1. Audio zusammenfügen",
            command=self._join_audio,
            style="Action.TButton"
        )
        self.join_btn.pack(fill=tk.X)

        ttk.Checkbutton(
            join_frame,
            text="Nach Zusammenfügen verschieben",
            variable=self.move_after_join
        ).pack(anchor=tk.W, pady=(5, 0))

        # Transkribieren Button
        transcribe_frame = ttk.Frame(button_container)
        transcribe_frame.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(5, 0))

        self.transcribe_btn = ttk.Button(
            transcribe_frame,
            text="2. Transkribieren",
            command=self._transcribe,
            style="Action.TButton"
        )
        self.transcribe_btn.pack(fill=tk.X)

        ttk.Checkbutton(
            transcribe_frame,
            text="Bereits transkribierte neu verarbeiten",
            variable=self.force_retranscribe
        ).pack(anchor=tk.W, pady=(5, 0))

        self.move_transcribe_check = ttk.Checkbutton(
            transcribe_frame,
            text="Quellordner nach processed/ verschieben",
            variable=self.move_after_transcribe
        )
        self.move_transcribe_check.pack(anchor=tk.W, pady=(5, 0))

        self.merge_check = ttk.Checkbutton(
            transcribe_frame,
            text="Sammeltranskript je Ordner erstellen",
            variable=self.merge_transcripts
        )
        self.merge_check.pack(anchor=tk.W, pady=(5, 0))

        # Kompletter Workflow Button
        ttk.Separator(action_frame, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=10)

        self.workflow_btn = ttk.Button(
            action_frame,
            text=WORKFLOW_TEXT_SINGLE,
            command=self._full_workflow,
            style="Primary.TButton"
        )
        self.workflow_btn.pack(fill=tk.X)

        # Buttons/Checkboxen an den gespeicherten Workflow-Modus anpassen.
        self._on_mode_change()

        # === Engine (Transkription) ===
        engine_frame = ttk.LabelFrame(main_frame, text="Engine (Transkription)", padding="10")
        engine_frame.pack(fill=tk.X, pady=(0, 10))

        ttk.Label(engine_frame, text="Engine:").grid(row=0, column=0, sticky=tk.W, padx=(0, 5))
        self.engine_combo = ttk.Combobox(
            engine_frame,
            textvariable=self.engine,
            values=list(ENGINE_LABELS.keys()),
            state="readonly",
            width=34,
        )
        self.engine_combo.grid(row=0, column=1, sticky=tk.EW)
        self.engine_combo.bind("<<ComboboxSelected>>", self._on_engine_change)

        ttk.Label(engine_frame, text="Modell:").grid(row=1, column=0, sticky=tk.W, padx=(0, 5), pady=(5, 0))
        self.model_combo = ttk.Combobox(
            engine_frame,
            textvariable=self.model,
            state="readonly",
            width=34,
        )
        self.model_combo.grid(row=1, column=1, sticky=tk.EW, pady=(5, 0))
        self.model_combo.bind("<<ComboboxSelected>>", self._on_model_change)

        self.engine_hint = ttk.Label(engine_frame, text="", foreground="gray")
        self.engine_hint.grid(row=2, column=1, sticky=tk.W, pady=(5, 0))

        engine_frame.columnconfigure(1, weight=1)

        # Modell-Dropdown passend zur aktuellen Engine befüllen.
        self._refresh_model_choices()

        # === Optionen ===
        options_frame = ttk.LabelFrame(main_frame, text="Optionen", padding="10")
        options_frame.pack(fill=tk.X, pady=(0, 10))

        # Prompt
        ttk.Label(options_frame, text="Kontext-Prompt (für bessere Erkennung):").pack(anchor=tk.W)
        prompt_entry = ttk.Entry(options_frame, textvariable=self.prompt)
        prompt_entry.pack(fill=tk.X, pady=(2, 5))

        # Weitere Optionen
        ttk.Checkbutton(
            options_frame,
            text="Automatische Ersetzungen deaktivieren",
            variable=self.no_replacements
        ).pack(anchor=tk.W)

        # === LLM-Korrektur (lokal) ===
        correct_frame = ttk.LabelFrame(main_frame, text="LLM-Korrektur (lokal, optional)", padding="10")
        correct_frame.pack(fill=tk.X, pady=(0, 10))

        ttk.Checkbutton(
            correct_frame,
            text="LLM-Korrektur aktivieren (benötigt laufendes Ollama/LM Studio)",
            variable=self.correct_enabled,
            command=self._on_correct_toggle,
        ).grid(row=0, column=0, columnspan=2, sticky=tk.W)

        ttk.Label(correct_frame, text="Backend:").grid(row=1, column=0, sticky=tk.W, padx=(0, 5), pady=(5, 0))
        self.correct_backend_combo = ttk.Combobox(
            correct_frame,
            textvariable=self.correct_backend,
            values=list(CORRECTION_BACKEND_LABELS.keys()),
            state="readonly",
            width=20,
        )
        self.correct_backend_combo.grid(row=1, column=1, sticky=tk.W, pady=(5, 0))

        ttk.Label(correct_frame, text="Modell:").grid(row=2, column=0, sticky=tk.W, padx=(0, 5), pady=(5, 0))
        self.correct_model_entry = ttk.Entry(correct_frame, textvariable=self.correct_model)
        self.correct_model_entry.grid(row=2, column=1, sticky=tk.EW, pady=(5, 0))
        ttk.Label(
            correct_frame,
            text="z. B. llama3.1:8b (Ollama) — ein geladenes Modell angeben",
            foreground="gray",
        ).grid(row=3, column=1, sticky=tk.W, pady=(2, 0))

        correct_frame.columnconfigure(1, weight=1)

        # Korrektur-Felder passend zum Checkbox-Status aktivieren/deaktivieren.
        self._on_correct_toggle()

        # === Log-Ausgabe ===
        log_frame = ttk.LabelFrame(main_frame, text="Ausgabe", padding="10")
        log_frame.pack(fill=tk.BOTH, expand=True)

        self.log_text = scrolledtext.ScrolledText(
            log_frame,
            height=15,
            font=("Consolas", 9),
            state=tk.DISABLED,
            wrap=tk.WORD
        )
        self.log_text.pack(fill=tk.BOTH, expand=True)

        # Clear-Button
        ttk.Button(log_frame, text="Ausgabe löschen", command=self._clear_log).pack(anchor=tk.E, pady=(5, 0))

        # === Statusleiste ===
        self.status_var = tk.StringVar(value="Bereit")
        status_bar = ttk.Label(main_frame, textvariable=self.status_var, relief=tk.SUNKEN, padding=(5, 2))
        status_bar.pack(fill=tk.X, pady=(10, 0))

        # Styles definieren
        style = ttk.Style()
        style.configure("Action.TButton", padding=10)
        style.configure("Primary.TButton", padding=10)

    def _browse_input(self):
        """Öffnet Ordner-Dialog für Input."""
        folder = filedialog.askdirectory(initialdir=self.input_folder.get())
        if folder:
            self.input_folder.set(folder)

    def _browse_output(self):
        """Öffnet Ordner-Dialog für Output."""
        folder = filedialog.askdirectory(initialdir=self.output_folder.get())
        if folder:
            self.output_folder.set(folder)

    def _log(self, message: str):
        """Fügt eine Nachricht zum Log hinzu."""
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, message + "\n")
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

    def _clear_log(self):
        """Löscht die Log-Ausgabe."""
        self.log_text.config(state=tk.NORMAL)
        self.log_text.delete(1.0, tk.END)
        self.log_text.config(state=tk.DISABLED)

    def _set_buttons_state(self, enabled: bool):
        """Aktiviert/Deaktiviert alle Aktions-Buttons (modusabhängig)."""
        state = tk.NORMAL if enabled else tk.DISABLED
        # Join-Button bleibt im Einzelmodus deaktiviert.
        if enabled and self._is_single_mode():
            self.join_btn.config(state=tk.DISABLED)
        else:
            self.join_btn.config(state=state)
        self.transcribe_btn.config(state=state)
        self.workflow_btn.config(state=state)

    def _run_command(self, cmd: list[str], description: str, callback=None):
        """Führt einen Befehl im Hintergrund aus."""
        def run():
            self._set_buttons_state(False)
            self.status_var.set(f"Läuft: {description}...")
            self._log(f"\n{'=' * 50}")
            self._log(f"▶ {description}")
            self._log(f"Befehl: {' '.join(cmd)}")
            self._log(f"{'=' * 50}\n")

            try:
                # Starte Prozess
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    encoding="utf-8",
                    errors="replace"
                )
                self.running_process = process

                # Lese Output zeilenweise
                for line in process.stdout:
                    # Update im Main-Thread
                    self.root.after(0, lambda l=line: self._log(l.rstrip()))

                process.wait()

                if process.returncode == 0:
                    self.root.after(0, lambda: self._log(f"\n✔ {description} abgeschlossen.\n"))
                    self.root.after(0, lambda: self.status_var.set("Bereit"))
                    if callback:
                        self.root.after(0, callback)
                else:
                    self.root.after(0, lambda: self._log(f"\n✖ {description} fehlgeschlagen (Code: {process.returncode})\n"))
                    self.root.after(0, lambda: self.status_var.set("Fehler aufgetreten"))

            except Exception as e:
                self.root.after(0, lambda: self._log(f"\n✖ Fehler: {e}\n"))
                self.root.after(0, lambda: self.status_var.set("Fehler aufgetreten"))

            finally:
                self.running_process = None
                self.root.after(0, lambda: self._set_buttons_state(True))

        # Starte in eigenem Thread
        thread = threading.Thread(target=run, daemon=True)
        thread.start()

    def _join_audio(self):
        """Führt das Audio-Zusammenfügen aus."""
        input_path = Path(self.input_folder.get())

        if not input_path.exists():
            messagebox.showerror("Fehler", f"Input-Ordner existiert nicht:\n{input_path}")
            return

        cmd = [sys.executable, "join_audio.py", str(input_path), "--all-subfolders"]

        if self.move_after_join.get():
            cmd.append("--move")

        self._run_command(cmd, "Audio zusammenfügen")

    def _transcribe(self):
        """Führt die Transkription aus."""
        input_path = Path(self.input_folder.get())

        if not input_path.exists():
            messagebox.showerror("Fehler", f"Input-Ordner existiert nicht:\n{input_path}")
            return

        cmd = [
            sys.executable, "transcribe.py",
            str(input_path),
            "--output-dir", self.output_folder.get(),
            "--provider", self._current_provider(),
            "--model", self.model.get(),
        ]

        if self.prompt.get().strip():
            cmd.extend(["--prompt", self.prompt.get()])

        if self.force_retranscribe.get():
            cmd.append("--force")

        if self.no_replacements.get():
            cmd.append("--no-replacements")

        # Einzeldatei-Workflow: Metadaten-Kopf je Aufnahme; optional Quellordner
        # nach fehlerfreiem Lauf aufräumen.
        if self._is_single_mode():
            cmd.append("--metadata-header")
            if self.merge_transcripts.get():
                cmd.append("--merge-transcripts")
            if self.move_after_transcribe.get():
                cmd.append("--move-processed")

        if self.correct_enabled.get():
            model = self.correct_model.get().strip()
            if not model:
                messagebox.showwarning(
                    "LLM-Korrektur",
                    "Für die LLM-Korrektur muss ein Modellname angegeben werden "
                    "(z. B. llama3.1:8b).\nBitte eintragen oder die Korrektur "
                    "deaktivieren.",
                )
                return
            cmd.extend([
                "--correct",
                "--correct-backend", self._current_correct_backend(),
                "--correct-model", model,
            ])

        self._run_command(cmd, "Transkription")

    def _full_workflow(self):
        """Führt den kompletten Workflow aus (modusabhängig)."""
        input_path = Path(self.input_folder.get())

        if not input_path.exists():
            messagebox.showerror("Fehler", f"Input-Ordner existiert nicht:\n{input_path}")
            return

        # Einzelmodus (Standard): kein Join — direkt einzeln transkribieren.
        if self._is_single_mode():
            self._transcribe()
            return

        # Sonderfall: Erst Join, dann Transcribe
        cmd = [sys.executable, "join_audio.py", str(input_path), "--all-subfolders"]

        if self.move_after_join.get():
            cmd.append("--move")

        # Nach dem Join -> Transcribe starten
        self._run_command(cmd, "Audio zusammenfügen", callback=self._transcribe)


def main():
    root = tk.Tk()

    # Icon setzen (falls vorhanden)
    try:
        # Für Windows .ico
        icon_path = Path(__file__).parent / "icon.ico"
        if icon_path.exists():
            root.iconbitmap(str(icon_path))
    except Exception:
        pass

    app = TranscriptionGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
