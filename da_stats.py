#!/usr/bin/env python3
"""Relève périodique des statistiques d'un compte DeviantArt (API officielle).

Config (variables d'environnement ou fichier .env à côté du script) :
  DA_CLIENT_ID, DA_CLIENT_SECRET   identifiants de l'app (deviantart.com/developers)
  DA_USERNAME                      compte à suivre
  DA_INTERVAL                      secondes entre deux relevés en mode loop (défaut 3600)
  DA_DB                            chemin SQLite (défaut da_stats.db)
  DA_STORE_RAW                     1 = garde le JSON brut de l'API (défaut), 0 = non

Usage :
  python3 da_stats.py once      un relevé
  python3 da_stats.py loop      relevés réguliers
  python3 da_stats.py report    dernier relevé + évolution depuis le précédent
  python3 da_stats.py csv       export CSV des stats par œuvre (stdout)
  python3 da_stats.py export    génère docs/data.json pour la page GitHub Pages
"""
import csv
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://www.deviantart.com/api/v1/oauth2"
TOKEN_URL = "https://www.deviantart.com/oauth2/token"
HERE = Path(__file__).resolve().parent


def load_env():
    env_file = HERE / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def cfg(name, default=None):
    v = os.environ.get(name) or default
    if not v:
        sys.exit(f"Variable manquante : {name}")
    return v


# ---------- HTTP ----------

def http_json(url, data=None, token=None, retries=6):
    for attempt in range(retries):
        req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode() if data else None)
        if token:  # OAuth 2.1 : jeton dans l'en-tête, jamais dans l'URL
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < retries - 1:
                time.sleep(2 ** attempt * 3)
                continue
            raise RuntimeError(f"HTTP {e.code} sur {url}: {e.read()[:300]!r}")
    raise RuntimeError("unreachable")


class DA:
    def __init__(self, client_id, client_secret):
        tok = http_json(TOKEN_URL, {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        })
        self.token = tok["access_token"]

    def get(self, path, **params):
        q = urllib.parse.urlencode(params, doseq=True)
        time.sleep(0.3)  # reste poli avec l'API
        return http_json(f"{API}{path}?{q}", token=self.token)

    def paged(self, path, limit, max_items=None, **params):
        offset, out = 0, []
        while True:
            page = self.get(path, offset=offset, limit=limit, **params)
            out.extend(page.get("results", []))
            print(f"    {path} offset={offset}: {len(page.get('results', []))} résultats, "
                  f"has_more={page.get('has_more')}, next_offset={page.get('next_offset')}, "
                  f"clés={sorted(k for k in page if k != 'results')}")
            if not page.get("has_more") or (max_items and len(out) >= max_items):
                return out
            offset = page.get("next_offset", offset + limit)


# ---------- Base ----------

SCHEMA = """
CREATE TABLE IF NOT EXISTS profile_snapshots (
  ts INTEGER, username TEXT,
  deviations INTEGER, favourites INTEGER, comments INTEGER,
  pageviews INTEGER, profile_comments INTEGER,
  watchers INTEGER, friends INTEGER,
  raw TEXT
);
CREATE TABLE IF NOT EXISTS deviations (
  deviationid TEXT PRIMARY KEY, title TEXT, url TEXT, published_time INTEGER,
  category TEXT, is_mature INTEGER, tags TEXT, thumb TEXT
);
CREATE TABLE IF NOT EXISTS deviation_snapshots (
  ts INTEGER, deviationid TEXT,
  views INTEGER, favourites INTEGER, comments INTEGER, downloads INTEGER,
  raw TEXT
);
CREATE INDEX IF NOT EXISTS ix_ds ON deviation_snapshots(deviationid, ts);
"""


def db_connect():
    con = sqlite3.connect(cfg("DA_DB", str(HERE / "da_stats.db")))
    con.executescript(SCHEMA)
    return con


# ---------- Relevé ----------

