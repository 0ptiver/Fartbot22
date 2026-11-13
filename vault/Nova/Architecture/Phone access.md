---
tags: [nova, architecture, phone, security]
---
# Phone access

`assistant/remote/`: talk to Nova from the phone like at the desk. Confirmed working by Oliver.

- **Network:** Tailscale only (private network between your own devices). Nova binds to the Tailscale IP and follows Tailscale going up and down. No port forwarding, ever.
- **Sign-in** (`auth.py`): a phone password (scrypt) + Nova's own TOTP 2FA (authenticator app). Secrets in Windows Credential Manager (`PHONE_AUTH`). Per-device tokens, each revocable. 5 wrong tries lock it for 15 min and Nova tells you. A new device signing in is announced out loud.
- **Voice** (`remote/voice.py`): tap the mic → the PC's Whisper → the same brain → Kokoro WAV played on the phone. Needs HTTPS: in the Tailscale admin turn on **MagicDNS** and **HTTPS Certificates**; Nova gets a cert for its own name.
- **PC control from the phone:** `phone.pc_control: ask` (Oliver's pick) → Yes/No on the phone. `REMOTE_NEVER` (terminal, voice lock, dictation, teach) stays blocked. `off` blocks all PC control.
- Setup: the window's **Phone** page (password, QR code, address like `https://laptop-name.tailxxxx.ts.net:8767`), then "Add to Home Screen".
- The phone app shows Nova's orb; tap to talk.

> [!note] Doctor
> "Tailscale isn't connected on this PC" means: open Tailscale and sign in. Only matters for the phone.
