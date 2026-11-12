import time, requests, threading
from db import init, conn
from main import CFG, pretty, cmd_discover, cmd_auto, cmd_decide
import state

TOK = CFG["telegram_token"]; CHAT = str(CFG["telegram_chat"])
API = f"https://api.telegram.org/bot{TOK}"

def send(t):
    try: requests.post(f"{API}/sendMessage", data={"chat_id": CHAT, "text": t, "parse_mode": "HTML"}, timeout=15)
    except Exception: pass

def handle(text):
    p = text.split(); c = p[0].lower().split("@")[0] if p else ""
    if c in ("/start", "/ayuda"):
        return "/picks /stats /scan /pausa /sigue /bankroll 3000 /settle <id> <win|loss|void>"
    if c == "/picks":
        with conn() as cc:
            rows = cc.execute("SELECT p.*, e.home, e.away FROM picks p JOIN events e ON e.id=p.event_id "
                              "WHERE p.status='PENDIENTE'").fetchall()
        if not rows: return "Sin picks."
        return "\n\n".join(f'{r["home"]} vs {r["away"]} · <b>{pretty(r["outcome"])}</b> @ '
                           f'{r["price"]} · id {r["id"]}' for r in rows)
    if c == "/scan": cmd_discover(); cmd_decide(); return "Hecho."
    if c == "/pausa": state.set("paused", "1"); return "Pausado."
    if c == "/sigue": state.set("paused", "0"); return "Seguido."
    if c == "/bankroll" and len(p) > 1: state.set("bankroll", p[1]); return f"Banca {p[1]}"
    if c == "/settle" and len(p) > 2:
        with conn() as cc: cc.execute("UPDATE picks SET status='CERRADO', result=? WHERE id=?", (p[2], p[1]))
        return f"{p[1]} -> {p[2]}"
    return "?"

def main():
    init()
    send("En linea.")
    threading.Thread(target=cmd_auto, daemon=True).start()
    off = int(state.get("offset", 0))
    while True:
        try:
            r = requests.get(f"{API}/getUpdates", params={"offset": off, "timeout": 30}, timeout=40)
            for u in r.json().get("result", []):
                off = u["update_id"] + 1; state.set("offset", off)
                m = u.get("message", {})
                if str(m.get("chat", {}).get("id")) == CHAT and m.get("text"):
                    send(handle(m["text"]))
        except Exception:
            time.sleep(5)

if __name__ == "__main__":
    main()