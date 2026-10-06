"""One live Gemini request, by hand only. Never part of the test suite.

    .\\.venv\\Scripts\\python.exe -m backend.scripts.gemini_smoke_test --yes

Sends ONE tiny zoom-planning request (two invented English sentences, no
video, no audio, no project data) to the configured model using
GEMINI_API_KEY from the environment, with no retries, and checks that the
structured answer passes the same validation the app uses. Prints success or
a sanitised error — never the key.
"""

import argparse
import sys

from backend import gemini, zoom_planner
from backend.config import get_zoom_provider_settings, load_env_file

SAMPLE = [
    {"start": 0.2, "end": 2.4, "text": "Most people quit their goals within a week."},
    {"start": 2.8, "end": 5.6, "text": "Here is the one trick that kept me going."},
]
DURATION = 6.0
FPS = 30.0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--yes", action="store_true", help="confirm that one live request may be sent")
    arguments = parser.parse_args()
    if not arguments.yes:
        print("Not sent. Re-run with --yes to send one live request to Gemini.")
        return 2

    load_env_file()
    settings = get_zoom_provider_settings()
    if settings["provider"] != gemini.GEMINI:
        print("VIDEO_FACTORY_ZOOM_PROVIDER is %r, not gemini; nothing sent." % settings["provider"])
        return 2
    try:
        planner = zoom_planner.get_planner()
    except Exception as error:  # noqa: BLE001
        print("NOT READY: %s" % gemini.redact(getattr(error, "message", error)))
        return 1

    request = zoom_planner.build_request(
        duration_seconds=DURATION, fps=FPS, width=1080, height=1920,
        segments=SAMPLE, language="en", clips=None, preference="balanced",
    )
    print("Model: %s. Sending one request (no retries)…" % settings["model"])
    try:
        outcome, _ = gemini.call_with_retries(
            lambda: planner.complete(zoom_planner.user_message(request)), max_attempts=1
        )
    except gemini.GeminiError as error:
        print("FAILED (%s): %s" % (error.kind, gemini.redact(error.message)))
        return 1

    plan, errors = zoom_planner.validate_plan(outcome["data"], duration_seconds=DURATION, fps=FPS)
    usage = outcome.get("usage") or {}
    if plan is None:
        print("CONNECTED, but the answer failed validation: %s" % "; ".join(errors[:3]))
        return 1
    print(
        "SUCCESS: structured plan accepted (%d interval(s); tokens: %s)."
        % (len(plan["intervals"]), usage.get("total_token_count", "unknown"))
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
