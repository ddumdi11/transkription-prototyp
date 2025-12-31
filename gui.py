"""
GUI für den Transkriptions-Prototyp.

Starte mit:
    python gui.py
"""

import os
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk, scrolledtext, filedialog, messagebox
from pathlib import Path

from dotenv import load_dotenv

# .env laden für API-Key Check
load_dotenv()


class TranscriptionGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Transkriptions-Prototyp")
        self.root.geometry("700x600")
        self.root.minsize(600, 500)

        # Variablen für Optionen
        self.input_folder = tk.StringVar(value="input")
        self.output_folder = tk.StringVar(value="output")
        self.prompt = tk.StringVar(value="Fachbegriffe: Diktiergerät, Claude, Claude Code, KI")
        self.move_after_join = tk.BooleanVar(value=True)
        self.force_retranscribe = tk.BooleanVar(value=False)
        self.no_replacements = tk.BooleanVar(value=False)

        # Prozess-Tracking
        self.running_process = None

        self._create_widgets()
        self._check_api_key()

    def _check_api_key(self):
        """Prüft ob der API-Key gesetzt ist."""
        if not os.getenv("OPENAI_API_KEY"):
            self._log("⚠ WARNUNG: OPENAI_API_KEY ist nicht gesetzt!")
            self._log("   Bitte in der .env-Datei eintragen.\n")

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

        # Kompletter Workflow Button
        ttk.Separator(action_frame, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=10)

        self.workflow_btn = ttk.Button(
            action_frame,
            text="▶ Kompletter Workflow (1 + 2)",
            command=self._full_workflow,
            style="Primary.TButton"
        )
        self.workflow_btn.pack(fill=tk.X)

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
        """Aktiviert/Deaktiviert alle Aktions-Buttons."""
        state = tk.NORMAL if enabled else tk.DISABLED
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
            "--output-dir", self.output_folder.get()
        ]

        if self.prompt.get().strip():
            cmd.extend(["--prompt", self.prompt.get()])

        if self.force_retranscribe.get():
            cmd.append("--force")

        if self.no_replacements.get():
            cmd.append("--no-replacements")

        self._run_command(cmd, "Transkription")

    def _full_workflow(self):
        """Führt den kompletten Workflow aus (Join + Transcribe)."""
        input_path = Path(self.input_folder.get())

        if not input_path.exists():
            messagebox.showerror("Fehler", f"Input-Ordner existiert nicht:\n{input_path}")
            return

        # Erst Join, dann Transcribe
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
