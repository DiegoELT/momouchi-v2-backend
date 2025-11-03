from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from youtube_transcript_api import YouTubeTranscriptApi
from urllib.parse import urlparse, parse_qs
from mwrogue.esports_client import EsportsClient
from mwrogue.auth_credentials import AuthCredentials
import os

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

site = EsportsClient('lol', credentials=credentials)

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
    results = site.cargo_client.query(
        tables='Tournaments, ScoreboardGames',
        join_on='Tournaments.OverviewPage = ScoreboardGames.OverviewPage',
        fields='Tournaments.OverviewPage, Name, Team1, Team2, VOD, IsOfficial',
        where="VOD IS NOT NULL AND Tournaments.OverviewPage != '2025 Season World Championship/Main Event' AND IsOfficial='1' AND VOD NOT LIKE \"%live%\"",
        order_by='DateTime_UTC DESC',
        limit=10
    )
    return {"results": list(results)}
