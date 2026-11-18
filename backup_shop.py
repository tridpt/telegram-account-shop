"""Create and restore-test a consistent SQLite backup of the running shop."""

from contextlib import closing
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import logging
import sqlite3

from cryptography.fernet import Fernet

from admin_alerts import send_admin_message
from config import ROOT, backup_directory, load_config


def inspect_database(path: Path, encryption_key: str) -> dict[str, int]:
    with closing(sqlite3.connect(path)) as conn:
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise RuntimeError(f"SQLite integrity check failed: {result}")
        tables = [
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        counts = {}
        for table in tables:
            escaped = table.replace('"', '""')
            counts[table] = conn.execute(f'SELECT COUNT(*) FROM "{escaped}"').fetchone()[0]
        if "stock" in tables:
            row = conn.execute("SELECT secret_ciphertext FROM stock LIMIT 1").fetchone()
            if row:
                Fernet(encryption_key.encode("ascii")).decrypt(row[0].encode("ascii"))
        return counts


def backup_and_verify(database_path: Path, backup_dir: Path, encryption_key: str) -> Path:
    database_path = Path(database_path)
    backup_dir = Path(backup_dir)
    if not database_path.is_file():
        raise FileNotFoundError(f"Shop database does not exist: {database_path}")
    backup_dir.mkdir(parents=True, exist_ok=True)
    name = "shop-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".sqlite3"
    destination = backup_dir / name
    if destination.exists():
        raise FileExistsError(destination)

    with TemporaryDirectory(prefix="backup-check-", dir=backup_dir) as temp:
        snapshot = Path(temp) / "snapshot.sqlite3"
        restored = Path(temp) / "restored.sqlite3"
        with closing(sqlite3.connect(database_path, timeout=30)) as source, closing(
            sqlite3.connect(snapshot, timeout=30)
        ) as target:
            source.execute("PRAGMA query_only=ON")
            source.backup(target, pages=1000, sleep=0.25)

        source_counts = inspect_database(snapshot, encryption_key)
        with closing(sqlite3.connect(snapshot, timeout=30)) as source, closing(
            sqlite3.connect(restored, timeout=30)
        ) as target:
            source.backup(target, pages=1000, sleep=0.25)
        if inspect_database(restored, encryption_key) != source_counts:
            raise RuntimeError("Restored database row counts do not match backup")

        snapshot.rename(destination)
    return destination


def alert_backup_failure(error_type: str) -> None:
    text = (
        f"Canh bao: sao luu shop that bai ({error_type}). "
        "Kiem tra .runtime/backup.log va chay lai backup_shop.py."
    )
    send_admin_message(text, root=ROOT)


def main() -> None:
    try:
        log_dir = ROOT / ".runtime"
        log_dir.mkdir(exist_ok=True)
        logging.basicConfig(
            filename=log_dir / "backup.log",
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(message)s",
        )
        config = load_config()
        result = backup_and_verify(
            config.database_path, backup_directory(), config.inventory_encryption_key,
        )
        logging.info("Backup and restore test passed: %s", result)
        print(f"Backup and restore test passed: {result}")
    except Exception as exc:
        logging.exception("Backup and restore test failed")
        try:
            alert_backup_failure(type(exc).__name__)
        except Exception as alert_exc:
            logging.warning("Could not prepare backup alert: %s", type(alert_exc).__name__)
        raise


if __name__ == "__main__":
    main()
