#!/usr/bin/env python3
"""
Freeze gate for the annotated corpus directory.

`validate_annotations.py` checks one file at a time. This checks the DIRECTORY
as a unit — the things that are only wrong in aggregate: stray files, duplicate
games, a mixed guideline version, a pool too thin to draw a split from.

Exit 0 means the corpus is safe to freeze and tag. Anything else, don't.

Usage:
    python freeze_check.py <corpus-dir>
    python freeze_check.py <corpus-dir> --exceptions exceptions.json
    python freeze_check.py <corpus-dir> --test 8 --dev 4 --reserve 5

An exceptions file records facts that cannot be fixed, so the gate is not
blocked forever by them, and so they stay visible instead of being quietly
ignored:

    { "IEM Season VII ... .json": ["match-fields"] }
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

import validate_annotations as va

ERA_BOUNDS = [(2017, "classic"), (2022, "modern"), (9999, "current")]
ALLOWED_SUFFIXES = {".json"}


def era_of(year):
    if year is None:
        return "unknown"
    for upper, name in ERA_BOUNDS:
        if year <= upper:
            return name
    return "current"


def year_of(match):
    try:
        return int(str((match or {}).get("datetime_utc"))[:4])
    except (TypeError, ValueError):
        return None


class Gate:
    def __init__(self):
        self.failures = []
        self.notes = []
        self.accepted = []

    def fail(self, check, detail):
        self.failures.append((check, detail))

    def note(self, check, detail):
        self.notes.append((check, detail))

    def accept(self, check, detail):
        self.accepted.append((check, detail))


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("corpus", help="directory of annotation exports")
    p.add_argument("--exceptions", help="JSON map of filename -> accepted error codes")
    p.add_argument("--version", default="2.0", help="required guideline version")
    p.add_argument("--test", type=int, default=8)
    p.add_argument("--dev", type=int, default=4)
    p.add_argument("--reserve", type=int, default=5)
    args = p.parse_args(argv)

    root = Path(args.corpus)
    if not root.is_dir():
        print(f"not a directory: {root}")
        return 2

    exceptions = {}
    if args.exceptions:
        exceptions = json.loads(Path(args.exceptions).read_text(encoding="utf-8"))

    gate = Gate()
    print(f"Freeze check: {root}\n")

    # ---- 1. nothing in the directory but annotation files -----------------
    strays = []
    for entry in sorted(root.iterdir()):
        if entry.name.startswith("."):
            strays.append(f"{entry.name} (hidden)")
        elif entry.is_dir():
            strays.append(f"{entry.name}/ (directory)")
        elif entry.suffix not in ALLOWED_SUFFIXES:
            strays.append(entry.name)
    if strays:
        gate.fail("stray files",
                  f"{len(strays)} entry/entries that are not annotation exports: "
                  + ", ".join(strays[:8]))

    paths = sorted(root.glob("*.json"))
    if not paths:
        gate.fail("empty", "no annotation exports found")
        return report(gate, {})

    # ---- 2. every file passes the per-file validator -----------------------
    per_file_errors = 0
    for path in paths:
        rep = va.validate(path)
        allowed = set(exceptions.get(path.name, []))
        for _sev, code, message, _ex in rep.errors:
            if code in allowed:
                gate.accept(path.name, f"{code} — {message} (declared exception)")
            else:
                gate.fail(path.name, f"{code}: {message}")
                per_file_errors += 1
        for _sev, code, message, _ex in rep.warnings:
            gate.note(path.name, f"{code}: {message}")

    # ---- 3. corpus-level invariants ---------------------------------------
    games, versions, vids, gids = [], Counter(), Counter(), Counter()
    for path in paths:
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue  # already reported by the validator
        m = d.get("matchInfo")
        m = (m[0] if isinstance(m, list) and m else m) or {}
        caps = d.get("captions") or []
        nc = sum(1 for c in caps
                 if c.get("label") in (None, "", "None") and c.get("flag") == "NON-CONTENT")
        sa = sum(1 for c in caps if c.get("label") == "Strategic Analysis")
        labelled = len(caps) - nc
        versions[str(d.get("guideline_version"))] += 1
        if d.get("video_id"):
            vids[d["video_id"]] += 1
        if m.get("gameid"):
            gids[m["gameid"]] += 1
        games.append({
            "file": path.name, "era": era_of(year_of(m)),
            "labelled": labelled, "non_content": nc,
            "sa_rate": (sa / labelled) if labelled else 0.0,
        })

    if len(versions) > 1:
        gate.fail("mixed guideline versions",
                  ", ".join(f"v{k}: {n}" for k, n in sorted(versions.items())))
    elif args.version and args.version not in versions:
        gate.fail("guideline version",
                  f"corpus is at v{list(versions)[0]}, expected v{args.version}")

    for label, counter in (("video_id", vids), ("gameid", gids)):
        dupes = [k for k, n in counter.items() if n > 1]
        if dupes:
            gate.fail(f"duplicate {label}", ", ".join(dupes[:5]))

    unknown_era = [g["file"] for g in games if g["era"] == "unknown"]
    if unknown_era:
        share = len(unknown_era) / len(games)
        detail = f"{len(unknown_era)}/{len(games)} games have no datetime_utc"
        (gate.fail if share > 0.2 else gate.note)("era unknown", detail)

    # ---- 4. is the pool actually big enough to draw a split? --------------
    need = args.test + args.dev + args.reserve
    if len(games) < need:
        gate.fail("pool too small",
                  f"{len(games)} games; need {need} "
                  f"({args.test} test + {args.dev} dev + {args.reserve} reserve)")
    else:
        eras = Counter(g["era"] for g in games)
        thin = [e for e, n in eras.items() if e != "unknown" and n < 3]
        if thin:
            gate.note("thin strata",
                      "fewer than 3 games in: " + ", ".join(sorted(thin)))

    return report(gate, games)


def report(gate, games):
    if games:
        eras = Counter(g["era"] for g in games)
        total = sum(g["labelled"] for g in games)
        nc = sum(g["non_content"] for g in games)
        rates = [g["sa_rate"] for g in games]
        print("Corpus")
        print(f"  Games .................... {len(games)}")
        print(f"  Labelled segments ........ {total:,}")
        print(f"  NON-CONTENT segments ..... {nc:,} (unlabelled by design)")
        print(f"  Segments per game ........ "
              f"min {min(g['labelled'] for g in games)}, "
              f"max {max(g['labelled'] for g in games)}, "
              f"mean {total // len(games)}")
        print(f"  SA rate .................. {min(rates):.1%} – {max(rates):.1%}")
        print("  Era mix .................. "
              + ", ".join(f"{e} {n}" for e, n in sorted(eras.items())))
        print()

    if gate.accepted:
        print("Accepted exceptions")
        for check, detail in gate.accepted:
            print(f"  · {check}: {detail}")
        print()

    if gate.notes:
        print("Notes (do not block the freeze)")
        for check, detail in gate.notes:
            print(f"  ! {check}: {detail}")
        print()

    if gate.failures:
        print("BLOCKING")
        for check, detail in gate.failures:
            print(f"  ✗ {check}: {detail}")
        print()
        print(f"NOT READY TO FREEZE — {len(gate.failures)} blocking issue(s).")
        return 1

    print("READY TO FREEZE.")
    print("  1. git add -A && git commit   (thesis repo, the corpus)")
    print("  2. python make_split.py <corpus> --write && commit the split")
    print("  3. git tag corpus-v1.0        (momouchi-v2/backend)")
    print("  4. git tag guidelines-v2.0    (msc-thesis-esports-discourse)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
