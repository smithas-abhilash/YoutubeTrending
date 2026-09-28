# YouTube Trending Analyzer

A local web app that finds which kinds of YouTube channels are trending (by language), works out their content formula, and writes a master prompt you can reuse to make similar, original videos.

## Quick start (Windows)

1. Install Python 3.10+ from https://www.python.org/downloads/ and tick **Add python.exe to PATH**.
2. Double-click **`run.bat`**. The first run creates a virtual environment and installs everything.
3. The app opens at http://localhost:8501. Paste your keys into the sidebar.

To avoid pasting the keys every time, create `keys.bat` next to `run.bat` (it's git-ignored):

```bat
set YOUTUBE_API_KEY=AIza...
set ANTHROPIC_API_KEY=sk-ant-...
```

On Mac/Linux, or to run it by hand: `pip install -r requirements.txt` then `streamlit run app.py`.

| Key | Needed for | Get it |
|---|---|---|
| `YOUTUBE_API_KEY` | everything | Google Cloud Console → enable **YouTube Data API v3** → Credentials → API key |
| `ANTHROPIC_API_KEY` | optional: style labels, language checks, comment insights, image analysis, AI master prompt, refinement chat | https://console.anthropic.com |

Without an Anthropic key the app still works. It skips the Claude features and builds a simpler template prompt from the numbers.

## The six tabs

**① Trending by language.** Pulls YouTube's trending chart for the regions you pick.
- Claude labels each channel by its real format (e.g. "faceless AI horror Shorts") instead of YouTube's broad category.
- Claude also checks each video's language, telling Hindi from Marathi and catching Hinglish.
- "Hide TV / film / music labels" leaves only channels a newcomer could realistically copy.
- **Exclude categories** (Music, Film & Animation and Gaming by default) and a **minimum views/hour** filter cut out the noise.
- Each run is saved as a daily snapshot. After two or more days, **Trends over time** charts which formats are rising or fading.

**② 🤖 AI video finder.** YouTube's trending chart is dominated by trailers, music videos and big gamers, so AI channels rarely show up there. This tab searches recent uploads with AI-video keyword packs (AI stories, Hindi AI kahani, AI animals/babies Shorts, AI history/what-if, AI horror, AI kids cartoons, AI music, Sora/Veo/Kling), plus your own keywords, and ranks results by **views per hour**.
- Each video is judged AI or not using YouTube's "altered or synthetic content" disclosure and AI terms in the metadata. Optionally, Claude also looks at the thumbnail and gives a verdict, a reason, and the video's niche.
- Results are grouped into **AI niches ranked by median VPH**, so you can see which type of AI video is working right now.
- Each keyword costs 100 quota units per page, and results are cached for 6 hours.

**③ Breakout finder.** Finds recent videos from small channels that got far more views than their subscriber count would predict. These are the most copyable formats. Each search page costs 100 quota units.

**④ Analyse channels.** Enter one channel, or several to blend them into one shared formula. It reports:
- upload rhythm (in your time zone)
- Shorts vs long-form mix and typical lengths
- how the titles of the top 25% of videos differ from the rest
- tags and chapter sequences
- transcripts of the best videos
- what viewers ask for in the comments

**⑤ Master prompt.**
- Add screenshots, and optionally a reference video file. The app finds shot cuts, measures pacing (cuts per minute, hook vs body) and extracts keyframes. Only use videos you're allowed to use; the app never downloads from YouTube.
- Fill in your brief, then generate. Claude returns a beat sheet, packaging rules, a style guide, the **MASTER PROMPT**, topic ideas based on audience comments, and **tool packs** for the tools you list (Midjourney, Leonardo, ElevenLabs, CapCut, Canva, Runway, HeyGen, InVideo…).
- Use **Refine** to make changes by chatting ("make the hook shorter", "adapt for Shorts").

**⑥ History.** Every analysis, master prompt and breakout search is saved in `data/ytrend.db`. You can reload or delete any of them.

The master prompt copies the reference channel's structure and packaging only. It is written to produce original videos and does not reuse scripts, branding or titles.

## Quota and caching

The YouTube API gives 10,000 units a day by default. The sidebar shows how much you've used today.

| Action | Cost |
|---|---|
| Trending fetch | ~1 unit per 50 videos |
| Channel analysis | ~3–6 units |
| Breakout search | 100 units per page |

Responses are cached locally (trending for 1 hour, most other data for 6 hours), so repeat lookups cost nothing. Transcripts come from `youtube-transcript-api` and use no quota.

## Project layout

```
app.py                   Streamlit UI (6 tabs)
run.bat                  Windows one-click launcher
ytrend/youtube.py        YouTube Data API client (cache + quota tracking)
ytrend/trending.py       trending, breakout finder, trends over time
ytrend/ai_finder.py      AI-generated video discovery, ranked by views/hour
ytrend/channel.py        channel analysis, comments, multi-channel blend
ytrend/frames.py         local video shot/keyframe analysis (OpenCV)
ytrend/ai.py             Claude: style labels, language, comment insights
ytrend/master_prompt.py  master prompt, tool packs, refinement, offline template
ytrend/storage.py        SQLite cache, quota log, snapshots, history
ytrend/utils.py          durations, language detection, time zones
tests/                   offline tests with a fake YouTube API (pytest)
```
