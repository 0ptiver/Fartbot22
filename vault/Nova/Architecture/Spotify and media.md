---
tags: [nova, architecture, music]
---
# Spotify and media

- `integrations/spotify.py`: PKCE login (no client secret), tokens in Windows Credential Manager, strict check that playback really started (`NotPlaying`).
- `tools/music.play_music`: "play some Drake", "play my liked songs", "queue ...", "shuffle on", "what's playing?" Confirms through Windows' media list, and falls back to clicking the Spotify app's own Play button.
- Without Spotify setup, "pause", "next song" and "go back" work for any app (media keys), checked.
- `video`: browser video via Windows' media list (GSMTC) + the player's own buttons (full screen, skip ad, skip ahead).
- Windows' media list **can hang forever**: every call is time-limited (`video.ASK_S`); after a hang it isn't asked for 60 s (`MediaBackend.stuck()`), and Spotify's own check is used instead (`_spotify_checked`).
- Setup: `python -m assistant spotify login` (an app at developer.spotify.com, redirect `http://127.0.0.1:8888/callback`). Diagnose: `spotify devices`, `spotify test [song]`.
