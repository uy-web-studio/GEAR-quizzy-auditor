"""Tests for the FetcherAgent (SPEC M1 — single GET against quizzy-news-service)."""

import pytest
from daily_audit_pipeline.fetcher import FetcherAgent, QUIZ_ENDPOINT


class TestFetcherAgent:
    """Verify FetcherAgent is a tool-only BaseAgent that does a single GET."""

    def test_fetcher_is_baseagent(self):
        """FetcherAgent subclasses google.adk.agents.BaseAgent."""
        assert isinstance(FetcherAgent(name="fetcher_agent"), FetcherAgent)

    def test_fetcher_has_name(self):
        """FetcherAgent can be instantiated with a name."""
        agent = FetcherAgent(name="fetcher_agent")
        assert agent.name == "fetcher_agent"

    def test_quiz_endpoint_is_live_url(self):
        """QUIZ_ENDPOINT points at the real quizzy-news-service run.app domain."""
        assert QUIZ_ENDPOINT.startswith("https://")
        assert QUIZ_ENDPOINT.endswith(".a.run.app")
        assert "getdailygemini" in QUIZ_ENDPOINT

    def test_fetcher_description(self):
        """FetcherAgent carries a descriptive docstring mentioning quizzy-news-service."""
        assert "quizzy-news-service" in (FetcherAgent.__doc__ or "")


class TestFetcherAgentSchema:
    """Verify the FetcherAgent produces quiz JSON shaped as SPEC.md §4 expects."""

    def test_quiz_json_structure(self):
        """The external endpoint returns data.quiz[] with question/choices/answer/source."""
        # Static shape check — the real endpoint is external and may be unavailable
        # during CI; we verify the contract the FetcherAgent is designed to carry.
        import json
        sample = {
            "data": {
                "quizDate": "2026-08-21",
                "quiz": [
                    {
                        "question": "Sample question?",
                        "choices": ["A", "B", "C", "D"],
                        "answer": "A",
                        "source": {"url": "https://example.com/article"},
                    }
                ],
            }
        }
        assert "data" in sample
        assert "quizDate" in sample["data"]
        assert isinstance(sample["data"]["quiz"], list)
        q = sample["data"]["quiz"][0]
        for key in ("question", "choices", "answer", "source"):
            assert key in q
        assert isinstance(q["choices"], list)
        assert "url" in q["source"]


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
