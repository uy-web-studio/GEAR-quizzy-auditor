# Quizzy Auditor — Design Spec

**Hackathon:** All Things Agentic (devpost) — Taskmaster track
**Status:** Deployed and running (Cloud Run + Cloud Scheduler, hourly checks 09:00-12:00 America/Los_Angeles until the day's audit resolves — see §4's no-quiz retry policy)
**Author:** Donovan + Claude, 2026-08-20

## 1. Problem & context

`quizzy-news-service` is a pre-existing, currently-live Firebase Cloud
Functions (Gen2) app that generates a 5-question daily news quiz using
NewsAPI + Gemini (`@google/generative-ai`), and serves it from Firestore
over HTTP endpoints (`us-central1`, `*.run.app` domains — Gen2 Firebase
Functions run on Cloud Run under the hood). It has no quality gate: bad
phrasing, answer/choice mismatches, or factually wrong questions ship
straight to the quiz app.

A parallel, currently-manual process exists to catch this: a Claude Code
skill (`news-quiz-qc`) encodes editorial QC rules (banned "according to
the article" phrasing, exact-match answer/choices check, 3–5 choice count,
fact-check against the source URL), and a Claude Code subagent
(`quiz-auditor-notifier`) fetches the day's quiz, applies those rules, and
emails a report to `donovanuy@gmail.com` via local `mailx` — but only when
a human starts a Claude Code session and invokes it. There is an existing
TODO to make this run automatically in the cloud.

**This spec is for a newly-built system for the hackathon submission**:
an autonomous agent, built fresh during the submission period (Aug 3–31,
2026) on Google ADK + Gemini, that performs this audit on a schedule with
no human trigger, and publishes a hosted report.

### Disclosure (pre-existing work incorporated, per hackathon rules)
- `quizzy-news-service`'s live quiz-generation endpoints are treated as
  an **external system this agent consumes** — not rebuilt, not modified,
  not part of the new codebase. Only `GET`s against its public
  `getDailyGemini` endpoint are made.
- The editorial rule *content* (banned phrasing list, exact-match rule,
  choice-count rule) from the `news-quiz-qc` skill is reused as the basis
  for the new agent's audit instructions — this is prior written work
  being incorporated and will be disclosed in the submission per the
  "must disclose pre-existing work" rule.
- No code from `qz-news-agents` or `quizzy-news-service` is copied into
  the new repo. The new repo is 100% freshly written ADK/Python.

## 2. Goals / non-goals

**Goals**
- Run unattended, daily, with zero human trigger (Cloud Scheduler →
  Cloud Run).
- Reproduce and improve on the existing manual audit: phrasing check,
  answer/choice consistency, choice-count check, source fact-check.
- Persist audit history (not just a one-off email) so there's a hosted
  URL to demo and something for judges to click through without waiting
  for a scheduled run.
- Notify a human when a quiz has failing questions.
- Demonstrate genuine multi-step agentic behavior (not a single prompt
  call) using ADK's workflow primitives, and use Gemini's built-in
  Google Search grounding for the fact-check step as the "why an agent
  and not a script" story for judges.

