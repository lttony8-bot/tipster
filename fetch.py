import requests
from collections import defaultdict

BASE = "https://api.the-odds-api.com/v4"
LAST = {"status": None, "body": "", "remaining": None}

class Budget:
    def __init__(self, limit): self.left = limit
    def spend(self, n):
        self.left -= n
        return self.left >= 0

def list_sports(key):
    r = requests.get(f"{BASE}/sports/", params={"apiKey": key}, timeout=20)
    r.raise_for_status()
    return r.json()

def _get(url, key, regions, markets):
    r = requests.get(url, params={"apiKey": key, "regions": regions,
                                  "markets": markets, "oddsFormat": "decimal"}, timeout=25)
    if r.status_code != 200:
        LAST["status"] = r.status_code
        LAST["body"] = r.text[:300]
    return r

def fetch_odds(key, sport, regions, markets):
    r = _get(f"{BASE}/sports/{sport}/odds", key, regions, markets)
    if r.status_code != 200:
        r = _get(f"{BASE}/sports/{sport}/odds", key, regions, "h2h")
        if r.status_code != 200:
            return [], 0
    return r.json(), len(markets.split(",")) * len(regions.split(","))

def fetch_event_odds(key, sport, event_id, regions, markets):
    url = f"{BASE}/sports/{sport}/events/{event_id}/odds"
    r = _get(url, key, regions, markets)
    if r.status_code != 200:
        r = _get(url, key, regions, "h2h")
        if r.status_code != 200:
            return None
    return r.json()

def group(events):
    out = defaultdict(lambda: defaultdict(lambda: defaultdict(dict)))
    for e in events:
        for bk in e.get("bookmakers", []):
            for mk in bk.get("markets", []):
                for oc in mk.get("outcomes", []):
                    out[e["id"]][mk["key"]][bk["key"]][f'{oc["name"]}|{oc.get("point","")}'] = oc["price"]
    return out