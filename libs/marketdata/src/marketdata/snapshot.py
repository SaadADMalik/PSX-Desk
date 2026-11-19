"""Last-good snapshot: the fallback when the live source fails.

Degraded-but-available beats unavailable, *as long as the consumer knows*. Every
value served from here is marked stale, and that flag travels inside the Evidence
all the way into the brief.

Slice 0 stores snapshots as files. Slice 1 moves them behind the market-data
MCP server, and later into Postgres.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd


class SnapshotStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, symbol: str, kind: str) -> Path:
        return self.root / f"{symbol}.{kind}.json"

    def save_history(self, symbol: str, df: pd.DataFrame) -> None:
        payload = {
            "saved_at": datetime.now(UTC).isoformat(),
            "rows": json.loads(df.to_json(orient="records", date_format="iso")),
        }
        self._atomic_write(self._path(symbol, "history"), payload)

    def load_history(self, symbol: str) -> tuple[pd.DataFrame, datetime] | None:
        p = self._path(symbol, "history")
        if not p.exists():
            return None
        payload = json.loads(p.read_text())
        df = pd.DataFrame(payload["rows"])
        df["date"] = pd.to_datetime(df["date"])
        return df, datetime.fromisoformat(payload["saved_at"])

    def save_quote(self, symbol: str, quote: dict) -> None:
        self._atomic_write(
            self._path(symbol, "quote"),
            {"saved_at": datetime.now(UTC).isoformat(), "quote": quote},
        )

    def load_quote(self, symbol: str) -> tuple[dict, datetime] | None:
        p = self._path(symbol, "quote")
        if not p.exists():
            return None
        payload = json.loads(p.read_text())
        return payload["quote"], datetime.fromisoformat(payload["saved_at"])

    def save_json(self, symbol: str, kind: str, data: object) -> None:
        self._atomic_write(self._path(symbol, kind),
                           {"saved_at": datetime.now(UTC).isoformat(), "data": data})

    def load_json(self, symbol: str, kind: str) -> tuple[object, datetime] | None:
        p = self._path(symbol, kind)
        if not p.exists():
            return None
        payload = json.loads(p.read_text(encoding="utf-8"))
        return payload["data"], datetime.fromisoformat(payload["saved_at"])

    @staticmethod
    def _atomic_write(path: Path, payload: dict) -> None:
        # Write-then-rename: a crash mid-write never leaves a half-written snapshot.
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, default=str))
        tmp.replace(path)
