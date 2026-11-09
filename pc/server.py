# -*- coding: utf-8 -*-
"""
Inventaire Clinique — programme PC.

Lance un petit serveur sur le réseau local :
  * interface d'inventaire dans le navigateur : http://localhost:8765
  * réception des produits envoyés par les téléphones (Wi-Fi)
  * téléchargement de l'application Android : http://<ip-du-pc>:8765/app
Les données sont dans le dossier « donnees » à côté du programme.
"""
import base64, datetime as dt, io, json, os, re, secrets, shutil, socket, sqlite3, sys
import threading, time, webbrowser, zipfile
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

APP_VERSION = "2.0"
PORT = 8765
DISCOVERY_PORT = 8766

FROZEN = getattr(sys, "frozen", False)
RES_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
BASE_DIR = os.path.dirname(sys.executable) if FROZEN else os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "donnees")
PHOTO_DIR = os.path.join(DATA_DIR, "photos")
BACKUP_DIR = os.path.join(DATA_DIR, "sauvegardes")
DB_PATH = os.path.join(DATA_DIR, "inventaire.db")
WEB_DIR = os.path.join(RES_DIR, "web")
APK_PATH = os.path.join(RES_DIR, "app.apk")
for d in (DATA_DIR, PHOTO_DIR, BACKUP_DIR):
    os.makedirs(d, exist_ok=True)

XLSX_COLS = ["Produit", "Catégorie", "Marque", "Lot", "Péremption",
             "Coffrets fermés", "Flacons entamés", "Flacons/coffret", "Prix (DA/coffret)"]
FIELDS = ["nom", "categorie", "marque", "lot", "peremption", "coffrets", "entames",
          "flacons", "prix", "code_barres", "nom_propose", "source", "detail", "emplacement"]

LOCK = threading.RLock()
REV = [1]


def bump():
    REV[0] += 1


def now():
    return dt.datetime.now().isoformat(timespec="seconds")


# ------------------------------------------------------------------ database
def db():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


CON = db()
CON.executescript("""
CREATE TABLE IF NOT EXISTS products(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  nom TEXT NOT NULL, categorie TEXT DEFAULT '', marque TEXT DEFAULT '', lot TEXT DEFAULT '',
  peremption TEXT DEFAULT '', coffrets INTEGER DEFAULT 0, entames INTEGER DEFAULT 0,
  flacons INTEGER, prix REAL, code_barres TEXT DEFAULT '', nom_propose INTEGER DEFAULT 0,
  source TEXT DEFAULT '', detail TEXT DEFAULT '', emplacement TEXT DEFAULT '',
  photos TEXT DEFAULT '[]', a_verifier INTEGER DEFAULT 0, created TEXT, updated TEXT);
CREATE TABLE IF NOT EXISTS history(id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER, ts TEXT, text TEXT);
CREATE TABLE IF NOT EXISTS devices(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, token TEXT UNIQUE,
  version TEXT DEFAULT '', created TEXT, last_seen TEXT);
CREATE TABLE IF NOT EXISTS pairings(id TEXT PRIMARY KEY, name TEXT, code TEXT, status TEXT, token TEXT, created REAL);
CREATE TABLE IF NOT EXISTS receptions(id INTEGER PRIMARY KEY AUTOINCREMENT, device TEXT, ts TEXT,
  results TEXT, undone INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
""")
CON.commit()


def setting(key, default=""):
    r = CON.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_setting(key, value):
    CON.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, value))
    CON.commit()


if not setting("pc_name"):
    set_setting("pc_name", "PC " + socket.gethostname()[:20])


def row_to_product(r):
    p = dict(r)
    p["photos"] = json.loads(p.get("photos") or "[]")
    p["nom_propose"] = bool(p.get("nom_propose"))
    p["a_verifier"] = bool(p.get("a_verifier"))
    return p


