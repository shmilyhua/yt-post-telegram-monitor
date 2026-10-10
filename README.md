[![AI Generated](https://img.shields.io/badge/AI_Generated-Gemini-blue.svg)](https://gemini.google.com)
## Acknowledgments

* **Code Generation:** The core logic and boilerplate for this project were generated using Gemini. All AI-generated code was subsequently reviewed and tested.
> **Note:** This repository is directly inspired by [NothingNaN/YoutubeCommunityScraper](https://github.com/NothingNaN/YoutubeCommunityScraper)) and [HoloArchivists/youtube-community-tab](https://github.com/HoloArchivists/youtube-community-tab). The modifications, refactoring, and new features introduced in this project were primarily implemented using Gemini.
# yt-post-telegram-monitor
An asynchronous Python bot that monitors YouTube channel posts (formerly Community Tab) and pushes formatted updates directly to a Telegram chat.

## Features
* **Asynchronous Fetching:** Utilizes `aiohttp` and `asyncio` for non-blocking network requests.
* **Smart Link Resolution:** Extracts exact URLs from YouTube's internal redirect wrappers.
* **Membership Detection:** Identifies membership-only posts and hides gated content while notifying you of the post's existence.
* **State Persistence:** Maintains a local JSON database of seen post IDs to prevent duplicate notifications across restarts.

## Installation

1. Clone the repository:
   ```bash
   git clone https://github.com/shmilyhua/yt-post-telegram-monitor.git
   cd yt-post-telegram-monitor
   ```

2. This project uses `uv` for dependency management. Install the dependencies:
   ```bash
   uv venv --python 3.13
   uv sync
   ```
   *(Alternatively, you can install the dependencies listed in `pyproject.toml` via pip).*

## Configuration

1. Create a `config.json` file in the root directory. This file is ignored by git to protect your credentials.
2. Structure your `config.json` as follows:

```json
{
  "CHECK_INTERVAL": 180,
  "TARGET_CHANNELS": [
    "@NinomaeInanis",
    "https://www.youtube.com/@3blue1brown"
  ],
  "COOKIE_FILE": "cookies.txt",
  "TELEGRAM_TOKEN": "YOUR_TELEGRAM_BOT_TOKEN",
  "TELEGRAM_CHAT_ID": "-100YOUR_CHAT_ID",
  "SEED_FILE": "seen_posts.json",
  "MAX_HISTORY": 1000
}
```
TARGET_CHANNELS: List of YouTube channel handles or URLs.

TELEGRAM_TOKEN: Your Telegram Bot API token.

TELEGRAM_CHAT_ID: The target Telegram chat ID.

COOKIE_FILE: Path to your Netscape format cookies.txt (optional, required for membership posts).
## Usage

Start the monitor by running:

```bash
uv run monitor.py
```
