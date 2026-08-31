#!/usr/bin/env python3
"""
Corpus ingestion — RUN THIS LOCALLY, not on the server.

YouTube blocks datacenter IPs, so caption fetching only works from a normal
residential connection. This script pulls the captions and Leaguepedia metadata
for a VOD once and writes a snapshot into data/corpus/. The hosted backend then
serves those files and needs no outbound network access at all.

VODs carry pick/ban and post-game content that is not annotated material, so a
trim can be recorded with --start/--end. Captions are always stored in FULL; the
trim is applied when serving. Use --retrim to change the bounds later without
going back to YouTube.

Usage:
    python ingest.py https://www.youtube.com/watch?v=XXXX [more urls...]
    python ingest.py <url> --start 4:35 --end 41:20
    python ingest.py --from-file urls.txt
    python ingest.py --list
    python ingest.py --retrim <url> --start 5:02      # no re-fetch
    python ingest.py --refresh <url>                  # re-fetch (see warning)

Ingested games are meant to be committed to git — the snapshot IS the corpus.

The same operations are available from the frontend ("Add to corpus"), which
calls the identical functions through the local backend.
"""

import argparse
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

import corpus
from sources import (
    SourceUnavailable,
    extract_youtube_id,
    fetch_captions,
    fetch_match_details,
)


def parse_time(value: str | None) -> float | None:
    """Accepts seconds ("275"), mm:ss ("4:35") or hh:mm:ss."""
    if value in (None, ""):
        return None
    parts = str(value).strip().split(":")
    try:
        numbers = [float(p) for p in parts]
    except ValueError:
        raise ValueError(f"Could not parse a time from {value!r}")
    seconds = 0.0
    for n in numbers:
        seconds = seconds * 60 + n
    return seconds


def fmt(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"


def ingest_one(
    video_url: str, refresh: bool = False, trim: dict | None = None
) -> bool:
    video_id = extract_youtube_id(video_url)
    if not video_id:
        print(f"  ✗ could not extract a video id from {video_url}")
        return False

    if corpus.exists(video_id) and not refresh:
        print(
            f"  · {video_id} already ingested "
            f"(use --retrim to change bounds, --refresh to re-fetch)"
        )
        return True

    print(f"  → {video_id}: fetching captions...", flush=True)
    try:
        captions = fetch_captions(video_id)
    except SourceUnavailable as e:
        print(f"  ✗ {e}")
        return False

    if not captions:
        print(f"  ✗ {video_id}: no captions returned")
        return False

    print(f"    {len(captions)} caption blocks", flush=True)

    print("    fetching match details from Leaguepedia...", flush=True)
    try:
        match = fetch_match_details(video_id)
    except SourceUnavailable as e:
        print(f"    ! Leaguepedia lookup failed ({e})")
        print("    ! saving captions anyway; patch/teams can be filled in by hand")
        match = None

    if match:
        print(
            f"    {match.get('team1', {}).get('team_name')} vs "
            f"{match.get('team2', {}).get('team_name')} "
            f"— patch {match.get('patch') or '?'}"
        )
    else:
        print("    ! no Leaguepedia game matched this VOD")

    path = corpus.save(video_id, video_url, captions, match, trim)
    kept = len(corpus.apply_trim(captions, trim))
    if trim:
        print(
            f"    trim {fmt(trim.get('start'))} → {fmt(trim.get('end'))}: "
            f"keeping {kept} of {len(captions)} blocks"
        )
    print(f"  ✓ wrote {path.relative_to(Path(__file__).parent)}")
    return True


def retrim_one(video_url: str, trim: dict | None) -> bool:
    video_id = extract_youtube_id(video_url) or video_url
    if not corpus.exists(video_id):
        print(f"  ✗ {video_id} is not in the corpus")
        return False

    entry = corpus.set_trim(video_id, trim)
    print(
        f"  ✓ {video_id}: trim {fmt((trim or {}).get('start'))} → "
        f"{fmt((trim or {}).get('end'))}, keeping "
        f"{entry['trimmed_count']} of {entry['caption_count']} blocks"
    )
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("urls", nargs="*", help="YouTube VOD URLs to ingest")
    parser.add_argument("--from-file", help="file with one VOD URL per line")
    parser.add_argument("--start", help="trim start (seconds, mm:ss or hh:mm:ss)")
    parser.add_argument("--end", help="trim end (seconds, mm:ss or hh:mm:ss)")
    parser.add_argument(
        "--retrim",
        action="store_true",
        help="change trim bounds of an already-ingested game (no re-fetch)",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help=(
            "re-fetch games already in the corpus. WARNING: YouTube may return "
            "different captions, which breaks existing annotations. Use "
            "--retrim if you only need to change the bounds."
        ),
    )
    parser.add_argument(
        "--list", action="store_true", help="list what is already ingested"
    )
    args = parser.parse_args(argv)

    if args.list:
        listing = corpus.entries()
        if not listing:
            print(f"Corpus is empty ({corpus.DATA_DIR})")
            return 0
        print(f"{len(listing)} game(s) in {corpus.DATA_DIR}:")
        for e in listing:
            t = e.get("trim") or {}
            bounds = (
                f"  [{fmt(t.get('start'))} → {fmt(t.get('end'))}]" if t else ""
            )
            print(
                f"  {e['video_id']}  {e.get('team1') or '?'} vs "
                f"{e.get('team2') or '?'}  patch {e.get('patch') or '?'}  "
                f"{e['trimmed_count']}/{e['caption_count']} captions{bounds}"
            )
        return 0

    urls = list(args.urls)
    if args.from_file:
        for line in Path(args.from_file).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                urls.append(line)

    if not urls:
        parser.print_help()
        return 1

    try:
        trim = corpus.normalize_trim(parse_time(args.start), parse_time(args.end))
    except ValueError as e:
        print(f"error: {e}")
        return 2

    if trim and len(urls) > 1:
        print("error: --start/--end apply to a single VOD, not a batch")
        return 2

    if args.retrim:
        failures = sum(0 if retrim_one(u, trim) else 1 for u in urls)
        return 1 if failures else 0

    print(f"Ingesting {len(urls)} VOD(s) into {corpus.DATA_DIR}\n")
    failures = 0
    for url in urls:
        print(url)
        if not ingest_one(url, refresh=args.refresh, trim=trim):
            failures += 1
        print()

    print(f"Done. {len(urls) - failures} succeeded, {failures} failed.")
    if failures:
        print(
            "\nIf captions failed with a blocked-request error, you are not on a "
            "residential connection — try again off VPN / off the university network."
        )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
