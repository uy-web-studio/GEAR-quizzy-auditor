from google.adk.agents import LlmAgent, SequentialAgent
from google.adk.tools import google_search

from .fetcher import FetcherAgent
from .reporter import ReporterAgent
from .schemas import QuestionAudit

# Rubric ported from the pre-existing `news-quiz-qc` skill, per SPEC.md §5 —
# disclosed as prior written work being incorporated, not new hackathon code.
AUDITOR_INSTRUCTION = """\
You are auditing today's news quiz for editorial quality before it ships.

The previous message in this conversation contains the raw quiz JSON,
shaped like:
{"data": {"quizDate": "...", "quiz": [
  {"question": "...", "choices": ["...", ...], "answer": "...",
   "source": {"url": "https://..."}}
]}}

For every question in `data.quiz`, check all four rules below and decide
`approved`. If any rule fails, set `approved` to false and explain which
rule failed and why in `review`; otherwise leave `review` as an empty string.

1. Phrasing: reject meta-referential phrasing that refers to the quiz's own
   source material — e.g. "according to the article", "as mentioned in the
   news source", "a news article says...", "according to <outlet name>...".
   Direct phrasing or attributing a claim to a named expert/official is fine.
2. Answer/choice integrity: `answer` must be an exact, case-sensitive match
   to one entry in `choices`. Any mismatch (typo, casing, paraphrase) fails.
3. Choice count: `choices` must have between 3 and 5 entries, inclusive.
4. Fact-check: use the google_search tool to verify the question and the
   correct answer against the topic covered by the question's `source.url`.
   If search results contradict the question/answer, or you can't find
   support for the claimed fact, fail this rule.

Output one entry per question in the quiz, in the same order.
"""


auditor_agent = LlmAgent(
    name="auditor_agent",
    model="gemini-3.7-flash",
    description="Audits each quiz question against the editorial rubric.",
    instruction=AUDITOR_INSTRUCTION,
    tools=[google_search],
    output_schema=list[QuestionAudit],
    output_key="audit_results",
)

root_agent = SequentialAgent(
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
        auditor_agent,
        ReporterAgent(
            name="reporter_agent",
            description="Saves audit report to Firestore and sends email notification.",
        ),
    ],
)
