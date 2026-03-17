"""
Cricket Twitter Bot
Monitors live matches on Cricbuzz, scrapes cricket news,
generates journalist-style tweets with Gemini, and posts to X.
"""

import asyncio
import base64
import io
import json
import logging
import logging.handlers
import os
import re
from datetime import date
from pathlib import Path

import httpx
import tweepy
from dotenv import load_dotenv
from google import genai
from google.genai import types as genai_types
from requests_oauthlib import OAuth1
import requests as sync_requests
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

load_dotenv()

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    handlers=[
        logging.StreamHandler(),
        logging.handlers.RotatingFileHandler(
            "cricket_bot.log", maxBytes=5_000_000, backupCount=3
        ),
    ],
)
logger = logging.getLogger("cricket_bot")

STATE_FILE = Path("bot_state.json")
HTTP_TIMEOUT = 15.0
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)

# Lazy-initialised clients (created on first use so missing keys don't crash import)
_gemini_client = None
_twitter_client = None


def get_gemini_client() -> genai.Client:
    global _gemini_client
    if _gemini_client is None:
        key = os.getenv("GEMINI_API_KEY")
        if not key:
            raise RuntimeError("GEMINI_API_KEY not set in .env")
        _gemini_client = genai.Client(api_key=key)
    return _gemini_client


def get_twitter_client() -> tweepy.Client:
    global _twitter_client
    if _twitter_client is None:
        _twitter_client = tweepy.Client(
            consumer_key=os.getenv("TWITTER_API_KEY"),
            consumer_secret=os.getenv("TWITTER_API_SECRET"),
            access_token=os.getenv("TWITTER_ACCESS_TOKEN"),
            access_token_secret=os.getenv("TWITTER_ACCESS_TOKEN_SECRET"),
        )
    return _twitter_client


def get_twitter_oauth1() -> OAuth1:
    """OAuth 1.0a signer for v2 media upload endpoint."""
    return OAuth1(
        os.getenv("TWITTER_API_KEY"),
        os.getenv("TWITTER_API_SECRET"),
        os.getenv("TWITTER_ACCESS_TOKEN"),
        os.getenv("TWITTER_ACCESS_TOKEN_SECRET"),
    )


# In-memory state
match_states: dict[str, dict] = {}
seen_headlines: set[str] = set()

# ---------------------------------------------------------------------------
# Persistence — survive restarts without duplicate tweets
# ---------------------------------------------------------------------------


def save_state():
    """Persist match states and seen headlines to disk."""
    data = {
        "match_states": match_states,
        "seen_headlines": list(seen_headlines)[-500:],
    }
    STATE_FILE.write_text(json.dumps(data, default=str), encoding="utf-8")


def load_state():
    """Reload state from disk if available."""
    global match_states, seen_headlines
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            match_states.update(data.get("match_states", {}))
            seen_headlines.update(data.get("seen_headlines", []))
            logger.info("Loaded saved state (%d matches, %d headlines)",
                        len(match_states), len(seen_headlines))
        except Exception as e:
            logger.warning("Could not load state file: %s", e)


# ---------------------------------------------------------------------------
# Helpers for extracting JSON from Cricbuzz RSC (React Server Components)
# ---------------------------------------------------------------------------


def _unescape_rsc(html: str) -> str:
    """Unescape the double-escaped JSON embedded in Cricbuzz RSC streams."""
    return html.replace('\\"', '"')


def _extract_json_block(text: str, key: str) -> dict | None:
    """
    Find a JSON object that is the value of a given key in a text blob.
    e.g. key='scorecardApiData' finds {"scoreCard": [...], ...}
    Returns the parsed dict or None.
    """
    marker = f'"{key}":'
    idx = text.find(marker)
    if idx < 0:
        return None

    # Find the opening brace
    brace_start = text.find("{", idx + len(marker))
    if brace_start < 0:
        return None

    # Walk to find balanced closing brace
    depth = 0
    for i in range(brace_start, min(brace_start + 500_000, len(text))):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
        if depth == 0:
            try:
                return json.loads(text[brace_start : i + 1])
            except json.JSONDecodeError:
                return None
    return None


# ---------------------------------------------------------------------------
# Cricbuzz data fetching — scrapes the website's RSC data
# ---------------------------------------------------------------------------


