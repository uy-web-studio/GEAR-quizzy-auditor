# Quizzy Auditor — Admin Dashboard: Editable Rules, Dry Runs, Historical Editing, Report Transparency

**Status:** Approved design, not yet implemented
**Author:** Donovan + Claude, 2026-08-30

## 1. Problem & context

The dashboard (`main.py`) is currently pure read-only server-rendered HTML with
zero auth — every route is public. The auditor's rubric is one hardcoded
prompt string (`AUDITOR_INSTRUCTION` in `daily_audit_pipeline/agent.py`) that
can only be changed by editing code and redeploying. Firestore only stores
audit *results* (`audits/{date}`) — the raw quiz content that was actually
audited (question/choices/answer/source) is fetched, used once, and
discarded, so there is nothing to retroactively re-audit or inspect against.
Per-question fact-checking (rule 4, via the `google_search` tool) happens for
real but its results — what was searched, what sources were found — are
never captured or surfaced anywhere.

This spec covers four related asks from the operator:

1. Edit the auditor's rules from the dashboard, with a dry-run to preview
   changes before they take effect.
2. Retroactively re-audit and/or manually edit historical quiz data from the
   dashboard.
3. Report-level transparency into fact-checking: what sources were used, per
   question.
4. See the quiz questions (and MCQ choices) themselves in the report, with
   any choice-integrity violation called out explicitly — without revealing
   which choice is correct.

## 2. Goals / non-goals

**Goals:**
- Admin-only editing of the auditor's rule prompt, with a safe preview
  (dry-run) before committing.
- Raw quiz content persisted going forward, enabling historical dry-runs,
  retroactive re-audits, and manual corrections.
- Every question's report shows its choices, an explicit answer-integrity
  check result, and a review note — pass or fail, not just on failure.
- Real fact-check transparency: actual search queries/sources from the
  `google_search` tool calls, not fabricated ones — cross-validated against
  what the model claims it used per question.
- All edits (rule changes, manual overrides, raw content edits, re-audits)
  logged to a revision history.

**Non-goals:**
- Backfilling raw quiz data for dates before this ships (confirmed
  impossible — see §3).
- Multi-user roles/permissions (single allowlisted admin email for now; see
  §4, trivially extensible later).
