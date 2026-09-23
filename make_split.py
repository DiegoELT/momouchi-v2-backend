#!/usr/bin/env python3
"""
Build the test/dev split from annotated games, stratified by era and SA rate.

Reads annotation EXPORTS (the files Save JSON produces), not corpus snapshots —
the SA rate only exists where labels exist. Selection is deterministic for a
given (seed, pool, parameters), and the written file records every parameter
needed to reproduce it.

Usage:
    python make_split.py ../"Claude outputs"                 # dry run, prints the plan
    python make_split.py <dir> --write                       # writes data/splits/<name>.json
    python make_split.py <dir> --test 8 --dev 4 --seed 20260923
    python make_split.py <dir> --reserve data/splits/reserve.json

The reserve pool is SWAP-IN-ONLY: reserved game ids are excluded from both test
and dev, and are never auto-selected. They exist to replace a selected game that
turns out unlabelable. Swapping one in is a manual edit to the split file.
"""

import argparse
import json
import random
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

SA_LABEL = "Strategic Analysis"

# Era boundaries by broadcast year (upper bound inclusive). This is a
# METHODOLOGICAL choice, not a fact — it defines the strata the split balances
# over, so it belongs in the thesis. Override with --era-bounds.
DEFAULT_ERA_BOUNDS = [(2017, "classic"), (2022, "modern"), (9999, "current")]


def era_for(year, bounds):
    if year is None:
        return "unknown"
    for upper, name in bounds:
        if year <= upper:
            return name
    return bounds[-1][1]


def year_of(match):
    raw = (match or {}).get("datetime_utc")
    if not raw:
        return None
    try:
        return int(str(raw)[:4])
    except ValueError:
        return None