async def _get_page(url: str) -> str | None:
    """Fetch a Cricbuzz page and return the unescaped HTML."""
    try:
        async with httpx.AsyncClient(
            timeout=HTTP_TIMEOUT, follow_redirects=True
        ) as client:
            resp = await client.get(url, headers={"User-Agent": BROWSER_UA})
            resp.raise_for_status()
            return _unescape_rsc(resp.text)
    except Exception as e:
        logger.warning("Failed to fetch %s: %s", url, e)
        return None


async def fetch_live_matches() -> list[dict]:
    """
    Scrape the Cricbuzz live-scores page and return matches
    that are currently in progress.
    """
    text = await _get_page("https://www.cricbuzz.com/cricket-match/live-scores")
    if not text:
        return []

    # The page embeds a JSON blob containing typeMatches
    data = _extract_json_block(text, "typeMatches")
    # typeMatches is an array, not an object — find it differently
    if not data:
        # Try to find the parent object that contains typeMatches
        idx = text.find('"typeMatches"')
        if idx < 0:
            return []
        # Walk backwards to find the enclosing {
        brace = text.rfind("{", 0, idx)
        if brace < 0:
            return []
        depth = 0
        for i in range(brace, min(brace + 500_000, len(text))):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
            if depth == 0:
                try:
                    data = json.loads(text[brace : i + 1])
                except json.JSONDecodeError:
                    return []
                break

    if not data:
        return []

    matches = []
    live_states = {"inprogress", "toss", "innings break"}

    for group in data.get("typeMatches", []):
        for series in group.get("seriesMatches", []):
            sw = series.get("seriesAdWrapper", {})
            if not sw:
                continue
            for m in sw.get("matches", []):
                mi = m.get("matchInfo", {})
                state = mi.get("state", "").lower()
                match_id = str(mi.get("matchId", ""))

                if match_id and state in live_states:
                    toss = mi.get("tossResults", {})
                    matches.append({
                        "id": match_id,
                        "state": state,
                        "team1": mi.get("team1", {}).get("teamSName", ""),
                        "team2": mi.get("team2", {}).get("teamSName", ""),
                        "status": mi.get("status", ""),
                        "matchDesc": mi.get("matchDesc", ""),
                        "toss_winner": toss.get("tossWinnerName", ""),
                        "toss_decision": toss.get("decision", ""),
                    })

    return matches


async def fetch_scorecard(match_id: str) -> dict | None:
    """
    Fetch the scorecard page for a match and extract the
    scorecardApiData JSON from the RSC stream.
    """
    text = await _get_page(
        f"https://www.cricbuzz.com/live-cricket-scorecard/{match_id}/"
    )
    if not text:
        return None

    data = _extract_json_block(text, "scorecardApiData")
    return data


# ---------------------------------------------------------------------------
# Parse scorecard into normalised state
# ---------------------------------------------------------------------------


def parse_scorecard(scorecard: dict, live_match: dict) -> dict:
    """
    Convert Cricbuzz scorecard data into a normalised MatchState dict.
    """
    state = {
        "wickets": 0,
        "wickets_detail": [],
        "all_batsmen_runs": {},
        "toss_done": False,
        "toss_detail": "",
        "match_over": False,
        "result": "",
        "score_summary": live_match.get("status", ""),
        "team1": live_match.get("team1", ""),
        "team2": live_match.get("team2", ""),
        "matchDesc": live_match.get("matchDesc", ""),
    }

    # --- Toss info (from live match listing) ---
    if live_match.get("toss_winner"):
        state["toss_done"] = True
        state["toss_detail"] = (
            f"{live_match['toss_winner']} won the toss "
            f"and chose to {live_match.get('toss_decision', 'bat')}"
        )

    # --- Innings data ---
    innings_list = scorecard.get("scoreCard", [])
    for innings in innings_list:
        # Wickets
        wkts = innings.get("scoreDetails", {}).get("wickets", 0)
        state["wickets"] += wkts

        # Batsmen
        batsmen_data = innings.get("batTeamDetails", {}).get("batsmenData", {})
        for bat in batsmen_data.values():
            if isinstance(bat, dict) and bat.get("batName"):
                name = bat["batName"]
                runs = bat.get("runs", 0)
                state["all_batsmen_runs"][name] = runs

                out_desc = bat.get("outDesc", "")
                if out_desc:
                    state["wickets_detail"].append({
                        "batsman": name,
                        "runs": runs,
                        "how_out": out_desc,
                    })

    # --- Score summary ---
    if innings_list:
        last = innings_list[-1].get("scoreDetails", {})
        runs_str = last.get("runs", "?")
        wkts_str = last.get("wickets", "?")
        overs_str = last.get("overs", "?")
        batting_team = (
            innings_list[-1].get("batTeamDetails", {}).get("batTeamShortName", "")
        )
        state["score_summary"] = (
            f"{batting_team} {runs_str}/{wkts_str} ({overs_str} ov)"
        )

    # --- Match over? ---
    if scorecard.get("isMatchComplete"):
        state["match_over"] = True
        state["result"] = scorecard.get("status", live_match.get("status", ""))

    # Also check matchHeader for result
    match_header = scorecard.get("matchHeader", {})
    if match_header.get("state", "").lower() == "complete":
        state["match_over"] = True
        state["result"] = match_header.get("status", state["result"])

    return state


