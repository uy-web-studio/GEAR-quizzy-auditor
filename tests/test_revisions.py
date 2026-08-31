"""Unit tests for daily_audit_pipeline.revisions."""

from unittest.mock import MagicMock

from daily_audit_pipeline.revisions import log_revision


class TestLogRevision:
  def test_writes_expected_fields_to_revisions_collection(self):
    mock_db = MagicMock()

    log_revision(
        mock_db,
        revision_type="rule_change",
        actor="donovanuy@gmail.com",
        target={"scope": "auditor_rules"},
        before={"instruction": "old text"},
        after={"instruction": "new text"},
    )

    mock_db.collection.assert_called_once_with("revisions")
    added_doc = mock_db.collection.return_value.add.call_args.args[0]
    assert added_doc["type"] == "rule_change"
    assert added_doc["actor"] == "donovanuy@gmail.com"
    assert added_doc["target"] == {"scope": "auditor_rules"}
    assert added_doc["before"] == {"instruction": "old text"}
    assert added_doc["after"] == {"instruction": "new text"}
    assert "timestamp" in added_doc
