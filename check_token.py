"""Health-check for the local Google OAuth token.

Loads ``token.json``, attempts a refresh if the access portion is stale,
then makes a tiny Gmail API call to confirm the refresh token is still
honored by Google. Returns exit code 0 on success, 1 on any failure.

Run before kicking off the pipeline if you're unsure whether you still
have valid Gmail access — especially if your OAuth consent screen is in
"Testing" mode, in which case refresh tokens expire every 7 days.

Usage:
    python check_token.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

TOKEN_FILE = Path(__file__).parent / "token.json"
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def _fail(msg: str, hint: str | None = None) -> int:
    print(f"❌ {msg}")
    if hint:
        print(f"   {hint}")
    print("\nRecovery:")
    print("   rm token.json && python main.py")
    print("   gh secret set TOKEN_JSON --repo $(git config remote.origin.url \\")
    print("       | sed -E 's|.*github.com[:/]([^/]+/[^/.]+)(\\.git)?|\\1|') < token.json")
    return 1


def main() -> int:
    if not TOKEN_FILE.exists():
        return _fail("token.json not found", "Pipeline has never authenticated locally.")

    try:
        raw = json.loads(TOKEN_FILE.read_text())
    except json.JSONDecodeError as e:
        return _fail(f"token.json is malformed: {e}")

    if not raw.get("refresh_token"):
        return _fail("token.json has no refresh_token", "Token was issued without offline access; needs re-auth.")

    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
    except ImportError as e:
        return _fail(f"Required library missing: {e}", "Run: pip install -r requirements.txt")

    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    if creds.expiry:
        exp = creds.expiry.replace(tzinfo=timezone.utc) if creds.expiry.tzinfo is None else creds.expiry
        delta = exp - datetime.now(timezone.utc)
        print(f"Access token expiry: {exp.isoformat()} ({delta.total_seconds() / 60:+.0f} min)")

    if not creds.valid:
        if creds.expired and creds.refresh_token:
            print("Access token is stale; attempting refresh...")
            try:
                creds.refresh(Request())
            except Exception as e:
                return _fail(
                    f"Refresh failed: {e}",
                    "Refresh token has been revoked by Google (testing-app 7-day expiry, "
                    "password change, manual revoke, etc.).",
                )
            TOKEN_FILE.write_text(creds.to_json())
            print("✓ Refresh succeeded; token.json updated.")
        else:
            return _fail("Credentials invalid and not refreshable.")

    try:
        service = build("gmail", "v1", credentials=creds)
        profile = service.users().getProfile(userId="me").execute()
    except Exception as e:
        return _fail(f"Gmail API call failed: {e}", "Token loaded but Google rejected the request.")

    print(f"✓ Authenticated as {profile.get('emailAddress')}")
    print(f"  Total messages in mailbox: {profile.get('messagesTotal', 'unknown'):,}")
    print("\n✓ OAuth token is healthy. Pipeline should run cleanly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