# ---------------------------------------------------------------------------
# Event detection
# ---------------------------------------------------------------------------


def detect_events(match_id: str, old: dict | None, new: dict) -> list[dict]:
    """Compare two MatchState snapshots and return detected events."""
    events = []

    # Toss
    if (old is None or not old.get("toss_done")) and new.get("toss_done"):
        events.append({"type": "toss", "detail": new["toss_detail"]})

    if old is None:
        return events  # first snapshot, nothing to compare

    # Wickets
    if new["wickets"] > old["wickets"]:
        new_dismissals = new["wickets_detail"][len(old["wickets_detail"]):]
        for d in new_dismissals:
            events.append({
                "type": "wicket",
                "detail": f"{d['batsman']} out for {d['runs']} — {d['how_out']}",
            })

    # Fifty / Hundred
    for batsman, runs in new["all_batsmen_runs"].items():
        old_runs = old.get("all_batsmen_runs", {}).get(batsman, 0)
        if old_runs < 100 <= runs:
            events.append({"type": "hundred", "batsman": batsman, "runs": runs})
        elif old_runs < 50 <= runs:
            events.append({"type": "fifty", "batsman": batsman, "runs": runs})

    # Match ended
    if not old.get("match_over") and new.get("match_over"):
        events.append({
            "type": "match_end",
            "detail": new.get("result", "Match over"),
        })

    return events


# ---------------------------------------------------------------------------
# Tweet generation with Gemini
# ---------------------------------------------------------------------------

TWEET_PROMPTS = {
    "wicket": (
        "You are a cricket journalist tweeting live. Write a punchy tweet "
        "(under 250 chars) reacting to this wicket: {detail}. "
        "Match: {context}. Use cricket lingo. Sound natural, not robotic. "
        "No hashtags unless they feel organic."
    ),
    "fifty": (
        "You are a cricket journalist. Write a tweet (under 250 chars) "
        "celebrating {batsman} reaching {runs} runs. Be enthusiastic but "
        "professional. Match: {context}."
    ),
    "hundred": (
        "You are a cricket journalist. Write a memorable tweet (under 250 chars) "
        "celebrating {batsman} scoring a brilliant century ({runs} runs). "
        "Make it vivid. Match: {context}."
    ),
    "toss": (
        "You are a cricket journalist. Write a crisp tweet (under 250 chars) "
        "announcing the toss result: {detail}. Be informative."
    ),
    "match_end": (
        "You are a cricket journalist. Write a sharp analytical tweet "
        "(under 250 chars) summarising this result: {detail}."
    ),
    "news": (
        "You are a cricket journalist sharing a story. Write a tweet "
        "(under 250 chars) about this headline: {headline}. "
        "Sound like you're breaking the news. No hashtags."
    ),
}

FALLBACK_TEMPLATES = {
    "wicket": "WICKET! {detail} | {context}",
    "fifty": "FIFTY! {batsman} reaches {runs}* | {context}",
    "hundred": "CENTURY! {batsman} smashes {runs}* | {context}",
    "toss": "TOSS: {detail}",
    "match_end": "RESULT: {detail}",
    "news": "NEWS: {headline}",
}


