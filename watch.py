#!/usr/bin/env python3
"""
Play Store 24/7 watcher.

Polls Google Play listing pages for a set of packages, reads the "Updated on"
date, and sends a Telegram message the moment that date moves past the recorded
baseline (i.e. an uploaded update has passed review and gone live).

Stdlib only — runs on the system python3 (/usr/bin/python3). No pip installs.

Usage:
  watch.py                 run forever (daemon loop) — used by launchd
  watch.py --once          do a single check of every package and exit
  watch.py --resolve-chat  print chat ids that have messaged your bot (getUpdates)
  watch.py --test-telegram send a test message to the configured chat
"""
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
STATE_PATH = os.path.join(HERE, "state.json")
LOG_PATH = os.path.join(HERE, "watch.log")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

DATE_RE = re.compile(r"\b([A-Z][a-z]{2,8}) (\d{1,2}), (20\d{2})\b")


def log(msg):
    line = "%s  %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.getcode(), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        # 404 = app not on store yet; return the code instead of raising.
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:  # noqa
            pass
        return e.code, body


def parse_dates(html):
    """Return list of (datetime, 'Mon D, YYYY') tuples found on the page."""
    out = []
    for m in DATE_RE.finditer(html):
        token = "%s %s, %s" % (m.group(1), m.group(2), m.group(3))
        for fmt in ("%B %d, %Y", "%b %d, %Y"):
            try:
                out.append((datetime.strptime(token, fmt), token))
                break
            except ValueError:
                continue
    return out


def latest_updated(html):
    """Best-effort 'Updated on' date = the latest real date on the listing page."""
    dates = parse_dates(html)
    if not dates:
        return None, None
    dt, token = max(dates, key=lambda x: x[0])
    return dt, token


def tg_creds(cfg):
    """Telegram creds — env vars (GitHub Secrets) win over config.json."""
    tg = cfg.get("telegram", {})
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or tg.get("bot_token", "")).strip()
    chat_id = str(os.environ.get("TELEGRAM_CHAT_ID") or tg.get("chat_id", "")).strip()
    return token, chat_id


def _tg_api(token, method, params, timeout=30):
    url = "https://api.telegram.org/bot%s/%s" % (token, method)
    data = urllib.parse.urlencode(params).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=timeout) as r:
            return r.getcode(), json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:  # noqa
        log("Telegram %s error: %s" % (method, e))
        return None, None


def recipients(cfg, state):
    """Everyone who should get alerts: the seed chat (secret) + all subscribers."""
    token, seed = tg_creds(cfg)
    ids = []
    for c in [seed] + list(state.get("_subscribers", [])):
        c = str(c).strip()
        if c and c not in ids:
            ids.append(c)
    return token, ids


def telegram_send(cfg, state, text):
    """Broadcast a message to every recipient."""
    token, ids = recipients(cfg, state)
    if not token or not ids:
        log("TELEGRAM no recipients — message NOT sent: " + text)
        return False
    ok_any = False
    for cid in ids:
        code, _ = _tg_api(token, "sendMessage",
                          {"chat_id": cid, "text": text, "disable_web_page_preview": "true"})
        ok_any = ok_any or (code == 200)
    return ok_any


WELCOME = ("\U0001F44B Play Watcher\n"
           "You're subscribed — I'll message you the moment a tracked app goes "
           "live or gets an update on Google Play.\n\n"
           "/status — status of every tracked app\n"
           "/help — show commands")

HELP = ("Play Watcher commands:\n"
        "/status — status of every tracked app (live / waiting)\n"
        "/help — this message\n\n"
        "Alerts arrive here automatically when a tracked app goes live or updates.")


KEYBOARD = json.dumps({
    "keyboard": [["\U0001F4E1 Status", "❓ Help"]],
    "resize_keyboard": True,
})


def _send(token, cid, text, keyboard=True):
    params = {"chat_id": cid, "text": text, "disable_web_page_preview": "true"}
    if keyboard:
        params["reply_markup"] = KEYBOARD
    _tg_api(token, "sendMessage", params)


