"""
Fügt alle Audio-Dateien aus einem Ordner zu einer einzigen Datei zusammen.

Verwendung:
    python join_audio.py input/My_Cents_10_12_2025

Ergebnis:
    input/My_Cents_10_12_2025.mp3

Voraussetzung: ffmpeg muss installiert sein.
"""

import argparse
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median

AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".webm"}

# Standard-Bitrate für Komprimierung (Mono, optimal für Sprache)
# 64k Mono entspricht der Qualität von 128k Stereo
DEFAULT_BITRATE = "64k"


def get_audio_files(folder: Path) -> list[Path]:
    """Sammelt alle Audio-Dateien im Ordner (unsortiert)."""
    files = [
        f for f in folder.iterdir()
        if f.is_file() and f.suffix.lower() in AUDIO_EXTS
    ]
    return files


def extract_number_from_filename(filename: str) -> int | None:
    """Extrahiert eine Nummer aus dem Dateinamen für Sortierung."""
    # Suche nach Nummern wie 001, 002 oder _001, _002
    match = re.search(r'(\d+)', filename)
    if match:
        return int(match.group(1))
    return None


def get_file_mtime(file: Path) -> datetime:
    """Gibt das Änderungsdatum einer Datei zurück."""
    return datetime.fromtimestamp(file.stat().st_mtime)


def detect_date_outliers(files: list[Path], threshold_hours: int = 24) -> tuple[list[Path], list[Path]]:
    """
    Erkennt Datums-Ausreißer (z.B. durch Batterie-Reset).
    Gibt (normale_dateien, ausreißer) zurück.
    """
    if len(files) < 2:
        return files, []

    # Hole alle Änderungsdaten
    dates = [(f, get_file_mtime(f)) for f in files]

    # Berechne Median der Timestamps
    timestamps = [d.timestamp() for _, d in dates]
    median_ts = median(timestamps)
    median_date = datetime.fromtimestamp(median_ts)

    normal = []
    outliers = []

    for file, date in dates:
        diff = abs((date - median_date).total_seconds() / 3600)  # Differenz in Stunden
        if diff > threshold_hours:
            outliers.append(file)
        else:
            normal.append(file)

    return normal, outliers


def can_sort_by_filename(files: list[Path]) -> bool:
    """Prüft, ob alle Dateien eine extrahierbare Nummer haben."""
    numbers = [extract_number_from_filename(f.name) for f in files]
    # Alle müssen eine Nummer haben und die Nummern müssen eindeutig sein
    if None in numbers:
        return False
    return len(numbers) == len(set(numbers))


def sort_files_smart(files: list[Path]) -> tuple[list[Path], str, bool]:
    """
    Sortiert Dateien intelligent.
    Gibt zurück: (sortierte_liste, methode, erfolg)
    """
    if not files:
        return [], "keine Dateien", False

    if len(files) == 1:
        return files, "einzelne Datei", True

    # Versuche 1: Nach Dateiname (wenn nummeriert)
    if can_sort_by_filename(files):
        sorted_files = sorted(files, key=lambda f: extract_number_from_filename(f.name) or 0)
        return sorted_files, "Dateiname (nummeriert)", True

    # Versuche 2: Nach Datum (mit Ausreißer-Erkennung)
    normal_files, outliers = detect_date_outliers(files)

    if not outliers:
        # Alle Daten sind konsistent - sortiere nach Datum (älteste zuerst)
        sorted_files = sorted(files, key=lambda f: get_file_mtime(f))
        return sorted_files, "Aenderungsdatum", True

    if len(outliers) < len(files) / 2:
        # Nur wenige Ausreißer - sortiere normale nach Datum, Ausreißer ans Ende
        sorted_normal = sorted(normal_files, key=lambda f: get_file_mtime(f))
        # Ausreißer nach Dateiname sortieren als Fallback
        sorted_outliers = sorted(outliers, key=lambda f: f.name)
        print(f"   [!] {len(outliers)} Datei(en) mit ungewoehnlichem Datum erkannt:")
        for o in sorted_outliers:
            print(f"      - {o.name} ({get_file_mtime(o).strftime('%d.%m.%Y %H:%M')})")
        return sorted_normal + sorted_outliers, "Datum (mit Ausreissern am Ende)", False

    # Zu viele Ausreißer oder keine klare Sortierung möglich
    return sorted(files, key=lambda f: f.name), "Dateiname (alphabetisch)", False


def format_file_list(files: list[Path], show_date: bool = True) -> str:
    """Formatiert eine Dateiliste für die Anzeige."""
    lines = []
    for i, f in enumerate(files, 1):
        date_str = ""
        if show_date:
            date_str = f"  ({get_file_mtime(f).strftime('%d.%m.%Y %H:%M')})"
        lines.append(f"  [{i}] {f.name}{date_str}")
    return "\n".join(lines)


