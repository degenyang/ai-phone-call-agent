# Security and data handling

What the bridge trusts, what it stores, and what its limits do and do not protect against. Setup steps are in [deployment.md](deployment.md).

## Security model

The bridge's websocket is public (Twilio has to reach it), so it trusts nothing that arrives on it:

- **Per-call URL, checked before the upgrade.** Twilio connects to `/media/<call_id>/<expires>/<token>`, where the token is an HMAC-SHA256 over `<call_id>.<expires>` made with `AICALL_SHARED_SECRET`. The bridge checks it in the HTTP request: a wrong, missing or expired token, a lifetime over an hour, an unknown call or a call id that was already used (`.claimed`) gets HTTP 403, so the connection never becomes a websocket and holds nothing. A replayed URL cannot start a second model session.
- **Paths.** The stream path must be exactly `/media/<call id>/<decimal expiry>/<64 lowercase hex>`, ASCII only, with no query string. Call ids must match `YYYYMMDD_HHMMSS_<12 hex>` and resolve to a direct child of `calls/`; traversal is rejected before any file access.
- **Malformed input.** Odd paths and headers get a 403; a socket whose first message is not a well-formed `start` event (with one consistent `streamSid`) is closed with 1008; malformed messages during a call are ignored. None of these raise or log a traceback.
- **Limits.** `AICALL_MAX_CONCURRENT_CALLS` (4 calls), `AICALL_MAX_CALL_SECONDS` (900; Gemini calls 540 at most), 64 KiB websocket messages, bounded recording buffers. When a call hits its duration limit, both audio pumps are cancelled and awaited before the transcript is written. Only `/media/…` and `/healthz` are served.
- **Twilio signature (optional).** With `AICALL_VERIFY_TWILIO_SIGNATURE=1`, the handshake's `X-Twilio-Signature` is also checked. Twilio signs the full per-call URL, so a signature only fits the one call it was made for. It is off by default because it depends on the exact URL Twilio signs (`PUBLIC_WSS_URL` plus the path); enable it after one successful test call. The cost: Twilio signs with the account's master auth token, so enabling it puts `TWILIO_AUTH_TOKEN` on the VPS. That token is account-wide (it can place calls, read every recording and change the account) and an API key cannot stand in for it here. Since the per-call token is already checked before the upgrade, the signature mainly proves the request came from Twilio; weigh that against keeping the master token off the VPS.
- **Service.** systemd runs the bridge as `aicall`, a user with no login shell and no home, with `NoNewPrivileges`, `ProtectSystem=strict` (writable: `/srv/aicall/calls` only), `ProtectProc=invisible` (it sees only its own processes), no capabilities, a syscall filter and memory/task caps. The code, the venv and `.env` in `/opt/aicall` are owned by root, so the bridge cannot rewrite what `Restart=always` runs next.
- **Secrets.** `.env` is `root:root 0600` on disk and read by systemd before it drops privileges, so the file is root-only. The values are not: the API keys are in the bridge process's environment, and any process running as `aicall` could read them from `/proc/<pid>/environ`. That is why nothing else runs as `aicall` and nobody can log in as it.
- **Upload account.** The driver's SSH key logs in as `aicall-upload`, which can only use SFTP, is jailed in `/srv/aicall` and shares the `aicall` group to upload prompts and fetch results. It cannot run commands, read `/proc` or `.env`, or touch the code. It can create symlinks inside `calls/`, so the bridge opens uploaded files with `O_NOFOLLOW` and refuses a symlinked `prompt.txt` or `config.json`, and writes its own results through a randomly named temporary file created with `O_EXCL|O_NOFOLLOW` and then renamed, so a planted `outcome.json.tmp` (or symlink) is never written through (it could otherwise point at the bridge's own `/proc/self/environ`).

## Recording, transcripts and retention

**Recording is off by default.** One switch, `--record` (or `AICALL_RECORD=1`, overridable with `--no-record`), controls all stored audio:

| | default | `--record` |
|---|---|---|
| Twilio recording (`Record=true`, dual channel) | not requested | requested; downloaded as `call.mp3` once `completed` |
| Bridge WAVs on the VPS (`callee_8k.wav`, `agent_24k.wav` / `agent_8k.wav`) | not written | written, capped at `AICALL_MAX_CALL_SECONDS` of audio |
| Recording disclosure | none | Twilio says "This call may be recorded." (TwiML `<Say>`) before the AI is connected, and the driver tells the agent to mention it in its first sentence |

The `<Say>` notice is fixed and in English, so it is said even if the model skips it. If the call is in another language, also put the notice in your prompt's opening line in that language.

**Transcripts are saved on every call**, separately from recording: `transcript.json` holds the models' own speech-to-text of both sides, on the VPS and in your local `calls/<call_id>/`, next to `outcome.json` (status and reason only). The transcript is personal data like the audio. Independently of both, the call audio is always streamed to the model provider (Google or OpenAI) and processed under their API data terms.

**Voicemail.** The example prompts leave only a minimal callback message (who is calling and a number to call back), never names, dates or booking details, because a mailbox may be shared or belong to the wrong person. Keep that rule in your own prompts.

**Twilio credential.** The driver sends its Twilio credential (preferably an API key, see the README) only to `api.twilio.com`. Recording downloads redirect to a storage host; the driver drops the `Authorization` header on any redirect to another host, which `urllib` would otherwise forward.

**Permissions.** On the VPS, call directories are `0770` and artifacts `0640` (the bridge runs with `UMask=0027`), group `aicall`, which holds only the service user and the upload account; nobody else can read them. The driver writes and keeps its local files as `0600` (on POSIX).

**Retention and deletion.** Nothing is deleted automatically; pick a retention period that matches what you tell callees. To delete one call everywhere:

```bash
rm -r calls/<call_id>                                              # local
sudo rm -r /srv/aicall/calls/<call_id>                             # VPS, as an admin
# Twilio: Console → Monitor → Call recordings → delete, or
curl -X DELETE -u "$TWILIO_ACCOUNT_SID:$TWILIO_AUTH_TOKEN" \
  "https://api.twilio.com/2010-04-01/Accounts/$TWILIO_ACCOUNT_SID/Recordings/<RE…>.json"
```

For automatic expiry on the VPS, a root crontab entry such as `0 3 * * * find /srv/aicall/calls -mindepth 1 -maxdepth 1 -type d -mtime +30 -exec rm -r {} +` removes calls older than 30 days.