def _status_text(cfg, state):
    """A full list of every tracked app with its live/waiting status."""
    pkgs = packages(cfg)
    live_lines, wait_lines = [], []
    for p in pkgs:
        st = state.get(p["id"], {})
        label = p.get("label", p["id"])
        kind = " (%s)" % p["kind"] if p.get("kind") else ""
        if st.get("baseline_live") or st.get("alerted"):
            date = st.get("last_seen") or st.get("baseline_date") or ""
            live_lines.append("\U0001F7E2 %s%s%s" % (label, kind, (" — " + date) if date else ""))
        else:
            wait_lines.append("⏳ %s%s" % (label, kind))
    n = len(pkgs)
    out = ["\U0001F4E1 Play Watcher — %d apps (%d live, %d waiting)" %
           (n, len(live_lines), len(wait_lines)), ""]
    out += live_lines + wait_lines
    return "\n".join(out)


def handle_updates(cfg, state, long_poll=False):
    """Process incoming bot messages: auto-subscribe senders + answer commands
    and keyboard buttons (/start, Status, List, Help). long_poll=True makes it
    wait up to ~25s for a message so replies are near-instant. Does NOT touch
    the alerting flow. Logs nothing identifying."""
    token, _ = tg_creds(cfg)
    if not token:
        return
    offset = state.get("_tg_offset", 0)
    params = {"timeout": 25 if long_poll else 0}
    if offset:
        params["offset"] = offset + 1
    code, data = _tg_api(token, "getUpdates", params, timeout=(40 if long_poll else 30))
    if not data or not data.get("ok"):
        return
    subs = state.setdefault("_subscribers", [])
    subs_str = [str(s) for s in subs]
    last = offset
    for upd in data.get("result", []):
        last = max(last, upd.get("update_id", last))
        msg = upd.get("message") or upd.get("channel_post") or {}
        chat = msg.get("chat") or {}
        cid = chat.get("id")
        if cid is None:
            continue
        cid = str(cid)
        if cid not in subs_str:
            subs.append(cid)
            subs_str.append(cid)
            log("new subscriber added")  # no id/name in the public log
        t = (msg.get("text") or "").strip().lower()
        if t.startswith("/start") or t == "start":
            _send(token, cid, WELCOME)
        elif "status" in t:
            body = _status_text(cfg, state)
            for i in range(0, len(body), 3500):
                _send(token, cid, body[i:i + 3500])
        elif "help" in t:
            _send(token, cid, HELP)
        elif t:
            _send(token, cid, "Tap a button below \U0001F447", keyboard=True)
    if last != offset:
        state["_tg_offset"] = last


def resolve_chat(cfg):
    token, _ = tg_creds(cfg)
    if not token:
        print("Set telegram.bot_token in config.json first.")
        return
    code, body = http_get("https://api.telegram.org/bot%s/getUpdates" % token)
    data = json.loads(body)
    seen = {}
    for upd in data.get("result", []):
        msg = upd.get("message") or upd.get("channel_post") or {}
        chat = msg.get("chat") or {}
        if "id" in chat:
            seen[chat["id"]] = chat.get("title") or (
                (chat.get("first_name", "") + " " + chat.get("last_name", "")).strip()
                or chat.get("username", "") or chat.get("type", ""))
    if not seen:
        print("No chats found. Open Telegram, send your bot any message, then re-run --resolve-chat.")
        return
    print("Chats that have messaged the bot:")
    for cid, name in seen.items():
        print("  chat_id=%s   (%s)" % (cid, name))


def parse_token(token):
    if not token:
        return None
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(token, fmt)
        except ValueError:
            continue
    return None


def listing_state(code, html):
    """Return (is_live, updated_token). is_live is None when transient/unknown."""
    if code == 404:
        return False, None
    if code != 200:
        return None, None
    m = re.search(r'<meta property="og:title"[^>]*content="([^"]*)"', html or "")
    title = m.group(1) if m else ""
    live = "Google Play" in title
    _, token = latest_updated(html)
    return live, token


