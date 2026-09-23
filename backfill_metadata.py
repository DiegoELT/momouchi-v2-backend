#!/usr/bin/env python3
"""
Backfill trim and match metadata into annotation exports — RUN LOCALLY.

Two independent repairs, in the order that matters:

1. TRIM, from an older copy of the same export that still has it. Matched by
   caption fingerprint, not filename, so a rename or a " (1)" suffix cannot
   pair the wrong files. No YouTube call, so caption text cannot change under
   existing labels.

2. MATCH METADATA (patch, datetime_utc, gamelength, scoreboard) from
   Leaguepedia. Needs network + credentials, so this half only works on a
   machine that can reach Leaguepedia.

Trim is restored FIRST because it is what disambiguates a series VOD: every
game of a best-of-N shares one VOD URL, and the trim window tells us which of
the candidate rows is actually the annotated game.

Captions, labels, flags and events are never touched — the script asserts this
before writing.

Usage:
    python backfill_metadata.py <annotations-dir> --old-corpus <dir>     # dry run
    python backfill_metadata.py <annotations-dir> --old-corpus <dir> --write
    python backfill_metadata.py <annotations-dir> --trim-only            # no network
"""

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

# Casters name the game they are calling ("this is game number one"). When two
# candidate rows have indistinguishable lengths, that phrase is a far stronger
# signal than a 5% difference in recorded duration.
_ORDINALS = {
    "one": 1, "1": 1, "first": 1,
    "two": 2, "2": 2, "second": 2,
    "three": 3, "3": 3, "third": 3,
    "four": 4, "4": 4, "fourth": 4,
    "five": 5, "5": 5, "fifth": 5,
}
_GAME_CUE = re.compile(
    r"\bgame\s+(?:number\s+)?(one|two|three|four|five|[1-5])\b"
    r"|\b(first|second|third|fourth|fifth)\s+game\b"
)


def game_number_cues(captions):
    """Count explicit 'game N' references in the annotated commentary."""
    text = " ".join((c.get("text") or "") for c in captions).lower()
    counts = Counter()
    for a, b in _GAME_CUE.findall(text):
        word = a or b
        if word in _ORDINALS:
            counts[_ORDINALS[word]] += 1
    return counts

# A candidate game is accepted when its recorded length is within this factor of
# the annotated trim window. Correctly matched games observed at 0.97-1.06.
RATIO_MIN, RATIO_MAX = 0.92, 1.15

REQUIRED = ("patch", "datetime_utc", "gamelength")


def fingerprint(captions):
    starts = [round(float(c.get("start", 0)), 3) for c in captions]
    return hashlib.sha256(json.dumps(starts).encode()).hexdigest()


def parse_gamelength(value):
    if not value:
        return None
    try:
        seconds = 0.0
        for part in str(value).split(":"):
            seconds = seconds * 60 + float(part)
        return seconds
    except ValueError:
        return None


def match_of(data):
    m = data.get("matchInfo")
    if isinstance(m, list):
        m = m[0] if m else None
    return m or {}


def index_old_corpus(directory):
    """Map caption-fingerprint -> trim, from an older export set."""
    index = {}
    for path in sorted(Path(directory).glob("*.json")):
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        caps = d.get("captions") or []
        trim = d.get("trim")
        if caps and isinstance(trim, dict) and trim.get("start") is not None:
            index[fingerprint(caps)] = (trim, path.name)
    return index


def describe_candidates(candidates, trim_window):
    lines = []
    for row in candidates:
        length = parse_gamelength(row.get("gamelength"))
        ratio = (trim_window / length) if (length and trim_window) else None
        lines.append(
            f"      game {row.get('n_gameinmatch', '?')}: {row.get('gameid')} "
            f"| length {row.get('gamelength')} "
            f"| ratio {ratio:.2f}" if ratio else
            f"      game {row.get('n_gameinmatch', '?')}: {row.get('gameid')} "
            f"| length {row.get('gamelength')}"
        )
    return lines


