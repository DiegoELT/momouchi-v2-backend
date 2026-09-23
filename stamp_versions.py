#!/usr/bin/env python3
"""
Stamp `guideline_version` onto annotated captions that are missing it.

The tool records the version only when a label or flag CHANGES, so captions
labelled before the field existed carry nothing, and re-saving in the app will
not add it — the change never fires. This backfills the field directly.

It touches exactly one key per caption. Labels, flags, text, ids, events and
match metadata are asserted unchanged before anything is written.

Only captions that are actually annotated (a label, or a flag) are stamped: an
unlabelled caption has no annotation to attribute to a guideline version.

Usage:
    python stamp_versions.py <dir-or-file> --version pre-1.0          # dry run
    python stamp_versions.py <dir-or-file> --version pre-1.0 --write
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path


def is_annotated(cap):
    label = cap.get("label")
    flag = cap.get("flag")
    return (label not in (None, "", "None")) or (flag not in (None, ""))


def fingerprint(captions):
    """Everything about a caption EXCEPT guideline_version."""
    return json.dumps(
        [
            {k: v for k, v in c.items() if k != "guideline_version"}
            for c in captions
        ],
        sort_keys=True,
    )


def process(path, version, write):
    data = json.loads(path.read_text(encoding="utf-8"))
    captions = data.get("captions") or []
    if not captions:
        return None

    before = fingerprint(captions)
    before_events = json.dumps(data.get("events") or [], sort_keys=True)

    existing = Counter(
        c.get("guideline_version") for c in captions if c.get("guideline_version")
    )
    to_stamp = [
        c for c in captions if is_annotated(c) and not c.get("guideline_version")
    ]
    unlabelled_no_version = sum(
        1 for c in captions if not is_annotated(c) and not c.get("guideline_version")
    )

    if not to_stamp:
        return {"path": path, "stamped": 0, "existing": existing,
                "skipped_unlabelled": unlabelled_no_version}

    for c in to_stamp:
        c["guideline_version"] = version

    # Only the version key may have changed.
    assert fingerprint(captions) == before, f"{path.name}: caption content changed"
    assert json.dumps(data.get("events") or [], sort_keys=True) == before_events, \
        f"{path.name}: events changed"

    if write:
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                       encoding="utf-8")
        tmp.replace(path)

    return {"path": path, "stamped": len(to_stamp), "existing": existing,
            "skipped_unlabelled": unlabelled_no_version}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("target", help="annotation export file, or a directory of them")
    p.add_argument("--version", required=True, help='e.g. "pre-1.0"')
    p.add_argument("--write", action="store_true")
    args = p.parse_args(argv)

    target = Path(args.target)
    paths = sorted(target.glob("*.json")) if target.is_dir() else [target]

    total = 0
    for path in paths:
        try:
            result = process(path, args.version, args.write)
        except Exception as e:
            print(f"! {path.name}: {e}")
            continue
        if not result or not result["stamped"]:
            continue
        total += result["stamped"]
        existing = ", ".join(f"v{k}: {v}" for k, v in sorted(result["existing"].items()))
        print(f"{path.name}")
        print(f"    stamping {result['stamped']} annotated caption(s) as v{args.version}")
        if existing:
            print(f"    leaving alone: {existing}")
        if result["skipped_unlabelled"]:
            print(f"    skipped {result['skipped_unlabelled']} unlabelled caption(s)")

    print()
    if args.write:
        print(f"Stamped {total} caption(s).")
    else:
        print(f"Dry run — would stamp {total} caption(s). Pass --write to apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
