import os
import re
from datetime import datetime
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse  # 👈 Ezt add hozzá!
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx
import psycopg2

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
    # PostgreSQL kompatibilis adattáblázat
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
EvaluationRequest.model_rebuild
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

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute('''
        INSERT INTO ertekelesek (timestamp, diak_nev, osztaly, technologia, mod, pontszam, diak_kod, ai_valasz)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    ''', (now, req.diak_nev, req.osztaly, req.technologia, req.mod, score_text, req.kod, ai_response))
    
    conn.commit()
    conn.close()

    return {"status": "success", "score": score_text, "feedback": ai_response}

@app.get("/api/results")
def get_results():
    conn = get_db_connection()
    cursor = conn.cursor()
    # 💡 Lelkérjük a diak_kod és ai_valasz mezőket is:
    cursor.execute("SELECT id, timestamp, diak_nev, osztaly, technologia, mod, pontszam, diak_kod, ai_valasz FROM ertekelesek ORDER BY id DESC")
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
            "ai_valasz": r[8]
        }
        for r in rows
    ]

@app.get("/tanar")
def read_teacher_page():
    # Megkeressük a tanar.html fájlt a main.py mellett
    file_path = os.path.join(os.path.dirname(__file__), "tanar.html")
    if os.path.exists(file_path):
        return FileResponse(file_path)
    return HTMLResponse("<h2>Hiba: A tanar.html nem található a szerveren!</h2>", status_code=404)