def add_history(pid, text):
    CON.execute("INSERT INTO history(product_id,ts,text) VALUES(?,?,?)", (pid, now(), text))


def norm(s):
    return re.sub(r"\s+", " ", str(s or "")).strip()


def to_int(v, default=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def to_num(v):
    if v in (None, ""):
        return None
    try:
        f = float(str(v).replace(" ", "").replace(",", "."))
        return int(f) if f.is_integer() else f
    except ValueError:
        return None


def find_same(item):
    """Même produit déjà en stock : même lot + (même code-barres ou même nom), ou sans lot : même nom et marque."""
    nom, lot, cb = norm(item.get("nom")).lower(), norm(item.get("lot")), norm(item.get("code_barres"))
    marque = norm(item.get("marque")).lower()
    for r in CON.execute("SELECT * FROM products"):
        rl = norm(r["lot"])
        if lot and rl == lot and ((cb and norm(r["code_barres"]) == cb) or norm(r["nom"]).lower() == nom):
            return r
        if not lot and not rl and norm(r["nom"]).lower() == nom and norm(r["marque"]).lower() == marque:
            return r
    return None


def save_photos(pid, photos):
    names = []
    for i, b64 in enumerate(photos[:4]):
        try:
            data = base64.b64decode(b64.split(",")[-1])
        except Exception:
            continue
        if not data.startswith(b"\xff\xd8") or len(data) > 2_000_000:
            continue
        name = f"{pid}_{int(time.time()*1000)}_{i}.jpg"
        with open(os.path.join(PHOTO_DIR, name), "wb") as f:
            f.write(data)
        names.append(name)
    return names


def clean_item(it):
    per = norm(it.get("peremption"))
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", per):
        per = ""
    return {
        "nom": norm(it.get("nom"))[:200] or "Produit non identifié",
        "categorie": norm(it.get("categorie"))[:80],
        "marque": norm(it.get("marque"))[:120],
        "lot": norm(it.get("lot"))[:80],
        "peremption": per,
        "coffrets": max(0, to_int(it.get("coffrets"), 1)),
        "entames": max(0, to_int(it.get("entames"), 0)),
        "flacons": (None if it.get("flacons") in (None, "") else max(0, to_int(it.get("flacons")))),
        "prix": to_num(it.get("prix")),
        "code_barres": re.sub(r"\s", "", norm(it.get("code_barres") or it.get("codeBarres")))[:60],
        "nom_propose": 1 if it.get("nom_propose") or it.get("nomPropose") else 0,
        "source": norm(it.get("source"))[:20],
        "detail": norm(it.get("detail"))[:400],
        "emplacement": norm(it.get("emplacement"))[:80],
    }


def receive(device_name, items):
    results = []
    with LOCK:
        for raw in items[:100]:
            it = clean_item(raw)
            photos = raw.get("photos") or []
            a_verifier = 1 if (it["source"] in ("estime", "inconnu") or it["nom_propose"]) else 0
            same = find_same(it)
            if same:
                pid = same["id"]
                upd = {"coffrets": same["coffrets"] + it["coffrets"], "entames": same["entames"] + it["entames"]}
                for k in ("categorie", "marque", "peremption", "code_barres", "emplacement"):
                    if not same[k] and it[k]:
                        upd[k] = it[k]
                if same["flacons"] is None and it["flacons"] is not None:
                    upd["flacons"] = it["flacons"]; upd["source"] = it["source"]
                if same["prix"] is None and it["prix"] is not None:
                    upd["prix"] = it["prix"]
                sets = ",".join(f"{k}=?" for k in upd)
                CON.execute(f"UPDATE products SET {sets}, updated=? WHERE id=?", (*upd.values(), now(), pid))
                add_history(pid, f"Reçu de « {device_name} » : +{it['coffrets']} coffret(s), +{it['entames']} entamé(s)")
                results.append({"status": "merged", "id": pid, "nom": same["nom"], "coffrets": it["coffrets"],
                                "entames": it["entames"], "a_verifier": bool(same["a_verifier"])})
            else:
                cur = CON.execute(
                    "INSERT INTO products(nom,categorie,marque,lot,peremption,coffrets,entames,flacons,prix,code_barres,"
                    "nom_propose,source,detail,emplacement,a_verifier,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (it["nom"], it["categorie"], it["marque"], it["lot"], it["peremption"], it["coffrets"], it["entames"],
                     it["flacons"], it["prix"], it["code_barres"], it["nom_propose"], it["source"], it["detail"],
                     it["emplacement"], a_verifier, now(), now()))
                pid = cur.lastrowid
                names = save_photos(pid, photos)
                if names:
                    CON.execute("UPDATE products SET photos=? WHERE id=?", (json.dumps(names), pid))
                add_history(pid, f"Reçu de « {device_name} »" + (" — nom proposé d'après l'image" if it["nom_propose"] else ""))
                results.append({"status": "added", "id": pid, "nom": it["nom"], "coffrets": it["coffrets"],
                                "entames": it["entames"], "a_verifier": bool(a_verifier)})
        CON.execute("INSERT INTO receptions(device,ts,results) VALUES(?,?,?)", (device_name, now(), json.dumps(results)))
        CON.commit()
        bump()
    return results


