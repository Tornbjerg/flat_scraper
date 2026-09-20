#!/usr/bin/env python3
"""Poll Copenhagen rental sites, ntfy me when a new matching flat appears."""
import datetime, json, os, sys, zoneinfo
import requests
from bs4 import BeautifulSoup

MIN_SQM = 50
# Copenhagen + Frederiksberg postcodes (2700 Brønshøj / 2720 Vanløse are Kbh kommune too)
SKIP_ZIPS = {2300, 2450, 2500}  # København S, SV, Valby — not wanted
def in_area(z): return z not in SKIP_ZIPS and (1000 <= z <= 2500 or z in (2700, 2720))

RENT_ALERT = 14000  # under this: notify at once. at or above: end-of-day digest.

NTFY = os.environ.get("NTFY_TOPIC", "")  # set in the environment; never commit the topic
HERE = os.path.dirname(os.path.abspath(__file__))
TZ = zoneinfo.ZoneInfo("Europe/Copenhagen")   # all wall-clock rules below are local, DST and all
DIGEST_AFTER = 20      # send the daily summary from 20:00, before the snooze starts
SNOOZE = (21, 5)       # no scraping 21:00-05:00: the sites do not publish overnight
STATE, STAMP, QUEUE = "seen.json", "last_digest.txt", "pending.json"
# Blob when FLAT_BLOB_CONN is set (Azure), plain files otherwise (local, tests).
BLOB_CONN = os.environ.get("FLAT_BLOB_CONN", "")
CONTAINER = "state"


def _blob(name):
    from azure.storage.blob import BlobClient  # lazy: local runs never need the SDK
    return BlobClient.from_connection_string(BLOB_CONN, CONTAINER, name)


def read_state(name):
    if BLOB_CONN:
        from azure.core.exceptions import ResourceNotFoundError
        try:
            return _blob(name).download_blob().readall().decode()
        except ResourceNotFoundError:
            return None
    p = os.path.join(HERE, name)
    return open(p).read() if os.path.exists(p) else None


def write_state(name, text):
    if BLOB_CONN:
        _blob(name).upload_blob(text.encode(), overwrite=True)
    else:
        open(os.path.join(HERE, name), "w").write(text)


def delete_state(name):
    if BLOB_CONN:
        from azure.core.exceptions import ResourceNotFoundError
        try:
            _blob(name).delete_blob()
        except ResourceNotFoundError:
            pass
    else:
        p = os.path.join(HERE, name)
        if os.path.exists(p):
            os.remove(p)
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) flat-watch"}


def kereby():
    html = requests.get("https://kereby.dk/bolig/", headers=UA, timeout=30).text
    for c in BeautifulSoup(html, "html.parser").select("article.jorato-case-card"):
        if c.get("data-state") != "available":
            continue
        a = c.find("a", href=True)
        yield {
            "id": "kereby:" + c["data-card-id"],
            "title": c.select_one(".jorato-case-card__headline").get_text(strip=True),
            "addr": c.select_one(".jorato-case-card__location-text").get_text(strip=True),
            "zip": int(c["data-zip"]),
            "sqm": int(c["data-size"]),
            "rooms": c.get("data-rooms"),
            "rent": int(c["data-rent"]),
            "url": a["href"] if a else "https://kereby.dk/bolig/",
        }


def cej():
    url = ("https://udlejning.cej.dk/find-bolig/overblik?collection=residences"
           "&p=sj%C3%A6lland,k%C3%B8benhavn")
    html = requests.get(url, headers=UA, timeout=30).text
    # bolig.io/Remix streams the result set into the page as one JSON blob
    i = html.index('"searchResponse", ') + len('"searchResponse", ')
    for r in json.JSONDecoder().raw_decode(html[i:])[0]["items"]:
        if r.get("status") != "available":
            continue
        yield {
            "id": "cej:" + r["id"],
            "title": r.get("name", ""),
            "addr": r["location"]["formatted"],
            "zip": int(r["location"]["zipCode"]),
            "sqm": int(r.get("floorSize") or 0),
            "rooms": r.get("numberOfRooms"),
            "rent": int(r.get("price", {}).get("amount") or 0),
            "url": "https://udlejning.cej.dk/boliger/" + r["id"],
        }


SOURCES = [kereby, cej]


def matches(f):
    return f["sqm"] >= MIN_SQM and in_area(f["zip"])


def kr(n): return f"{n:,}".replace(",", ".")


def line(f): return f"{f['sqm']} m² · {f['rooms']} vær · {kr(f['rent'])} kr/md\n{f['addr']}"


def post(**kw):
    assert NTFY, "NTFY_TOPIC is not set"
    kw["headers"] = {k: v.encode("utf-8") for k, v in kw["headers"].items()}
    requests.post(f"https://ntfy.sh/{NTFY}", timeout=30, **kw)


def notify(f, updated):
    post(data=(line(f) + "\n" + f["url"]).encode(),
         headers={"Title": ("Opdateret: " if updated else "") + (f["title"][:70] or f["addr"]),
                  "Click": f["url"], "Tags": "house", "Priority": "high"})