def packages(cfg):
    """Watch list comes from the WATCH_PACKAGES env (GitHub Secret, JSON) so the
    app/package list is NEVER in the public repo. Falls back to config.json.
    Apps already marked done (dropped after go-live) are filtered out."""
    raw = os.environ.get("WATCH_PACKAGES", "").strip()
    lst = None
    if raw:
        try:
            lst = json.loads(raw)
        except ValueError:
            log("WATCH_PACKAGES is not valid JSON — falling back to config.json")
    if lst is None:
        lst = cfg.get("packages", [])
    return lst


def remove_from_watchlist(pid):
    """On go-live, drop this package from the WATCH_PACKAGES secret via the GitHub
    API, so it stops being tracked (auto-cleanup). Uses the token already present
    for re-dispatch (GH_SECRETS_TOKEN) — NO Jira creds involved. Best-effort:
    no-ops locally / if PyNaCl or the token is missing."""
    token = os.environ.get("GH_SECRETS_TOKEN", "").strip()
    repo = os.environ.get("GH_REPO", "").strip()
    raw = os.environ.get("WATCH_PACKAGES", "").strip()
    if not token or not repo or not raw:
        return False
    try:
        lst = json.loads(raw)
    except ValueError:
        return False
    new = [p for p in lst if p.get("id") != pid]
    if len(new) == len(lst):
        return False  # wasn't in the secret list
    try:
        import base64
        from nacl import encoding, public  # present on the Actions runner
    except Exception:  # noqa
        return False

    def gh(path, method="GET", data=None):
        req = urllib.request.Request("https://api.github.com/repos/%s%s" % (repo, path), method=method)
        req.add_header("Authorization", "token " + token)
        req.add_header("Accept", "application/vnd.github+json")
        body = json.dumps(data).encode() if data is not None else None
        if body:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, body, timeout=30) as r:
            out = r.read().decode("utf-8", "replace")
            return json.loads(out) if out.strip() else {}

    try:
        pk = gh("/actions/secrets/public-key")
        box = public.SealedBox(public.PublicKey(pk["key"].encode(), encoding.Base64Encoder()))
        sealed = box.encrypt(json.dumps(new, ensure_ascii=False).encode())
        gh("/actions/secrets/WATCH_PACKAGES", "PUT",
           {"encrypted_value": base64.b64encode(sealed).decode(), "key_id": pk["key_id"]})
        log("dropped a package from the watch list on go-live (%d left)" % len(new))
        return True
    except Exception as e:  # noqa
        log("watch-list removal failed: %s" % e)
        return False


def check_package(cfg, state, pkg):
    """Returns a status string only — NEVER logs the package id or label, so app
    identities stay out of the (public) Actions logs. The label/url appear solely
    in the private Telegram message."""
    pid = pkg["id"]
    label = pkg.get("label", pid)
    url = "https://play.google.com/store/apps/details?id=%s&hl=en&gl=US" % pid
    try:
        code, html = http_get(url)
    except Exception:  # noqa
        return "transient"

    live, token = listing_state(code, html)
    if live is None:
        return "transient"

    st = state.setdefault(pid, {})

    # First observation → record baseline, never alert on it.
    if "baseline_live" not in st:
        st["baseline_live"] = live
        st["baseline_date"] = token
        st["baseline_set_at"] = datetime.now().isoformat(timespec="seconds")
        st["last_checked"] = st["baseline_set_at"]
        return "baseline"

    st["last_seen"] = token
    st["last_checked"] = datetime.now().isoformat(timespec="seconds")
    already = st.get("alerted")
    msg = None

    base_live = st.get("baseline_live")
    base_dt = parse_token(st.get("baseline_date"))
    now_dt = parse_token(token)

    head = label + ((" (%s)" % pkg["kind"]) if pkg.get("kind") else "")
    play = "https://play.google.com/store/apps/details?id=%s" % pid
    jira = pkg.get("jira_url", "")
    if not base_live and live:
        msg = ("\U0001F7E2 %s — is now LIVE on Google Play\n"
               "Updated on: %s\n"
               "Google Play url:\n%s\n"
               "Jira url:\n%s" % (head, token or "?", play, jira))
    elif base_live and live and base_dt and now_dt and now_dt > base_dt:
        msg = ("\U0001F7E2 %s — update is LIVE on Google Play\n"
               "Updated on: %s (was %s)\n"
               "Google Play url:\n%s\n"
               "Jira url:\n%s" % (head, token, st.get("baseline_date"), play, jira))

    went_live = (not base_live) and live
    if msg and not already:
        st["alerted"] = True
        sent = telegram_send(cfg, state, msg)
        # First go-live → drop it from the WATCH_PACKAGES secret so it stops being
        # tracked (auto-cleanup via the re-dispatch token — no Jira creds).
        if went_live and not st.get("dropped"):
            if remove_from_watchlist(pid):
                st["dropped"] = True
        return "alert" if sent else "alert_failed"
    return "live" if live else "pending"


