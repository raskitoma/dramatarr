#!/usr/bin/env python3
"""Check that every translation key the UI asks for actually exists.

The existing `keys.test.ts` asserts es.json and en.json have the SAME keys. That catches a
half-translated feature, but it is blind to the failure that actually happens here: two agents
editing the locale files at once, the second one read-modify-writing over the first. Both files
lose the same keys, parity still holds, the test stays green — and the interface renders raw
key strings like `admin.settings.tocLabel` to the user.

This walks the source for t('...') calls instead, and diffs against the locale files.

    python3 scripts/check_i18n_keys.py          # exit 1 on a missing key
    python3 scripts/check_i18n_keys.py --list-dynamic
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "frontend" / "src"
LOCALES = {
    "es": SRC / "i18n" / "es.json",
    "en": SRC / "i18n" / "en.json",
}

#: t('a.b.c') / t("a.b.c") / t(`a.b.c`) with no interpolation.
STATIC_CALL = re.compile(r"""\bt\(\s*(['"`])([A-Za-z0-9_.]+)\1""")
#: t(`a.b.${x}`) and friends — real, but not resolvable by reading the source.
DYNAMIC_CALL = re.compile(r"\bt\(\s*`[^`]*\$\{")
#: i18nKey="a.b.c" on <Trans>.
JSX_KEY = re.compile(r"""\bi18nKey=\s*(['"])([A-Za-z0-9_.]+)\1""")


def flatten(node: object, prefix: str = "") -> set[str]:
    if not isinstance(node, dict):
        return {prefix}
    keys: set[str] = set()
    for key, value in node.items():
        keys |= flatten(value, f"{prefix}.{key}" if prefix else key)
    return keys


def plural_variants(key: str) -> set[str]:
    """i18next resolves `x` for a count of one and `x_other` for the rest, so a declared
    `x_other` means `x` is legitimately in use too."""
    return {key, f"{key}_other", f"{key}_one", f"{key}_zero", f"{key}_many"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--list-dynamic", action="store_true", help="show computed t() call sites")
    args = parser.parse_args()

    declared = {name: flatten(json.loads(path.read_text(encoding="utf-8")))
                for name, path in LOCALES.items()}

    used: dict[str, set[str]] = {}
    dynamic: list[str] = []
    for path in sorted(SRC.rglob("*.ts*")):
        if path.name.endswith(".test.ts") or path.name.endswith(".test.tsx"):
            continue
        text = path.read_text(encoding="utf-8")
        where = str(path.relative_to(ROOT))
        for match in STATIC_CALL.finditer(text):
            used.setdefault(match.group(2), set()).add(where)
        for match in JSX_KEY.finditer(text):
            used.setdefault(match.group(2), set()).add(where)
        if DYNAMIC_CALL.search(text):
            dynamic.append(where)

    if args.list_dynamic:
        print("Files building keys at runtime (verify these by hand):")
        for where in sorted(set(dynamic)):
            print(f"  {where}")
        print()

    failed = False
    for name, keys in declared.items():
        missing = sorted(
            key for key in used if not (plural_variants(key) & keys)
        )
        if missing:
            failed = True
            print(f"{name}.json — {len(missing)} key(s) used in the UI but not declared:")
            for key in missing:
                sites = ", ".join(sorted(used[key])[:2])
                print(f"    {key}   ({sites})")

    # A key nobody asks for is not a failure — dynamic call sites legitimately reach keys this
    # scan cannot see — but a big number usually means a feature was removed and left litter.
    every_used = set(used)
    unused = sorted(declared["es"] - every_used)
    if unused:
        print(f"\n{len(unused)} declared key(s) with no static reference "
              f"(fine when reached dynamically; {len(set(dynamic))} file(s) do that).")

    if failed:
        return 1
    print(f"OK — {len(used)} static keys used, all declared in {', '.join(LOCALES)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