def undo_reception(rid):
    with LOCK:
        r = CON.execute("SELECT * FROM receptions WHERE id=?", (rid,)).fetchone()
        if not r or r["undone"]:
            return False
        for res in json.loads(r["results"]):
            p = CON.execute("SELECT * FROM products WHERE id=?", (res["id"],)).fetchone()
            if not p:
                continue
            if res["status"] == "added":
                delete_product(res["id"], commit=False)
            else:
                CON.execute("UPDATE products SET coffrets=max(0,coffrets-?), entames=max(0,entames-?), updated=? WHERE id=?",
                            (res["coffrets"], res["entames"], now(), res["id"]))
                add_history(res["id"], "Envoi annulé depuis le PC")
        CON.execute("UPDATE receptions SET undone=1 WHERE id=?", (rid,))
        CON.commit()
        bump()
        return True


def delete_product(pid, commit=True):
    p = CON.execute("SELECT photos FROM products WHERE id=?", (pid,)).fetchone()
    if p:
        for n in json.loads(p["photos"] or "[]"):
            try:
                os.remove(os.path.join(PHOTO_DIR, os.path.basename(n)))
            except OSError:
                pass
    CON.execute("DELETE FROM products WHERE id=?", (pid,))
    CON.execute("DELETE FROM history WHERE product_id=?", (pid,))
    if commit:
        CON.commit()
        bump()


def catalog():
    out = []
    for r in CON.execute("SELECT nom,categorie,marque,lot,flacons,prix,code_barres,nom_propose FROM products"):
        out.append({"nom": r["nom"], "categorie": r["categorie"], "marque": r["marque"], "lot": r["lot"],
                    "flacons": r["flacons"], "prix": r["prix"], "codeBarres": r["code_barres"]})
    return out


