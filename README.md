# essusic

A Discord music bot that plays audio in voice channels from YouTube and Spotify links.

## Features

- Stream audio from YouTube URLs or search keywords
- Spotify track, playlist, and album support (resolved via Spotify API, played from YouTube)
- Per-server queue with loop modes (off / single track / whole queue)
- Volume control, shuffle, pause/resume
- `/search` with interactive button UI to pick from results

## Commands

| Command | Description |
|---------|-------------|
| `/play <query>` | Play from YouTube URL, Spotify URL, or search keywords |
| `/stop` | Stop playback, clear queue, disconnect |
| `/skip` | Skip current track |
| `/queue` | Show the current queue |
| `/pause` | Pause playback |
| `/resume` | Resume playback |
| `/nowplaying` | Show currently playing track info |
| `/volume <1-100>` | Adjust volume |
| `/search <query>` | Search YouTube, show 5 results as buttons |
| `/shuffle` | Shuffle the queue |
| `/loop` | Cycle loop mode: off → single → queue → off |

## Prerequisites

- Python 3.10+
- FFmpeg (`sudo apt install ffmpeg`)
- libopus (`sudo apt install libopus0`)

## Setup

1. Clone the repo and install dependencies:
   ```bash
   git clone https://github.com/ts-solidarity/essusic.git
   cd essusic
   pip install -r requirements.txt
   ```

2. Copy `.env.example` to `.env` and fill in your tokens:
   ```bash
   cp .env.example .env
   ```
   - **DISCORD_TOKEN** — [Discord Developer Portal](https://discord.com/developers/applications) → Bot → Token
   - **SPOTIFY_CLIENT_ID** / **SPOTIFY_CLIENT_SECRET** — [Spotify Developer Dashboard](https://developer.spotify.com/dashboard) → Create App → Client ID + Secret

3. Run the bot:
   ```bash
   python3 bot.py
   ```

4. Invite the bot to your server with the `bot` and `applications.commands` scopes, then join a voice channel and try `/play`.
