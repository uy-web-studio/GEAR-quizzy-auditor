# Quizzy Auditor

Autonomous daily QC agent for [`quizzy-news-service`](https://github.com/dceu/quizzy-news-service)'s
generated news quiz, built on Google's Agent Development Kit (ADK) and
Gemini. See [`SPEC.md`](SPEC.md) for the full design spec, architecture,
and hackathon disclosure notes.

## Architecture (summary)

```
Cloud Scheduler (daily) -> Cloud Run: ADK SequentialAgent "daily_audit_pipeline"
  1. FetcherAgent  (tool-only)  -> GET the day's quiz from quizzy-news-service
  2. AuditorAgent  (LlmAgent)   -> gemini-3.7-flash, rubric + google_search fact-check
  3. ReporterAgent (tool-only)  -> Firestore + SendGrid (deployed)
```

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
uv pip install google-adk
```
</details>

<details>
<summary><strong>Windows (PowerShell)</strong></summary>

```powershell
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
uv venv
.venv\Scripts\activate
uv pip install google-adk
```
</details>

<details>
<summary><strong>Don't have/want uv? Plain venv works too</strong></summary>

```bash
python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install google-adk
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
daily_audit_pipeline/
  agent.py       # AuditorAgent + root SequentialAgent
  fetcher.py     # FetcherAgent (tool-only)
  schemas.py     # QuestionAudit output schema
SPEC.md          # full design spec
```
