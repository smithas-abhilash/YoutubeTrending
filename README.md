# YouTube Trending Analyzer

A web app that:

1. **Finds what's trending by language.** It pulls YouTube's trending charts for one or more regions, detects each video's language, and shows the top channel categories, formats, lengths, keywords, and channels for each language.
2. **Works out a channel's formula.** For any channel (pick one from step 1 or paste a handle/URL) it reads the recent uploads and reports:
   - upload cadence (days and hours)
   - Shorts vs long-form mix and typical lengths
   - how the titles of the top 25% of videos differ from the rest
   - tags and description boilerplate
   - chapter sequences
   - transcripts of the best videos, timestamped, so you can see the hook → segments → CTA order
   - thumbnails of the best videos
3. **Writes a master prompt.** You add screenshots of a trending video (hook frame, title card, typical shots, captions, end screen) and a short brief (your channel, language, topic, audience, presenter style, tools). Claude looks at the data and the images and returns:
   - a timestamped beat sheet
   - title and thumbnail recipes
   - a style guide
   - a paste-ready **MASTER PROMPT** with `{TOPIC}`, `{LANGUAGE}`, `{DURATION}` and `{CHANNEL_NAME}` placeholders

The master prompt copies the reference channel's structure and packaging only. It is written to produce original videos and does not reuse the channel's scripts, branding, or titles.

## Setup

```bash
pip install -r requirements.txt
streamlit run app.py
```

Enter these keys in the sidebar, or set them as environment variables:

| Key | Needed for | Get it |
|---|---|---|
| `YOUTUBE_API_KEY` | everything | Google Cloud Console → enable **YouTube Data API v3** → Credentials → API key |
| `ANTHROPIC_API_KEY` | analysing images and writing the AI master prompt (optional) | https://console.anthropic.com |

Without an Anthropic key, the app builds a simpler template prompt from the numbers alone.

### Quota notes
The YouTube API gives 10,000 units a day by default. A trending fetch costs about 1 unit per 50 videos, and a channel analysis about 3–5 units. Transcripts don't use any quota because they come from `youtube-transcript-api`.

## Project layout

```
app.py                  Streamlit UI (3 tabs)
ytrend/youtube.py       YouTube Data API v3 client
ytrend/trending.py      trending collection + per-language aggregation
ytrend/channel.py       channel formula analysis (cadence, titles, chapters, transcripts)
ytrend/master_prompt.py Claude (vision) master-prompt generation + offline template
ytrend/utils.py         durations, language detection, text features
```