def ask_manual_order(files: list[Path]) -> list[Path]:
    """
    Fragt den Benutzer nach der gewünschten Reihenfolge.
    """
    print("\n" + "=" * 60)
    print("MANUELLE SORTIERUNG ERFORDERLICH")
    print("=" * 60)
    print("\nDateien konnten nicht automatisch sortiert werden:")
    print(format_file_list(files))
    print("\nGib die gewünschte Reihenfolge ein (z.B. '2,1,3' oder '2 1 3')")
    print("Oder drücke Enter, um die aktuelle Reihenfolge zu übernehmen.")
    print("-" * 60)

    while True:
        try:
            user_input = input("Reihenfolge: ").strip()

            if not user_input:
                print("-> Uebernehme aktuelle Reihenfolge.")
                return files

            # Parse Eingabe (Komma oder Leerzeichen getrennt)
            parts = re.split(r'[,\s]+', user_input)
            indices = [int(p) for p in parts if p]

            # Validierung
            if len(indices) != len(files):
                print(f"   [X] Bitte genau {len(files)} Nummern eingeben.")
                continue

            if set(indices) != set(range(1, len(files) + 1)):
                print(f"   [X] Bitte die Nummern 1-{len(files)} verwenden (jede genau einmal).")
                continue

            # Umordnen
            reordered = [files[i - 1] for i in indices]
            print("\n-> Neue Reihenfolge:")
            print(format_file_list(reordered, show_date=False))
            return reordered

        except ValueError:
            print("   [X] Ungueltige Eingabe. Bitte nur Zahlen eingeben.")
        except KeyboardInterrupt:
            print("\n-> Abgebrochen, uebernehme aktuelle Reihenfolge.")
            return files


def join_audio_files(
    audio_files: list[Path],
    output_path: Path,
    compress: bool = True,
    bitrate: str = DEFAULT_BITRATE,
) -> None:
    """
    Fügt Audio-Dateien mit ffmpeg zusammen.
    Bei compress=True wird auf die angegebene Bitrate komprimiert (Standard: 128k).
    """
    # Temporäre Datei mit Liste der Dateien für ffmpeg
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as f:
        for audio in audio_files:
            # ffmpeg concat format: file 'pfad'
            # Pfade mit Escape für Windows
            escaped_path = str(audio.resolve()).replace("\\", "/").replace("'", "'\\''")
            f.write(f"file '{escaped_path}'\n")
        concat_file = Path(f.name)

    try:
        if compress:
            print(f"-> Fuege {len(audio_files)} Dateien zusammen (komprimiert auf {bitrate})...")
        else:
            print(f"-> Fuege {len(audio_files)} Dateien zusammen (ohne Komprimierung)...")

        cmd = [
            "ffmpeg",
            "-y",  # Überschreiben ohne Nachfrage
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_file),
        ]

        if compress:
            # Neu-Kodierung mit Komprimierung und Mono (optimal für Sprache)
            cmd.extend(["-c:a", "libmp3lame", "-b:a", bitrate, "-ac", "1"])
        else:
            # Keine Neu-Kodierung (schnell & verlustfrei)
            cmd.extend(["-c", "copy"])

        cmd.append(str(output_path))

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            print(f"   [X] ffmpeg Fehler: {result.stderr}")
            raise RuntimeError(f"ffmpeg fehlgeschlagen: {result.stderr}")

        # Dateigröße anzeigen
        size_mb = output_path.stat().st_size / (1024 * 1024)
        print(f"   [OK] Gespeichert: {output_path} ({size_mb:.1f} MB)")

    finally:
        # Temporäre Datei aufräumen
        concat_file.unlink(missing_ok=True)


def move_to_processed(folder_path: Path) -> Path:
    """Verschiebt einen Ordner nach input/processed/"""
    processed_dir = folder_path.parent / "processed"
    processed_dir.mkdir(exist_ok=True)

    dest = processed_dir / folder_path.name

    # Falls Ziel existiert, nummerieren
    if dest.exists():
        i = 1
        while (processed_dir / f"{folder_path.name}_{i}").exists():
            i += 1
        dest = processed_dir / f"{folder_path.name}_{i}"

    shutil.move(str(folder_path), str(dest))
    print(f"   [->] Verschoben nach: processed/{dest.name}")
    return dest


