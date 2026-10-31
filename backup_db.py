import argparse
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent
DEFAULT_DATABASE = ROOT_DIR / "asf_study.sqlite3"
DEFAULT_BACKUP_DIRECTORY = Path.home() / "ASF-Backups"
BACKUP_FILENAME_PATTERN = re.compile(r"asf_database_(\d{8}T\d{6}\.\d{6}Z)\.sqlite3")


def create_database_backup(
    database_path: Path,
    backup_directory: Path,
) -> Path:
    source_path = database_path.expanduser().resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"Database file not found: {source_path}")

    backup_path = backup_directory.expanduser().resolve()
    backup_path.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    destination_path = backup_path / f"asf_database_{timestamp}.sqlite3"
    if source_path == destination_path:
        raise ValueError("Backup destination must be different from the source database.")

    source = sqlite3.connect(source_path, timeout=10)
    destination = sqlite3.connect(destination_path, timeout=10)
    try:
        source.backup(destination)
        integrity = destination.execute("PRAGMA integrity_check").fetchone()
        if not integrity or integrity[0] != "ok":
            raise sqlite3.DatabaseError("Database backup integrity check failed.")
    except Exception:
        destination.close()
        source.close()
        destination_path.unlink(missing_ok=True)
        raise
    else:
        destination.close()
        source.close()
    return destination_path


def prune_database_backups(
    backup_directory: Path,
    retention_days: int,
    now: datetime | None = None,
) -> list[Path]:
    if retention_days < 1:
        raise ValueError("Backup retention must be at least one day.")
    backup_path = backup_directory.expanduser().resolve()
    if not backup_path.is_dir():
        return []

    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        raise ValueError("The current time must include a timezone.")
    cutoff = current_time.astimezone(timezone.utc) - timedelta(days=retention_days)
    removed: list[Path] = []
    for candidate in backup_path.iterdir():
        match = BACKUP_FILENAME_PATTERN.fullmatch(candidate.name)
        if not candidate.is_file() or not match:
            continue
        created_at = datetime.strptime(match.group(1), "%Y%m%dT%H%M%S.%fZ").replace(
            tzinfo=timezone.utc
        )
        if created_at < cutoff:
            candidate.unlink()
            removed.append(candidate)
    return removed


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a consistent SQLite backup of ASF data.")
    parser.add_argument(
        "--database",
        type=Path,
        default=Path(os.getenv("STUDY_DB_PATH", str(DEFAULT_DATABASE))),
        help="SQLite database path (defaults to STUDY_DB_PATH or the app database).",
    )
    parser.add_argument(
        "--destination",
        type=Path,
        default=DEFAULT_BACKUP_DIRECTORY,
        help="Folder for the backup (defaults to ~/ASF-Backups).",
    )
    parser.add_argument(
        "--retention-days",
        type=int,
        help="After a successful backup, remove ASF backup files older than this many days.",
    )
    args = parser.parse_args()
    if args.retention_days is not None and args.retention_days < 1:
        parser.error("--retention-days must be at least one.")
    backup_path = create_database_backup(args.database, args.destination)
    print(f"Database backup created and integrity-checked: {backup_path}")
    if args.retention_days is not None:
        removed = prune_database_backups(args.destination, args.retention_days)
        print(f"Removed {len(removed)} backup file(s) older than {args.retention_days} days.")


if __name__ == "__main__":
    main()
