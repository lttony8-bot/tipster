from db import conn

def get(k, default=None):
    with conn() as c:
        r = c.execute("SELECT v FROM state WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default

def set(k, v):
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (k, str(v)))