#!/usr/bin/env python3
"""
Corpus statistics — how much annotatable material is in data/corpus.

"Segments" means caption blocks INSIDE the trim window, i.e. the units that are
actually annotated. The untrimmed totals are reported alongside so the two are
never confused in the thesis.

Usage:
    python corpus_stats.py              # summary + per-game table
    python corpus_stats.py --json       # machine-readable
    python corpus_stats.py --csv out.csv
"""

import argparse
import csv
import json
import statistics
import sys

import corpus


def game_stats(entry: dict) -> dict:
    captions = entry.get("captions") or []
    trim = entry.get("trim")
    kept = corpus.apply_trim(captions, trim)

    words = [len((c.get("text") or "").split()) for c in kept]
    match = entry.get("match") or {}

    window = None
    if trim and trim.get("start") is not None and trim.get("end") is not None:
        window = trim["end"] - trim["start"]

    # A VOD of a best-of-N carries the same URL on every game row in
    # Leaguepedia, so metadata can end up attached to the wrong game of the
    # series. Comparing the trim window against the recorded game length is the
    # cheapest way to catch that.
    game_seconds = parse_gamelength(match.get("gamelength"))
    length_ratio = (window / game_seconds) if (window and game_seconds) else None

    return {
        "video_id": entry.get("video_id"),
        "gameid": match.get("gameid"),
        "tournament": match.get("tournament"),
        "team1": (match.get("team1") or {}).get("team_name"),
        "team2": (match.get("team2") or {}).get("team_name"),
        "patch": match.get("patch"),
        "segments": len(kept),
        "segments_untrimmed": len(captions),
        "trim_start": (trim or {}).get("start"),
        "trim_end": (trim or {}).get("end"),
        "window_seconds": window,
        "gamelength_seconds": game_seconds,
        "length_ratio": round(length_ratio, 3) if length_ratio else None,
        "words": sum(words),
        "mean_words_per_segment": (statistics.mean(words) if words else 0),
        "single_word_segments": sum(1 for w in words if w == 1),
        "empty_segments": sum(1 for w in words if w == 0),
        # Trust but verify: the cached count must match a recomputation.
        "cached_count_matches": entry.get("trimmed_count") == len(kept),
    }


def collect() -> list[dict]:
    rows = []
    for listed in corpus.entries():
        entry = corpus.load(listed["video_id"])
        if entry is None:
            continue
        rows.append(game_stats(entry))
    return rows


def parse_gamelength(value):
    """Leaguepedia Gamelength is "mm:ss" (or "h:mm:ss")."""
    if not value:
        return None
    try:
        seconds = 0.0
        for part in str(value).split(":"):
            seconds = seconds * 60 + float(part)
        return seconds
    except ValueError:
        return None


def patch_key(patch: str):
    """Sort 7.12 before 26.16. Riot moved to year-based numbering in 2025, so
    string sorting puts a 2017 patch after a 2026 one."""
    try:
        return tuple(int(p) for p in str(patch).split("."))
    except ValueError:
        return (999, 999)


