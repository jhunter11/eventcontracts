"""DuckDB read path over Parquet partitions.

DuckDB is a read-only query layer: writes go through
:class:`ParquetEventStore`. This class exposes the partition tree as
SQL views (``raw_events`` and ``normalized_events``) and a handful of
convenience methods for common queries.

Use this for ad-hoc analytics, parity case generation, or to drive a
DuckDB-backed feature builder. Production writers stay on Parquet so
the seam between Python and Rust remains plain files.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import duckdb


class DuckDbEventStore:
    """Query a :class:`ParquetEventStore` directory tree via DuckDB.

    Read-only. Construct with the same ``root`` you handed to the
    writer. The first query lazily creates ``raw_events`` and
    ``normalized_events`` views with Hive-style partition discovery on.
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self._conn = duckdb.connect()
        self._views_created = False

    def _has_parquet(self, subdir: str) -> bool:
        return any((self.root / subdir).rglob("*.parquet")) if (self.root / subdir).exists() else False

    def _ensure_views(self) -> None:
        if self._views_created:
            return
        raw_glob = (self.root / "raw" / "**" / "*.parquet").as_posix()
        norm_glob = (self.root / "normalized" / "**" / "*.parquet").as_posix()
        if self._has_parquet("raw"):
            self._conn.execute(
                f"""
                CREATE OR REPLACE VIEW raw_events AS
                SELECT *
                FROM read_parquet('{raw_glob}', hive_partitioning=1, union_by_name=true)
                """
            )
        else:
            # Empty placeholder view so queries return zero rows cleanly.
            self._conn.execute(
                "CREATE OR REPLACE VIEW raw_events AS "
                "SELECT NULL::VARCHAR AS venue, NULL::VARCHAR AS source, NULL::VARCHAR AS channel, "
                "NULL::TIMESTAMP AS received_at, NULL::TIMESTAMP AS exchange_ts, "
                "NULL::VARCHAR AS payload_json, NULL::VARCHAR AS schema_version, "
                "NULL::VARCHAR AS metadata_json WHERE FALSE"
            )
        if self._has_parquet("normalized"):
            self._conn.execute(
                f"""
                CREATE OR REPLACE VIEW normalized_events AS
                SELECT *
                FROM read_parquet('{norm_glob}', hive_partitioning=1, union_by_name=true)
                """
            )
        else:
            self._conn.execute(
                "CREATE OR REPLACE VIEW normalized_events AS "
                "SELECT NULL::VARCHAR AS kind, NULL::VARCHAR AS event_id, "
                "NULL::TIMESTAMP AS exchange_ts, NULL::TIMESTAMP AS received_at, "
                "NULL::VARCHAR AS source, NULL::VARCHAR AS channel, "
                "NULL::VARCHAR AS payload_json WHERE FALSE"
            )
        self._views_created = True

    def query(self, sql: str, *args: Any) -> list[tuple[Any, ...]]:
        """Run an ad-hoc SQL query. Returns rows as tuples."""

        self._ensure_views()
        rows: list[tuple[Any, ...]] = self._conn.execute(sql, list(args)).fetchall()
        return rows

    def raw_count(self) -> int:
        rows = self.query("SELECT COUNT(*) FROM raw_events")
        return int(rows[0][0]) if rows else 0

    def normalized_count(self) -> int:
        rows = self.query("SELECT COUNT(*) FROM normalized_events")
        return int(rows[0][0]) if rows else 0

    def kinds_present(self) -> list[str]:
        rows = self.query("SELECT DISTINCT kind FROM normalized_events ORDER BY kind")
        return [str(r[0]) for r in rows]

    def trades(
        self,
        *,
        market_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return parsed trade rows from normalized events."""

        self._ensure_views()
        clauses = ["kind = 'trade'"]
        params: list[Any] = []
        if market_id is not None:
            # json.dumps with sort_keys uses default separators (', ', ': ')
            # so the marker pattern includes the space after the colon.
            clauses.append("payload_json LIKE ?")
            params.append(f'%"market_id": "{market_id}"%')
        where = " AND ".join(clauses)
        sql = f"""
        SELECT event_id,
               CAST(exchange_ts AS VARCHAR) AS exchange_ts,
               CAST(received_at AS VARCHAR) AS received_at,
               payload_json
        FROM normalized_events
        WHERE {where}
        ORDER BY exchange_ts, received_at, event_id
        """
        result = self._conn.execute(sql, params).fetchall()
        out = []
        for event_id, exchange_ts, received_at, payload_json in result:
            out.append(
                {
                    "event_id": event_id,
                    "exchange_ts": exchange_ts,
                    "received_at": received_at,
                    "payload": json.loads(payload_json),
                }
            )
        return out

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> DuckDbEventStore:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()
