"""
Momouchi-v2 annotation backend (serving layer).

By default this process makes NO outbound calls to YouTube or Leaguepedia. It
serves captions and match metadata from the corpus snapshot in data/corpus,
which is produced locally by ingest.py.

That is deliberate: YouTube blocks datacenter IPs, so any cloud-hosted service
that fetches captions at request time will break. Serving a snapshot also keeps
the annotation unit stable across the annotation period.

Set ALLOW_LIVE_FETCH=1 for local development if you want the old behaviour of
falling back to the live APIs when a game has not been ingested yet.
"""

import os

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import corpus
from sources import SourceUnavailable, extract_youtube_id


def _flag(name: str, default: str) -> bool:
    return os.getenv(name, default).lower() in ("1", "true", "yes")


ALLOW_LIVE_FETCH = _flag("ALLOW_LIVE_FETCH", "0")

# Ingestion WRITES to the corpus and fetches from YouTube, so it only works on a
# machine with a residential IP and a persistent disk — i.e. your laptop.
# Defaults to whatever ALLOW_LIVE_FETCH is, so the local dev flag turns on the
# whole local workflow. MUST stay off on the hosted deployment.
ALLOW_INGEST = _flag("ALLOW_INGEST", "1" if ALLOW_LIVE_FETCH else "0")


class IngestRequest(BaseModel):
    video_url: str
    start: float | None = None
    end: float | None = None
    refresh: bool = False


class TrimRequest(BaseModel):
    start: float | None = None
    end: float | None = None

# Comma-separated list; defaults to open for backwards compatibility.
ALLOWED_ORIGINS = [
    o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",") if o.strip()
]