def process_folder(
    folder_path: Path,
    move_after: bool = False,
    delete_folder: bool = False,
    dry_run: bool = False,
    auto_confirm: bool = False,
    compress: bool = True,
    bitrate: str = DEFAULT_BITRATE,
) -> Path | None:
    """
    Verarbeitet einen Ordner: Fügt alle Audio-Dateien zusammen.
    Output-Datei bekommt den Namen des Ordners + .mp3
    """
    if not folder_path.is_dir():
        raise ValueError(f"Kein Ordner: {folder_path}")

    audio_files = get_audio_files(folder_path)

    if not audio_files:
        raise ValueError(f"Keine Audio-Dateien in {folder_path} gefunden")

    print(f"\n{'=' * 60}")
    print(f"Ordner: {folder_path.name}")
    print(f"{'=' * 60}")
    print(f"Gefundene Dateien: {len(audio_files)}")

    # Intelligente Sortierung
    sorted_files, sort_method, auto_success = sort_files_smart(audio_files)

    print(f"Sortierung: {sort_method}")
    print(format_file_list(sorted_files))

    # Bei fehlgeschlagener Auto-Sortierung: Nachfragen
    if not auto_success and not auto_confirm and not dry_run:
        print(f"\n[!] Automatische Sortierung unsicher ({sort_method})")
        sorted_files = ask_manual_order(sorted_files)

    # Output-Datei: Ordnername + .mp3, im gleichen Parent-Verzeichnis
    output_path = folder_path.parent / f"{folder_path.name}.mp3"

    if dry_run:
        print(f"\n[DRY-RUN] Würde erstellen: {output_path}")
        print(f"[DRY-RUN] Komprimierung: {'ja (' + bitrate + ')' if compress else 'nein'}")
        print(f"[DRY-RUN] Reihenfolge:")
        for i, f in enumerate(sorted_files, 1):
            print(f"   {i}. {f.name}")
        return None

    join_audio_files(sorted_files, output_path, compress=compress, bitrate=bitrate)

    # Nach Verarbeitung: Ordner verschieben oder löschen
    if delete_folder:
        print(f"   [DEL] Loesche Ordner: {folder_path.name}")
        for f in audio_files:
            f.unlink()
        folder_path.rmdir()
    elif move_after:
        move_to_processed(folder_path)

    return output_path


def main():
    parser = argparse.ArgumentParser(
        description="Fügt alle Audio-Dateien aus einem Ordner zu einer Datei zusammen."
    )
    parser.add_argument(
        "folders",
        nargs="+",
        help="Ein oder mehrere Ordner mit Audio-Dateien (z.B. input/My_Cents_10_12_2025)",
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="Ordner nach erfolgreichem Zusammenfügen löschen.",
    )
    parser.add_argument(
        "--move",
        action="store_true",
        help="Ordner nach erfolgreichem Zusammenfügen nach 'processed/' verschieben.",
    )
    parser.add_argument(
        "--all-subfolders",
        action="store_true",
        help="Alle Unterordner im angegebenen Pfad verarbeiten.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Zeigt nur an, was gemacht würde (keine Änderungen).",
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help="Keine manuelle Bestätigung bei unsicherer Sortierung.",
    )
    parser.add_argument(
        "--no-compress",
        action="store_true",
        help="Keine Komprimierung (Standard: 128kbps für kleinere Dateien).",
    )
    parser.add_argument(
        "--bitrate",
        default=DEFAULT_BITRATE,
        help=f"Bitrate für Komprimierung (Standard: {DEFAULT_BITRATE}).",
    )

    args = parser.parse_args()

    # --delete und --move schließen sich aus
    if args.delete and args.move:
        print("Fehler: --delete und --move können nicht gleichzeitig verwendet werden.")
        return

    folders_to_process = []

    for folder_arg in args.folders:
        folder_path = Path(folder_arg).expanduser().resolve()

        if args.all_subfolders:
            # Alle Unterordner finden (außer 'processed')
            subfolders = [
                d for d in sorted(folder_path.iterdir())
                if d.is_dir() and d.name != "processed"
            ]
            if not subfolders:
                print(f"Keine Unterordner in {folder_path} gefunden.")
            folders_to_process.extend(subfolders)
        else:
            folders_to_process.append(folder_path)

    if not folders_to_process:
        print("Keine Ordner zum Verarbeiten gefunden.")
        return

    print(f"Zu verarbeiten: {len(folders_to_process)} Ordner")

    if args.dry_run:
        print("[DRY-RUN MODUS - keine Änderungen werden vorgenommen]")

    for folder in folders_to_process:
        try:
            process_folder(
                folder,
                move_after=args.move,
                delete_folder=args.delete,
                dry_run=args.dry_run,
                auto_confirm=args.auto,
                compress=not args.no_compress,
                bitrate=args.bitrate,
            )
        except Exception as e:
            print(f"   [X] Fehler bei {folder.name}: {e}")


if __name__ == "__main__":
    main()
