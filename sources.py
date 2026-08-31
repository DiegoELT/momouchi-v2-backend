"""
Live data sources (YouTube captions + Leaguepedia).

IMPORTANT: this module is for INGESTION, which is meant to run on a machine with
a residential IP. YouTube blocks datacenter IP ranges, so
calling fetch_captions() from a cloud host will fail with a blocked-request
error. The hosted backend serves from the corpus snapshot instead and never
imports the live path unless ALLOW_LIVE_FETCH is explicitly enabled.
"""

import os
import threading
import time
from urllib.parse import urlparse, parse_qs

# How long any single Leaguepedia HTTP request may take. Without this,
# requests/mwclient wait forever, sync endpoints pile up in FastAPI's threadpool
# (40 slots) and the whole app stops responding until it is restarted.
LEAGUEPEDIA_TIMEOUT = float(os.getenv("LEAGUEPEDIA_TIMEOUT", "20"))

# Minimum gap between re-logins. Re-authenticating on every failure gets the
# account throttled by MediaWiki, which looks exactly like "the backend died".
RELOGIN_COOLDOWN_SECONDS = float(os.getenv("RELOGIN_COOLDOWN_SECONDS", "30"))

_site_lock = threading.Lock()
_site = None
_last_login_attempt = 0.0


class SourceUnavailable(RuntimeError):
    """Raised when an upstream source cannot be reached or is blocking us."""


# --------------------------------------------------------------------------
# YouTube
# --------------------------------------------------------------------------

def extract_youtube_id(video_url: str) -> str | None:
    """Supports regular youtube URLs and youtu.be short links."""
    try:
        parsed = urlparse(video_url)
        if parsed.hostname and "youtu" in parsed.hostname:
            if parsed.hostname == "youtu.be":
                return parsed.path.strip("/") or None
            return parse_qs(parsed.query).get("v", [None])[0]
    except Exception:
        return None
    return None


def fetch_captions(video_id: str) -> list[dict]:
    """
    Fetch captions from YouTube. Only usable from a residential IP.
    """
    from youtube_transcript_api import YouTubeTranscriptApi

    try:
        transcript = YouTubeTranscriptApi().fetch(video_id)
    except Exception as e:
        name = type(e).__name__
        if any(k in name for k in ("Blocked", "IpBlocked", "TooManyRequests")):
            raise SourceUnavailable(
                f"YouTube is blocking this IP ({name}). Caption fetching only "
                f"works from a residential connection — run ingest.py locally."
            ) from e
        raise SourceUnavailable(f"YouTube caption fetch failed ({name}): {e}") from e

    return transcript.to_raw_data()


# --------------------------------------------------------------------------
# Leaguepedia
# --------------------------------------------------------------------------

# Cargo returns some fields with spaces in the key (e.g. "DateTime UTC" for the
# DateTime_UTC column). Normalise everything to lowercase snake_case so the
# frontend can rely on stable key names.
_KEY_ALIASES = {
    "datetime utc": "datetime_utc",
    "gamelength number": "gamelength_number",
}


def normalize_keys(row: dict) -> dict:
    normalized = {}
    for key, value in row.items():
        lowered = key.lower()
        normalized[_KEY_ALIASES.get(lowered, lowered.replace(" ", "_"))] = value
    return normalized


def _install_timeout(client, timeout: float) -> bool:
    """
    requests has no session-level timeout, so inject a default into the
    session's request(). Best-effort: never let this break startup.
    """
    for path in ("connection", "client.connection", "site.connection"):
        obj = client
        try:
            for part in path.split("."):
                obj = getattr(obj, part)
        except AttributeError:
            continue

        original = getattr(obj, "request", None)
        if original is None or getattr(original, "_timeout_patched", False):
            continue

        def request_with_timeout(*args, _original=original, **kwargs):
            kwargs.setdefault("timeout", timeout)
            return _original(*args, **kwargs)

        request_with_timeout._timeout_patched = True
        try:
            obj.request = request_with_timeout
            return True
        except Exception:
            continue
    return False


