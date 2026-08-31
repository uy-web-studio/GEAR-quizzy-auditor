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
