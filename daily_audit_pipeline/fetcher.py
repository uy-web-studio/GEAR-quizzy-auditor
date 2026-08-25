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


class FetcherAgent(BaseAgent):
  """Tool-only agent (no LLM call): fetches the day's quiz from
  quizzy-news-service and hands it to the next agent in the pipeline
  via conversation history.
  """

  @override
  async def _run_async_impl(
      self, ctx: InvocationContext
  ) -> AsyncGenerator[Event, None]:
    async with httpx.AsyncClient(timeout=30.0) as client:
      response = await client.get(QUIZ_ENDPOINT)
      response.raise_for_status()
    quiz_json = response.text

    yield Event(
        invocation_id=ctx.invocation_id,
        author=self.name,
        branch=ctx.branch,
        content=types.Content(
            role="model",
            parts=[types.Part.from_text(text=quiz_json)],
        ),
    )
