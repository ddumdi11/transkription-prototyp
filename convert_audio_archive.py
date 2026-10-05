#!/usr/bin/env python3
"""Explicitly create and verify one local lossless FLAC archive package."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from math import isfinite
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile
from typing import Any, BinaryIO, Iterator

from plan_audio_archive import (
    DEFAULT_STATE,
    build_plan,
    open_readonly_state,
    setup_cli_logging,
)


DEFAULT_OUTPUT = Path("staging/audio-archive")
STARTUP_TIMEOUT_SECONDS = 30.0
REALTIME_TIMEOUT_FACTOR = 2.0
SHA256_PATTERN = re.compile(r"^SHA256=([0-9a-fA-F]{64})$", re.MULTILINE)


def file_digest(path: Path, algorithm: str) -> str:
    try:
        digest = hashlib.new(algorithm)
    except ValueError as exc:
        raise ValueError(f"Nicht unterstützter Quellhash: {algorithm}") from exc
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().lower()


def archive_key(item: dict[str, Any]) -> str:
    material = {
        "drive_id": item["drive_id"],
        "source_hash_type": item["source_hash_type"],
        "source_hash": item["source_hash"],
    }
    encoded = json.dumps(
        material, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_conversion_plan(
    item: dict[str, Any], output_root: Path
) -> dict[str, Any]:
    if item["archive_status"] != "CANDIDATE":
        blockers = ", ".join(item["archive_blockers"]) or "unbekannt"
        raise ValueError(f"Aufnahme ist kein Archivkandidat: {blockers}")
    key = archive_key(item)
    package_name = Path(item["archive_name"]).stem
    package_dir = output_root / package_name
    return {
        "archive_key": key,
        "drive_id": item["drive_id"],
        "source_path": item["source_path"],
        "source_audio": item["local_audio"],
        "source_size": item["source_size"],
        "source_hash_type": item["source_hash_type"],
        "source_hash": item["source_hash"],
        "archive_name": item["archive_name"],
        "package_dir": str(package_dir),
        "archive_file": str(package_dir / item["archive_name"]),
        "metadata_file": str(package_dir / "archive.json"),
        "quality_status": item["quality"]["status"],
        "cleanup_ready": False,
    }


def select_conversion_plan(
    state: Path, drive_id: str, output_root: Path
) -> dict[str, Any]:
    with open_readonly_state(state) as db:
        items = build_plan(db, selected_ids={drive_id})
    return build_conversion_plan(items[0], output_root)


def operation_timeout(
    duration: float, configured_timeout: float | None = None
) -> float:
    if configured_timeout is not None:
        if not isfinite(configured_timeout) or configured_timeout <= 0:
            raise ValueError("Der FFmpeg-Timeout muss eine positive Zahl sein")
        return configured_timeout
    if not isfinite(duration) or duration < 0:
        raise ValueError("Audiodauer ist ungültig")
    return STARTUP_TIMEOUT_SECONDS + duration * REALTIME_TIMEOUT_FACTOR


def _run(command: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        message = f": {detail}" if detail else ""
        raise RuntimeError(f"{command[0]} fehlgeschlagen{message}") from exc


def probe_audio(path: Path, timeout: float) -> dict[str, Any]:
    result = _run([
        "ffprobe", "-v", "error", "-select_streams", "a",
        "-show_entries",
        "stream=codec_name,sample_rate,channels,channel_layout,duration:format=duration",
        "-of", "json", str(path),
    ], timeout)
    try:
        payload = json.loads(result.stdout)
        streams = payload["streams"]
        if not isinstance(streams, list) or not streams:
            raise ValueError("Audiospur fehlt")
        stream = streams[0]
        sample_rate = int(stream["sample_rate"])
        channels = int(stream["channels"])
        duration = None
        for duration_value in (
            stream.get("duration"), payload.get("format", {}).get("duration")
        ):
            try:
                candidate = float(duration_value)
            except (TypeError, ValueError):
                continue
            if isfinite(candidate) and candidate >= 0:
                duration = candidate
                break
        if duration is None:
            raise ValueError("Audiodauer fehlt")
        codec = str(stream["codec_name"])
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"Ungültige ffprobe-Audiodaten für {path}") from exc
    if sample_rate <= 0 or channels <= 0 or not isfinite(duration) or duration < 0:
        raise ValueError(f"Ungültige Audioeigenschaften für {path}")
    return {
        "audio_stream_count": len(streams),
        "codec": codec,
        "sample_rate": sample_rate,
        "channels": channels,
        "channel_layout": stream.get("channel_layout"),
        "duration": duration,
    }


def decoded_pcm_sha256(path: Path, timeout: float) -> str:
    """Fully decode audio into one canonical PCM representation and hash it."""
    result = _run([
        "ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a:0",
        "-vn", "-sn", "-dn", "-c:a", "pcm_s32le",
        "-f", "hash", "-hash", "sha256", "-",
    ], timeout)
    match = SHA256_PATTERN.search(result.stdout)
    if match is None:
        raise ValueError(f"FFmpeg lieferte keinen PCM-SHA256 für {path}")
    return match.group(1).lower()


def validate_media_match(
    source: dict[str, Any], archive: dict[str, Any], source_pcm: str, archive_pcm: str
) -> dict[str, Any]:
    if source["audio_stream_count"] != 1:
        raise ValueError(
            f"WAV enthält {source['audio_stream_count']} Audiospuren; genau eine erforderlich"
        )
    if archive["audio_stream_count"] != 1:
        raise ValueError(
            f"FLAC enthält {archive['audio_stream_count']} Audiospuren; genau eine erforderlich"
        )
    if archive["codec"] != "flac":
        raise ValueError(f"Archivcodec ist nicht FLAC: {archive['codec']}")
    if source["sample_rate"] != archive["sample_rate"]:
        raise ValueError("Abtastrate des FLAC weicht vom WAV ab")
    if source["channels"] != archive["channels"]:
        raise ValueError("Kanalzahl des FLAC weicht vom WAV ab")
    tolerance = max(0.01, 2.0 / source["sample_rate"])
    duration_delta = abs(source["duration"] - archive["duration"])
    if duration_delta > tolerance:
        raise ValueError(
            f"FLAC-Dauer weicht um {duration_delta:.6f} Sekunden vom WAV ab"
        )
    if source_pcm != archive_pcm:
        raise ValueError("Dekodierter PCM-Inhalt des FLAC weicht vom WAV ab")
    return {
        "full_decode": True,
        "decoded_pcm_sha256": source_pcm,
        "sample_rate_match": True,
        "channels_match": True,
        "duration_match": True,
        "duration_delta_seconds": round(duration_delta, 9),
        "duration_tolerance_seconds": round(tolerance, 9),
    }


def _lock_file(handle: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _unlock_file(handle: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def archive_lock(package_dir: Path) -> Iterator[None]:
    lock_path = package_dir.parent / f".{package_dir.name}.lock"
    with lock_path.open("a+b") as handle:
        _lock_file(handle)
        try:
            yield
        finally:
            _unlock_file(handle)


def fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def existing_package_matches(plan: dict[str, Any]) -> bool:
    package_dir = Path(plan["package_dir"])
    archive_file = Path(plan["archive_file"])
    metadata_file = Path(plan["metadata_file"])
    if not package_dir.is_dir() or not archive_file.is_file() or not metadata_file.is_file():
        return False
    try:
        payload = json.loads(metadata_file.read_text(encoding="utf-8"))
        source = payload.get("source") if isinstance(payload, dict) else None
        archive = payload.get("archive") if isinstance(payload, dict) else None
        verification = payload.get("verification") if isinstance(payload, dict) else None
        return (
            isinstance(payload, dict)
            and payload.get("schema_version") == 1
            and payload.get("archive_type") == "local_lossless_flac"
            and payload.get("archive_key") == plan["archive_key"]
            and payload.get("confirmation") == "explicit_cli"
            and payload.get("cleanup_ready") is False
            and isinstance(source, dict)
            and source.get("drive_id") == plan["drive_id"]
            and source.get("size") == plan["source_size"]
            and source.get("hash_type") == plan["source_hash_type"]
            and source.get("content_hash") == plan["source_hash"]
            and isinstance(archive, dict)
            and archive.get("file") == archive_file.name
            and archive.get("codec") == "flac"
            and archive.get("size") == archive_file.stat().st_size
            and archive.get("sha256") == file_digest(archive_file, "sha256")
            and isinstance(verification, dict)
            and verification.get("full_decode") is True
            and verification.get("sample_rate_match") is True
            and verification.get("channels_match") is True
            and verification.get("duration_match") is True
            and isinstance(verification.get("decoded_pcm_sha256"), str)
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False


def convert_one(
    plan: dict[str, Any], timeout_seconds: float | None = None
) -> tuple[Path, Path, bool]:
    """Create one atomic FLAC package; never change or remove the source WAV."""
    source_audio = Path(plan["source_audio"])
    output_root = Path(plan["package_dir"]).parent
    package_dir = Path(plan["package_dir"])
    archive_file = Path(plan["archive_file"])
    metadata_file = Path(plan["metadata_file"])

    if not source_audio.is_file():
        raise ValueError(f"Lokales WAV fehlt: {source_audio}")
    if source_audio.stat().st_size != plan["source_size"]:
        raise ValueError("Lokale WAV-Größe hat sich seit dem Dry-Run geändert")
    actual_source_hash = file_digest(source_audio, plan["source_hash_type"])
    if actual_source_hash != str(plan["source_hash"]).lower():
        raise ValueError("Lokaler WAV-Hash stimmt nicht mit dem Drive-State überein")

    output_root.mkdir(parents=True, exist_ok=True)
    with archive_lock(package_dir):
        if package_dir.exists():
            if existing_package_matches(plan):
                return archive_file, metadata_file, False
            raise FileExistsError(
                f"Archivpaket existiert bereits mit abweichendem Inhalt: {package_dir}"
            )

        temporary_dir = Path(tempfile.mkdtemp(
            prefix=f".{package_dir.name}.", suffix=".tmp", dir=output_root
        ))
        temporary_audio = temporary_dir / archive_file.name
        temporary_metadata = temporary_dir / metadata_file.name
        installed = False
        try:
            probe_timeout = operation_timeout(0.0, timeout_seconds)
            source_probe = probe_audio(source_audio, probe_timeout)
            if source_probe["audio_stream_count"] != 1:
                raise ValueError(
                    f"WAV enthält {source_probe['audio_stream_count']} Audiospuren; "
                    "genau eine erforderlich"
                )
            timeout = operation_timeout(source_probe["duration"], timeout_seconds)
            _run([
                "ffmpeg", "-v", "error", "-n", "-i", str(source_audio),
                "-map", "0:a:0", "-vn", "-sn", "-dn",
                "-c:a", "flac", "-compression_level", "8",
                str(temporary_audio),
            ], timeout)
            if not temporary_audio.is_file() or temporary_audio.stat().st_size == 0:
                raise RuntimeError("FFmpeg hat keine verwendbare FLAC-Datei erzeugt")

            archive_probe = probe_audio(temporary_audio, timeout)
            source_pcm = decoded_pcm_sha256(source_audio, timeout)
            archive_pcm = decoded_pcm_sha256(temporary_audio, timeout)
            verification = validate_media_match(
                source_probe, archive_probe, source_pcm, archive_pcm
            )
            archive_hash = file_digest(temporary_audio, "sha256")
            archive_size = temporary_audio.stat().st_size
            payload = {
                "schema_version": 1,
                "archive_type": "local_lossless_flac",
                "archive_key": plan["archive_key"],
                "created_at": datetime.now(timezone.utc).isoformat(),
                "confirmation": "explicit_cli",
                "source": {
                    "drive_id": plan["drive_id"],
                    "path": plan["source_path"],
                    "local_file": str(source_audio),
                    "size": plan["source_size"],
                    "hash_type": plan["source_hash_type"],
                    "content_hash": actual_source_hash,
                    "media": source_probe,
                },
                "archive": {
                    "file": archive_file.name,
                    "size": archive_size,
                    "sha256": archive_hash,
                    "codec": "flac",
                    "compression_level": 8,
                    "media": archive_probe,
                    "size_ratio": round(archive_size / plan["source_size"], 6),
                    "saved_bytes": plan["source_size"] - archive_size,
                },
                "verification": verification,
                "quality_status_at_conversion": plan["quality_status"],
                "cleanup_ready": False,
            }
            with temporary_metadata.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            with temporary_audio.open("rb") as handle:
                os.fsync(handle.fileno())
            temporary_dir.replace(package_dir)
            installed = True
            fsync_directory(output_root)
        except Exception:
            shutil.rmtree(temporary_dir, ignore_errors=True)
            if installed:
                shutil.rmtree(package_dir, ignore_errors=True)
                try:
                    fsync_directory(output_root)
                except OSError:
                    pass
            raise
    return archive_file, metadata_file, True


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ein lokales WAV ausdrücklich und verifiziert als FLAC archivieren"
    )
    parser.add_argument("--drive-id", required=True)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--timeout-seconds", type=float)
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logger = setup_cli_logging()
    try:
        plan = select_conversion_plan(args.state, args.drive_id, args.output_dir)
        if args.json and not args.confirm:
            print(json.dumps({"dry_run": True, "plan": plan}, ensure_ascii=False, indent=2))
        elif not args.confirm:
            logger.info(
                "FLAC-Dry-Run id=%s source=%r target=%r qa=%s",
                plan["drive_id"], plan["source_audio"], plan["archive_file"],
                plan["quality_status"],
            )
            logger.info("Keine Datei geschrieben. Mit --confirm ausdrücklich bestätigen.")
        else:
            audio, metadata, created = convert_one(plan, args.timeout_seconds)
            result = {
                "dry_run": False,
                "created": created,
                "archive_file": str(audio),
                "metadata_file": str(metadata),
                "source_preserved": True,
                "cleanup_ready": False,
            }
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                logger.info(
                    "FLAC %s id=%s file=%r metadata=%r WAV=unverändert",
                    "erstellt" if created else "bereits verifiziert vorhanden",
                    plan["drive_id"], str(audio), str(metadata),
                )
        return 0
    except (
        FileNotFoundError,
        OSError,
        RuntimeError,
        sqlite3.Error,
        subprocess.TimeoutExpired,
        ValueError,
    ) as exc:
        logger.error("Lokale FLAC-Archivierung fehlgeschlagen: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
