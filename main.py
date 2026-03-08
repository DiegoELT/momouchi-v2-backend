from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from youtube_transcript_api import YouTubeTranscriptApi
from urllib.parse import urlparse, parse_qs
from mwrogue.esports_client import EsportsClient
from mwrogue.auth_credentials import AuthCredentials
import os
import threading

app = FastAPI()
ytt_api = YouTubeTranscriptApi()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # loosen later for security
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

username = os.getenv("LEAGUEPEDIA_USERNAME")
password = os.getenv("LEAGUEPEDIA_PASSWORD")

if username and password:
    # Use credentials from environment (Render)
    credentials = AuthCredentials(username=username, password=password)
else:
    # Fallback for local file (development)
    credentials = AuthCredentials(user_file="momouchi")

_site_lock = threading.Lock()
site = None

def _create_site():
    return EsportsClient('lol', credentials=credentials)

def _reset_site():
    global site
    with _site_lock:
        site = _create_site()
    return site

def _get_site():
    global site
    if site is None:
        site = _create_site()
    return site

def leaguepedia_query(*, retries: int = 1, **kwargs):
    """
    Cargo query with automatic reconnect on failure.
    """
    last_error = None 
    for attempt in range(retries + 1):
        try:
            client = _get_site()
            return client.cargo_client.query(**kwargs)
        except Exception as e:
            last_error = e
            # rebuild connection and retry
            _reset_site()
            if attempt == retries:
                raise last_error
            
site = _create_site()

def extract_youtube_id(video_url: str) -> str | None:
    try:
        # supports regular youtube URL and youtu.be
        parsed = urlparse(video_url)
        if parsed.hostname and "youtu" in parsed.hostname:
            if parsed.hostname == "youtu.be":
                return parsed.path.strip("/")
            qs = parse_qs(parsed.query)
            return qs.get("v", [None])[0]
    except Exception:
        return None
    return None

@app.get("/captions/")
def get_captions(video_url: str = Query(...)):
    """
    Extracts YouTube video ID and fetches captions if available.
    """
    try:
        parsed = urlparse(video_url)
        video_id = parse_qs(parsed.query).get("v", [None])[0]
        if not video_id:
            return {"error": "Invalid YouTube URL."}
        transcript = ytt_api.fetch(video_id)

        return {"video_id": video_id, "captions": transcript.to_raw_data()}
    except Exception as e:
        return {"error": str(e)}
    
@app.get("/leaguepedia/latest_games/")
def latest_games():
    try:
        results = leaguepedia_query(
            tables='Tournaments, ScoreboardGames',
            join_on='Tournaments.OverviewPage = ScoreboardGames.OverviewPage',
            fields='Tournaments.OverviewPage, Name, Team1, Team2, VOD, IsOfficial',
            where="VOD IS NOT NULL AND Tournaments.OverviewPage != '2025 Season World Championship/Main Event' AND IsOfficial='1' AND VOD NOT LIKE \"%live%\"",
            order_by='DateTime_UTC DESC',
            limit=10,
            retries=1
        )
        return {"results": list(results)}
    except Exception as e:
        return {"error": f"Leaguepedia query failed after reconnect: {str(e)}"}
    
@app.get("/match_details/")
def match_details(video_url: str = Query(...)):
    """
    Fetches match details from Leaguepedia based on the provided VOD URL.
    """
    video_id = extract_youtube_id(video_url)
    if not video_id:
        return {"error": "Could not extract YouTube video id from URL."}
    
    try:
        game_result = leaguepedia_query(
            tables='ScoreboardGames',
            fields='OverviewPage, Tournament, Team1, Team2, GameId, Team1Score, Team2Score',
            where='VOD LIKE "%{}%"'.format(video_id),
            limit=1,
            retries=1
        )
    except Exception as e:
        return {"error": f"Leaguepedia query failed after reconnect: {str(e)}"}

    # Change the key names to lowercase
    matches = []
    for match in game_result:
        matches.append({k.lower(): v for k, v in match.items()})

    # Now get the players for the game.
    game = matches[0] if matches else None
    if game:
        try:
            players = leaguepedia_query(
                tables='ScoreboardPlayers',
                fields='Name, Champion, Kills, Deaths, Assists, Team, Role',
                where='GameId="{}"'.format(game['gameid']),
                retries=1
            )
        except Exception as e:
            return {"error": f"Leaguepedia player query failed after reconnect: {str(e)}"}
        
        # Make Team1 and Team2 objects, with the name of the team from before, and the players
        team1 = {'team_name': game['team1'], 'players': []}
        team2 = {'team_name': game['team2'], 'players': []}
        for player in players:
            player_data = {k.lower(): v for k, v in player.items()}
            if player_data['team'] == game['team1']:
                team1['players'].append(player_data)
            elif player_data['team'] == game['team2']:
                team2['players'].append(player_data)
        game['team1'] = team1
        game['team2'] = team2

    return {"matches": game}
