
from fastapi import FastAPI, Request, Form, UploadFile, File, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from pydantic import BaseModel
from pathlib import Path
import sqlite3, hashlib, secrets, qrcode, io, os, re
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent
# Railway Volume: set QR_FIND_DATA_DIR=/data and mount a persistent volume there.
# Locally, data stays beside app.py.
DATA_DIR = Path(os.getenv("QR_FIND_DATA_DIR", str(ROOT))).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB = DATA_DIR / "qrfind.db"
UPLOADS = DATA_DIR / "uploads"
UPLOADS.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="QR-Find")
secret_key = os.getenv("QR_FIND_SECRET")
if not secret_key:
    # Safe local-development fallback; Railway must define QR_FIND_SECRET.
    if os.getenv("RAILWAY_ENVIRONMENT"):
        raise RuntimeError("Set QR_FIND_SECRET in Railway Variables")
    secret_key = "local-development-secret-change-before-deployment"
app.add_middleware(
    SessionMiddleware,
    secret_key=secret_key,
    same_site="lax",
    https_only=bool(os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("RAILWAY_PUBLIC_DOMAIN")),
)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
app.mount("/uploads", StaticFiles(directory=UPLOADS), name="uploads")

def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(
          id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, email TEXT UNIQUE NOT NULL,
          password_hash TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS items(
          id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          token TEXT UNIQUE NOT NULL, title TEXT NOT NULL, description TEXT DEFAULT '',
          photo TEXT DEFAULT '', status TEXT NOT NULL DEFAULT 'active',
          created_at TEXT NOT NULL, found_at TEXT DEFAULT '', found_place TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS messages(
          id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
          finder_name TEXT NOT NULL, message TEXT NOT NULL, place TEXT DEFAULT '', found_time TEXT DEFAULT '',
          created_at TEXT NOT NULL, is_read INTEGER NOT NULL DEFAULT 0
        );
        """)
init_db()

def now():
    return datetime.now().astimezone().isoformat(timespec="minutes")

def pw_hash(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return salt.hex() + "$" + digest.hex()

def pw_check(password, stored):
    try:
        salt, digest = stored.split("$", 1)
        return secrets.compare_digest(hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1).hex(), digest)
    except Exception:
        return False

def current_user(request):
    uid = request.session.get("uid")
    if not uid: return None
    with db() as c:
        return c.execute("SELECT id,name,email FROM users WHERE id=?", (uid,)).fetchone()

def require_user(request):
    user = current_user(request)
    if not user: raise HTTPException(401, "Войдите в аккаунт")
    return user

@app.get("/")
def home():
    return FileResponse(ROOT / "static" / "index.html")

@app.get("/api/me")
def me(request: Request):
    u = current_user(request)
    return {"user": dict(u) if u else None}

@app.post("/api/register")
def register(request: Request, name: str = Form(...), email: str = Form(...), password: str = Form(...)):
    name, email = name.strip(), email.strip().lower()
    if len(name) < 2 or len(password) < 8 or not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise HTTPException(400, "Проверьте имя, электронную почту и пароль (не менее 8 символов)")
    try:
        with db() as c:
            cur = c.execute("INSERT INTO users(name,email,password_hash,created_at) VALUES(?,?,?,?)", (name,email,pw_hash(password),now()))
            request.session["uid"] = cur.lastrowid
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Аккаунт с такой почтой уже существует")
    return {"ok": True}

@app.post("/api/login")
def login(request: Request, email: str = Form(...), password: str = Form(...)):
    with db() as c:
        u = c.execute("SELECT * FROM users WHERE email=?", (email.strip().lower(),)).fetchone()
    if not u or not pw_check(password, u["password_hash"]):
        raise HTTPException(401, "Неверная почта или пароль")
    request.session.clear()
    request.session["uid"] = u["id"]
    return {"ok": True}

@app.post("/api/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}

@app.get("/api/items")
def items(request: Request):
    u = require_user(request)
    with db() as c:
        rows = c.execute("""
          SELECT i.*, (SELECT COUNT(*) FROM messages m WHERE m.item_id=i.id) AS messages_count,
          (SELECT COUNT(*) FROM messages m WHERE m.item_id=i.id AND m.is_read=0) AS unread_count
          FROM items i WHERE owner_id=? ORDER BY i.id DESC
        """, (u["id"],)).fetchall()
    return [dict(r) for r in rows]

@app.post("/api/items")
async def create_item(request: Request, title: str = Form(...), description: str = Form(""), photo: UploadFile | None = File(None)):
    u = require_user(request)
    title = title.strip()
    if not title: raise HTTPException(400, "Укажите название вещи")
    photo_url = ""
    if photo and photo.filename:
        ext = Path(photo.filename).suffix.lower()
        if ext not in [".jpg",".jpeg",".png",".webp"]: raise HTTPException(400, "Допустимы JPG, PNG и WEBP")
        data = await photo.read()
        if len(data) > 5 * 1024 * 1024: raise HTTPException(400, "Фото должно быть не больше 5 МБ")
        filename = secrets.token_hex(16) + ext
        (UPLOADS / filename).write_bytes(data)
        photo_url = "/uploads/" + filename
    token = secrets.token_urlsafe(12)
    with db() as c:
        cur = c.execute("INSERT INTO items(owner_id,token,title,description,photo,created_at) VALUES(?,?,?,?,?,?)",
                        (u["id"],token,title,description.strip(),photo_url,now()))
        item_id = cur.lastrowid
    return {"ok": True, "id": item_id, "token": token}

@app.patch("/api/items/{item_id}")
async def edit_item(item_id: int, request: Request, title: str = Form(...), description: str = Form(""), photo: UploadFile | None = File(None), remove_photo: str = Form("false")):
    u = require_user(request)
    title = title.strip()
    if not title:
        raise HTTPException(400, "Укажите название вещи")
    with db() as c:
        item = c.execute("SELECT photo FROM items WHERE id=? AND owner_id=?", (item_id, u["id"])).fetchone()
        if not item:
            raise HTTPException(404, "Вещь не найдена")
        photo_url = item["photo"] or ""
        if remove_photo.lower() == "true":
            if photo_url.startswith("/uploads/"):
                (UPLOADS / Path(photo_url).name).unlink(missing_ok=True)
            photo_url = ""
        if photo and photo.filename:
            ext = Path(photo.filename).suffix.lower()
            if ext not in [".jpg", ".jpeg", ".png", ".webp"]:
                raise HTTPException(400, "Допустимы JPG, PNG и WEBP")
            data = await photo.read()
            if len(data) > 5 * 1024 * 1024:
                raise HTTPException(400, "Фото должно быть не больше 5 МБ")
            filename = secrets.token_hex(16) + ext
            (UPLOADS / filename).write_bytes(data)
            if photo_url.startswith("/uploads/"):
                (UPLOADS / Path(photo_url).name).unlink(missing_ok=True)
            photo_url = "/uploads/" + filename
        c.execute("UPDATE items SET title=?, description=?, photo=? WHERE id=? AND owner_id=?",
                  (title, description.strip(), photo_url, item_id, u["id"]))
    return {"ok": True}

@app.delete("/api/items/{item_id}")
def delete_item(item_id: int, request: Request):
    u = require_user(request)
    with db() as c:
        item = c.execute("SELECT photo FROM items WHERE id=? AND owner_id=?", (item_id, u["id"])).fetchone()
        if not item:
            raise HTTPException(404, "Вещь не найдена")
        photo_url = item["photo"] or ""
        c.execute("DELETE FROM items WHERE id=? AND owner_id=?", (item_id, u["id"]))
    if photo_url.startswith("/uploads/"):
        (UPLOADS / Path(photo_url).name).unlink(missing_ok=True)
    return {"ok": True}

@app.get("/api/items/{item_id}/qr")
def item_qr(item_id: int, request: Request):
    u = require_user(request)
    with db() as c:
        item = c.execute("SELECT token FROM items WHERE id=? AND owner_id=?", (item_id,u["id"])).fetchone()
    if not item: raise HTTPException(404, "Вещь не найдена")
    url = str(request.base_url).rstrip("/") + "/f/" + item["token"]
    img = qrcode.make(url)
    buf = io.BytesIO(); img.save(buf, format="PNG"); buf.seek(0)
    from fastapi.responses import StreamingResponse
    return StreamingResponse(buf, media_type="image/png", headers={"Content-Disposition": f'attachment; filename="qr-find-{item_id}.png"'})

@app.get("/api/items/{item_id}/messages")
def get_messages(item_id: int, request: Request):
    u = require_user(request)
    with db() as c:
        item = c.execute("SELECT id FROM items WHERE id=? AND owner_id=?", (item_id,u["id"])).fetchone()
        if not item: raise HTTPException(404, "Вещь не найдена")
        rows = c.execute("SELECT * FROM messages WHERE item_id=? ORDER BY id DESC", (item_id,)).fetchall()
        c.execute("UPDATE messages SET is_read=1 WHERE item_id=?", (item_id,))
    return [dict(r) for r in rows]

@app.patch("/api/items/{item_id}/status")
async def status(item_id: int, request: Request):
    u = require_user(request); data = await request.json()
    new_status = data.get("status")
    if new_status not in ("active","found","returned"): raise HTTPException(400, "Недопустимый статус")
    with db() as c:
        cur = c.execute("UPDATE items SET status=? WHERE id=? AND owner_id=?", (new_status,item_id,u["id"]))
        if cur.rowcount == 0: raise HTTPException(404, "Вещь не найдена")
    return {"ok": True}

@app.get("/f/{token}")
def public_page(token: str):
    return FileResponse(ROOT / "static" / "found.html")

@app.get("/api/public/{token}")
def public_item(token: str):
    with db() as c:
        item = c.execute("SELECT title,description,photo,status FROM items WHERE token=?", (token,)).fetchone()
    if not item: raise HTTPException(404, "QR-код не найден или вещь удалена")
    return dict(item)

@app.post("/api/public/{token}/message")
async def send_message(token: str, request: Request):
    data = await request.json()
    finder = str(data.get("finder_name","")).strip()[:80]
    message = str(data.get("message","")).strip()[:1200]
    place = str(data.get("place","")).strip()[:180]
    found_time = str(data.get("found_time","")).strip()[:80]
    if len(finder) < 2 or len(message) < 5: raise HTTPException(400, "Укажите имя и сообщение")
    with db() as c:
        item = c.execute("SELECT id,status FROM items WHERE token=?", (token,)).fetchone()
        if not item: raise HTTPException(404, "Вещь не найдена")
        if item["status"] == "returned": raise HTTPException(400, "Вещь уже отмечена как возвращённая")
        c.execute("INSERT INTO messages(item_id,finder_name,message,place,found_time,created_at) VALUES(?,?,?,?,?,?)",
                  (item["id"],finder,message,place,found_time,now()))
        c.execute("UPDATE items SET status='found', found_at=?, found_place=? WHERE id=?", (now(),place,item["id"]))
    return {"ok": True}
