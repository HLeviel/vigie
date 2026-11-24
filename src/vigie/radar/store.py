"""Stockage SQLite de radar (``radar.db``) : offres, alias, runs, emails traités.

La synchronisation avec S3 (lot M2) viendra plus tard ; ce module ne gère que le fichier local.
"""

import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from vigie.match.schema import EMBEDDINGS_MIGRATION
from vigie.radar.models import Offer, SourceReport

DEFAULT_DB_PATH = Path.home() / ".local" / "share" / "vigie" / "radar.db"

# Migration n° i+1 : MIGRATIONS[i]. Versionnées par ``PRAGMA user_version``.
MIGRATIONS: list[str] = [
    """
    CREATE TABLE offers (
        uid TEXT PRIMARY KEY,
        source TEXT NOT NULL,
        source_id TEXT NOT NULL,
        title TEXT NOT NULL,
        company TEXT,
        location_label TEXT,
        postal_code TEXT,
        department TEXT,
        contract_type TEXT NOT NULL,
        remote TEXT NOT NULL,
        salary_min REAL,
        salary_max REAL,
        salary_label TEXT,
        description TEXT NOT NULL,
        url TEXT NOT NULL,
        published_at TEXT NOT NULL,
        fetched_at TEXT NOT NULL,
        dedup_key TEXT NOT NULL,
        suspicious INTEGER NOT NULL,
        raw TEXT NOT NULL,
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        filtered_reason TEXT,
        notified_at TEXT
    );
    CREATE INDEX idx_offers_dedup_key ON offers (dedup_key);
    CREATE INDEX idx_offers_pending ON offers (notified_at, filtered_reason);
    CREATE TABLE offer_aliases (
        uid TEXT PRIMARY KEY,
        canonical_uid TEXT NOT NULL
    );
    CREATE TABLE processed_emails (
        message_id TEXT PRIMARY KEY,
        sender_key TEXT NOT NULL,
        processed_at TEXT NOT NULL,
        offers_extracted INTEGER NOT NULL
    );
    CREATE TABLE runs (
        run_id TEXT PRIMARY KEY,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        status TEXT NOT NULL
    );
    CREATE TABLE run_sources (
        run_id TEXT NOT NULL,
        source TEXT NOT NULL,
        fetched INTEGER NOT NULL,
        new INTEGER NOT NULL,
        duplicates INTEGER NOT NULL,
        filtered INTEGER NOT NULL,
        errors INTEGER NOT NULL,
        duration_ms INTEGER NOT NULL,
        error_message TEXT,
        PRIMARY KEY (run_id, source)
    );
    """,
    EMBEDDINGS_MIGRATION,
]
SCHEMA_VERSION = len(MIGRATIONS)

_OFFER_COLUMNS = (
    "uid, source, source_id, title, company, location_label, postal_code, department, "
    "contract_type, remote, salary_min, salary_max, salary_label, description, url, "
    "published_at, fetched_at, dedup_key, suspicious, raw"
)


class StoreError(Exception):
    pass


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