def raw(obj):
    return json.dumps(obj) if cfg("DA_STORE_RAW", "1") == "1" else None


def thumb_of(g):
    thumbs = g.get("thumbs") or []
    return thumbs[len(thumbs) // 2]["src"] if thumbs else None


def count_paged(da, path, username):
    """Compte les éléments d'une liste paginée (watchers/friends), None si inaccessible."""
    try:
        return len(da.paged(path, 50, username=username))
    except RuntimeError as e:
        print(f"  ! {path}: {e}", file=sys.stderr)
        return None


def snapshot(con, da, username):
    ts = int(time.time())

    prof = da.get(f"/user/profile/{urllib.parse.quote(username)}", ext_collections="false", ext_galleries="false")
    st = prof.get("stats", {})
    watchers = count_paged(da, f"/user/watchers/{urllib.parse.quote(username)}", username)
    friends = count_paged(da, f"/user/friends/{urllib.parse.quote(username)}", username)
    con.execute(
        "INSERT INTO profile_snapshots VALUES (?,?,?,?,?,?,?,?,?,?)",
        (ts, username, st.get("user_deviations"), st.get("user_favourites"), st.get("user_comments"),
         st.get("profile_pageviews"), st.get("profile_comments"), watchers, friends, raw(prof)),
    )

    by_id = {d["deviationid"]: d for d in
             da.paged("/gallery/all", 24, username=username, mature_content="true")}
    print(f"  gallery/all : {len(by_id)} œuvres (profil annonce {st.get('user_deviations')})")
    try:  # second passage par les dossiers, pour ce que gallery/all ne renvoie pas
        folders = da.paged("/gallery/folders", 50, username=username, mature_content="true")
        for f in folders:
            for d in da.paged(f"/gallery/{f['folderid']}", 24, username=username, mature_content="true"):
                by_id.setdefault(d["deviationid"], d)
        print(f"  + {len(folders)} dossiers : {len(by_id)} œuvres au total")
    except RuntimeError as e:
        print(f"  ! dossiers: {e}", file=sys.stderr)
    ids = list(by_id)

    n = 0
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        meta = da.get("/deviation/metadata", deviationids=chunk, ext_stats="true")
        for m in meta.get("metadata", []):
            did = m["deviationid"]
            g = by_id.get(did, {})
            s = m.get("stats", {})
            con.execute(
                "INSERT OR REPLACE INTO deviations VALUES (?,?,?,?,?,?,?,?)",
                (did, m.get("title") or g.get("title"), g.get("url"),
                 int(g["published_time"]) if g.get("published_time") else None,
                 g.get("category"), int(bool(g.get("is_mature"))),
                 ",".join(t.get("tag_name", "") for t in m.get("tags", [])), thumb_of(g)),
            )
            con.execute(
                "INSERT INTO deviation_snapshots VALUES (?,?,?,?,?,?,?)",
                (ts, did, s.get("views"), s.get("favourites"), s.get("comments"), s.get("downloads"), raw(m)),
            )
            n += 1
    con.commit()
    print(f"[{time.strftime('%F %T')}] {username}: {st.get('user_deviations')} œuvres, "
          f"{watchers} followers, {n} œuvres relevées")


def run_once():
    da = DA(cfg("DA_CLIENT_ID"), cfg("DA_CLIENT_SECRET"))
    con = db_connect()
    snapshot(con, da, cfg("DA_USERNAME"))
    con.close()


def loop():
    interval = int(cfg("DA_INTERVAL", "3600"))
    while True:
        try:
            run_once()
        except Exception as e:  # on ne meurt pas sur une erreur réseau
            print(f"[{time.strftime('%F %T')}] erreur: {e}", file=sys.stderr)
        time.sleep(interval)


# ---------- Rapports ----------

def fmt_delta(new, old):
    if new is None:
        return "—"
    if old is None or old == new:
        return str(new)
    return f"{new} ({new - old:+d})"


def report():
    con = db_connect()
    rows = con.execute("SELECT ts, deviations, favourites, comments, pageviews, profile_comments, watchers, friends "
                       "FROM profile_snapshots ORDER BY ts DESC LIMIT 2").fetchall()
    if not rows:
        sys.exit("Aucun relevé. Lancez d'abord : python3 da_stats.py once")
    cur, prev = rows[0], rows[1] if len(rows) > 1 else [None] * 8
    print(f"Relevé du {time.strftime('%F %T', time.localtime(cur[0]))}")
    for i, label in enumerate(["Œuvres", "Favoris donnés", "Commentaires postés", "Vues du profil",
                               "Commentaires du profil", "Followers", "Suivis"], start=1):
        print(f"  {label:<24} {fmt_delta(cur[i], prev[i])}")

    print("\nTop œuvres (dernier relevé) :")
    q = """
    SELECT d.title, s.views, s.favourites, s.comments, s.downloads,
           s.favourites - COALESCE((SELECT p.favourites FROM deviation_snapshots p
                                    WHERE p.deviationid=s.deviationid AND p.ts<s.ts
                                    ORDER BY p.ts DESC LIMIT 1), s.favourites)
    FROM deviation_snapshots s JOIN deviations d USING(deviationid)
    WHERE s.ts = ? ORDER BY s.favourites DESC LIMIT 15"""
    print(f"  {'Titre':<40} {'Vues':>7} {'Fav':>6} {'Com':>5} {'DL':>5} {'ΔFav':>5}")
    for t, v, f, c, dl, df in con.execute(q, (cur[0],)):
        print(f"  {(t or '')[:39]:<40} {v or 0:>7} {f or 0:>6} {c or 0:>5} {dl or 0:>5} {df:>+5}")


def export_csv():
    con = db_connect()
    w = csv.writer(sys.stdout)
    w.writerow(["ts", "deviationid", "title", "published", "views", "favourites", "comments", "downloads"])
    for r in con.execute("SELECT s.ts, s.deviationid, d.title, d.published_time, s.views, s.favourites, "
                         "s.comments, s.downloads FROM deviation_snapshots s JOIN deviations d USING(deviationid) "
                         "ORDER BY s.ts, s.deviationid"):
        w.writerow(r)


def export_json():
    con = db_connect()
    out = {"generated": int(time.time()), "username": cfg("DA_USERNAME", ""), "profile": [], "deviations": []}
    for r in con.execute("SELECT ts, deviations, favourites, comments, pageviews, profile_comments, watchers, friends "
                         "FROM profile_snapshots ORDER BY ts"):
        out["profile"].append(dict(zip(
            ["ts", "deviations", "favourites", "comments", "pageviews", "profile_comments", "watchers", "friends"], r)))
    hist = {}
    for did, ts, v, f, c, dl in con.execute(
            "SELECT deviationid, ts, views, favourites, comments, downloads FROM deviation_snapshots ORDER BY ts"):
        hist.setdefault(did, []).append([ts, v, f, c, dl])
    for did, title, url, pub, cat, mat, tags, thumb in con.execute(
            "SELECT deviationid, title, url, published_time, category, is_mature, tags, thumb FROM deviations"):
        out["deviations"].append({"id": did, "title": title, "url": url, "published": pub, "category": cat,
                                  "mature": bool(mat), "tags": tags, "thumb": thumb, "history": hist.get(did, [])})
    path = HERE / "docs" / "data.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(out, separators=(",", ":")))
    print(f"{path} : {len(out['profile'])} relevés de profil, {len(out['deviations'])} œuvres")


if __name__ == "__main__":
    load_env()
    cmds = {"once": run_once, "loop": loop, "report": report, "csv": export_csv, "export": export_json}
    if len(sys.argv) != 2 or sys.argv[1] not in cmds:
        sys.exit(__doc__)
    cmds[sys.argv[1]]()
