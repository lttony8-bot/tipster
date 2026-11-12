import sys, time, yaml, requests
from datetime import datetime
from db import init, conn
from devig import fair, overround
from stake import kelly, flat
from fetch import Budget, list_sports, fetch_odds, fetch_event_odds, group, LAST
import state

CFG = yaml.safe_load(open("config.yaml", encoding="utf-8"))
SHARPS = set(CFG.get("sharp_books", ["pinnacle"]))
SEMI = set(CFG.get("semi_sharp_books", []))

def leagues():
    return list(dict.fromkeys(CFG["soft_leagues"] + CFG.get("watchlist", [])))

def ts(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()

def pretty(o):
    n, _, p = o.partition("|")
    return f"{n} {p}".strip()

def notify(text):
    tok, chat = CFG.get("telegram_token"), CFG.get("telegram_chat")
    if not tok or not chat:
        print(text); return
    try:
        requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                      data={"chat_id": chat, "text": text, "parse_mode": "HTML"}, timeout=15)
    except Exception as e:
        print(f"[tg fallo] {e}\n{text}")

def cmd_sports():
    for s in list_sports(CFG["api_key"]):
        if s.get("active"):
            print(f'{s["key"]:45} {s["title"]}')

def cmd_discover():
    init()
    budget = Budget(CFG["max_credits_per_run"])
    cost = len(CFG["markets"].split(",")) * len(CFG["regions"].split(","))
    n = 0
    for sport in leagues():
        if not budget.spend(cost): print("Sin creditos."); break
        events, _ = fetch_odds(CFG["api_key"], sport, CFG["regions"], CFG["markets"])
        with conn() as c:
            for e in events:
                c.execute("INSERT OR REPLACE INTO events VALUES (?,?,?,?,?,?,?)",
                          (e["id"], sport, e["commence_time"], ts(e["commence_time"]),
                           e.get("home_team", ""), e.get("away_team", ""), time.time()))
                n += 1
    print(f"{n} eventos en calendario.")

def cmd_collect(hours=None, limit=None, quiet=False):
    init()
    now = time.time()
    lim = CFG.get("max_pick_age_min", 25) * 60
    if hours is None:
        hours = CFG["scan_lead_minutes"] / 60
        lo = now + lim
        hi = now + lim + 1800
    else:
        lo = now + lim
        hi = now + hours * 3600
    with conn() as c:
        rows = c.execute("SELECT id, sport FROM events WHERE commence_ts BETWEEN ? AND ? "
                         "ORDER BY commence_ts LIMIT ?",
                         (lo, hi, limit or 9999)).fetchall()
    done = fail = 0
    for r in rows:
        mk = CFG.get("event_markets", "h2h")
        extra = CFG.get("props_by_sport", {}).get(r["sport"])
        if extra: mk = f"{mk},{extra}"
        ev = fetch_event_odds(CFG["api_key"], r["sport"], r["id"], CFG["regions"], mk)
        if not ev:
            fail += 1; continue
        g = group([ev])
        with conn() as c:
            for m, books in g[r["id"]].items():
                for bk, outs in books.items():
                    for oc, pr in outs.items():
                        c.execute("INSERT INTO snaps(event_id,market,book,outcome,price,ts) "
                                  "VALUES (?,?,?,?,?,?)", (r["id"], m, bk, oc, pr, time.time()))
            c.execute("INSERT INTO scans(event_id, ts) VALUES (?,?)", (r["id"], time.time()))
        done += 1
    if not quiet:
        print(f"recolectados {done} · fallidos {fail}")
    if fail:
        print(f" ultimo error: {LAST['status']} {LAST['body']}")
    return done, fail

