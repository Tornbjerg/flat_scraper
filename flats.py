#!/usr/bin/env python3
"""Poll Copenhagen rental sites, ntfy me when a new matching flat appears."""
import datetime, json, os, sys
import requests
from bs4 import BeautifulSoup

MIN_SQM = 50
# Copenhagen + Frederiksberg postcodes (2700 Brønshøj / 2720 Vanløse are Kbh kommune too)
SKIP_ZIPS = {2300, 2450, 2500}  # København S, SV, Valby — not wanted
def in_area(z): return z not in SKIP_ZIPS and (1000 <= z <= 2500 or z in (2700, 2720))

RENT_ALERT = 14000  # under this: notify at once. at or above: end-of-day digest.

NTFY = os.environ.get("NTFY_TOPIC", "")  # set in the environment; never commit the topic
HERE = os.path.dirname(os.path.abspath(__file__))
DIGEST_AFTER_UTC = 21  # 23:00 CEST / 22:00 CET
STATE = os.path.join(HERE, "seen.json")      # id -> fingerprint, to spot changes
STAMP = os.path.join(HERE, "last_digest.txt")
QUEUE = os.path.join(HERE, "pending.json")   # id -> listing, waiting for the digest
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
    q = json.load(open(QUEUE)) if os.path.exists(QUEUE) else {}
    fs = sorted(q.values(), key=lambda f: -f["sqm"])
    if fs:
        title = f"{len(fs)} nye boliger over {kr(RENT_ALERT)} kr"
        body = "These listings are new:\n\n" + "\n\n".join(
            f"{line(f)}\n{f['url']}" for f in fs)
        click = fs[0]["url"]
    else:
        title = "Ingen nye boliger i dag"
        body = (f"No new listings over {kr(RENT_ALERT)} kr today.\n"
                f"Watcher ran fine — {len(json.load(open(STATE))) if os.path.exists(STATE) else 0}"
                " listings tracked across Kereby + CEJ.")
        click = "https://udlejning.cej.dk/find-bolig/overblik"
    post(data=body.encode(),
         headers={"Title": title, "Click": click, "Tags": "house"})
    if os.path.exists(QUEUE):
        os.remove(QUEUE)


# ponytail: GitHub drops most scheduled slots, so the digest cannot rely on its own
# cron entry. Any poll run after the cutoff sends it, once per day. Date stamp is the lock.
def due_for_digest():
    today = datetime.datetime.now(datetime.timezone.utc)
    if today.hour < DIGEST_AFTER_UTC:
        return False
    stamp = open(STAMP).read().strip() if os.path.exists(STAMP) else ""
    if stamp == today.strftime("%F"):
        return False
    open(STAMP, "w").write(today.strftime("%F"))
    return True


# what a change looks like from outside: price, size or room count moved
def fingerprint(f): return f"{f['rent']}|{f['sqm']}|{f['rooms']}"


def main():
    prev = json.load(open(STATE)) if os.path.exists(STATE) else None
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
    queued = json.load(open(QUEUE)) if os.path.exists(QUEUE) else {}
    for f in changed:
        was = "seeding" if first_run else "UPDATED" if f["id"] in prev else "NEW"
        print(f"{was} {f['sqm']}m² {kr(f['rent'])}kr {f['addr']} {f['url']}")
        if first_run:
            continue
        if f["rent"] < RENT_ALERT:
            notify(f, updated=f["id"] in prev)
        else:
            queued[f["id"]] = f  # ponytail: digest only fires from cron, not on its own
    json.dump(queued, open(QUEUE, "w"))
    json.dump(cur, open(STATE, "w"))
    if not first_run and due_for_digest():
        digest()


def check():
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