# ---------------------------------------------------------------------------
# Image generation with Gemini Imagen
# ---------------------------------------------------------------------------

IMAGE_PROMPTS = {
    "wicket": (
        "Professional cricket broadcast-style graphic, dark blue-green gradient "
        "background, dramatic sports atmosphere. Bold white text overlay showing: "
        "\"WICKET\" at the top. Below it: \"{detail}\". Current score: \"{context}\". "
        "Cricket stumps breaking apart visual element. Clean modern sports design, "
        "16:9 aspect ratio, no real player faces."
    ),
    "fifty": (
        "Professional cricket broadcast graphic celebrating a half-century milestone. "
        "Dark background with gold accents and sparkle effects. Bold gold text: "
        "\"50*\" prominently displayed. Player name: \"{batsman}\" with \"{runs} runs\". "
        "Match: \"{context}\". Celebratory sports design with cricket bat icon, "
        "16:9 aspect ratio, no real player faces."
    ),
    "hundred": (
        "Premium cricket broadcast graphic celebrating a century. Rich dark background "
        "with gold and red accents, dramatic lighting effects. Bold gold text: "
        "\"CENTURY!\" prominently displayed. Player name: \"{batsman}\" with "
        "\"{runs} runs\". Match: \"{context}\". Triumphant sports design with cricket "
        "bat and stars, 16:9 aspect ratio, no real player faces."
    ),
    "toss": (
        "Clean cricket match preview graphic. Dark gradient background with team "
        "colors. A coin toss visual element. Text overlay: \"{detail}\". "
        "Professional sports broadcast style, 16:9 aspect ratio."
    ),
    "match_end": (
        "Professional cricket match result graphic. Dark dramatic background. "
        "Bold white and gold text: \"RESULT\" at top. Below: \"{detail}\". "
        "Trophy or championship visual element. Clean sports broadcast design, "
        "16:9 aspect ratio."
    ),
    "news": (
        "Professional cricket news graphic. Dark blue gradient background with "
        "subtle cricket field pattern. Bold white headline text: \"{headline}\". "
        "\"BREAKING\" tag in red at top corner. Clean news broadcast style, "
        "16:9 aspect ratio."
    ),
}


async def generate_image(event: dict) -> bytes | None:
    """Generate a cricket graphic using Gemini Imagen. Returns PNG bytes or None."""
    event_type = event.get("type", "")
    prompt_template = IMAGE_PROMPTS.get(event_type)
    if not prompt_template:
        return None

    prompt = prompt_template.format(
        detail=event.get("detail", ""),
        context=event.get("context", ""),
        batsman=event.get("batsman", ""),
        runs=event.get("runs", ""),
        headline=event.get("headline", ""),
    )

    try:
        response = get_gemini_client().models.generate_images(
            model="imagen-3.0-generate-002",
            prompt=prompt,
            config=genai_types.GenerateImagesConfig(
                number_of_images=1,
                aspect_ratio="16:9",
                safety_filter_level="BLOCK_ONLY_HIGH",
            ),
        )
        if response.generated_images:
            image = response.generated_images[0].image
            if image and image.image_bytes:
                logger.info("Image generated for %s event", event_type)
                return image.image_bytes
        logger.warning("Imagen returned no images for %s event", event_type)
        return None
    except Exception as e:
        logger.warning("Image generation failed: %s", e)
        return None


