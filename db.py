import sqlite3, os
import time 

DB = os.path.join(os.path.dirname(__file__), "tipster.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS events(
  id TEXT PRIMARY KEY, sport TEXT, commence TEXT, commence_ts REAL,
  home TEXT, away TEXT, created REAL);
CREATE TABLE IF NOT EXISTS snaps(
  id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT, market TEXT,
  book TEXT, outcome TEXT, price REAL, ts REAL);
CREATE TABLE IF NOT EXISTS picks(
  id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT, market TEXT, outcome TEXT,
  book TEXT, price REAL, model_prob REAL, fair_prob REAL, edge REAL,
  stake REAL, confidence TEXT, status TEXT, result TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS scans(id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS state(k TEXT PRIMARY KEY, v TEXT);
CREATE INDEX IF NOT EXISTS ix_events_ts ON events(commence_ts);
CREATE INDEX IF NOT EXISTS ix_snaps_ev ON snaps(event_id);
"""

def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c

def init():
    with conn() as c:
        c.executescript(SCHEMA)
        cols = [r["name"] for r in c.execute("PRAGMA table_info(picks)")]
        for col in ("clv", "closing_price"):
            if col not in cols:
                c.execute(f"ALTER TABLE picks ADD COLUMN {col} REAL")

def clean_garbage():
    """Borra el historial de cuotas de más de 3 días para que la base de datos sea veloz."""
    limite = time.time() - 259200 # 3 días en segundos
    
    # 1. Primero borramos los datos (dentro de una transacción segura)
    with conn() as c:
        n_snaps = c.execute("DELETE FROM snaps WHERE ts < ?", (limite,)).rowcount
        n_scans = c.execute("DELETE FROM scans WHERE ts < ?", (limite,)).rowcount
        
    # 2. Luego liberamos el espacio (fuera de la transacción)
    conexion = conn()
    conexion.execute("VACUUM")
    conexion.close()
    
    print(f"Limpieza de DB: {n_snaps} snaps y {n_scans} scans viejos eliminados.")