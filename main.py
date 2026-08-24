import os
import re  # 👈 Kisbetűs 'i'! (A nagy 'I' SyntaxError-t dobna)
from datetime import datetime
from typing import Optional
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx
import psycopg2
from zoneinfo import ZoneInfo

# 1. Lekérjük a pontos magyarországi időt (UTC+1 / UTC+2 automatikus nyári/téli időszámítással)
hungarian_time = datetime.now(ZoneInfo("Europe/Budapest"))

# 2. Formázzuk a kívánt alakúra az adatbázisba mentéshez
formatted_timestamp = hungarian_time.strftime("%Y-%m-%d %H:%M:%S")

app = FastAPI(title="InfoTanár MI Szerver - Render")

# 🌐 CORS engedélyezése (hogy bármilyen kliens elérhesse)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 🔑 API KULCSOK ÉS DATABASE URL A RENDER KÖRNYEZETI VÁLTOZÓIBÓL
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
DATABASE_URL = os.getenv("DATABASE_URL", "") # Render automatikusan kitölti

# 🗄️ ADATBÁZIS CSATLAKOZÁS (PostgreSQL / SQLite fallback)
def get_db_connection():
    if DATABASE_URL:
        # PostgreSQL (Render)
        return psycopg2.connect(DATABASE_URL)
    else:
        # Helyi tesztelésnél (ha nincs Postgres)
        import sqlite3
        return sqlite3.connect("naplo.db")

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Értékelések tábla
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS ertekelesek (
            id SERIAL PRIMARY KEY,
            timestamp TEXT,
            diak_nev TEXT,
            osztaly TEXT,
            technologia TEXT,
            mod TEXT,
            pontszam TEXT,
            diak_kod TEXT,
            ai_valasz TEXT
        )
    ''')
    
    # Auto-módosítás az ertekelesek táblához
    cursor.execute("ALTER TABLE ertekelesek ADD COLUMN IF NOT EXISTS feladat TEXT;")

    # ÚJ: Dolgozatok tábla létrehozása
    # SQLite és PostgreSQL kompatibilis adattípusokkal
    if DATABASE_URL:
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS dolgozat (
                id SERIAL PRIMARY KEY,
                osztaly INT,
                technologia TEXT,
                nehezseg INT,
                feladat TEXT,
                datum_ido TEXT,
                hossz INT
            )
        ''')
    else:
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS dolgozat (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                osztaly INTEGER,
                technologia TEXT,
                nehezseg INTEGER,
                feladat TEXT,
                datum_ido TEXT,
                hossz INTEGER
            )
        ''')
    
    conn.commit()
    conn.close()

try:
    init_db()
except Exception as e:
    print(" Adatbázis inicializálási hiba:", e)

class EvaluationRequest(BaseModel):
    diak_nev: str
    osztaly: str
    technologia: str
    mod: str
    feladat: str
    kod: str
    preferalt_motor: Optional[str] = "groq"

EvaluationRequest.model_rebuild()

# ==========================================
# DOLGOZAT MENTÉSI ADATMODELL ÉS VÉGPONT
# ==========================================
class ExamCreateRequest(BaseModel):
    osztaly: int
    technologia: str
    nehezseg: int
    feladat: str
    datum_ido: str
    hossz: int

@app.post("/api/create-exam")
async def create_exam(req: ExamCreateRequest):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # PostgreSQL placeholder: %s | SQLite placeholder: %s (psycopg2) vagy ?
        placeholder = "%s" if DATABASE_URL else "?"
        query = f'''
            INSERT INTO dolgozat (osztaly, technologia, nehezseg, feladat, datum_ido, hossz)
            VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder})
        '''
        
        cursor.execute(query, (req.osztaly, req.technologia, req.nehezseg, req.feladat, req.datum_ido, req.hossz))
        conn.commit()
        conn.close()

        return {"status": "success", "message": "Dolgozat sikeresen elmentve!"}
    except Exception as e:
        print("Hiba a dolgozat mentésekor:", e)
        raise HTTPException(status_code=500, detail=f"Adatbázis hiba: {str(e)}")

# ==========================================
# 2. FELADAT GENERÁLÁSI VÉGPONT
# ==========================================
class TaskRequest(BaseModel):
    language: str
    difficulty: int = 3
    mode: str = "gyakorlas"
    time_limit: str = "nincs időkorlát"
    preferalt_motor: str = "groq"  # Opció: "groq" vagy "gemini"

@app.post("/api/generate-task")
async def generate_ai_task(req: TaskRequest):
    sys_prompt = (
        "Egy informatika tanár segítője vagy. A feladatod rövid, egyértelmű, magyar nyelvű "
        "programozási feladatok generálása diákok számára. CSAK ÉS KIZÁRÓLAG a feladat leírását "
        "add vissza! Ne írj bevezetőt, üdvözlést, magyarázatot vagy kódblokkot sem."
    )

    user_prompt = f"""
    Hozz létre egy programozási feladatot diákok számára az alábbi paraméterek alapján:
    - Programozási nyelv / Technológia: {req.language}
    - Nehézségi szint: {req.difficulty} / 5
    - Mód: {req.mode}
    - Rendelkezésre álló idő: {req.time_limit}

    Követelmények:
    - A feladat hossza és nehézsége pontosan igazodjon a(z) {req.difficulty}/5 szinthez.
    - Ha a mód Dolgozat, a feladat terjedelme kényelmesen megoldható legyen a megadott idő alatt ({req.time_limit}).
    """

    ai_response = await call_ai(sys_prompt, user_prompt, req.preferalt_motor)
    
    return {"task": ai_response}

async def call_ai(system_prompt: str, user_prompt: str, engine: str = "groq") -> str:
    async with httpx.AsyncClient(timeout=40.0) as client:
        # 1. Groq Hívás
        if engine == "groq" and GROQ_API_KEY:
            url = "https://api.groq.com/openai/v1/chat/completions"
            headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
            payload = {
                "model": "openai/gpt-oss-120b",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                "temperature": 0.2
            }
            res = await client.post(url, json=payload, headers=headers)
            if res.status_code == 200:
                return res.json()["choices"][0]["message"]["content"]
            
        # 2. Gemini Hívás (ha Groq nem volt vagy hibát dobott)
        if GEMINI_API_KEY:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
            payload = {
                "system_instruction": {"parts": [{"text": system_prompt}]},
                "contents": [{"parts": [{"text": user_prompt}]}]
            }
            res = await client.post(url, json=payload)
            if res.status_code == 200:
                return res.json()["candidates"][0]["content"]["parts"][0]["text"]

        raise HTTPException(status_code=500, detail="Nincs érvényes API kulcs beállítva a szerveren!")

@app.get("/")
def home():
    return {"status": "ok", "message": "InfoTanár MI Szerver fut a Render-en!"}

@app.post("/api/evaluate")
async def evaluate_code(req: EvaluationRequest):
    conn = get_db_connection()
    cursor = conn.cursor()

    # Csalás elleni ellenőrzés dolgozat esetén
    if req.mod == "exam":
        cursor.execute("SELECT id FROM ertekelesek WHERE diak_nev = %s AND mod = 'exam'", (req.diak_nev,))
        if cursor.fetchone():
            conn.close()
            raise HTTPException(status_code=400, detail="Ezzel a névvel már küldtél be dolgozatot!")

    sys_prompt = f"Te egy tanár vagy. Értékeld a kódját {req.technologia} nyelven 10 pontból. Adj rövid hibaelemzést és javított kódot! Pontszám formátuma: PONTSZÁM: X/10"
    user_prompt = f"Feladat: {req.feladat}\nDiák kódja:\n{req.kod}"

    ai_response = await call_ai(sys_prompt, user_prompt, req.preferalt_motor)

    score_match = re.search(r"PONTSZÁM:\s*(\d{1,2}\s*/\s*10)", ai_response, re.IGNORECASE)
    score_text = score_match.group(1) if score_match else "Értékelve"

    now_hu = datetime.now(ZoneInfo("Europe/Budapest")).strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute('''
        INSERT INTO ertekelesek (timestamp, diak_nev, osztaly, technologia, mod, pontszam, diak_kod, ai_valasz, feladat)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    ''', (now_hu, req.diak_nev, req.osztaly, req.technologia, req.mod, score_text, req.kod, ai_response, req.feladat))
    
    conn.commit()
    conn.close()

    return {"status": "success", "score": score_text, "feedback": ai_response}

@app.get("/api/results")
def get_results():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, timestamp, diak_nev, osztaly, technologia, mod, pontszam, diak_kod, ai_valasz, feladat FROM ertekelesek ORDER BY id DESC")
    rows = cursor.fetchall()
    conn.close()

    return [
        {
            "id": r[0], 
            "timestamp": r[1], 
            "diak_nev": r[2], 
            "osztaly": r[3], 
            "technologia": r[4], 
            "mod": r[5], 
            "pontszam": r[6],
            "diak_kod": r[7],
            "ai_valasz": r[8],
            "feladat": r[9]
        }
        for r in rows
    ]
# ==========================================
# AKTUÁLIS/LEGKÖZELEBBI DOLGOZAT LEKÉRÉSE
# ==========================================
@app.get("/api/get-active-exam")
def get_active_exam(osztaly: int, technologia: str):
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        # A megadott osztály és technológia alapján lekérjük a dolgozatokat
        cursor.execute('''
            SELECT id, osztaly, technologia, nehezseg, feladat, datum_ido, hossz
            FROM dolgozat
            WHERE osztaly = %s AND technologia = %s
        ''', (osztaly, technologia))
        rows = cursor.fetchall()
        conn.close()

        if not rows:
            raise HTTPException(status_code=404, detail="Nem található dolgozat a megadott osztályhoz és technológiához!")

        # A jelenlegi pontos időpont (Magyarországi időzóna szerint)
        now = datetime.now(ZoneInfo("Europe/Budapest"))

        best_exam = None
        min_diff = float('inf')

        # Megkeressük azt a dolgozatot, aminek a datum_ido értéke a legközelebb áll a jelenlegi időhöz
        for r in rows:
            try:
                # ISO formátum parse-olása (pl. "2026-08-24T10:00")
                exam_dt = datetime.fromisoformat(r[5])
                if exam_dt.tzinfo is None:
                    exam_dt = exam_dt.replace(tzinfo=ZoneInfo("Europe/Budapest"))
                
                diff = abs((exam_dt - now).total_seconds())
                if diff < min_diff:
                    min_diff = diff
                    best_exam = {
                        "id": r[0],
                        "osztaly": r[1],
                        "technologia": r[2],
                        "nehezseg": r[3],
                        "feladat": r[4],
                        "datum_ido": r[5],
                        "hossz": r[6],
                    }
            except Exception as parse_err:
                print("Dátum parszolási hiba egy sornál:", parse_err)
                continue

        if not best_exam:
            raise HTTPException(status_code=404, detail="Nem sikerült érvényes dolgozatot azonosítani.")

        return best_exam

    except HTTPException:
        raise
    except Exception as e:
        conn.close()
        raise HTTPException(status_code=500, detail=f"Adatbázis hiba: {str(e)}")
    
@app.get("/tanar")
def read_teacher_page():
    file_path = os.path.join(os.path.dirname(__file__), "tanar.html")
    if os.path.exists(file_path):
        return FileResponse(file_path)
    return HTMLResponse("<h2>Hiba: A tanar.html nem található a szerveren!</h2>", status_code=404)
