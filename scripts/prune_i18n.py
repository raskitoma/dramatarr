#!/usr/bin/env python3
"""Remove translation keys nothing reaches any more.

Deleting TMDB, OMDb and the web search left 361 strings behind. Dead copy is not harmless:
it is what the next person greps, translates and reasons about, and every one of these
describes a feature that no longer exists.

**The whole difficulty is that "unreferenced" is not "unused".** A dozen call sites build
their key at runtime — `t(\`errors.${code}\`)`, `t(\`admin.jobType.${type}\`)` — so a prune
keyed on static references alone would delete every error message and every job label in the
application, and nothing would fail until a user hit one. So this refuses to guess:

* Every dynamic prefix found in the source is protected wholesale, along with its children.
* `--check` prints what would go without touching anything. Read it before running for real.
* The two locale files are pruned to the *same* key set, so the parity the i18n check enforces
  cannot be broken by pruning one and not the other.

    python3 scripts/prune_i18n.py --check    # report only
    python3 scripts/prune_i18n.py            # prune, then re-run the other checks
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "frontend" / "src"
LOCALES = {name: SRC / "i18n" / f"{name}.json" for name in ("es", "en")}

#: Prefixes whose children are built at runtime, harvested from the source rather than
#: listed by hand — a hand-written list goes stale the first time someone adds one.
#:
#: Matching the *template shape* rather than `t(...)` calls is deliberate: a key can be
#: built in one place and translated in another, which is how `requests.status.${status}`
#: in `api/requests.ts` slipped past the first version of this and would have taken every
#: wishlist status label with it.
DYNAMIC_PATTERN = re.compile(r"`([a-z][a-zA-Z.]*)\.\$\{")

#: `AdminImportPage` passes its prefix in as a parameter, so the template literal in that file
#: reads `${prefix}.generic` and reveals nothing. These are the values its call sites pass.
INDIRECT_PREFIXES = (
    "admin.import.imported",
    "admin.import.why",
    "admin.import.warnings",
    "admin.import.strategies",
)

#: Whole sections addressed only through code, never through a literal key.
ALWAYS_KEEP = ("errors",)


def flatten(node: object, prefix: str = "") -> dict[str, object]:
    out: dict[str, object] = {}
    if isinstance(node, dict):
        for key, value in node.items():
            out |= flatten(value, f"{prefix}.{key}" if prefix else key)
    else:
        out[prefix] = node
    return out


def source_text() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in SRC.rglob("*")
        if path.suffix in {".ts", ".tsx"} and path.is_file()
    )


def dynamic_prefixes(text: str) -> set[str]:
    found = {match.rstrip(".") for match in DYNAMIC_PATTERN.findall(text) if match.strip(".")}
    return found | set(INDIRECT_PREFIXES) | set(ALWAYS_KEEP)


def referenced(text: str, keys: list[str]) -> set[str]:
    """A key counts as referenced if its literal string appears anywhere in the source."""
    return {key for key in keys if f"'{key}'" in text or f'"{key}"' in text or f"`{key}`" in text}


def protected(key: str, prefixes: set[str]) -> bool:
    return any(key == p or key.startswith(f"{p}.") for p in prefixes)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="report without writing")
    args = parser.parse_args()

    text = source_text()
    prefixes = dynamic_prefixes(text)

    flat = {name: flatten(json.loads(path.read_text(encoding="utf-8"))) for name, path in LOCALES.items()}
    keys = sorted(set(flat["es"]) | set(flat["en"]))
    live = referenced(text, keys)

    # Plural variants ride along with their base: i18next resolves `x_other` from `x`.
    for key in list(live):
        for other in keys:
            if other.startswith(f"{key}_"):
                live.add(other)

    dead = [k for k in keys if k not in live and not protected(k, prefixes)]

    print(f"  {len(keys)} keys · {len(live)} referenced · {len(prefixes)} dynamic prefixes protected")
    print(f"  {len(dead)} unreachable")
    by_section = collections.Counter(k.split(".")[0] + "." + (k.split(".")[1] if "." in k[k.index(".") + 1:] + "." else "") for k in dead) if dead else {}
    for section, count in collections.Counter(".".join(k.split(".")[:2]) for k in dead).most_common(12):
        print(f"      {section:<34} {count}")

    if args.check or not dead:
        return 0

    for name, path in LOCALES.items():
        data = json.loads(path.read_text(encoding="utf-8"))

        def prune(node: object, prefix: str = "") -> object:
            if not isinstance(node, dict):
                return node
            kept: dict[str, object] = {}
            for key, value in node.items():
                full = f"{prefix}.{key}" if prefix else key
                if isinstance(value, dict):
                    child = prune(value, full)
                    if child:
                        kept[key] = child
                elif full not in dead:
                    kept[key] = value
            return kept

        path.write_text(json.dumps(prune(data), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"  {name}.json pruned")
    return 0


sys.exit(main())
