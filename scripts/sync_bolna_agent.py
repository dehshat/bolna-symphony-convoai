"""Create or update the Bolna negotiator agents (one per language) from the command line.

    python -m scripts.sync_bolna_agent --dry-run                  # print the exact payload, no API call
    python -m scripts.sync_bolna_agent --base-url https://x.up.railway.app --languages hi,kn

The same logic backs the ⚙︎ Setup → "Create / update Bolna agents" button in the web UI, so you can
provision from an Android phone without touching Bolna's desktop dashboard.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config  # noqa: E402
from app.bolna_client import BolnaError, sync_agents  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="print the payloads without calling Bolna")
    parser.add_argument("--base-url", default=None, help="public HTTPS URL of this app (default: PUBLIC_BASE_URL)")
    parser.add_argument("--languages", default=None, help="comma list, e.g. hi,kn (default: BOLNA_AGENT_LANGUAGES)")
    args = parser.parse_args()

    base = args.base_url or config.settings.public_base_url
    if not base and not args.dry_run:
        print("Set PUBLIC_BASE_URL or pass --base-url: Bolna must be able to reach the tool webhooks.")
        return 2
    langs = [l.strip() for l in args.languages.split(",")] if args.languages else None
    try:
        result = asyncio.run(sync_agents(langs, base, dry_run=args.dry_run))
    except BolnaError as exc:
        print(f"Bolna error: {exc}")
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if result.get("env_hint"):
        print(f"\nAdd this env var to Railway/Render so the agent ids survive restarts:\n  {result['env_hint']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