async def upload_media(image_bytes: bytes) -> str | None:
    """
    Upload image to X using v2 media upload endpoint.
    Tries simple upload first, falls back to chunked (INIT/APPEND/FINALIZE).
    Uses OAuth 1.0a authentication as required by the media endpoint.
    """
    auth = get_twitter_oauth1()
    loop = asyncio.get_event_loop()

    # --- Attempt 1: Simple upload with base64 media_data ---
    try:
        def _simple_upload():
            return sync_requests.post(
                "https://api.x.com/2/media/upload",
                data={
                    "media_data": base64.b64encode(image_bytes).decode(),
                    "media_category": "tweet_image",
                },
                auth=auth,
                timeout=30,
            )

        resp = await loop.run_in_executor(None, _simple_upload)

        if resp.status_code in (200, 201, 202):
            data = resp.json()
            media_id = data.get("media_id_string", str(data.get("media_id", "")))
            if media_id:
                logger.info("Media uploaded (v2 simple): %s", media_id)
                return media_id

        logger.warning("Simple upload returned %s: %s",
                        resp.status_code, resp.text[:200])
    except Exception as e:
        logger.warning("Simple upload failed (%s), trying chunked...", e)

    # --- Attempt 2: Chunked upload (INIT → APPEND → FINALIZE) ---
    try:
        def _chunked_upload():
            # INIT
            init_resp = sync_requests.post(
                "https://api.x.com/2/media/upload",
                data={
                    "command": "INIT",
                    "media_type": "image/png",
                    "total_bytes": str(len(image_bytes)),
                    "media_category": "tweet_image",
                },
                auth=auth,
                timeout=30,
            )
            if init_resp.status_code not in (200, 201, 202):
                logger.warning("INIT failed %s: %s",
                                init_resp.status_code, init_resp.text[:200])
                return None

            media_id = init_resp.json().get(
                "media_id_string",
                str(init_resp.json().get("media_id", ""))
            )
            if not media_id:
                return None

            # APPEND — send in 1MB chunks
            chunk_size = 1_000_000
            for i in range(0, len(image_bytes), chunk_size):
                chunk = image_bytes[i : i + chunk_size]
                seg_idx = i // chunk_size
                append_resp = sync_requests.post(
                    "https://api.x.com/2/media/upload",
                    data={
                        "command": "APPEND",
                        "media_id": media_id,
                        "segment_index": str(seg_idx),
                    },
                    files={"media": ("chunk", io.BytesIO(chunk), "application/octet-stream")},
                    auth=auth,
                    timeout=30,
                )
                if append_resp.status_code not in (200, 201, 202, 204):
                    logger.warning("APPEND failed %s: %s",
                                    append_resp.status_code, append_resp.text[:200])
                    return None

            # FINALIZE
            fin_resp = sync_requests.post(
                "https://api.x.com/2/media/upload",
                data={
                    "command": "FINALIZE",
                    "media_id": media_id,
                },
                auth=auth,
                timeout=30,
            )
            if fin_resp.status_code in (200, 201, 202):
                logger.info("Media uploaded (v2 chunked): %s", media_id)
                return media_id

            logger.warning("FINALIZE failed %s: %s",
                            fin_resp.status_code, fin_resp.text[:200])
            return None

        media_id = await loop.run_in_executor(None, _chunked_upload)
        if media_id:
            return media_id
    except Exception as e:
        logger.warning("Chunked upload failed: %s", e)

    return None


async def generate_tweet(event: dict) -> str:
    """Generate a tweet using Gemini. Falls back to template if Gemini fails."""
    event_type = event.get("type", "")
    prompt_template = TWEET_PROMPTS.get(event_type)
    if not prompt_template:
        return ""

    prompt = prompt_template.format(
        detail=event.get("detail", ""),
        context=event.get("context", ""),
        batsman=event.get("batsman", ""),
        runs=event.get("runs", ""),
        headline=event.get("headline", ""),
    )

    try:
        response = get_gemini_client().models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
        )
        tweet = response.text.strip().strip('"').strip("'")
        if len(tweet) > 280:
            tweet = tweet[:277] + "..."
        return tweet
    except Exception as e:
        logger.warning("Gemini failed, using template: %s", e)
        fallback = FALLBACK_TEMPLATES.get(event_type, "")
        return fallback.format(
            detail=event.get("detail", ""),
            context=event.get("context", ""),
            batsman=event.get("batsman", ""),
            runs=event.get("runs", ""),
            headline=event.get("headline", ""),
        )[:280]


# ---------------------------------------------------------------------------
# Post to X (Twitter)
# ---------------------------------------------------------------------------

# Daily tweet counter — free tier allows ~500 posts/month (~16/day)
MAX_TWEETS_PER_DAY = int(os.getenv("MAX_TWEETS_PER_DAY", "15"))
_tweet_count = 0
_tweet_count_date = date.today()
recent_tweets: set[str] = set()


def _check_daily_limit() -> bool:
    """Return True if we can still tweet today."""
    global _tweet_count, _tweet_count_date
    today = date.today()
    if today != _tweet_count_date:
        _tweet_count = 0
        _tweet_count_date = today
    return _tweet_count < MAX_TWEETS_PER_DAY