app = FastAPI(title="Momouchi-v2 backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _error(message: str, status: int = 404, **extra):
    return JSONResponse({"error": message, **extra}, status_code=status)


def _not_ingested(video_id: str):
    return _error(
        f"Game {video_id} is not in the corpus. Run "
        f"`python ingest.py <video_url>` locally, commit data/corpus, and "
        f"redeploy.",
        status=404,
        video_id=video_id,
        ingested=False,
    )


@app.get("/health")
def health():
    """Cheap liveness probe that touches nothing external."""
    return {
        "status": "ok",
        "games_in_corpus": len(corpus.entries()),
        "live_fetch_enabled": ALLOW_LIVE_FETCH,
        "ingest_enabled": ALLOW_INGEST,
    }


@app.get("/corpus/")
def list_corpus():
    """Games available to annotate, for the frontend picker."""
    return {"games": corpus.entries()}


@app.get("/captions/")
def get_captions(video_url: str = Query(...)):
    video_id = extract_youtube_id(video_url)
    if not video_id:
        return _error("Invalid YouTube URL.", status=400)

    entry = corpus.load(video_id)
    if entry:
        captions = entry.get("captions", [])
        trim = entry.get("trim")
        return {
            "video_id": video_id,
            "captions": corpus.apply_trim(captions, trim),
            "trim": trim,
            "caption_count": len(captions),
            "source": "corpus",
            "fetched_at": entry.get("fetched_at"),
        }

    if not ALLOW_LIVE_FETCH:
        return _not_ingested(video_id)

    try:
        from sources import fetch_captions

        # Preview only — nothing is written until "Add to corpus".
        return {
            "video_id": video_id,
            "captions": fetch_captions(video_id),
            "trim": None,
            "source": "live",
        }
    except SourceUnavailable as e:
        return _error(str(e), status=503)
    except Exception as e:
        return _error(f"Caption fetch failed: {e}", status=502)


@app.get("/match_details/")
def match_details(video_url: str = Query(...)):
    video_id = extract_youtube_id(video_url)
    if not video_id:
        return _error("Could not extract YouTube video id from URL.", status=400)

    entry = corpus.load(video_id)
    if entry:
        return {"matches": entry.get("match"), "source": "corpus"}

    if not ALLOW_LIVE_FETCH:
        return _not_ingested(video_id)

    try:
        from sources import fetch_match_details

        return {"matches": fetch_match_details(video_id), "source": "live"}
    except SourceUnavailable as e:
        return _error(str(e), status=503)
    except Exception as e:
        return _error(f"Match lookup failed: {e}", status=502)


def _ingest_disabled():
    return _error(
        "Ingestion is disabled on this deployment. Corpus snapshots are built "
        "locally (ALLOW_INGEST=1) and committed to git.",
        status=403,
    )


@app.post("/corpus/")
def add_to_corpus(req: IngestRequest):
    """
    Fetch a VOD's captions and metadata and write a corpus snapshot.

    Local-only: YouTube blocks datacenter IPs, and a hosted filesystem is
    ephemeral anyway. Captions are stored in FULL; the trim is recorded
    alongside them so it can be redone later without re-fetching.
    """
    if not ALLOW_INGEST:
        return _ingest_disabled()

    video_id = extract_youtube_id(req.video_url)
    if not video_id:
        return _error("Invalid YouTube URL.", status=400)

    try:
        trim = corpus.normalize_trim(req.start, req.end)
    except ValueError as e:
        return _error(str(e), status=400)

    if corpus.exists(video_id) and not req.refresh:
        return _error(
            f"{video_id} is already in the corpus. Adjust its bounds with "
            f"/corpus/{video_id}/trim instead of re-fetching — re-fetching can "
            f"return different captions and would break existing annotations.",
            status=409,
            video_id=video_id,
        )

    try:
        from sources import fetch_captions, fetch_match_details

        captions = fetch_captions(video_id)
    except SourceUnavailable as e:
        return _error(str(e), status=503)
    except Exception as e:
        return _error(f"Caption fetch failed: {e}", status=502)

    if not captions:
        return _error("YouTube returned no captions for this VOD.", status=502)

    # Missing match metadata must not block ingestion — patch and teams can be
    # filled in by hand in the UI.
    match = None
    match_error = None
    try:
        match = fetch_match_details(video_id)
        if match is None:
            match_error = "No Leaguepedia game matched this VOD."
    except Exception as e:
        match_error = f"Leaguepedia lookup failed: {e}"

    corpus.save(video_id, req.video_url, captions, match, trim)
    entry = corpus.load(video_id)

    return {
        "video_id": video_id,
        "saved": True,
        "trim": trim,
        "caption_count": entry["caption_count"],
        "trimmed_count": entry["trimmed_count"],
        "match_warning": match_error,
    }


@app.post("/corpus/{video_id}/trim")
def retrim(video_id: str, req: TrimRequest):
    """
    Recompute the trim from the stored full captions. Never touches YouTube, so
    the caption text is guaranteed identical to what was ingested.
    """
    if not ALLOW_INGEST:
        return _ingest_disabled()

    try:
        trim = corpus.normalize_trim(req.start, req.end)
    except ValueError as e:
        return _error(str(e), status=400)

    if not corpus.exists(video_id):
        return _error(f"{video_id} is not in the corpus.", status=404)

    entry = corpus.set_trim(video_id, trim)
    return {
        "video_id": video_id,
        "trim": trim,
        "caption_count": entry["caption_count"],
        "trimmed_count": entry["trimmed_count"],
    }


@app.get("/leaguepedia/latest_games/")
def latest_games():
    """
    Discovery helper for finding VODs to ingest. Live-only by nature, so it is
    disabled on the hosted deployment.
    """
    if not ALLOW_LIVE_FETCH:
        return _error(
            "Live Leaguepedia search is disabled on this deployment. Run the "
            "backend locally with ALLOW_LIVE_FETCH=1 to search for VODs.",
            status=503,
        )

    try:
        from sources import leaguepedia_query

        results = leaguepedia_query(
            tables="Tournaments, ScoreboardGames",
            join_on="Tournaments.OverviewPage = ScoreboardGames.OverviewPage",
            fields="Tournaments.OverviewPage, Name, Team1, Team2, VOD, IsOfficial",
            where=(
                "VOD IS NOT NULL AND Tournaments.OverviewPage != "
                "'2025 Season World Championship/Main Event' AND "
                "IsOfficial='1' AND VOD NOT LIKE \"%live%\""
            ),
            order_by="DateTime_UTC DESC",
            limit=10,
            retries=1,
        )
        return {"results": list(results)}
    except SourceUnavailable as e:
        return _error(str(e), status=503)
    except Exception as e:
        return _error(f"Leaguepedia query failed: {e}", status=502)
