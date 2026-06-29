# Play Store 24/7 Watcher

Standalone background service. Polls Google Play listing pages and sends a
**Telegram** message the moment a package's "Updated on" date moves past its
baseline (i.e. an uploaded update passed review and went live). Runs via
`launchd` — independent of Claude / any app being open.

Stdlib-only Python, runs on `/usr/bin/python3`. No pip installs.

## Files
- `watch.py` — the watcher (daemon loop + `--once`, `--resolve-chat`, `--test-telegram`).
- `config.json` — Telegram creds, poll interval, list of packages + baselines.
- `com.workspace.playwatcher.plist` — launchd job (RunAtLoad + KeepAlive).
- `state.json` — runtime state (last-seen date per package). Auto-managed.
- `watch.log` / `watch.out.log` / `watch.err.log` — logs.

## Setup (one time)

1. **Create a bot:** in Telegram open **@BotFather** → `/newbot` → copy the token
   (looks like `123456789:AAE...`).
2. **Put the token** into `config.json` → `telegram.bot_token`.
3. **Find your chat id:** send your new bot any message in Telegram, then:
   ```bash
   /usr/bin/python3 watch.py --resolve-chat
   ```
   Copy the printed `chat_id` into `config.json` → `telegram.chat_id`.
4. **Test the wiring:**
   ```bash
   /usr/bin/python3 watch.py --test-telegram   # you should get a Telegram message
   ```

## Run 24/7 (launchd)
```bash
cp com.workspace.playwatcher.plist ~/Library/LaunchAgents/
launchctl load  ~/Library/LaunchAgents/com.workspace.playwatcher.plist   # start
launchctl list | grep playwatcher                                        # verify
```
Stop / restart:
```bash
launchctl unload ~/Library/LaunchAgents/com.workspace.playwatcher.plist  # stop
launchctl unload ~/Library/LaunchAgents/com.workspace.playwatcher.plist && \
launchctl load   ~/Library/LaunchAgents/com.workspace.playwatcher.plist  # reload after config edit
```

> **24/7 caveat:** the job runs whenever the Mac is awake. While the Mac is
> asleep it pauses and resumes on wake. For literal always-on while the lid is
> closed, run it on an always-on machine or wrap with `caffeinate`.

## Add / change packages
Edit `config.json` → `packages[]`:
```json
{ "id": "com.foo.bar123", "label": "My App", "baseline_updated": "Jun 1, 2026" }
```
`baseline_updated` = the "Updated on" date currently shown on the listing. The
watcher alerts once when the live date becomes later than this. Reload launchd
after editing.

`interval_seconds` controls poll frequency (default 300 = every 5 min).