- True per-tool-call attribution of grounding to a specific question (not
  structurally available — see §7's hybrid approach instead).
- Editing the daily quiz's *correct answer* visibility in the public report
  (never shown, admin or not, per the operator's ask).

## 3. Confirmed limitation: no historical backfill

Probed `quizzy-news-service`'s public endpoint
(`getdailygemini-mgpsab4ctq-uc.a.run.app`) directly — no path, `?date=`,
`?quizDate=`, and `/{date}` path variants all returned byte-identical
content (today's quiz only; confirmed via `quizDate` in the response
matching the actual current date). The service only ever serves "today" and
has no historical lookup. Per `SPEC.md`'s existing disclosed scope
("the new agent only ever makes outbound GETs to that project's public
endpoints, never reads or writes its Firestore/GCP resources directly"),
reading `quizzy-news-service`'s own Firestore directly is out of bounds —
this project doesn't have access to it and won't seek it out.

**Consequence:** raw quiz storage (§6) only starts capturing data from
whenever it ships. 2026-08-27/28/29 (and any date before this feature is
live) can never be retroactively dry-run-tested or re-audited — there is no
raw content to work from. Manual override of pass/fail (§6) still works on
those dates, since it only touches the already-stored audit result.

Side finding while probing: the upstream payload already carries `title`,
`ambiguity`, and its own `approved` flag per question, alongside
`question`/`choices`/`answer`/`source` — none of which are currently
persisted. Worth capturing in `quizzes/{date}` (§6) since it's already in
the payload.

## 4. Auth

**Approach:** app-level Google Sign-In, not a new IAM/proxy layer.

- Google Identity Services (GIS) "Sign In With Google" button, added to the
  dashboard topbar. Requires creating an OAuth 2.0 Client ID in GCP Console
  (APIs & Services → Credentials → Create Credentials → OAuth client ID →
  Web application; authorized JS origin = the Cloud Run service URL) —
  **manual one-time setup step**, same category as the SendGrid sender
  verification done earlier this project.
- On sign-in, the browser gets a Google ID token (JWT) via GIS's callback;
  POSTs it to `POST /admin/login`.
- Backend verifies the token via `google.oauth2.id_token.verify_oauth2_token`
  (already a dependency, already used identically for Cloud Scheduler's OIDC
  token in `verify_cloud_scheduler_request`) against `GOOGLE_OAUTH_CLIENT_ID`,
  checks `email_verified` and that `email` is in `ALLOWED_ADMIN_EMAILS`.
- On success, issues a signed session cookie: `HttpOnly`, `Secure`,
  `SameSite=Strict`, containing `email:expiry_ts:hmac_signature` (HMAC-SHA256
  over `email:expiry_ts` with a server secret). Stateless — no server-side
  session store needed, works fine across multiple Cloud Run instances.
  12-hour expiry.
- `POST /admin/logout` clears the cookie.
- New `require_admin(request: Request) -> str` FastAPI dependency (returns
  the verified email or raises 401) gates every new write/admin-only route
  and strips admin-only UI affordances from server-rendered HTML when the
  request has no valid session — public pages genuinely omit admin controls
  server-side for anonymous viewers, not just hide them with CSS.

**Config:**
- `GOOGLE_OAUTH_CLIENT_ID` env var (public value, safe client-side).
- `ALLOWED_ADMIN_EMAILS` env var, default `donovanuy@gmail.com` (same
  address as the existing `RECIPIENT_EMAIL` default). Implemented as a
  comma-split list even though it holds one address today, so adding a
  second admin later is a config change, not a code change.
- `SESSION_SECRET` — new Secret Manager secret `admin-session-secret`
  (random value, e.g. `openssl rand -hex 32`), fetched the same way
  `get_sendgrid_api_key()` fetches the SendGrid key. The existing
  project-level `roles/secretmanager.secretAccessor` grant in `deploy.sh`
  already covers any new secret — no `deploy.sh` IAM changes needed, just
  creating this one secret once.

## 5. Rule editing + dry run

- New Firestore doc `config/auditor_rules`: `{instruction, updatedAt,
  updatedBy}`. Seeded from the current hardcoded `DEFAULT_AUDITOR_INSTRUCTION`
  (renamed from `AUDITOR_INSTRUCTION`) the first time it's read and no doc
  exists yet.
- `daily_audit_pipeline/agent.py` refactor: `auditor_agent` (module-level
  singleton) becomes `build_auditor_agent(instruction: str) -> LlmAgent`, and
  `root_agent` becomes `build_daily_pipeline(instruction: str) ->
  SequentialAgent` (constructs `FetcherAgent` + `build_auditor_agent(...)` +
  `ReporterAgent` fresh per call). `main.py`'s `/trigger-audit` reads
  `config/auditor_rules` (falling back to the default) and calls
  `build_daily_pipeline()` instead of importing a fixed `root_agent`.
- New auth-gated page `GET /admin`: textarea pre-filled with the current
  saved instruction, plus:
  - **Save** — `POST /admin/rules` `{instruction}`. Writes
    `config/auditor_rules`, logs a revision (§9), redirects back to `/admin`.
  - **Dry Run** — `POST /admin/dry-run` `{instruction, target: "today" |
    "<date>"}`. Runs *only* the auditor step (not Fetcher, not Reporter)
    against either a fresh live fetch ("today") or a stored
    `quizzes/{date}` (a historical date — populated in Phase 2, see §10)
    using a throwaway `InMemorySessionService`/`Runner` scoped to the
    request. Renders results inline in the same page (full-page POST +
    re-render, matching the codebase's existing all-server-rendered style —
    no new client JS beyond what Google Sign-In itself requires). **Writes
    nothing to Firestore, sends no email.**
  - The target selector's historical-date options come from
    `GET /admin/quizzes` (auth-gated), listing `quizzes` collection doc IDs
    — Phase 2 only; Phase 1 ships with "today" as the only dry-run target.
- Shared helper: `daily_audit_pipeline/fetcher.py` gains a standalone
  `async def fetch_quiz() -> dict` (parses and returns `{quizDate, quiz:
  [...]}`), called by both `FetcherAgent._run_async_impl` (which
  re-serializes it to text for the next turn, preserving current behavior)
  and the dry-run "today" path — avoiding duplicated fetch/parse logic.

## 6. Historical data: raw storage, retroactive re-audit, manual override, raw editing

*(Phase 2 — depends on raw storage accumulating; see §10)*

- New collection `quizzes/{date}`, written by `FetcherAgent` right after
  every fetch, regardless of audit outcome:
  ```json
  {
    "quizDate": "2026-08-30",
    "fetchedAt": "2026-08-30T09:00:03Z",
    "questions": [
      {"question": "...", "choices": ["...", "...", "..."], "answer": "...",
       "source": {"name": "...", "url": "..."}, "title": "...",
       "ambiguity": "0.00", "upstreamApproved": true}
    ]
  }
  ```
- **Re-audit a past day** — `POST /admin/reaudit/{date}` (auth). Requires
  `quizzes/{date}` to exist (404 with a clear message otherwise) and
  `audits/{date}.status` to already be `complete` or `empty_final` (§8's
  safety rule). Runs the auditor step (using the *currently saved* rule
  instruction — not an ad-hoc override; overrides belong to dry-run, not to
  a real audit-of-record action) against the stored raw questions, and
  overwrites `audits/{date}` via a shared `save_audit_report(db, date,
  report_doc)` function (extracted from `ReporterAgent`'s existing
  Firestore-write step so both the real daily pipeline and this endpoint use
  the same code). Logs a revision (`type: reaudit`).
- **Manual override** — `PATCH /admin/reports/{date}/questions/{index}`
  (auth) `{approved, review}`. Works on *any* date with an existing
  `audits/{date}` doc, including 08-27/28/29, since it only touches the
  stored audit result. Firestore doesn't support atomic per-index array
  updates, so this is read-modify-write: fetch `audits/{date}`, mutate the
  `questions[index]` entry in Python, recompute `summary.approved/failed`,
  write the whole doc back. Logs a revision (`type: manual_override`,
  `target: {date, questionIndex}`).
- **Edit raw quiz content** — `PUT /admin/quizzes/{date}/questions/{index}`
  (auth) `{question, choices, answer, source}`. Only for dates with stored
  raw data. Same read-modify-write pattern against `quizzes/{date}`. Does
  **not** auto-recompute `audits/{date}` — editing raw content and
  re-auditing are separate, explicit actions, so a correction and its
  re-scored result stay visibly decoupled rather than silently coupled.
  Logs a revision (`type: raw_edit`).

## 7. Report transparency

Applies to **every** audit result — the real daily pipeline (Phase 1) *and*
dry-run output — not just historical tooling.

**Schema (`daily_audit_pipeline/schemas.py`) — `QuestionAudit` gains:**
```python
class QuestionAudit(BaseModel):
  question: str
  choices: list[str]                 # NEW — shown in report; correct one never revealed
  answer_matches_choice: bool        # NEW — explicit rule-2 result, shown as a check/cross
  approved: bool
  review: str                        # CHANGED — always populated, not just on failure
  sources_checked: list[str] = []    # NEW — self-reported source URL(s) used for this question
```
`review` example on a pass: *"Phrasing OK, answer matches a choice, 3 choices
present, source supports the claim."*

**Real fact-check data — the hybrid approach:**

True per-tool-call attribution to a specific question isn't structurally
available: the auditor processes the whole quiz in one `LlmAgent`
invocation, and ADK/Gemini's `google_search` grounding tool calls aren't
tagged with "this was for question N." What *is* confirmed available
(verified against ADK's installed source, `llm_agent.py`): `output_schema`
and `tools` genuinely work together — `google_search` calls happen as real
intermediate turns before the schema-enforced final answer, and each turn's
real `grounding_metadata` (`web_search_queries`, `grounding_chunks` with
source URLs) is preserved on ADK `Event` objects, retrievable via
`Session.events` (confirmed field). Since `ReporterAgent` already reads
`ctx.session` for `state`, it can equally scan `ctx.session.events` for
prior turns' `grounding_metadata` — no changes needed to `main.py`'s trigger
endpoint.

So: **`ReporterAgent`** (and the dry-run endpoint, via a shared
`daily_audit_pipeline/grounding.py::extract_grounding_activity(events) ->
dict` helper) extracts the *real* aggregate grounding activity for the whole
run:
```json
"groundingActivity": {
  "searchQueries": ["...", "..."],
  "sources": [{"url": "...", "title": "...", "domain": "..."}]
}
```
Then, for each question's self-reported `sources_checked` (from the LLM's
own structured output), cross-check each URL against `groundingActivity`'s
real source list. Persist per-question sources as
`[{"url": "...", "verified": true|false}]` — `verified: false` when a
self-reported URL never actually appeared in the run's real grounding
activity. The report renders this as e.g. *"⚠ unverified — not found in
this run's search activity"* for anything that fails the check, rather than
silently trusting the model's self-report.

**Report rendering (`main.py`):** `/reports/{date}` shows every question
(not just failures) with its choices, the answer-match check, the always-
populated review, and its cross-validated sources. The correct answer is
never rendered.

## 8. Safety rule (re-audit / manual override)

`POST /admin/reaudit/{date}` and `PATCH .../questions/{index}` both mutate
the same `audits/{date}` docs the hourly scheduler writes to. To avoid a
manual edit colliding with an in-progress scheduled run, both require
`audits/{date}.status` to already be `complete` or `empty_final` (i.e.
resolved) before allowing the mutation — 409 otherwise. This naturally
excludes *today* while the scheduler might still be retrying it (per the
existing no-quiz retry policy in `SPEC.md` §4), without needing any new
locking mechanism.

## 9. Revision history

New collection `revisions`, one doc per write action:
```json
{
  "timestamp": "2026-08-30T10:00:00Z",
  "type": "rule_change | manual_override | raw_edit | reaudit",
  "actor": "donovanuy@gmail.com",
  "target": {"date": "2026-08-27", "questionIndex": 2},
  "before": {...},
  "after": {...}
}
```
Shared helper `log_revision(db, type, actor, target, before, after)`, called
as the last step of all four write paths (rule save, manual override, raw
edit, re-audit). New auth-gated page `GET /admin/history` — flat
reverse-chronological list.

## 10. Phasing

Two shippable phases, not one large change:

- **Phase 1** — auth (§4), rule editing (§5), report transparency (§7,
  applied to both the real daily pipeline and dry-run), dry-run against
  **today's live quiz only**, revision history for rule changes.
- **Phase 2** — raw quiz storage going forward, dry-run gains a historical-
  date target option, retroactive re-audit, manual override, raw content
  editing, revision history for those three additional types, the §8 safety
  rule.

Phase 2 can't really be tested until Phase 1's been live long enough to
accumulate a few days of raw quiz data anyway, so this isn't arbitrary
slicing — it's the natural dependency order.

## 11. New files / module layout

- `auth.py` (new, repo root, sibling to `main.py`) — session cookie
  signing/verification, Google ID token verification wrapper,
  `require_admin` dependency, `ALLOWED_ADMIN_EMAILS` config.
- `daily_audit_pipeline/grounding.py` (new) —
  `extract_grounding_activity(events) -> dict`, shared by `ReporterAgent`
  and the dry-run endpoint.
- `daily_audit_pipeline/revisions.py` (new) — `log_revision(...)` helper.
- `daily_audit_pipeline/agent.py` — `build_auditor_agent(instruction)`,
  `build_daily_pipeline(instruction)`, `DEFAULT_AUDITOR_INSTRUCTION`
  (renamed from `AUDITOR_INSTRUCTION`, updated to instruct the model to
  populate the new schema fields).
- `daily_audit_pipeline/reporter.py` — `save_audit_report(db, date,
  report_doc)` extracted to module level (reused by re-audit endpoint);
  grounding extraction + cross-validation added to the audited-report path.
- `daily_audit_pipeline/fetcher.py` — `fetch_quiz() -> dict` extracted;
  Phase 2 adds the `quizzes/{date}` write.
- `daily_audit_pipeline/schemas.py` — `QuestionAudit` gains `choices`,
  `answer_matches_choice`, `sources_checked`; `review` always populated.
- `main.py` — new admin routes (`/admin`, `/admin/rules`, `/admin/dry-run`,
  `/admin/login`, `/admin/logout`, `/admin/history`, and Phase 2's
  `/admin/reaudit/{date}`, `/admin/reports/{date}/questions/{index}`,
  `/admin/quizzes/{date}/questions/{index}`, `/admin/quizzes`); `/` and
  `/reports/{date}` updated to show admin affordances only for authenticated
  requests, and to render the new per-question fields from §7.

## 12. Testing

Unit tests needed (extending the existing 64-test suite, all of which must
keep passing):
- `auth.py`: cookie sign/verify round-trip, expiry rejection, wrong-email
  rejection, `require_admin` dependency (valid/missing/expired/tampered
  cookie).
- `build_auditor_agent`/`build_daily_pipeline` factories construct correctly
  with a given instruction.
- `extract_grounding_activity` against mocked ADK events carrying synthetic
  `grounding_metadata`.
- `save_audit_report` (extracted function) — same coverage the current
  inline version has, plus reuse by the re-audit endpoint.
- Re-audit/override/raw-edit endpoints: mocked Firestore, verifying
  read-modify-write correctness, revision logging, and the §8 safety-rule
  409 case.
- Dry-run endpoint: mocked fetch (today) and mocked `quizzes/{date}` read
  (historical), confirming no Firestore write and no email send occur.
- Rules save endpoint: writes `config/auditor_rules`, logs a revision.
- Report rendering: choices shown, correct answer never rendered,
  `answer_matches_choice` indicator, always-populated review, unverified-
  source flagging.

## 13. Known limitations (restated)

- No historical backfill for dates before this ships (§3) — confirmed via
  direct probing, not assumed.
- Per-question source attribution is a self-report cross-validated against
  real aggregate grounding data, not true per-tool-call attribution (§7) —
  the tooling doesn't support the latter within a single multi-question
  agent invocation.
- Single hardcoded admin email for now; trivially extensible to a list
  later (§4).
