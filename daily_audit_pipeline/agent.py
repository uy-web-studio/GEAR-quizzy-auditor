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


# `adk run`/`adk web` discover a runnable agent by looking for a
# module-level `root_agent` — they have no Firestore/request context to
# fetch an operator-edited rubric from, so this is always the built-in
# default instruction. The real Cloud Run service never imports this name;
# it calls build_daily_pipeline(instruction) directly with whatever's
# currently saved (main.py's get_auditor_instruction()).
root_agent = build_daily_pipeline()
