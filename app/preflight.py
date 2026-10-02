"""Report why CloudPulse will not boot, before gunicorn gives up.

Render says "Your service is live" the moment the container starts, then serves
"the page isn't working right now" because the worker exited. The reason is in
the log, where the person deploying is unlikely to look.

This runs at boot instead and prints a short, actionable block: what is missing,
and the exact click to fix it. It never relaxes the requirement -- a missing
signing key must still stop the boot.
"""

from __future__ import annotations

import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_HELP = """
==============================================================================
 CloudPulse did not start.

 On Render this is almost always one missing environment variable.
 CloudPulse refuses to start without a real session signing key, because a
 known key lets anyone forge a session cookie and sign in as any user.

 FIX -- Render dashboard -> your service -> Environment -> Add:

     Key:    SECRET_KEY
     Value:  press the "Generate" button (do not type one by hand)
     Save, then hit "Manual Deploy".

 Optional, and worth setting at the same time:

     Key:    APP_ENV                 Value: production
     Key:    AUTO_CREATE_SCHEMA      Value: true
     Key:    SESSION_COOKIE_SECURE   Value: false
     Key:    ENABLE_SCHEDULER        Value: true

 To generate a key locally instead of using the button:

     python -c "import secrets; print(secrets.token_urlsafe(48))"

 It must be at least 32 characters, and it must not be any value that appears
 in this repository.
==============================================================================
"""


def report(reason: str) -> None:
    print(reason, file=sys.stderr, flush=True)
    print(_HELP, file=sys.stderr, flush=True)

    if os.getenv("RENDER"):
        print(
            "This deployment looks like Render (RENDER is set). "
            "https://dashboard.render.com",
            file=sys.stderr,
            flush=True,
        )


def main() -> None:
    from config import _looks_like_placeholder_key  # noqa: PLC0415

    key = (os.getenv("SECRET_KEY") or "").strip()

    if not key:
        report("SECRET_KEY is not set.")
        raise SystemExit(78)  # EX_CONFIG

    if _looks_like_placeholder_key(key):
        report(
            f"SECRET_KEY is set to {key!r}, which is a value published in this "
            "repository. Anyone who can read it can forge session cookies."
        )
        raise SystemExit(78)

    if len(key) < 32:
        report(f"SECRET_KEY is only {len(key)} characters; 32 is the minimum.")
        raise SystemExit(78)


if __name__ == "__main__":
    main()