def cmd_decide(notify_out=True):
    init()
    if state.get("paused") == "1":
        print("Pausado."); return []
    cap = int(state.get("daily_cap", CFG["daily_cap"]))
    now = time.time()
    d0 = datetime.now()
    dia = datetime(d0.year, d0.month, d0.day).timestamp()
    with conn() as c:
        hoy = c.execute("SELECT COUNT(*) n FROM picks WHERE ts >= ?", (dia,)).fetchone()["n"]
    if hoy >= cap:
        print(f"Tope diario alcanzado ({hoy}/{cap})."); return []

    now_lim = now + CFG.get("max_pick_age_min", 25) * 60
    with conn() as c:
        evs = {r["id"]: r for r in c.execute(
            "SELECT id, sport, commence_ts FROM events WHERE commence_ts > ?", (now_lim,))}
        rows = c.execute(
            "SELECT event_id, market, book, outcome, price, MAX(ts) FROM snaps "
            "WHERE event_id IN (SELECT id FROM events WHERE commence_ts > ?) "
            "GROUP BY event_id, market, book, outcome", (now_lim,)).fetchall()

    books = {}
    for r in rows:
        books.setdefault(r["event_id"], {}).setdefault(r["market"], {}) \
             .setdefault(r["book"], {})[r["outcome"]] = r["price"]

    excl = set(CFG.get("exclude_markets", []))
    found = []
    for eid, markets in books.items():
        sport = evs[eid]["sport"]
        for mk, bks in markets.items():
            if mk in excl: continue
            pres = SHARPS & set(bks)
            if len(pres) < CFG.get("min_sharp_books", 1): continue
            if len({frozenset(bks[s].keys()) for s in pres}) != 1: continue
            outs = sorted(bks[next(iter(pres))].keys())
            if len(outs) < 2: continue
            refset = set(outs)
            fp = fair([max(bks[s][o] for s in pres) for o in outs])
            if not fp: continue
            best = {o: max(bks[s][o] for s in pres) for o in outs}
            for bk, pr in bks.items():
                if bk in SHARPS or set(pr) != refset: continue
                if overround(list(pr.values())) > CFG.get("max_book_overround", 0.10): continue
                need = CFG["min_price_beat"] * (CFG.get("semi_sharp_factor", 1.6) if bk in SEMI else 1.0)
                for oc, price in pr.items():
                    sp = best.get(oc)
                    if not sp or price < CFG["min_price"]: continue
                    edge = price / sp - 1.0
                    if edge < need or edge > CFG.get("max_price_beat", 0.20): continue
                    p = (fp[outs.index(oc)] + 1.0 / sp) / 2.0
                    if p >= 1.0: continue
                    found.append({"event": eid, "sport": sport, "market": mk, "outcome": oc,
                                  "book": bk, "price": price, "p": p, "edge": edge})

    picked, used_ev, used_sp, used_bk = [], {}, {}, {}
    for f in found:
        if len(picked) >= cap - hoy: break
        if used_ev.get(f["event"]): continue
        if used_sp.get(f["sport"], 0) >= CFG.get("max_per_sport", 1): continue
        if used_bk.get(f["book"], 0) >= CFG.get("max_per_book", 2): continue
        used_ev[f["event"]] = 1
        used_sp[f["sport"]] = used_sp.get(f["sport"], 0) + 1
        used_bk[f["book"]] = used_bk.get(f["book"], 0) + 1
        picked.append(f)

    saved = []
    br = float(state.get("bankroll", CFG["bankroll"]))
    peak = max(float(state.get("peak", br)), br)
    state.set("peak", peak)
    guard = 0.5 if br < peak * (1 - CFG.get("drawdown_guard", 0.15)) else 1.0
    with conn() as c:
        for t in picked:
            dup = c.execute("SELECT 1 FROM picks WHERE event_id=? AND market=? AND outcome=? "
                            "AND status='PENDIENTE'",
                            (t["event"], t["market"], t["outcome"])).fetchone()
            if dup: 
                continue
            if CFG.get("staking_mode", "flat") == "flat":
                st = flat(br, CFG.get("flat_pct", 0.01) * guard)
            else:
                st = kelly(t["p"], t["price"], br, CFG["kelly_fraction"] * guard,
                           CFG["max_stake_pct"])
            if st <= 0: continue
            conf = "ALTA" if t["edge"] >= CFG["min_price_beat"] * 2 else "MEDIA"
            cur = c.execute(
                "INSERT INTO picks(event_id,market,outcome,book,price,model_prob,fair_prob,"
                "edge,stake,confidence,status,ts) VALUES (?,?,?,?,?,?,?,?,?,?,'PENDIENTE',?)",
                (t["event"], t["market"], t["outcome"], t["book"], t["price"], t["p"],
                 t["p"], t["edge"], st, conf, time.time()))
            saved.append((cur.lastrowid, t, st))
    if saved and notify_out:
        notify(fmt(saved))
    print(f"{len(saved)} picks guardados de {len(found)} candidatos")
    return saved

