# Dev-only entry point for `adk web` prompt tuning — NOT part of the
# submitted pipeline. Points straight at the real auditor_agent instance
# from daily_audit_pipeline/agent.py, skipping FetcherAgent, so the rubric
# can be tuned by pasting quiz JSON directly as the first chat message
# (useful while the live getDailyGemini endpoint is down).
#
# Once the rubric is finalized here, the AUDITOR_INSTRUCTION text lives in
# daily_audit_pipeline/agent.py — this folder doesn't need to be touched
# again, and should be deleted before the pipeline is deployed/submitted.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from daily_audit_pipeline.agent import auditor_agent as root_agent  # noqa: E402