async def post_tweet(text: str, image_bytes: bytes | None = None) -> bool:
    """Post a tweet to X, optionally with an image. Returns True on success."""
    global _tweet_count
    if not text:
        return False
    if text in recent_tweets:
        logger.info("Skipping duplicate tweet")
        return False
    if not _check_daily_limit():
        logger.warning("Daily tweet limit reached (%d/%d) — skipping",
                        _tweet_count, MAX_TWEETS_PER_DAY)
        return False

    try:
        # Upload image first if provided
        media_ids = None
        if image_bytes:
            media_id = await upload_media(image_bytes)
            if media_id:
                media_ids = [media_id]

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: get_twitter_client().create_tweet(
                text=text,
                media_ids=media_ids,
            ),
        )
        recent_tweets.add(text)
        _tweet_count += 1
        if len(recent_tweets) > 200:
            recent_tweets.clear()
        logger.info("Tweet posted%s (%d/%d today): %s",
                     " with image" if media_ids else "",
                     _tweet_count, MAX_TWEETS_PER_DAY, text[:80])
        return True
    except tweepy.TooManyRequests:
        logger.warning("Twitter rate limit hit — skipping")
        return False
    except tweepy.TweepyException as e:
        logger.error("Tweet failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# News scraping
# ---------------------------------------------------------------------------


async def scrape_news() -> list[dict]:
    """Scrape latest cricket news headlines from Cricbuzz."""
    articles = []

    text = await _get_page("https://www.cricbuzz.com/cricket-news")
    if not text:
        return articles

    # Extract headlines from the RSC data via regex
    headlines = re.findall(r'"headline":"([^"]{20,})"', text)

    for title in headlines[:10]:
        if title not in seen_headlines:
            seen_headlines.add(title)
            articles.append({"source": "Cricbuzz", "headline": title})

    # Keep seen_headlines bounded
    if len(seen_headlines) > 500:
        to_remove = len(seen_headlines) - 250
        for _ in range(to_remove):
            seen_headlines.pop()

    return articles


# ---------------------------------------------------------------------------
# Scheduled jobs
# ---------------------------------------------------------------------------


async def match_monitor_job():
    """Poll Cricbuzz for live matches, detect events, tweet."""
    try:
        matches = await fetch_live_matches()
        if not matches:
            logger.info("No live matches right now")
            return

        logger.info("Tracking %d live match(es)", len(matches))

        for match in matches:
            mid = match["id"]

            scorecard = await fetch_scorecard(mid)
            if not scorecard:
                continue

            new_state = parse_scorecard(scorecard, match)
            old_state = match_states.get(mid)

            events = detect_events(mid, old_state, new_state)
            match_states[mid] = new_state

            for event in events:
                event["context"] = new_state.get("score_summary", "")
                tweet_text = await generate_tweet(event)
                if tweet_text:
                    image = await generate_image(event)
                    await post_tweet(tweet_text, image_bytes=image)
                    await asyncio.sleep(2)

        save_state()

    except Exception as e:
        logger.exception("Match monitor error: %s", e)


async def news_scraper_job():
    """Scrape cricket news and tweet about it."""
    try:
        articles = await scrape_news()
        logger.info("Found %d new article(s)", len(articles))

        for article in articles[:3]:
            event = {"type": "news", "headline": article["headline"]}
            tweet_text = await generate_tweet(event)
            if tweet_text:
                image = await generate_image(event)
                await post_tweet(tweet_text, image_bytes=image)
                await asyncio.sleep(5)

        save_state()

    except Exception as e:
        logger.exception("News scraper error: %s", e)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main():
    load_state()

    poll_interval = int(os.getenv("MATCH_POLL_INTERVAL", "120"))
    news_interval = int(os.getenv("NEWS_POLL_INTERVAL", "5400"))

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        match_monitor_job, "interval", seconds=poll_interval,
        id="match_monitor", max_instances=1,
    )
    scheduler.add_job(
        news_scraper_job, "interval", seconds=news_interval,
        id="news_scraper", max_instances=1,
    )

    scheduler.start()
    logger.info(
        "Cricket bot started — match poll: %ds, news poll: %ds",
        poll_interval, news_interval,
    )

    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        save_state()
        logger.info("Bot shut down.")


if __name__ == "__main__":
    asyncio.run(main())
