# JARVIS cloud notifier: runs on GitHub Actions every ~5 minutes, sends Telegram alerts
# (earthquakes, geomagnetic storms, filtered news). Standard library only.
# Secrets come from environment variables, never from this file.
import os, sys, json, math, re, time, datetime, urllib.request, urllib.parse
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "state.json")
UA = {"User-Agent": "Mozilla/5.0"}


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def fetch(url, timeout=15):
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout).read()


def km(a, b, c, d):
    t = math.radians
    h = math.sin(t(c - a) / 2) ** 2 + math.cos(t(a)) * math.cos(t(c)) * math.sin(t(d - b) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


def in_quiet(cfg):
    try:
        from zoneinfo import ZoneInfo
        now = datetime.datetime.now(ZoneInfo(cfg.get("timezone", "Asia/Jerusalem")))
    except Exception:
        now = datetime.datetime.utcnow() + datetime.timedelta(hours=2)
    m = now.hour * 60 + now.minute
    f = lambda s: int(s[:2]) * 60 + int(s[3:5])
    a, b = f(cfg["quiet_from"]), f(cfg["quiet_to"])
    return (a <= m < b) if a <= b else (m >= a or m < b)


def make_sender(cfg):
    token, chat = os.environ.get("TELEGRAM_TOKEN", "").strip(), os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        print("Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID secrets.")
        return None
    quiet = in_quiet(cfg)

    def send(text):
        body = json.dumps({"chat_id": chat, "text": text[:900], "disable_notification": quiet,
                           "disable_web_page_preview": True}).encode()
        req = urllib.request.Request("https://api.telegram.org/bot%s/sendMessage" % token, data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=15).read()
            return True
        except Exception as e:
            print("send failed:", type(e).__name__)
            return False
    return send


def check_quakes(cfg, seen, baseline, send):
    sent = 0
    d = json.loads(fetch("https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_day.geojson"))
    for f in d.get("features", []):
        qid = "q:" + f.get("id", "")
        if qid in seen:
            continue
        seen.add(qid)
        if baseline:
            continue
        p, g = f["properties"], f["geometry"]["coordinates"]
        mag = p.get("mag") or 0
        dist = km(cfg["center_lat"], cfg["center_lon"], g[1], g[0])
        if (mag >= cfg["quake_min_mag"] and dist <= cfg["quake_radius_km"]) or mag >= cfg["quake_anywhere_mag"]:
            if send("\U0001F30D Earthquake M%.1f\n%s\n%d km from your area\n%s" % (mag, p.get("place", ""), dist, p.get("url", ""))):
                sent += 1
    return sent


def check_solar(cfg, seen, baseline, send):
    rows = json.loads(fetch("https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json"))
    if len(rows) < 2:
        return 0
    last = rows[-1]
    key = "k:" + str(last[0])
    if key in seen:
        return 0
    seen.add(key)
    try:
        kp = float(last[1])
    except Exception:
        return 0
    if not baseline and kp >= cfg["kp_alert"]:
        return 1 if send("☀️ Geomagnetic storm: Kp index %.1f (G%d)" % (kp, max(1, int(kp) - 4))) else 0
    return 0


def check_news(cfg, state, seen, baseline, send):
    now = time.time()
    state["news_sent"] = [t for t in state.get("news_sent", []) if now - t < 3600]
    kws = [k.lower() for k in cfg["news_keywords"]]
    sent = 0
    for q in cfg["news_queries"]:
        try:
            lang = "hl=he&gl=IL&ceid=IL:he" if re.search(r"[֐-׿]", q) else "hl=en-US&gl=US&ceid=US:en"
            url = "https://news.google.com/rss/search?q=%s&%s" % (urllib.parse.quote(q + " when:2h"), lang)
            root = ET.fromstring(fetch(url))
        except Exception as e:
            print("news fetch failed:", q, type(e).__name__)
            continue
        for it in root.iter("item"):
            title = (it.findtext("title") or "").strip()
            link = (it.findtext("link") or "").strip()
            src = (it.findtext("source") or "").strip()
            key = "n:" + (link or title)
            if not title or key in seen:
                continue
            seen.add(key)
            try:
                dt = parsedate_to_datetime(it.findtext("pubDate") or "")
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=datetime.timezone.utc)
                age = now - dt.timestamp()
            except Exception:
                age = 1e9
            if baseline or age > 7200:
                continue
            if any(k in title.lower() for k in kws) and sent < cfg["news_per_run"] and len(state["news_sent"]) < cfg["news_per_hour"]:
                if send("\U0001F4F0 %s%s\n%s" % (title, (" - " + src) if src else "", link)):
                    state["news_sent"].append(now)
                    sent += 1
    return sent


def main(test=False):
    cfg = load_json(os.path.join(HERE, "config.json"), None)
    if not cfg:
        print("config.json missing")
        return
    send = make_sender(cfg)
    if not send:
        return
    if test:
        send("Cloud JARVIS is connected, sir. I will watch quakes, geomagnetic storms and news while your PC is off.")
        print("test message sent")
        return
    state = load_json(STATE, {})
    baseline = not state.get("init")
    seen = set(state.get("seen", []))
    total = 0
    for name, fn in (("quakes", lambda: check_quakes(cfg, seen, baseline, send)),
                     ("solar", lambda: check_solar(cfg, seen, baseline, send)),
                     ("news", lambda: check_news(cfg, state, seen, baseline, send))):
        try:
            total += fn()
        except Exception as e:
            print(name, "failed:", type(e).__name__)
    state["init"] = True
    state["seen"] = list(seen)[-1500:]
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(state, f)
    print("baseline run" if baseline else "sent %d" % total)


if __name__ == "__main__":
    try:
        main(test=("--test" in sys.argv) or os.environ.get("TEST_MESSAGE") == "true")
    except Exception as e:
        print("error:", type(e).__name__)
    sys.exit(0)