def fmt(saved):
    out = []
    with conn() as c:
        for pid, t, st in saved:
            e = c.execute("SELECT home, away, commence FROM events WHERE id=?", (t["event"],)).fetchone()
            if not e: continue
            d = datetime.fromtimestamp(ts(e["commence"]))
            out.append(f'<b>{e["home"]} vs {e["away"]}</b>\n{d:%d/%m %H:%M} · {t["market"]}\n'
                       f'<b>{pretty(t["outcome"])}</b> @ {t["price"]} ({t["book"]})\n'
                       f'beat {t["edge"]*100:.1f}% · ${st:.2f} · id {pid}')
    return "\n\n".join(out)

def cmd_scan():
    cmd_credits()
    if LAST.get("remaining") is not None and LAST["remaining"] < CFG.get("credit_floor", 1000):
        print("Créditos insuficientes, abortando.")
        return
    cmd_collect(hours=CFG.get("test_window_hours", 48),
                limit=CFG.get("test_max_events", 50))
    cmd_decide()

def cmd_auto():
    init()
    lead = CFG["scan_lead_minutes"] * 60
    cool = CFG["rescan_cooldown"] * 60
    print(f"Auto T-{CFG['scan_lead_minutes']}min")
    while True:
        now = time.time()
        with conn() as c:
            rows = c.execute(
                "SELECT e.id, e.sport FROM events e WHERE e.commence_ts BETWEEN ? AND ? "
                "AND NOT EXISTS (SELECT 1 FROM scans s WHERE s.event_id=e.id AND s.ts > ?)",
                (now + lead - 900, now + lead + 900, now - cool)).fetchall()
        for r in rows:
            mk = CFG.get("event_markets", "h2h")
            extra = CFG.get("props_by_sport", {}).get(r["sport"])
            if extra: mk = f"{mk},{extra}"
            ev = fetch_event_odds(CFG["api_key"], r["sport"], r["id"], CFG["regions"], mk)
            if ev:
                g = group([ev])
                with conn() as c:
                    for m, bs in g[r["id"]].items():
                        for bk, outs in bs.items():
                            for oc, pr in outs.items():
                                c.execute("INSERT INTO snaps(event_id,market,book,outcome,price,ts) "
                                          "VALUES (?,?,?,?,?,?)", (r["id"], m, bk, oc, pr, time.time()))
                    c.execute("INSERT INTO scans(event_id, ts) VALUES (?,?)", (r["id"], time.time()))
        cmd_decide()
        capture_closing()
        time.sleep(300)