class Store:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._conn.row_factory = sqlite3.Row
        self._migrate()

    # --- ouverture -------------------------------------------------------------------------

    @classmethod
    def open(cls, path: Path | str) -> Self:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        return cls(sqlite3.connect(str(path)))

    @classmethod
    def open_copy(cls, path: Path) -> Self:
        """Copie en mémoire (base vide si le fichier n'existe pas) ; rien n'est écrit sur disque."""
        memory = sqlite3.connect(":memory:")
        if path.exists():
            source = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                source.backup(memory)
            finally:
                source.close()
        return cls(memory)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._conn.commit()
        self._conn.close()

    def commit(self) -> None:
        self._conn.commit()

    def _migrate(self) -> None:
        version = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise StoreError("la base est plus récente que cette version de vigie")
        for index in range(version, SCHEMA_VERSION):
            self._conn.executescript(MIGRATIONS[index])
            self._conn.execute(f"PRAGMA user_version = {index + 1}")
        self._conn.commit()

    @property
    def connection(self) -> sqlite3.Connection:
        """Connexion partagée avec les tables des autres modules (``match``)."""
        return self._conn

    @property
    def version(self) -> int:
        return int(self._conn.execute("PRAGMA user_version").fetchone()[0])

    # --- offres ----------------------------------------------------------------------------

    def known(self, uid: str) -> bool:
        """Vrai si l'offre existe, comme offre ou comme alias."""
        row = self._conn.execute(
            "SELECT 1 FROM offers WHERE uid = ? UNION SELECT 1 FROM offer_aliases WHERE uid = ?",
            (uid, uid),
        ).fetchone()
        return row is not None

    def touch(self, uid: str, now: datetime) -> None:
        self._conn.execute("UPDATE offers SET last_seen_at = ? WHERE uid = ?", (_iso(now), uid))

    def insert_offer(self, offer: Offer, now: datetime, filtered_reason: str | None = None) -> None:
        data = offer.model_dump(mode="json")
        self._conn.execute(
            # Colonnes et nombre de « ? » sont des constantes du module, pas des entrées.
            f"""INSERT INTO offers ({_OFFER_COLUMNS}, first_seen_at, last_seen_at, filtered_reason)
            VALUES ({", ".join("?" * 23)})""",  # noqa: S608
            (
                data["uid"],
                data["source"],
                data["source_id"],
                data["title"],
                data["company"],
                data["location_label"],
                data["postal_code"],
                data["department"],
                data["contract_type"],
                data["remote"],
                data["salary_min"],
                data["salary_max"],
                data["salary_label"],
                data["description"],
                data["url"],
                _iso(offer.published_at),
                _iso(offer.fetched_at),
                data["dedup_key"],
                int(data["suspicious"]),
                json.dumps(data["raw"], ensure_ascii=False),
                _iso(now),
                _iso(now),
                filtered_reason,
            ),
        )

    def find_canonical(self, dedup_key: str, exclude_source: str) -> str | None:
        """Première offre d'une *autre* source ayant la même clé de dédoublonnage."""
        row = self._conn.execute(
            "SELECT uid FROM offers WHERE dedup_key = ? AND source != ? "
            "ORDER BY first_seen_at, uid LIMIT 1",
            (dedup_key, exclude_source),
        ).fetchone()
        return str(row["uid"]) if row else None

    def add_alias(self, uid: str, canonical_uid: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO offer_aliases (uid, canonical_uid) VALUES (?, ?)",
            (uid, canonical_uid),
        )

    def aliases_of(self, canonical_uid: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT uid FROM offer_aliases WHERE canonical_uid = ? ORDER BY uid", (canonical_uid,)
        ).fetchall()
        return [str(row["uid"]) for row in rows]

    def filtered_reason(self, uid: str) -> str | None:
        row = self._conn.execute(
            "SELECT filtered_reason FROM offers WHERE uid = ?", (uid,)
        ).fetchone()
        return str(row["filtered_reason"]) if row and row["filtered_reason"] else None

    def pending_offers(self, limit: int | None = None) -> list[Offer]:
        """Offres non filtrées et non notifiées, de la plus récente à la plus ancienne."""
        sql = (
            f"SELECT {_OFFER_COLUMNS} FROM offers "  # noqa: S608 (colonnes constantes)
            "WHERE filtered_reason IS NULL AND notified_at IS NULL "
            "ORDER BY published_at DESC, uid"
        )
        params: tuple[Any, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (limit,)
        return [self._offer_from_row(row) for row in self._conn.execute(sql, params)]

    def count_pending(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM offers WHERE filtered_reason IS NULL AND notified_at IS NULL"
        ).fetchone()
        return int(row[0])

    def mark_notified(self, uids: Iterable[str], now: datetime) -> None:
        self._conn.executemany(
            "UPDATE offers SET notified_at = ? WHERE uid = ?", [(_iso(now), uid) for uid in uids]
        )

    @staticmethod
    def _offer_from_row(row: sqlite3.Row) -> Offer:
        data = dict(row)
        data.pop("uid")
        data.pop("dedup_key")
        data.pop("suspicious")
        data["raw"] = json.loads(data["raw"])
        return Offer(**data)

    # --- emails traités ----------------------------------------------------------------------

    def email_processed(self, message_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM processed_emails WHERE message_id = ?", (message_id,)
        ).fetchone()
        return row is not None

    def mark_email_processed(
        self, message_id: str, sender_key: str, now: datetime, offers_extracted: int
    ) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO processed_emails "
            "(message_id, sender_key, processed_at, offers_extracted) VALUES (?, ?, ?, ?)",
            (message_id, sender_key, _iso(now), offers_extracted),
        )

    def count_processed_emails(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM processed_emails").fetchone()[0])

    # --- runs ------------------------------------------------------------------------------

    def start_run(self, run_id: str, started_at: datetime) -> None:
        self._conn.execute(
            "INSERT INTO runs (run_id, started_at, status) VALUES (?, ?, 'running')",
            (run_id, _iso(started_at)),
        )

    def finish_run(self, run_id: str, finished_at: datetime, status: str) -> None:
        self._conn.execute(
            "UPDATE runs SET finished_at = ?, status = ? WHERE run_id = ?",
            (_iso(finished_at), status, run_id),
        )

    def add_run_source(self, run_id: str, report: SourceReport) -> None:
        self._conn.execute(
            "INSERT INTO run_sources (run_id, source, fetched, new, duplicates, filtered, errors, "
            "duration_ms, error_message) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                report.source,
                report.fetched,
                report.new,
                report.duplicates,
                report.filtered,
                report.errors,
                report.duration_ms,
                report.error_message,
            ),
        )

    def last_success_started_at(self) -> datetime | None:
        row = self._conn.execute(
            "SELECT started_at FROM runs WHERE status = 'success' ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        return datetime.fromisoformat(row["started_at"]) if row else None

    def recent_runs(self, limit: int = 5) -> list[dict[str, Any]]:
        runs = self._conn.execute(
            "SELECT run_id, started_at, finished_at, status FROM runs "
            "ORDER BY started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for run in runs:
            sources = self._conn.execute(
                "SELECT source, fetched, new, duplicates, filtered, errors, duration_ms, "
                "error_message FROM run_sources WHERE run_id = ? ORDER BY source",
                (run["run_id"],),
            ).fetchall()
            result.append({**dict(run), "sources": [dict(s) for s in sources]})
        return result
