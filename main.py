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
        
    for attempt in range(3): # Reintenta 3 veces si Telegram falla
        try:
            r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                              data={"chat_id": chat, "text": text, "parse_mode": "HTML"}, timeout=15)
            if r.status_code == 429: # Si envías muchos mensajes, Telegram te pide esperar
                time.sleep(5)
                continue
            r.raise_for_status()
            break # Éxito, salir del bucle
        except Exception as e:
            print(f"[TG fallo intento {attempt+1}] Esperando red... {e}")
            time.sleep(3)

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
                         
    # Agrupar por deportes para gastar 1 crédito por liga en lugar de 1 por partido
    sports_to_fetch = list(set(r["sport"] for r in rows))
    valid_ids = set(r["id"] for r in rows)
    
    done = fail = 0
    for sport in sports_to_fetch:
        mk = CFG.get("event_markets", "h2h")
        extra = CFG.get("props_by_sport", {}).get(sport)
        if extra: mk = f"{mk},{extra}"
        
        # AQUÍ ESTÁ LA MAGIA: Pide toda la liga de un solo golpe
        evs_data, _ = fetch_odds(CFG["api_key"], sport, CFG["regions"], mk)
        
        if not evs_data:
            fail += len([r for r in rows if r["sport"] == sport])
            continue
            
        relevant_evs = [e for e in evs_data if e["id"] in valid_ids]
        if not relevant_evs: continue
        
        g = group(relevant_evs)
        with conn() as c:
            for ev in relevant_evs:
                eid = ev["id"]
                if eid not in g: continue
                for m, books in g[eid].items():
                    for bk, outs in books.items():
                        for oc, pr in outs.items():
                            c.execute("INSERT INTO snaps(event_id,market,book,outcome,price,ts) "
                                      "VALUES (?,?,?,?,?,?)", (eid, m, bk, oc, pr, time.time()))
                c.execute("INSERT INTO scans(event_id, ts) VALUES (?,?)", (eid, time.time()))
                done += 1
                
    if not quiet:
        print(f"Recolectados {done} eventos en {len(sports_to_fetch)} ligas · fallidos {fail}")
    if fail:
        print(f" Último error API: {LAST['status']} {LAST['body']}")
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
        if eid not in evs: continue
        sport = evs[eid]["sport"]
        commence_ts = evs[eid]["commence_ts"]
        horas_para_partido = (commence_ts - now) / 3600.0
        
        for mk, bks in markets.items():
            if mk in excl: continue
            pres = SHARPS & set(bks)
            if len(pres) < CFG.get("min_sharp_books", 1): continue
            if len({frozenset(bks[s].keys()) for s in pres}) != 1: continue
            outs = sorted(bks[next(iter(pres))].keys())
            if len(outs) < 2: continue
            refset = set(outs)
            
            # Devigging puro con Shin
            fp = fair([max(bks[s][o] for s in pres) for o in outs])
            if not fp: continue
            best = {o: max(bks[s][o] for s in pres) for o in outs}
            
            for bk, pr in bks.items():
                if bk in SHARPS or set(pr) != refset: continue
                if overround(list(pr.values())) > CFG.get("max_book_overround", 0.10): continue
                
                # --- LÓGICA DE EDGE DINÁMICO POR TIEMPO ---
                if horas_para_partido > 24:
                    base_need = 0.06 # Exige 6% si falta más de 1 día (mercado inestable)
                elif horas_para_partido < 3:
                    base_need = 0.03 # Exige 3% si falta menos de 3 horas (mercado preciso)
                else:
                    base_need = CFG.get("min_price_beat", 0.04) # Exige 4% promedio
                    
                need = base_need * (CFG.get("semi_sharp_factor", 1.6) if bk in SEMI else 1.0)
                # ------------------------------------------

                for oc, price in pr.items():
                    sp = best.get(oc)
                    if not sp or price < CFG.get("min_price", 1.45): continue
                    
                    edge = price / sp - 1.0
                    if edge < need or edge > CFG.get("max_price_beat", 0.20): continue
                    
                    # Probabilidad real del modelo Shin
                    p = fp[outs.index(oc)] 
                    
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
    br = float(state.get("bankroll", CFG.get("bankroll", 2500.0)))
    peak = max(float(state.get("peak", br)), br)
    state.set("peak", peak)
    
    # Drawdown Guard: Si la banca cae, apuesta la mitad para proteger el dinero
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
                st = kelly(t["p"], t["price"], br, CFG.get("kelly_fraction", 0.18) * guard,
                           CFG.get("max_stake_pct", 0.02))
            if st <= 0: continue
            
            conf = "ALTA" if t["edge"] >= base_need * 2 else "MEDIA"
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
    print(f"Auto T-{CFG['scan_lead_minutes']}min (Versión Optimizada)")
    
    while True:
        now = time.time()
        with conn() as c:
            rows = c.execute(
                "SELECT e.id, e.sport FROM events e WHERE e.commence_ts BETWEEN ? AND ? "
                "AND NOT EXISTS (SELECT 1 FROM scans s WHERE s.event_id=e.id AND s.ts > ?)",
                (now + lead - 900, now + lead + 900, now - cool)).fetchall()
                
        sports_to_fetch = list(set(r["sport"] for r in rows))
        valid_ids = set(r["id"] for r in rows)
        
        for sport in sports_to_fetch:
            mk = CFG.get("event_markets", "h2h")
            extra = CFG.get("props_by_sport", {}).get(sport)
            if extra: mk = f"{mk},{extra}"
            
            evs, _ = fetch_odds(CFG["api_key"], sport, CFG["regions"], mk)
            if not evs: continue
            
            relevant = [e for e in evs if e["id"] in valid_ids]
            if not relevant: continue
            
            g = group(relevant)
            with conn() as c:
                for ev in relevant:
                    eid = ev["id"]
                    if eid not in g: continue
                    for m, bs in g[eid].items():
                        for bk, outs in bs.items():
                            for oc, pr in outs.items():
                                c.execute("INSERT INTO snaps(event_id,market,book,outcome,price,ts) "
                                          "VALUES (?,?,?,?,?,?)", (eid, m, bk, oc, pr, time.time()))
                    c.execute("INSERT INTO scans(event_id, ts) VALUES (?,?)", (eid, time.time()))
                    
        cmd_decide()
        capture_closing()
        cmd_grade()        # Califica los picks de ayer
        from db import clean_garbage
        clean_garbage()    # Limpia la base de datos
        time.sleep(300)    # Duerme 5 minutos

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