**Non-goals (v1, cut if time is short)**
- Auto-fixing/rewriting failing questions and pushing corrections back
  into `quizzy-news-service` (that service has no write/override
  endpoint for this; adding one would mean modifying pre-existing code,
  which we're avoiding). v1 is read + audit + report, not closed-loop
  correction.
- Auth/multi-tenant support — single owner (Donovan), single quiz
  source.
- Historical backfill/audit of all past quizzes — v1 audits going
  forward from first deploy.

## 3. Architecture

```
Cloud Scheduler (daily cron, e.g. 09:00 America/Los_Angeles)
        │  HTTPS POST (OIDC-authenticated)
        ▼
Cloud Run service: quizzy-auditor
  ┌─────────────────────────────────────────────────────┐
  │ ADK SequentialAgent "daily_audit_pipeline"           │
  │                                                       │
  │  1. FetcherAgent (tool-only, no LLM)                 │
  │     tool: fetch_quiz()                               │
  │       → GET https://getdailygemini-mgpsab4ctq-uc     │
  │         .a.run.app                                   │
  │                                                       │
  │  2. AuditorAgent (LlmAgent, Gemini 3.5+)             │
  │     instructions: ported news-quiz-qc rules          │
  │     tools:                                           │
  │       - google_search (ADK built-in grounding tool,  │
  │         used per-question against source.url domain  │
  │         to verify the claimed fact)                  │
  │     output: structured per-question                  │
  │       {approved: bool, review: str|null}              │
  │       via ADK output_schema (Pydantic)                │
  │                                                       │
  │  3. ReporterAgent (tool-only, no LLM)                 │
  │     tools:                                            │
  │       - save_report()  → Firestore                    │
  │       - notify()       → email if any question failed │
  └─────────────────────────────────────────────────────┘
        │
        ├── Firestore (new project's own DB)
        │     collection `audits`, doc id = quizDate
        │     { quizDate, questions[], summary: {total,     │
        │       approved}, generatedAt, model }
        │
        └── Cloud Run: GET / and GET /reports/{date}
              small server-rendered HTML dashboard reading
              from Firestore — this is the "hosted project
              URL" for the Devpost submission
```

Everything above the two external touchpoints (the `GET` to
`quizzy-news-service`, and outbound email) is new code written during
the submission period.

### Why ADK's `SequentialAgent` instead of one big prompt
Maps 1:1 onto the existing three-step manual process (fetch → audit →
notify), keeps the fact-checking tool call (Google Search grounding)
scoped to the step that needs it, and gives a legible demo-video
narration: three named agents, each with one job, chained by the
framework rather than hand-rolled control flow.

### Notification channel
Email via **SendGrid free tier** (100 emails/day, no domain verification
required for a single sender) rather than Gmail API OAuth, to avoid
adding an interactive auth flow to a background job. Sends only when
`summary.approved < summary.total` (i.e., only on failures) to
`donovanuy@gmail.com`.

### Model
**Confirmed (Day 1, 2026-08-21): `gemini-3.7-flash`** — GA as of
2026-08-13, satisfies the hackathon's "3.5 or newer" requirement, and is
Google's current flagship for "coding and agents." Called via the
`google-genai` SDK as ADK's underlying model client.

**Location gotcha:** the entire Gemini 3.x family (`gemini-3-flash`,
`gemini-3.5-flash`, `gemini-3.6-flash`, `gemini-3.7-flash`) is only
served from Vertex AI's **`global`** location, not region-specific
locations like `us-central1` — confirmed by direct `generateContent`
probing after `gemini-3.7-flash` 404'd in `us-central1`. `gemini-2.5-flash`
and earlier are available in both. Every agent/service in this project
must set `GOOGLE_CLOUD_LOCATION=global`, independent of which region the
Cloud Run *service itself* is deployed to (those are unrelated — a Cloud
Run service in `us-central1` can call Vertex AI's `global` endpoint fine).

## 4. Data contracts

**Input** (from existing `getDailyGemini` endpoint — unmodified, external):
```json
{
  "data": {
    "quizDate": "2026-08-21",
    "quiz": [
      {"question": "...", "choices": ["...","...","...","..."],
       "answer": "...", "source": {"url": "https://..."}}
    ]
  }
}
```

**Output** (new Firestore doc, `audits/{quizDate}`):
```json
{
  "quizDate": "2026-08-21",
  "generatedAt": "2026-08-21T09:00:12Z",
  "model": "<gemini model id>",
  "status": "complete",
  "questions": [
    {"question": "...", "approved": true, "review": ""},
    {"question": "...", "approved": false,
     "review": "Answer 'Paris' not present in choices array."}
  ],
  "summary": {"total": 5, "approved": 4, "failed": 1}
}
```

**No-quiz retry policy:** if FetcherAgent/AuditorAgent come back with zero
questions (upstream `quizzy-news-service` hadn't published yet, or errored),
ReporterAgent does *not* immediately alert — a genuinely empty result looks
identical to a temporary "not published yet" gap. Instead it writes
`status: "empty"` with a `fetchAttempts` counter, and Cloud Scheduler
re-triggers `/trigger-audit` hourly (`0 9-12 * * *`, 4 checks total).
`/trigger-audit` no-ops once today's doc is `status: "complete"` or
`"empty_final"`, so each hourly firing either does real work or costs one
cheap Firestore read. Only on the 4th consecutive empty check does
ReporterAgent write `status: "empty_final"` and send the no-quiz alert email
(`send_no_quiz_alert` in `sendgrid_dispatch.py`) — so a single missed/late
publish never pages anyone, only a quiz that's absent for ~3+ hours does.
The dashboard and `/reports/{date}` render `status: "empty"/"empty_final"`
distinctly from "all questions passed" (0 total ≠ 0 failures).

## 5. Audit rules (ported from `news-quiz-qc`, unchanged in substance)

1. **Phrasing** — reject meta-referential phrasing ("according to the
   article", "as mentioned in the news source", "a news article...",
   "according to <outlet>..."); direct or named-expert framing is
   allowed.
2. **Answer/choice integrity** — `answer` must be an exact, case-sensitive
   match to one entry in `choices`.
3. **Choice count** — 3–5 total choices.
4. **Fact-check** — use Google Search grounding against `source.url`'s
   domain/topic to verify the question and correct answer are factually
   supported.

## 6. Deployment

- New, dedicated GCP project (e.g. `quizzy-auditor-hackathon`), kept
  separate from the pre-existing Firebase project backing
  `quizzy-news-service` (project id `qz-news`) — the new agent only ever
  makes outbound `GET`s to that project's public endpoints, never reads
  or writes its Firestore/GCP resources directly.
- APIs enabled: Cloud Run, Cloud Scheduler, Firestore, Vertex AI /
  Gemini API (`aiplatform.googleapis.com`), Cloud Build (for container
  deploys), **Secret Manager (`secretmanager.googleapis.com`) —
  separate API, does not come enabled with the others, enable it
  explicitly.**
- `Dockerfile` + Cloud Build → Cloud Run (Python 3.12, ADK, `google-genai`,
  `google-cloud-firestore`, `sendgrid`).
- Cloud Scheduler job: daily HTTP trigger with OIDC auth to the Cloud
  Run service's `/run` endpoint (service is otherwise unauthenticated
  for the dashboard `GET` routes, authenticated for the trigger route).
- Secrets (SendGrid key, any API keys) in Secret Manager, mounted as env
  vars.

### Deployment gotchas learned from the Day 1 hello-world spike (all
confirmed live against `quizzy-auditor-hackathon`; assume these apply to
BestBeagle Agent too — see its own SPEC.md)

1. **New projects no longer auto-grant the default Compute Engine service
   account (`<PROJECT_NUMBER>-compute@developer.gserviceaccount.com`) the
   `Editor` role.** Both the Cloud Build step and the running Cloud Run
   service use this account by default, and it starts with *zero* roles.
   Explicitly grant:
   - `roles/cloudbuild.builds.builder` — needed for `gcloud run deploy
     --source` (and `adk deploy cloud_run`) to read the uploaded source
     from the auto-created `run-sources-*` GCS bucket during the build.
     Without it: `storage.objects.get` permission errors mid-deploy.
   - `roles/aiplatform.user` — needed for the *running* service to call
     Vertex AI/Gemini at all. Without it: `403 PERMISSION_DENIED` on
     `aiplatform.endpoints.predict` at request time, not deploy time (the
     deploy itself succeeds, so this fails silently until first use —
     check for it explicitly during testing).
   - `gcloud run deploy`'s `--build-service-account` flag will **not**
     accept the legacy per-project Cloud Build SA
     (`<PROJECT_NUMBER>@cloudbuild.gserviceaccount.com`) — the newer
     Build API rejects it ("provide a user-managed service account or
     leave unset"). Leave the flag unset and grant roles to the default
     compute SA instead, as above, rather than fighting this.
2. **`adk deploy cloud_run` does not carry the local `.env` file into the
   deployed service.** The scaffolded `.env` (`GOOGLE_GENAI_USE_ENTERPRISE`,
   `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION`) is dev-only; the
   deployed Cloud Run service starts with **no env vars at all** unless
   set explicitly via `gcloud run services update --update-env-vars=...`
   (or the equivalent flags on `adk deploy cloud_run` itself, if using
   them — not yet done here). Missing `GOOGLE_CLOUD_LOCATION` silently
   falls back to `us-central1`, which then 404s/403s against Gemini 3.x
   models (see §3's location gotcha) — the failure mode looks like a
   permissions problem, not a missing-env-var problem, so check env vars
   first.
3. **In-memory session state does not survive a new revision.** Every
   `gcloud run services update` (e.g. to fix env vars) creates a new
   revision, which resets ADK's default in-memory `SessionService` —
   any session id created against the old revision 404s
   (`"Session not found"`) against the new one. Not a bug, just a trap
   during iterative debugging; recreate the session after any redeploy.
   For the real Quizzy Auditor pipeline (single scheduled run per day,
   no persistent chat session), this doesn't matter — but it will matter
   for BestBeagle Agent's long-running pipeline jobs, which need
   Firestore-backed session state for exactly this reason (already
   planned in its spec, §3 "ADK session state is persisted to
   Firestore").
4. **Vertex AI publisher-model existence isn't discoverable via a simple
   `GET`/list call** with normal IAM — `GET .../publishers/google/models`
   and `GET .../publishers/google/models/<id>` both 403/404 even with a
   project owner token. The reliable way to confirm a model id + region
   combination is live is a real `generateContent` POST probe (200 vs.
   404/403), not a discovery/list API call.
5. **`--allow-unauthenticated` silently no-ops under a "Domain Restricted
   Sharing" org policy (`iam.allowedPolicyMemberDomains`).** New GCP
   orgs tied to a Workspace domain commonly have this set, which blocks
   `allUsers`/`allAuthenticatedUsers` from ever being granted IAM roles —
   `gcloud run deploy --allow-unauthenticated` and
   `gcloud run services add-iam-policy-binding ... --member=allUsers`
   both fail (or appear to succeed while the binding never lands) with
   no obvious error pointing at the real cause; the symptom is every
   route, including intentionally-public dashboard `GET`s, returning
   `403`. Fix: override the constraint to `Allow All` scoped to just
   this project (Org Policies page in the console, or
   `gcloud resource-manager org-policies set-policy` with a
   `listPolicy: {allValues: ALLOW}` YAML, `--project=<id>` — do **not**
   apply org-wide). Setting the org policy itself requires
   `roles/orgpolicy.policyAdmin`, which `roles/resourcemanager
   .organizationAdmin` does **not** include by default — an org admin
   may need to self-grant `orgpolicy.policyAdmin` on the project first.
   App-level route auth (e.g. this service's own OIDC check on
   `/trigger-audit` in `main.py`) still works independently of this —
   Cloud Run IAM is all-or-nothing per service with no per-route
   granularity, so "public reads, authenticated writes" requires both
   the org policy allowing `allUsers` *and* the app doing its own
   per-route check.

## 7. Cost / budget (runtime, not dev-time)

Trivial: 1 scheduled run/day, 5 questions/run, ≤10 Gemini calls/day
(1 per question + fetch/report overhead), 1 Firestore write/day, ≤1
email/day. Comfortably inside free tiers (Cloud Run, Cloud Scheduler,
Firestore, Gemini free quota, SendGrid free tier). No cost concern for
the demo period or beyond.

## 8. Testing / success criteria

- Unit tests for each rule against fixture quizzes (including the real
  `qz-news-agents/reviews/original_quiz.json` sample already on disk, as
  a known-good regression fixture — disclosed as pre-existing test data,
  not code).
- One end-to-end manual trigger against the live `getDailyGemini`
  endpoint before recording the demo video, to confirm the full chain
  (fetch → audit → Firestore write → dashboard render → conditional
  email) works against real data.
- Demo-video script: trigger the Cloud Scheduler job manually, show the
  Cloud Run logs streaming through the 3 agent steps, show the Firestore
  doc land, show the dashboard update, show the email (or its absence
  when all questions pass).

## 9. Submission checklist mapping

- Hosted URL → Cloud Run dashboard (`GET /`)
- Repo → new GitHub repo, ADK + Gemini + Cloud Run/Scheduler/Firestore
- Architecture diagram → the ASCII block above, rendered
- Demo video → script in §8
- Track → Taskmaster (unattended multi-step workflow automation)
