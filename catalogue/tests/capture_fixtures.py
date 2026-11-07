"""Capture real responses into the fixture corpus.

Run manually, not in CI:  python tests/capture_fixtures.py

Deliberately paced and small. The point is a handful of representative and
adversarial responses, not a crawl.
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

import httpx

FIXTURES = Path(__file__).parent / "fixtures"
UA = "DramadexBot/0.1 (self-hosted metadata indexer)"
PAUSE = 2.0


def save(name: str, resp: httpx.Response) -> None:
    path = FIXTURES / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.with_suffix(".body").write_bytes(resp.content)
    path.with_suffix(".meta.json").write_text(
        json.dumps(
            {
                "url": str(resp.url),
                "status": resp.status_code,
                "headers": {k.lower(): v for k, v in resp.headers.items()},
            },
            indent=2,
        )
    )
    print(f"  {name:44} {resp.status_code}  {len(resp.content):>8} bytes")


def main() -> int:
    client = httpx.Client(headers={"User-Agent": UA}, timeout=30, follow_redirects=True)

    home = client.get("https://www.dramabox.com/")
    match = re.search(r'"buildId":"([^"]+)"', home.text)
    if not match:
        print("could not read DramaBox buildId", file=sys.stderr)
        return 1
    build_id = match.group(1)
    print(f"DramaBox buildId: {build_id}")
    base = f"https://www.dramabox.com/_next/data/{build_id}"

    targets = [
        # HTTP 200 with an empty list -- valid, not an error, not a block.
        ("dramabox/browse_empty", f"{base}/browse/0/251.json"),
        # The page-1 __N_REDIRECT stub.
        ("dramabox/redirect_stub", f"{base}/browse/161/1.json"),
        # A normal listing page, for parser tests in M2.
        ("dramabox/browse_ok", f"{base}/browse/161.json"),
        # HTTP 404 that still contains valid-looking __NEXT_DATA__.
        ("reelshort/tags_404_page375", "https://www.reelshort.com/tags/mc-types/375"),
        # A normal listing page, for parser tests in M3.
        ("reelshort/tags_ok", "https://www.reelshort.com/tags/mc-types/1"),
    ]

    for name, url in targets:
        time.sleep(PAUSE)
        save(name, client.get(url))

    # A detail record, derived from the listing we just captured so the fixture
    # stays self-consistent. Cast and the episode manifest only exist here.
    listing = json.loads((FIXTURES / "dramabox/browse_ok.body").read_text())
    book = listing["pageProps"]["bookList"][0]
    time.sleep(PAUSE)
    save(
        "dramabox/detail_ok",
        client.get(f"{base}/drama/{book['bookId']}/{book['bookNameEn']}.json"),
    )

    # A record that actually HAS cast. Newer titles frequently ship with an
    # absent performerList, so pinning a known-cast title is the only way to
    # keep the cast path under test.
    time.sleep(PAUSE)
    save(
        "dramabox/detail_with_cast",
        client.get(f"{base}/drama/42000013483/Revenge-Puck.json"),
    )

    # ReelShort detail records. Its listing is thin -- no tags, no dates, no
    # cast -- so the detail page carries almost everything and must be pinned.
    reelshort_details = [
        # Rich: 119 episodes, alt title, cast with IMDb person ids.
        ("reelshort/detail_ok", "69d91b3ee15105112e0dc022"),
        # Edge case: a real, cast-credited, IMDb-linked title that reports
        # total=0 with an empty episode manifest. Must not be dropped.
        ("reelshort/detail_no_episodes", "6858f90310d21493f5048723"),
        # Legacy catalogue: online_at is '0', so the year has to come from
        # publish_at instead.
        ("reelshort/detail_legacy_dates", "64596abc36c69c470e326454"),
    ]
    for name, book_id in reelshort_details:
        time.sleep(PAUSE)
        save(name, client.get(f"https://www.reelshort.com/movie/{book_id}"))

    client.close()

    # The silent WAF challenge is SYNTHESISED, not captured: reproducing it
    # would mean deliberately hammering ReelShort until it defends itself, and
    # its shape is well documented (HTTP 202, empty body, x-amzn-waf-action).
    save_synthetic_challenge()
    return 0


def save_synthetic_challenge() -> None:
    path = FIXTURES / "reelshort/challenge_202"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.with_suffix(".body").write_bytes(b"")
    path.with_suffix(".meta.json").write_text(
        json.dumps(
            {
                "url": "https://www.reelshort.com/tags/mc-types/120",
                "status": 202,
                "headers": {
                    "content-length": "0",
                    "x-amzn-waf-action": "challenge",
                    "access-control-expose-headers": "x-amzn-waf-action",
                    "server": "CloudFront",
                },
                "_note": "SYNTHETIC: documented shape, not captured live.",
            },
            indent=2,
        )
    )
    print(f"  {'reelshort/challenge_202 (synthetic)':44} 202         0 bytes")


if __name__ == "__main__":
    raise SystemExit(main())
