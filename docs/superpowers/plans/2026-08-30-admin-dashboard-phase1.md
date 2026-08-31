# Admin Dashboard Phase 1: Auth, Editable Rules, Dry Run, Report Transparency — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the operator sign in with Google, edit the auditor's rubric from the dashboard with a dry-run preview before saving, and see per-question fact-check transparency (choices, answer-integrity check, always-populated review, cross-validated sources) on every report — without touching historical-data editing (that's Phase 2, a separate plan, once this phase has been live long enough to accumulate raw quiz data).

**Architecture:** `auth.py` (new, repo root) handles Google Sign-In verification and stateless signed session cookies. `daily_audit_pipeline/agent.py` becomes factory functions instead of module-level singletons, so both the real daily pipeline and the dry-run endpoint can build an auditor with any instruction. `daily_audit_pipeline/grounding.py` extracts real `google_search` tool-call metadata from ADK's event stream. `ReporterAgent` persists richer per-question data and cross-validates self-reported sources against that real grounding data. `main.py` gains `/admin`, `/admin/rules`, `/admin/login`, `/admin/logout` routes, all auth-gated except login, and both `/` and `/reports/{date}` render admin UI conditionally.

**Tech Stack:** FastAPI, Google ADK (`google-adk`), Firestore, Google Identity Services (client-side), `google.oauth2.id_token` (already a dependency, already used for Cloud Scheduler's OIDC verification).

**Spec:** `docs/superpowers/specs/2026-08-30-admin-dashboard-design.md`

## Global Constraints

- Single allowlisted admin email for now (`ALLOWED_ADMIN_EMAILS` env var, default `donovanuy@gmail.com`), implemented as a list so adding more later is a config change, not a code change (spec §4).
- Session cookie: `HttpOnly`, `Secure`, `SameSite=Strict`, 12-hour TTL, stateless HMAC-signed (spec §4).
- Dry run must never write to Firestore's `audits/{date}` and must never send email (spec §5).
- `QuestionAudit.choices` and `.answer_matches_choice` are **required** fields (no default) — a malformed/incomplete model response should fail loudly, not silently render an empty report (spec §7).
- The correct answer is never rendered in any report, admin or not (spec §2 non-goals).
- All 64 existing tests must keep passing throughout.
- Dry-run target in this phase is **"today" only** — historical targets require Phase 2's raw quiz storage and aren't available yet.

---

## Task 1: Extract `fetch_quiz()` in `fetcher.py`

**Files:**
- Modify: `daily_audit_pipeline/fetcher.py`
- Test: `tests/test_ingest.py`

**Interfaces:**
- Produces: `async def fetch_quiz() -> dict` — fetches and parses today's quiz, returns the `data` object (`{"quizDate": str, "quiz": [...]}`). Used by `FetcherAgent` (this task) and, later, the dry-run endpoint's "today" target (Task 10).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_ingest.py`:

```python
class TestFetchQuiz:
  """Verify fetch_quiz() parses the endpoint's response into the data dict."""

  @pytest.mark.anyio
  async def test_fetch_quiz_returns_data_object(self):
    import httpx
    from unittest.mock import patch, AsyncMock

    from daily_audit_pipeline.fetcher import fetch_quiz

    sample_response = {
        "status": 200,
        "data": {
            "quizDate": "2026-08-30",
            "quiz": [
                {
                    "question": "Sample question?",
                    "choices": ["A", "B", "C"],
                    "answer": "A",
                    "source": {"url": "https://example.com/article"},
                }
            ],
        },
    }
    mock_response = httpx.Response(
        200, json=sample_response, request=httpx.Request("GET", "https://example.com")
    )

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
      mock_get.return_value = mock_response
      result = await fetch_quiz()

    assert result == sample_response["data"]
    assert result["quizDate"] == "2026-08-30"
    assert len(result["quiz"]) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_ingest.py::TestFetchQuiz -v`
Expected: FAIL with `ImportError: cannot import name 'fetch_quiz'`

- [ ] **Step 3: Implement `fetch_quiz()` and refactor `FetcherAgent` to use it**

Replace the full contents of `daily_audit_pipeline/fetcher.py` with:

```python
import json
from typing import AsyncGenerator

import httpx
from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.genai import types
from typing_extensions import override

# quizzy-news-service's public endpoint — external, pre-existing, unmodified.
# See SPEC.md §1's disclosure note: only GETs are made against this service.
QUIZ_ENDPOINT = "https://getdailygemini-mgpsab4ctq-uc.a.run.app"


async def fetch_quiz() -> dict:
  """Fetch and parse today's quiz from quizzy-news-service.

  Returns the `data` object: {"quizDate": str, "quiz": [...]}. Shared by
  FetcherAgent and the admin dry-run endpoint's "today" target so both use
  the exact same fetch/parse logic.
  """
  async with httpx.AsyncClient(timeout=30.0) as client:
    response = await client.get(QUIZ_ENDPOINT)
    response.raise_for_status()
  return response.json()["data"]


class FetcherAgent(BaseAgent):
  """Tool-only agent (no LLM call): fetches the day's quiz from
  quizzy-news-service and hands it to the next agent in the pipeline
  via conversation history.
  """

  @override
  async def _run_async_impl(
      self, ctx: InvocationContext
  ) -> AsyncGenerator[Event, None]:
    data = await fetch_quiz()
    quiz_json = json.dumps({"data": data})

    yield Event(
        invocation_id=ctx.invocation_id,
        author=self.name,
        branch=ctx.branch,
        content=types.Content(
            role="model",
            parts=[types.Part.from_text(text=quiz_json)],
        ),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_ingest.py -v`
Expected: All PASS (including the pre-existing `TestFetcherAgent`/`TestFetcherAgentSchema` tests, which don't touch `_run_async_impl` internals directly).

- [ ] **Step 5: Run the full suite to confirm no regressions**

Run: `.venv/bin/python3 -m pytest -q tests/`
Expected: `65 passed` (64 existing + 1 new)

- [ ] **Step 6: Commit**

```bash
git add daily_audit_pipeline/fetcher.py tests/test_ingest.py
git commit -m "Extract fetch_quiz() helper from FetcherAgent for reuse by the dry-run endpoint"
```

---

## Task 2: Extend `QuestionAudit` schema + update the default rubric prompt

**Files:**
- Modify: `daily_audit_pipeline/schemas.py`
- Modify: `daily_audit_pipeline/agent.py` (prompt text only in this task — the factory refactor is Task 3)
- Modify: `tests/test_evaluator.py`, `tests/test_original_quiz_fixture.py`, `tests/test_reporter.py`, `tests/test_sendgrid_dispatch.py` (fixing 19 call sites broken by the new required fields)

**Interfaces:**
- Produces: `QuestionAudit` now requires `choices: list[str]` and `answer_matches_choice: bool` (no defaults — a malformed model response must fail validation, not silently render empty). `sources_checked: list[str]` stays optional (`default_factory=list`). `review: str` is unchanged in type but its semantics change: always populated, not just on failure.

- [ ] **Step 1: Extend the schema**

Replace the full contents of `daily_audit_pipeline/schemas.py` with:

```python
from pydantic import BaseModel, Field


class QuestionAudit(BaseModel):
  """Per-question audit result, per SPEC.md §4's Firestore output contract."""

  question: str
  choices: list[str] = Field(
      description="The question's MCQ choices, copied verbatim from the input."
  )
  answer_matches_choice: bool = Field(
      description="Result of rule 2's answer/choice-integrity check."
  )
  approved: bool
  review: str = Field(
      description=(
          "Always populated — a short note on what was checked, pass or"
          " fail. Never left empty."
      )
  )
  sources_checked: list[str] = Field(
      default_factory=list,
      description=(
          "Source URL(s) actually used for this question's rule-4"
          " fact-check via google_search."
      ),
  )
```

- [ ] **Step 2: Run the full suite to see exactly what breaks**

Run: `.venv/bin/python3 -m pytest -q tests/`
Expected: Several `ValidationError` failures in `test_evaluator.py`, `test_original_quiz_fixture.py`, `test_reporter.py`, `test_sendgrid_dispatch.py` — every existing `QuestionAudit(...)` construction (direct or via dict) now missing `choices`/`answer_matches_choice`.

- [ ] **Step 3: Fix `tests/test_evaluator.py`'s 10 broken call sites**

Replace `tests/test_evaluator.py`'s `TestQuestionAudit` class (lines 14–45) with:

```python
class TestQuestionAudit:
  """Test QuestionAudit schema."""

  def test_question_audit_approved(self):
    """Test creating an approved audit result."""
    audit = QuestionAudit(
        question="What is 2+2?",
        choices=["4", "3", "5"],
        answer_matches_choice=True,
        approved=True,
        review="Answer matches a choice.",
    )
    assert audit.question == "What is 2+2?"
    assert audit.choices == ["4", "3", "5"]
    assert audit.answer_matches_choice is True
    assert audit.approved is True
    assert audit.review == "Answer matches a choice."

  def test_question_audit_failed(self):
    """Test creating a failed audit result."""
    audit = QuestionAudit(
        question="What is the capital of France?",
        choices=["Paris", "London", "Berlin"],
        answer_matches_choice=True,
        approved=False,
        review="Rule 1 (Phrasing): Meta-referential phrasing detected.",
    )
    assert audit.question == "What is the capital of France?"
    assert audit.approved is False
    assert "Meta-referential" in audit.review

  def test_question_audit_serialization(self):
    """Test QuestionAudit can be serialized to dict."""
    audit = QuestionAudit(
        question="Test question",
        choices=["A", "B", "C"],
        answer_matches_choice=True,
        approved=True,
        review="Looks good.",
    )
    data = audit.model_dump()
    assert data == {
        "question": "Test question",
        "choices": ["A", "B", "C"],
        "answer_matches_choice": True,
        "approved": True,
        "review": "Looks good.",
        "sources_checked": [],
    }
```

Replace `test_audit_all_approved` (lines 131–152) with:

```python
  def test_audit_all_approved(self):
    """Test audit result for all approved questions."""
    audit_results = [
        QuestionAudit(
            question="Q1: What is 2+2?",
            choices=["4", "3", "5"],
            answer_matches_choice=True,
            approved=True,
            review="Answer matches a choice.",
        ),
        QuestionAudit(
            question="Q2: What is the capital of France?",
            choices=["Paris", "London", "Berlin"],
            answer_matches_choice=True,
            approved=True,
            review="Answer matches a choice.",
        ),
    ]

    total = len(audit_results)
    approved = sum(1 for a in audit_results if a.approved)
    failed = total - approved

    assert total == 2
    assert approved == 2
    assert failed == 0
```

Replace `test_audit_mixed_results` (lines 154–183) with:

```python
  def test_audit_mixed_results(self):
    """Test audit result with mixed approved/failed questions."""
    audit_results = [
        QuestionAudit(
            question="Q1: What is 2+2?",
            choices=["4", "3", "5"],
            answer_matches_choice=True,
            approved=True,
            review="Answer matches a choice.",
        ),
        QuestionAudit(
            question="Q2: According to the article, what happened?",
            choices=["A", "B", "C"],
            answer_matches_choice=True,
            approved=False,
            review="Rule 1 (Phrasing): Meta-referential phrasing detected.",
        ),
        QuestionAudit(
            question="Q3: What is the capital of France?",
            choices=["Paris", "London", "Berlin"],
            answer_matches_choice=True,
            approved=True,
            review="Answer matches a choice.",
        ),
    ]

    total = len(audit_results)
    approved = sum(1 for a in audit_results if a.approved)
    failed = total - approved
    failed_questions = [a for a in audit_results if not a.approved]

    assert total == 3
    assert approved == 2
    assert failed == 1
    assert len(failed_questions) == 1
    assert "Meta-referential" in failed_questions[0].review
```

Replace `test_audit_all_failed` (lines 185–206) with:

```python
  def test_audit_all_failed(self):
    """Test audit result for all failed questions."""
    audit_results = [
        QuestionAudit(
            question="Q1: According to the source...",
            choices=["A", "B", "C"],
            answer_matches_choice=True,
            approved=False,
            review="Rule 1 (Phrasing): Meta-referential phrasing detected.",
        ),
        QuestionAudit(
            question="Q2: What is X?",
            choices=["Y", "Z", "W"],
            answer_matches_choice=False,
            approved=False,
            review="Rule 2 (Answer/Choice Integrity): Answer mismatch.",
        ),
    ]

    total = len(audit_results)
    approved = sum(1 for a in audit_results if a.approved)
    failed = total - approved

    assert total == 2
    assert approved == 0
    assert failed == 2
```

- [ ] **Step 4: Fix `tests/test_original_quiz_fixture.py`'s broken call site**

In `tests/test_original_quiz_fixture.py`, replace `test_fixture_sample_audit_approved` (lines 74–87) with:

```python
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
```

- [ ] **Step 5: Fix `tests/test_reporter.py`'s 2 broken call sites**

In `tests/test_reporter.py`, replace the `session.state` dict in `test_reporter_agent_all_approved_no_email` (lines 21–26):

```python
    session.state = {
        "audit_results": [
            {"question": "Q1", "choices": ["A", "B", "C"], "answer_matches_choice": True, "approved": True, "review": "Looks good."},
            {"question": "Q2", "choices": ["A", "B", "C"], "answer_matches_choice": True, "approved": True, "review": "Looks good."},
        ]
    }
```

Replace the `session.state` dict in `test_reporter_agent_with_failures_triggers_email` (lines 59–64):

```python
    session.state = {
        "audit_results": [
            QuestionAudit(question="Q1", choices=["A", "B", "C"], answer_matches_choice=True, approved=True, review="Looks good."),
            QuestionAudit(question="Q2", choices=["X", "Y", "Z"], answer_matches_choice=False, approved=False, review="Rule 1 failure"),
        ]
    }
```

- [ ] **Step 6: Fix `tests/test_sendgrid_dispatch.py`'s 2 broken call sites**

In `tests/test_sendgrid_dispatch.py`, replace the `audit_results` dict in `test_reporter_agent_triggers_sendgrid_on_failed_questions` (lines 499–502):

```python
    mock_session.state = {
        "audit_results": [
            {"question": "Q1: Valid?", "choices": ["A", "B", "C"], "answer_matches_choice": True, "approved": True, "review": "Looks good."},
            {"question": "Q2: Broken?", "choices": ["X", "Y", "Z"], "answer_matches_choice": False, "approved": False, "review": "Rule 1 violated"},
        ]
    }
```

Replace the `audit_results` dict in `test_reporter_agent_skips_sendgrid_when_all_questions_approved` (lines 548–551):

```python
    mock_session.state = {
        "audit_results": [
            {"question": "Q1", "choices": ["A", "B", "C"], "answer_matches_choice": True, "approved": True, "review": "Looks good."},
            {"question": "Q2", "choices": ["A", "B", "C"], "answer_matches_choice": True, "approved": True, "review": "Looks good."},
        ]
    }
```

- [ ] **Step 7: Run the full suite to confirm all fixes land**

Run: `.venv/bin/python3 -m pytest -q tests/`
Expected: `65 passed` (no new tests in this step, just fixed ones)

- [ ] **Step 8: Update the default rubric prompt**

In `daily_audit_pipeline/agent.py`, replace `AUDITOR_INSTRUCTION`'s value (keep the constant name as `AUDITOR_INSTRUCTION` for now — it gets renamed to `DEFAULT_AUDITOR_INSTRUCTION` in Task 3, alongside the factory refactor, to keep this task focused on schema/prompt content only) with:

```python
AUDITOR_INSTRUCTION = """\
You are auditing today's news quiz for editorial quality before it ships.

The previous message in this conversation contains the raw quiz JSON,
shaped like:
{"data": {"quizDate": "...", "quiz": [
  {"question": "...", "choices": ["...", ...], "answer": "...",
   "source": {"url": "https://..."}}
]}}

For every question in `data.quiz`, check all four rules below and decide
`approved`. If any rule fails, set `approved` to false; otherwise leave
`approved` as true. In BOTH cases, always fill in `review` with a short
note on what you checked (e.g. "Phrasing OK, answer matches a choice, 3
choices present, source supports the claim." on a pass, or the specific
rule that failed and why on a failure) — never leave `review` empty.

1. Phrasing: reject meta-referential phrasing that refers to the quiz's own
   source material — e.g. "according to the article", "as mentioned in the
   news source", "a news article says...", "according to <outlet name>...".
   Direct phrasing or attributing a claim to a named expert/official is fine.
2. Answer/choice integrity: `answer` must be an exact, case-sensitive match
   to one entry in `choices`. Any mismatch (typo, casing, paraphrase) fails.
   Always set `answer_matches_choice` to reflect this check's result
   (true/false), even when the question otherwise fails for a different
   reason.
3. Choice count: `choices` must have between 3 and 5 entries, inclusive.
4. Fact-check: use the google_search tool to verify the question and the
   correct answer against the topic covered by the question's `source.url`.
   If search results contradict the question/answer, or you can't find
   support for the claimed fact, fail this rule. Record the specific
   source URL(s) you actually consulted for this question's fact-check in
   `sources_checked` (an empty list only if the search tool genuinely
   returned nothing usable).

Always copy `choices` from the input into your output verbatim, for every
question, regardless of approval status — the report displays them.

Output one entry per question in the quiz, in the same order.
"""
```

- [ ] **Step 9: Run the full suite once more**

Run: `.venv/bin/python3 -m pytest -q tests/`
Expected: `65 passed` (prompt text isn't covered by a unit test directly — it's exercised by Task 3's factory tests)

- [ ] **Step 10: Commit**

```bash
git add daily_audit_pipeline/schemas.py daily_audit_pipeline/agent.py tests/test_evaluator.py tests/test_original_quiz_fixture.py tests/test_reporter.py tests/test_sendgrid_dispatch.py
git commit -m "Extend QuestionAudit with choices/answer_matches_choice/sources_checked; update rubric prompt to populate them and always fill review"
```

---

## Task 3: Refactor `agent.py` into configurable factories; wire `/trigger-audit` to `config/auditor_rules`

**Files:**
- Modify: `daily_audit_pipeline/agent.py`
- Modify: `main.py`
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: `QuestionAudit` (Task 2), `FetcherAgent`/`ReporterAgent` (unchanged).
- Produces: `build_auditor_agent(instruction: str = DEFAULT_AUDITOR_INSTRUCTION) -> LlmAgent` and `build_daily_pipeline(instruction: str = DEFAULT_AUDITOR_INSTRUCTION) -> SequentialAgent`, both in `daily_audit_pipeline/agent.py`. `main.py` gains `get_auditor_instruction(db) -> str`, used by Task 9 (rule editor page) and Task 10 (dry-run default) too.

- [ ] **Step 1: Refactor `agent.py`**

Replace the full contents of `daily_audit_pipeline/agent.py` with:

```python
from google.adk.agents import LlmAgent, SequentialAgent
from google.adk.tools import google_search

from .fetcher import FetcherAgent
from .reporter import ReporterAgent
from .schemas import QuestionAudit

# Rubric ported from the pre-existing `news-quiz-qc` skill, per SPEC.md §5 —
# disclosed as prior written work being incorporated, not new hackathon code.
# This is the seed default for Firestore's config/auditor_rules doc — the
# operator can edit the live rubric from the admin dashboard without a
# redeploy; see docs/superpowers/specs/2026-08-30-admin-dashboard-design.md.
DEFAULT_AUDITOR_INSTRUCTION = """\
You are auditing today's news quiz for editorial quality before it ships.

The previous message in this conversation contains the raw quiz JSON,
shaped like:
{"data": {"quizDate": "...", "quiz": [
  {"question": "...", "choices": ["...", ...], "answer": "...",
   "source": {"url": "https://..."}}
]}}

For every question in `data.quiz`, check all four rules below and decide
`approved`. If any rule fails, set `approved` to false; otherwise leave
`approved` as true. In BOTH cases, always fill in `review` with a short
note on what you checked (e.g. "Phrasing OK, answer matches a choice, 3
choices present, source supports the claim." on a pass, or the specific
rule that failed and why on a failure) — never leave `review` empty.

1. Phrasing: reject meta-referential phrasing that refers to the quiz's own
   source material — e.g. "according to the article", "as mentioned in the
   news source", "a news article says...", "according to <outlet name>...".
   Direct phrasing or attributing a claim to a named expert/official is fine.
2. Answer/choice integrity: `answer` must be an exact, case-sensitive match
   to one entry in `choices`. Any mismatch (typo, casing, paraphrase) fails.
   Always set `answer_matches_choice` to reflect this check's result
   (true/false), even when the question otherwise fails for a different
   reason.
3. Choice count: `choices` must have between 3 and 5 entries, inclusive.
4. Fact-check: use the google_search tool to verify the question and the
   correct answer against the topic covered by the question's `source.url`.
   If search results contradict the question/answer, or you can't find
   support for the claimed fact, fail this rule. Record the specific
   source URL(s) you actually consulted for this question's fact-check in
   `sources_checked` (an empty list only if the search tool genuinely
   returned nothing usable).

Always copy `choices` from the input into your output verbatim, for every
question, regardless of approval status — the report displays them.

Output one entry per question in the quiz, in the same order.
"""


def build_auditor_agent(instruction: str = DEFAULT_AUDITOR_INSTRUCTION) -> LlmAgent:
  """Build the auditor LlmAgent with a given rubric instruction.

  A factory rather than a module-level singleton so the admin dashboard's
  rule editor and dry-run endpoint can construct an auditor with an
  operator-edited or draft instruction, while the real daily pipeline uses
  whatever's currently saved in Firestore's config/auditor_rules doc.
  """
  return LlmAgent(
      name="auditor_agent",
      model="gemini-3.7-flash",
      description="Audits each quiz question against the editorial rubric.",
      instruction=instruction,
      tools=[google_search],
      output_schema=list[QuestionAudit],
      output_key="audit_results",
  )


def build_daily_pipeline(instruction: str = DEFAULT_AUDITOR_INSTRUCTION) -> SequentialAgent:
  """Build the full fetch -> audit -> report pipeline with a given rubric."""
  return SequentialAgent(
      name="daily_audit_pipeline",
      description=(
          "Fetches the day's news quiz, audits it against the editorial rubric,"
          " and reports results to Firestore with optional SendGrid notification."
      ),
      sub_agents=[
          FetcherAgent(
              name="fetcher_agent",
              description="Fetches today's quiz from quizzy-news-service.",
          ),
          build_auditor_agent(instruction),
          ReporterAgent(
              name="reporter_agent",
              description="Saves audit report to Firestore and sends email notification.",
          ),
      ],
  )
```

- [ ] **Step 2: Wire `main.py`'s imports and add `get_auditor_instruction`**

In `main.py`, replace:

```python
from daily_audit_pipeline.agent import root_agent
```

with:

```python
from daily_audit_pipeline.agent import DEFAULT_AUDITOR_INSTRUCTION, build_daily_pipeline
```

Then, immediately after `get_db()`'s definition, add:

```python
def get_auditor_instruction(db) -> str:
  """Read the currently saved auditor rubric, falling back to the default."""
  doc = db.collection("config").document("auditor_rules").get()
  if doc.exists:
    data = doc.to_dict() or {}
    instruction = data.get("instruction")
    if instruction:
      return instruction
  return DEFAULT_AUDITOR_INSTRUCTION
```

- [ ] **Step 3: Wire `trigger_audit()` to use the saved instruction**

In `main.py`'s `trigger_audit()`, the existing skip-check already fetches a client into a local variable named `check_db`. Rename it to `db` and reuse it for the rest of the function. Replace the whole function body from `quiz_date_today = ...` through the end of the `try` block that builds the `Runner` with:

```python
  quiz_date_today = datetime.now().strftime("%Y-%m-%d")
  db = get_db()
  try:
    existing_doc = db.collection("audits").document(quiz_date_today).get()
    if existing_doc.exists:
      existing_status = (existing_doc.to_dict() or {}).get("status")
      if existing_status in ("complete", "empty_final"):
        return {
            "status": "skipped",
            "timestamp": datetime.now().isoformat(),
            "message": (
                f"Audit for {quiz_date_today} already resolved "
                f"(status={existing_status}); not re-running."
            ),
        }
  except Exception as e:
    print(f"Warning: could not check existing audit status before trigger: {e}")

  try:
    instruction = get_auditor_instruction(db)
    session_service = InMemorySessionService()
    session = await session_service.create_session(
        app_name="quizzy_auditor", user_id="scheduler"
    )
    runner = Runner(
        agent=build_daily_pipeline(instruction),
        app_name="quizzy_auditor",
        session_service=session_service,
    )
```

(Everything below — the `async for event in runner.run_async(...)` loop and the `return`/`except` — stays exactly as it is; only the lines shown above change.)

- [ ] **Step 4: Write the failing test for instruction wiring**

Add to `tests/test_main.py`:

```python
def test_trigger_audit_uses_saved_rule_instruction(client):
  """POST /trigger-audit builds the pipeline with the saved config/auditor_rules
  instruction, not the hardcoded default, when one has been saved."""
  mock_audit_doc = MagicMock()
  mock_audit_doc.exists = False  # no audits/{today} doc yet -> not skipped

  mock_rules_doc = MagicMock()
  mock_rules_doc.exists = True
  mock_rules_doc.to_dict.return_value = {"instruction": "CUSTOM RUBRIC TEXT"}

  mock_db = MagicMock()

  def collection_side_effect(name):
    coll = MagicMock()
    if name == "audits":
      coll.document.return_value.get.return_value = mock_audit_doc
    elif name == "config":
      coll.document.return_value.get.return_value = mock_rules_doc
    return coll

  mock_db.collection.side_effect = collection_side_effect

  with patch.dict("os.environ", {"SKIP_AUTH": "true"}):
    with patch("main.get_db", return_value=mock_db):
      with patch("main.build_daily_pipeline") as mock_build_pipeline:
        with patch("main.Runner") as mock_runner_cls:
          mock_runner = MagicMock()

          async def fake_run_async(**kwargs):
            if False:
              yield None

          mock_runner.run_async = fake_run_async
          mock_runner_cls.return_value = mock_runner

          response = client.post("/trigger-audit")

          assert response.status_code == 200
          mock_build_pipeline.assert_called_once_with("CUSTOM RUBRIC TEXT")
```

- [ ] **Step 5: Run test to verify it fails, then passes**

Run: `.venv/bin/python3 -m pytest tests/test_main.py::test_trigger_audit_uses_saved_rule_instruction -v`
Expected before Step 2/3's changes land: FAIL (`ImportError` or `AttributeError: module 'main' has no attribute 'build_daily_pipeline'`). After Steps 1–3 above are applied: PASS.

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/python3 -m pytest -q tests/`
Expected: `66 passed`. If `test_trigger_audit_skips_when_already_complete` (existing test) fails, check that it still patches `main.Runner` and asserts `mock_runner_cls.assert_not_called()` — that test's assertion is about the pipeline never being *constructed*, which still holds since the skip-check now runs before `build_daily_pipeline` is ever called.

- [ ] **Step 7: Commit**

```bash
git add daily_audit_pipeline/agent.py main.py tests/test_main.py
git commit -m "Refactor agent.py into configurable factories; /trigger-audit now reads config/auditor_rules"
```

---

## Task 4: `daily_audit_pipeline/grounding.py` — extract and cross-validate real fact-check data

**Files:**
- Create: `daily_audit_pipeline/grounding.py`
- Test: `tests/test_grounding.py`

**Interfaces:**
- Produces: `extract_grounding_activity(events: list) -> dict` (returns `{"searchQueries": [...], "sources": [{"url", "title", "domain"}, ...]}`, deduplicated) and `cross_validate_sources(sources_checked: list[str], grounding_activity: dict) -> list[dict]` (returns `[{"url", "verified": bool}, ...]`). Used by `ReporterAgent` (Task 6) and the dry-run endpoint (Task 10).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_grounding.py`:

```python
"""Unit tests for daily_audit_pipeline.grounding."""

from google.adk.events import Event
from google.genai import types

from daily_audit_pipeline.grounding import (
    cross_validate_sources,
    extract_grounding_activity,
)


def _make_event(queries, chunks):
  gm = types.GroundingMetadata(
      web_search_queries=queries,
      grounding_chunks=[
          types.GroundingChunk(
              web=types.GroundingChunkWeb(uri=uri, title=title, domain=domain)
          )
          for uri, title, domain in chunks
      ],
  )
  return Event(
      invocation_id="inv-1",
      author="auditor_agent",
      branch=None,
      grounding_metadata=gm,
      content=types.Content(role="model", parts=[types.Part.from_text(text="...")]),
  )


class TestExtractGroundingActivity:
  def test_aggregates_queries_and_sources_across_events(self):
    events = [
        _make_event(
            ["capital of France"],
            [("https://example.com/a", "Example A", "example.com")],
        ),
        _make_event(
            ["Eiffel Tower height"],
            [("https://example.com/b", "Example B", "example.com")],
        ),
    ]

    result = extract_grounding_activity(events)

    assert result["searchQueries"] == ["capital of France", "Eiffel Tower height"]
    assert result["sources"] == [
        {"url": "https://example.com/a", "title": "Example A", "domain": "example.com"},
        {"url": "https://example.com/b", "title": "Example B", "domain": "example.com"},
    ]

  def test_deduplicates_repeated_queries_and_sources(self):
    events = [
        _make_event(
            ["capital of France"],
            [("https://example.com/a", "Example A", "example.com")],
        ),
        _make_event(
            ["capital of France"],
            [("https://example.com/a", "Example A", "example.com")],
        ),
    ]

    result = extract_grounding_activity(events)

    assert result["searchQueries"] == ["capital of France"]
    assert len(result["sources"]) == 1

  def test_ignores_events_with_no_grounding_metadata(self):
    plain_event = Event(
        invocation_id="inv-2",
        author="fetcher_agent",
        branch=None,
        content=types.Content(role="model", parts=[types.Part.from_text(text="{}")]),
    )

    result = extract_grounding_activity([plain_event])

    assert result == {"searchQueries": [], "sources": []}


class TestCrossValidateSources:
  def test_marks_real_sources_verified(self):
    grounding_activity = {
        "sources": [{"url": "https://example.com/a", "title": "A", "domain": "example.com"}]
    }

    result = cross_validate_sources(["https://example.com/a"], grounding_activity)

    assert result == [{"url": "https://example.com/a", "verified": True}]

  def test_flags_unfound_source_as_unverified(self):
    grounding_activity = {"sources": []}

    result = cross_validate_sources(["https://fabricated.example/x"], grounding_activity)

    assert result == [{"url": "https://fabricated.example/x", "verified": False}]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_grounding.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'daily_audit_pipeline.grounding'`

- [ ] **Step 3: Implement `grounding.py`**

Create `daily_audit_pipeline/grounding.py`:

```python
"""Extract real fact-checking activity (search queries, sources) from an
ADK agent run's event history, and cross-validate self-reported per-question
sources against it. See
docs/superpowers/specs/2026-08-30-admin-dashboard-design.md §7 for why this
is a hybrid (real aggregate data + cross-validated self-report) rather than
true per-question tool-call attribution, which ADK/Gemini don't expose.
"""


def extract_grounding_activity(events: list) -> dict:
  """Aggregate real grounding metadata across an agent run's events.

  Scans every event for `grounding_metadata` (populated by ADK whenever the
  google_search tool is actually invoked) and returns the real search
  queries issued and real source URLs returned, deduplicated in order of
  first appearance.
  """
  search_queries: list[str] = []
  sources: list[dict] = []
  seen_queries = set()
  seen_urls = set()

  for event in events:
    metadata = getattr(event, "grounding_metadata", None)
    if metadata is None:
      continue

    for query in (metadata.web_search_queries or []):
      if query not in seen_queries:
        seen_queries.add(query)
        search_queries.append(query)

    for chunk in (metadata.grounding_chunks or []):
      web = getattr(chunk, "web", None)
      if web is None or not web.uri:
        continue
      if web.uri in seen_urls:
        continue
      seen_urls.add(web.uri)
      sources.append({
          "url": web.uri,
          "title": web.title or "",
          "domain": web.domain or "",
      })

  return {"searchQueries": search_queries, "sources": sources}


def cross_validate_sources(sources_checked: list[str], grounding_activity: dict) -> list[dict]:
  """Cross-check self-reported per-question source URLs against real
  grounding activity for the whole run.

  Returns [{"url": ..., "verified": bool}, ...] — verified is False when a
  self-reported URL never actually appeared among the run's real grounding
  sources (the model claimed to check something the real tool calls don't
  back up).
  """
  real_urls = {s["url"] for s in grounding_activity.get("sources", [])}
  return [
      {"url": url, "verified": url in real_urls}
      for url in sources_checked
  ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_grounding.py -v`
Expected: All PASS

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python3 -m pytest -q tests/`
Expected: `71 passed` (66 + 5 new)

- [ ] **Step 6: Commit**

```bash
git add daily_audit_pipeline/grounding.py tests/test_grounding.py
git commit -m "Add grounding.py: extract real fact-check activity from ADK events, cross-validate self-reported sources"
```

---

## Task 5: `daily_audit_pipeline/revisions.py` — revision history logging

**Files:**
- Create: `daily_audit_pipeline/revisions.py`
- Test: `tests/test_revisions.py`

**Interfaces:**
- Produces: `log_revision(db, revision_type: str, actor: str, target: dict, before, after) -> None`. Used by the rule-save route (Task 9); Phase 2 reuses it for manual overrides, raw edits, and re-audits.

- [ ] **Step 1: Write the failing test**

Create `tests/test_revisions.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_revisions.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement `revisions.py`**

Create `daily_audit_pipeline/revisions.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_revisions.py -v`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python3 -m pytest -q tests/`
Expected: `72 passed`

- [ ] **Step 6: Commit**

```bash
git add daily_audit_pipeline/revisions.py tests/test_revisions.py
git commit -m "Add revisions.py: shared revision-history logging for admin write actions"
```

---

## Task 6: Refactor `ReporterAgent` — extract `save_audit_report()`, persist choices/sources/grounding

**Files:**
- Modify: `daily_audit_pipeline/reporter.py`
- Modify: `tests/test_reporter.py`, `tests/test_sendgrid_dispatch.py` (explicitly set `session.events` on every existing mock — see the important gotcha in Step 1 below)

**Interfaces:**
- Consumes: `extract_grounding_activity`, `cross_validate_sources` (Task 4).
- Produces: `save_audit_report(db, quiz_date: str, report_doc: dict) -> str` at module level in `reporter.py` (extracted from `ReporterAgent._save_report`, so Phase 2's re-audit endpoint can reuse it directly).

- [ ] **Step 1: Important — the `MagicMock(spec=Session)` gotcha**

`Session.events` is a real Pydantic field, but `dir(Session)` doesn't expose it (Pydantic fields aren't class-level attributes), so `MagicMock(spec=Session).events` raises `AttributeError` unless a test explicitly sets `session.events = [...]`. Every existing `ReporterAgent` test that will now exercise the audited-report path needs `session.events = []` (or a real list) added to its mock setup — verified directly:

```bash
.venv/bin/python3 -c "
from unittest.mock import MagicMock
from google.adk.sessions.session import Session
m = MagicMock(spec=Session)
m.events
"
```
This raises `AttributeError: Mock object has no attribute 'events'` today — confirming the fix is needed before Step 5 below, not optional.

- [ ] **Step 2: Update existing `ReporterAgent` tests to set `session.events`**

In `tests/test_reporter.py`, add `session.events = []` right after each `session.state = {...}` assignment in `test_reporter_agent_all_approved_no_email` and `test_reporter_agent_with_failures_triggers_email` (the two tests that reach `_report_audited`). The two no-quiz tests (`test_reporter_agent_no_quiz_first_attempt_no_email`, `test_reporter_agent_no_quiz_fourth_attempt_sends_alert`) never reach `_report_audited` since `total_questions == 0` short-circuits to `_report_no_quiz`, but `_run_async_impl` will still call `extract_grounding_activity(session.events)` unconditionally before that branch — so add `session.events = []` to those two as well.

In `tests/test_sendgrid_dispatch.py`, add `mock_session.events = []` right after each `mock_session.state = {...}` assignment in `test_reporter_agent_triggers_sendgrid_on_failed_questions` and `test_reporter_agent_skips_sendgrid_when_all_questions_approved`.

- [ ] **Step 3: Write the failing test for grounding integration**

Add to `tests/test_reporter.py` (needs `from google.genai import types as genai_types` and `from google.adk.events import Event as AdkEvent` added to the file's imports):

```python
  @pytest.mark.anyio
  async def test_reporter_agent_persists_choices_and_verified_sources(self):
    """Persisted questions include choices, answer_matches_choice, and
    sources_checked cross-validated against real grounding activity from
    ctx.session.events."""
    agent = ReporterAgent(name="reporter_agent")

    session = MagicMock(spec=Session)
    session.state = {
        "audit_results": [
            {
                "question": "What is the capital of France?",
                "choices": ["Paris", "London", "Berlin"],
                "answer_matches_choice": True,
                "approved": True,
                "review": "Answer matches a choice.",
                "sources_checked": ["https://example.com/paris-article"],
            },
        ]
    }
    grounding_event = AdkEvent(
        invocation_id="inv-x",
        author="auditor_agent",
        branch=None,
        grounding_metadata=genai_types.GroundingMetadata(
            web_search_queries=["capital of France"],
            grounding_chunks=[
                genai_types.GroundingChunk(
                    web=genai_types.GroundingChunkWeb(
                        uri="https://example.com/paris-article",
                        title="Paris facts",
                        domain="example.com",
                    )
                )
            ],
        ),
        content=genai_types.Content(role="model", parts=[genai_types.Part.from_text(text="...")]),
    )
    session.events = [grounding_event]

    ctx = MagicMock(spec=InvocationContext)
    ctx.session = session
    ctx.invocation_id = "test-inv-grounding"
    ctx.branch = "main"

    with patch("daily_audit_pipeline.reporter.send_audit_report", new_callable=AsyncMock):
      with patch("daily_audit_pipeline.reporter.firestore.Client") as mock_firestore:
        mock_db = MagicMock()
        mock_firestore.return_value = mock_db

        async for _ in agent._run_async_impl(ctx):
          pass

        saved_doc = mock_db.collection.return_value.document.return_value.set.call_args.args[0]
        question = saved_doc["questions"][0]
        assert question["choices"] == ["Paris", "London", "Berlin"]
        assert question["answer_matches_choice"] is True
        assert question["sources_checked"] == [
            {"url": "https://example.com/paris-article", "verified": True}
        ]
        assert saved_doc["groundingActivity"]["searchQueries"] == ["capital of France"]
```

- [ ] **Step 4: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_reporter.py -v`
Expected: FAIL — `KeyError: 'choices'` (current `_report_audited` doesn't persist it yet) or `AttributeError` on `session.events` (if Step 2's fixes haven't landed yet).

- [ ] **Step 5: Refactor `reporter.py`**

Replace the full contents of `daily_audit_pipeline/reporter.py` with:

```python
from datetime import datetime
from typing import AsyncGenerator
import os

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.cloud import firestore
from google.genai import types
from typing_extensions import override

from .grounding import cross_validate_sources, extract_grounding_activity
from .sendgrid_dispatch import send_audit_report, send_no_quiz_alert
from .schemas import QuestionAudit

# How many hourly checks the scheduler will make for a day's quiz before
# ReporterAgent gives up and alerts, per SPEC.md's no-quiz retry policy.
MAX_FETCH_ATTEMPTS = 4


def save_audit_report(db, quiz_date: str, report_doc: dict) -> str:
  """Write a report doc to audits/{quiz_date}.

  Module-level (not a ReporterAgent method) so Phase 2's retroactive
  re-audit endpoint can reuse the exact same save path.
  """
  if db is None:
    return "skipped (no Firestore client)"
  try:
    db.collection("audits").document(quiz_date).set(report_doc)
    return "saved"
  except Exception as e:
    print(f"Warning: Could not save report to Firestore: {e}")
    return f"skipped ({e})"


class ReporterAgent(BaseAgent):
  """Tool-only agent: reads audit results from session state, writes to
  Firestore, and sends SendGrid notification if any questions failed — or,
  once no quiz has been available for MAX_FETCH_ATTEMPTS consecutive hourly
  checks, sends a no-quiz alert instead.
  """

  @override
  async def _run_async_impl(
      self, ctx: InvocationContext
  ) -> AsyncGenerator[Event, None]:
    # Extract audit results from session state
    audit_results: list[QuestionAudit] = []
    quiz_date = datetime.now().strftime("%Y-%m-%d")
    recipient_email = os.environ.get("RECIPIENT_EMAIL", "donovanuy@gmail.com")

    # Get the session state to extract audit_results
    session = ctx.session
    if session and session.state:
      audit_results_raw = session.state.get("audit_results", [])
      audit_results = [
          QuestionAudit(**r) if isinstance(r, dict) else r
          for r in audit_results_raw
      ]

    total_questions = len(audit_results)
    grounding_activity = extract_grounding_activity(session.events if session else [])

    db = None
    try:
      project_id = os.environ.get("GOOGLE_CLOUD_PROJECT")
      db = firestore.Client(project=project_id) if project_id else firestore.Client()
    except Exception as e:
      print(f"Warning: Could not initialize Firestore client: {e}")

    if total_questions > 0:
      summary_text = await self._report_audited(
          db, quiz_date, recipient_email, audit_results, grounding_activity
      )
    else:
      summary_text = await self._report_no_quiz(db, quiz_date, recipient_email)

    yield Event(
        invocation_id=ctx.invocation_id,
        author=self.name,
        branch=ctx.branch,
        content=types.Content(
            role="model",
            parts=[types.Part.from_text(text=summary_text)],
        ),
    )

  async def _report_audited(
      self, db, quiz_date: str, recipient_email: str, audit_results, grounding_activity: dict
  ) -> str:
    """Save a normal report and email only if any question failed."""
    total_questions = len(audit_results)
    approved_count = sum(1 for q in audit_results if q.approved)
    failed_questions = [
        {"question": q.question, "review": q.review if q.review else ""}
        for q in audit_results
        if not q.approved
    ]
    questions_data = [
        {
            "question": q.question,
            "choices": q.choices,
            "answer_matches_choice": q.answer_matches_choice,
            "approved": q.approved,
            "review": q.review if q.review else "",
            "sources_checked": cross_validate_sources(q.sources_checked, grounding_activity),
        }
        for q in audit_results
    ]

    report_doc = {
        "quizDate": quiz_date,
        "generatedAt": datetime.now().isoformat() + "Z",
        "model": "gemini-3.7-flash",
        "status": "complete",
        "questions": questions_data,
        "groundingActivity": grounding_activity,
        "summary": {
            "total": total_questions,
            "approved": approved_count,
            "failed": len(failed_questions),
        },
    }
    firestore_status = save_audit_report(db, quiz_date, report_doc)

    if failed_questions:
      dry_run = os.environ.get("SENDGRID_DRY_RUN", "false").lower() in ("true", "1", "yes")
      send_result = await send_audit_report(
          recipient_email=recipient_email,
          quiz_date=quiz_date,
          total_questions=total_questions,
          approved_count=approved_count,
          failed_questions=failed_questions,
          dry_run=dry_run,
      )
      status_text = f"Report {firestore_status}. SendGrid status: {send_result.get('status', 'unknown')}"
    else:
      status_text = f"Report {firestore_status}. All questions approved — no email sent."

    return (
        f"Report Summary:\n"
        f"- Date: {quiz_date}\n"
        f"- Total: {total_questions}\n"
        f"- Approved: {approved_count}\n"
        f"- Failed: {len(failed_questions)}\n"
        f"\n{status_text}"
    )

  async def _report_no_quiz(self, db, quiz_date: str, recipient_email: str) -> str:
    """Record a no-quiz check; only alert once MAX_FETCH_ATTEMPTS is reached.

    The scheduler re-triggers this pipeline hourly, and this method reads
    how many previous checks already came back empty today, so it can
    decide whether to wait for another hourly retry or give up and email.
    """
    previous_attempts = 0
    if db is not None:
      try:
        existing = db.collection("audits").document(quiz_date).get()
        if existing.exists:
          previous_attempts = (existing.to_dict() or {}).get("fetchAttempts", 0) or 0
      except Exception as e:
        print(f"Warning: Could not read existing audit doc: {e}")

    attempts = previous_attempts + 1
    final_attempt = attempts >= MAX_FETCH_ATTEMPTS

    report_doc = {
        "quizDate": quiz_date,
        "generatedAt": datetime.now().isoformat() + "Z",
        "model": "gemini-3.7-flash",
        "status": "empty_final" if final_attempt else "empty",
        "fetchAttempts": attempts,
        "questions": [],
        "summary": {"total": 0, "approved": 0, "failed": 0},
    }
    firestore_status = save_audit_report(db, quiz_date, report_doc)

    if final_attempt:
      dry_run = os.environ.get("SENDGRID_DRY_RUN", "false").lower() in ("true", "1", "yes")
      send_result = await send_no_quiz_alert(
          recipient_email=recipient_email,
          quiz_date=quiz_date,
          attempts=attempts,
          dry_run=dry_run,
      )
      status_text = (
          f"Report {firestore_status}. No quiz was available after {attempts} "
          f"attempts — alert sent (SendGrid status: {send_result.get('status', 'unknown')})."
      )
    else:
      status_text = (
          f"Report {firestore_status}. No quiz was available (attempt "
          f"{attempts}/{MAX_FETCH_ATTEMPTS}) — will retry next hour, no email sent yet."
      )

    return (
        f"Report Summary:\n"
        f"- Date: {quiz_date}\n"
        f"- Total: 0\n"
        f"- Fetch attempts: {attempts}/{MAX_FETCH_ATTEMPTS}\n"
        f"\n{status_text}"
    )
```

- [ ] **Step 6: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_reporter.py -v`
Expected: All PASS

- [ ] **Step 7: Run the full suite**

Run: `.venv/bin/python3 -m pytest -q tests/`
Expected: `73 passed`

- [ ] **Step 8: Commit**

```bash
git add daily_audit_pipeline/reporter.py tests/test_reporter.py tests/test_sendgrid_dispatch.py
git commit -m "ReporterAgent: extract save_audit_report(), persist choices/answer_matches_choice/sources_checked/groundingActivity"
```

---

## Task 7: `auth.py` — Google Sign-In verification, signed session cookies, `require_admin` dependency

**Files:**
- Create: `auth.py` (repo root, sibling to `main.py`)
- Test: `tests/test_auth.py`

**Interfaces:**
- Produces: `SESSION_COOKIE_NAME`, `SESSION_TTL_SECONDS`, `get_allowed_admin_emails() -> list[str]`, `create_session_cookie(email, secret=None) -> str`, `verify_session_cookie(cookie_value, secret=None) -> Optional[str]`, `verify_google_id_token(token, client_id) -> str` (raises `ValueError`), `get_admin_email(request: Request) -> Optional[str]` (non-raising), `require_admin(request: Request) -> str` (FastAPI dependency, raises `HTTPException(401)`).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_auth.py`:

```python
"""Unit tests for auth.py."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from auth import (
    SESSION_COOKIE_NAME,
    create_session_cookie,
    get_admin_email,
    get_allowed_admin_emails,
    require_admin,
    verify_google_id_token,
    verify_session_cookie,
)


class TestSessionCookie:
  def test_round_trip_valid(self):
    cookie = create_session_cookie("donovanuy@gmail.com", secret="test-secret")
    assert verify_session_cookie(cookie, secret="test-secret") == "donovanuy@gmail.com"

  def test_tampered_signature_rejected(self):
    cookie = create_session_cookie("donovanuy@gmail.com", secret="test-secret")
    tampered = cookie[:-1] + ("0" if cookie[-1] != "0" else "1")
    assert verify_session_cookie(tampered, secret="test-secret") is None

  def test_wrong_secret_rejected(self):
    cookie = create_session_cookie("donovanuy@gmail.com", secret="test-secret")
    assert verify_session_cookie(cookie, secret="other-secret") is None

  def test_expired_cookie_rejected(self):
    with patch("auth.SESSION_TTL_SECONDS", -10):
      cookie = create_session_cookie("donovanuy@gmail.com", secret="test-secret")
    assert verify_session_cookie(cookie, secret="test-secret") is None

  def test_malformed_cookie_rejected(self):
    assert verify_session_cookie("not-a-valid-cookie", secret="test-secret") is None


class TestAllowedAdminEmails:
  def test_defaults_to_donovan(self, monkeypatch):
    monkeypatch.delenv("ALLOWED_ADMIN_EMAILS", raising=False)
    assert get_allowed_admin_emails() == ["donovanuy@gmail.com"]

  def test_parses_comma_separated_list(self, monkeypatch):
    monkeypatch.setenv("ALLOWED_ADMIN_EMAILS", "a@example.com, b@example.com")
    assert get_allowed_admin_emails() == ["a@example.com", "b@example.com"]


class TestVerifyGoogleIdToken:
  def test_valid_token_for_allowed_email_returns_email(self, monkeypatch):
    monkeypatch.setenv("ALLOWED_ADMIN_EMAILS", "donovanuy@gmail.com")
    with patch("auth.id_token.verify_oauth2_token") as mock_verify:
      mock_verify.return_value = {"email": "donovanuy@gmail.com", "email_verified": True}
      email = verify_google_id_token("fake-token", client_id="client-id-123")
    assert email == "donovanuy@gmail.com"

  def test_unverified_email_rejected(self, monkeypatch):
    monkeypatch.setenv("ALLOWED_ADMIN_EMAILS", "donovanuy@gmail.com")
    with patch("auth.id_token.verify_oauth2_token") as mock_verify:
      mock_verify.return_value = {"email": "donovanuy@gmail.com", "email_verified": False}
      with pytest.raises(ValueError):
        verify_google_id_token("fake-token", client_id="client-id-123")

  def test_non_allowlisted_email_rejected(self, monkeypatch):
    monkeypatch.setenv("ALLOWED_ADMIN_EMAILS", "donovanuy@gmail.com")
    with patch("auth.id_token.verify_oauth2_token") as mock_verify:
      mock_verify.return_value = {"email": "someone-else@gmail.com", "email_verified": True}
      with pytest.raises(ValueError):
        verify_google_id_token("fake-token", client_id="client-id-123")


class TestRequireAdmin:
  def test_valid_session_returns_email(self, monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "test-secret")
    cookie = create_session_cookie("donovanuy@gmail.com", secret="test-secret")
    request = MagicMock()
    request.cookies = {SESSION_COOKIE_NAME: cookie}
    assert require_admin(request) == "donovanuy@gmail.com"

  def test_missing_cookie_raises_401(self, monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "test-secret")
    request = MagicMock()
    request.cookies = {}
    with pytest.raises(HTTPException) as exc_info:
      require_admin(request)
    assert exc_info.value.status_code == 401

  def test_invalid_cookie_raises_401(self, monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "test-secret")
    request = MagicMock()
    request.cookies = {SESSION_COOKIE_NAME: "garbage"}
    with pytest.raises(HTTPException) as exc_info:
      require_admin(request)
    assert exc_info.value.status_code == 401


class TestGetAdminEmail:
  def test_returns_none_without_raising_when_missing(self, monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "test-secret")
    request = MagicMock()
    request.cookies = {}
    assert get_admin_email(request) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_auth.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'auth'`

- [ ] **Step 3: Implement `auth.py`**

Create `auth.py` at the repo root:

```python
"""Admin authentication: Google Sign-In verification and signed session
cookies. See docs/superpowers/specs/2026-08-30-admin-dashboard-design.md §4.
"""

import hashlib
import hmac
import os
import time
from typing import Optional

from fastapi import HTTPException, Request
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

SESSION_COOKIE_NAME = "admin_session"
SESSION_TTL_SECONDS = 12 * 60 * 60  # 12 hours


def get_allowed_admin_emails() -> list[str]:
  """Admin allowlist — one address today, a comma-separated list if more
  are ever added. See spec §4."""
  raw = os.environ.get("ALLOWED_ADMIN_EMAILS", "donovanuy@gmail.com")
  return [email.strip() for email in raw.split(",") if email.strip()]


def get_session_secret() -> str:
  secret = os.environ.get("SESSION_SECRET")
  if not secret:
    raise RuntimeError("SESSION_SECRET is not configured")
  return secret


def verify_google_id_token(token: str, client_id: str) -> str:
  """Verify a Google Identity Services ID token and return the verified
  email. Raises ValueError if invalid or the email isn't allowlisted."""
  claims = id_token.verify_oauth2_token(
      token, google_requests.Request(), audience=client_id
  )
  if not claims.get("email_verified"):
    raise ValueError("Email not verified by Google")
  email = claims.get("email")
  if email not in get_allowed_admin_emails():
    raise ValueError(f"{email} is not an allowed admin")
  return email


def create_session_cookie(email: str, secret: Optional[str] = None) -> str:
  """Build a signed, stateless session token: email:expiry:hmac_signature."""
  secret = secret or get_session_secret()
  expiry = int(time.time()) + SESSION_TTL_SECONDS
  payload = f"{email}:{expiry}"
  signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
  return f"{payload}:{signature}"


def verify_session_cookie(cookie_value: str, secret: Optional[str] = None) -> Optional[str]:
  """Verify a session cookie's signature and expiry. Returns the verified
  email, or None if invalid/expired/tampered."""
  secret = secret or get_session_secret()
  try:
    email, expiry_str, signature = cookie_value.rsplit(":", 2)
  except ValueError:
    return None

  payload = f"{email}:{expiry_str}"
  expected_signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
  if not hmac.compare_digest(signature, expected_signature):
    return None

  try:
    if int(expiry_str) < int(time.time()):
      return None
  except ValueError:
    return None

  return email


def get_admin_email(request: Request) -> Optional[str]:
  """Return the verified admin email for this request's session cookie, or
  None if there isn't a valid one. Non-raising — for conditionally showing
  admin UI to anonymous visitors without gating the route."""
  cookie_value = request.cookies.get(SESSION_COOKIE_NAME)
  if not cookie_value:
    return None
  return verify_session_cookie(cookie_value)


def require_admin(request: Request) -> str:
  """FastAPI dependency: raises 401 if there's no valid admin session,
  otherwise returns the verified email."""
  email = get_admin_email(request)
  if email is None:
    raise HTTPException(status_code=401, detail="Admin sign-in required")
  return email
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_auth.py -v`
Expected: All PASS

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python3 -m pytest -q tests/`
Expected: `87 passed` (73 + 14 new)

- [ ] **Step 6: Commit**

```bash
git add auth.py tests/test_auth.py
git commit -m "Add auth.py: Google Sign-In verification, signed session cookies, require_admin dependency"
```

---

## Task 8: `main.py` — `/admin/login`, `/admin/logout`

**Files:**
- Modify: `main.py`
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: `auth.verify_google_id_token`, `auth.create_session_cookie`, `auth.SESSION_COOKIE_NAME`, `auth.SESSION_TTL_SECONDS` (Task 7).
- Produces: `POST /admin/login` (body `{"credential": "<google id token>"}`, sets session cookie on success), `POST /admin/logout` (clears cookie).

- [ ] **Step 1: Add imports**

In `main.py`, add near the top (alongside the existing imports):

```python
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import auth
```

- [ ] **Step 2: Write the failing tests**

Add to `tests/test_main.py`:

```python
def test_admin_login_valid_token_sets_cookie(client):
  with patch("main.auth.verify_google_id_token", return_value="donovanuy@gmail.com"):
    response = client.post("/admin/login", json={"credential": "fake-id-token"})
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "email": "donovanuy@gmail.com"}
    assert "admin_session" in response.cookies


def test_admin_login_invalid_token_returns_401(client):
  with patch("main.auth.verify_google_id_token", side_effect=ValueError("bad token")):
    response = client.post("/admin/login", json={"credential": "bad-token"})
    assert response.status_code == 401


def test_admin_logout_clears_cookie(client):
  response = client.post("/admin/logout")
  assert response.status_code == 200
```

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_main.py -k admin_login -v`
Expected: FAIL with 404 (route doesn't exist yet)

- [ ] **Step 4: Implement the routes**

In `main.py`, add (after `dashboard()`, before `report_detail()` — placement doesn't affect routing, just keeps admin routes grouped):

```python
class LoginRequest(BaseModel):
  credential: str


@app.post("/admin/login")
async def admin_login(body: LoginRequest):
  """Verify a Google Identity Services ID token and issue a session cookie."""
  client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
  try:
    email = auth.verify_google_id_token(body.credential, client_id=client_id)
  except Exception as e:
    raise HTTPException(status_code=401, detail=str(e))

  cookie_value = auth.create_session_cookie(email)
  response = JSONResponse({"status": "ok", "email": email})
  response.set_cookie(
      key=auth.SESSION_COOKIE_NAME,
      value=cookie_value,
      httponly=True,
      secure=True,
      samesite="strict",
      max_age=auth.SESSION_TTL_SECONDS,
  )
  return response


@app.post("/admin/logout")
async def admin_logout():
  """Clear the admin session cookie."""
  response = JSONResponse({"status": "ok"})
  response.delete_cookie(auth.SESSION_COOKIE_NAME)
  return response
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_main.py -k admin_login -v`
Expected: PASS

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/python3 -m pytest -q tests/`
Expected: `90 passed`

- [ ] **Step 7: Commit**

```bash
git add main.py tests/test_main.py
git commit -m "Add /admin/login and /admin/logout routes"
```

---

## Task 9: `main.py` — `GET /admin` (rule editor page), `POST /admin/rules` (save action)

**Files:**
- Modify: `main.py`
- Modify: `requirements.txt` (add `python-multipart`, needed for FastAPI's `Form(...)` — confirmed present in the dev venv only transitively; the Docker build installs strictly from `requirements.txt`, so it must be listed explicitly or the deployed container 500s on this route)
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: `auth.require_admin` (Task 7), `log_revision` (Task 5), `get_auditor_instruction` (Task 3), `DEFAULT_AUDITOR_INSTRUCTION` (Task 3).
- Produces: `_render_admin_page(instruction, dry_run_result, saved) -> str` (HTML), `GET /admin` (auth-gated), `POST /admin/rules` with a `action` form field of `"save"` in this task (`"dry_run"` is added in Task 10, same route).

- [ ] **Step 1: Add `python-multipart` to requirements**

In `requirements.txt`, add a new line:

```
python-multipart>=0.0.20
```

- [ ] **Step 2: Write the failing tests**

Add to `tests/test_main.py`:

```python
def _admin_cookie_header():
  """A valid signed admin session cookie header for tests, using the same
  SESSION_SECRET the test process sets via monkeypatch."""
  import auth
  cookie_value = auth.create_session_cookie("donovanuy@gmail.com", secret="test-secret")
  return {"Cookie": f"{auth.SESSION_COOKIE_NAME}={cookie_value}"}


def test_admin_rules_page_requires_auth(client):
  response = client.get("/admin")
  assert response.status_code == 401


def test_admin_rules_page_shows_current_instruction(client, monkeypatch):
  monkeypatch.setenv("SESSION_SECRET", "test-secret")
  mock_rules_doc = MagicMock()
  mock_rules_doc.exists = True
  mock_rules_doc.to_dict.return_value = {"instruction": "CURRENT RUBRIC TEXT"}
  mock_db = MagicMock()
  mock_db.collection.return_value.document.return_value.get.return_value = mock_rules_doc

  with patch("main.get_db", return_value=mock_db):
    response = client.get("/admin", headers=_admin_cookie_header())
    assert response.status_code == 200
    assert "CURRENT RUBRIC TEXT" in response.text


def test_admin_save_rules_requires_auth(client):
  response = client.post("/admin/rules", data={"instruction": "x", "action": "save"})
  assert response.status_code == 401


def test_admin_save_rules_writes_and_logs_revision(client, monkeypatch):
  monkeypatch.setenv("SESSION_SECRET", "test-secret")
  mock_existing_doc = MagicMock()
  mock_existing_doc.exists = False
  mock_db = MagicMock()
  mock_db.collection.return_value.document.return_value.get.return_value = mock_existing_doc

  with patch("main.get_db", return_value=mock_db):
    with patch("main.log_revision") as mock_log_revision:
      response = client.post(
          "/admin/rules",
          data={"instruction": "NEW RUBRIC TEXT", "action": "save"},
          headers=_admin_cookie_header(),
      )
      assert response.status_code == 200
      assert "Rules saved" in response.text

      set_call = mock_db.collection.return_value.document.return_value.set.call_args.args[0]
      assert set_call["instruction"] == "NEW RUBRIC TEXT"
      assert set_call["updatedBy"] == "donovanuy@gmail.com"

      mock_log_revision.assert_called_once()
      call_kwargs = mock_log_revision.call_args.kwargs
      assert call_kwargs["revision_type"] == "rule_change"
      assert call_kwargs["actor"] == "donovanuy@gmail.com"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_main.py -k admin_rules -v`
Expected: FAIL with 404 (routes don't exist yet)

- [ ] **Step 4: Implement `_render_admin_page` and the two routes**

In `main.py`, add the following imports near the top:

```python
from fastapi import Depends, Form

from daily_audit_pipeline.revisions import log_revision
```

Add, after `get_auditor_instruction`:

```python
def _render_admin_page(instruction: str, dry_run_result: Optional[dict], saved: bool) -> str:
  saved_banner = (
      '<div class="empty" style="color: var(--good-shadow); background: var(--good-tint); border-radius: 10px; padding: 12px;">Rules saved.</div>'
      if saved else ""
  )

  dry_run_html = ""
  if dry_run_result is not None:
    if dry_run_result.get("error"):
      dry_run_html = f'<div class="empty" style="color: var(--bad-shadow); background: var(--bad-tint); border-radius: 10px; padding: 12px;">Dry run failed: {dry_run_result["error"]}</div>'
    else:
      cards = "".join(
          f"""
          <div class="fail-card" style="background: var(--card); box-shadow: 0 4px 0 {'var(--good-shadow)' if q['approved'] else 'var(--bad-shadow)'};">
            <div class="q">{q['question']} — {'PASS' if q['approved'] else 'FAIL'}</div>
            <div class="why">{q['review']}</div>
          </div>
          """
          for q in dry_run_result["questions"]
      )
      dry_run_html = f"""
      <h2 style="margin-top: 24px;">Dry Run Results ({dry_run_result['approved']}/{dry_run_result['total']} approved)</h2>
      <div class="card-list">{cards}</div>
      """

  return f"""
  <!DOCTYPE html>
  <html>
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1.0">
      <title>Quizzy Auditor — Edit Rules</title>
{FONT_LINKS}
      <style>
{SHARED_STYLES}
        textarea {{ width: 100%; min-height: 320px; font-family: monospace; font-size: 13px; padding: 12px; border-radius: 10px; border: 1px solid var(--line); box-sizing: border-box; }}
        .btn {{ display: inline-block; padding: 10px 20px; border-radius: 8px; border: none; background: var(--brand); color: white; font-weight: 700; font-size: 14px; cursor: pointer; box-shadow: 0 4px 0 var(--brand-shadow); }}
      </style>
    </head>
    <body>
      <div class="page-header container">
        <a href="/" class="back-link">← All audits</a>
        <h1>Edit Auditor Rules</h1>
      </div>
      <div class="container">
        {saved_banner}
        <form method="POST" action="/admin/rules">
          <textarea name="instruction">{instruction}</textarea>
          <div style="margin-top: 12px;">
            <button class="btn" type="submit" name="action" value="save">Save</button>
            <button class="btn" type="submit" name="action" value="dry_run" style="background: var(--good); box-shadow: 0 4px 0 var(--good-shadow);">Dry Run (today's live quiz)</button>
          </div>
          <input type="hidden" name="target" value="today">
        </form>
        {dry_run_html}
      </div>
    </body>
  </html>
  """


@app.get("/admin", response_class=HTMLResponse)
async def admin_rules_page(admin_email: str = Depends(auth.require_admin)):
  """Rule editor — auth-gated. Shows the current saved rubric in a textarea."""
  db = get_db()
  instruction = get_auditor_instruction(db)
  return _render_admin_page(instruction=instruction, dry_run_result=None, saved=False)


@app.post("/admin/rules", response_class=HTMLResponse)
async def admin_rules_action(
    instruction: str = Form(...),
    action: str = Form(...),
    target: str = Form("today"),
    admin_email: str = Depends(auth.require_admin),
):
  """Save an edited rubric instruction (action=save)."""
  db = get_db()

  # action == "dry_run" is handled in Task 10; "save" is this task's scope.
  before = get_auditor_instruction(db)
  db.collection("config").document("auditor_rules").set({
      "instruction": instruction,
      "updatedAt": datetime.now().isoformat() + "Z",
      "updatedBy": admin_email,
  })
  log_revision(
      db,
      revision_type="rule_change",
      actor=admin_email,
      target={"scope": "auditor_rules"},
      before={"instruction": before},
      after={"instruction": instruction},
  )
  return _render_admin_page(instruction=instruction, dry_run_result=None, saved=True)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_main.py -k admin_rules -v`
Expected: PASS

- [ ] **Step 6: Install `python-multipart` in the dev venv and run the full suite**

Run: `.venv/bin/pip install python-multipart`
Run: `.venv/bin/python3 -m pytest -q tests/`
Expected: `94 passed`

- [ ] **Step 7: Commit**

```bash
git add main.py requirements.txt tests/test_main.py
git commit -m "Add GET /admin rule editor page and POST /admin/rules save action"
```

---

## Task 10: `main.py` — dry-run action (`POST /admin/rules` with `action=dry_run`)

**Files:**
- Modify: `main.py`
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: `fetch_quiz` (Task 1), `build_auditor_agent` (Task 3), `extract_grounding_activity`/`cross_validate_sources` (Task 4).
- Produces: `run_dry_run(instruction: str, target: str) -> dict` in `main.py` — returns `{"questions": [...], "total", "approved", "groundingActivity"}` or `{"error": "..."}`. Never writes to `audits/{date}`, never sends email.

- [ ] **Step 1: Add imports**

In `main.py`, add:

```python
import json

from daily_audit_pipeline.agent import build_auditor_agent
from daily_audit_pipeline.fetcher import fetch_quiz
from daily_audit_pipeline.grounding import cross_validate_sources, extract_grounding_activity
from daily_audit_pipeline.schemas import QuestionAudit
```

- [ ] **Step 2: Write the failing test**

Add to `tests/test_main.py`:

```python
def test_admin_dry_run_today_returns_results_without_saving(client, monkeypatch):
  monkeypatch.setenv("SESSION_SECRET", "test-secret")
  mock_db = MagicMock()

  with patch("main.get_db", return_value=mock_db):
    with patch("main.fetch_quiz", new_callable=AsyncMock) as mock_fetch:
      mock_fetch.return_value = {
          "quizDate": "2026-08-30",
          "quiz": [{"question": "Q1?", "choices": ["A", "B", "C"], "answer": "A", "source": {"url": "https://example.com"}}],
      }
      with patch("main.InMemorySessionService") as mock_session_service_cls:
        mock_session_service = MagicMock()

        async def fake_create_session(**kwargs):
          return MagicMock(id="dry-run-session-1")

        mock_session_service.create_session = fake_create_session

        async def fake_get_session(**kwargs):
          session = MagicMock()
          session.state = {
              "audit_results": [
                  {
                      "question": "Q1?",
                      "choices": ["A", "B", "C"],
                      "answer_matches_choice": True,
                      "approved": True,
                      "review": "Looks good.",
                      "sources_checked": [],
                  }
              ]
          }
          return session

        mock_session_service.get_session = fake_get_session
        mock_session_service_cls.return_value = mock_session_service

        with patch("main.Runner") as mock_runner_cls:
          mock_runner = MagicMock()

          async def fake_run_async(**kwargs):
            if False:
              yield None

          mock_runner.run_async = fake_run_async
          mock_runner_cls.return_value = mock_runner

          response = client.post(
              "/admin/rules",
              data={"instruction": "DRAFT INSTRUCTION", "action": "dry_run", "target": "today"},
              headers=_admin_cookie_header(),
          )

          assert response.status_code == 200
          assert "1/1 approved" in response.text
          # Dry run never writes to audits/{date}
          for call in mock_db.collection.call_args_list:
            assert call.args[0] != "audits"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_main.py -k dry_run -v`
Expected: FAIL — the `"save"`-only branch from Task 9 runs regardless of `action`, so it 500s or asserts wrong content.

- [ ] **Step 4: Implement `run_dry_run` and wire the dispatch**

In `main.py`, add (near `get_auditor_instruction`):

```python
async def run_dry_run(instruction: str, target: str) -> dict:
  """Run the auditor step only (no Fetcher, no Reporter) against either a
  fresh live fetch ("today") or — in Phase 2 — a stored quizzes/{date} doc.
  No Firestore write to audits/{date}, no email. See spec §5."""
  try:
    if target == "today":
      quiz_data = await fetch_quiz()
    else:
      return {"error": f"Historical dry-run targets aren't available yet (target={target})"}

    quiz_json = json.dumps({"data": quiz_data})

    session_service = InMemorySessionService()
    session = await session_service.create_session(app_name="dry_run", user_id="admin")
    runner = Runner(
        agent=build_auditor_agent(instruction),
        app_name="dry_run",
        session_service=session_service,
    )
    events = []
    async for event in runner.run_async(
        user_id="admin",
        session_id=session.id,
        new_message=types.Content(role="user", parts=[types.Part.from_text(text=quiz_json)]),
    ):
      events.append(event)

    final_session = await session_service.get_session(
        app_name="dry_run", user_id="admin", session_id=session.id
    )
    audit_results_raw = (final_session.state or {}).get("audit_results", [])
    audit_results = [QuestionAudit(**r) if isinstance(r, dict) else r for r in audit_results_raw]

    grounding_activity = extract_grounding_activity(events)
    questions = [
        {
            "question": q.question,
            "choices": q.choices,
            "answer_matches_choice": q.answer_matches_choice,
            "approved": q.approved,
            "review": q.review,
            "sources_checked": cross_validate_sources(q.sources_checked, grounding_activity),
        }
        for q in audit_results
    ]
    approved_count = sum(1 for q in questions if q["approved"])

    return {
        "questions": questions,
        "total": len(questions),
        "approved": approved_count,
        "groundingActivity": grounding_activity,
    }
  except Exception as e:
    return {"error": str(e)}
```

Then update `admin_rules_action` (from Task 9) to dispatch on `action`. Replace its body with:

```python
@app.post("/admin/rules", response_class=HTMLResponse)
async def admin_rules_action(
    instruction: str = Form(...),
    action: str = Form(...),
    target: str = Form("today"),
    admin_email: str = Depends(auth.require_admin),
):
  """Save an edited rubric (action=save), or preview it without saving
  (action=dry_run)."""
  db = get_db()

  if action == "dry_run":
    dry_run_result = await run_dry_run(instruction, target)
    return _render_admin_page(instruction=instruction, dry_run_result=dry_run_result, saved=False)

  before = get_auditor_instruction(db)
  db.collection("config").document("auditor_rules").set({
      "instruction": instruction,
      "updatedAt": datetime.now().isoformat() + "Z",
      "updatedBy": admin_email,
  })
  log_revision(
      db,
      revision_type="rule_change",
      actor=admin_email,
      target={"scope": "auditor_rules"},
      before={"instruction": before},
      after={"instruction": instruction},
  )
  return _render_admin_page(instruction=instruction, dry_run_result=None, saved=True)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_main.py -k dry_run -v`
Expected: PASS

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/python3 -m pytest -q tests/`
Expected: `95 passed`

- [ ] **Step 7: Commit**

```bash
git add main.py tests/test_main.py
git commit -m "Add dry-run action to POST /admin/rules — preview rule edits against today's live quiz"
```

---

## Task 11: Admin sign-in UI + per-question report transparency rendering

**Files:**
- Modify: `main.py`
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: `auth.get_admin_email` (Task 7), the new `QuestionAudit` fields as persisted by Task 6 (`choices`, `answer_matches_choice`, `sources_checked`).
- Produces: updated `dashboard()` and `report_detail()` — both take `request: Request`, both show admin sign-in/nav conditionally, `report_detail()` renders every question (not just failures) with choices, the answer-match check, always-populated review, and cross-validated sources. The correct answer is never rendered.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_main.py`:

```python
def test_dashboard_shows_sign_in_when_anonymous(client):
  mock_db = MagicMock()
  mock_db.collection.return_value.order_by.return_value.limit.return_value.stream.return_value = []

  with patch("main.get_db", return_value=mock_db):
    response = client.get("/")
    assert response.status_code == 200
    assert "g_id_signin" in response.text
    assert 'href="/admin"' not in response.text


def test_dashboard_shows_admin_nav_when_authenticated(client, monkeypatch):
  monkeypatch.setenv("SESSION_SECRET", "test-secret")
  mock_db = MagicMock()
  mock_db.collection.return_value.order_by.return_value.limit.return_value.stream.return_value = []

  with patch("main.get_db", return_value=mock_db):
    response = client.get("/", headers=_admin_cookie_header())
    assert response.status_code == 200
    assert 'href="/admin"' in response.text


def test_report_detail_shows_all_questions_with_choices_no_answer(client):
  mock_doc = MagicMock()
  mock_doc.exists = True
  mock_doc.to_dict.return_value = {
      "quizDate": "2026-08-30",
      "status": "complete",
      "summary": {"total": 2, "approved": 1, "failed": 1},
      "questions": [
          {
              "question": "What is the capital of France?",
              "choices": ["Paris", "London", "Berlin"],
              "answer_matches_choice": True,
              "approved": True,
              "review": "Answer matches a choice.",
              "sources_checked": [{"url": "https://example.com/paris", "verified": True}],
          },
          {
              "question": "According to the article, what happened?",
              "choices": ["X", "Y", "Z"],
              "answer_matches_choice": False,
              "approved": False,
              "review": "Rule 1 failure: meta-referential phrasing.",
              "sources_checked": [{"url": "https://fabricated.example/x", "verified": False}],
          },
      ],
  }
  mock_db = MagicMock()
  mock_db.collection.return_value.document.return_value.get.return_value = mock_doc

  with patch("main.get_db", return_value=mock_db):
    response = client.get("/reports/2026-08-30")
    assert response.status_code == 200
    # Both questions shown, not just the failed one
    assert "What is the capital of France?" in response.text
    assert "According to the article, what happened?" in response.text
    # Choices shown
    assert "Paris" in response.text
    assert "London" in response.text
    # Sources and their verification status shown
    assert "https://example.com/paris" in response.text
    assert "https://fabricated.example/x" in response.text
    assert "unverified" in response.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_main.py -k "sign_in or admin_nav or all_questions" -v`
Expected: FAIL — no sign-in UI yet, `report_detail()` still only renders failed questions.

- [ ] **Step 3: Update `dashboard()` for conditional admin UI**

In `main.py`, change the `dashboard()` signature and add the admin-nav block. Replace:

```python
@app.get("/", response_class=HTMLResponse)
async def dashboard():
```

with:

```python
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
```

Then, right before the final `return f"""..."""` in `dashboard()`, add:

```python
  admin_email = auth.get_admin_email(request)
  oauth_client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
  if admin_email:
    admin_nav = (
        f'<div style="text-align: center; margin-top: 8px; font-size: 13px;">'
        f'<a href="/admin">Admin</a> · '
        f'<a href="#" onclick="fetch(\'/admin/logout\', {{method: \'POST\'}}).then(() => location.reload()); return false;">Sign out ({admin_email})</a>'
        f'</div>'
    )
  else:
    admin_nav = f"""
    <div style="text-align: center; margin-top: 8px;">
      <div id="g_id_onload" data-client_id="{oauth_client_id}" data-callback="handleGoogleSignIn"></div>
      <div class="g_id_signin" style="display: inline-block;" data-type="standard"></div>
    </div>
    <script src="https://accounts.google.com/gsi/client" async defer></script>
    <script>
      function handleGoogleSignIn(response) {{
        fetch('/admin/login', {{
          method: 'POST',
          headers: {{'Content-Type': 'application/json'}},
          body: JSON.stringify({{credential: response.credential}})
        }}).then(() => location.reload());
      }}
    </script>
    """
```

Then, in the return f-string, add `{admin_nav}` right after the closing `</div>` of `.topbar` (i.e. immediately before `<div class="container">`).

- [ ] **Step 4: Update `report_detail()` to render all questions with new fields**

Replace the `else:` branch of `report_detail()` (the `total > 0` branch, from Task 2's earlier work — currently builds `failed_questions`/`fail_cards`) with:

```python
    else:
      def render_sources(sources_checked):
        if not sources_checked:
          return ""
        items = "".join(
            f'<li>{s["url"]}{"" if s.get("verified") else " — ⚠ unverified"}</li>'
            for s in sources_checked
        )
        return (
            '<div style="margin-top: 6px; font-size: 12px; color: var(--muted);">'
            f'<strong>Sources checked:</strong><ul style="margin: 4px 0 0 18px;">{items}</ul></div>'
        )

      question_cards = "".join(
          f"""
          <div class="fail-card" style="background: var(--card); box-shadow: 0 4px 0 {'var(--good-shadow)' if r.get('approved') else 'var(--bad-shadow)'};">
            <div class="q">{r.get('question', 'N/A')} {'✓' if r.get('approved') else '✗'}</div>
            <div class="why">{r.get('review') or ('Approved' if r.get('approved') else 'Failed')}</div>
            <div style="margin-top: 8px; font-size: 13px;">
              <strong>Choices:</strong> {', '.join(r.get('choices', []))}
              &nbsp;·&nbsp;
              <strong>Answer matches a choice:</strong> {'✓' if r.get('answer_matches_choice') else '✗'}
            </div>
            {render_sources(r.get('sources_checked', []))}
          </div>
          """
          for r in audit_results
      )
      failed_section = (
          f'<h2 style="margin-top: 32px; margin-bottom: 4px;">Questions</h2>'
          f'<div class="card-list">{question_cards}</div>'
      )
      stat_approved_class = "good"
      stat_failed_class = "bad"
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_main.py -k "sign_in or admin_nav or all_questions" -v`
Expected: PASS

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/python3 -m pytest -q tests/`
Expected: `98 passed`. If `test_dashboard_rendering` (existing, pre-this-plan) fails because it now requires a `request` fixture implicitly via `TestClient` (it shouldn't — `TestClient.get("/")` supplies a real `Request` automatically), investigate; the `dashboard()` signature change only adds a parameter FastAPI injects itself, it doesn't change the call contract for `TestClient`.

- [ ] **Step 7: Commit**

```bash
git add main.py tests/test_main.py
git commit -m "Add admin sign-in UI to dashboard; report page renders all questions with choices, answer-match check, and cross-validated sources"
```

---

## Self-Review

**Spec coverage** (against `docs/superpowers/specs/2026-08-30-admin-dashboard-design.md`):
- §4 Auth → Tasks 7, 8, 11 (sign-in UI). ✅
- §5 Rule editing + dry run → Tasks 3, 9, 10. ✅ (historical dry-run target explicitly deferred to Phase 2, per spec §10's phasing)
- §7 Report transparency → Tasks 2, 4, 6, 11. ✅
- §9 Revision history → Task 5 (module), Task 9 (first real caller — rule_change). Phase 2 plan will add the remaining three revision types and the `/admin/history` page.
- §6, §8 (historical data, safety rule) → explicitly Phase 2, not in this plan.
- §11 file layout → matches: `auth.py`, `daily_audit_pipeline/grounding.py`, `daily_audit_pipeline/revisions.py` all created as specified; `agent.py`, `reporter.py`, `fetcher.py`, `schemas.py`, `main.py` all modified as specified.

**Placeholder scan:** No TBD/TODO markers; every step has complete, runnable code; no "similar to Task N" references — every diff is written out in full even where two tasks touch the same function (Task 10 shows `admin_rules_action`'s full replaced body rather than referring back to Task 9's version).

**Type consistency:** `QuestionAudit` field names (`choices`, `answer_matches_choice`, `sources_checked`) are identical across Task 2 (schema), Task 6 (`ReporterAgent`), and Task 10 (`run_dry_run`). `extract_grounding_activity`/`cross_validate_sources` signatures match between Task 4's definition and Tasks 6/10's call sites. `save_audit_report(db, quiz_date, report_doc)` signature matches its Task 6 definition (no callers outside `reporter.py` in this phase — Phase 2's re-audit endpoint will import it). `get_auditor_instruction(db)` defined once in Task 3, called identically in Tasks 9 and 10.

**Known deferred item:** `/admin/history` (revision-history viewing page) isn't in this phase — `log_revision` is called starting now (rule saves), but nothing renders the collection yet. This is intentional: the spec places `/admin/history` in Phase 2 alongside the other three revision types, so viewing a log with only one entry type for a while isn't a useful page yet. If review disagrees, the Task 5 module already supports adding a simple list-rendering route with no changes to `revisions.py`.

---

Plan complete and saved to `docs/superpowers/plans/2026-08-30-admin-dashboard-phase1.md`. Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