def run_once(cfg, state):
    # Broadcast: alerts go to the seed chat (TELEGRAM_CHAT_ID) + anyone who
    # pressed Start (collected here, stored in the cached state — never in the
    # repo). Logs are AGGREGATE counts only — no app names/ids to stdout.
    handle_updates(cfg, state)
    tally = {}
    pkgs = packages(cfg)
    for pkg in pkgs:
        s = check_package(cfg, state, pkg)
        tally[s] = tally.get(s, 0) + 1
    save_json(STATE_PATH, state)
    summary = ", ".join("%s=%d" % (k, tally[k]) for k in sorted(tally)) or "none"
    log("cycle: %d packages (%s)" % (len(pkgs), summary))


def bot_loop(cfg, state, max_seconds):
    """Run for ~max_seconds: answer bot commands/buttons near-instantly
    (long-poll) AND check Play every interval_seconds. Keeps 24/7 alerting at
    the same cadence while making commands responsive."""
    interval = int(cfg.get("interval_seconds", 300))
    log("bot started — %d packages, Play check every %ds" % (len(packages(cfg)), interval))
    end = time.time() + max_seconds
    last_play = 0.0
    while time.time() < end:
        try:
            handle_updates(cfg, state, long_poll=True)   # ~25s, instant replies
        except Exception as e:  # noqa
            log("updates error: %s" % e)
            time.sleep(3)
        if time.time() - last_play >= interval:
            tally = {}
            pkgs = packages(cfg)
            for pkg in pkgs:
                s = check_package(cfg, state, pkg)
                tally[s] = tally.get(s, 0) + 1
            last_play = time.time()
            summary = ", ".join("%s=%d" % (k, tally[k]) for k in sorted(tally)) or "none"
            log("cycle: %d packages (%s)" % (len(pkgs), summary))
        save_json(STATE_PATH, state)


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    cfg = load_json(CONFIG_PATH, None)
    if cfg is None:
        log("Missing/invalid config.json at %s" % CONFIG_PATH)
        sys.exit(1)

    if arg == "--resolve-chat":
        resolve_chat(cfg)
        return

    state = load_json(STATE_PATH, {})

    if arg == "--test-telegram":
        ok = telegram_send(cfg, state, "✅ Play watcher test — wiring works.")
        print("sent" if ok else "FAILED (check bot_token/chat_id)")
        return

    if arg == "--once":
        run_once(cfg, state)
        return

    if arg == "--bot":
        bot_loop(cfg, state, int(os.environ.get("LOOP_MAX_SECONDS", "19800")))
        save_json(STATE_PATH, state)
        return

    interval = int(cfg.get("interval_seconds", 300))
    log("Play watcher started — %d package(s), interval %ds" %
        (len(packages(cfg)), interval))
    while True:
        try:
            run_once(cfg, state)
        except Exception as e:  # noqa
            log("loop error: %s" % e)
        time.sleep(interval)


if __name__ == "__main__":
    main()
