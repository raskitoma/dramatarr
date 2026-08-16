#!/usr/bin/env python3
"""Keep backend error codes and frontend translations in sync.

The frontend maps a stable error `code` to a localized string. Nothing in either codebase
fails when a code has no translation — the UI silently falls back to "something went wrong",
which is exactly the kind of drift that survives review. This walks the backend AST for every
code actually raised and diffs it against the locale files.

    python3 scripts/check_error_codes.py          # report, exit 1 on drift
    python3 scripts/check_error_codes.py --fix    # add missing keys from the table below
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend" / "app"
LOCALES = {
    "es": ROOT / "frontend" / "src" / "i18n" / "es.json",
    "en": ROOT / "frontend" / "src" / "i18n" / "en.json",
}

#: Error-raising helpers from app.errors. NotFound puts the code in the SECOND positional arg.
#:
#: `read_capped` and `reencode` (app.services.images) are in here for the same reason the
#: others are: they raise, and the code they raise is *chosen by the caller* and passed in. An
#: avatar upload and an artwork upload share one validation pipeline and differ only by the
#: code they hand it, so without these two entries every code that pipeline raises would be
#: invisible to this scan — which is precisely the drift the script exists to catch.
RAISERS = {
    "AppError",
    "Conflict",
    "NotFound",
    "Forbidden",
    "Unauthorized",
    "MailNotConfigured",
    "read_capped",
    "reencode",
}
CODE_ARG_INDEX = {"NotFound": 1, "read_capped": 2}

#: Codes the frontend raises itself, so they never appear in the backend AST.
FRONTEND_ONLY = {
    "PASSKEY_CANCELLED",
    "PASSKEY_ALREADY_REGISTERED",
    "WEBAUTHN_FAILED",
    "PLAYBACK_FAILED",
    "NETWORK_ERROR",
}

#: Codes raised via a default constructor argument, invisible to a positional-arg scan.
IMPLICIT = {"UNAUTHORIZED", "FORBIDDEN", "NOT_FOUND", "VALIDATION_ERROR", "INTERNAL_ERROR",
            "MAIL_NOT_CONFIGURED"}


def raised_codes() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for path in sorted(BACKEND.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:  # a half-written file during development
            print(f"  ! could not parse {path}: {exc}", file=sys.stderr)
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else None
            )
            if name not in RAISERS:
                continue
            index = CODE_ARG_INDEX.get(name, 0)
            where = str(path.relative_to(ROOT))
            if len(node.args) > index:
                arg = node.args[index]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    found.setdefault(arg.value, set()).add(where)
            for keyword in node.keywords:
                if keyword.arg == "code" and isinstance(keyword.value, ast.Constant):
                    found.setdefault(str(keyword.value.value), set()).add(where)
    return found


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def save(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fix", action="store_true", help="add missing keys from TRANSLATIONS")
    args = parser.parse_args()

    codes = raised_codes()
    expected = set(codes) | IMPLICIT | FRONTEND_ONLY

    locales = {name: load(path) for name, path in LOCALES.items()}
    drift = False

    for name, data in locales.items():
        errors = data.setdefault("errors", {})
        missing = sorted(c for c in expected if c not in errors)
        if not missing:
            continue
        drift = True
        translatable = [c for c in missing if c in TRANSLATIONS]
        untranslated = [c for c in missing if c not in TRANSLATIONS]

        if args.fix:
            for code in translatable:
                errors[code] = TRANSLATIONS[code][name]
            save(LOCALES[name], data)
            print(f"{name}: added {len(translatable)} translations")
        else:
            print(f"{name}: {len(missing)} codes missing")
            for code in missing:
                print(f"    {code}")

        if untranslated:
            print(f"{name}: NO TRANSLATION DEFINED for {untranslated} — add them to TRANSLATIONS")

    # Parity is what the frontend test asserts; check it here too so one run covers both.
    keysets = {name: set(data.get("errors", {})) for name, data in locales.items()}
    if len(set(map(frozenset, keysets.values()))) > 1:
        drift = True
        base = keysets["es"]
        for name, keys in keysets.items():
            if keys != base:
                print(f"parity: {name} differs — only-here={sorted(keys - base)} missing={sorted(base - keys)}")

    if not drift:
        print(f"OK — {len(expected)} error codes, all translated in {', '.join(LOCALES)}")
        return 0
    return 0 if args.fix else 1


TRANSLATIONS: dict[str, dict[str, str]] = {
    # --- Auth / passkeys ---
    "CHALLENGE_INVALID": {
        "es": "La verificación no coincide. Probá de nuevo.",
        "en": "That verification didn't match. Please try again.",
    },
    "CHALLENGE_MISMATCH": {
        "es": "La verificación no coincide con este dispositivo.",
        "en": "That verification doesn't match this device.",
    },
    "CHALLENGE_USED": {
        "es": "Esa verificación ya se usó. Empezá de nuevo.",
        "en": "That verification was already used. Start again.",
    },
    "INVALID_CREDENTIAL": {
        "es": "No reconocemos esa passkey.",
        "en": "We don't recognize that passkey.",
    },
    "INVALID_REGISTRATION_TARGET": {
        "es": "No pudimos determinar para quién es esta passkey.",
        "en": "We couldn't tell who this passkey is for.",
    },
    "PASSKEY_NOT_FOUND": {
        "es": "Esa passkey ya no existe.",
        "en": "That passkey no longer exists.",
    },
    "PASSKEY_SIGN_COUNT_REGRESSION": {
        "es": "Esa passkey parece haber sido clonada. Por seguridad la rechazamos.",
        "en": "That passkey looks cloned, so we rejected it for safety.",
    },
    "PASSKEY_REGISTRATION_FAILED": {
        "es": "No pudimos registrar la passkey.",
        "en": "We couldn't register that passkey.",
    },
    "PASSKEY_VERIFICATION_FAILED": {
        "es": "No pudimos verificar la passkey.",
        "en": "We couldn't verify that passkey.",
    },
    "PASSKEY_UNKNOWN": {
        "es": "Esa passkey no está asociada a ninguna cuenta.",
        "en": "That passkey isn't linked to any account.",
    },
    "ACCOUNT_DISABLED": {
        "es": "Tu cuenta está desactivada. Pedile al administrador que la reactive.",
        "en": "Your account is disabled. Ask an admin to reactivate it.",
    },
    "EMAIL_IN_USE": {"es": "Ese correo ya tiene cuenta.", "en": "That email already has an account."},
    "INVALID_ROLE": {"es": "Ese rol no existe.", "en": "That role doesn't exist."},
    "SESSION_NOT_FOUND": {"es": "Esa sesión ya no existe.", "en": "That session no longer exists."},
    "LAST_SIGN_IN_METHOD": {
        "es": "Es la única forma que tenés de entrar. Agregá una passkey, o activá el enlace por correo, antes de desconectarla: si no, te quedás sin manera de volver a tu cuenta.",
        "en": "This is the only way you can sign in. Add a passkey, or turn on the email link, before disconnecting it — otherwise you'd have no way back into your account.",
    },
    # --- Sign in with Google ---
    #
    # Each sentence has to match the *one* condition that raises it, or the UI explains the
    # wrong problem. Two in particular are easy to get backwards: GOOGLE_DISABLED is the
    # unconfigured case, GOOGLE_UNAVAILABLE is Google itself being unreachable; and
    # GOOGLE_ACCOUNT_MISMATCH is about this local account already answering to another Google
    # account, not about which address the Google account uses.
    "GOOGLE_DISABLED": {
        "es": "El acceso con Google está desactivado en esta casa.",
        "en": "Sign-in with Google is turned off for this household.",
    },
    "GOOGLE_UNAVAILABLE": {
        "es": "No pudimos comunicarnos con Google. Suele ser algo pasajero: probá de nuevo en un momento.",
        "en": "We couldn't reach Google. That's usually temporary — try again in a moment.",
    },
    "GOOGLE_CANCELLED": {
        "es": "Cancelaste el acceso con Google.",
        "en": "You cancelled the Google sign-in.",
    },
    "GOOGLE_TOKEN_INVALID": {
        "es": "La respuesta de Google no verificó, así que no te dejamos entrar. Probá de nuevo.",
        "en": "Google's answer didn't check out, so we didn't sign you in. Please try again.",
    },
    "GOOGLE_EXCHANGE_FAILED": {
        "es": "No pudimos terminar el intercambio con Google. Probá de nuevo.",
        "en": "We couldn't finish the exchange with Google. Please try again.",
    },
    "GOOGLE_EMAIL_UNVERIFIED": {
        "es": "Google no verificó el correo de esa cuenta, así que no podemos asociarla a ningún miembro. Verificalo en Google y probá de nuevo.",
        "en": "Google hasn't verified the email address on that account, so we can't match it to a member. Verify it with Google and try again.",
    },
    "GOOGLE_NO_ACCOUNT": {
        "es": "Esa cuenta de Google no es miembro acá. dramatarr es solo por invitación, así que la cuenta tiene que crearse desde una invitación: pedile a un administrador que invite esa dirección y después entrá con Google.",
        "en": "That Google account isn't a member here. dramatarr is invite-only, so an account has to be created from an invitation first — ask an administrator to invite that address, then sign in with Google.",
    },
    "GOOGLE_ACCOUNT_MISMATCH": {
        "es": "Esta cuenta ya está conectada a otra cuenta de Google. Entrá con esa, o desconectala primero desde tu perfil.",
        "en": "This account is already connected to a different Google account. Sign in with that one, or disconnect it from your profile first.",
    },
    "GOOGLE_ACCOUNT_IN_USE": {
        "es": "Esa cuenta de Google ya está conectada a otro miembro.",
        "en": "That Google account is already connected to another member.",
    },
    "GOOGLE_ALREADY_LINKED": {
        "es": "Tu cuenta ya está conectada a una cuenta de Google.",
        "en": "Your account is already connected to a Google account.",
    },
    "GOOGLE_NOT_LINKED": {
        "es": "Esta cuenta no está conectada con Google.",
        "en": "This account isn't connected to Google.",
    },
    "GOOGLE_STATE_INVALID": {
        "es": "Ese acceso no coincide con el que habíamos empezado. Probá de nuevo.",
        "en": "That sign-in didn't match the one we started. Please try again.",
    },
    "GOOGLE_STATE_EXPIRED": {
        "es": "Ese acceso tardó demasiado. Empezá de nuevo.",
        "en": "That sign-in took too long. Please start again.",
    },
    "GOOGLE_STATE_USED": {
        "es": "Ese acceso ya se completó. Empezá de nuevo si todavía necesitás entrar.",
        "en": "That sign-in was already completed. Start again if you still need to get in.",
    },
    # --- Invites ---
    "INVITE_ALREADY_USED": {
        "es": "Esta invitación ya fue usada.",
        "en": "This invitation has already been used.",
    },
    "INVITE_ALREADY_ACCEPTED": {
        "es": "Esta invitación ya fue aceptada.",
        "en": "This invitation was already accepted.",
    },
    "INVITE_REVOKED": {
        "es": "Esta invitación fue revocada.",
        "en": "This invitation was revoked.",
    },
    "INVITE_NOT_FOUND": {"es": "No encontramos esa invitación.", "en": "We couldn't find that invitation."},
    "INVITE_COMPLETE_FAILED": {
        "es": "No pudimos completar el registro. Probá de nuevo.",
        "en": "We couldn't finish signing you up. Please try again.",
    },
    # --- Magic links ---
    "MAGIC_LINK_EXPIRED": {"es": "Ese enlace venció. Pedí uno nuevo.", "en": "That link expired. Request a new one."},
    "MAGIC_LINK_USED": {"es": "Ese enlace ya fue usado.", "en": "That link was already used."},
    "MAGIC_LINK_DISABLED": {
        "es": "El acceso por enlace de correo está desactivado.",
        "en": "Email link sign-in is turned off.",
    },
    # --- Mail ---
    "MAIL_SEND_FAILED": {"es": "No pudimos enviar el correo.", "en": "We couldn't send the email."},
    "MAIL_TEMPLATE_MISSING": {
        "es": "Falta la plantilla de ese correo.",
        "en": "That email template is missing.",
    },
    "MAIL_INVALID_RECIPIENT": {
        "es": "Esa dirección de correo no es válida.",
        "en": "That email address isn't valid.",
    },
    "SMTP_SEND_FAILED": {
        "es": "El servidor SMTP rechazó el envío. Revisá la configuración.",
        "en": "The SMTP server rejected the message. Check your settings.",
    },
    # --- Library / media ---
    "EPISODE_NOT_FOUND": {"es": "No encontramos ese episodio.", "en": "We couldn't find that episode."},
    "EPISODE_NUMBER_TAKEN": {
        "es": "Ya existe un episodio con ese número en esta temporada.",
        "en": "An episode with that number already exists in this season.",
    },
    "SERIES_BULK_EMPTY": {
        "es": "No elegiste ningún campo para cambiar.",
        "en": "You didn't choose any field to change.",
    },
    "SERIES_NOT_FOUND": {"es": "No encontramos esa serie.", "en": "We couldn't find that series."},
    # --- OMDb (IMDb metadata) ---
    "OMDB_NOT_CONFIGURED": {
        "es": "Agregá una clave de OMDb en preferencias para traer metadata de IMDb.",
        "en": "Add an OMDb API key in settings to pull metadata from IMDb.",
    },
    "OMDB_UNAUTHORIZED": {
        "es": "OMDb rechazó la clave.",
        "en": "OMDb rejected the API key.",
    },
    "OMDB_UNAVAILABLE": {"es": "No pudimos conectarnos a OMDb.", "en": "We couldn't reach OMDb."},
    "OMDB_ERROR": {"es": "OMDb devolvió una respuesta inesperada.", "en": "OMDb returned an unexpected response."},
    "OMDB_TITLE_NOT_FOUND": {
        "es": "IMDb no tiene ese título.",
        "en": "IMDb doesn't have that title.",
    },
    # --- Requests ---
    # `ALREADY_IN_LIBRARY` is a refusal that is good news, so it reads as an answer rather
    # than as an error: the thing they were about to ask for is already on the shelf.
    "REQUEST_ALREADY_IN_LIBRARY": {
        "es": "Eso ya está en la biblioteca.",
        "en": "That's already in the library.",
    },
    "REQUEST_NOT_FOUND": {
        "es": "No encontramos ese pedido.",
        "en": "We couldn't find that request.",
    },
    # Raised inside the poster cache and swallowed there — a card with no picture is an
    # ordinary outcome, not a failure worth reporting. Translated anyway: it is a raised code,
    # and the rule is that every raised code has a sentence, so that a future caller which
    # *does* surface it cannot ship an untranslated one.
    "REQUEST_POSTER_INVALID": {
        "es": "Esa imagen no se pudo procesar.",
        "en": "That image could not be processed.",
    },
    "MEDIA_FILE_NOT_FOUND": {"es": "No encontramos ese archivo.", "en": "We couldn't find that file."},
    "FILE_NOT_FOUND": {
        "es": "El archivo no está donde lo esperábamos. Puede que el NAS esté desconectado.",
        "en": "The file isn't where we expected. The NAS may be disconnected.",
    },
    "LIBRARY_UNAVAILABLE": {
        "es": "No podemos leer la biblioteca. Verificá que el NAS esté montado.",
        "en": "We can't read the library. Check that the NAS is mounted.",
    },
    # --- Import assistant ---
    "IMPORT_FOLDER_NOT_FOUND": {
        "es": "Esa carpeta ya no tiene archivos sin asignar. Actualizá la lista.",
        "en": "That folder has no unmatched files any more. Refresh the list.",
    },
    "IMPORT_FILE_NOT_IN_FOLDER": {
        "es": "Alguno de los archivos ya no está en esa carpeta. Recalculá el plan.",
        "en": "One of those files is no longer in that folder. Recalculate the plan.",
    },
    "UNKNOWN_IMPORT_STRATEGY": {
        "es": "Esa estrategia de importación no existe.",
        "en": "That import strategy doesn't exist.",
    },
    "TITLE_EXISTS": {"es": "Ese título alternativo ya está cargado.", "en": "That alternate title already exists."},
    "TITLE_NOT_FOUND": {"es": "No encontramos ese título.", "en": "We couldn't find that title."},
    "IMAGE_PATH_INVALID": {"es": "Esa ruta de imagen no es válida.", "en": "That image path isn't valid."},
    # --- Avatars and artwork ---
    #
    # Two of these have to say more than "that failed", because the person reading them did
    # nothing obviously wrong. ARTWORK_URL_BLOCKED names the actual reason — the address is on
    # the household's own network — since an admin who pasted a link off a wiki has no way to
    # guess that otherwise. The size limits are stated as numbers for the same reason.
    "AVATAR_INVALID": {
        "es": "Ese archivo no es una imagen PNG, JPEG o WebP que podamos abrir. Probá con otra foto.",
        "en": "That file isn't a PNG, JPEG or WebP image we can open. Try another photo.",
    },
    "AVATAR_TOO_LARGE": {
        "es": "Esa foto pesa demasiado. El máximo son 5 MB.",
        "en": "That photo is too big. The maximum is 5 MB.",
    },
    "AVATAR_NOT_FOUND": {
        "es": "Ese miembro todavía no subió una foto de perfil.",
        "en": "That member hasn't uploaded a profile picture yet.",
    },
    "ARTWORK_INVALID": {
        "es": "No pudimos abrir esa imagen. Tiene que ser un PNG, JPEG o WebP.",
        "en": "We couldn't open that image. It has to be a PNG, JPEG or WebP.",
    },
    "ARTWORK_TOO_LARGE": {
        "es": "Esa imagen pesa demasiado. El máximo son 10 MB.",
        "en": "That image is too big. The maximum is 10 MB.",
    },
    "ARTWORK_URL_INVALID": {
        "es": "Esa dirección no sirve: tiene que ser un enlace http o https a una imagen.",
        "en": "That address won't work — it has to be an http or https link to an image.",
    },
    "ARTWORK_URL_BLOCKED": {
        "es": "Esa dirección apunta a un equipo de tu propia red, así que no la descargamos. Usá un enlace público a la imagen.",
        "en": "That address points at a machine on your own network, so we didn't download it. Use a public link to the image.",
    },
    "ARTWORK_FETCH_FAILED": {
        "es": "No pudimos descargar esa imagen. Fijate que el enlace siga andando.",
        "en": "We couldn't download that image. Check that the link still works.",
    },
    "IMAGE_SEARCH_FAILED": {
        "es": "La búsqueda de imágenes falló. Revisá la clave y la cuota del proveedor en preferencias.",
        "en": "The image search failed. Check the provider's key and quota in settings.",
    },
    # --- Image search: SearXNG ---
    #
    # SEARXNG_JSON_DISABLED is the one message in this table that has to *teach*. SearXNG ships
    # with its JSON output turned off, so this is the first thing a new configuration hits, and
    # the difference between a five-minute setup and an afternoon is whether the message names
    # the file and the setting. It does. Do not shorten it to "the search failed".
    "SEARXNG_JSON_DISABLED": {
        "es": "Tu instancia de SearXNG no contesta en JSON: viene con esa salida desactivada de fábrica. Agregá «json» a search.formats en el settings.yml de la instancia y reiniciala.",
        "en": "Your SearXNG instance isn't answering in JSON — it ships with that output turned off. Add \"json\" to search.formats in the instance's settings.yml and restart it.",
    },
    "SEARXNG_UNREACHABLE": {
        "es": "No pudimos contactar tu instancia de SearXNG. Fijate que la dirección sea la correcta y que el servicio esté andando.",
        "en": "We couldn't reach your SearXNG instance. Check that the address is right and the service is running.",
    },
    "SEARXNG_BAD_RESPONSE": {
        "es": "Esa dirección contestó algo que no parece SearXNG. Revisá que apunte a tu instancia y no a otro servicio.",
        "en": "That address answered with something that doesn't look like SearXNG. Check that it points at your instance and not at another service.",
    },
    "SEARCH_NOT_CONFIGURED": {
        "es": "Todavía no configuraste la búsqueda de imágenes. Elegí un proveedor en preferencias y completá su dirección.",
        "en": "Image search isn't set up yet. Pick a provider in settings and fill in its address.",
    },
    "RANGE_NOT_SATISFIABLE": {
        "es": "El reproductor pidió una porción del archivo que no existe.",
        "en": "The player asked for a part of the file that doesn't exist.",
    },
    # --- Subtitles / thumbnails ---
    "SUBTITLE_NOT_AVAILABLE": {
        "es": "Ese subtítulo no está disponible.",
        "en": "That subtitle isn't available.",
    },
    "SUBTITLE_SOURCE_MISSING": {
        "es": "Falta el archivo de origen del subtítulo.",
        "en": "The subtitle's source file is missing.",
    },
    "SUBTITLE_TRACK_NOT_FOUND": {
        "es": "No encontramos esa pista de subtítulos.",
        "en": "We couldn't find that subtitle track.",
    },
    "THUMBNAILS_NOT_GENERATED": {
        "es": "Todavía no se generaron las miniaturas de este episodio.",
        "en": "Thumbnails haven't been generated for this episode yet.",
    },
    "SPRITE_NO_FRAMES": {
        "es": "No pudimos extraer cuadros para las miniaturas.",
        "en": "We couldn't extract frames for the thumbnails.",
    },
    # --- ffmpeg ---
    "FFMPEG_NOT_AVAILABLE": {
        "es": "ffmpeg no está disponible en el servidor.",
        "en": "ffmpeg isn't available on the server.",
    },
    "FFMPEG_FAILED": {"es": "ffmpeg falló al procesar el archivo.", "en": "ffmpeg failed to process the file."},
    "FFMPEG_TIMEOUT": {"es": "ffmpeg tardó demasiado y se canceló.", "en": "ffmpeg took too long and was cancelled."},
    # --- Jobs ---
    "JOB_NOT_FOUND": {"es": "No encontramos ese trabajo.", "en": "We couldn't find that job."},
    "JOB_NOT_CANCELABLE": {
        "es": "Ese trabajo ya terminó y no se puede cancelar.",
        "en": "That job already finished and can't be cancelled.",
    },
    "JOB_NOT_QUEUED": {"es": "Ese trabajo ya no está en espera.", "en": "That job is no longer waiting."},
    "JOB_NOT_RETRYABLE": {
        "es": "Solo se pueden reintentar trabajos que fallaron.",
        "en": "Only failed jobs can be retried.",
    },
    "UNKNOWN_JOB_TYPE": {"es": "Ese tipo de trabajo no existe.", "en": "That job type doesn't exist."},
    "INVALID_PAYLOAD": {
        "es": "Los datos de ese trabajo no son válidos.",
        "en": "That job's payload isn't valid.",
    },
    "UNKNOWN_DURATION": {
        "es": "No conocemos la duración de ese archivo.",
        "en": "We don't know that file's duration.",
    },
    # --- Remote import ---
    "REMOTE_IMPORT_FAILED": {"es": "La importación falló.", "en": "The import failed."},
    "REMOTE_IMPORT_NOT_A_FILE": {
        "es": "Esa URL no apunta a un archivo descargable.",
        "en": "That URL doesn't point to a downloadable file.",
    },
    "REMOTE_IMPORT_REDIRECTED": {
        "es": "Esa URL redirige fuera del origen permitido.",
        "en": "That URL redirects outside the allowed origin.",
    },
    "REMOTE_IMPORT_TOO_LARGE": {
        "es": "Ese archivo supera el tamaño máximo permitido.",
        "en": "That file is larger than the allowed maximum.",
    },
    "URL_NOT_ALLOWED": {
        "es": "Esa URL no pertenece al origen configurado.",
        "en": "That URL isn't from the configured origin.",
    },
    # --- TMDB ---
    "TMDB_ERROR": {"es": "TMDB devolvió un error.", "en": "TMDB returned an error."},
    "TMDB_INVALID_KEY": {"es": "La clave de TMDB no es válida.", "en": "That TMDB key isn't valid."},
    "TMDB_KEY_MISSING": {
        "es": "Falta configurar la clave de TMDB en preferencias.",
        "en": "The TMDB key hasn't been set in settings.",
    },
    "TMDB_NOT_FOUND": {"es": "TMDB no tiene ese título.", "en": "TMDB doesn't have that title."},
    "TMDB_LOOKUP_EMPTY": {
        "es": "Indicá un id de TMDB o de IMDb para buscar el título.",
        "en": "Give a TMDB id or an IMDb id to look the title up.",
    },
    "TMDB_RATE_LIMITED": {
        "es": "TMDB nos limitó por exceso de pedidos. Reintentá en un momento.",
        "en": "TMDB rate-limited us. Try again shortly.",
    },
    "TMDB_UNREACHABLE": {"es": "No pudimos contactar a TMDB.", "en": "We couldn't reach TMDB."},
    # --- Wikipedia ---
    #
    # The second metadata source, for the shows TMDB does not carry. Two of these messages
    # have to *teach* rather than report, because both name a state the admin can fix on the
    # spot: a disambiguation page is the wrong page and the right one is in the same list of
    # results, and a rejected language code is a typo in a two-letter field.
    # --- Users ---
    "USER_NOT_FOUND": {"es": "No encontramos ese usuario.", "en": "We couldn't find that user."},
    # The founding account. The message has to say where the change *can* be made, or an
    # admin reads it as a bug rather than as the deliberate rule it is.
    "PROTECTED_ACCOUNT": {
        "es": "Esa es la cuenta fundadora de la casa y no se puede modificar desde acá: es el último camino de vuelta si algo sale mal. Se cambia desde una terminal, con «python -m app.cli unprotect».",
        "en": "That's the household's founding account, so it can't be changed from here — it's the last way back in if something goes wrong. Change it from a shell, with `python -m app.cli unprotect`.",
    },
    "INVALID_EMAIL": {
        "es": "Esa dirección de correo no es válida.",
        "en": "That email address isn't valid.",
    },
    # Raised by the API client itself when the response was not even JSON.
    "NETWORK_ERROR": {
        "es": "No pudimos conectarnos al servidor.",
        "en": "We couldn't reach the server.",
    },
    # A post-sign-in redirect target that is not a plain in-app path. Refusing it is what stops
    # a crafted link from bouncing someone to another site straight after authenticating.
    "INVALID_NEXT_PATH": {
        "es": "Ese destino de redirección no es válido.",
        "en": "That redirect destination isn't valid.",
    },
}


if __name__ == "__main__":
    raise SystemExit(main())