def load_games(directory, want_version):
    """Read every annotation export and keep those at the target version."""
    games, skipped = [], []
    for path in sorted(Path(directory).glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            skipped.append((path.name, f"unreadable: {e}"))
            continue

        version = str(data.get("guideline_version") or "")
        if want_version and version != want_version:
            skipped.append((path.name, f"guideline_version {version or 'missing'}"))
            continue

        captions = data.get("captions") or []
        if not captions:
            skipped.append((path.name, "no captions"))
            continue

        match = data.get("matchInfo")
        if isinstance(match, list):
            match = match[0] if match else None
        match = match or {}

        gameid = match.get("gameid")
        video_id = data.get("video_id")
        if not gameid and not video_id:
            skipped.append((path.name, "no gameid or video_id"))
            continue

        # A partially annotated game must not enter test or dev: its SA rate is
        # measured against a denominator that includes unlabelled segments, and
        # an evaluation set with holes is not an evaluation set.
        # A NON-CONTENT segment is unlabelled on purpose (technical pause,
        # filler). It is not a gap, and it must not sit in the SA-rate
        # denominator — otherwise a game with more pauses looks lower-SA for a
        # reason that has nothing to do with discourse.
        non_content = sum(
            1 for c in captions
            if c.get("label") in (None, "", "None") and c.get("flag") == "NON-CONTENT"
        )
        unlabelled = sum(
            1 for c in captions
            if c.get("label") in (None, "", "None") and c.get("flag") != "NON-CONTENT"
        )
        if unlabelled:
            skipped.append(
                (path.name, f"{unlabelled}/{len(captions)} segments unlabelled")
            )
            continue

        annotatable = len(captions) - non_content
        if annotatable <= 0:
            skipped.append((path.name, "no annotatable segments"))
            continue

        sa = sum(1 for c in captions if c.get("label") == SA_LABEL)
        year = year_of(match)

        games.append(
            {
                "gameid": gameid,
                "video_id": video_id,
                "file": path.name,
                "segments": annotatable,
                "segments_total": len(captions),
                "non_content": non_content,
                "sa_count": sa,
                "sa_rate": sa / annotatable,
                "year": year,
                "patch": match.get("patch"),
            }
        )
    return games, skipped


def tertile_bands(values):
    """Cut points that split values into three roughly equal groups."""
    if len(values) < 3:
        return None
    ordered = sorted(values)
    return (
        statistics.quantiles(ordered, n=3, method="inclusive")
        if len(ordered) >= 3
        else None
    )


def band_for(rate, cuts):
    if not cuts:
        return "all"
    lo, hi = cuts
    if rate <= lo:
        return "sa-low"
    if rate <= hi:
        return "sa-mid"
    return "sa-high"


def pick_reserve(games, cuts, count):
    """
    Choose the swap-in reserve: the games sitting closest to an SA-rate stratum
    boundary. Those are the least disruptive to remove and to swap back in — a
    boundary game is nearly as representative of the band on either side, so
    losing it barely shifts the balance of the draw.

    Deterministic: ties break on game id, not on file order or a seed.
    """
    if not cuts or count <= 0:
        return []

    def distance(g):
        return min(abs(g["sa_rate"] - c) for c in cuts)

    ranked = sorted(games, key=lambda g: (distance(g), ident(g)))
    return ranked[:count]


def allocate(strata, total):
    """Largest-remainder allocation of `total` picks across strata by size."""
    pool = sum(len(v) for v in strata.values())
    if pool == 0:
        return {}
    exact = {k: len(v) * total / pool for k, v in strata.items()}
    base = {k: int(v) for k, v in exact.items()}
    left = total - sum(base.values())
    for k, _ in sorted(exact.items(), key=lambda kv: kv[1] - int(kv[1]), reverse=True):
        if left <= 0:
            break
        if base[k] < len(strata[k]):
            base[k] += 1
            left -= 1
    # If rounding under-filled (strata exhausted), top up anywhere with room.
    while left > 0:
        progressed = False
        for k in sorted(strata, key=lambda k: len(strata[k]), reverse=True):
            if left > 0 and base[k] < len(strata[k]):
                base[k] += 1
                left -= 1
                progressed = True
        if not progressed:
            break
    return base


def stratified_pick(games, n_test, n_dev, seed, era_bounds, cuts):
    strata = defaultdict(list)
    for g in games:
        g["era"] = era_for(g["year"], era_bounds)
        g["sa_band"] = band_for(g["sa_rate"], cuts)
        strata[f"{g['era']}/{g['sa_band']}"].append(g)

    rng = random.Random(seed)
    for k in strata:
        strata[k].sort(key=lambda g: (g["gameid"] or "", g["video_id"] or ""))
        rng.shuffle(strata[k])

    test_alloc = allocate(strata, n_test)
    test, remaining = [], defaultdict(list)
    for k, items in strata.items():
        take = test_alloc.get(k, 0)
        test.extend(items[:take])
        remaining[k] = items[take:]

    dev_alloc = allocate(remaining, n_dev)
    dev = []
    for k, items in remaining.items():
        dev.extend(items[: dev_alloc.get(k, 0)])

    return test, dev, strata


def ident(g):
    return g["gameid"] or g["video_id"]


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("annotations", help="directory of annotation export files")
    p.add_argument("--test", type=int, default=8)
    p.add_argument("--dev", type=int, default=4)
    p.add_argument("--version", default="2.0", help="guideline version to draw from")
    p.add_argument("--seed", type=int, default=20260923)
    p.add_argument("--reserve", help="JSON file listing swap-in-only reserve game ids")
    p.add_argument(
        "--reserve-count",
        type=int,
        default=5,
        help="how many games to auto-reserve when --reserve is not given (default 5)",
    )
    p.add_argument("--era-bounds", help='e.g. "2017:classic,2022:modern,9999:current"')
    p.add_argument("--out", default="data/splits/split-v2.0.json")
    p.add_argument("--write", action="store_true", help="actually write the split file")
    p.add_argument(
        "--allow-unknown-era",
        action="store_true",
        help="draw the split even when era is unknown for many games (NOT stratified by era)",
    )
    args = p.parse_args(argv)

    bounds = DEFAULT_ERA_BOUNDS
    if args.era_bounds:
        bounds = []
        for part in args.era_bounds.split(","):
            upper, name = part.split(":")
            bounds.append((int(upper), name))
        bounds.sort()

    games, skipped = load_games(args.annotations, args.version)

    reserve_ids = []
    if args.reserve:
        rp = Path(args.reserve)
        if not rp.is_file():
            print(f"error: reserve file not found: {rp}")
            return 2
        payload = json.loads(rp.read_text(encoding="utf-8"))
        reserve_ids = payload if isinstance(payload, list) else payload.get("reserve", [])

    # SA-rate bands are computed on the WHOLE annotated pool and then held
    # fixed, so removing the reserve cannot move the boundaries underneath the
    # draw.
    pool_cuts = tertile_bands([g["sa_rate"] for g in games])

    reserve_method = "explicit"
    if not reserve_ids and args.reserve_count > 0:
        auto = pick_reserve(games, pool_cuts, args.reserve_count)
        reserve_ids = [ident(g) for g in auto]
        reserve_method = f"nearest-SA-boundary (n={args.reserve_count})"

    eligible = [g for g in games if ident(g) not in set(reserve_ids)]
    held_back = [g for g in games if ident(g) in set(reserve_ids)]

    print(f"Annotated exports at guidelines v{args.version}: {len(games)}")
    if skipped:
        print(f"Skipped {len(skipped)} file(s):")
        for name, why in skipped[:10]:
            print(f"  - {name}: {why}")
    if reserve_ids:
        print(f"Reserve pool ({reserve_method}) — swap-in only, excluded from "
              f"both sets: {len(held_back)}/{len(reserve_ids)} found in the pool")
        for g in sorted(held_back, key=lambda g: ident(g)):
            print(f"    {ident(g)}  (SA {g['sa_rate']:.1%})")
    print(f"Eligible for selection: {len(eligible)}")
    print()

    need = args.test + args.dev
    if len(eligible) < need:
        print(f"CANNOT BUILD THE SPLIT: need {need} games "
              f"({args.test} test + {args.dev} dev), {len(eligible)} eligible.")
        print()
        print("Games currently available:")
        for g in sorted(eligible, key=lambda g: -g["sa_rate"]):
            print(f"  {ident(g)}")
            print(f"      {g['segments']} segments | SA {g['sa_rate']:.1%} "
                  f"| {g['year']} | patch {g['patch']}")
        print()
        print(f"Annotate {need - len(eligible)} more game(s) at v{args.version}, "
              f"then re-run. Nothing was written.")
        return 1

    # Era comes from matchInfo.datetime_utc. If that is missing for much of the
    # pool the draw is stratified by SA rate ONLY, which is not what was asked
    # for — refuse rather than produce a split that misdescribes itself.
    unknown = [g for g in eligible if era_for(g["year"], bounds) == "unknown"]
    if unknown and not args.allow_unknown_era:
        share = len(unknown) / len(eligible)
        if share > 0.2:
            print(f"CANNOT STRATIFY BY ERA: {len(unknown)}/{len(eligible)} games "
                  f"({share:.0%}) have no `datetime_utc`, so their era is unknown.")
            print()
            print("Backfill the match metadata first — the same gap the validator")
            print("reports as `match-fields`. Games missing it:")
            for g in sorted(unknown, key=lambda g: ident(g))[:30]:
                print(f"  {ident(g)}")
            print()
            print("Re-run once metadata is present, or pass --allow-unknown-era to")
            print("draw a split stratified by SA rate alone. Nothing was written.")
            return 1

    test, dev, strata = stratified_pick(
        eligible, args.test, args.dev, args.seed, bounds, pool_cuts
    )
    cuts = pool_cuts

    print("SA-rate tertile cuts: " + (
        f"{cuts[0]:.1%} / {cuts[1]:.1%}" if cuts else "n/a (too few games)"))
    print("Strata (era/sa-band): " +
          ", ".join(f"{k}={len(v)}" for k, v in sorted(strata.items())))
    print()
    for name, chunk in (("TEST", test), ("DEV", dev)):
        print(f"{name} ({len(chunk)})")
        for g in sorted(chunk, key=lambda g: (g["era"], -g["sa_rate"])):
            print(f"  {ident(g)}")
            print(f"      {g['era']:8} {g['sa_band']:8} SA {g['sa_rate']:.1%} "
                  f"| {g['segments']} segments | {g['year']}")
        print()

    payload = {
        "test": sorted(ident(g) for g in test),
        "dev": sorted(ident(g) for g in dev),
        "reserve": sorted(reserve_ids),
        "_provenance": {
            "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "guideline_version": args.version,
            "seed": args.seed,
            "n_test": args.test,
            "n_dev": args.dev,
            "era_bounds": [[u, n] for u, n in bounds],
            "sa_tertile_cuts": list(cuts) if cuts else None,
            "eligible_pool": sorted(ident(g) for g in eligible),
            "reserve_is_swap_in_only": True,
            "reserve_selection": reserve_method,
        },
    }

    if not args.write:
        print("Dry run — pass --write to save. Would write:", args.out)
        return 0

    out = Path(args.out)
    if out.exists():
        print(f"refusing to overwrite an existing split: {out}")
        print("A split that has been used is part of the record — "
              "delete it deliberately or pass a new --out.")
        return 2
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
