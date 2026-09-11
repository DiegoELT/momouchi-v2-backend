#!/usr/bin/env python3
"""
Validate annotation export files against the guidelines and the tool's schema.

Run this on every finished annotation file before it goes into analysis. It is
self-contained — it reads only the export itself, no corpus or network — so an
annotator can run it on their own machine.

Usage:
    python validate_annotations.py FILE [FILE ...]
    python validate_annotations.py annotations/          # a directory of exports
    python validate_annotations.py annotations/ --json   # machine-readable
    python validate_annotations.py annotations/ -q       # only files with findings

Exit code is 0 when no ERRORs were found, 1 otherwise (WARNs do not fail).
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

# Keep in sync with frontend/src/constants/annotation.js
LABELS = {
    "Play-by-Play",
    "Strategic Analysis",
    "Banter",
    "Storytelling",
    "Hype",
    "Recap",
}

FLAGS = {"UNCERTAIN", "ASR-ERROR", "NON-CONTENT", "MULTI-FUNCTION"}

# Required Leaguepedia metadata.
REQUIRED_MATCH_FIELDS = ("patch", "datetime_utc", "gamelength")

# Trim window vs recorded game length. Observed range on correctly matched games
# is 0.97-1.06; outside this band usually means the metadata belongs to a
# different game of the series.
RATIO_MIN, RATIO_MAX = 0.92, 1.15

MAX_EXAMPLES = 5


class Report:
    def __init__(self, path):
        self.path = path
        self.findings = []   # (severity, code, message, examples)
        self.info = {}

    def add(self, severity, code, message, examples=None):
        self.findings.append((severity, code, message, examples or []))

    def error(self, code, message, examples=None):
        self.add("ERROR", code, message, examples)

    def warn(self, code, message, examples=None):
        self.add("WARN", code, message, examples)

    @property
    def errors(self):
        return [f for f in self.findings if f[0] == "ERROR"]

    @property
    def warnings(self):
        return [f for f in self.findings if f[0] == "WARN"]


def fmt_hms(seconds):
    if seconds is None:
        return "—"
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


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


def describe(caption, index):
    text = (caption.get("text") or "").strip()
    if len(text) > 48:
        text = text[:45] + "..."
    return f"#{index} @{fmt_hms(caption.get('start'))} {text!r}"


def validate(path):
    rep = Report(path)

    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as e:
        rep.error("unreadable", f"could not parse JSON: {e}")
        return rep

    if not isinstance(data, dict):
        rep.error("unreadable", "top level is not an object")
        return rep

    captions = data.get("captions")
    if not isinstance(captions, list):
        rep.error("no-captions", "no captions array")
        return rep

    events = data.get("events") or []
    rep.info = {
        "captions": len(captions),
        "events": len(events),
        "guideline_version": data.get("guideline_version"),
        "video_id": data.get("video_id"),
    }

    # --- top-level guideline version -------------------------------------
    top_version = data.get("guideline_version")
    if not top_version:
        rep.error("top-version", "top-level `guideline_version` is missing")

    # --- trim present -----------------------------------------------------
    trim_window = None
    valid_trim = None  # only set once the bounds are known to be numeric
    if "trim" not in data:
        rep.error(
            "trim-missing",
            "no `trim` key — the file does not record which part of the VOD it covers",
        )
    else:
        trim = data["trim"]
        if trim is None:
            rep.warn("trim-null", "`trim` is null (captions recorded as untrimmed)")
        elif not isinstance(trim, dict):
            rep.error("trim-shape", f"`trim` is {type(trim).__name__}, expected an object")
        else:
            start, end = trim.get("start"), trim.get("end")
            bad = [k for k, v in (("start", start), ("end", end))
                   if v is not None and not isinstance(v, (int, float))]
            if bad:
                rep.error("trim-shape", f"`trim` has non-numeric {', '.join(bad)}")
            elif start is not None and end is not None:
                if end <= start:
                    rep.error("trim-order", f"trim end ({end}) is not after start ({start})")
                else:
                    trim_window = end - start
                    valid_trim = trim
                    rep.info["trim"] = f"{fmt_hms(start)} → {fmt_hms(end)}"
            elif not bad:
                # one bound open, the other numeric — still usable for bounds checks
                valid_trim = trim

    # --- match metadata ---------------------------------------------------
    match = data.get("matchInfo")
    if isinstance(match, list):
        match = match[0] if match else None

    if not isinstance(match, dict):
        rep.error("match-missing", "no matchInfo — patch/datetime_utc/gamelength unavailable")
        match = {}
    else:
        missing = [f for f in REQUIRED_MATCH_FIELDS
                   if match.get(f) in (None, "", [])]
        if missing:
            rep.error(
                "match-fields",
                f"matchInfo is missing {', '.join(missing)}",
            )
        rep.info["gameid"] = match.get("gameid")
        rep.info["patch"] = match.get("patch")

    # --- trim plausibility against gamelength -----------------------------
    game_seconds = parse_gamelength(match.get("gamelength"))
    if trim_window and game_seconds:
        ratio = trim_window / game_seconds
        rep.info["length_ratio"] = round(ratio, 3)
        if not (RATIO_MIN <= ratio <= RATIO_MAX):
            rep.warn(
                "trim-implausible",
                f"trim window {fmt_hms(trim_window)} vs gamelength "
                f"{fmt_hms(game_seconds)} (ratio {ratio:.2f}) — the metadata may "
                f"belong to a different game of the series, or the trim is off",
            )

    # --- per-caption checks ------------------------------------------------
    missing_label, bad_label = [], []
    bad_flag, multi_flag = [], []
    missing_version = []
    outside_trim = []
    versions = Counter()
    id_counts = Counter()
    missing_id = []

    trim_obj = valid_trim

    for i, cap in enumerate(captions):
        if not isinstance(cap, dict):
            bad_label.append(f"#{i} is not an object")
            continue

        # label in the six-set
        label = cap.get("label")
        if label in (None, "", "None"):
            missing_label.append(describe(cap, i))
        elif label not in LABELS:
            bad_label.append(f"{describe(cap, i)} → {label!r}")

        # flag in the four-set, at most one
        flag = cap.get("flag", None)
        if isinstance(flag, (list, tuple, set)):
            if len(flag) > 1:
                multi_flag.append(f"{describe(cap, i)} → {list(flag)!r}")
            elif len(flag) == 1 and list(flag)[0] not in FLAGS:
                bad_flag.append(f"{describe(cap, i)} → {list(flag)[0]!r}")
            elif len(flag) <= 1:
                multi_flag.append(f"{describe(cap, i)} → flag stored as a list")
        elif flag not in (None, "") and flag not in FLAGS:
            bad_flag.append(f"{describe(cap, i)} → {flag!r}")

        # per-caption guideline version, on anything actually annotated
        version = cap.get("guideline_version")
        annotated = label not in (None, "", "None") or flag not in (None, "")
        if annotated and not version:
            missing_version.append(describe(cap, i))
        if version:
            versions[version] += 1

        # caption id collisions
        cid = cap.get("id")
        if cid in (None, ""):
            missing_id.append(describe(cap, i))
        else:
            id_counts[cid] += 1

        # captions must lie inside the recorded window
        if trim_obj:
            lo = trim_obj.get("start")
            hi = trim_obj.get("end")
            start = cap.get("start")
            dur = cap.get("duration") or 0
            if not isinstance(dur, (int, float)):
                dur = 0
            if isinstance(start, (int, float)):
                if (lo is not None and start + dur <= lo) or (
                    hi is not None and start >= hi
                ):
                    outside_trim.append(describe(cap, i))

    if missing_label:
        rep.error("label-missing", f"{len(missing_label)} caption(s) have no label", missing_label)
    if bad_label:
        rep.error("label-invalid", f"{len(bad_label)} caption(s) have a label outside the six-set", bad_label)
    if bad_flag:
        rep.error("flag-invalid", f"{len(bad_flag)} caption(s) have a flag outside the four-set", bad_flag)
    if multi_flag:
        rep.error("flag-multiple", f"{len(multi_flag)} caption(s) do not carry exactly one scalar flag", multi_flag)
    if missing_version:
        rep.error("caption-version", f"{len(missing_version)} annotated caption(s) have no `guideline_version`", missing_version)
    if missing_id:
        rep.error("id-missing", f"{len(missing_id)} caption(s) have no `id`", missing_id)

    dupes = [(cid, n) for cid, n in id_counts.items() if n > 1]
    if dupes:
        rep.error(
            "id-collision",
            f"{len(dupes)} caption id(s) used more than once",
            [f"{cid} ×{n}" for cid, n in dupes],
        )

    if outside_trim:
        rep.error(
            "caption-outside-trim",
            f"{len(outside_trim)} caption(s) fall entirely outside the recorded trim",
            outside_trim,
        )

    if versions:
        rep.info["caption_versions"] = dict(versions)
        if top_version and top_version not in versions:
            rep.warn(
                "version-mismatch",
                f"top-level version is {top_version!r} but no caption carries it "
                f"(captions: {', '.join(sorted(versions))})",
            )

    return rep


def collect_paths(args):
    paths = []
    for raw in args:
        p = Path(raw)
        if p.is_dir():
            paths.extend(sorted(p.glob("*.json")))
        else:
            paths.append(p)
    return paths


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", help="annotation export files or a directory")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument("-q", "--quiet", action="store_true", help="only show files with findings")
    parser.add_argument("--max-examples", type=int, default=MAX_EXAMPLES)
    args = parser.parse_args(argv)

    paths = collect_paths(args.paths)
    if not paths:
        print("No files to validate.")
        return 1

    reports = [validate(p) for p in paths]

    if args.json:
        json.dump(
            {
                "files": [
                    {
                        "path": str(r.path),
                        "info": r.info,
                        "errors": [
                            {"code": c, "message": m, "examples": e}
                            for s, c, m, e in r.errors
                        ],
                        "warnings": [
                            {"code": c, "message": m, "examples": e}
                            for s, c, m, e in r.warnings
                        ],
                    }
                    for r in reports
                ],
                "files_checked": len(reports),
                "files_with_errors": sum(1 for r in reports if r.errors),
            },
            sys.stdout,
            indent=2,
        )
        print()
        return 1 if any(r.errors for r in reports) else 0

    for rep in reports:
        if args.quiet and not rep.findings:
            continue

        mark = "✗" if rep.errors else ("!" if rep.warnings else "✓")
        print(f"{mark} {Path(rep.path).name}")

        i = rep.info
        if i.get("captions") is not None:
            bits = [f"{i['captions']} captions", f"{i.get('events', 0)} events"]
            if i.get("guideline_version"):
                bits.append(f"guidelines v{i['guideline_version']}")
            if i.get("trim"):
                bits.append(f"trim {i['trim']}")
            if i.get("patch"):
                bits.append(f"patch {i['patch']}")
            print(f"    {' | '.join(bits)}")
            if i.get("caption_versions"):
                spread = ", ".join(
                    f"v{v}: {n}" for v, n in sorted(i["caption_versions"].items())
                )
                print(f"    stamped {spread}")

        for severity, code, message, examples in rep.findings:
            print(f"    {severity:5} [{code}] {message}")
            for ex in examples[: args.max_examples]:
                print(f"            {ex}")
            if len(examples) > args.max_examples:
                print(f"            ... and {len(examples) - args.max_examples} more")
        print()

    n_err = sum(1 for r in reports if r.errors)
    n_warn = sum(1 for r in reports if r.warnings and not r.errors)
    clean = len(reports) - n_err - n_warn
    print(
        f"{len(reports)} file(s): {clean} clean, {n_warn} with warnings, "
        f"{n_err} with errors."
    )
    return 1 if n_err else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        # piping into head/less closes the stream early; not an error
        try:
            sys.stdout.close()
        except Exception:
            pass
        sys.exit(0)
    except KeyboardInterrupt:
        sys.exit(130)
