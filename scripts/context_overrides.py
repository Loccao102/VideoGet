#!/usr/bin/env python3
"""Human-approved context facts for VideoGet localization.

Generated context is useful but not authoritative. This module loads a per-job
`*.translation-overrides.json` file written by Subtitle Studio and overlays only
facts the user explicitly approved: character names, relationships, terminology,
and optional notes. These facts stay local to the job and are not Translation
Memory by themselves.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path


def _clean(value: object) -> str:
    return " ".join(str(value or "").replace("\u200b", " ").replace("\ufeff", " ").split()).strip()


def overrides_path() -> Path | None:
    raw = _clean(os.getenv("TRANSLATE_CONTEXT_OVERRIDES_FILE", ""))
    return Path(raw) if raw else None


def load_overrides(path: Path | None = None) -> dict:
    path = path or overrides_path()
    if path is None or not path.exists():
        return {"version": 1, "characters": [], "relationships": [], "terms": [], "notes": ""}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "characters": [], "relationships": [], "terms": [], "notes": ""}
    if not isinstance(payload, dict):
        return {"version": 1, "characters": [], "relationships": [], "terms": [], "notes": ""}
    return {
        "version": 1,
        "characters": payload.get("characters") if isinstance(payload.get("characters"), list) else [],
        "relationships": payload.get("relationships") if isinstance(payload.get("relationships"), list) else [],
        "terms": payload.get("terms") if isinstance(payload.get("terms"), list) else [],
        "notes": _clean(payload.get("notes")),
        "updatedAt": _clean(payload.get("updatedAt")),
    }


def overrides_fingerprint(path: Path | None = None) -> str:
    path = path or overrides_path()
    if path is None or not path.exists():
        return "none"
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:20]
    except OSError:
        return "unreadable"


def _same_name(a: object, b: object) -> bool:
    return _clean(a).casefold() == _clean(b).casefold() and bool(_clean(a))


def apply_overrides(context: dict, overrides: dict | None = None) -> dict:
    result = dict(context or {})
    approved = overrides or load_overrides()
    characters = [dict(item) for item in (result.get("characters") or []) if isinstance(item, dict)]
    relationships = [dict(item) for item in (result.get("relationships") or []) if isinstance(item, dict)]
    terms = [dict(item) for item in (result.get("terms") or []) if isinstance(item, dict)]

    approved_characters: list[dict] = []
    for raw in approved.get("characters") or []:
        if not isinstance(raw, dict):
            continue
        source = _clean(raw.get("source"))
        preferred = _clean(raw.get("preferredName"))
        if not source or not preferred:
            continue
        override = {
            "source": source,
            "vi": preferred,
            "role": _clean(raw.get("role")),
            "gender": _clean(raw.get("gender")) or "unknown",
            "voiceGender": _clean(raw.get("voiceGender")) or "auto",
            "confidence": 1.0,
            "locked": True,
            "approvedByUser": True,
        }
        found = False
        for index, item in enumerate(characters):
            aliases = [_clean(value) for value in (item.get("aliases") or [])]
            if _same_name(item.get("source"), source) or any(_same_name(alias, source) for alias in aliases):
                merged = dict(item)
                merged.update({key: value for key, value in override.items() if value not in ("", None)})
                characters[index] = merged
                found = True
                break
        if not found:
            characters.append(override)
        approved_characters.append(override)

    approved_relationships: list[dict] = []
    for raw in approved.get("relationships") or []:
        if not isinstance(raw, dict):
            continue
        source_from = _clean(raw.get("from"))
        source_to = _clean(raw.get("to"))
        relation = _clean(raw.get("relation"))
        if not source_from or not source_to or not relation:
            continue
        override = {
            "from": source_from,
            "to": source_to,
            "relation": relation,
            "confidence": 1.0,
            "locked": True,
            "approvedByUser": True,
        }
        found = False
        for index, item in enumerate(relationships):
            direct = _same_name(item.get("from"), source_from) and _same_name(item.get("to"), source_to)
            reverse = _same_name(item.get("from"), source_to) and _same_name(item.get("to"), source_from)
            if direct or reverse:
                merged = dict(item)
                merged.update(override)
                relationships[index] = merged
                found = True
                break
        if not found:
            relationships.append(override)
        approved_relationships.append(override)

    approved_terms: list[dict] = []
    for raw in approved.get("terms") or []:
        if not isinstance(raw, dict):
            continue
        source = _clean(raw.get("source"))
        preferred = _clean(raw.get("preferredText"))
        if not source or not preferred:
            continue
        override = {
            "source": source,
            "vi": preferred,
            "note": _clean(raw.get("note")),
            "confidence": 1.0,
            "locked": True,
            "approvedByUser": True,
        }
        found = False
        for index, item in enumerate(terms):
            if _same_name(item.get("source"), source):
                merged = dict(item)
                merged.update(override)
                terms[index] = merged
                found = True
                break
        if not found:
            terms.append(override)
        approved_terms.append(override)

    result["characters"] = characters
    result["relationships"] = relationships
    result["terms"] = terms
    result["approvedFacts"] = {
        "characters": approved_characters,
        "relationships": approved_relationships,
        "terms": approved_terms,
        "notes": _clean(approved.get("notes")),
    }
    result["approvedContextLocked"] = bool(approved_characters or approved_relationships or approved_terms or _clean(approved.get("notes")))
    return result


def compact_approved_facts(context: dict) -> dict:
    approved = context.get("approvedFacts") if isinstance(context, dict) else None
    if not isinstance(approved, dict):
        return {"characters": [], "relationships": [], "terms": [], "notes": ""}
    return {
        "characters": approved.get("characters") or [],
        "relationships": approved.get("relationships") or [],
        "terms": approved.get("terms") or [],
        "notes": _clean(approved.get("notes")),
    }


def apply_voice_hints(segments: list[dict], context: dict) -> list[dict]:
    """Attach a voice gender hint when speaker identity matches an approved character."""
    characters = context.get("characters") or []
    lookup: dict[str, str] = {}
    for item in characters:
        if not isinstance(item, dict):
            continue
        hint = _clean(item.get("voiceGender")).lower()
        if hint not in {"male", "female"}:
            continue
        for key in [item.get("source"), item.get("vi"), *(item.get("aliases") or [])]:
            cleaned = _clean(key).casefold()
            if cleaned:
                lookup[cleaned] = hint
    out: list[dict] = []
    for raw in segments:
        item = dict(raw)
        speaker = _clean(item.get("speaker")).casefold()
        if speaker and speaker in lookup:
            item["voiceGender"] = lookup[speaker]
        out.append(item)
    return out