# ------------------------------------------------------------------ excel
def xml_esc(v):
    s = str(v)
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", s)
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def build_xlsx(rows):
    def col(i):
        return chr(65 + i)
    body = []
    for ri, row in enumerate([XLSX_COLS] + rows, start=1):
        cells = []
        for ci, v in enumerate(row):
            if v is None or v == "":
                continue
            ref = f"{col(ci)}{ri}"
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                cells.append(f'<c r="{ref}"><v>{v}</v></c>')
            else:
                cells.append(f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{xml_esc(v)}</t></is></c>')
        body.append(f'<row r="{ri}">{"".join(cells)}</row>')
    files = {
        "[Content_Types].xml": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>',
        "_rels/.rels": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        "xl/workbook.xml": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Inventaire" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
        "xl/worksheets/sheet1.xml": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><cols><col min="1" max="9" width="18.83203125" customWidth="1"/></cols><sheetData>' + "".join(body) + "</sheetData></worksheet>",
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in files.items():
            z.writestr(name, text.encode("utf-8"))
    return buf.getvalue()


def export_rows(ids=None):
    q = "SELECT * FROM products ORDER BY categorie, nom"
    rows = []
    for r in CON.execute(q):
        if ids and r["id"] not in ids:
            continue
        prix = r["prix"]
        if isinstance(prix, float) and prix.is_integer():
            prix = int(prix)
        rows.append([r["nom"], r["categorie"], r["marque"], r["lot"], r["peremption"], r["coffrets"],
                     r["entames"], r["flacons"], prix])
    return rows


# ------------------------------------------------------------------ network helpers
def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def qr_svg(text):
    try:
        import segno  # inclus dans le programme compilé
        buf = io.BytesIO()
        segno.make(text, error="m").save(buf, kind="svg", scale=6, border=2, dark="#0E1B1A")
        return buf.getvalue().decode("utf-8")
    except Exception:
        return ""


def discovery_loop():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("", DISCOVERY_PORT))
    except OSError:
        return
    while True:
        try:
            data, addr = s.recvfrom(256)
            if data.strip() == b"INVENTAIRE?":
                reply = json.dumps({"app": "inventaire-clinique", "name": setting("pc_name"), "port": PORT,
                                    "ip": lan_ip(), "version": APP_VERSION}).encode()
                s.sendto(reply, addr)
        except Exception:
            time.sleep(0.5)


def backup_loop():
    while True:
        try:
            day = dt.date.today().isoformat()
            dest = os.path.join(BACKUP_DIR, f"inventaire-{day}.db")
            if not os.path.exists(dest):
                with LOCK:
                    b = sqlite3.connect(dest)
                    CON.backup(b)
                    b.close()
                olds = sorted(f for f in os.listdir(BACKUP_DIR) if f.endswith(".db"))
                for f in olds[:-30]:
                    os.remove(os.path.join(BACKUP_DIR, f))
        except Exception:
            pass
        time.sleep(3600)


def cleanup_pairings():
    CON.execute("DELETE FROM pairings WHERE status!='ok' AND created < ?", (time.time() - 600,))


# ------------------------------------------------------------------ HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "InventaireClinique/" + APP_VERSION

    def log_message(self, *a):
        pass

    # -- helpers
    def is_local(self):
        return self.client_address[0] in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

    def send(self, code, body=b"", ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def err(self, code, msg):
        self.send(code, {"error": msg})

    def body_json(self, limit=40_000_000):
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            raise ValueError("trop grand")
        raw = self.rfile.read(n) if n else b"{}"
        return json.loads(raw.decode("utf-8") or "{}")

    def device(self):
        tok = self.headers.get("X-Token", "")
        if not tok:
            return None
        d = CON.execute("SELECT * FROM devices WHERE token=?", (tok,)).fetchone()
        if d:
            CON.execute("UPDATE devices SET last_seen=?, version=? WHERE id=?",
                        (now(), self.headers.get("X-App-Version", d["version"]), d["id"]))
            CON.commit()
        return d

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Token, X-App-Version")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.end_headers()

    # -- routes
    def do_GET(self):
        with LOCK:
            return self._do_GET()

    def _do_GET(self):
        u = urlparse(self.path); path = u.path; qs = parse_qs(u.query)
        try:
            if path in ("/", "/index.html"):
                if not self.is_local():
                    return self.send(200, self.install_page(), "text/html; charset=utf-8")
                with open(os.path.join(WEB_DIR, "index.html"), "rb") as f:
                    return self.send(200, f.read(), "text/html; charset=utf-8")
            if path in ("/app", "/app.apk", "/Inventaire.apk"):
                if not os.path.exists(APK_PATH):
                    return self.send(404, "<h1>Application non incluse dans ce programme.</h1>", "text/html; charset=utf-8")
                with open(APK_PATH, "rb") as f:
                    data = f.read()
                return self.send(200, data, "application/vnd.android.package-archive",
                                 {"Content-Disposition": 'attachment; filename="Inventaire.apk"'})
            if path.startswith("/photos/"):
                name = os.path.basename(path)
                fp = os.path.join(PHOTO_DIR, name)
                if not re.fullmatch(r"[\w.-]+\.jpg", name) or not os.path.exists(fp):
                    return self.err(404, "photo introuvable")
                with open(fp, "rb") as f:
                    return self.send(200, f.read(), "image/jpeg")
            if path == "/api/hello":
                return self.send(200, {"app": "inventaire-clinique", "name": setting("pc_name"), "version": APP_VERSION})
            if path.startswith("/api/pair/"):
                pid = path.rsplit("/", 1)[-1]
                r = CON.execute("SELECT * FROM pairings WHERE id=?", (pid,)).fetchone()
                if not r:
                    return self.send(200, {"status": "expired"})
                out = {"status": r["status"]}
                if r["status"] == "ok":
                    out["token"] = r["token"]; out["name"] = setting("pc_name")
                    CON.execute("DELETE FROM pairings WHERE id=?", (pid,)); CON.commit()
                return self.send(200, out)
            if path == "/api/catalog":
                if not self.device():
                    return self.err(401, "téléphone non autorisé")
                return self.send(200, {"products": catalog(), "name": setting("pc_name")})

            # ---- admin (PC only)
            if not self.is_local():
                return self.err(403, "réservé au PC")
            if path == "/api/state":
                since = int((qs.get("rev") or ["0"])[0])
                if since == REV[0]:
                    return self.send(200, {"rev": REV[0], "same": True})
                return self.send(200, self.state())
            if path.startswith("/api/history/"):
                pid = to_int(path.rsplit("/", 1)[-1])
                rows = [dict(r) for r in CON.execute("SELECT ts,text FROM history WHERE product_id=? ORDER BY id DESC", (pid,))]
                return self.send(200, {"history": rows})
            if path == "/api/export.xlsx":
                ids = None
                if qs.get("ids"):
                    ids = {to_int(x) for x in qs["ids"][0].split(",") if x}
                data = build_xlsx(export_rows(ids))
                name = f"Inventaire_{dt.date.today().isoformat()}.xlsx"
                return self.send(200, data, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                 {"Content-Disposition": f'attachment; filename="{name}"'})
            return self.err(404, "introuvable")
        except Exception as e:
            return self.err(500, str(e))

    def do_POST(self):
        with LOCK:
            return self._do_POST()

    def _do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self.body_json()
            if path == "/api/pair":
                cleanup_pairings()
                name = norm(body.get("name"))[:60] or "Téléphone"
                code = re.sub(r"\D", "", str(body.get("code", "")))[:4]
                if len(code) != 4:
                    return self.err(400, "code invalide")
                pid = secrets.token_urlsafe(12)
                CON.execute("INSERT INTO pairings(id,name,code,status,created) VALUES(?,?,?,?,?)",
                            (pid, name, code, "pending", time.time()))
                CON.commit(); bump()
                return self.send(200, {"id": pid, "name": setting("pc_name")})
            if path == "/api/receive":
                d = self.device()
                if not d:
                    return self.err(401, "téléphone non autorisé")
                items = body.get("items") or []
                if not isinstance(items, list) or not items:
                    return self.err(400, "aucun produit")
                return self.send(200, {"results": receive(d["name"], items)})

            if not self.is_local():
                return self.err(403, "réservé au PC")
            m = re.fullmatch(r"/api/pair/([\w-]+)/(approve|refuse)", path)
            if m:
                pid, act = m.groups()
                r = CON.execute("SELECT * FROM pairings WHERE id=?", (pid,)).fetchone()
                if not r:
                    return self.err(404, "demande expirée")
                if act == "approve":
                    tok = secrets.token_urlsafe(24)
                    CON.execute("INSERT INTO devices(name,token,created,last_seen) VALUES(?,?,?,?)", (r["name"], tok, now(), now()))
                    CON.execute("UPDATE pairings SET status='ok', token=? WHERE id=?", (tok, pid))
                else:
                    CON.execute("UPDATE pairings SET status='refused' WHERE id=?", (pid,))
                CON.commit(); bump()
                return self.send(200, {"ok": True})
            if path == "/api/products":
                it = clean_item(body)
                it["coffrets"] = max(0, to_int(body.get("coffrets"), 0))
                with LOCK:
                    cur = CON.execute(
                        "INSERT INTO products(nom,categorie,marque,lot,peremption,coffrets,entames,flacons,prix,code_barres,"
                        "nom_propose,source,detail,emplacement,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,0,'manuel',?,?,?,?)",
                        (it["nom"], it["categorie"], it["marque"], it["lot"], it["peremption"], it["coffrets"], it["entames"],
                         it["flacons"], it["prix"], it["code_barres"], it["detail"], it["emplacement"], now(), now()))
                    add_history(cur.lastrowid, "Ajouté à la main sur le PC")
                    CON.commit(); bump()
                return self.send(200, {"id": cur.lastrowid})
            m = re.fullmatch(r"/api/receptions/(\d+)/undo", path)
            if m:
                return self.send(200, {"ok": undo_reception(int(m.group(1)))})
            if path == "/api/settings":
                if body.get("pc_name"):
                    set_setting("pc_name", norm(body["pc_name"])[:40]); bump()
                return self.send(200, {"ok": True})
            return self.err(404, "introuvable")
        except Exception as e:
            return self.err(500, str(e))

    def do_PUT(self):
        with LOCK:
            return self._do_PUT()

    def _do_PUT(self):
        path = urlparse(self.path).path
        if not self.is_local():
            return self.err(403, "réservé au PC")
        try:
            m = re.fullmatch(r"/api/products/(\d+)", path)
            if not m:
                return self.err(404, "introuvable")
            pid = int(m.group(1))
            body = self.body_json()
            with LOCK:
                old = CON.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
                if not old:
                    return self.err(404, "produit introuvable")
                it = clean_item({**dict(old), **body})
                if "coffrets" in body:
                    it["coffrets"] = max(0, to_int(body["coffrets"], 0))
                changed = [k for k in ("nom", "categorie", "marque", "lot", "peremption", "coffrets", "entames",
                                       "flacons", "prix", "code_barres", "emplacement") if str(old[k] or "") != str(it[k] if it[k] is not None else "")]
                verified = 0 if body.get("valider", True) else old["a_verifier"]
                nom_prop = 0 if body.get("valider", True) else old["nom_propose"]
                CON.execute("UPDATE products SET nom=?,categorie=?,marque=?,lot=?,peremption=?,coffrets=?,entames=?,flacons=?,"
                            "prix=?,code_barres=?,emplacement=?,a_verifier=?,nom_propose=?,source=?,updated=? WHERE id=?",
                            (it["nom"], it["categorie"], it["marque"], it["lot"], it["peremption"], it["coffrets"], it["entames"],
                             it["flacons"], it["prix"], it["code_barres"], it["emplacement"], verified, nom_prop,
                             "manuel" if "flacons" in changed else old["source"], now(), pid))
                if changed:
                    add_history(pid, "Modifié sur le PC : " + ", ".join(changed))
                elif old["a_verifier"] and not verified:
                    add_history(pid, "Vérifié sur le PC")
                CON.commit(); bump()
            return self.send(200, {"ok": True})
        except Exception as e:
            return self.err(500, str(e))

    def do_DELETE(self):
        with LOCK:
            return self._do_DELETE()

    def _do_DELETE(self):
        path = urlparse(self.path).path
        if not self.is_local():
            return self.err(403, "réservé au PC")
        m = re.fullmatch(r"/api/products/(\d+)", path)
        if m:
            with LOCK:
                delete_product(int(m.group(1)))
            return self.send(200, {"ok": True})
        m = re.fullmatch(r"/api/devices/(\d+)", path)
        if m:
            CON.execute("DELETE FROM devices WHERE id=?", (int(m.group(1)),)); CON.commit(); bump()
            return self.send(200, {"ok": True})
        return self.err(404, "introuvable")

    # -- views
    def state(self):
        cleanup_pairings()
        ip = lan_ip()
        url = f"http://{ip}:{PORT}/app"
        prods = [row_to_product(r) for r in CON.execute("SELECT * FROM products ORDER BY updated DESC")]
        recs = []
        for r in CON.execute("SELECT * FROM receptions ORDER BY id DESC LIMIT 15"):
            recs.append({"id": r["id"], "device": r["device"], "ts": r["ts"], "undone": bool(r["undone"]),
                         "results": json.loads(r["results"])})
        pend = [dict(id=r["id"], name=r["name"], code=r["code"]) for r in
                CON.execute("SELECT * FROM pairings WHERE status='pending' ORDER BY created DESC")]
        devs = [dict(id=r["id"], name=r["name"], version=r["version"], last_seen=r["last_seen"]) for r in
                CON.execute("SELECT * FROM devices ORDER BY id")]
        return {"rev": REV[0], "products": prods, "receptions": recs, "pairings": pend, "devices": devs,
                "pcName": setting("pc_name"), "version": APP_VERSION, "ip": ip, "port": PORT, "installUrl": url,
                "apk": os.path.exists(APK_PATH), "qr": qr_svg(url), "dataDir": DATA_DIR, "now": now()}

    def install_page(self):
        return f"""<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Inventaire Clinique</title><style>body{{margin:0;font-family:system-ui,sans-serif;background:#0E1B1A;color:#EAF2F0;display:flex;min-height:100vh;align-items:center;justify-content:center;padding:24px;box-sizing:border-box}}
.c{{max-width:420px;display:flex;flex-direction:column;gap:16px}}a{{display:block;text-align:center;background:#3DDC97;color:#06231A;font-weight:800;padding:18px;border-radius:16px;text-decoration:none;font-size:18px}}p{{color:#B8CCC8;line-height:1.5;margin:0}}</style></head>
<body><div class="c"><h1 style="margin:0">Inventaire Clinique</h1><p>Installez l'application sur ce téléphone pour envoyer les produits à « {xml_esc(setting('pc_name'))} ».</p>
<a href="/app">Télécharger l'application</a><p>Ensuite, touchez le fichier Inventaire.apk, autorisez l'installation puis Installer. À l'ouverture, l'application trouve ce PC toute seule.</p></div></body></html>"""


def main():
    threading.Thread(target=discovery_loop, daemon=True).start()
    threading.Thread(target=backup_loop, daemon=True).start()
    try:
        srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    except OSError:
        print("Le programme est déjà ouvert. Ouverture de l'interface…")
        webbrowser.open(f"http://localhost:{PORT}")
        time.sleep(3)
        return
    ip = lan_ip()
    print("=" * 60)
    print("  INVENTAIRE CLINIQUE  —  version", APP_VERSION)
    print("=" * 60)
    print(f"  Interface sur ce PC   : http://localhost:{PORT}")
    print(f"  Adresse pour le Wi-Fi : http://{ip}:{PORT}")
    print(f"  Données               : {DATA_DIR}")
    print("  Laissez cette fenêtre ouverte pendant l'inventaire.")
    print("  Si Windows le demande, autorisez l'accès au réseau privé.")
    print("=" * 60)
    if "--no-browser" not in sys.argv:
        threading.Timer(1.0, lambda: webbrowser.open(f"http://localhost:{PORT}")).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
