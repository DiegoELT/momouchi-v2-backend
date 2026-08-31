# Momouchi-v2 backend

Two pieces that deliberately do **not** run in the same place.

| | `ingest.py` | `main.py` |
|---|---|---|
| Runs on | your laptop | the server |
| Talks to | YouTube + Leaguepedia | nothing external |
| How often | once per VOD | every request |

## Why the split

YouTube blocks datacenter IP ranges. Any cloud-hosted service that calls
`youtube-transcript-api` at request time will fail with a blocked-request error
— this is not fixable with retries or a different provider, because AWS, GCP,
Azure, Render, Fly and Railway are all on the same blocklist.

So captions are fetched **once**, from a residential connection, and committed
as a snapshot in `data/corpus/`. The server just reads those files.

This also closes a methodological hole: the annotation guidelines define the
unit of annotation as the YouTube caption block *as delivered by the tool*.
YouTube regenerates ASR captions when its models improve, so without a frozen
snapshot the unit of annotation could change mid-annotation without anyone
noticing, and segments annotated months apart would not be commensurable.

## Trimming

VODs carry pick/ban at the start and post-game content at the end that is not
annotated material. A snapshot therefore records **trim bounds** alongside the
captions.

Captions are always stored in **full**; the trim is applied when serving. That
means changing the bounds later (`--retrim`, or "Update trim" in the UI) is
recomputed from the stored captions and **never** goes back to YouTube — so
fixing a bad trim cannot swap the caption text underneath annotations that
already exist. Re-fetching (`--refresh`) can, which is why it is a separate
flag with a warning on it.

Blocks that straddle a boundary are kept, matching how the frontend range
filter has always behaved.

## Ingesting from the UI (easiest)

Run the backend locally with `ALLOW_LIVE_FETCH=1` and the frontend gains an
**Add to corpus** button:

1. Paste the VOD URL, hit **Load** → captions come straight from YouTube,
   badged *Preview — not saved yet*. Nothing is written.
2. Scrub the player, set start/end, hit **Load** again to check the bounds.
3. Hit **Add to corpus** → the full captions and Leaguepedia metadata are
   fetched and saved with those bounds. The view reloads from the snapshot and
   the badge flips to *In corpus*.
4. Bounds wrong? Adjust them and hit **Update trim** — no re-fetch.

This calls the same functions as the CLI below, so the two are equivalent.

## Ingesting a game (CLI)

```bash
source venv/bin/activate
pip install -r requirements.txt
python ingest.py "https://www.youtube.com/watch?v=XXXXXXXXXXX"
python ingest.py "<url>" --start 4:35 --end 41:20   # with trim bounds
python ingest.py --from-file urls.txt               # bulk (no trim)
python ingest.py --list                             # what's already in
python ingest.py --retrim "<url>" --start 5:02      # change bounds, no re-fetch
python ingest.py --refresh "<url>"                  # re-fetch (avoid mid-annotation!)
```

Times accept seconds, `mm:ss` or `hh:mm:ss`.

Then **commit `data/corpus/`** — the snapshot is the corpus, it belongs in git.

If ingestion fails with a blocked-request error, you are not on a residential
connection: get off the VPN and off the university network and retry.

## Running the server

```bash
uvicorn main:app --reload                  # hosted mode: corpus only
ALLOW_LIVE_FETCH=1 uvicorn main:app --reload   # dev: fall back to live APIs
```

### Environment variables

| Variable | Default | What it does |
|---|---|---|
| `ALLOW_LIVE_FETCH` | `0` | `1` lets the server call YouTube/Leaguepedia when a game is not in the corpus (the preview path). **Leave off in production.** |
| `ALLOW_INGEST` | follows `ALLOW_LIVE_FETCH` | `1` enables the corpus write endpoints behind the "Add to corpus" / "Update trim" buttons. **Leave off in production** — the hosted disk is ephemeral, so writes there would be lost on redeploy. |
| `ALLOWED_ORIGINS` | `*` | Comma-separated CORS origins. Set to your frontend URL once deployed. |
| `CORPUS_DIR` | `./data/corpus` | Where snapshots live. |
| `LEAGUEPEDIA_USERNAME` / `_PASSWORD` | — | Ingestion only. Not needed by the server. |
| `LEAGUEPEDIA_TIMEOUT` | `20` | Seconds before a Cargo request is abandoned. |
| `RELOGIN_COOLDOWN_SECONDS` | `30` | Minimum gap between re-logins, so failures cannot get the account throttled. |

## Deploying

Any host works, since nothing outbound is required:

```
Build:  pip install -r requirements.txt
Start:  uvicorn main:app --host 0.0.0.0 --port $PORT
```

Set `ALLOWED_ORIGINS` to the frontend URL. Leave `ALLOW_LIVE_FETCH` and
`ALLOW_INGEST` unset — the hosted instance must be read-only.
`GET /health` is a cheap liveness probe that touches nothing external — point
the platform's health check at it.

The ingestion-only dependencies (`mwrogue`, `youtube-transcript-api`,
`python-dotenv`) are not imported unless `ALLOW_LIVE_FETCH=1`, so they can be
stripped from a deployment build if you want a smaller image.

## Endpoints

| Endpoint | Notes |
|---|---|
| `GET /health` | liveness + how many games are loaded |
| `GET /corpus/` | list of ingested games (drives the sidebar picker) |
| `GET /captions/?video_url=` | 404 with a clear message if not ingested |
| `GET /match_details/?video_url=` | ditto |
| `POST /corpus/` | ingest a VOD `{video_url, start, end, refresh}`; 403 unless `ALLOW_INGEST=1` |
| `POST /corpus/{video_id}/trim` | change bounds `{start, end}`, no re-fetch; 403 unless `ALLOW_INGEST=1` |
| `GET /leaguepedia/latest_games/` | VOD discovery; 503 unless `ALLOW_LIVE_FETCH=1` |
