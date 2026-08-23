#!/usr/bin/env python3
"""Test script for SendGrid dispatch functionality.

Usage:
  python scripts/test_sendgrid_dispatch.py --dry-run
  python scripts/test_sendgrid_dispatch.py --send (requires valid SendGrid key)
"""

import asyncio
import os
import sys
from argparse import ArgumentParser
from datetime import datetime
from pathlib import Path

# If dependencies are missing from current python environment, auto-reexec with .venv python
try:
  import sendgrid  # noqa: F401
  import google.cloud  # noqa: F401
except ModuleNotFoundError:
  repo_root = Path(__file__).resolve().parent.parent
  venv_python = repo_root / ".venv" / "bin" / "python"
  if venv_python.exists() and sys.executable != str(venv_python):
    os.execv(str(venv_python), [str(venv_python)] + sys.argv)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from daily_audit_pipeline.sendgrid_dispatch import send_audit_report


async def run_test(dry_run: bool = True):
  """Run SendGrid dispatch test."""
  quiz_date = datetime.now().strftime("%Y-%m-%d")

  failed_questions = [
      {
          "question": "According to the article, what was the main topic?",
          "review": "Rule 1 (Phrasing): Meta-referential phrasing 'according to the article' is forbidden.",
      },
      {
          "question": "Which of these is the correct answer?",
          "review": "Rule 2 (Answer/Choice Integrity): Answer 'Option A' doesn't match any choice exactly.",
      },
  ]

  print(f"Testing SendGrid dispatch (dry_run={dry_run})...")
  print(f"Quiz Date: {quiz_date}")
  print(f"Total Questions: 5")
  print(f"Approved: 3")
  print(f"Failed: 2")
  print()

  result = await send_audit_report(
      recipient_email="test@example.com",
      quiz_date=quiz_date,
      total_questions=5,
      approved_count=3,
      failed_questions=failed_questions,
      dry_run=dry_run,
  )

  print("SendGrid Response:")
  for key, value in result.items():
    if key == "html_preview":
      print(f"  {key}: {value}")
    else:
      print(f"  {key}: {value}")

  if result.get("status") == "error":
    print(f"\n❌ Error: {result.get('error')}")
    return 1

  print(f"\n✅ Test {'dry_run' if dry_run else 'send'} passed!")
  return 0


def main():
  parser = ArgumentParser(description="Test SendGrid dispatch")
  parser.add_argument(
      "--dry-run",
      action="store_true",
      default=False,
      help="Run in dry-run mode (don't actually send email)",
  )
  parser.add_argument(
      "--send",
      action="store_true",
      default=False,
      help="Actually send the email (requires valid SendGrid key)",
  )
  args = parser.parse_args()

  dry_run = not args.send

  try:
    exit_code = asyncio.run(run_test(dry_run=dry_run))
    sys.exit(exit_code)
  except Exception as e:
    print(f"❌ Test failed: {e}")
    import traceback

    traceback.print_exc()
    sys.exit(1)


if __name__ == "__main__":
  main()