def cmd_grade():
    """Busca resultados de partidos ya terminados y califica los picks automáticamente."""
    init()
    now = time.time()
    # Busca picks pendientes de partidos que empezaron hace más de 4 horas
    with conn() as c:
        rows = c.execute("SELECT p.id, p.event_id, p.outcome, p.market, e.sport FROM picks p "
                         "JOIN events e ON e.id=p.event_id WHERE p.status='PENDIENTE' "
                         "AND e.commence_ts < ?", (now - 14400,)).fetchall()
    if not rows: return
    
    sports = set(r["sport"] for r in rows)
    for sport in sports:
        r = requests.get(f"https://api.the-odds-api.com/v4/sports/{sport}/scores/",
                         params={"apiKey": CFG["api_key"], "daysFrom": 3})
        if r.status_code != 200: continue
        
        scores = {e["id"]: e for e in r.json() if e.get("completed")}
        
        for row in rows:
            if row["sport"] != sport or row["event_id"] not in scores: continue
            match = scores[row["event_id"]]
            
            # Lógica para H2H (Ganador directo)
            if row["market"] == "h2h" and match.get("scores"):
                s1, s2 = match["scores"][0], match["scores"][1]
                v1, v2 = int(s1["score"]), int(s2["score"])
                
                ganador = s1["name"] if v1 > v2 else (s2["name"] if v2 > v1 else "Empate")
                resultado = "win" if row["outcome"] == ganador else "loss"
                
                with conn() as c:
                    c.execute("UPDATE picks SET status='CERRADO', result=? WHERE id=?", 
                              (resultado, row["id"]))
                print(f"✅ Pick {row['id']} auto-calificado como {resultado} ({v1}-{v2})")

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
        elif cmd == "grade": cmd_grade()
        elif cmd == "stats": cmd_stats()
        else: print("sports|discover|collect|decide|scan|auto|funnel|picks|why|reset|settle|grade|stats")
    except KeyboardInterrupt:
        print("\nDetenido.")