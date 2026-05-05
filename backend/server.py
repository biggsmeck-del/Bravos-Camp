from fastapi import FastAPI, APIRouter, HTTPException, Depends, UploadFile, File, Header, Response
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import uuid
import logging
import mimetypes
import requests
from pathlib import Path
from pydantic import BaseModel, Field, ConfigDict
from typing import List, Optional, Dict, Any
from datetime import datetime, timezone, timedelta
import jwt

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# Mongo
mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

# Config
JWT_SECRET = os.environ['JWT_SECRET']
JWT_ALGO = "HS256"
ADMIN_EMAIL = os.environ['ADMIN_EMAIL']
ADMIN_PASSWORD = os.environ['ADMIN_PASSWORD']

# Local file storage directory (configurable via env)
UPLOAD_DIR = Path(os.environ.get('UPLOAD_DIR', str(ROOT_DIR / 'uploads'))).resolve()
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

MAX_UPLOAD_MB = int(os.environ.get('MAX_UPLOAD_MB', '100'))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024

# App
app = FastAPI()
api_router = APIRouter(prefix="/api")

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


# ---- Models ----
class LoginBody(BaseModel):
    email: str
    password: str

class Button(BaseModel):
    id: str
    label: str
    url: str
    bg: str
    text_color: str
    hover: str

class SiteConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    title: str
    hero_text: str
    logo_url: str
    video_url: str
    overlay_opacity: float
    footer_text: str
    background_color: str
    text_color: str
    buttons: List[Button]
    discord_guild_id: Optional[str] = ""

class SiteConfigUpdate(BaseModel):
    title: Optional[str] = None
    hero_text: Optional[str] = None
    logo_url: Optional[str] = None
    video_url: Optional[str] = None
    overlay_opacity: Optional[float] = None
    footer_text: Optional[str] = None
    background_color: Optional[str] = None
    text_color: Optional[str] = None
    buttons: Optional[List[Button]] = None
    discord_guild_id: Optional[str] = None

# Default site config
DEFAULT_CONFIG: Dict[str, Any] = {
    "title": "BRAVOS CAMP",
    "hero_text": "O melhor servidor de ranks, batalhas e comunidade! Venha fazer parte da família e mostrar seu valor.",
    "logo_url": "/logo.png",
    "video_url": "https://cdn.pixabay.com/video/2021/08/04/83864-585141042_tiny.mp4",
    "overlay_opacity": 0.65,
    "footer_text": "© 2026 BRAVOS CAMP. Todos os direitos reservados.",
    "background_color": "#050508",
    "text_color": "#FFFFFF",
    "buttons": [
        {"id": "discord", "label": "Entrar no Discord", "url": "#", "bg": "#5865F2", "text_color": "#FFFFFF", "hover": "#4752C4"},
        {"id": "whatsapp", "label": "Grupo do WhatsApp", "url": "#", "bg": "#25D366", "text_color": "#FFFFFF", "hover": "#20BD5A"},
        {"id": "times", "label": "Conhecer os Times", "url": "#", "bg": "#FBBF24", "text_color": "#000000", "hover": "#F59E0B"},
        {"id": "patentes", "label": "Ver as Patentes", "url": "#", "bg": "linear-gradient(135deg, #9333EA 0%, #E11D48 100%)", "text_color": "#FFFFFF", "hover": "linear-gradient(135deg, #7E22CE 0%, #BE123C 100%)"},
        {"id": "bot", "label": "Comandos do Bot", "url": "#", "bg": "#14B8A6", "text_color": "#000000", "hover": "#0D9488"},
    ],
    "discord_guild_id": "",
}

# ---- Auth helpers ----
def create_token(email: str) -> str:
    payload = {
        "sub": email,
        "role": "admin",
        "exp": datetime.now(timezone.utc) + timedelta(days=7),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGO)

def require_admin(authorization: Optional[str] = Header(None)) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing token")
    token = authorization.split(" ", 1)[1]
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid token")
    if payload.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Forbidden")
    return payload["sub"]

