# YTAi
## Introduction
YTAi is a YouTube shorts clipper and channel scraper that utilizes the YouTube Data API and Innertube API to fetch and process YouTube data.
## Key Features
- Clips YouTube shorts videos
- Scrapes YouTube channels
- Utilizes Tor for anonymous browsing
- Supports environment variables for API keys
## Tech Stack
- Python 3
- requests library
- google-generativeai library
- yt-dlp
- Tor
## Installation
1. Install required system packages: python, ffmpeg, yt-dlp, tor
2. Install required Python packages: requests, google-generativeai
3. Set environment variables: GEMINI_API_KEY, YOUTUBE_API_KEY
## Usage
1. Run the run.sh script to set up and run the application
2. Follow the prompts to enter API keys if not already set
## Environment Variables
- GEMINI_API_KEY: Gemini API key for AI services
- YOUTUBE_API_KEY: YouTube API key for YouTube Data API
- TOR_PROXY: Tor proxy address (default: socks5h://127.0.0.1:9050)
## Code

```
# Example code snippet
import os
import requests

# ...```