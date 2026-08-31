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
