"""Dry-run test for the cricket bot — no API keys needed."""

import asyncio
import bot


async def test_all():
    results = {"pass": 0, "fail": 0}

    def check(name, condition, detail=""):
        if condition:
            results["pass"] += 1
            print(f"  PASS  {name}")
        else:
            results["fail"] += 1
            print(f"  FAIL  {name} -- {detail}")

    # -----------------------------------------------------------
    print("=== 1. CRICBUZZ LIVE MATCHES ===")
    matches = await bot.fetch_live_matches()
    check("fetch_live_matches returns list", isinstance(matches, list))
    if matches:
        m = matches[0]
        check("match has id", bool(m.get("id")))
        check("match has team1", bool(m.get("team1")))
        check("match has team2", bool(m.get("team2")))
        print(f"         Found {len(matches)} live match(es)")
        for m in matches[:3]:
            print(f"         {m['team1']} vs {m['team2']} [{m['state']}]")
    else:
        print("         (No live matches right now)")

    # -----------------------------------------------------------
    print()
    print("=== 2. SCORECARD FETCH + PARSE ===")
    sc = await bot.fetch_scorecard("148569")
    check("fetch_scorecard returns dict", isinstance(sc, dict), f"got {type(sc)}")
    check("scoreCard key exists", "scoreCard" in sc if sc else False)

    if sc:
        dummy_match = {
            "id": "148569", "state": "complete",
            "team1": "INDCAP", "team2": "SNSS",
            "status": "SNSS won by 8 wkts", "matchDesc": "6th Match",
            "toss_winner": "Southern Super Stars", "toss_decision": "bowl",
        }
        state = bot.parse_scorecard(sc, dummy_match)
        check("parse returns dict", isinstance(state, dict))
        check("wickets > 0", state["wickets"] > 0, f"got {state['wickets']}")
        check("has batsmen data", len(state["all_batsmen_runs"]) > 0)
        check("score_summary set", state["score_summary"] != "")
        check("toss_done is True", state["toss_done"] is True)
        check("toss_detail has content", "chose to" in state["toss_detail"])
        check("match_over is True", state["match_over"] is True)
        check("result has content", len(state["result"]) > 5)
        print(f"         Score: {state['score_summary']}")
        print(f"         Wickets: {state['wickets']}")
        print(f"         Toss: {state['toss_detail']}")
        print(f"         Result: {state['result']}")

    # -----------------------------------------------------------
    print()
    print("=== 3. EVENT DETECTION ===")

    base_old = {
        "wickets": 2,
        "wickets_detail": [
            {"batsman": "A", "runs": 10, "how_out": "c X b Y"},
            {"batsman": "B", "runs": 25, "how_out": "lbw b Z"},
        ],
        "all_batsmen_runs": {"C": 45},
        "toss_done": True,
        "match_over": False,
    }
    base_new = {
        "wickets": 3,
        "wickets_detail": [
            {"batsman": "A", "runs": 10, "how_out": "c X b Y"},
            {"batsman": "B", "runs": 25, "how_out": "lbw b Z"},
            {"batsman": "C", "runs": 48, "how_out": "b W"},
        ],
        "all_batsmen_runs": {"C": 48, "D": 30},
        "toss_done": True,
        "match_over": False,
    }

    # Wicket
    ev = bot.detect_events("t1", base_old, base_new)
    check("detects wicket", any(e["type"] == "wicket" for e in ev))
    check("wicket count = 1", sum(1 for e in ev if e["type"] == "wicket") == 1)

    # Fifty
    old2 = {**base_old, "all_batsmen_runs": {"C": 48}}
    new2 = {**base_new, "all_batsmen_runs": {"C": 52}}
    ev2 = bot.detect_events("t2", old2, new2)
    check("detects fifty", any(e["type"] == "fifty" for e in ev2))

    # Hundred
    old3 = {**base_old, "all_batsmen_runs": {"C": 98}}
    new3 = {**base_new, "wickets": 2, "all_batsmen_runs": {"C": 103}}
    ev3 = bot.detect_events("t3", old3, new3)
    check("detects hundred", any(e["type"] == "hundred" for e in ev3))
    check("hundred not also fifty", not any(e["type"] == "fifty" for e in ev3))

    # Toss
    old4 = {**base_old, "toss_done": False}
    new4 = {**base_new, "toss_done": True, "toss_detail": "India won toss, bat"}
    ev4 = bot.detect_events("t4", old4, new4)
    check("detects toss", any(e["type"] == "toss" for e in ev4))

    # Match end
    old5 = {**base_old, "match_over": False}
    new5 = {**base_new, "match_over": True, "result": "India won by 5 wkts"}
    ev5 = bot.detect_events("t5", old5, new5)
    check("detects match_end", any(e["type"] == "match_end" for e in ev5))

    # First snapshot
    ev6 = bot.detect_events("t6", None, new4)
    check("first snapshot: only toss", len(ev6) <= 1)

    # No change
    ev7 = bot.detect_events("t7", base_old, base_old)
    check("no change = no events", len(ev7) == 0)

    print("         Ran 7 event detection scenarios")

    # -----------------------------------------------------------
    print()
    print("=== 4. NEWS SCRAPING ===")
    articles = await bot.scrape_news()
    check("scrape_news returns list", isinstance(articles, list))
    check("found articles", len(articles) > 0, f"got {len(articles)}")
    if articles:
        check("article has headline", bool(articles[0].get("headline")))
        check("article has source", bool(articles[0].get("source")))
        check("headline length > 20", len(articles[0]["headline"]) > 20)
        print(f"         Found {len(articles)} article(s)")
        for a in articles[:3]:
            print(f"         [{a['source']}] {a['headline'][:65]}")

    # -----------------------------------------------------------
    print()
    print("=== 5. PERSISTENCE ===")
    bot.match_states["test_match"] = {"wickets": 5, "score_summary": "TEST 100/5"}
    bot.seen_headlines.add("Test headline persistence")
    bot.save_state()
    check("state file created", bot.STATE_FILE.exists())

    bot.match_states.clear()
    bot.seen_headlines.clear()
    bot.load_state()
    check("match_states restored", "test_match" in bot.match_states)
    check("seen_headlines restored", "Test headline persistence" in bot.seen_headlines)

    bot.STATE_FILE.unlink(missing_ok=True)
    bot.match_states.clear()
    bot.seen_headlines.clear()

    # -----------------------------------------------------------
    print()
    print("=== 6. PROMPTS & TEMPLATES ===")
    event_types = ["wicket", "fifty", "hundred", "toss", "match_end", "news"]
    for et in event_types:
        check(f"tweet prompt: {et}", et in bot.TWEET_PROMPTS)
        check(f"image prompt: {et}", et in bot.IMAGE_PROMPTS)
        check(f"fallback tmpl: {et}", et in bot.FALLBACK_TEMPLATES)

    # Test fallback templates format correctly
    test_kw = {
        "detail": "Kohli out for 45", "context": "IND 150/3",
        "batsman": "Kohli", "runs": "45", "headline": "Test headline",
    }
    for et in event_types:
        tmpl = bot.FALLBACK_TEMPLATES[et]
        try:
            result = tmpl.format(**test_kw)
            check(f"fallback formats: {et}", len(result) > 0)
        except Exception as ex:
            check(f"fallback formats: {et}", False, str(ex))

    # -----------------------------------------------------------
    print()
    print("=== 7. ALL FUNCTIONS EXIST ===")
    fns = [
        "fetch_live_matches", "fetch_scorecard", "parse_scorecard",
        "detect_events", "generate_tweet", "generate_image",
        "upload_media", "post_tweet", "scrape_news",
        "match_monitor_job", "news_scraper_job", "main",
        "get_gemini_client", "get_twitter_client", "get_twitter_api",
        "save_state", "load_state",
    ]
    for fn in fns:
        check(f"fn: {fn}", hasattr(bot, fn) and callable(getattr(bot, fn)))

    # -----------------------------------------------------------
    print()
    total = results["pass"] + results["fail"]
    print(f"=== RESULTS: {results['pass']}/{total} passed, {results['fail']} failed ===")
    if results["fail"] == 0:
        print("ALL TESTS PASSED -- ready to deploy!")
    else:
        print("SOME TESTS FAILED -- check above")


if __name__ == "__main__":
    asyncio.run(test_all())