def choose_candidate(candidates, trim_window, cues=None):
    """
    Pick the ScoreboardGames row that matches the annotated window.

    Length first; if two games are too close to separate, fall back to what the
    casters actually say. Returns (row, reason) or (None, reason) — refuses
    rather than guessing.
    """
    if not candidates:
        return None, "no Leaguepedia game matches this VOD"
    if len(candidates) == 1:
        return candidates[0], "only candidate"

    if not trim_window:
        return None, (
            f"{len(candidates)} games share this VOD and there is no trim "
            f"window to tell them apart"
        )

    scored = []
    for row in candidates:
        length = parse_gamelength(row.get("gamelength"))
        if not length:
            continue
        scored.append((abs(trim_window / length - 1.0), trim_window / length, row))
    if not scored:
        return None, f"{len(candidates)} candidates, none with a usable gamelength"

    scored.sort(key=lambda t: t[0])
    best_delta, best_ratio, best = scored[0]
    if not (RATIO_MIN <= best_ratio <= RATIO_MAX):
        return None, (
            f"best of {len(candidates)} candidates is off "
            f"(ratio {best_ratio:.2f}); refusing to guess"
        )
    if len(scored) > 1 and abs(scored[1][0] - best_delta) < 0.03:
        # Lengths cannot separate them — ask the commentary.
        # The cue names a game within ITS match. A stream carrying several
        # matches has several "game one"s, so the cue is only decisive when the
        # candidates all belong to the same match.
        matches = {c.get("gameid", "").rsplit("_", 1)[0] for c in candidates}
        if cues and len(matches) > 1:
            return None, (
                f"{len(candidates)} candidates span {len(matches)} matches; "
                f"a \"game N\" cue cannot separate them"
            )
        if cues:
            top, n = cues.most_common(1)[0]
            runner = cues.most_common(2)[1][1] if len(cues) > 1 else 0
            if n >= 2 and n > runner:
                for row in candidates:
                    if str(row.get("n_gameinmatch")) == str(top):
                        return row, (
                            f"lengths tied (ratios {best_ratio:.2f}/"
                            f"{scored[1][1]:.2f}); commentary says "
                            f"\"game {top}\" x{n}"
                        )
        return None, (
            f"two candidates fit equally well "
            f"(ratios {best_ratio:.2f} and {scored[1][1]:.2f}); ambiguous"
        )
    return best, f"chose game {best.get('n_gameinmatch', '?')} by length (ratio {best_ratio:.2f})"


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("annotations", help="directory of annotation export files")
    p.add_argument("--old-corpus", help="directory of older exports that still have trim")
    p.add_argument("--write", action="store_true", help="apply the changes")
    p.add_argument("--trim-only", action="store_true", help="skip Leaguepedia entirely")
    p.add_argument("--show-candidates", action="store_true",
                   help="list every Leaguepedia row sharing each VOD, and write nothing")
    p.add_argument("--resolve", help='JSON map {"<filename>": <game number>} for '
                                     "cases the script refuses to decide")
    p.add_argument("--overwrite", action="store_true",
                   help="replace existing values instead of only filling gaps")
    args = p.parse_args(argv)

    overrides = {}
    if args.resolve:
        overrides = json.loads(Path(args.resolve).read_text(encoding="utf-8"))

    old_index = index_old_corpus(args.old_corpus) if args.old_corpus else {}
    if args.old_corpus:
        print(f"Trim source: {len(old_index)} usable export(s) in {args.old_corpus}\n")

    fetch_candidates = attach_players = None
    if not args.trim_only:
        try:
            from sources import fetch_match_candidates, attach_players as _ap
            fetch_candidates, attach_players = fetch_match_candidates, _ap
        except Exception as e:
            print(f"Leaguepedia unavailable ({e}) — running as --trim-only.\n")

    changed = unresolved = untouched = 0

    target = Path(args.annotations)
    paths = sorted(target.glob("*.json")) if target.is_dir() else [target]

    for path in paths:
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except Exception as e:
            print(f"! {path.name}: unreadable ({e})")
            continue

        captions = data.get("captions") or []
        if not captions:
            continue

        before_caps = json.dumps(captions, sort_keys=True)
        before_events = json.dumps(data.get("events") or [], sort_keys=True)

        notes = []

        # --- 1. trim -------------------------------------------------------
        trim = data.get("trim")
        if (trim is None or args.overwrite) and old_index:
            hit = old_index.get(fingerprint(captions))
            if hit:
                restored, src = hit
                data["trim"] = restored
                notes.append(f"trim {restored['start']:.0f}-{restored['end']:.0f} "
                             f"from {src}")
            elif trim is None:
                notes.append("trim MISSING (no fingerprint match in --old-corpus)")

        trim_now = data.get("trim") if isinstance(data.get("trim"), dict) else None
        window = None
        if trim_now and trim_now.get("start") is not None and trim_now.get("end") is not None:
            window = trim_now["end"] - trim_now["start"]

        # --- 2. match metadata ---------------------------------------------
        m = match_of(data)
        missing = [f for f in REQUIRED if m.get(f) in (None, "", [])]
        if (missing or args.overwrite or args.show_candidates) and fetch_candidates:
            vid = data.get("video_id")
            if not vid:
                notes.append("no video_id — cannot look up")
            else:
                try:
                    candidates = fetch_candidates(vid)
                except Exception as e:
                    candidates = []
                    notes.append(f"lookup failed: {e}")
                if args.show_candidates:
                    notes.append(f"candidates for {vid} "
                                 f"(trim window {window/60:.1f} min)"
                                 if window else f"candidates for {vid} (no trim)")
                    notes.extend(describe_candidates(candidates, window))
                    print(f"{path.name}")
                    for n in notes:
                        print(f"    {n}")
                    print()
                    continue

                cues = game_number_cues(captions)
                forced = overrides.get(path.name)
                if forced is not None:
                    # A gameid is unique; a game number is not when the VOD
                    # carries several matches. Prefer the gameid.
                    by_id = [c for c in candidates if c.get("gameid") == str(forced)]
                    by_num = [c for c in candidates
                              if str(c.get("n_gameinmatch")) == str(forced)]
                    if by_id:
                        row, reason = by_id[0], f"gameid {forced} chosen by --resolve"
                    elif len(by_num) == 1:
                        row, reason = by_num[0], f"game {forced} chosen by --resolve"
                    elif len(by_num) > 1:
                        row, reason = None, (
                            f"--resolve says game {forced}, but {len(by_num)} "
                            f"candidates share that number — use the full gameid"
                        )
                    else:
                        row, reason = None, f"--resolve says {forced}, no such candidate"
                else:
                    row, reason = choose_candidate(candidates, window, cues)
                if row is None:
                    notes.append(f"metadata UNRESOLVED — {reason}")
                    if candidates:
                        notes.extend(describe_candidates(candidates, window))
                        if cues:
                            notes.append(
                                "      commentary game cues: "
                                + ", ".join(f"game {k} x{v}" for k, v in cues.most_common())
                            )
                        notes.append(
                            "      resolve by hand: add this file to --resolve "
                            "as \"<filename>\": <game number>"
                        )
                    unresolved += 1
                else:
                    chosen_len = parse_gamelength(row.get("gamelength"))
                    if window and chosen_len:
                        ratio = window / chosen_len
                        if not (RATIO_MIN <= ratio <= RATIO_MAX):
                            notes.append(
                                f"      WARNING: chosen row's length "
                                f"{row.get('gamelength')} vs trim window "
                                f"{window/60:.1f} min (ratio {ratio:.2f}) — "
                                f"check this one by hand"
                            )
                    try:
                        row = attach_players(row)
                    except Exception:
                        pass  # scoreboard is a bonus, not the point
                    merged = dict(m)
                    for k, v in row.items():
                        if args.overwrite or merged.get(k) in (None, "", []):
                            merged[k] = v
                    if isinstance(data.get("matchInfo"), list):
                        data["matchInfo"] = [merged] + data["matchInfo"][1:]
                    else:
                        data["matchInfo"] = merged
                    notes.append(
                        f"metadata: patch {merged.get('patch')}, "
                        f"{str(merged.get('datetime_utc'))[:10]}, "
                        f"{merged.get('gamelength')} ({reason})"
                    )
        elif missing:
            notes.append(f"missing {', '.join(missing)} (no Leaguepedia this run)")

        if not notes:
            untouched += 1
            continue

        print(f"{path.name}")
        for n in notes:
            print(f"    {n}")

        # Never let a repair touch the annotations themselves.
        assert json.dumps(data.get("captions"), sort_keys=True) == before_caps, \
            f"{path.name}: captions changed — refusing to write"
        assert json.dumps(data.get("events") or [], sort_keys=True) == before_events, \
            f"{path.name}: events changed — refusing to write"

        if args.write and not any("UNRESOLVED" in n or "MISSING" in n for n in notes):
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                           encoding="utf-8")
            tmp.replace(path)
            changed += 1

    print()
    if args.write:
        print(f"Wrote {changed} file(s). {unresolved} unresolved, {untouched} already complete.")
    else:
        print(f"Dry run — pass --write to apply. {unresolved} would remain unresolved, "
              f"{untouched} already complete.")
    return 1 if unresolved else 0


if __name__ == "__main__":
    sys.exit(main())
