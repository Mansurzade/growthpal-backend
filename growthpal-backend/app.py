"""
GrowthPal Deploy — app.py
Flask backend for Render deployment
PostgreSQL database
"""

import os
import jwt
import psycopg2
import psycopg2.extras
import hashlib
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, request, jsonify
from flask_cors import CORS
import google.generativeai as genai

app = Flask(__name__)
CORS(app, origins="*")

# ── CONFIG ────────────────────────────────────────────────────
JWT_SECRET  = os.environ.get("JWT_SECRET", "growthpal-deploy-secret-2025")
JWT_EXPIRE  = 60 * 60 * 24 * 30   # 30 gün
DATABASE_URL = os.environ.get("DATABASE_URL", "")
GEMINI_KEY   = os.environ.get("GEMINI_API_KEY", "")

# Gemini
try:
    genai.configure(api_key=GEMINI_KEY)
    gemini = genai.GenerativeModel("gemini-2.0-flash-lite")
except:
    gemini = None

DEFAULT_TAGS = [
    {"name": "Work",    "key": "working", "description": "Deep work sessions — projects, coding, tasks", "weekly_goal_hours": 0, "fruit": "pear"},
    {"name": "Reading", "key": "reading", "description": "Focused reading and research",                  "weekly_goal_hours": 0, "fruit": "strawberry"},
    {"name": "Drawing", "key": "drawing", "description": "Creative work — art, design, sketching",        "weekly_goal_hours": 0, "fruit": "apple"},
]

