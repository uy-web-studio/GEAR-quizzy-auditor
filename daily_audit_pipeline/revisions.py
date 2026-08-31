"""Revision history logging for admin edits (rule changes, and — in Phase
2 — manual overrides, raw quiz content edits, retroactive re-audits). See
docs/superpowers/specs/2026-08-30-admin-dashboard-design.md §9.
"""

from datetime import datetime
from typing import Any


def log_revision(
    db,
    revision_type: str,
    actor: str,
    target: dict,
    before: Any,
    after: Any,
) -> None:
  """Append one entry to the `revisions` collection.

  Called as the last step of every admin write path.
  """
  db.collection("revisions").add({
      "timestamp": datetime.now().isoformat() + "Z",
      "type": revision_type,
      "actor": actor,
      "target": target,
      "before": before,
      "after": after,
  })
