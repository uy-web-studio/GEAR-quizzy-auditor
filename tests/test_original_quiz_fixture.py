import json
import pytest
from pathlib import Path

from daily_audit_pipeline.schemas import QuestionAudit


FIXTURE_PATH = Path(__file__).parent / "original_quiz.json"


def _load_fixture():
  with open(FIXTURE_PATH) as f:
    return json.load(f)


class TestOriginalQuizFixture:
  """Test that the disclosed original_quiz.json fixture is loadable and shaped correctly."""

  def test_fixture_exists(self):
    assert FIXTURE_PATH.exists(), f"Fixture missing: {FIXTURE_PATH}"

  def test_fixture_shape(self):
    data = _load_fixture()
    assert "status" in data
    assert "data" in data
    assert "quizDate" in data["data"]
    assert "quiz" in data["data"]
    assert isinstance(data["data"]["quiz"], list)
    assert len(data["data"]["quiz"]) > 0

  def test_fixture_questions_have_required_fields(self):
    data = _load_fixture()
    for q in data["data"]["quiz"]:
      assert "question" in q
      assert "choices" in q
      assert "answer" in q
      assert "source" in q
      assert "url" in q["source"]

  def test_fixture_rule_1_phrasing_violations(self):
    """Rule 1: meta-referential phrasing — flagged in the fixture."""
    data = _load_fixture()
    violations = []
    banned = ["according to", "a news article says", "as mentioned in"]
    for q in data["data"]["quiz"]:
      text = q["question"].lower()
      for phrase in banned:
        if phrase in text:
          violations.append(q["question"][:60])
    # At least one violation expected — the Vrabel question
    assert len(violations) >= 1, f"Expected at least one Rule 1 violation, got {violations}"

  def test_fixture_rule_2_answer_choice_integrity(self):
    """Rule 2: answer must exactly match a choice."""
    data = _load_fixture()
    mismatches = []
    for q in data["data"]["quiz"]:
      if q["answer"] not in q["choices"]:
        mismatches.append((q["question"][:40], q["answer"], q["choices"]))
    assert len(mismatches) == 0, f"Answer/choice mismatches: {mismatches}"

  def test_fixture_rule_3_choice_count_bounds(self):
    """Rule 3: 3-5 choices required."""
    data = _load_fixture()
    out_of_bounds = []
    for q in data["data"]["quiz"]:
      n = len(q["choices"])
      if n < 3 or n > 5:
        out_of_bounds.append((q["question"][:40], n))
    # At least some questions have 3 choices (valid)
    assert len([q for q in data["data"]["quiz"] if len(q["choices"]) == 3]) >= 1
    assert len(out_of_bounds) == 0, f"Choice count out of 3-5 bounds: {out_of_bounds}"

  def test_fixture_sample_audit_approved(self):
    """Build a QuestionAudit from one clearly-valid fixture question."""
    data = _load_fixture()
    # The Steelers question: 3 choices, answer matches, no banned phrasing
    q = next(q for q in data["data"]["quiz"] if "Steelers" in q["title"])
    assert len(q["choices"]) == 3
    assert q["answer"] in q["choices"]
    audit = QuestionAudit(
      question=q["question"],
      choices=q["choices"],
      answer_matches_choice=q["answer"] in q["choices"],
      approved=True,
      review="Answer matches a choice.",
    )
    assert audit.approved is True
    assert audit.answer_matches_choice is True
