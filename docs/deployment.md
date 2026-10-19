# Deployment

Step-by-step setup of the Twilio account, the VPS bridge and the SFTP upload account the local driver needs. The [README](../README.md#quick-start) has the short version; security and data handling are in [security.md](security.md).

Commands are for a Debian/Ubuntu VPS and a Linux or macOS machine for the driver. On Windows, run the driver with `python` (or `py`) instead of `python3`.

## 1. Twilio

1. Upgrade the account and finish any regulatory / KYC profile Twilio asks for. Outbound calls may be blocked until it is approved.
2. Buy a voice-capable number (`TWILIO_FROM_NUMBER`). Optionally verify your own mobile as an outgoing caller ID (`TWILIO_VERIFIED_CALLER_ID`).
3. In *Voice → Settings → Geo permissions*, enable the countries you will call.

## 2. Shared secret

Generate one secret and put the same value in the local `.env` and the VPS `.env` (`AICALL_SHARED_SECRET`):

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

## 3. VPS bridge

The layout keeps everything the bridge runs root-owned and gives it exactly one writable directory:

| Path | Owner, mode | Holds |
|---|---|---|
| `/opt/aicall/` | `root:root 755` | `server.py` (644), the venv, `.env` (`root:root 600`) |
| `/srv/aicall/` | `root:root 755` | the data root; also the upload account's chroot (step 5) |
| `/srv/aicall/calls/` | `aicall:aicall 2770` | one directory per call; the only path the service can write |

The service user `aicall` has no login shell and no home. A compromised bridge can therefore not replace its own code for the next `Restart=always`, and nobody logs in as the user whose process holds the API keys.

Run as root on the VPS:

```bash
apt update && apt install -y python3 python3-venv git caddy   # or install Caddy from caddyserver.com/docs/install
git clone https://github.com/degenyang/ai-phone-call-agent.git /root/aicall-src

# service account: no shell, no home, its own group
useradd --system --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin aicall

# code, venv and secrets: root-owned, read-only for the service
install -o root -g root -m 755 -d /opt/aicall
install -o root -g root -m 644 /root/aicall-src/server/server.py /opt/aicall/server.py
python3 -m venv /opt/aicall/venv
/opt/aicall/venv/bin/pip install -r /root/aicall-src/requirements.txt
# secrets: root-only on disk, read by systemd before it drops to the aicall user
install -o root -g root -m 600 /root/aicall-src/.env.example /opt/aicall/.env
nano /opt/aicall/.env    # AICALL_DIR=/srv/aicall, AICALL_SHARED_SECRET, GEMINI_API_KEY, OPENAI_API_KEY (gpt-* only)

# call data: the only writable directory (setgid, so new entries stay in group aicall)
install -o root -g root -m 755 -d /srv/aicall
install -o aicall -g aicall -m 2770 -d /srv/aicall/calls

install -m 644 /root/aicall-src/deploy/aicall.service /etc/systemd/system/aicall.service
systemctl daemon-reload && systemctl enable --now aicall
systemctl status aicall                      # "listening 127.0.0.1:8765"
curl -s http://127.0.0.1:8765/healthz        # ok
```

To update later: `git -C /root/aicall-src pull`, repeat the `install ... server.py` and `pip install` lines (as root), then `systemctl restart aicall`.

## 4. Caddy and firewall

Copy `deploy/Caddyfile` to `/etc/caddy/Caddyfile`, replace `your-domain.example` with your hostname, then `systemctl reload caddy`. Caddy fetches a Let's Encrypt certificate on its own. Open only 22 (SSH), 80 (certificate challenge) and 443 (wss) in your firewall, e.g. `ufw allow 22,80,443/tcp && ufw enable`. Port 8765 stays on localhost. Check from outside:

```bash
curl -s https://your-domain.example/healthz   # ok
```

Your `PUBLIC_WSS_URL` is `wss://your-domain.example/media`. The driver appends `/<call_id>/<expires>/<token>` for each call, which is why Caddy forwards `/media/*`.

## 5. Upload account for the driver

The driver never logs in as the service user. It uses a separate account, `aicall-upload`, that can only speak SFTP, is jailed in `/srv/aicall` (where it sees `calls/` and nothing else) and has no password. It shares the `aicall` group, which is how the bridge can read the prompts it uploads and it can fetch the results the bridge writes (files `0640`, call directories `0770`). It cannot open a shell, read `/proc`, read `.env` or touch the code.

On the VPS, as root:

```bash
# home "/" is the jail's root; password "*" means none, without the "locked" marker sshd refuses
useradd --system --gid aicall --no-create-home --home-dir / --shell /usr/sbin/nologin aicall-upload
usermod -p '*' aicall-upload

# the driver's public key, kept outside the jail; "restrict" turns off forwarding, PTYs and the like
install -o root -g root -m 755 -d /etc/ssh/authorized_keys
echo "restrict ssh-ed25519 AAAA... aicall-driver" > /etc/ssh/authorized_keys/aicall-upload
chmod 644 /etc/ssh/authorized_keys/aicall-upload

cat > /etc/ssh/sshd_config.d/10-aicall.conf <<'EOF'
# keys only, for every account: make sure your own admin login uses a key before reloading
PasswordAuthentication no
KbdInteractiveAuthentication no

Match User aicall-upload
    AuthorizedKeysFile /etc/ssh/authorized_keys/%u
    ChrootDirectory /srv/aicall
    ForceCommand internal-sftp -u 0007
    AllowTcpForwarding no
    X11Forwarding no
    PermitTTY no
EOF
sshd -t && systemctl reload ssh
```

The file is named `10-…` so it is read before distribution defaults such as `50-cloud-init.conf`; for these options the first value sshd reads wins. `ChrootDirectory` requires `/srv/aicall` and every directory above it to be root-owned and not group- or world-writable, which the layout in step 3 gives.

On your machine:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/aicall -N ""   # paste ~/.ssh/aicall.pub into the echo line above
# add to ~/.ssh/config:
#   Host your-domain.example
#     User aicall-upload
#     IdentityFile ~/.ssh/aicall
#     IdentitiesOnly yes
```

In the local `.env`, set `AICALL_VPS_SSH=aicall-upload@your-domain.example` and `AICALL_REMOTE_DIR=/` (the jail's root, so calls go to `/calls/<call_id>`). The driver runs one `sftp -b -` batch per transfer: `mkdir`, `put` and `chmod` to upload, `get` to fetch. It never runs a remote command.

`sftp` runs with `BatchMode=yes`, which cannot answer the "unknown host key" prompt, so the VPS's host key must already be trusted. Make the first connection interactively and check the fingerprint against the server's own:

```bash
# on the VPS: print the host key fingerprints
for k in /etc/ssh/ssh_host_*_key.pub; do ssh-keygen -lf "$k"; done
# on your machine: connect once interactively; answer "yes" only if the fingerprint shown matches, then type "bye"
sftp aicall-upload@your-domain.example
# now the non-interactive check the driver relies on must print "Remote working directory: /"
echo pwd | sftp -b - -o BatchMode=yes aicall-upload@your-domain.example
```

If the VPS is ever rebuilt, its host key changes: remove the old entry with `ssh-keygen -R your-domain.example` and repeat the fingerprint check.

## 6. Local driver

Continue with [Quick start → Local driver](../README.md#3-local-driver) in the README.

## Settings reference

Every setting is an environment variable; `.env.example` lists them with comments. The ones that must match on both sides are `AICALL_SHARED_SECRET` and `AICALL_MAX_CALL_SECONDS`. `AICALL_MAX_CONCURRENT_CALLS` and `AICALL_VERIFY_TWILIO_SIGNATURE` only matter on the VPS; see [security.md](security.md).

`config.json` on the server (written per call by the driver) also accepts `"vad": false` (Gemini's default voice-activity detection) and `"kickoff"` (the hidden first message that tells the model the line is open).

## Testing the bridge without a phone call

`tools/fake_twilio.py` speaks the Twilio Media Streams protocol against your deployed bridge: it connects to the same per-call URL Twilio would get, plays three callee clips, waits for the AI to answer, echoes `mark` events and reports how many audio frames the AI sent. With `TWILIO_AUTH_TOKEN` set it also signs the request like Twilio, so it works with `AICALL_VERIFY_TWILIO_SIGNATURE=1`. No Twilio call is placed; the model provider still bills its usage.

Run it from the local virtual environment described under [Testing](../README.md#testing) (it needs `websockets` and, on Python 3.13+, `audioop-lts`), with [FFmpeg](https://ffmpeg.org) on your PATH to convert the clips:

```bash
# 1. three callee clips as raw PCM16 mono 16 kHz (edge-tts or any recording works)
mkdir -p clips
ffmpeg -i u1.mp3 -f s16le -ac 1 -ar 16000 clips/u1.pcm   # same for u2, u3
# 2. a prepared call on the server (a call id can be used once), uploaded the way the driver does it
ID=$(python3 -c "import aicall; print(aicall.new_call_id())")
mkdir -p sim && cp examples/demo_prompt_en.txt sim/prompt.txt && echo '{"voice": "Kore"}' > sim/config.json
python3 -c "import aicall, pathlib; aicall.upload_call_files(aicall.Config.from_env(), '$ID', pathlib.Path('sim'))"
# 3. run it (uses PUBLIC_WSS_URL, AICALL_SHARED_SECRET and, if set, TWILIO_AUTH_TOKEN from .env)
python3 tools/fake_twilio.py $ID ./clips
```