def digest():
    """Always fires, even on an empty queue — silence must not be ambiguous."""
    raw = read_state(QUEUE)
    q = json.loads(raw) if raw else {}
    fs = sorted(q.values(), key=lambda f: -f["sqm"])
    tracked = len(json.loads(read_state(STATE) or "{}"))
    if fs:
        title = f"{len(fs)} nye boliger over {kr(RENT_ALERT)} kr"
        body = (f"New listings over {kr(RENT_ALERT)} kr/md."
                f" (Under that, you get pinged the moment they appear.)\n\n"
                + "\n\n".join(f"{line(f)}\n{f['url']}" for f in fs))
        click = fs[0]["url"]
    else:
        title = "Ingen nye boliger i dag"
        body = (f"Nothing new today.\n\n"
                f"Under {kr(RENT_ALERT)} kr/md — sent the moment they appear, none did.\n"
                f"Over {kr(RENT_ALERT)} kr/md — collected for this summary, none did.\n\n"
                f"Tracking {tracked} listings on Kereby + CEJ.")
        click = "https://udlejning.cej.dk/find-bolig/overblik"
    post(data=body.encode(),
         headers={"Title": title, "Click": click, "Tags": "house"})
    delete_state(QUEUE)


# ponytail: GitHub drops most scheduled slots, so the digest cannot rely on its own cron
# entry, nor on "is it today?" — a gap over midnight would skip a day silently. Rule is
# elapsed time: normally the first run after the evening cutoff, but if the scheduler was
# dead all evening, the next run to land at any hour catches up.
def snoozing(now=None):
    h = (now or datetime.datetime.now(TZ)).astimezone(TZ).hour
    start, end = SNOOZE
    return h >= start or h < end


def due_for_digest(now=None):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    try:
        last = datetime.datetime.fromisoformat(read_state(STAMP).strip())
        last = last if last.tzinfo else last.replace(tzinfo=datetime.timezone.utc)
    except (OSError, ValueError, AttributeError):
        last = now - datetime.timedelta(days=99)   # never sent
    hours = (now - last).total_seconds() / 3600
    if hours >= 20 and (now.astimezone(TZ).hour >= DIGEST_AFTER or hours >= 28):
        write_state(STAMP, now.isoformat())
        return True
    return False


# what a change looks like from outside: price, size or room count moved
def fingerprint(f): return f"{f['rent']}|{f['sqm']}|{f['rooms']}"


def main():
    if snoozing():
        print("snoozing - no scrape")
        return
    raw = read_state(STATE)
    prev = json.loads(raw) if raw else None
    first_run = prev is None
    prev = prev or {}
    found = []
    for src in SOURCES:
        try:
            found += list(src())
        except Exception as e:  # one dead site must not silence the others
            print(f"{src.__name__} failed: {e}", file=sys.stderr)
    cur = {f["id"]: fingerprint(f) for f in found}
    changed = [f for f in found if matches(f) and prev.get(f["id"]) != cur[f["id"]]]
    qraw = read_state(QUEUE)
    queued = json.loads(qraw) if qraw else {}
    for f in changed:
        was = "seeding" if first_run else "UPDATED" if f["id"] in prev else "NEW"
        print(f"{was} {f['sqm']}m² {kr(f['rent'])}kr {f['addr']} {f['url']}")
        if first_run:
            continue
        if f["rent"] < RENT_ALERT:
            notify(f, updated=f["id"] in prev)
        else:
            queued[f["id"]] = f  # ponytail: digest only fires from cron, not on its own
    write_state(QUEUE, json.dumps(queued))
    write_state(STATE, json.dumps(cur, sort_keys=True))
    if not first_run and due_for_digest():
        digest()


def check():
    tz = datetime.timezone.utc
    evening = datetime.datetime(2026, 9, 19, 21, 30, tzinfo=tz)   # after the cutoff
    morning = datetime.datetime(2026, 9, 19, 9, 30, tzinfo=tz)    # before it
    small_hours = datetime.datetime(2026, 9, 20, 3, 30, tzinfo=tz)  # scheduler was dead all evening
    st = lambda when, ago: write_state(STAMP, (when - datetime.timedelta(hours=ago)).isoformat())
    st(evening, 2);      assert not due_for_digest(evening), "2h since last, too soon"
    st(evening, 22);     assert due_for_digest(evening), "evening + 22h elapsed -> send"
    assert not due_for_digest(evening), "the stamp it just wrote must block a second send"
    st(morning, 22);     assert not due_for_digest(morning), "22h but before cutoff -> wait"
    st(small_hours, 30); assert due_for_digest(small_hours), "30h -> catch up at any hour"
    delete_state(STAMP)
    awake = datetime.datetime(2026, 9, 21, 20, 59, tzinfo=TZ)
    assert not snoozing(awake) and snoozing(awake + datetime.timedelta(minutes=1)), "snooze starts 21:00"
    assert snoozing(awake.replace(hour=4)) and not snoozing(awake.replace(hour=5)), "snooze ends 05:00"
    print("digest timing + snooze: ok")
    assert matches({"sqm": 50, "zip": 2200}) and matches({"sqm": 80, "zip": 2000})
    assert not matches({"sqm": 90, "zip": 2300}) and not matches({"sqm": 90, "zip": 2500})
    assert not matches({"sqm": 49, "zip": 2200}) and not matches({"sqm": 90, "zip": 2800})
    for src in SOURCES:
        rows = list(src())
        assert rows, f"{src.__name__} returned nothing — layout probably changed"
        assert all(r["sqm"] > 0 and r["zip"] > 0 and r["url"].startswith("http") for r in rows), src.__name__
        print(f"{src.__name__}: {len(rows)} available, {sum(map(matches, rows))} match")


if __name__ == "__main__":
    digest() if "--digest" in sys.argv else check() if "--check" in sys.argv else main()