# ── DATABASE ──────────────────────────────────────────────────
def get_db():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            camera_enabled BOOLEAN DEFAULT TRUE
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS tags (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            key TEXT NOT NULL,
            description TEXT DEFAULT '',
            weekly_goal_hours REAL DEFAULT 0,
            fruit TEXT DEFAULT 'pear'
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            tag_id INTEGER REFERENCES tags(id),
            started_at TIMESTAMPTZ NOT NULL,
            ended_at TIMESTAMPTZ,
            duration_sec INTEGER DEFAULT 0,
            presence_pct REAL DEFAULT 100,
            focus_min REAL DEFAULT 0,
            fruit_size TEXT,
            fruit_count INTEGER DEFAULT 0,
            is_complete BOOLEAN DEFAULT FALSE
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS active_sessions (
            id SERIAL PRIMARY KEY,
            user_id INTEGER UNIQUE REFERENCES users(id) ON DELETE CASCADE,
            session_id INTEGER REFERENCES sessions(id),
            tag_id INTEGER REFERENCES tags(id),
            tag_name TEXT,
            tag_key TEXT,
            started_at TIMESTAMPTZ NOT NULL,
            duration_sec INTEGER DEFAULT 1500
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS user_level (
            user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            level INTEGER DEFAULT 1,
            total_spots INTEGER DEFAULT 0,
            streak_days INTEGER DEFAULT 0,
            last_session_date DATE,
            credit_working REAL DEFAULT 0,
            credit_reading REAL DEFAULT 0,
            credit_drawing REAL DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()

# ── AUTH ──────────────────────────────────────────────────────
def hash_pw(pw): return hashlib.sha256(pw.encode()).hexdigest()

def make_token(user_id):
    return jwt.encode(
        {"user_id": user_id, "exp": datetime.utcnow() + timedelta(seconds=JWT_EXPIRE)},
        JWT_SECRET, algorithm="HS256"
    )

def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get("Authorization","").replace("Bearer ","")
        if not token:
            return jsonify({"error": "Unauthorized"}), 401
        try:
            payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
            request.user_id = payload["user_id"]
        except:
            return jsonify({"error": "Invalid token"}), 401
        return f(*args, **kwargs)
    return decorated

# ── AUTH ENDPOINTS ────────────────────────────────────────────
@app.route("/api/auth/signup", methods=["POST"])
def signup():
    d = request.get_json()
    username = (d.get("username","")).strip().lower()
    password = d.get("password","")
    if len(username) < 3 or len(password) < 6:
        return jsonify({"error": "Username min 3, password min 6 characters"}), 400
    conn = get_db(); c = conn.cursor()
    try:
        c.execute("INSERT INTO users (username,password) VALUES (%s,%s) RETURNING id",
                  (username, hash_pw(password)))
        user_id = c.fetchone()["id"]
        for tag in DEFAULT_TAGS:
            c.execute("""INSERT INTO tags (user_id,name,key,description,weekly_goal_hours,fruit)
                         VALUES (%s,%s,%s,%s,%s,%s)""",
                      (user_id, tag["name"], tag["key"],
                       tag["description"], tag["weekly_goal_hours"], tag["fruit"]))
        c.execute("INSERT INTO user_level (user_id) VALUES (%s)", (user_id,))
        conn.commit()
        return jsonify({"token": make_token(user_id), "username": username}), 201
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        return jsonify({"error": "Username already taken"}), 409
    finally:
        conn.close()

@app.route("/api/auth/login", methods=["POST"])
def login():
    d = request.get_json()
    username = (d.get("username","")).strip().lower()
    password = d.get("password","")
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT id,username FROM users WHERE username=%s AND password=%s",
              (username, hash_pw(password)))
    user = c.fetchone()
    conn.close()
    if not user:
        return jsonify({"error": "Invalid username or password"}), 401
    return jsonify({"token": make_token(user["id"]), "username": user["username"]})

@app.route("/api/auth/me", methods=["GET"])
@require_auth
def me():
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT id,username FROM users WHERE id=%s", (request.user_id,))
    user = c.fetchone(); conn.close()
    if not user: return jsonify({"error": "Not found"}), 404
    return jsonify(dict(user))

# ── TAGS ──────────────────────────────────────────────────────
@app.route("/api/tags", methods=["GET"])
@require_auth
def get_tags():
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT * FROM tags WHERE user_id=%s ORDER BY id", (request.user_id,))
    tags = c.fetchall(); conn.close()
    return jsonify([dict(t) for t in tags])

@app.route("/api/tags", methods=["POST"])
@require_auth
def add_tag():
    d = request.get_json()
    conn = get_db(); c = conn.cursor()
    c.execute("""INSERT INTO tags (user_id,name,key,description,weekly_goal_hours,fruit)
                 VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""",
              (request.user_id, d["name"], d.get("key","custom"),
               d.get("description",""), float(d.get("weekly_goal_hours",0)),
               d.get("fruit","pear")))
    tag_id = c.fetchone()["id"]
    conn.commit(); conn.close()
    return jsonify({"id": tag_id, "ok": True}), 201

@app.route("/api/tags/<int:tag_id>", methods=["PUT"])
@require_auth
def update_tag(tag_id):
    d = request.get_json()
    conn = get_db(); c = conn.cursor()
    c.execute("""UPDATE tags SET name=%s, description=%s, weekly_goal_hours=%s
                 WHERE id=%s AND user_id=%s""",
              (d["name"], d.get("description",""),
               float(d.get("weekly_goal_hours",0)), tag_id, request.user_id))
    conn.commit(); conn.close()
    return jsonify({"ok": True})

# ── SESSIONS ──────────────────────────────────────────────────
@app.route("/api/sessions/start", methods=["POST"])
@require_auth
def session_start():
    d = request.get_json()
    tag_id   = int(d.get("tag_id"))
    duration = int(d.get("duration_sec", 1500))
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT * FROM tags WHERE id=%s AND user_id=%s", (tag_id, request.user_id))
    tag = c.fetchone()
    if not tag:
        conn.close()
        return jsonify({"error": "Tag not found"}), 404
    now = datetime.utcnow()
    c.execute("""INSERT INTO sessions (user_id,tag_id,started_at,presence_pct,is_complete)
                 VALUES (%s,%s,%s,100,FALSE) RETURNING id""",
              (request.user_id, tag_id, now))
    session_id = c.fetchone()["id"]
    c.execute("""INSERT INTO active_sessions (user_id,session_id,tag_id,tag_name,tag_key,started_at,duration_sec)
                 VALUES (%s,%s,%s,%s,%s,%s,%s)
                 ON CONFLICT (user_id) DO UPDATE SET
                   session_id=EXCLUDED.session_id, tag_id=EXCLUDED.tag_id,
                   tag_name=EXCLUDED.tag_name, tag_key=EXCLUDED.tag_key,
                   started_at=EXCLUDED.started_at, duration_sec=EXCLUDED.duration_sec""",
              (request.user_id, session_id, tag_id, tag["name"], tag["key"], now, duration))
    conn.commit(); conn.close()
    return jsonify({"session_id": session_id, "tag_name": tag["name"], "tag_key": tag["key"]})

@app.route("/api/sessions/end", methods=["POST"])
@require_auth
def session_end():
    d = request.get_json()
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT * FROM active_sessions WHERE user_id=%s", (request.user_id,))
    active = c.fetchone()
    if not active:
        conn.close()
        return jsonify({"error": "No active session"}), 404
    now = datetime.utcnow()
    started = active["started_at"]
    duration_sec = int((now - started).total_seconds())
    focus_min = duration_sec / 60   # No camera → 100% presence
    # Fruit calc
    if focus_min >= 30:   fruit_size, spots = "big", 3
    elif focus_min >= 20: fruit_size, spots = "medium", 2
    elif focus_min >= 10: fruit_size, spots = "small", 1
    else:                 fruit_size, spots = None, 0
    c.execute("""UPDATE sessions SET ended_at=%s, duration_sec=%s, presence_pct=100,
                 focus_min=%s, fruit_size=%s, fruit_count=%s, is_complete=TRUE
                 WHERE id=%s""",
              (now, duration_sec, round(focus_min,2), fruit_size, 1 if fruit_size else 0,
               active["session_id"]))
    c.execute("DELETE FROM active_sessions WHERE user_id=%s", (request.user_id,))
    # Update level
    _update_level(c, request.user_id, spots)
    _update_streak(c, request.user_id, now.date())
    conn.commit(); conn.close()
    return jsonify({"fruit_size": fruit_size, "spots_earned": spots,
                    "focus_min": round(focus_min,2)})

@app.route("/api/sessions/active", methods=["GET"])
@require_auth
def get_active():
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT * FROM active_sessions WHERE user_id=%s", (request.user_id,))
    active = c.fetchone(); conn.close()
    if not active: return jsonify({"active": False})
    return jsonify({"active": True, **dict(active)})

# ── STATS ─────────────────────────────────────────────────────
@app.route("/api/stats", methods=["GET"])
@require_auth
def stats():
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT * FROM user_level WHERE user_id=%s", (request.user_id,))
    lvl = c.fetchone() or {}
    c.execute("""SELECT s.*, t.name as tag_name, t.key as tag_key
                 FROM sessions s JOIN tags t ON s.tag_id=t.id
                 WHERE s.user_id=%s AND s.is_complete=TRUE
                 ORDER BY s.started_at DESC LIMIT 50""", (request.user_id,))
    sessions = c.fetchall()
    week_ago = datetime.utcnow() - timedelta(days=7)
    c.execute("""SELECT COALESCE(SUM(focus_min)/60,0) as wh FROM sessions
                 WHERE user_id=%s AND started_at>=%s AND is_complete=TRUE""",
              (request.user_id, week_ago))
    weekly = c.fetchone()
    conn.close()
    return jsonify({
        "level": dict(lvl).get("level",1),
        "total_spots": dict(lvl).get("total_spots",0),
        "streak_days": dict(lvl).get("streak_days",0),
        "weekly_focus_hours": round(float(weekly["wh"]),2),
        "recent_sessions": [dict(s) for s in sessions]
    })

@app.route("/api/leaderboard", methods=["GET"])
@require_auth
def leaderboard():
    week_ago = datetime.utcnow() - timedelta(days=7)
    conn = get_db(); c = conn.cursor()
    c.execute("""
        SELECT u.username, ul.level, ul.streak_days,
               COALESCE((SELECT SUM(focus_min)/60 FROM sessions s
                         WHERE s.user_id=u.id AND s.started_at>=%s
                         AND s.is_complete=TRUE),0) as weekly_hours
        FROM users u JOIN user_level ul ON u.id=ul.user_id
        ORDER BY (ul.level*10 + ul.streak_days*0.75) DESC LIMIT 50
    """, (week_ago,))
    rows = c.fetchall(); conn.close()
    result = []
    for i,r in enumerate(rows):
        r = dict(r)
        r["rank"] = i+1
        r["xal"] = round(r["level"]*10 + r["streak_days"]*0.75 + float(r["weekly_hours"])*5, 1)
        result.append(r)
    return jsonify(result)

@app.route("/api/settings", methods=["PUT"])
@require_auth
def update_settings():
    d = request.get_json()
    conn = get_db(); c = conn.cursor()
    c.execute("UPDATE users SET camera_enabled=%s WHERE id=%s",
              (bool(d.get("camera_enabled",True)), request.user_id))
    conn.commit(); conn.close()
    return jsonify({"ok": True})

# ── GEMINI CHAT ───────────────────────────────────────────────
SYSTEM_PROMPT = """You are GrowthPal's personal AI assistant (Harvest Bot). You know the user deeply — their habits, patterns, past sessions, level, and streak. You speak directly and naturally, like a friend who actually pays attention and happens to know their stats. Keep responses to 2-4 sentences. No excessive punctuation or emoji. Always respond in the same language the user writes in."""

@app.route("/api/chat", methods=["POST"])
@require_auth
def chat():
    if not gemini:
        return jsonify({"error": "AI not available"}), 503
    d = request.get_json()
    message = d.get("message","").strip()
    if not message: return jsonify({"error": "Empty message"}), 400
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT * FROM user_level WHERE user_id=%s", (request.user_id,))
    lvl = dict(c.fetchone() or {})
    c.execute("SELECT username FROM users WHERE id=%s", (request.user_id,))
    user = c.fetchone()
    week_ago = datetime.utcnow() - timedelta(days=7)
    c.execute("""SELECT s.focus_min, s.presence_pct, t.name as tag_name, s.started_at
                 FROM sessions s JOIN tags t ON s.tag_id=t.id
                 WHERE s.user_id=%s AND s.is_complete=TRUE
                 ORDER BY s.started_at DESC LIMIT 5""", (request.user_id,))
    recent = c.fetchall(); conn.close()
    context = f"User: {user['username'] if user else 'user'}\n"
    context += f"Level: {lvl.get('level',1)}, Streak: {lvl.get('streak_days',0)} days\n"
    context += "Recent sessions:\n"
    for s in recent:
        s = dict(s)
        context += f"- {str(s['started_at'])[:10]}: {s['tag_name']}, {round(s['focus_min'],1)} min focus, {round(s['presence_pct'],1)}% presence\n"
    try:
        resp = gemini.generate_content(f"{SYSTEM_PROMPT}\n\nUser data:\n{context}\n\nMessage: {message}")
        return jsonify({"reply": resp.text.strip()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── HELPERS ───────────────────────────────────────────────────
def _update_level(c, user_id, new_spots):
    c.execute("SELECT total_spots FROM user_level WHERE user_id=%s", (user_id,))
    row = c.fetchone()
    if not row: return
    total = row["total_spots"] + new_spots
    level = 1
    acc = 0
    while total >= acc + level * 10:
        acc += level * 10
        level += 1
    c.execute("UPDATE user_level SET total_spots=%s, level=%s WHERE user_id=%s",
              (total, level, user_id))

def _update_streak(c, user_id, today):
    c.execute("SELECT streak_days, last_session_date FROM user_level WHERE user_id=%s", (user_id,))
    row = c.fetchone()
    if not row: return
    streak = row["streak_days"]
    last   = row["last_session_date"]
    if last is None:
        streak = 1
    else:
        diff = (today - last).days
        if diff == 0:   pass
        elif diff == 1: streak += 1
        elif diff == 2: pass
        else:           streak = 1
    c.execute("UPDATE user_level SET streak_days=%s, last_session_date=%s WHERE user_id=%s",
              (streak, today, user_id))

# ── INIT & RUN ────────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
