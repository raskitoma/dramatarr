#!/usr/bin/env python3
"""Keep the Spanish UI in neutral Latin American, not Rioplatense.

The app was written in the owner's own register — voseo, which is how Argentina, Uruguay and
parts of Central America speak. The household reads it in more accents than that, so the UI
moved to tuteo: the form every Spanish-speaking country understands without noticing it.

This is a **lint, not a translator**. It flags the forms rather than rewriting them, because
the conversion is not mechanical in either direction:

* The accent moves rather than disappearing. `probá` → `prueba` also diphthongizes the stem;
  a naive "drop the accent" gives `proba`, which is not a word.
* Some pairs are irregular. `tenés` → `tienes`, `querés` → `quieres`, `podés` → `puedes`.
* And a few forms are identical in both registers and must NOT be touched — `usa`, `busca`
  and `deja` are already neutral, so a rule matching on the stem would corrupt them.

So the table below is the record of what was decided, one form at a time, and this script's
job is only to notice when something outside it creeps back in.

    python3 scripts/check_neutral_spanish.py          # report, exit 1 on drift
    python3 scripts/check_neutral_spanish.py --list   # show every conversion in the table
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPANISH = ROOT / "frontend" / "src" / "i18n" / "es.json"

#: voseo form -> neutral (tuteo) form. Every entry was present in the file when this was
#: written; the case-capitalized variants are handled by the matcher, not duplicated here.
CONVERSIONS: dict[str, str] = {
    # --- Present indicative: irregular, and the reason this is a table ---
    "tenés": "tienes",
    "podés": "puedes",
    "querés": "quieres",
    "sabés": "sabes",
    "hacés": "haces",
    "debés": "debes",
    "preferís": "prefieres",
    "elegís": "eliges",
    # --- Imperatives: the bulk of UI copy, because every hint is an instruction ---
    "abrí": "abre",
    "activá": "activa",
    "actualizá": "actualiza",
    "agregá": "agrega",
    "andá": "ve",
    "apretá": "presiona",
    "avisá": "avisa",
    "borrá": "borra",
    "buscá": "busca",
    "cambiá": "cambia",
    "cargá": "carga",
    "cerrá": "cierra",
    "configurá": "configura",
    "contá": "cuenta",
    "copiá": "copia",
    "corré": "ejecuta",
    "dejá": "deja",
    "desactivá": "desactiva",
    "elegí": "elige",
    "enviá": "envía",
    "escribí": "escribe",
    "guardá": "guarda",
    "hacé": "haz",
    "ingresá": "ingresa",
    "mandá": "envía",
    "marcá": "marca",
    "mirá": "mira",
    "pedí": "pide",
    "pegá": "pega",
    "poné": "pon",
    "probá": "prueba",
    "quitá": "quita",
    "revisá": "revisa",
    "sacá": "quita",
    "seguí": "sigue",
    "seleccioná": "selecciona",
    "subí": "sube",
    "tocá": "toca",
    "usá": "usa",
    "vení": "ven",
    "verificá": "verifica",
    "volvé": "vuelve",
    # --- Enclitic forms: the pronoun rides along and the stress shifts ---
    "acordate": "recuerda",
    "asegurate": "asegúrate",
    "dejalo": "déjalo",
    "escribilo": "escríbelo",
    "fijate": "fíjate",
    "hacelo": "hazlo",
    "ponelo": "ponlo",
    # --- Pronoun ---
    "vos": "tú",
    # `dale` is Rioplatense as an interjection but standard as "give it to him"; it never
    # appears in that second sense here, so it is listed and converted by hand where found.
    "dale": "adelante",
}

PATTERN = re.compile(r"\b(" + "|".join(sorted(CONVERSIONS, key=len, reverse=True)) + r")\b", re.I)


def strings(node: object, prefix: str = "") -> list[tuple[str, str]]:
    if isinstance(node, dict):
        out: list[tuple[str, str]] = []
        for key, value in node.items():
            out += strings(value, f"{prefix}.{key}" if prefix else key)
        return out
    if isinstance(node, str):
        return [(prefix, node)]
    return []


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", action="store_true", help="print the conversion table")
    args = parser.parse_args()

    if args.list:
        for voseo, neutral in sorted(CONVERSIONS.items()):
            print(f"  {voseo:<14} -> {neutral}")
        return 0

    data = json.loads(SPANISH.read_text(encoding="utf-8"))
    found: list[tuple[str, str, list[str]]] = []
    for key, text in strings(data):
        hits = PATTERN.findall(text)
        if hits:
            found.append((key, text, hits))

    if not found:
        total = len(strings(data))
        print(f"OK — {total} Spanish strings, no Rioplatense forms")
        return 0

    print(f"{len(found)} string(s) still in voseo:")
    for key, text, hits in found:
        print(f"  {key}")
        print(f"    {text[:100]}")
        print(f"    -> {', '.join(f'{h} = {CONVERSIONS[h.lower()]}' for h in dict.fromkeys(hits))}")
    return 1


sys.exit(main())
