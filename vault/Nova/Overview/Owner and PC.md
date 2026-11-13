---
tags: [nova, overview, owner]
---
# Owner and PC

## Oliver (the owner)
- Not a developer. Prefers plain explanations, copy-paste PowerShell commands and screenshots.
- Tests on the real PC and reports back with screenshots and `data\logs\nova.log`.
- Cares a lot about **security and privacy**. Likes unique features and a polished feel.
- Notices (fairly) when Nova says it did something it didn't, so Nova checks everything.
- Plays **GTA RP (FiveM)** with voice chat running, so there's lots of other speech in the room. That's why the name is required and the [[Voice lock]] exists.
- Watches YouTube in **Firefox**. Uses Steam, Discord and Spotify Premium.
- Has Claude Pro, Ollama, Claude Code (signed in) and Spotify Premium. Budget: free or subscription only.

## The PC (`LAPTOP-VKHGAOUV`)
| Part | Detail |
|---|---|
| CPU | Intel i9-14900HX |
| GPU | RTX 5070 Laptop, 8 GB (Blackwell) |
| RAM | 32 GB |
| Screens | **Two** |
| OS | Windows 11 Home |
| Mic | Was a Fifine SC3; lately a Logitech PRO X Wireless headset. Windows renumbers devices, so save the mic by **name** (see [[Troubleshooting]]) |
| Speakers | Realtek, so echo cancellation matters |
| Code folder | `C:\Users\patto\Fartbot22` |
| This vault | `C:\Users\patto\ObsidianClaude\Nova` |

> [!warning] Updating
> Always update with `powershell -ExecutionPolicy Bypass -File scripts\update.ps1`. **Never run `pip install` directly**: it puts the CPU voice engine back and the voice slows to ~650 ms. See [[Start, stop, update]].
