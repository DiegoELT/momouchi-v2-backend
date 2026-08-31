"""
The corpus snapshot: one JSON file per VOD, holding the captions and match
metadata exactly as they were fetched.

This is the tool's source of truth once a game has been ingested. The hosted
backend only ever reads from here, which is what makes it independent of
YouTube's IP blocking.

It also matters methodologically: the annotation guidelines define the unit of
annotation as the YouTube caption block "as delivered by the tool". YouTube
regenerates ASR captions when its models improve, so without a frozen snapshot
the unit of annotation can change underneath an in-progress annotation pass
without anyone noticing. Ingest once, then leave it alone.

TRIMMING
--------
VODs carry pick/ban and post-game content that is not part of the annotated
material. The snapshot stores the FULL caption list plus the trim bounds
separately, and serves the trimmed view. Storing the full list is deliberate:
re-trimming then never requires going back to YouTube, so fixing a bad trim
cannot silently swap the captions underneath existing annotations.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 2

DATA_DIR = Path(
    os.getenv("CORPUS_DIR", Path(__file__).parent / "data" / "corpus")
).resolve()


def _path_for(video_id: str) -> Path:
    # video ids are [A-Za-z0-9_-]; refuse anything else so a crafted id cannot
    # escape the corpus directory.
    if not video_id or not all(c.isalnum() or c in "-_" for c in video_id):
        raise ValueError(f"Invalid video id: {video_id!r}")
    return DATA_DIR / f"{video_id}.json"


def normalize_trim(start=None, end=None) -> dict | None:
    """None for either bound means unbounded. Returns None when both are open."""
    start = None if start in (None, "") else float(start)
    end = None if end in (None, "") else float(end)

    if start is not None and start < 0:
        start = 0.0
    if start is not None and end is not None and end <= start:
        raise ValueError("Trim end must be after trim start.")
    if start is None and end is None:
        return None
    return {"start": start, "end": end}


def apply_trim(captions: list, trim: dict | None) -> list:
    """
    Keep any caption block that OVERLAPS the window — matching how the frontend
    range filter has always behaved, so a block straddling the boundary is not
    silently dropped.
    """
    if not trim:
        return captions

    start = trim.get("start")
    end = trim.get("end")
    lo = float("-inf") if start is None else start
    hi = float("inf") if end is None else end

    return [
        c
        for c in captions
        if (c.get("start", 0) + c.get("duration", 0)) > lo and c.get("start", 0) < hi
    ]


def exists(video_id: str) -> bool:
    try:
        return _path_for(video_id).is_file()
    except ValueError:
        return False


def load(video_id: str) -> dict | None:
    try:
        path = _path_for(video_id)
    except ValueError:
        return None
    if not path.is_file():
        return None
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def _write(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write via a temp file so an interrupted write cannot leave a half-written
    # snapshot behind.
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    tmp.replace(path)
    return path


def save(
    video_id: str,
    video_url: str,
    captions: list,
    match: dict | None,
    trim: dict | None = None,
) -> Path:
    path = _path_for(video_id)

    payload = {
        "schema_version": SCHEMA_VERSION,
        "video_id": video_id,
        "video_url": video_url,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "trim": trim,
        # Full, untrimmed capture — never overwritten by a re-trim.
        "caption_count": len(captions),
        "trimmed_count": len(apply_trim(captions, trim)),
        "captions": captions,
        "match": match,
    }
    return _write(path, payload)


def set_trim(video_id: str, trim: dict | None) -> dict | None:
    """
    Re-trim an existing snapshot from the stored full captions. Does not touch
    YouTube, so the caption text is guaranteed identical to what was ingested.
    """
    entry = load(video_id)
    if entry is None:
        return None

    entry["trim"] = trim
    entry["trimmed_count"] = len(apply_trim(entry.get("captions") or [], trim))
    entry["retrimmed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _write(_path_for(video_id), entry)
    return entry


def entries() -> list[dict]:
    """Lightweight listing for the frontend picker — no caption payloads."""
    if not DATA_DIR.is_dir():
        return []

    listing = []
    for path in sorted(DATA_DIR.glob("*.json")):
        try:
            with path.open(encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            continue

        match = data.get("match") or {}
        captions = data.get("captions") or []
        trim = data.get("trim")

        listing.append(
            {
                "video_id": data.get("video_id", path.stem),
                "video_url": data.get("video_url"),
                "fetched_at": data.get("fetched_at"),
                "trim": trim,
                "caption_count": data.get("caption_count") or len(captions),
                "trimmed_count": data.get("trimmed_count")
                or len(apply_trim(captions, trim)),
                "gameid": match.get("gameid"),
                "tournament": match.get("tournament"),
                "team1": (match.get("team1") or {}).get("team_name"),
                "team2": (match.get("team2") or {}).get("team_name"),
                "patch": match.get("patch"),
                "datetime_utc": match.get("datetime_utc"),
            }
        )

    listing.sort(key=lambda e: (e.get("datetime_utc") or "", e.get("video_id") or ""))
    return listing