def fmt_hms(seconds) -> str:
    if not seconds:
        return "—"
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument("--csv", metavar="PATH", help="write a per-game CSV")
    args = parser.parse_args(argv)

    rows = collect()
    if not rows:
        print(f"No games in {corpus.DATA_DIR}")
        return 1

    segments = sum(r["segments"] for r in rows)
    untrimmed = sum(r["segments_untrimmed"] for r in rows)
    words = sum(r["words"] for r in rows)
    per_game = [r["segments"] for r in rows]
    windows = [r["window_seconds"] for r in rows if r["window_seconds"]]

    summary = {
        "games": len(rows),
        "segments": segments,
        "segments_untrimmed": untrimmed,
        "segments_discarded_by_trim": untrimmed - segments,
        "words": words,
        "mean_segments_per_game": round(statistics.mean(per_game), 1),
        "median_segments_per_game": round(statistics.median(per_game), 1),
        "min_segments_per_game": min(per_game),
        "max_segments_per_game": max(per_game),
        "mean_words_per_segment": round(words / segments, 2) if segments else 0,
        "single_word_segments": sum(r["single_word_segments"] for r in rows),
        "empty_segments": sum(r["empty_segments"] for r in rows),
        "annotatable_seconds": round(sum(windows), 1) if windows else None,
        "games_without_trim": sum(1 for r in rows if r["window_seconds"] is None),
        "games_without_match_metadata": sum(1 for r in rows if not r["gameid"]),
        "games_without_patch": sum(1 for r in rows if not r["patch"]),
        "patches": sorted({r["patch"] for r in rows if r["patch"]}, key=patch_key),
    }

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        print(f"Wrote {args.csv}")

    if args.json:
        json.dump({"summary": summary, "games": rows}, sys.stdout, indent=2)
        print()
        return 0

    print(f"Corpus: {corpus.DATA_DIR}\n")
    header = f"{'video_id':14} {'matchup':30} {'patch':7} {'segments':>9} {'of full':>9} {'window':>9}"
    print(header)
    print("-" * len(header))
    for r in sorted(rows, key=lambda x: -x["segments"]):
        matchup = f"{r['team1'] or '?'} vs {r['team2'] or '?'}"
        print(
            f"{r['video_id']:14} {matchup[:30]:30} {r['patch'] or '?':7} "
            f"{r['segments']:>9} {r['segments_untrimmed']:>9} "
            f"{fmt_hms(r['window_seconds']):>9}"
        )
    print("-" * len(header))
    total_label = f"{len(rows)} games"
    print(
        f"{'TOTAL':14} {total_label:30} {'':7} {segments:>9} {untrimmed:>9} "
        f"{fmt_hms(summary['annotatable_seconds']):>9}"
    )

    print("\nSummary")
    print(f"  Games ..................... {summary['games']}")
    print(f"  Annotatable segments ...... {summary['segments']:,}")
    print(f"  Discarded by trimming ..... {summary['segments_discarded_by_trim']:,} "
          f"({summary['segments_discarded_by_trim'] / untrimmed:.0%} of captured)")
    print(f"  Words ..................... {summary['words']:,}")
    print(f"  Annotatable duration ...... {fmt_hms(summary['annotatable_seconds'])}")
    print(f"  Segments per game ......... mean {summary['mean_segments_per_game']}, "
          f"median {summary['median_segments_per_game']}, "
          f"range {summary['min_segments_per_game']}–{summary['max_segments_per_game']}")
    print(f"  Words per segment ......... mean {summary['mean_words_per_segment']}")
    print(f"  Single-word segments ...... {summary['single_word_segments']:,} "
          f"({summary['single_word_segments'] / segments:.1%})")
    if summary["empty_segments"]:
        print(f"  Empty segments ............ {summary['empty_segments']:,}")
    patches = summary["patches"]
    print(f"  Patches ................... {len(patches)} distinct"
          f"{f' ({patches[0]} → {patches[-1]})' if patches else ''}")
    print(f"                              {', '.join(patches) or '—'}")

    problems = []
    if summary["games_without_trim"]:
        problems.append(f"{summary['games_without_trim']} game(s) have no trim window")
    if summary["games_without_match_metadata"]:
        problems.append(
            f"{summary['games_without_match_metadata']} game(s) have no Leaguepedia metadata"
        )
    if summary["games_without_patch"]:
        missing = [r["video_id"] for r in rows if not r["patch"]]
        problems.append(f"no patch recorded for: {', '.join(missing)}")
    mismatched = [
        r for r in rows
        if r["length_ratio"] is not None and not (0.92 <= r["length_ratio"] <= 1.15)
    ]
    for r in mismatched:
        problems.append(
            f"{r['video_id']}: trim window {fmt_hms(r['window_seconds'])} vs "
            f"recorded game length {fmt_hms(r['gamelength_seconds'])} "
            f"(ratio {r['length_ratio']:.2f}) — metadata may belong to a "
            f"different game of the series"
        )

    stale = [r["video_id"] for r in rows if not r["cached_count_matches"]]
    if stale:
        problems.append(
            f"cached trimmed_count disagrees with a recount for: {', '.join(stale)}"
        )

    if problems:
        print("\nCheck:")
        for p in problems:
            print(f"  ! {p}")
    else:
        print("\nAll games trimmed, all metadata present, counts verified.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
