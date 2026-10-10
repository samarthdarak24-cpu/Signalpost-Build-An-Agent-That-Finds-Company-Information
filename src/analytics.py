"""Phase 2 — instant analytics over the envelope + change logs (DuckDB).

DuckDB is an embedded, serverless columnar engine. It reads the JSONL output
directly (no ETL, no server) and answers SQL in milliseconds, which is what the
dashboard's aggregate views — coverage, status mix, field frequency — want
without loading 1000 envelopes into Python on every request.

If DuckDB is unavailable the functions fall back to a small pure-Python reader so
the analytics endpoints never hard-fail.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

ENVELOPES_PATH = ROOT / "runs" / "envelopes.jsonl"
CHANGES_PATH = ROOT / "runs" / "changes.jsonl"

_DUCKDB_OK: bool | None = None


def _duckdb_ok() -> bool:
    global _DUCKDB_OK
    if _DUCKDB_OK is None:
        try:
            import duckdb  # noqa: F401

            _DUCKDB_OK = True
        except Exception:
            _DUCKDB_OK = False
    return _DUCKDB_OK


def status_counts(path: Path = ENVELOPES_PATH) -> dict[str, int]:
    """How many envelopes sit in each of the six result states."""
    if _duckdb_ok() and path.exists():
        import duckdb

        rows = duckdb.execute(
            "SELECT status, count(*) FROM read_json(?, format='newline_delimited') "
            "GROUP BY status",
            [str(path)],
        ).fetchall()
        return {r[0]: int(r[1]) for r in rows}
    out: dict[str, int] = {}
    for env in _iter_jsonl(path):
        out[env.get("status", "?")] = out.get(env.get("status", "?"), 0) + 1
    return out


def fact_stats(path: Path = ENVELOPES_PATH) -> dict[str, float]:
    """Totals and per-envelope fact-count distribution."""
    if _duckdb_ok() and path.exists():
        import duckdb

        row = duckdb.execute(
            "SELECT count(*), sum(fact_count), avg(fact_count), max(fact_count), "
            "min(fact_count) FROM read_json(?, format='newline_delimited')",
            [str(path)],
        ).fetchone()
        return {
            "envelopes": int(row[0] or 0),
            "total_facts": int(row[1] or 0),
            "avg_facts": round(float(row[2] or 0), 2),
            "max_facts": int(row[3] or 0),
            "min_facts": int(row[4] or 0),
        }
    counts = [len(e.get("facts", [])) for e in _iter_jsonl(path)]
    return {
        "envelopes": len(counts),
        "total_facts": sum(counts),
        "avg_facts": round(sum(counts) / len(counts), 2) if counts else 0,
        "max_facts": max(counts) if counts else 0,
        "min_facts": min(counts) if counts else 0,
    }


def field_frequency(path: Path = ENVELOPES_PATH, limit: int = 30) -> list[tuple[str, int]]:
    """Which fields the agent actually publishes, ranked by how often.

    The honest-coverage view for the recall category: a field that almost no
    envelope carries is a coverage gap worth a new source, not a bug to hide.
    """
    if _duckdb_ok() and path.exists():
        import duckdb

        try:
            rows = duckdb.execute(
                "SELECT f.value->>'field' AS field, count(*) AS n "
                "FROM read_json(?, format='newline_delimited') t, "
                "UNNEST(t.facts) AS f "
                "WHERE f.value IS NOT NULL "
                "GROUP BY 1 ORDER BY 2 DESC LIMIT ?",
                [str(path), limit],
            ).fetchall()
            return [(r[0], int(r[1])) for r in rows]
        except Exception:
            pass  # fall through to the Python reader below
    freq: dict[str, int] = {}
    for env in _iter_jsonl(path):
        for fact in env.get("facts", []):
            field = fact.get("field")
            if field:
                freq[field] = freq.get(field, 0) + 1
    return sorted(freq.items(), key=lambda kv: kv[1], reverse=True)[:limit]


def recent_changes(path: Path = CHANGES_PATH, limit: int = 50) -> list[dict[str, Any]]:
    """Most recent change records (newest first)."""
    rows = list(_iter_jsonl(path))
    rows.reverse()
    return rows[:limit]


def coverage_by_field(path: Path = ENVELOPES_PATH) -> dict[str, float]:
    """Share of available envelopes that carry each field — the recall heatmap."""
    envelopes = [e for e in _iter_jsonl(path) if e.get("status") == "available"]
    if not envelopes:
        return {}
    total = len(envelopes)
    freq: dict[str, int] = {}
    for env in envelopes:
        for fact in env.get("facts", []):
            field = fact.get("field")
            if field:
                freq[field] = freq.get(field, 0) + 1
    return {f: round(n / total, 3) for f, n in sorted(freq.items(), key=lambda kv: kv[1], reverse=True)}


def _iter_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def analytics_bundle() -> dict[str, Any]:
    """Everything the dashboard's analytics pane needs, in one call."""
    return {
        "status_counts": status_counts(),
        "fact_stats": fact_stats(),
        "field_frequency": field_frequency(),
        "coverage_by_field": coverage_by_field(),
        "recent_changes": recent_changes(),
        "engine": "duckdb" if _duckdb_ok() else "python",
    }
