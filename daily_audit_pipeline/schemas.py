from pydantic import BaseModel, Field


class QuestionAudit(BaseModel):
  """Per-question audit result, per SPEC.md §4's Firestore output contract."""

  question: str
  approved: bool
  review: str = Field(
      description=(
          "Reason the question failed a rubric check. Empty string when"
          " approved."
      )
  )
