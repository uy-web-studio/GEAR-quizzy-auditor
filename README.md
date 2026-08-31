# Quizzy Auditor

Autonomous daily QC agent for [`quizzy-news-service`](https://github.com/dceu/quizzy-news-service)'s
generated news quiz, built on Google's Agent Development Kit (ADK) and
Gemini. See [`SPEC.md`](SPEC.md) for the full design spec, architecture,
and hackathon disclosure notes, and
[`docs/superpowers/specs/2026-08-30-admin-dashboard-design.md`](docs/superpowers/specs/2026-08-30-admin-dashboard-design.md)
for the admin dashboard's design spec.

**Live:** [quizzy-auditor-r34ubsveba-uc.a.run.app](https://quizzy-auditor-r34ubsveba-uc.a.run.app)

## Problem

`quizzy-news-service` generates a 5-question daily news quiz from NewsAPI +
Gemini and serves it straight to users — with no quality gate. Bad
phrasing, an answer that doesn't match any of its own choices, or a
question the source article doesn't actually support can all ship as-is.
Catching this today means a human manually starting a Claude Code session
and invoking an ad-hoc QC skill — so on any day nobody remembers to run
it, nothing gets checked.

## How this addresses it

Quizzy Auditor is a small, dedicated service that fetches each day's quiz,
audits it against an editorial rubric using Gemini with real Google Search
grounding for fact-checking, and publishes the result — unattended, on a
schedule, with no human trigger required:

- **Fetch → Audit → Report**, as three ADK agents chained in a
  `SequentialAgent`, not one big prompt — see [§3 of SPEC.md](SPEC.md) for
  why that split matters for the fact-check step specifically.
- **Four rubric checks per question**: banned meta-referential phrasing,
  exact answer/choice match, 3–5 choice count, and a Google Search
  fact-check against the source article — ported from the pre-existing
  manual `news-quiz-qc` skill (disclosed, [SPEC.md §1](SPEC.md)).
- **A public dashboard** (`/`, `/reports/{date}`) so every day's result —
  including *why* each question passed or failed, its choices, and which
  fact-check sources were actually consulted (cross-validated against
  ADK's real grounding metadata, not just the model's self-report) — is a
  hosted page, not a one-off email.
- **An admin control surface** (`/admin`, Google Sign-In gated) so the
  rubric itself is editable from the dashboard, with a dry-run preview
  against today's live quiz before saving, and every change logged to a
  revision history — instead of the rubric being a hardcoded prompt string
  that needs a redeploy to change.
- **Email only on a real problem**: SendGrid fires when a question fails,
  or after four consecutive hourly checks find no quiz published at all —
  never on a normal, all-clear day.

## Architecture

```
Cloud Scheduler ──POST /trigger-audit──▶  Cloud Run: quizzy-auditor (FastAPI)
(OIDC, hourly 09:00–12:00 PT,                │
 until today's audit resolves)               │
                                              ▼
                          ADK SequentialAgent "daily_audit_pipeline"
                          ┌──────────────────────────────────────────┐
                          │ 1. FetcherAgent (tool-only)               │
                          │    GET quizzy-news-service's public       │
                          │    getDailyGemini endpoint                 │
                          │                                            │
                          │ 2. AuditorAgent (LlmAgent, gemini-3.7-flash)│
                          │    rubric loaded from config/auditor_rules │
                          │    (editable — falls back to a built-in    │
                          │    default) + google_search tool for the   │
                          │    fact-check rule                         │
                          │                                            │
                          │ 3. ReporterAgent (tool-only)                │
                          │    → Firestore: audits/{date}               │
                          │    → cross-validates self-reported sources  │
                          │      against real grounding_metadata        │
                          │    → SendGrid, only if a question failed    │
                          │      or the no-quiz retry window is spent   │
                          └──────────────────────────────────────────┘

Also on the same FastAPI app:
  GET  /, /reports/{date}   public dashboard + per-question detail
  GET  /admin               rubric editor  ┐  Google Sign-In (GIS),
  POST /admin/rules         save / dry-run │  verified server-side,
  POST /admin/login,/logout session cookie ┘  signed HMAC, HttpOnly/Secure

Firestore (own project, never quizzy-news-service's):
  audits/{date}            one doc per day's report
  config/auditor_rules     the live, operator-editable rubric
  revisions/*              append-only log of every admin edit
```

`auth.py` handles Google ID token verification and the signed session
cookie; `daily_audit_pipeline/grounding.py` extracts real
`google_search` tool-call metadata from ADK's event stream so the report's
"sources checked" aren't just the model's unverified self-report;
`daily_audit_pipeline/revisions.py` is the shared write-path for anything
an admin changes. `daily_audit_pipeline/agent.py` builds the auditor and
the full pipeline as factories (not module-level singletons) so the same
code path serves the real daily run, the admin dry-run, and — see below —
a hypothetical synchronous pre-publish check.

## Roadmap: wiring into the Quizzy.News suite

Today the relationship with `quizzy-news-service` is deliberately
one-directional and decoupled: Quizzy Auditor only ever makes outbound
`GET`s against its already-public endpoint, after the quiz has already
shipped. That was a hackathon-scope constraint, not an architectural
ideal — `quizzy-news-service` is pre-existing, disclosed prior work
([SPEC.md §1](SPEC.md)), and v1 explicitly ruled out modifying it or
writing corrections back into it ([SPEC.md §2](SPEC.md), non-goals).

The natural next step turns this from a post-hoc watchdog into a
**pre-publish gate**, and most of the machinery already exists —
`run_dry_run()` in `main.py` already builds a fresh auditor from any
instruction and runs it against a quiz payload with no Firestore write and
no email. It just needs to accept an inline payload instead of only
"today's live fetch," and be exposed as a service-to-service endpoint
instead of only the admin UI's dry-run button:

```
quizzy-news-service (Gen2 Cloud Function)
  1. generates today's 5 questions (NewsAPI + Gemini), as it does today
  2. instead of writing straight to its own public Firestore doc:

     POST https://quizzy-auditor-<hash>.a.run.app/audit   (new endpoint,
       OIDC-authenticated service-to-service, same pattern as the
       existing /trigger-audit check)
     Body: {"quizDate": "...", "quiz": [...]}   ← the exact shape it
       already holds in memory before publish; no round-trip needed

     Response: {"approved": bool, "questions": [...], "summary": {...}}
       ← run_dry_run()'s existing return shape, unchanged

  3a. all questions approved → quizzy-news-service publishes as normal
  3b. any question failed    → quizzy-news-service either:
        - regenerates just the failing questions, feeding each one's
          `review` string back into its own Gemini prompt as the
          specific defect to fix, or
        - holds publish and surfaces the failure via Quizzy Auditor's
          already-built admin dashboard, where the same rubric-editor
          Google Sign-In session becomes the shared review UI for both
          services rather than something Quizzy Auditor alone uses
```

This keeps `quizzy-news-service` genuinely unmodified except for one new
outbound call it makes on its own schedule (still no code from it copied
into or vendored by this repo), while turning the audit rubric — already
the single source of truth for grading a shipped quiz — into the same
source of truth for whether a quiz ships at all.

## Prerequisites

- Python 3.10+
- A Google Cloud project with billing enabled
- The `gcloud` CLI, authenticated

## Setup

### 1. Install the Google Cloud CLI

Pick your platform:

<details>
<summary><strong>macOS</strong></summary>

```bash
brew install --cask google-cloud-sdk
```

or the official installer:

```bash
curl https://sdk.cloud.google.com | bash
exec -l $SHELL
```
</details>

<details>
<summary><strong>Windows</strong></summary>

Download and run the installer from
[cloud.google.com/sdk/docs/install](https://cloud.google.com/sdk/docs/install),
or via `winget` in PowerShell:

```powershell
winget install --id Google.CloudSDK
```
</details>

<details>
<summary><strong>Linux — Debian / Ubuntu</strong></summary>

```bash
echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" \
  | sudo tee -a /etc/apt/sources.list.d/google-cloud-sdk.list
curl https://packages.cloud.google.com/apt/doc/apt-key.gpg \
  | sudo gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg
sudo apt-get update && sudo apt-get install -y google-cloud-cli
```
</details>

<details>
<summary><strong>Linux — Fedora / RHEL / CentOS</strong></summary>

```bash
sudo tee -a /etc/yum.repos.d/google-cloud-sdk.repo << EOM
[google-cloud-cli]
name=Google Cloud CLI
baseurl=https://packages.cloud.google.com/yum/repos/cloud-sdk-el8-x86_64
enabled=1
gpgcheck=1
repo_gpgcheck=0
gpgkey=https://packages.cloud.google.com/yum/doc/rpm-package-key.gpg
EOM
sudo dnf install -y google-cloud-cli
```
</details>

<details>
<summary><strong>Linux — Arch (and Arch-based: Manjaro, EndeavourOS)</strong></summary>

Via the AUR:

```bash
yay -S google-cloud-cli
```

The AUR build can fail with missing-file errors if it races another build
of the same package running concurrently (source tarball extraction
collision) — just retry. If it keeps failing, fall back to the official
installer script, which doesn't require a package build at all (this is
what this project's own dev setup actually used):

```bash
curl https://sdk.cloud.google.com > /tmp/gcloud_install.sh
bash /tmp/gcloud_install.sh --disable-prompts --install-dir="$HOME"

# fish shell
fish_add_path "$HOME/google-cloud-sdk/bin"
# bash/zsh — add to your rc file instead:
# export PATH="$HOME/google-cloud-sdk/bin:$PATH"
```
</details>

<details>
<summary><strong>Linux — any other distro (generic fallback)</strong></summary>

```bash
curl https://sdk.cloud.google.com | bash
exec -l $SHELL
```
</details>

### 2. Authenticate

```bash
gcloud auth login
gcloud auth application-default login
```

### 3. Set up the GCP project

```bash
gcloud projects create <YOUR_PROJECT_ID> --name="Quizzy Auditor"
gcloud billing projects link <YOUR_PROJECT_ID> --billing-account=<YOUR_BILLING_ACCOUNT_ID>
gcloud config set project <YOUR_PROJECT_ID>

gcloud services enable run.googleapis.com cloudscheduler.googleapis.com firestore.googleapis.com \
  aiplatform.googleapis.com cloudbuild.googleapis.com cloudtasks.googleapis.com \
  storage.googleapis.com pubsub.googleapis.com secretmanager.googleapis.com
```

New GCP projects no longer auto-grant the default Compute Engine service
account the `Editor` role, so grant it what it needs explicitly (see
Troubleshooting below for why):

```bash
PROJECT_NUM=$(gcloud projects describe <YOUR_PROJECT_ID> --format="value(projectNumber)")
gcloud projects add-iam-policy-binding <YOUR_PROJECT_ID> \
  --member="serviceAccount:${PROJECT_NUM}-compute@developer.gserviceaccount.com" \
  --role="roles/cloudbuild.builds.builder"
gcloud projects add-iam-policy-binding <YOUR_PROJECT_ID> \
  --member="serviceAccount:${PROJECT_NUM}-compute@developer.gserviceaccount.com" \
  --role="roles/aiplatform.user"
```

### 4. Install Python dependencies

Requires Python 3.10+. This project uses [uv](https://docs.astral.sh/uv/)
for a fast, isolated virtual environment — recommended especially on
distros (like Arch) that block system-wide `pip install`.

<details>
<summary><strong>macOS / Linux (all distros, including Arch)</strong></summary>

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
# from the repo root:
uv venv
source .venv/bin/activate
uv pip install -r requirements.txt
```
</details>

<details>
<summary><strong>Windows (PowerShell)</strong></summary>

```powershell
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
uv venv
.venv\Scripts\activate
uv pip install -r requirements.txt
```
</details>

<details>
<summary><strong>Don't have/want uv? Plain venv works too</strong></summary>

```bash
python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```
</details>

### 5. Configure environment variables

Create `daily_audit_pipeline/.env`:

```
GOOGLE_GENAI_USE_ENTERPRISE=1
GOOGLE_CLOUD_PROJECT=<YOUR_PROJECT_ID>
GOOGLE_CLOUD_LOCATION=global
```

**`global` is required, not a placeholder.** The Gemini 3.x model family
(including `gemini-3.7-flash`, used here) is only served from Vertex AI's
`global` location — regional locations like `us-central1` will 404.

For the admin dashboard specifically (`SESSION_SECRET`,
`GOOGLE_OAUTH_CLIENT_ID`, `ALLOWED_ADMIN_EMAILS`), see
[`docs/superpowers/specs/2026-08-30-admin-dashboard-design.md`](docs/superpowers/specs/2026-08-30-admin-dashboard-design.md)
§4 and `deploy.sh`'s post-deploy output.

### 6. Run it

```bash
adk run daily_audit_pipeline      # one-shot CLI run
adk web                           # interactive dev UI at http://127.0.0.1:8000
```

## Troubleshooting

Gotchas hit and documented during this project's own build (full detail
in [`SPEC.md`](SPEC.md) §6):

- **`403 PERMISSION_DENIED` on `aiplatform.endpoints.predict` at runtime** —
  the running service's service account is missing `roles/aiplatform.user`
  (see step 3 above). The deploy itself succeeds; this only shows up on
  first actual Gemini call.
- **`404` on a Gemini 3.x model** — check `GOOGLE_CLOUD_LOCATION=global`,
  not a region.
- **Deployed Cloud Run service missing env vars** — `adk deploy cloud_run`
  does not carry over the local `.env` file. Set variables explicitly with
  `--set-env-vars` on deploy, or `gcloud run services update
  --update-env-vars=...` afterward.
- **`gcloud run deploy --source` fails with a `storage.objects.get`
  permission error** — the build's service account needs
  `roles/cloudbuild.builds.builder` too (see step 3).
- **`adk run`/`adk web` says "Session not found" after a redeploy** —
  the default in-memory session service resets on every new Cloud Run
  revision. Expected; not a bug.

## Project structure

```
main.py                        FastAPI app: dashboard, admin routes,
                                /trigger-audit, /health
auth.py                        Google Sign-In verification, signed
                                session cookies, require_admin dependency
daily_audit_pipeline/
  agent.py                     AuditorAgent + daily_audit_pipeline
                                factories (build_auditor_agent,
                                build_daily_pipeline)
  fetcher.py                   FetcherAgent (tool-only) + fetch_quiz()
  reporter.py                  ReporterAgent (tool-only) +
                                save_audit_report()
  grounding.py                 extract/cross-validate real google_search
                                grounding metadata from ADK's event stream
  revisions.py                 shared revision-history logging for admin
                                writes
  sendgrid_dispatch.py         SendGrid email dispatch (failures, no-quiz
                                alert)
  schemas.py                   QuestionAudit output schema
tests/                         pytest suite (100 tests)
docs/superpowers/specs/        design specs (this dashboard, the original
docs/superpowers/plans/          agent, and their implementation plans)
SPEC.md                        original hackathon design spec + disclosure
DEPLOYMENT.md                  Cloud Run deployment reference
deploy.sh                      one-shot deploy script (build, deploy,
                                scheduler, secrets check)
```
