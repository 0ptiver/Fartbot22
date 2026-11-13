---
tags: [nova, moc]
aliases: [Nova, Home]
updated: 2026-11-13
---
# 🤖 Nova: Home

> [!summary] What this is
> **Nova** is a voice-first, JARVIS-style personal assistant on Oliver's Windows 11 gaming laptop. It's a British butler that calls you "sir". It listens for its name, controls the PC, sees the screen, and hands hard jobs to Claude on your Pro subscription. Everything runs locally except the Claude hand-off, and it costs nothing beyond Claude Pro.

**Code:** `C:\Users\patto\Fartbot22` · GitHub `0ptiver/Fartbot22`, branch `claude/jarvis-voice-assistant-lhnfza`
**Python package:** `assistant/` (the name "Nova" is only config, so renaming is easy)

## Start here
- [[What is Nova]]: the big picture in plain words
- [[Start, stop, update]]: the commands you actually need
- [[Voice commands cheat sheet]]: what to say
- [[Troubleshooting]]: when something goes wrong

## Overview
- [[What is Nova]]
- [[Owner and PC]]
- [[How Nova thinks]]: local model vs Claude

## How it's built (Architecture)
- [[Request flow]]: what happens from "Nova, ..." to the answer
- Brains: [[Local brain]] · [[Fast path]] · [[Claude hand-off]] · [[Work it out]] · [[Background jobs]]
- Learning and remembering: [[Self-learning]] · [[Memory]]
- Hearing and speaking: [[Voice pipeline]] · [[Voice lock]]
- Doing things: [[Tools and safety]] · [[Browser control]] · [[Screen reading]] · [[Screen watch and windows]] · [[Spotify and media]]
- Faces: [[HUD window]] · [[Phone access]] · [[Tray and autostart]]
- Extras: [[Unique features]]

## Using Nova
- [[Start, stop, update]]
- [[Voice commands cheat sheet]]
- [[Routines]]
- [[Configuration and data]]
- [[Troubleshooting]]

## Development
- [[Lessons learned]]: mistakes not to repeat (the most important dev note)
- [[Sandbox and testing]]
- [[Working conventions]]

## Status
- [[Status and testing]]: what's confirmed on the PC and what isn't
- [[Roadmap]]: the menu of next features
- [[History]]: how Nova got here, phase by phase
