"""Photo handling for the Telegram studio control surface.

Two roles:

1. **Ingestion** — when the user sends a photo, download the largest
   resolution variant via Telegram's getFile + file download URL, save
   under ``data/design_references/by_user/<user_id>/`` with a stable
   hash filename, run vision extraction (cached), and add the entry
   to the library index.

2. **Library + attachment** — a JSON-backed registry of references
   with tags, auto-attach window (5 min after a photo upload), and
   keyword-matching for /build commands.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

_TELEGRAM_FILE_API = "https://api.telegram.org/file/bot"
_TELEGRAM_API = "https://api.telegram.org/bot"
_HTTP_TIMEOUT_SECONDS = 30.0

# Auto-attach window: if the user uploaded a photo within this many
# seconds before issuing a /build, the photo auto-attaches.
ATTACH_WINDOW_SECONDS = 300.0


def _refs_dir() -> Path:
    try:
        from skyn3t.config.settings import get_settings
        return Path(get_settings().data_dir) / "design_references"
    except Exception:  # noqa: BLE001
        return Path("data/design_references")


def _library_path() -> Path:
    return _refs_dir() / "library.json"


def _user_dir(user_id: str) -> Path:
    return _refs_dir() / "by_user" / str(user_id)


@dataclass
class LibraryEntry:
    id: str  # short hash — used as the user-facing reference ID
    sha: str  # full sha256 of the image bytes
    path: str  # absolute file path on disk
    user_id: str
    uploaded_at: float
    caption: str = ""
    tags: List[str] = field(default_factory=list)
    extraction_ok: bool = False  # has design_vision.extract succeeded?
    verdict_one_liner: str = ""  # cached so /references can show it without re-reading extraction


def _load_library() -> Dict[str, LibraryEntry]:
    path = _library_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        logger.warning("reference library unreadable; starting fresh", exc_info=True)
        return {}
    out: Dict[str, LibraryEntry] = {}
    for k, v in (data or {}).items():
        if not isinstance(v, dict):
            continue
        try:
            out[str(k)] = LibraryEntry(
                id=str(v.get("id") or k),
                sha=str(v.get("sha") or ""),
                path=str(v.get("path") or ""),
                user_id=str(v.get("user_id") or ""),
                uploaded_at=float(v.get("uploaded_at") or 0.0),
                caption=str(v.get("caption") or ""),
                tags=[str(t) for t in (v.get("tags") or [])],
                extraction_ok=bool(v.get("extraction_ok", False)),
                verdict_one_liner=str(v.get("verdict_one_liner") or ""),
            )
        except Exception:  # noqa: BLE001
            continue
    return out


def _save_library(library: Dict[str, LibraryEntry]) -> None:
    path = _library_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {k: asdict(v) for k, v in library.items()}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _short_id(sha: str) -> str:
    return sha[:8]


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------


async def _get_file_path(bot_token: str, file_id: str) -> Optional[str]:
    url = f"{_TELEGRAM_API}{bot_token}/getFile"
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            response = await client.post(url, json={"file_id": file_id})
            response.raise_for_status()
            data = response.json()
            if not data.get("ok"):
                return None
            return str(data.get("result", {}).get("file_path") or "") or None
    except Exception:  # noqa: BLE001
        logger.warning("telegram getFile failed", exc_info=True)
        return None


async def _download_bytes(bot_token: str, file_path: str) -> Optional[bytes]:
    url = f"{_TELEGRAM_FILE_API}{bot_token}/{file_path}"
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.content
    except Exception:  # noqa: BLE001
        logger.warning("telegram file download failed", exc_info=True)
        return None


def _pick_largest_photo_variant(photos: List[dict]) -> Optional[dict]:
    """Telegram resends photos at multiple resolutions; pick the biggest
    one (largest file_size, falling back to width × height)."""
    if not photos:
        return None
    def size_key(p: dict) -> int:
        s = int(p.get("file_size") or 0)
        if s:
            return s
        return int(p.get("width") or 0) * int(p.get("height") or 0)
    return sorted(photos, key=size_key, reverse=True)[0]


@dataclass
class IngestResult:
    ok: bool
    entry: Optional[LibraryEntry] = None
    error: str = ""


async def ingest_telegram_document(
    bot_token: str,
    user_id: str,
    document: dict,
    caption: str = "",
) -> IngestResult:
    """Download a Telegram document (PDF or image-as-file) and save it
    as a design reference. Mirrors ``ingest_telegram_photo`` but takes
    the single ``document`` payload rather than a list of resized
    photo variants."""
    file_id = str(document.get("file_id") or "")
    if not file_id:
        return IngestResult(ok=False, error="missing file_id")

    file_path = await _get_file_path(bot_token, file_id)
    if not file_path:
        return IngestResult(ok=False, error="getFile failed")

    data = await _download_bytes(bot_token, file_path)
    if not data:
        return IngestResult(ok=False, error="file download failed")

    sha = hashlib.sha256(data).hexdigest()
    # Honor the original extension when possible — design_vision uses
    # the extension to decide between image-prompt and pdf-prompt.
    name = str(document.get("file_name") or "")
    suffix = Path(name).suffix.lower() or Path(file_path).suffix.lower() or ".bin"
    out_dir = _user_dir(user_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{sha[:12]}{suffix}"
    if not out_path.exists():
        out_path.write_bytes(data)

    entry = LibraryEntry(
        id=_short_id(sha),
        sha=sha,
        path=str(out_path),
        user_id=str(user_id),
        uploaded_at=time.time(),
        caption=caption.strip(),
        tags=_auto_tag_from_caption(caption),
        extraction_ok=False,
    )
    library = _load_library()
    library[entry.id] = entry
    _save_library(library)
    return IngestResult(ok=True, entry=entry)


async def ingest_telegram_photo(
    bot_token: str,
    user_id: str,
    photos: List[dict],
    caption: str = "",
) -> IngestResult:
    """Download the largest photo variant, save it, and add to the library.

    The caller is responsible for kicking off vision extraction (or it
    can be done lazily on first attach). Returns a result with the new
    LibraryEntry on success.
    """
    largest = _pick_largest_photo_variant(photos)
    if not largest:
        return IngestResult(ok=False, error="no photo payload")
    file_id = str(largest.get("file_id") or "")
    if not file_id:
        return IngestResult(ok=False, error="missing file_id")

    file_path = await _get_file_path(bot_token, file_id)
    if not file_path:
        return IngestResult(ok=False, error="getFile failed")

    data = await _download_bytes(bot_token, file_path)
    if not data:
        return IngestResult(ok=False, error="file download failed")

    sha = hashlib.sha256(data).hexdigest()
    suffix = Path(file_path).suffix.lower() or ".jpg"
    out_dir = _user_dir(user_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{sha[:12]}{suffix}"
    if not out_path.exists():
        out_path.write_bytes(data)

    entry = LibraryEntry(
        id=_short_id(sha),
        sha=sha,
        path=str(out_path),
        user_id=str(user_id),
        uploaded_at=time.time(),
        caption=caption.strip(),
        tags=_auto_tag_from_caption(caption),
        extraction_ok=False,
    )
    library = _load_library()
    library[entry.id] = entry
    _save_library(library)
    return IngestResult(ok=True, entry=entry)


_CAPTION_TAG_RE = None


def _auto_tag_from_caption(caption: str) -> List[str]:
    """Pull lightweight tags from a caption. Hashtag style or
    comma-separated keywords. ``"#warm,#dense"`` and ``"warm dense"``
    both produce ``["warm", "dense"]``."""
    if not caption:
        return []
    import re
    text = caption.lower()
    raw = re.split(r"[\s,#]+", text)
    out: list[str] = []
    seen: set = set()
    for word in raw:
        word = word.strip().strip(".:;!?")
        if not word or len(word) < 2 or len(word) > 24:
            continue
        if word in seen:
            continue
        seen.add(word)
        out.append(word)
    return out[:8]


# ---------------------------------------------------------------------------
# Vision extraction (kicked off after ingest)
# ---------------------------------------------------------------------------


async def run_vision_extraction(entry_id: str) -> bool:
    """Call design_vision.extract on an entry and update its
    ``extraction_ok`` + ``verdict_one_liner``. Returns True on success."""
    library = _load_library()
    entry = library.get(entry_id)
    if entry is None:
        return False
    try:
        from skyn3t.agents.design_vision import extract
        ref = await extract(Path(entry.path))
    except Exception:  # noqa: BLE001
        logger.exception("design vision extract failed")
        return False
    if ref is None:
        return False
    entry.extraction_ok = True
    entry.verdict_one_liner = ref.verdict_one_liner
    # Append mood adjectives as tags so /references search by mood works.
    for word in ref.mood:
        slug = word.strip().lower().replace(" ", "-")
        if slug and slug not in entry.tags:
            entry.tags.append(slug)
    _save_library(library)
    return True


# ---------------------------------------------------------------------------
# Lookups / matching
# ---------------------------------------------------------------------------


def list_references(user_id: Optional[str] = None) -> List[LibraryEntry]:
    """Return all references in the library, optionally filtered to a user."""
    lib = _load_library()
    entries = list(lib.values())
    if user_id is not None:
        entries = [e for e in entries if e.user_id == str(user_id)]
    return sorted(entries, key=lambda e: e.uploaded_at, reverse=True)


def get_reference(entry_id: str) -> Optional[LibraryEntry]:
    return _load_library().get(entry_id)


def update_tags(entry_id: str, add: Optional[List[str]] = None, remove: Optional[List[str]] = None) -> Optional[LibraryEntry]:
    lib = _load_library()
    entry = lib.get(entry_id)
    if entry is None:
        return None
    if add:
        for t in add:
            t = t.strip().lower()
            if t and t not in entry.tags:
                entry.tags.append(t)
    if remove:
        entry.tags = [t for t in entry.tags if t.lower() not in {r.strip().lower() for r in remove}]
    lib[entry_id] = entry
    _save_library(lib)
    return entry


def recent_uploads(user_id: str, window_seconds: float = ATTACH_WINDOW_SECONDS) -> List[LibraryEntry]:
    """Photos uploaded by ``user_id`` within the last ``window_seconds``
    — the candidate set for the auto-attach window."""
    now = time.time()
    return [
        e
        for e in list_references(user_id)
        if now - e.uploaded_at <= window_seconds
    ]


def match_references_to_brief(brief: str, user_id: str, limit: int = 3) -> List[LibraryEntry]:
    """Return references whose tags overlap with words in the brief.
    Order by overlap count desc, then recency."""
    if not brief:
        return []
    import re
    brief_words = {
        w for w in re.findall(r"[a-z]{3,}", brief.lower())
        if w not in {"the", "and", "for", "with", "build", "make", "create", "small", "simple"}
    }
    candidates = list_references(user_id)
    scored: list[tuple[int, float, LibraryEntry]] = []
    for entry in candidates:
        overlap = sum(1 for t in entry.tags if t in brief_words)
        if overlap > 0:
            scored.append((overlap, entry.uploaded_at, entry))
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return [s[2] for s in scored[:limit]]


# ---------------------------------------------------------------------------
# Attaching to projects
# ---------------------------------------------------------------------------


def attach_references_to_project(project_dir: Path, entry_ids: List[str]) -> None:
    """Write the matched references into ``<project_dir>/design_references.md``
    so the DesignerAgent can include them in its prompt."""
    entries = [get_reference(eid) for eid in entry_ids]
    entries = [e for e in entries if e is not None]
    if not entries:
        return
    project_dir = Path(project_dir)
    project_dir.mkdir(parents=True, exist_ok=True)
    out_path = project_dir / "design_references.md"

    blocks: list[str] = ["# Design references (user-supplied)\n"]
    for entry in entries:
        blocks.append(f"\n## Reference `{entry.id}`")
        if entry.caption:
            blocks.append(f"_caption: {entry.caption}_\n")
        if entry.tags:
            blocks.append(f"Tags: {', '.join(entry.tags)}\n")
        if entry.verdict_one_liner:
            blocks.append(f"> {entry.verdict_one_liner}\n")
        # Pull the rendered fragment from the cached extraction if it exists.
        try:
            from skyn3t.agents.design_vision import load_by_sha
            ref = load_by_sha(entry.sha)
        except Exception:  # noqa: BLE001
            ref = None
        if ref is not None:
            blocks.append(ref.to_brand_md_fragment())
            blocks.append("")
        else:
            blocks.append(f"_(no vision extraction available — image at `{entry.path}`)_\n")

    out_path.write_text("\n".join(blocks), encoding="utf-8")


def project_has_attached_references(project_dir: Path) -> bool:
    return (Path(project_dir) / "design_references.md").exists()


# ---------------------------------------------------------------------------
# Canonical brand registration
# ---------------------------------------------------------------------------

# The default reference attached to every new project. It lives next to
# the library JSON so users can drop files (e.g. their logo) at
# ``data/design_references/canonical_brand.png`` and have them
# automatically respected. The bot's startup registers any
# ``canonical_*.png`` it finds, tagged ``canonical`` so they match every
# brief.
_CANONICAL_TAGS = ["canonical", "brand", "default"]


def register_canonical_references() -> List[LibraryEntry]:
    """Scan ``data/design_references/canonical_*.png`` and register any
    files not already in the library. Returns the entries that were
    added or updated."""
    base = _refs_dir()
    if not base.exists():
        return []
    library = _load_library()
    added: List[LibraryEntry] = []
    for path in sorted(base.glob("canonical_*.png")):
        try:
            data = path.read_bytes()
        except Exception:  # noqa: BLE001
            continue
        sha = hashlib.sha256(data).hexdigest()
        existing = next((e for e in library.values() if e.sha == sha), None)
        if existing is not None:
            # Already registered. Make sure the canonical tags are on it.
            updated = False
            for tag in _CANONICAL_TAGS:
                if tag not in existing.tags:
                    existing.tags.append(tag)
                    updated = True
            if updated:
                library[existing.id] = existing
            continue
        entry = LibraryEntry(
            id=_short_id(sha),
            sha=sha,
            path=str(path),
            user_id="system",
            uploaded_at=time.time(),
            caption=f"canonical reference: {path.name}",
            tags=list(_CANONICAL_TAGS),
            extraction_ok=False,
        )
        library[entry.id] = entry
        added.append(entry)
    if added or library:
        _save_library(library)
    return added


async def extract_canonical_references() -> int:
    """Run vision extraction on every canonical entry that hasn't been
    extracted yet. Returns the count of successful extractions."""
    library = _load_library()
    canonical_ids = [
        entry_id for entry_id, e in library.items()
        if "canonical" in e.tags and not e.extraction_ok
    ]
    if not canonical_ids:
        return 0
    ok_count = 0
    for entry_id in canonical_ids:
        try:
            if await run_vision_extraction(entry_id):
                ok_count += 1
        except Exception:  # noqa: BLE001
            logger.exception("canonical extraction failed for %s", entry_id)
    return ok_count


def list_canonical_references() -> List[LibraryEntry]:
    return [e for e in _load_library().values() if "canonical" in e.tags]