def capture_closing():
    now = time.time()
    with conn() as c:
        rows = c.execute(
            "SELECT p.id, p.event_id, p.outcome, p.price, e.sport FROM picks p "
            "JOIN events e ON e.id=p.event_id WHERE p.status='PENDIENTE' AND p.clv IS NULL "
            "AND e.commence_ts BETWEEN ? AND ?", (now - 600, now + 1200)).fetchall()
    for r in rows:
        ev = fetch_event_odds(CFG["api_key"], r["sport"], r["event_id"], CFG["regions"], "h2h")
        if not ev: continue
        bs = group([ev]).get(r["event_id"], {}).get("h2h", {})
        for sb in SHARPS:
            if r["outcome"] in bs.get(sb, {}):
                close = bs[sb][r["outcome"]]
                with conn() as c:
                    c.execute("UPDATE picks SET closing_price=?, clv=? WHERE id=?",
                              (close, (r["price"] / close) - 1.0, r["id"]))
                break

def cmd_funnel():
    init()
    now = time.time()
    lim = now + CFG.get("max_pick_age_min", 25) * 60
    with conn() as c:
        evs = c.execute("SELECT id, sport FROM events WHERE commence_ts > ?", (lim,)).fetchall()
        rows = c.execute(
            "SELECT event_id, market, book, outcome, price, MAX(ts) FROM snaps "
            "WHERE event_id IN (SELECT id FROM events WHERE commence_ts > ?) "
            "GROUP BY event_id, market, book, outcome", (lim,)).fetchall()
    books = {}
    for r in rows:
        books.setdefault(r["event_id"], {}).setdefault(r["market"], {}) \
             .setdefault(r["book"], {})[r["outcome"]] = r["price"]
    st = {"eventos": len(evs), "con_afilada": 0, "tipo_igual": 0, "casa_valida": 0,
          "pasa_margen": 0, "pasa_precio": 0, "pasa_edge": 0}
    mejor = -1.0
    for eid, markets in books.items():
        for mk, bks in markets.items():
            if mk in set(CFG.get("exclude_markets", [])): continue
            pres = SHARPS & set(bks)
            if not pres: continue
            st["con_afilada"] += 1
            if len({frozenset(bks[s].keys()) for s in pres}) != 1: continue
            st["tipo_igual"] += 1
            outs = sorted(bks[next(iter(pres))].keys())
            refset = set(outs)
            best = {o: max(bks[s][o] for s in pres) for o in outs}
            for bk, pr in bks.items():
                if bk in SHARPS or set(pr) != refset: continue
                st["casa_valida"] += 1
                if overround(list(pr.values())) > CFG.get("max_book_overround", 0.10): continue
                st["pasa_margen"] += 1
                need = CFG["min_price_beat"] * (CFG.get("semi_sharp_factor", 1.6) if bk in SEMI else 1.0)
                for oc, price in pr.items():
                    if price < CFG["min_price"]: continue
                    st["pasa_precio"] += 1
                    edge = price / best[oc] - 1.0
                    mejor = max(mejor, edge)
                    if edge >= need: st["pasa_edge"] += 1
    for k, v in st.items(): print(f"  {k:14} {v}")
    print(f"  mejor_edge     {mejor*100:.2f}%")

def cmd_books():
    with conn() as c:
        rows = c.execute("SELECT book, COUNT(*) n FROM snaps GROUP BY book ORDER BY n DESC").fetchall()
    for r in rows:
        tag = " AFILADA" if r["book"] in SHARPS else (" SEMI" if r["book"] in SEMI else "")
        print(f'  {r["book"]:24} {r["n"]}{tag}')

def cmd_credits():
    r = requests.get("https://api.the-odds-api.com/v4/sports/",
                     params={"apiKey": CFG["api_key"]}, timeout=20)
    print("status", r.status_code)
    print("restantes", r.headers.get("x-requests-remaining"))
    print("usados", r.headers.get("x-requests-used"))

