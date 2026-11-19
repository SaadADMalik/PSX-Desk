"""Evidence: a fact computed by code, with provenance.

The anti-hallucination rule of the whole system: models never produce numbers.
Code computes them, stores them as Evidence, and models may only *cite* an
evidence id. A verifier can then check every cited figure mechanically.
"""

import hashlib
import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

EvidenceKind = Literal["price_summary", "valuation", "filing", "news"]


class Evidence(BaseModel):
    schema_version: Literal["evidence.v1"] = "evidence.v1"
    id: str = ""
    symbol: str
    kind: EvidenceKind
    values: dict[str, Any]
    source: str = Field(description="Where the raw data came from")
    as_of: str = Field(description="Market date the values describe (YYYY-MM-DD)")
    fetched_at: datetime
    stale: bool = Field(default=False, description="True if served from the last-good snapshot")

    def model_post_init(self, _context: Any) -> None:
        if not self.id:
            self.id = self.content_id()

    def prompt_view(self) -> dict[str, Any]:
        """What a model sees. Excludes fetch metadata: a timestamp that changes every run
        would change the prompt, and so the cache key, even when the facts are identical."""
        return self.model_dump(mode="json", exclude={"fetched_at", "schema_version"})

    def content_id(self) -> str:
        """Same facts -> same id. Makes evidence idempotent to store and to cite."""
        payload = json.dumps(
            {"symbol": self.symbol, "kind": self.kind, "as_of": self.as_of, "values": self.values},
            sort_keys=True,
            default=str,
        )
        return "ev_" + hashlib.sha256(payload.encode()).hexdigest()[:12]
