"""
Zusammenführen bereits gespeicherter Transkripte zu einem Sammel-Markdown.

Dieses Modul arbeitet bewusst NUR auf den fertigen ``.md``-Transkripten
(separater Schritt, unabhängig vom Transkriptionslauf). Es enthält reine,
testbare Funktionen; die interaktive Vorschau/Abwahl liegt in ``gui.py``.

Kernaufgaben:
  * Datum aus dem Dateinamen erkennen (mehrere Schreibweisen),
  * Plausibilität gegen Header-Datum (``- Datum:``) und das Datum der
    Quell-Audiodatei (mtime) prüfen und Abweichungen MARKIEREN
    (keine automatische Auflösung — der Nutzer entscheidet),
  * Positiv-/Negativ-Filter über Wildcard-Muster auf den Dateinamen,
  * Eingrenzung über einen Zeitraum (Default: aktueller Monat),
  * Aufbau von Konsolidat-Markdown + separatem Quellenindex.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

# Endungen, die als Quell-Audio zur Datumsprüfung herangezogen werden.
AUDIO_SUFFIXES = (".mp3", ".wav", ".m4a", ".ogg", ".flac")


# ---------------------------------------------------------------------------
# Datum aus Dateiname
# ---------------------------------------------------------------------------

# 1) ISO: 2026-05-06 oder 2026_05_06  (Jahr zuerst, mit Trenner)
_RE_ISO = re.compile(r"(?<!\d)(\d{4})[-_](\d{2})[-_](\d{2})(?!\d)")
# 2) Europäisch: 06_05_2026 / 06.05.2026 / 06-05-2026  (Tag/Monat zuerst)
_RE_DMY = re.compile(r"(?<!\d)(\d{2})[-_.](\d{2})[-_.](\d{4})(?!\d)")
# 3) Kompakt vom Diktiergerät: 260106_004  -> YYMMDD, gefolgt von _<Nummer>
_RE_COMPACT = re.compile(r"(?<!\d)(\d{2})(\d{2})(\d{2})(?=_\d)")


def _safe_date(year: int, month: int, day: int) -> date | None:
    """Baut ein date oder None, wenn die Kombination ungültig ist."""
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_date_from_filename(name: str) -> date | None:
    """
    Versucht, ein Datum aus dem Dateinamen (oder Stem) zu lesen.

    Unterstützte Schreibweisen (in dieser Reihenfolge geprüft):
      * ``2026-05-06`` / ``2026_05_06``       (ISO, Jahr zuerst)
      * ``06_05_2026`` / ``06.05.2026``       (Tag_Monat_Jahr, europäisch)
      * ``260106_004``                        (YYMMDD vom Diktiergerät)

    Bei der europäischen Schreibweise wird bei einem unmöglichen Monat
    (> 12) einmalig Tag/Monat getauscht, um z. B. ``01_13_2025`` als
    13.01.2025 zu deuten. Echte Tag/Monat-Mehrdeutigkeiten werden NICHT
    geraten — dafür dient die Plausibilitätsprüfung gegen die Dateidaten.
    """
    stem = Path(name).stem

    m = _RE_ISO.search(stem)
    if m:
        d = _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if d:
            return d

    m = _RE_DMY.search(stem)
    if m:
        day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        d = _safe_date(year, month, day)
        if d:
            return d
        # Monat unmöglich -> Tag/Monat tauschen (z. B. 01_13_2025).
        if month > 12 and day <= 12:
            d = _safe_date(year, day, month)
            if d:
                return d

    m = _RE_COMPACT.search(stem)
    if m:
        yy = int(m.group(1))
        year = 2000 + yy if yy < 70 else 1900 + yy
        d = _safe_date(year, int(m.group(2)), int(m.group(3)))
        if d:
            return d

    return None


# ---------------------------------------------------------------------------
# Datum / Quelle aus dem .md-Inhalt
# ---------------------------------------------------------------------------

_RE_HEADER_DATUM = re.compile(r"^-\s*Datum:\s*(\d{4})-(\d{2})-(\d{2})", re.MULTILINE)
_RE_HEADER_DATEI = re.compile(r"^-\s*Datei:\s*(.+?)\s*$", re.MULTILINE)
_RE_TITLE_TRANSKRIPT = re.compile(r"^#\s*Transkript:\s*(.+?)\s*$", re.MULTILINE)


def extract_header_date(text: str) -> date | None:
    """Liest das Datum aus dem Metadaten-Kopf (``- Datum: YYYY-MM-DD``)."""
    m = _RE_HEADER_DATUM.search(text)
    if not m:
        return None
    return _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))


def extract_source_audio_name(text: str) -> str | None:
    """
    Ermittelt den Namen der Quell-Audiodatei aus dem .md-Kopf
    (``- Datei: X.mp3`` oder ``# Transkript: X.mp3``).
    """
    m = _RE_HEADER_DATEI.search(text)
    if m:
        return m.group(1).strip()
    m = _RE_TITLE_TRANSKRIPT.search(text)
    if m:
        return m.group(1).strip()
    return None


def audio_date(audio_name: str | None, search_dirs: list[str | Path]) -> date | None:
    """
    Sucht die Quell-Audiodatei in den angegebenen Ordnern und gibt ihr
    Änderungsdatum (mtime) als date zurück (None, wenn nicht gefunden).
    """
    if not audio_name:
        return None
    cand = Path(audio_name).name
    stem = Path(cand).stem
    for d in search_dirs:
        if not d:
            continue
        d = Path(d)
        if not d.exists():
            continue
        # Exakter Name zuerst, sonst gleicher Stem mit Audio-Endung.
        exact = d / cand
        if exact.exists():
            return datetime.fromtimestamp(exact.stat().st_mtime).date()
        for suf in AUDIO_SUFFIXES:
            p = d / f"{stem}{suf}"
            if p.exists():
                return datetime.fromtimestamp(p.stat().st_mtime).date()
    return None


# ---------------------------------------------------------------------------
# Wildcard-Filter
# ---------------------------------------------------------------------------

def split_patterns(raw: str) -> list[str]:
    """Zerlegt die Eingabe in einzelne Muster (Trenner: ; oder |)."""
    if not raw:
        return []
    parts = re.split(r"[;|]", raw)
    return [p.strip() for p in parts if p.strip()]


def _normalize_pattern(pattern: str) -> str:
    """
    Macht ein Muster glob-tauglich. Enthält es keine Platzhalter
    (``*``/``?``), wird es als Teilstring-Suche behandelt (``*muster*``).
    """
    if "*" in pattern or "?" in pattern:
        return pattern
    return f"*{pattern}*"


def matches_patterns(name: str, patterns: list[str]) -> bool:
    """
    True, wenn der Stem des Dateinamens MINDESTENS EINES der Muster trifft
    (Groß-/Kleinschreibung wird ignoriert).
    """
    stem = Path(name).stem
    for pat in patterns:
        if fnmatch.fnmatch(stem.lower(), _normalize_pattern(pat).lower()):
            return True
    return False


# ---------------------------------------------------------------------------
# Zeitraum
# ---------------------------------------------------------------------------

def default_month_range(today: date | None = None) -> tuple[date, date]:
    """
    Default-Zeitraum: erster bis letzter Tag des aktuellen Monats.
    (Wird im Juni gestartet -> 01.06. bis 30.06.)
    """
    today = today or date.today()
    start = today.replace(day=1)
    if start.month == 12:
        next_month = start.replace(year=start.year + 1, month=1)
    else:
        next_month = start.replace(month=start.month + 1)
    end = next_month.fromordinal(next_month.toordinal() - 1)
    return start, end


# ---------------------------------------------------------------------------
# Kandidaten sammeln
# ---------------------------------------------------------------------------

@dataclass
class Candidate:
    """Eine Transkript-Datei als Kandidat für die Zusammenführung."""
    path: Path
    filename_date: date | None = None
    header_date: date | None = None
    audio_date: date | None = None
    md_mtime: date | None = None
    filter_date: date | None = None      # für den Zeitraumfilter genutztes Datum
    date_source: str = ""                # woher filter_date stammt
    conflict: bool = False               # Datumsangaben widersprechen sich
    selected: bool = True                # Vorabauswahl (vom Nutzer abwählbar)
    in_range: bool = True                # liegt im Zeitraum
    note: str = ""                       # Klartext-Hinweis für die Vorschau

    @property
    def name(self) -> str:
        return self.path.name


def _pick_filter_date(c: Candidate) -> tuple[date | None, str]:
    """Priorität für das Filter-Datum: Dateiname > Header > Audio > mtime."""
    if c.filename_date:
        return c.filename_date, "Dateiname"
    if c.header_date:
        return c.header_date, "Header"
    if c.audio_date:
        return c.audio_date, "Audio"
    return c.md_mtime, "Datei-mtime"


def _detect_conflict(c: Candidate) -> bool:
    """
    Konflikt, wenn sich die 'echten' Aufnahmedaten widersprechen
    (Dateiname / Header / Audio). Die .md-mtime ist die Erstellzeit des
    Transkripts und wird hier bewusst NICHT als Konfliktquelle gewertet.
    """
    known = [d for d in (c.filename_date, c.header_date, c.audio_date)
             if d is not None]
    return len(known) >= 2 and len(set(known)) > 1


def build_candidate(path: Path, search_dirs: list[Path]) -> Candidate:
    """Erstellt einen Candidate inkl. aller Datumsquellen und Konflikt-Flag."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        text = ""
    c = Candidate(path=path)
    c.filename_date = parse_date_from_filename(path.name)
    c.header_date = extract_header_date(text)
    src = extract_source_audio_name(text)
    c.audio_date = audio_date(src, search_dirs)
    try:
        c.md_mtime = datetime.fromtimestamp(path.stat().st_mtime).date()
    except OSError:
        c.md_mtime = None
    c.filter_date, c.date_source = _pick_filter_date(c)
    c.conflict = _detect_conflict(c)
    if c.conflict:
        parts = []
        if c.filename_date:
            parts.append(f"Name {c.filename_date.isoformat()}")
        if c.header_date:
            parts.append(f"Header {c.header_date.isoformat()}")
        if c.audio_date:
            parts.append(f"Audio {c.audio_date.isoformat()}")
        c.note = "Datums-Konflikt: " + ", ".join(parts)
    elif c.filter_date is None:
        c.note = "kein Datum erkennbar"
    return c


def scan_candidates(
    source_dir: Path,
    raw_patterns: str,
    negative: bool,
    start: date,
    end: date,
    audio_dirs: list[Path] | None = None,
    recursive: bool = False,
) -> list[Candidate]:
    """
    Durchsucht ``source_dir`` nach ``.md``-Transkripten, wendet den
    Positiv-/Negativ-Wildcard-Filter an, ermittelt je Datei die Datumsquellen
    und markiert, welche im Zeitraum liegen.

    Rückgabe: Kandidaten, die dem Namensfilter entsprechen, nach Datum
    sortiert. Nur die im Zeitraum liegenden sind vorab ausgewählt; die
    übrigen werden zur Transparenz mitgeliefert (``in_range = False``).
    """
    source_dir = Path(source_dir)
    patterns = split_patterns(raw_patterns)
    search_dirs = [source_dir] + list(audio_dirs or [])

    files = (source_dir.rglob("*.md") if recursive
             else source_dir.glob("*.md"))

    result: list[Candidate] = []
    for p in sorted(files):
        if not p.is_file():
            continue
        hit = matches_patterns(p.name, patterns) if patterns else True
        # Ohne Muster: alle Dateien. Negativ-Filter ohne Muster wäre sinnlos.
        if patterns:
            keep = (not hit) if negative else hit
            if not keep:
                continue
        c = build_candidate(p, search_dirs)
        c.in_range = bool(c.filter_date and start <= c.filter_date <= end)
        c.selected = c.in_range
        result.append(c)

    result.sort(key=lambda c: (c.filter_date or date.max, c.name))
    return result


# ---------------------------------------------------------------------------
# Ausgabe: Konsolidat + Quellenindex
# ---------------------------------------------------------------------------

def default_basename(label: str, start: date, end: date) -> str:
    """
    Schlägt einen Basisnamen vor. Liegt der Zeitraum in einem einzigen
    Monat, wird ``..._YYYY-MM`` verwendet, sonst ``..._YYYY-MM-DD_bis_...``.
    """
    label = re.sub(r"[^\w\-]+", "", label) or "Transkripte"
    if (start.year, start.month) == (end.year, end.month):
        period = start.strftime("%Y-%m")
    else:
        period = f"{start.isoformat()}_bis_{end.isoformat()}"
    return f"Konsolidat_{label}_{period}"


def build_index_text(
    candidates: list[Candidate], label: str, start: date, end: date,
    mode_text: str, pattern_text: str,
) -> str:
    """Baut den separaten Quellenindex (Tabelle mit Datum + Plausibilität)."""
    lines = [
        f"# Quellenindex {label}",
        "",
        f"- Zeitraum: {start.isoformat()} bis {end.isoformat()}",
        f"- Filter: {mode_text} `{pattern_text or '(kein Muster)'}`",
        f"- Erstellt: {date.today().isoformat()}",
        f"- Enthaltene Quellen: {len(candidates)}",
        "",
        "| Quelle | Datum | Quelle d. Datums | Header | Audio | Plausibilität |",
        "|---|---|---|---|---|---|",
    ]
    for c in candidates:
        fdate = c.filter_date.isoformat() if c.filter_date else "—"
        hdate = c.header_date.isoformat() if c.header_date else "—"
        adate = c.audio_date.isoformat() if c.audio_date else "—"
        status = "⚠ Konflikt" if c.conflict else ("OK" if c.filter_date else "? kein Datum")
        lines.append(
            f"| {c.name} | {fdate} | {c.date_source} | {hdate} | {adate} | {status} |"
        )
    lines.append("")
    return "\n".join(lines)


def build_consolidation_text(
    candidates: list[Candidate], label: str, start: date, end: date,
    mode_text: str, pattern_text: str, index_filename: str | None = None,
) -> str:
    """
    Baut das Konsolidat: Kopf (Zweck/Zeitraum/Filter) + Quellenliste +
    die Rohtexte der Einzeltranskripte nacheinander (Trenner dazwischen).
    """
    head = [
        f"# Konsolidat {label} — {start.isoformat()} bis {end.isoformat()}",
        "",
        "## Zweck",
        "",
        "Dieses Dokument bündelt bereits gespeicherte Transkripte des "
        "gewählten Zeitraums zu einem Sammel-Markdown (Rohtexte, "
        "Einzeltranskripte bleiben erhalten).",
        "",
        f"- Zeitraum: {start.isoformat()} bis {end.isoformat()}",
        f"- Filter: {mode_text} `{pattern_text or '(kein Muster)'}`",
        f"- Erstellt: {date.today().isoformat()}",
        f"- Enthaltene Quellen: {len(candidates)}",
    ]
    if index_filename:
        head.append(f"- Quellenindex: {index_filename}")
    if any(c.conflict for c in candidates):
        head.append("- Hinweis: ⚠ markierte Quellen haben einen Datums-Konflikt "
                     "(siehe Quellenindex).")

    head += ["", "## Enthaltene Quellen", ""]
    for c in candidates:
        fdate = c.filter_date.isoformat() if c.filter_date else "ohne Datum"
        warn = " ⚠" if c.conflict else ""
        head.append(f"- {c.name} ({fdate}){warn}")

    head += ["", "---", "", "# Rohtexte", ""]

    parts: list[str] = ["\n".join(head)]
    for c in candidates:
        try:
            body = c.path.read_text(encoding="utf-8").strip()
        except OSError:
            body = "_(Datei konnte nicht gelesen werden)_"
        parts.append(f"## {c.name}\n\n{body}")

    return "\n\n---\n\n".join(parts) + "\n"


def write_consolidation(
    candidates: list[Candidate], output_dir: Path, label: str,
    start: date, end: date, mode_text: str, pattern_text: str,
    write_index: bool = True,
) -> tuple[Path, Path | None]:
    """
    Schreibt Konsolidat (und optional separaten Quellenindex) in
    ``output_dir``. Gibt (konsolidat_pfad, index_pfad|None) zurück.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    base = default_basename(label, start, end)

    index_path: Path | None = None
    index_name: str | None = None
    if write_index:
        index_name = f"Quellenindex_{base.split('_', 1)[1]}.md" \
            if "_" in base else f"Quellenindex_{base}.md"
        index_path = output_dir / index_name
        index_path.write_text(
            build_index_text(candidates, label, start, end, mode_text, pattern_text),
            encoding="utf-8",
        )

    cons_path = output_dir / f"{base}.md"
    cons_path.write_text(
        build_consolidation_text(
            candidates, label, start, end, mode_text, pattern_text,
            index_filename=index_name,
        ),
        encoding="utf-8",
    )
    return cons_path, index_path
