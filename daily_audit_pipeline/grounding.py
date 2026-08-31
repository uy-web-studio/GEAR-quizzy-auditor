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
