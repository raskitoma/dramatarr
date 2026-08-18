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
    # --- Found by reading, not by this table, which is the point of the rule below ---
    "sumá": "suma",
    "salís": "sales",
    "mové": "mueve",
    "conectá": "conecta",
    "revisá": "revisa",
    "quedate": "quédate",
}

PATTERN = re.compile(r"\b(" + "|".join(sorted(CONVERSIONS, key=len, reverse=True)) + r")\b", re.I)

#: The rule the table cannot be: a *shape* rather than a word.
#:
#: A word list is unbounded by construction, and this one proved it — five strings sat in the
#: bundle for weeks reading "Sumá", "salís", "revisalos", "mové" and "Conectá" while the check
#: reported OK, because none of those five happened to be listed.
#:
#: This catches the enclitic imperative, which is where most of the misses live. In tuteo,
#: attaching a pronoun to an imperative makes the word esdrújula and the accent becomes
#: **obligatory**: revísalos, míralo, ponte, quédate. So a word ending in one of these pronoun
#: clusters with no accent anywhere is wrong in standard Spanish whatever dialect wrote it —
#: either Rioplatense (revisalos) or simply a missing tilde. There is no correct spelling this
#: rejects, which is what makes it safe to run over the whole bundle.
#: Nine characters minimum, and that number is doing real work rather than being a guess.
#: An enclitic imperative only *needs* the accent once the stress falls on the antepenultimate
#: syllable, which in practice means a verb of two or more syllables plus the pronoun —
#: "verifícalo", "desconéctala", "revísalos". Shorter words ending in the same letters are
#: ordinary vocabulary that is correct unaccented: "invite", "aporte", "brote". Counting
#: syllables properly would be the exact rule; the length floor is the cheap approximation that
#: has no false positives on this bundle, and a miss here costs one unlinted string rather than
#: a check nobody trusts.
#: The letter before the pronoun is the whole discriminator.
#:
#: An imperative ends in a vowel — revisA+los, verificA+lo, reiniciA+la — so the cluster sits
#: on an `a`, `e` or `i`. The two constructions that look identical and are perfectly correct
#: do not: an infinitive puts an `r` there (conectaRnos, tomaRlo) and a preterite an `s`
#: (configuraSte). Requiring the vowel excludes both without an allowlist, which the first two
#: attempts at this rule needed and still got wrong.
ENCLITIC = re.compile(
    r"\b(?P<word>[a-z]{6,}[aei](?:l[oa]s?|me|te|nos))\b",
    re.I,
)

#: Words that end in a pronoun-looking cluster and are simply nouns or adjectives. Kept short
#: and explicit: a long allowlist would mean the rule is matching the wrong thing.
ENCLITIC_SAFE = frozenset(
    {
        "paralelos", "capitulos", "articulos", "elementos", "documentos", "movimientos",
        "conocimientos", "requisitos", "resultados", "identificados",
    }
)


def enclitic_hits(text: str) -> list[str]:
    """Enclitic imperatives written without their obligatory accent."""
    out: list[str] = []
    for match in ENCLITIC.finditer(text):
        word = match.group("word")
        low = word.lower()
        if low in ENCLITIC_SAFE:
            continue
        # Correctly written ones carry the tilde somewhere; those are fine.
        if any(ch in "áéíóú" for ch in low):
            continue
        out.append(word)
    return out


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
        hits = PATTERN.findall(text) + enclitic_hits(text)
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
        print(
            "    -> "
            + ", ".join(
                f"{h} = {CONVERSIONS[h.lower()]}"
                if h.lower() in CONVERSIONS
                else f"{h} — enclitic imperative, needs its accent (or is voseo)"
                for h in dict.fromkeys(hits)
            )
        )
    return 1


sys.exit(main())