def cmd_picks():
    with conn() as c:
        rows = c.execute("SELECT p.*, e.home, e.away, e.commence FROM picks p "
                         "JOIN events e ON e.id=p.event_id WHERE p.status='PENDIENTE' "
                         "ORDER BY p.edge DESC").fetchall()
    if not rows: print("Sin picks pendientes."); return
    for r in rows:
        d = datetime.fromtimestamp(ts(r["commence"]))
        print(f'[{r["confidence"]}] {r["home"]} vs {r["away"]} — {d:%d/%m %H:%M}')
        print(f'  {r["market"]} · {pretty(r["outcome"])} @ {r["price"]} ({r["book"]})')
        print(f'  beat {r["edge"]*100:.1f}% · ${r["stake"]:.2f} · id {r["id"]}\n')

def cmd_why(pid):
    with conn() as c:
        p = c.execute("SELECT * FROM picks WHERE id=?", (pid,)).fetchone()
        if not p: print("No existe."); return
        e = c.execute("SELECT home, away FROM events WHERE id=?", (p["event_id"],)).fetchone()
        rows = c.execute("SELECT book, outcome, price, MAX(ts) FROM snaps WHERE event_id=? "
                         "AND market=? GROUP BY book, outcome ORDER BY book",
                         (p["event_id"], p["market"])).fetchall()
    print(f'{e["home"]} vs {e["away"]} · {p["market"]} · {pretty(p["outcome"])}')
    for r in rows:
        tag = "  <-- AFILADA" if r["book"] in SHARPS else ""
        print(f'  {r["book"]:20} {pretty(r["outcome"]):22} {r["price"]}{tag}')

def cmd_reset():
    with conn() as c:
        n = c.execute("DELETE FROM picks WHERE status='PENDIENTE'").rowcount
    print(f"{n} borrados.")

def expire_old():
    with conn() as c:
        c.execute("UPDATE picks SET status='CADUCADO' WHERE status='PENDIENTE' AND ts < ?",
                  (time.time() - CFG.get("max_pick_age_min", 25) * 60,))

def cmd_settle(pid, res):
    with conn() as c:
        c.execute("UPDATE picks SET status='CERRADO', result=? WHERE id=?", (res, pid))
    print(f"{pid} -> {res}")

def cmd_stats():
    with conn() as c:
        rows = c.execute("SELECT stake, price, result, clv FROM picks WHERE status='CERRADO'").fetchall()
    if not rows: print("Sin historial."); return
    staked = sum(r["stake"] for r in rows)
    pnl = sum((r["price"] - 1) * r["stake"] if r["result"] == "win"
              else (-r["stake"] if r["result"] == "loss" else 0) for r in rows)
    w = sum(1 for r in rows if r["result"] == "win")
    print(f"N={len(rows)} · acierto {w/len(rows)*100:.1f}% · P/L ${pnl:.2f} · ROI {pnl/staked*100:.2f}%")
    cl = [r["clv"] for r in rows if r["clv"] is not None]
    if cl:
        print(f"CLV medio {sum(cl)/len(cl)*100:+.2f}% · positivo {sum(1 for x in cl if x>0)/len(cl)*100:.0f}% de {len(cl)}")
    else:
        print("Sin CLV.")

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "picks"
    try:
        if cmd == "sports": cmd_sports()
        elif cmd == "discover": cmd_discover()
        elif cmd == "collect": cmd_collect(hours=CFG.get("test_window_hours", 48), limit=CFG.get("test_max_events", 60))
        elif cmd == "decide": cmd_decide()
        elif cmd == "scan": cmd_scan()
        elif cmd == "auto": cmd_auto()
        elif cmd == "funnel": cmd_funnel()
        elif cmd == "credits": cmd_credits()
        elif cmd == "picks": cmd_picks()
        elif cmd == "why": cmd_why(sys.argv[2])
        elif cmd == "reset": cmd_reset()
        elif cmd == "settle": cmd_settle(sys.argv[2], sys.argv[3])
        elif cmd == "books": cmd_books()
        elif cmd == "stats": cmd_stats()
        else: print("sports|discover|collect|decide|scan|auto|funnel|picks|why|reset|settle|stats")
    except KeyboardInterrupt:
        print("\nDetenido.")