# ---- Startup ----
@app.on_event("startup")
async def on_startup():
    existing = await db.site_config.find_one({"_id": "main"})
    if not existing:
        doc = {"_id": "main", **DEFAULT_CONFIG}
        await db.site_config.insert_one(doc)
        logger.info("Seeded default site config")
    logger.info(f"Upload directory: {UPLOAD_DIR}")

# ---- Routes ----
@api_router.get("/")
async def root():
    return {"message": "BRAVOS CAMP API"}

@api_router.post("/auth/login")
async def login(body: LoginBody):
    if body.email.strip().lower() != ADMIN_EMAIL.lower() or body.password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Credenciais inválidas")
    token = create_token(ADMIN_EMAIL)
    return {"access_token": token, "token_type": "bearer", "email": ADMIN_EMAIL}

@api_router.get("/auth/me")
async def me(email: str = Depends(require_admin)):
    return {"email": email, "role": "admin"}

@api_router.get("/config")
async def get_config():
    doc = await db.site_config.find_one({"_id": "main"}, {"_id": 0})
    if not doc:
        return DEFAULT_CONFIG
    return doc

@api_router.put("/config")
async def update_config(update: SiteConfigUpdate, email: str = Depends(require_admin)):
    patch = {k: v for k, v in update.model_dump(exclude_unset=True).items() if v is not None}
    if not patch:
        raise HTTPException(status_code=400, detail="Nada para atualizar")
    await db.site_config.update_one({"_id": "main"}, {"$set": patch}, upsert=True)
    doc = await db.site_config.find_one({"_id": "main"}, {"_id": 0})
    return doc

@api_router.post("/upload")
async def upload(file: UploadFile = File(...), email: str = Depends(require_admin)):
    ext = file.filename.split(".")[-1].lower() if "." in file.filename else "bin"
    # Basic allowlist for safety
    allowed = {"png", "jpg", "jpeg", "gif", "webp", "svg", "mp4", "webm", "ogg", "mov"}
    if ext not in allowed:
        raise HTTPException(status_code=400, detail=f"Extensão não permitida: .{ext}")

    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"Arquivo maior que {MAX_UPLOAD_MB}MB")

    file_id = str(uuid.uuid4())
    filename = f"{file_id}.{ext}"
    dest = UPLOAD_DIR / filename
    with open(dest, "wb") as f:
        f.write(data)

    content_type = file.content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    await db.files.insert_one({
        "id": file_id,
        "filename": filename,
        "original_filename": file.filename,
        "content_type": content_type,
        "size": len(data),
        "is_deleted": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    return {
        "id": file_id,
        "filename": filename,
        "url": f"/api/files/{filename}",
        "content_type": content_type,
        "size": len(data),
    }

@api_router.get("/files/{filename}")
async def download(filename: str):
    # Prevent path traversal
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Nome inválido")
    record = await db.files.find_one({"filename": filename, "is_deleted": False}, {"_id": 0})
    if not record:
        raise HTTPException(status_code=404, detail="Arquivo não encontrado")
    path = UPLOAD_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="Arquivo não encontrado no disco")
    with open(path, "rb") as f:
        data = f.read()
    return Response(content=data, media_type=record.get("content_type", "application/octet-stream"))

@api_router.get("/discord/widget")
async def discord_widget():
    cfg = await db.site_config.find_one({"_id": "main"}, {"_id": 0})
    guild_id = (cfg or {}).get("discord_guild_id") or ""
    guild_id = str(guild_id).strip()
    if not guild_id.isdigit():
        return {"enabled": False, "presence_count": 0, "name": None, "invite_url": None}
    try:
        r = requests.get(f"https://discord.com/api/guilds/{guild_id}/widget.json", timeout=6)
        if r.status_code != 200:
            return {"enabled": False, "presence_count": 0, "name": None, "invite_url": None, "error": f"status {r.status_code}"}
        data = r.json()
        return {
            "enabled": True,
            "presence_count": data.get("presence_count", 0),
            "name": data.get("name"),
            "invite_url": data.get("instant_invite"),
        }
    except Exception as e:
        logger.error(f"Discord widget error: {e}")
        return {"enabled": False, "presence_count": 0, "name": None, "invite_url": None, "error": str(e)}

app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()