def _create_site():
    from mwrogue.esports_client import EsportsClient
    from mwrogue.auth_credentials import AuthCredentials

    username = os.getenv("LEAGUEPEDIA_USERNAME")
    password = os.getenv("LEAGUEPEDIA_PASSWORD")

    if username and password:
        credentials = AuthCredentials(username=username, password=password)
    else:
        # Fallback to the local credentials file (development).
        credentials = AuthCredentials(user_file="momouchi")

    client = EsportsClient("lol", credentials=credentials)
    _install_timeout(client, LEAGUEPEDIA_TIMEOUT)
    return client


def _get_site(force_new: bool = False):
    """
    Lazy, locked, cooldown-guarded client accessor.

    Lazy matters: building this at import time means the process refuses to
    start whenever Leaguepedia is unreachable, which on a free tier happens on
    every cold start.
    """
    global _site, _last_login_attempt

    with _site_lock:
        if _site is not None and not force_new:
            return _site

        now = time.monotonic()
        if force_new and (now - _last_login_attempt) < RELOGIN_COOLDOWN_SECONDS:
            # Too soon to re-login; reuse what we have rather than get throttled.
            if _site is not None:
                return _site

        _last_login_attempt = now
        try:
            _site = _create_site()
        except Exception as e:
            raise SourceUnavailable(f"Leaguepedia login failed: {e}") from e
        return _site


def leaguepedia_query(*, retries: int = 1, **kwargs):
    """
    Cargo query with one reconnect attempt.

    Only connection-shaped failures justify a reconnect — a malformed query or a
    game that simply is not there must not trigger a re-login.
    """
    last_error = None

    for attempt in range(retries + 1):
        try:
            client = _get_site(force_new=attempt > 0)
            return client.cargo_client.query(**kwargs)
        except SourceUnavailable:
            raise
        except Exception as e:
            last_error = e
            if attempt == retries:
                raise SourceUnavailable(
                    f"Leaguepedia query failed after reconnect: {e}"
                ) from e

    raise SourceUnavailable(f"Leaguepedia query failed: {last_error}")


BASE_FIELDS = (
    "OverviewPage, Tournament, Team1, Team2, GameId, Team1Score, Team2Score"
)
EXTENDED_FIELDS = BASE_FIELDS + ", Patch, DateTime_UTC, Gamelength"


def fetch_match_details(video_id: str) -> dict | None:
    """
    Fetch match metadata for a VOD. Returns None when Leaguepedia has no game
    matching this video id.
    """
    game_result = None
    for fields in (EXTENDED_FIELDS, BASE_FIELDS):
        try:
            game_result = leaguepedia_query(
                tables="ScoreboardGames",
                fields=fields,
                where='VOD LIKE "%{}%"'.format(video_id),
                limit=1,
                retries=1,
            )
            break
        except Exception:
            # Patch/DateTime_UTC/Gamelength were added later; if Cargo ever
            # rejects them, fall back rather than failing the whole lookup.
            if fields == BASE_FIELDS:
                raise

    matches = [normalize_keys(row) for row in (game_result or [])]
    game = matches[0] if matches else None
    if not game:
        return None

    players = leaguepedia_query(
        tables="ScoreboardPlayers",
        fields="Name, Champion, Kills, Deaths, Assists, Team, Role",
        where='GameId="{}"'.format(game["gameid"]),
        retries=1,
    )

    team1 = {"team_name": game["team1"], "players": []}
    team2 = {"team_name": game["team2"], "players": []}
    for player in players:
        player_data = normalize_keys(player)
        if player_data["team"] == game["team1"]:
            team1["players"].append(player_data)
        elif player_data["team"] == game["team2"]:
            team2["players"].append(player_data)

    game["team1"] = team1
    game["team2"] = team2
    return game
