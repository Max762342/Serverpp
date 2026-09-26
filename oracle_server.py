"""
Sychos Hub — Oracle Server v5.0
Zentraler Server: Login & Register (50 Start-Credits), Credit-System (2-4 dynamisch),
KI-Chat, Chat-Verwaltung (inkl. Umbenennen), Admin-API (Online-User, Credits vergeben).
Serviert zudem das Frontend:  /        -> Sychos Hub   /admin/  -> Admin-Panel
Nur Python-Stdlib. Start:  python oracle_server.py
"""
import os, sys, json, time, uuid, threading, mimetypes, zlib, base64
import sqlite3, urllib.request, urllib.parse, hashlib, hmac, re, math
from http.server import HTTPServer, BaseHTTPRequestHandler
try:
    from http.server import ThreadingHTTPServer as HTTPServerCls
except ImportError:
    HTTPServerCls = HTTPServer
from urllib.parse import urlparse, unquote

# pythonw (versteckter Always-On-Modus) hat kein stdout/stderr -> auf devnull
if getattr(sys, "stdout", None) is None:
    sys.stdout = open(os.devnull, "w")
if getattr(sys, "stderr", None) is None:
    sys.stderr = open(os.devnull, "w")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOST = os.environ.get("ORACLE_HOST", "0.0.0.0")
PORT = int(os.environ.get("ORACLE_PORT", "7777"))
# Offizielle Sychos-Domains (+ zeneu.de) + kanonische Hauptdomain
SYCHOS_DOMAINS = ("sychos.com", "sychos.de", "sychos.global", "zeneu.de")
SYCHOS_HOME = "https://sychos.com"
DB_PATH = os.environ.get("ORACLE_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "sychos.db"))
APP_VERSION = "2.0.0"   # Sychos-Hub-App-Version (wird via /me und /status ausgeliefert)
# Provider-Keys: fest im Server hinterlegt (kein manuelles Eintragen noetig).
# Env GEMINI_API_KEY / GROQ_API_KEY / CLINE_API_KEY koennen sie bei Bedarf ueberschreiben.
# NIEMALS im Frontend, in Logs oder Fehlermeldungen ausgeben.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "AQ.Ab8RN6JC_fpFIoudZSPJyi0oLHbEpFZXt-PdJPBDobUt76TtOQ")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "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")
CLINE_API_KEY = os.environ.get("CLINE_API_KEY", "sk_4abca032ca41820c202afb587a624186f4db1a3318279994c889e47ea0bb7ee6")
# Echtes Zahlen: Stripe Secret Key (leer = Test-Modus, keine echte Abbuchung)
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")

# ── Merchant of Record (Paddle / Lemon Squeezy): der Anbieter ist der offizielle
#    Verkaeufer und fuehrt ALLE Steuern weltweit ab (USt / VAT / Sales-Tax). ──
def _kv(s):
    return dict(p.split("=", 1) for p in (s or "").split(",") if "=" in p)

PADDLE_CLIENT_TOKEN = os.environ.get("PADDLE_CLIENT_TOKEN", "live_c3597782b58fb3e865fe78fd68e")
PADDLE_WEBHOOK_SECRET = os.environ.get("PADDLE_WEBHOOK_SECRET",
    "pdl_ntfset_01m39q5bhx7fhebrevjn9w1py4_tF6BDSRxpcITbS3jbdrdDll2/DLxKOzG")
PADDLE_PRICES = _kv(os.environ.get("PADDLE_PRICES",
    "basic=pri_01m3818vm4r281eddpyg4a09g1,"
    "pro=pri_01m381a3nb4yfs4vqy4q7c77t9,"
    "max=pri_01m3817vz42r9xn2azg3vc5hkw,"
    "business=pri_01m3816q322wk6jve5jx5vrg9j,"
    "test=pri_01m381373a8gpxz6b2sdxnagxx"))                    # plan=pri_xxx
LEMONSQUEEZY_STORE = os.environ.get("LEMONSQUEEZY_STORE", "")               # Shop-Subdomain
LEMONSQUEEZY_WEBHOOK_SECRET = os.environ.get("LEMONSQUEEZY_WEBHOOK_SECRET", "")
LEMONSQUEEZY_VARIANTS = _kv(os.environ.get("LEMONSQUEEZY_VARIANTS", ""))    # plan=variant_id
PADDLE_LINKS = _kv(os.environ.get("PADDLE_LINKS", ""))    # plan=Payment-Link-URL (direkt zu Paddle)

START_CREDITS = 50.0
START_TOKENS = 25000.0   # Start-Bonus-Tokens fuer neue Accounts
# Passive Gutschrift: alle 7 Stunden bekommen alle User +25 Credits
CREDIT_REGEN_SECONDS = 7 * 3600
CREDIT_REGEN_AMOUNT = 25.0
ONLINE_WINDOW = 300
MAX_MSG_LEN = 8000
MAX_TITLE_LEN = 80
SEND_RATE_LIMIT = 20
LOGIN_RATE_LIMIT = 120     # Login-Versuche pro Minute pro IP (grosszuegig – kein "kann nicht mehr anmelden")
REGISTER_RATE_LIMIT = 12   # Registrierungen pro Minute pro IP (eigener Bucket!)
PWFAIL_LIMIT = 8           # echte Fehlversuche pro Konto ...
PWFAIL_WINDOW = 600        # ... innerhalb 10 Minuten -> dann kurz gesperrt
MAX_ACCOUNTS_PER_IP = int(os.environ.get("MAX_ACCOUNTS_PER_IP", "10"))

# Zentrale Credit-Preise je KI-Stufe (serverseitig verbindlich)
STRENGTHS = {
    "low":    {"name": "Low",    "max_tokens": 512,  "temp": 0.3, "cost": 1},
    "medium": {"name": "Medium", "max_tokens": 2048, "temp": 0.7, "cost": 2},
    "high":   {"name": "High",   "max_tokens": 4096, "temp": 1.0, "cost": 4},
    "extra":  {"name": "Extra",  "max_tokens": 8192, "temp": 1.3, "cost": 6},
}

# KI-Modelle: Anbieter + Kostenfaktor (Preis = Stufen-Cost x Faktor)
MODELS = {
    "gemini-3.6-flash": {"name": "Gemini 3.6 Flash", "provider": "gemini", "factor": 1.0, "vision": True},
    "gemini-2.5-flash": {"name": "Gemini 2.5 Flash", "provider": "gemini", "factor": 1.0, "vision": True},
    "gemini-2.5-lite":  {"name": "Gemini 2.5 Lite (schnell)", "provider": "gemini", "factor": 0.6, "vision": True},
    "gemini-3.6-pro":   {"name": "Gemini 3.6 Pro", "provider": "gemini", "factor": 1.5, "paid": True, "vision": True},
    "gpt-oss-120b":     {"name": "GPT-OSS 120B", "provider": "groq", "factor": 1.0,
                         "api_model": "openai/gpt-oss-120b"},
    "gpt-oss-20b":      {"name": "GPT-OSS 20B", "provider": "groq", "factor": 0.7,
                         "api_model": "openai/gpt-oss-20b"},
    "qwen3-27b":        {"name": "Qwen3 27B", "provider": "groq", "factor": 0.9,
                         "api_model": "qwen/qwen3.8-27b"},
    "llama-3.3-70b":    {"name": "Llama 3.3 70B", "provider": "groq", "factor": 0.8, "vision": True,
                         "api_model": "llama-3.3-70b-versatile"},
    "llama-3.1-8b":     {"name": "Llama 3.1 8B (schnell)", "provider": "groq", "factor": 0.4,
                         "api_model": "llama-3.1-8b-instant"},
    "qwen3-32b":        {"name": "Qwen3 32B", "provider": "groq", "factor": 0.8, "vision": True,
                         "api_model": "qwen/qwen3-32b"},
    "deepseek-r1-70b":  {"name": "DeepSeek R1 70B", "provider": "groq", "factor": 0.9,
                         "api_model": "deepseek-r1-distill-llama-70b"},
    "kimi-k2":          {"name": "Kimi K2", "provider": "groq", "factor": 1.0, "paid": True, "vision": True,
                         "api_model": "moonshotai/kimi-k2-instruct"},
    "nemotron-ultra":   {"name": "Nemotron Ultra 550B", "provider": "cline", "factor": 0.6,
                         "api_model": "nvidia/nemotron-3-ultra-550b-a55b:free"},
    "nemotron-super":   {"name": "Nemotron Super 120B", "provider": "cline", "factor": 0.5,
                         "api_model": "nvidia/nemotron-3-super-120b-a12b:free"},
    "qwen3-27b-free":   {"name": "Qwen3.8 27B", "provider": "cline", "factor": 0.4,
                         "api_model": "qwen/qwen3.8-27b:free"},
    "gemma-4-31b":      {"name": "Gemma 4 31B", "provider": "cline", "factor": 0.3, "vision": True,
                         "api_model": "google/gemma-4-31b-it:free"},
    "north-mini-code":  {"name": "North Mini Code", "provider": "cline", "factor": 0.3,
                         "api_model": "cohere/north-mini-code:free"},
    # ── Nur CLINEPASS-Modelle (:free = Flatrate, KEIN Usage-Billing) ──
    "glm-52-free":      {"name": "GLM 5.2 (free)", "provider": "cline", "factor": 0.3, "vision": True,
                         "api_model": "z-ai/glm-5.2:free"},
    "gemma-4-26b-free": {"name": "Gemma 4 26B (free)", "provider": "cline", "factor": 0.3, "vision": True,
                         "api_model": "google/gemma-4-26b-a4b-it:free"},
    "nemotron-3.5-lightning-free": {"name": "Nemotron 3.5 Lightning (free)", "provider": "cline", "factor": 0.3,
                         "api_model": "nvidia/nemotron-3.5-lightning:free"},
    "nex-n2.5-mini-free": {"name": "Nex N2.5 Mini (free)", "provider": "cline", "factor": 0.2,
                         "api_model": "nex-agi/nex-n2.5-mini:free"},
    "nex-n2.5-pro-free": {"name": "Nex N2.5 Pro (free)", "provider": "cline", "factor": 0.4,
                         "api_model": "nex-agi/nex-n2.5-pro:free"},
    "inkling-free":     {"name": "Inkling (free)", "provider": "cline", "factor": 0.3,
                         "api_model": "thinkingmachines/inkling:free"},
    "inkling-small-free": {"name": "Inkling Small (free)", "provider": "cline", "factor": 0.2,
                         "api_model": "thinkingmachines/inkling-small:free"},
    "laguna-s-free":    {"name": "Laguna S 2.1 (free)", "provider": "cline", "factor": 0.3,
                         "api_model": "poolside/laguna-s-2.1:free"},
    "laguna-xs-free":   {"name": "Laguna XS 2.1 (free)", "provider": "cline", "factor": 0.2,
                         "api_model": "poolside/laguna-xs-2.1:free"},
    "lfm-2.5-free":     {"name": "LFM 2.5 2.6B (free)", "provider": "cline", "factor": 0.15,
                         "api_model": "liquid/lfm-2.5-2.6b:free"},
    "ling-3-flash-free": {"name": "Ling 3.0 Flash (free)", "provider": "cline", "factor": 0.3,
                         "api_model": "inclusionai/ling-3.0-flash-sante:free"},
    "dots-3-note-free": {"name": "Dots 3 Note (free)", "provider": "cline", "factor": 0.3,
                         "api_model": "dots-studio/dots-3-note-preview:free"},
    "nemotron-nano-omni-free": {"name": "Nemotron Nano Omni 30B (free)", "provider": "cline", "factor": 0.3, "vision": True,
                         "api_model": "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"},
    # ── NEU: weitere Modelle – alle FREE (fuer jeden User direkt nutzbar) ──
    "llama-4-scout":    {"name": "Llama 4 Scout 17B", "provider": "groq", "factor": 0.4, "vision": True,
                         "api_model": "meta-llama/llama-4-scout-17b-16e-instruct"},
    "llama-4-maverick": {"name": "Llama 4 Maverick 17B", "provider": "groq", "factor": 0.5, "vision": True,
                         "api_model": "meta-llama/llama-4-maverick-17b-128e-instruct"},
    "gemma2-9b":        {"name": "Gemma 2 9B (schnell)", "provider": "groq", "factor": 0.3,
                         "api_model": "gemma2-9b-it"},
    "qwen-qwq-32b":     {"name": "QwQ 32B (Denker)", "provider": "groq", "factor": 0.6,
                         "api_model": "qwen/qwq-32b"},
    # ── NEU: MiMo (Xiaomi) via Ollama – 14 Tage Testphase pro User, danach Basic-Plan ──
    "mimo-v2.6-distill-qwen-9b": {"name": "MiMo V2.6 Distill Qwen 9B", "provider": "ollama",
                         "factor": 0.5, "trial": 14, "reasoning": True,
                         "api_model": "MiMo-V2.6-Distill-Qwen-9B"},
    # ── NEU: ClinePass MiMo-Pro-Reihe (Ultra) ──
    "mimo-2.5-pro": {"name": "MiMo 2.5 Pro", "provider": "cline", "factor": 1.2, "vision": True,
                         "api_model": "xiaomi/mimo-2.5-pro"},
    "mimo-2.6-pro": {"name": "MiMo 2.6 Pro", "provider": "cline", "factor": 1.4, "vision": True,
                         "api_model": "xiaomi/mimo-2.6-pro"},
}

# ── Pro Modell unabhaengig: Faehigkeiten + Planbedingungen ──
#   plan      – noetiger Plan; ein hoeherer Plan darf ALLES darunter (Max kann auch Pro-Modelle)
#   temp      – nur an Modelle senden, die "temperature" unterstuetzen (Free-Modelle lehnen ab)
#   reasoning – Denkprozess ("Thinking") wird live gesendet und angezeigt
PLAN_RANK = {"free": 0, "test": 1, "basic": 2, "pro": 3, "max": 4, "ultra": 5, "business": 6}
MODEL_PLAN = {
    # ── FREE: echte Free-Modelle (nicht nur Basic) ──
    "deepseek-r1-70b": "free", "gemini-2.5-lite": "free", "llama-3.1-8b": "free",
    "gpt-oss-20b": "free", "lfm-2.5-free": "free", "nex-n2.5-mini-free": "free",
    # ── BASIC ──
    "gemini-2.5-flash": "basic", "llama-3.3-70b": "basic", "qwen3-32b": "basic",
    "gemma2-9b": "basic", "llama-4-scout": "basic", "llama-4-maverick": "basic",
    "glm-52-free": "basic", "gemma-4-26b-free": "basic", "nemotron-3.5-lightning-free": "basic",
    "nex-n2.5-pro-free": "basic", "inkling-free": "basic", "inkling-small-free": "basic",
    "laguna-s-free": "basic", "laguna-xs-free": "basic", "ling-3-flash-free": "basic",
    "dots-3-note-free": "basic", "nemotron-nano-omni-free": "basic", "qwen3-27b-free": "basic",
    "north-mini-code": "basic", "gemma-4-31b": "basic", "nemotron-super": "basic",
    "mimo-v2.6-distill-qwen-9b": "basic",
    # ── PRO ──
    "gemini-3.6-flash": "pro", "gpt-oss-120b": "pro", "qwen3-27b": "pro", "qwen-qwq-32b": "pro",
    # ── MAX ──
    "gemini-3.6-pro": "max", "kimi-k2": "max",
    # ── ULTRA: MiMo-Pro-Reihe ──
    "nemotron-ultra": "ultra", "mimo-2.5-pro": "ultra", "mimo-2.6-pro": "ultra",
}
REASONING_MODELS = {"deepseek-r1-70b", "nemotron-ultra", "qwen-qwq-32b",
                    "mimo-v2.6-distill-qwen-9b", "nemotron-nano-omni-free"}
API_RATE_PER_MIN = {"free": 5, "test": 10, "basic": 20, "pro": 40, "max": 100, "ultra": 200, "business": 500}

def supports_temp(api_model):
    """Jedes Modell bekommt nur Parameter, die es UNTERSTUETZT:
    :free- und Reasoning-/Thinking-Modelle lehnen 'temperature' ab (HTTP 400/500)."""
    am = (api_model or "").lower()
    return ":free" not in am and not any(t in am for t in ("thinking", "reasoning", "-r1", "/deepseek-r1"))

def plan_ok(user_plan, need):
    """Hoeherer Plan enthaellt alles darunter (Max darf auch Pro-Modelle nutzen)."""
    return PLAN_RANK.get(user_plan or "free", 0) >= PLAN_RANK.get(need or "free", 0)

# ── 14-Tage-Testphase fuer Trial-Modelle (z. B. MiMo) ──────────
def trial_free(uid, model):
    """True wenn das Modell fuer diesen User noch kostenlos nutzbar ist:
    Testphase = 14 Tage ab der ERSTEN Nutzung (noch nie genutzt = frei)."""
    days = (MODELS.get(model) or {}).get("trial") or 0
    if not days or not uid:
        return False
    db = get_db()
    row = db.execute("SELECT started_at FROM model_trials WHERE uid=? AND model=?", (uid, model)).fetchone()
    db.close()
    if not row:
        return True
    return (time.time() - float(row["started_at"] or 0)) < days * 86400

def trial_start(db, uid, model):
    """Testphase starten (idempotent) – auf bereits offener DB-Verbindung."""
    if (MODELS.get(model) or {}).get("trial") and uid:
        db.execute("INSERT OR IGNORE INTO model_trials (uid,model,started_at) VALUES (?,?,?)",
                   (uid, model, time.time()))

def trial_touch(uid, model):
    """Testphase starten mit eigener DB-Verbindung (fuer /api/chat)."""
    if (MODELS.get(model) or {}).get("trial") and uid:
        db = get_db()
        db.execute("INSERT OR IGNORE INTO model_trials (uid,model,started_at) VALUES (?,?,?)",
                   (uid, model, time.time()))
        db.commit(); db.close()

def trial_left_days(uid, model):
    """Verbleibende Testphase in Tagen (volle Dauer wenn noch nie genutzt, 0 = abgelaufen/keine)."""
    days = (MODELS.get(model) or {}).get("trial") or 0
    if not days or not uid:
        return 0
    db = get_db()
    row = db.execute("SELECT started_at FROM model_trials WHERE uid=? AND model=?", (uid, model)).fetchone()
    db.close()
    if not row:
        return days
    left = days * 86400 - (time.time() - float(row["started_at"] or 0))
    return math.ceil(left / 86400) if left > 0 else 0

for _id, _m in MODELS.items():
    _m["plan"] = MODEL_PLAN.get(_id, "basic")
    _m["temp"] = supports_temp(_m.get("api_model") or _id)
    _m["reasoning"] = _id in REASONING_MODELS

# ── Token- & Plan-System (wie Cline): Nutzung in Tokens, Limits in 3 Fenstern ──
TOKEN_WINDOWS = {
    "5h":    {"seconds": 5 * 3600,       "label": "5 Stunden"},
    "week":  {"seconds": 7 * 24 * 3600,  "label": "Woche"},
    "month": {"seconds": 30 * 24 * 3600, "label": "Monat"},
}
PLANS = {
    "free":     {"name": "Free",     "price": 0.0,    "desc": "Zum Ausprobieren",
                 "t5": 40000,    "tweek": 250000,    "tmonth": 750000},
    "test":     {"name": "Test",     "price": 0.10,   "desc": "Zahlung testen – echte Abbuchung",
                 "t5": 5000,     "tweek": 25000,     "tmonth": 75000},
    "basic":    {"name": "Basic",    "price": 6.99,   "desc": "Fuer den Einstieg",
                 "t5": 150000,   "tweek": 1200000,   "tmonth": 4000000},
    "pro":      {"name": "Pro",      "price": 12.99,  "desc": "Fuer jeden Tag",
                 "t5": 400000,   "tweek": 3500000,   "tmonth": 12000000},
    "max":      {"name": "Max",      "price": 39.99,  "desc": "Viel los",
                 "t5": 1200000,  "tweek": 10000000,  "tmonth": 40000000},
    "ultra":    {"name": "Ultra",    "price": 99.99,  "desc": "Fast ohne Grenzen",
                 "t5": 3000000,  "tweek": 25000000,  "tmonth": 100000000},
    "business": {"name": "Business", "price": 199.99, "desc": "Fuer Teams & Firmen",
                 "t5": 8000000,  "tweek": 60000000,  "tmonth": 250000000},
}
PLAN_ORDER = ["free", "test", "basic", "pro", "max", "ultra", "business"]
SUPER_ADMIN_EMAIL = "admin@sychos.net"   # nur dieser Account darf Admin-Rechte vergeben/entziehen
PLAN_DAYS = 30   # ein gekaufter Plan gilt 30 Tage (monatlich kuendbar)
PROMO_CODES = {"release": 0.20}   # Rabattcode "Release" = -20 %

def effective_plan(user):
    """Aktiver Plan: gekaufter Plan (bis plan_until) > Admin-Paid (= Pro) > free."""
    if not user:
        return "free"
    p = user.get("plan") or "free"
    if p != "free":
        until = user.get("plan_until", 0) or 0
        if until and time.time() > until:
            p = "free"
        else:
            return p
    paid_until = user.get("paid_until", 0) or 0
    if user.get("is_paid") and (not paid_until or time.time() <= paid_until):
        return "pro"
    return "free"

def plan_limits(plan):
    p = PLANS.get(plan, PLANS["free"])
    return {"5h": p["t5"], "week": p["tweek"], "month": p["tmonth"]}

def usage_state(uid, plan):
    """Drei Fenster (5h / Woche / Monat): Verbrauch, Limit und Reset-Zeit."""
    now = time.time()
    lim = plan_limits(plan)
    out = {}
    db = get_db()
    brow = db.execute("SELECT bonus_tokens FROM users WHERE uid=?", (uid,)).fetchone()
    bonus = (brow["bonus_tokens"] if brow else 0) or 0
    for win, info in TOKEN_WINDOWS.items():
        row = db.execute("SELECT start, tokens FROM usage WHERE uid=? AND win=?", (uid, win)).fetchone()
        start = (row["start"] if row else 0) or 0
        used = (row["tokens"] if row else 0) or 0
        if not start or now - start >= info["seconds"]:
            start, used = now, 0.0
            db.execute("INSERT OR REPLACE INTO usage (uid,win,start,tokens) VALUES (?,?,?,?)",
                       (uid, win, start, used))
        out[win] = {"used": used, "limit": lim[win] + bonus,
                    "remaining": max(0.0, lim[win] + bonus - used),
                    "resets_at": start + info["seconds"]}
    db.commit(); db.close()
    return out

def add_usage(uid, tokens):
    """Verbrauchte Tokens in alle drei Fenster eintragen (Fenster laufen automatisch ab)."""
    now = time.time()
    db = get_db()
    for win, info in TOKEN_WINDOWS.items():
        row = db.execute("SELECT start, tokens FROM usage WHERE uid=? AND win=?", (uid, win)).fetchone()
        start = (row["start"] if row else 0) or 0
        used = (row["tokens"] if row else 0) or 0
        if not start or now - start >= info["seconds"]:
            start, used = now, 0.0
        db.execute("INSERT OR REPLACE INTO usage (uid,win,start,tokens) VALUES (?,?,?,?)",
                   (uid, win, start, used + max(0.0, float(tokens))))
    db.commit(); db.close()

def est_tokens(history):
    """Grobe Vorab-Schaetzung (Input + Bilder + Reserve fuer die Antwort)."""
    def _clen(c):
        if isinstance(c, str):
            return len(c)
        n = 0
        for p in c or []:
            n += len(p.get("text") or "") + (1800 if p.get("type") == "image" else 0)
        return n
    chars = sum(_clen(m.get("content")) for m in history)
    return int(chars / 4) + 900

def limit_block(uid, plan, est):
    """None = ok, sonst (win, state) des ersten ueberzogenen Fensters."""
    st = usage_state(uid, plan)
    for win in ("5h", "week", "month"):
        if st[win]["used"] + est > st[win]["limit"]:
            return win, st[win], st
    return None

def fmt_wait(seconds):
    seconds = max(0, int(seconds))
    h, m = seconds // 3600, (seconds % 3600) // 60
    if h:
        return "%d Std %d Min" % (h, m)
    return "%d Min" % max(1, m)

def strength_cost(strength, model_id):
    s = STRENGTHS.get(strength, STRENGTHS["medium"])
    f = MODELS.get(model_id, {}).get("factor", 1.0)
    return max(1, int(round(s["cost"] * f)))

request_times = []
LOAD_LOCK = threading.Lock()

def note_request():
    with LOAD_LOCK:
        now = time.time()
        recent = [t for t in request_times if now - t < 60]
        recent.append(now)
        request_times[:] = recent

def load_factor():
    """Auslastungs-Aufschlag: viele Anfragen pro Minute -> hoehere Kosten."""
    with LOAD_LOCK:
        n = len(request_times)
    if n >= 240:
        return 2.5
    if n >= 120:
        return 2.0
    if n >= 60:
        return 1.5
    if n >= 30:
        return 1.25
    return 1.0

def message_cost(strength, model_id):
    """Endgueltiger Credit-Preis inkl. Auslastungs-Aufschlag."""
    return max(1, int(round(strength_cost(strength, model_id) * load_factor())))

RATE = {}
RATE_LOCK = threading.Lock()

def rate_ok(key, limit, window=60):
    now = time.time()
    with RATE_LOCK:
        lst = [t for t in RATE.get(key, []) if now - t < window]
        if len(lst) >= limit:
            RATE[key] = lst
            return False
        lst.append(now)
        RATE[key] = lst
        return True

START_TIME = time.time()
# ═══════════════════════════════════════════════════════════
#  DATABASE
# ═══════════════════════════════════════════════════════════
def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn

def hash_pw(pw):
    salt = os.urandom(16).hex()
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 100000)
    return "pbkdf2$%s$%s" % (salt, dk.hex())

def verify_pw(pw, stored):
    if stored.startswith("pbkdf2$"):
        _, salt, dk = stored.split("$", 2)
        calc = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 100000).hex()
        return hmac.compare_digest(calc, dk)
    return hmac.compare_digest(hashlib.sha256(pw.encode()).hexdigest(), stored)

def new_token():
    return uuid.uuid4().hex + os.urandom(16).hex()

def resolve_user_ref(db, ref):
    """User-Referenz aufloesen: UID, Anzeigename ODER E-Mail (case-insensitive).
    Gibt (uid, None) bei Treffer bzw. (None, None) wenn unbekannt zurueck."""
    ref = (ref or "").strip()
    if not ref:
        return None, None
    row = db.execute("SELECT uid FROM users WHERE uid=?", (ref,)).fetchone()
    if row:
        return row["uid"], None
    row = db.execute("SELECT uid FROM users WHERE lower(display_name)=lower(?)", (ref,)).fetchone()
    if row:
        return row["uid"], None
    row = db.execute("SELECT uid FROM users WHERE lower(email)=lower(?)", (ref,)).fetchone()
    if row:
        return row["uid"], None
    return None, None

def display_name_taken(db, name, exclude_uid=None):
    """True wenn der Anzeigename bereits von einem ANDEREN User genutzt wird."""
    n = (name or "").strip()
    if not n:
        return False
    if exclude_uid:
        row = db.execute("SELECT uid FROM users WHERE lower(display_name)=lower(?) AND uid<>?",
                         (n, exclude_uid)).fetchone()
    else:
        row = db.execute("SELECT uid FROM users WHERE lower(display_name)=lower(?)", (n,)).fetchone()
    return bool(row)

def unique_display_name(db, base, exclude_uid=None):
    """Eindeutigen Anzeigename aus Basis erzeugen (Basis, Basis2, Basis3, ...)."""
    base = (base or "User").strip() or "User"
    cand, i = base, 2
    while display_name_taken(db, cand, exclude_uid):
        cand = base + str(i)
        i += 1
    return cand

def init_db(fresh=None):
    """Datenbank-Schema anlegen bzw. auf Stand bringen.

    fresh=True (CLI-Flag --fresh-db): alle Nicht-Admin-User inkl. Daten loeschen –
    uebrig bleibt nur der Admin (admin@sychos.net). Ohne Flag nur normalisieren.
    """
    if fresh is None:
        fresh = "--fresh-db" in sys.argv
    db = get_db()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            uid TEXT PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            display_name TEXT DEFAULT '',
            credits REAL DEFAULT 50.0,
            is_banned INTEGER DEFAULT 0,
            ban_reason TEXT DEFAULT '',
            ban_until REAL DEFAULT 0,
            is_admin INTEGER DEFAULT 0,
            is_paid INTEGER DEFAULT 0,
            reg_ip TEXT DEFAULT '',
            created_at REAL DEFAULT 0,
            last_login REAL DEFAULT 0,
            last_seen REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS chats (
            id TEXT PRIMARY KEY,
            uid TEXT NOT NULL,
            title TEXT DEFAULT 'Neuer Chat',
            model TEXT DEFAULT 'gemini-3.6-flash',
            created_at REAL DEFAULT 0,
            FOREIGN KEY (uid) REFERENCES users(uid)
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            model TEXT DEFAULT '',
            tokens INTEGER DEFAULT 0,
            cost REAL DEFAULT 0,
            created_at REAL DEFAULT 0,
            FOREIGN KEY (chat_id) REFERENCES chats(id)
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            uid TEXT NOT NULL,
            created_at REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS user_keys (
            uid TEXT NOT NULL,
            provider TEXT NOT NULL,
            api_key TEXT NOT NULL,
            created_at REAL DEFAULT 0,
            PRIMARY KEY (uid, provider)
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
    """)
    row = db.execute("SELECT uid FROM users WHERE is_admin=1").fetchone()
    if not row:
        uid = str(uuid.uuid4())
        # Admin-Passwort: Env ORACLE_ADMIN_PASSWORD, sonst "Lenamax5745"
        pw = os.environ.get("ORACLE_ADMIN_PASSWORD", "Lenamax5745")
        db.execute("INSERT INTO users (uid,email,password_hash,display_name,credits,is_admin,created_at) VALUES (?,?,?,?,?,?,?)",
                   (uid, "admin@sychos.net", hash_pw(pw), "Sychos", 99999, 1, time.time()))
        db.commit()
    try:
        db.execute("ALTER TABLE messages ADD COLUMN images TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    db.execute("""
        CREATE TABLE IF NOT EXISTS usage (
            uid TEXT NOT NULL,
            win TEXT NOT NULL,
            start REAL DEFAULT 0,
            tokens REAL DEFAULT 0,
            PRIMARY KEY (uid, win)
        );""")
    db.execute("""
        CREATE TABLE IF NOT EXISTS api_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uid TEXT NOT NULL,
            key TEXT UNIQUE,
            created_at REAL DEFAULT 0,
            revoked INTEGER DEFAULT 0
        );""")
    db.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uid TEXT NOT NULL,
            plan TEXT NOT NULL,
            code TEXT DEFAULT '',
            price REAL DEFAULT 0,
            created_at REAL DEFAULT 0
        );""")
    db.execute("""
        CREATE TABLE IF NOT EXISTS warnings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uid TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at REAL DEFAULT 0,
            read INTEGER DEFAULT 0
        );""")
    for col in ("last_login REAL DEFAULT 0", "last_seen REAL DEFAULT 0",
                "ban_reason TEXT DEFAULT ''", "ban_until REAL DEFAULT 0",
                "is_paid INTEGER DEFAULT 0", "reg_ip TEXT DEFAULT ''",
                "paid_until REAL DEFAULT 0", "last_credit REAL DEFAULT 0",
                "plan TEXT DEFAULT 'free'", "plan_until REAL DEFAULT 0",
                "bonus_tokens REAL DEFAULT 0",
                "totp_secret TEXT DEFAULT ''", "totp_on INTEGER DEFAULT 0",
                "verified INTEGER DEFAULT 0", "avatar TEXT DEFAULT ''"):
        try:
            db.execute("ALTER TABLE users ADD COLUMN " + col)
            db.commit()
        except Exception:
            pass
    # 14-Tage-Testphase pro User fuer Trial-Modelle (z. B. MiMo) – Start = 1. Nutzung
    db.execute("""
        CREATE TABLE IF NOT EXISTS model_trials (
            uid TEXT NOT NULL,
            model TEXT NOT NULL,
            started_at REAL DEFAULT 0,
            PRIMARY KEY (uid, model)
        );""")
    # ── 2.0: Notifications, Freunde, Workspaces, Reports & Bonus-Codes ──
    db.executescript("""
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_uid TEXT DEFAULT '',
            to_uid TEXT DEFAULT '',
            title TEXT DEFAULT '',
            body TEXT DEFAULT '',
            image TEXT DEFAULT '',
            kind TEXT DEFAULT 'user',
            read INTEGER DEFAULT 0,
            created_at REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS friends (
            uid TEXT NOT NULL,
            friend_uid TEXT NOT NULL,
            status TEXT DEFAULT 'accepted',
            created_at REAL DEFAULT 0,
            PRIMARY KEY (uid, friend_uid)
        );
        CREATE TABLE IF NOT EXISTS workspaces (
            id TEXT PRIMARY KEY,
            name TEXT DEFAULT '',
            owner_uid TEXT DEFAULT '',
            created_at REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS workspace_members (
            workspace_id TEXT NOT NULL,
            uid TEXT NOT NULL,
            role TEXT DEFAULT 'member',
            PRIMARY KEY (workspace_id, uid)
        );
        CREATE TABLE IF NOT EXISTS workspace_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workspace_id TEXT NOT NULL,
            uid TEXT DEFAULT '',
            content TEXT DEFAULT '',
            image TEXT DEFAULT '',
            created_at REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reporter_uid TEXT DEFAULT '',
            reported_uid TEXT DEFAULT '',
            reason TEXT DEFAULT '',
            status TEXT DEFAULT 'open',
            created_at REAL DEFAULT 0,
            resolved_at REAL DEFAULT 0,
            resolved_by TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS channels (
            id TEXT PRIMARY KEY,
            name TEXT DEFAULT '',
            kind TEXT DEFAULT 'group',
            description TEXT DEFAULT '',
            owner_uid TEXT DEFAULT '',
            verified INTEGER DEFAULT 0,
            created_at REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS channel_members (
            channel_id TEXT NOT NULL,
            uid TEXT NOT NULL,
            role TEXT DEFAULT 'member',
            banned INTEGER DEFAULT 0,
            created_at REAL DEFAULT 0,
            PRIMARY KEY (channel_id, uid)
        );
        CREATE TABLE IF NOT EXISTS channel_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id TEXT NOT NULL,
            uid TEXT DEFAULT '',
            content TEXT DEFAULT '',
            image TEXT DEFAULT '',
            created_at REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS dm_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_uid TEXT NOT NULL,
            recipient_uid TEXT NOT NULL,
            content TEXT DEFAULT '',
            image TEXT DEFAULT '',
            read INTEGER DEFAULT 0,
            created_at REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS bonus_codes (
            code TEXT PRIMARY KEY,
            credits REAL DEFAULT 0,
            max_uses INTEGER DEFAULT 0,
            used INTEGER DEFAULT 0,
            active INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS bonus_redemptions (
            uid TEXT NOT NULL,
            code TEXT NOT NULL,
            created_at REAL DEFAULT 0,
            PRIMARY KEY (uid, code)
        );
    """)
    # Bonus-Code "Happy40k" (40.000 Tokens, unbegrenzt einloesbar) – nur einmal seeden
    if not db.execute("SELECT 1 FROM bonus_codes WHERE code='Happy40k'").fetchone():
        db.execute("INSERT INTO bonus_codes (code,credits,max_uses,used,active) VALUES ('Happy40k',40000,0,0,1)")
    # ── Community "Sychos Offizel" seeden (nur wenn noch keine Community existiert) ──
    if not db.execute("SELECT 1 FROM channels WHERE kind='community'").fetchone():
        arow = db.execute("SELECT uid FROM users WHERE is_admin=1 ORDER BY created_at LIMIT 1").fetchone()
        if arow:
            now = time.time()
            cid = "sychos-offizel"
            db.execute("INSERT INTO channels (id,name,kind,description,owner_uid,verified,created_at) "
                       "VALUES (?,?,?,?,?,?,?)",
                       (cid, "Sychos Offizel", "community",
                        "Offizielle Sychos-Community – News, Austausch und Support.", arow["uid"], 1, now))
            db.execute("INSERT OR REPLACE INTO channel_members (channel_id,uid,role,banned,created_at) "
                       "VALUES (?,?,?,0,?)", (cid, arow["uid"], "owner", now))
    # ── Frische DB: --fresh-db loescht alle Nicht-Admin-User inkl. zugehoeriger Daten ──
    # (Leere users-Tabelle: der Admin-Block oben legt GENAU EINEN User an – admin@sychos.net.)
    if fresh:
        sub = "SELECT uid FROM users WHERE is_admin=0"
        for sql in (
            "DELETE FROM messages WHERE chat_id IN (SELECT id FROM chats WHERE uid IN (%s))" % sub,
            "DELETE FROM chats WHERE uid IN (%s)" % sub,
            "DELETE FROM sessions WHERE uid IN (%s)" % sub,
            "DELETE FROM user_keys WHERE uid IN (%s)" % sub,
            "DELETE FROM usage WHERE uid IN (%s)" % sub,
            "DELETE FROM api_keys WHERE uid IN (%s)" % sub,
            "DELETE FROM orders WHERE uid IN (%s)" % sub,
            "DELETE FROM warnings WHERE uid IN (%s)" % sub,
            "DELETE FROM model_trials WHERE uid IN (%s)" % sub,
            "DELETE FROM notifications WHERE from_uid IN (%s) OR to_uid IN (%s)" % (sub, sub),
            "DELETE FROM friends WHERE uid IN (%s) OR friend_uid IN (%s)" % (sub, sub),
            "DELETE FROM workspace_messages WHERE uid IN (%s) OR workspace_id IN "
            "(SELECT id FROM workspaces WHERE owner_uid IN (%s))" % (sub, sub),
            "DELETE FROM workspace_members WHERE uid IN (%s) OR workspace_id IN "
            "(SELECT id FROM workspaces WHERE owner_uid IN (%s))" % (sub, sub),
            "DELETE FROM workspaces WHERE owner_uid IN (%s)" % sub,
            "DELETE FROM reports WHERE reporter_uid IN (%s) OR reported_uid IN (%s)" % (sub, sub),
            "DELETE FROM bonus_redemptions WHERE uid IN (%s)" % sub,
            "DELETE FROM users WHERE is_admin=0",
        ):
            db.execute(sql)
        db.commit()
    db.commit()
    db.close()

def touch_user(uid):
    """Markiert einen User als aktiv (online)."""
    db = get_db()
    db.execute("UPDATE users SET last_seen=? WHERE uid=?", (time.time(), uid))
    db.commit(); db.close()

# ═══════════════════════════════════════════════════════════
#  KI-PROVIDER
# ═══════════════════════════════════════════════════════════
def mask_key(k):
    """Key unsichtbar machen (fuer Statusanzeigen)."""
    if not k:
        return ""
    return "*" * 8 + k[-4:]

def get_setting(db, key):
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else ""

def maintenance_on(db=None):
    """Wartungsmodus (settings.maintenance='1'): 'Server abschaltet' – Login/Modelle/Chat
    sind fuer normale User gesperrt, Admins arbeiten normal weiter. Server bleibt laufen."""
    own = db is None
    if own:
        db = get_db()
    val = get_setting(db, "maintenance") == "1"
    if own:
        db.close()
    return val

def set_maintenance(on):
    db = get_db()
    db.execute("INSERT OR REPLACE INTO settings (key,value) VALUES ('maintenance',?)",
               ("1" if on else "0",))
    db.commit(); db.close()

def set_setting(db, key, value):
    """Einzelnen settings-Wert schreiben (Key/Value)."""
    db.execute("INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)", (key, value))

def resolve_key(db, uid, provider):
    """Key-Auflösung: Admin-Settings (DB) > Env > fest im Code hinterlegter Default."""
    db_key = get_setting(db, provider + "_api_key")
    if db_key:
        return db_key
    return {"gemini": GEMINI_API_KEY, "groq": GROQ_API_KEY, "cline": CLINE_API_KEY}.get(provider, "")

def web_search(query):
    # Kostenlose Websuche (DuckDuckGo + Wikipedia) fuer KI-Kontext. Kein Key noetig.
    out = []
    try:
        url = "https://api.duckduckgo.com/?q=" + urllib.parse.quote(query) + "&format=json&no_html=1&skip_disambig=1"
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=10) as resp:
            d = json.loads(resp.read())
        if d.get("AbstractText"):
            out.append("- " + d["AbstractText"][:400] + " (Quelle: " + d.get("AbstractURL", "") + ")")
        for t in (d.get("RelatedTopics") or [])[:5]:
            if isinstance(t, dict) and t.get("Text"):
                out.append("- " + t["Text"][:300])
    except Exception:
        pass
    if len(out) < 2:
        try:
            url = ("https://de.wikipedia.org/w/api.php?action=query&list=search&srsearch="
                   + urllib.parse.quote(query) + "&format=json&utf8=1&srlimit=5")
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=10) as resp:
                d = json.loads(resp.read())
            for r in (d.get("query", {}).get("search") or []):
                snip = re.sub("<[^>]+>", "", r.get("snippet", ""))
                out.append("- " + r.get("title", "") + ": " + snip[:300])
        except Exception:
            pass
    return out

def totp_code(secret, counter=None):
    """6-stelliger TOTP-Code (RFC 6238, SHA-1, 30s) – nur Stdlib."""
    try:
        key = base64.b32decode(secret.upper() + "=" * ((8 - len(secret) % 8) % 8))
    except Exception:
        return ""
    if counter is None:
        counter = int(time.time()) // 30
    h = hmac.new(key, int(counter).to_bytes(8, "big"), hashlib.sha1).digest()
    o = h[-1] & 15
    return "%06d" % ((int.from_bytes(h[o:o + 4], "big") & 0x7FFFFFFF) % 1000000)

def totp_ok(secret, code):
    code = (code or "").replace(" ", "")
    if not secret or len(code) != 6 or not code.isdigit():
        return False
    now = int(time.time()) // 30
    return any(hmac.compare_digest(totp_code(secret, now + off), code) for off in (-1, 0, 1))

def make_pdf(title, text):
    """Minimaler PDF-Writer (A4, Helvetica) – ohne externe Bibliotheken."""
    def esc(s):
        s = str(s).encode("latin-1", "replace").decode("latin-1")
        return s.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
    t = re.sub(r"```[\s\S]*?```", " [Code] ", text or "")
    t = t.replace("**", "").replace("__", "").replace("`", "").replace("#", "")
    lines = []
    for para in ([title or "Sychos Export", ""] + t.split("\n")):
        para = (para or " ").rstrip() or " "
        while len(para) > 92:
            cut = para.rfind(" ", 0, 92)
            cut = cut if cut > 40 else 92
            lines.append(para[:cut])
            para = para[cut:].lstrip()
        lines.append(para)
    per_page = 46
    pages = [lines[i:i + per_page] for i in range(0, len(lines), per_page)] or [[""]]
    page_ids, content_ids, num = [], [], 4
    for _ in pages:
        page_ids.append(num); num += 1
        content_ids.append(num); num += 1
    out = bytearray(b"%PDF-1.4\n")
    offsets = {}
    def add(n, body):
        offsets[n] = len(out)
        out.extend(("%d 0 obj\n" % n).encode())
        out.extend(body)
        out.extend(b"\nendobj\n")
    add(1, b"<< /Type /Catalog /Pages 2 0 R >>")
    add(2, ("<< /Type /Pages /Kids [%s] /Count %d >>" %
            (" ".join("%d 0 R" % i for i in page_ids), len(pages))).encode())
    add(3, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for i, plines in enumerate(pages):
        body = ["BT /F1 11 Tf 50 792 Td 14 TL"]
        for ln in plines:
            body.append("(%s) Tj T*" % esc(ln))
        body.append("ET")
        stream = "\n".join(body).encode("latin-1", "replace")
        add(page_ids[i], ("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                          "/Contents %d 0 R /Resources << /Font << /F1 3 0 R >> >> >>"
                          % content_ids[i]).encode())
        add(content_ids[i], b"<< /Length " + str(len(stream)).encode() +
            b" >>\nstream\n" + stream + b"\nendstream")
    xref = len(out)
    out.extend(("xref\n0 %d\n0000000000 65535 f \n" % num).encode())
    for i in range(1, num):
        out.extend(("%010d 00000 n \n" % offsets[i]).encode())
    out.extend(("trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF" % (num, xref)).encode())
    return bytes(out)

def gemini_parts(content):
    """History-Inhalt -> Gemini 'parts' (Text + inline Bilder)."""
    if isinstance(content, str):
        return [{"text": content}]
    out = []
    for p in content or []:
        if p.get("type") == "image":
            out.append({"inline_data": {"mime_type": p.get("mime", "image/png"),
                                        "data": p.get("data", "")}})
        else:
            out.append({"text": p.get("text", "")})
    return out or [{"text": ""}]

def oai_content(content):
    """History-Inhalt -> OpenAI-Style 'content' (Text + image_url)."""
    if isinstance(content, str):
        return content
    out = []
    for p in content or []:
        if p.get("type") == "image":
            out.append({"type": "image_url", "image_url": {
                "url": "data:%s;base64,%s" % (p.get("mime", "image/png"), p.get("data", ""))}})
        else:
            out.append({"type": "text", "text": p.get("text", "")})
    return out or ""

def call_gemini(messages, api_key, strength="medium", model="gemini-3.6-flash"):
    """Echter Gemini-Aufruf. Kein Demo-/Mock-Fallback."""
    if not api_key:
        return {"ok": False, "error_type": "missing_key",
                "error": "Kein Gemini-API-Key hinterlegt. Bitte in den Einstellungen einen Key hinterlegen."}
    s = STRENGTHS.get(strength, STRENGTHS["medium"])
    contents = []
    for m in messages:
        role = "user" if m["role"] == "user" else "model"
        contents.append({"role": role, "parts": gemini_parts(m["content"])})
    payload = json.dumps({
        "contents": contents,
        "generationConfig": {"maxOutputTokens": s["max_tokens"], "temperature": s["temp"]},
    }).encode()
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           + model + ":generateContent?key=" + api_key)
    req = urllib.request.Request(url, data=payload,
        headers={"Content-Type": "application/json",
                 "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
            cand = data.get("candidates") or [{}]
            parts = (cand[0].get("content") or {}).get("parts") or []
            text = "".join(p.get("text", "") for p in parts)
            if not text.strip():
                return {"ok": False, "error_type": "api",
                        "error": "Gemini hat eine leere Antwort geliefert."}
            tokens = data.get("usageMetadata", {}).get("totalTokenCount", 0)
            return {"ok": True, "text": text, "tokens": tokens}
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return {"ok": False, "error_type": "invalid_key",
                    "error": "Gemini-API-Key ist ungueltig oder gesperrt. Bitte Key pruefen."}
        if e.code == 429:
            return {"ok": False, "error_type": "rate_limit",
                    "error": "Gemini-Rate-Limit erreicht. Bitte kurz warten und erneut versuchen."}
        return {"ok": False, "error_type": "api",
                "error": "Gemini-Fehler (HTTP %d). Bitte spaeter erneut versuchen." % e.code}
    except Exception:
        return {"ok": False, "error_type": "api",
                "error": "Gemini nicht erreichbar. Bitte spaeter erneut versuchen."}

def call_groq(messages, api_key, strength="medium", model="llama-3.3-70b-versatile"):
    """Echter Groq-Aufruf (OpenAI-kompatibel). Kein Demo-/Mock-Fallback."""
    if not api_key:
        return {"ok": False, "error_type": "missing_key",
                "error": "Kein Groq-API-Key hinterlegt. Bitte in den Einstellungen einen Key hinterlegen."}
    s = STRENGTHS.get(strength, STRENGTHS["medium"])
    msgs = [{"role": ("user" if m["role"] == "user" else "assistant"), "content": oai_content(m["content"])}
            for m in messages]
    effort = {"low": "low", "medium": "medium", "high": "high", "extra": "high"}.get(strength, "medium")
    payload = json.dumps({
        "model": model, "messages": msgs,
        "max_tokens": s["max_tokens"] + 512, "temperature": s["temp"],
        "reasoning_effort": effort,
    }).encode()
    req = urllib.request.Request("https://api.groq.com/openai/v1/chat/completions",
        data=payload, method="POST", headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + api_key,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Accept": "application/json",
        })
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
            msg = (data["choices"][0].get("message") or {})
            text = msg.get("content") or ""
            if not text.strip():
                return {"ok": False, "error_type": "api",
                        "error": "Groq hat eine leere Antwort geliefert. Bitte erneut versuchen."}
            tokens = data.get("usage", {}).get("total_tokens", 0)
            return {"ok": True, "text": text, "tokens": tokens}
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return {"ok": False, "error_type": "invalid_key",
                    "error": "Groq-API-Key ist ungueltig oder gesperrt. Bitte Key pruefen."}
        if e.code == 429:
            return {"ok": False, "error_type": "rate_limit",
                    "error": "Groq-Rate-Limit erreicht. Bitte kurz warten und erneut versuchen."}
        return {"ok": False, "error_type": "api",
                "error": "Groq-Fehler (HTTP %d). Bitte spaeter erneut versuchen." % e.code}
    except Exception:
        return {"ok": False, "error_type": "api",
                "error": "Groq nicht erreichbar. Bitte spaeter erneut versuchen."}

def call_cline(messages, api_key, strength="medium", model="anthropic/claude-sonnet-4.6"):
    """Echter Cline-Aufruf (OpenAI-kompatibel, api.cline.bot)."""
    if not api_key:
        return {"ok": False, "error_type": "missing_key",
                "error": "Kein Cline-API-Key hinterlegt (Admin-Panel -> API-Keys)."}
    s = STRENGTHS.get(strength, STRENGTHS["medium"])
    msgs = [{"role": ("user" if m["role"] == "user" else "assistant"), "content": oai_content(m["content"])}
            for m in messages]
    # Pro Modell nur senden, was es UNTERSTUETZT (Free-Modelle lehnen temperature ab)
    _body = {"model": model, "messages": msgs, "max_tokens": s["max_tokens"] + 512}
    if supports_temp(model):
        _body["temperature"] = s["temp"]
    payload = json.dumps(_body).encode()
    def _do_call():
        req2 = urllib.request.Request("https://api.cline.bot/api/v1/chat/completions",
            data=payload, method="POST", headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + api_key,
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
                "HTTP-Referer": SYCHOS_HOME,
                "X-Title": "Sychos",
            })
        with urllib.request.urlopen(req2, timeout=90) as resp:
            data = json.loads(resp.read())
        ch = data.get("choices") or (data.get("data") or {}).get("choices") or []
        msg = (ch[0].get("message") or {}) if ch else {}
        text = msg.get("content") or ""
        return text, data.get("usage", {}).get("total_tokens", 0)

    try:
        text, tokens = _do_call()
        if not text.strip():
            return {"ok": False, "error_type": "api",
                    "error": "Cline hat eine leere Antwort geliefert."}
        return {"ok": True, "text": text, "tokens": tokens}
    except urllib.error.HTTPError as e:
        if e.code == 500:
            # Free-Tier-Flakiness: ein automatischer Wiederholungsversuch
            try:
                time.sleep(1.5)
                text, tokens = _do_call()
                if text.strip():
                    return {"ok": True, "text": text, "tokens": tokens}
            except Exception:
                pass
            return {"ok": False, "error_type": "api",
                    "error": "Free-Modell voruebergehend ueberlastet. Bitte erneut versuchen."}
        if e.code == 402:
            return {"ok": False, "error_type": "insufficient_credits",
                    "error": "Cline-Guthaben aufgebraucht (Cline Credits). Bitte unter app.cline.bot aufladen."}
        if e.code in (401, 403):
            return {"ok": False, "error_type": "invalid_key",
                    "error": "Cline-API-Key ist ungueltig oder gesperrt."}
        if e.code == 429:
            return {"ok": False, "error_type": "rate_limit",
                    "error": "Cline-Rate-Limit erreicht. Bitte kurz warten."}
        return {"ok": False, "error_type": "api",
                "error": "Cline-Fehler (HTTP %d). Bitte spaeter erneut versuchen." % e.code}
    except Exception:
        return {"ok": False, "error_type": "api", "error": "Cline nicht erreichbar."}


# ── Ollama (lokaler Server bzw. Tunnel zu 130.61.174.2) ─────────
# Reihenfolge: zuerst direkt auf dem Server (localhost), dann die Tunnel-URLs.
# Per Env ueberschreibbar: OLLAMA_URLS="http://host:11434,https://..."
OLLAMA_URLS = [u.strip().rstrip("/") for u in os.environ.get(
    "OLLAMA_URLS",
    "http://127.0.0.1:11434,"
    "https://nnyrr-130-61-174-2.free.pinggy.net,"
    "https://wmzoy-130-61-174-2.run.pinggy-free.link").split(",") if u.strip()]

def _ollama_chat(payload, timeout):
    """POST /api/chat an den ersten erreichbaren Ollama-Server.
    Wirft die letzte Exception, wenn KEIN Server antwortet."""
    last = None
    for base in OLLAMA_URLS:
        try:
            req = urllib.request.Request(base + "/api/chat",
                data=json.dumps(payload).encode(), method="POST",
                headers={"Content-Type": "application/json", "User-Agent": UA})
            return urllib.request.urlopen(req, timeout=timeout)
        except Exception as e:
            last = e
    raise last or RuntimeError("kein Ollama-Server konfiguriert")

def call_ollama(messages, strength="medium", model="MiMo-V2.6-Distill-Qwen-9B"):
    """Echter Ollama-Aufruf (natives /api/chat). Kein API-Key noetig."""
    s = STRENGTHS.get(strength, STRENGTHS["medium"])
    msgs = [{"role": ("user" if m["role"] == "user" else "assistant"), "content": oai_content(m["content"])}
            for m in messages]
    payload = {"model": model, "messages": msgs, "stream": False,
               "options": {"temperature": s["temp"], "num_predict": s["max_tokens"] + 512}}
    try:
        resp = _ollama_chat(payload, 180)
        with resp:
            data = json.loads(resp.read())
        msg = data.get("message") or {}
        text = msg.get("content") or ""
        if not text.strip():
            return {"ok": False, "error_type": "api",
                    "error": "Ollama hat eine leere Antwort geliefert. Bitte erneut versuchen."}
        tokens = (data.get("eval_count") or 0) + (data.get("prompt_eval_count") or 0)
        return {"ok": True, "text": text, "tokens": tokens}
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"ok": False, "error_type": "api",
                    "error": "Modell '%s' ist auf dem Ollama-Server nicht geladen (dort ausfuehren: ollama pull %s)." % (model, model)}
        return {"ok": False, "error_type": "api",
                "error": "Ollama-Fehler (HTTP %d). Bitte spaeter erneut versuchen." % e.code}
    except Exception:
        return {"ok": False, "error_type": "api",
                "error": "Ollama nicht erreichbar (Server/Tunnel offline?). Bitte spaeter erneut versuchen."}

def stream_ollama(messages, strength, model):
    """Ollama-Streaming (NDJSON ueber natives /api/chat), inkl. Thinking-Feld."""
    s = STRENGTHS.get(strength, STRENGTHS["medium"])
    msgs = [{"role": ("user" if m["role"] == "user" else "assistant"), "content": oai_content(m["content"])}
            for m in messages]
    payload = {"model": model, "messages": msgs, "stream": True,
               "options": {"temperature": s["temp"], "num_predict": s["max_tokens"] + 512}}
    got = False
    try:
        resp = _ollama_chat(payload, 300)
        with resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("error"):
                    yield {"type": "error", "error_type": "api", "error": str(d["error"])}
                    return
                msg = d.get("message") or {}
                th = msg.get("thinking") or ""
                if th:
                    yield {"type": "think", "text": th}
                delta = msg.get("content") or ""
                if delta:
                    got = True
                    yield {"type": "delta", "text": delta}
                if d.get("done"):
                    if got:
                        yield {"type": "end", "tokens": (d.get("eval_count") or 0) + (d.get("prompt_eval_count") or 0)}
                        return
        if got:
            yield {"type": "end"}
            return
    except urllib.error.HTTPError as e:
        if e.code == 404:
            yield {"type": "error", "error_type": "api",
                   "error": "Modell '%s' ist auf dem Ollama-Server nicht geladen (dort ausfuehren: ollama pull %s)." % (model, model)}
        else:
            yield {"type": "error", "error_type": "api",
                   "error": "Ollama-Fehler (HTTP %d). Bitte spaeter erneut versuchen." % e.code}
        return
    except Exception:
        pass
    r = call_ollama(messages, strength, model)   # Fallback ohne Stream
    if r["ok"]:
        yield {"type": "delta", "text": r["text"]}
        yield {"type": "end", "tokens": r.get("tokens", 0)}
    else:
        yield {"type": "error", "error_type": r.get("error_type", "api"), "error": r["error"]}


# ═══════════════════════════════════════════════════════════
#  STREAMING — Token-fuer-Token (SSE) fuer echtes Live-Tippen
# ═══════════════════════════════════════════════════════════
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

def _http_err(provider, e):
    if provider == "Cline" and e.code == 402:
        return {"error_type": "insufficient_credits",
                "error": "Cline-Guthaben aufgebraucht (Cline Credits). Bitte unter app.cline.bot aufladen."}
    if e.code in (401, 403):
        return {"error_type": "invalid_key",
                "error": provider + "-API-Key ist ungueltig oder gesperrt."}
    if e.code == 429:
        return {"error_type": "rate_limit",
                "error": provider + "-Rate-Limit erreicht. Bitte kurz warten."}
    return {"error_type": "api",
            "error": provider + "-Fehler (HTTP %d). Bitte spaeter erneut versuchen." % e.code}

def stream_gemini(messages, api_key, strength, model):
    if not api_key:
        yield {"type": "error", "error_type": "missing_key",
               "error": "Kein Gemini-API-Key hinterlegt (Admin-Panel -> API-Keys)."}
        return
    s = STRENGTHS.get(strength, STRENGTHS["medium"])
    contents = [{"role": ("user" if m["role"] == "user" else "model"), "parts": gemini_parts(m["content"])}
                for m in messages]
    payload = json.dumps({"contents": contents,
        "generationConfig": {"maxOutputTokens": s["max_tokens"], "temperature": s["temp"]}}).encode()
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           + model + ":streamGenerateContent?alt=sse&key=" + api_key)
    req = urllib.request.Request(url, data=payload,
        headers={"Content-Type": "application/json", "User-Agent": UA}, method="POST")
    got = False
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                blob = line[5:].strip()
                if not blob or blob == "[DONE]":
                    continue
                try:
                    d = json.loads(blob)
                except Exception:
                    continue
                cand = (d.get("candidates") or [{}])[0]
                parts = (cand.get("content") or {}).get("parts") or []
                txt = "".join(p.get("text", "") for p in parts)
                if txt:
                    got = True
                    yield {"type": "delta", "text": txt}
        if got:
            yield {"type": "end"}
            return
    except urllib.error.HTTPError as e:
        yield {"type": "error", **_http_err("Gemini", e)}
        return
    except Exception:
        pass
    r = call_gemini(messages, api_key, strength, model)   # Fallback ohne Stream
    if r["ok"]:
        yield {"type": "delta", "text": r["text"]}
        yield {"type": "end", "tokens": r.get("tokens", 0)}
    else:
        yield {"type": "error", "error_type": r.get("error_type", "api"), "error": r["error"]}

def stream_openai_style(messages, api_key, strength, model, provider, base_url):
    name = "Cline" if provider == "cline" else "Groq"
    if not api_key:
        yield {"type": "error", "error_type": "missing_key",
               "error": "Kein %s-API-Key hinterlegt (Admin-Panel -> API-Keys)." % name}
        return
    s = STRENGTHS.get(strength, STRENGTHS["medium"])
    msgs = [{"role": ("user" if m["role"] == "user" else "assistant"), "content": oai_content(m["content"])}
            for m in messages]
    body = {"model": model, "messages": msgs, "max_tokens": s["max_tokens"] + 512, "stream": True}
    if supports_temp(model):        # nur was das Modell unterstuetzt
        body["temperature"] = s["temp"]
    if provider != "cline":
        body["temperature"] = s["temp"]
        body["reasoning_effort"] = {"low": "low", "medium": "medium", "high": "high", "extra": "high"}.get(strength, "medium")
    payload = json.dumps(body).encode()
    headers = {"Content-Type": "application/json", "Authorization": "Bearer " + api_key,
               "User-Agent": UA, "Accept": "text/event-stream"}
    if provider == "cline":
        headers["HTTP-Referer"] = SYCHOS_HOME
        headers["X-Title"] = "Sychos"

    def once():
        req = urllib.request.Request(base_url, data=payload, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=120) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                blob = line[5:].strip()
                if blob == "[DONE]":
                    break
                try:
                    d = json.loads(blob)
                except Exception:
                    continue
                ch = d.get("choices") or []
                dl = (ch[0].get("delta") or {}) if ch else {}
                rc = dl.get("reasoning_content") or ""
                if rc:
                    yield {"think": rc}          # Thinking-Modus: Denkprozess live
                delta = dl.get("content") or ""
                if delta:
                    yield {"text": delta}

    got = False
    try:
        for ev in once():
            if ev.get("think"):
                yield {"type": "think", "text": ev["think"]}
                continue
            got = True
            yield {"type": "delta", "text": ev.get("text", "")}
        if got:
            yield {"type": "end"}
            return
    except urllib.error.HTTPError as e:
        yield {"type": "error", **_http_err(name, e)}
        return
    except Exception:
        pass
    r = call_cline(messages, api_key, strength, model) if provider == "cline" \
        else call_groq(messages, api_key, strength, model)
    if r["ok"]:
        yield {"type": "delta", "text": r["text"]}
        yield {"type": "end", "tokens": r.get("tokens", 0)}
    else:
        yield {"type": "error", "error_type": r.get("error_type", "api"), "error": r["error"]}

def stream_provider(messages, api_key, strength, model):
    info = MODELS.get(model, MODELS["gemini-3.6-flash"])
    prov = info["provider"]
    api_model = info.get("api_model", model)
    if prov == "groq":
        gen = stream_openai_style(messages, api_key, strength, api_model, "groq",
                                  "https://api.groq.com/openai/v1/chat/completions")
    elif prov == "cline":
        gen = stream_openai_style(messages, api_key, strength, api_model, "cline",
                                  "https://api.cline.bot/api/v1/chat/completions")
    elif prov == "ollama":
        gen = stream_ollama(messages, strength, api_model)
    else:
        gen = stream_gemini(messages, api_key, strength, model)
    for ev in gen:
        yield ev


def call_provider(messages, api_key, strength, model):
    info = MODELS.get(model, MODELS["gemini-3.6-flash"])
    prov = info["provider"]
    api_model = info.get("api_model", model)
    if prov == "groq":
        return call_groq(messages, api_key, strength, api_model)
    if prov == "cline":
        return call_cline(messages, api_key, strength, api_model)
    if prov == "ollama":
        return call_ollama(messages, strength, api_model)
    return call_gemini(messages, api_key, strength, model)

# ═══════════════════════════════════════════════════════════
#  EINGEBETTETES FRONTEND (Single-File-Modus)
# ═══════════════════════════════════════════════════════════
WEB_ASSETS = {
    "index.html": ("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" "QYDjFTY+4H8Vs9VoNTqqJF4N6P0f7VySqQ==", "text/html; charset=utf-8"),
    "status.html": ("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" "0/MKjHPlkAXKtO9CB/qvrP4Ppga4fQ==", "text/html; charset=utf-8"),
    "css/style.css": ("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", "text/css; charset=utf-8"),
    "js/app.js": ("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" "Mb3PzehazoNtTHYcW/178Pf/AtWt/u4=", "application/javascript; charset=utf-8"),
    "admin/index.html": ("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", "text/html; charset=utf-8"),
    "terms.html": ("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", "text/html; charset=utf-8"),
    "privacy.html": ("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", "text/html; charset=utf-8"),
    "refund.html": ("eNqNVs1u20YQvhfoO0xZILBRibLV/Bj6C+zGaQrHrRE7CZrbkhyRCy+Xwu5Ssl0U6Dv03lzyDOnFp+pN+iT9dilakpMAvWjJ4c43s998M6vRN89++eHi17Nj" "KlypJl9/NfIrKaHzcZRxFCwsMr+W7ASlhTCW3Th6ffG8exDd2bUoeRzNJS9mlXERpZV2rLFvITNXjDOey5S74aVDUksnheraVCge7wcUJ53iybGxTjhX69wa" "mRZOYSvTv3/8SefXaVHZUa/ZBwd8uiTDahxZd63YFsyIWxiejqNeam0vmGM8PZ2PH8V7cT/ECVY8EMWKc6HoNyrFVZPZgJ483ptdDWExudQD2iNRu2pIM5Fl" "UucDeojP1Pc/B2EjkuBuwTIv3ID24ydD+n0DutgH+hRMdKeilOp6QHNhdrrdYMp2h803K294QP3H24H3KBi24Pr/H25/C67fR8YrxLRSlWldHV+53e0os077" "pGQbb4X5ffzoswjd/j2MOBHpJZwzaWdKIFGpA1OJqtLLNq1uUjlXlcA92ED9NjuYpo/SIQXgjNPKCCcrHEJXmkOYUa8t4qjXijOpsmu/ZnJOqRLWjqOQShRK" "PRKt0Sd2p5Jo8kAJY4b0rjbLW2R8U5f0ok5GPdG4FfubiuzSA1HOhvRWZmxMPd2QKBLZb1xmFJIbR815VhUSUrvdaHLuhM4GdM4zx2XCBlLqPx71ZnBt4vUn" "+w/pQuSMlKZGcsYa6nI2F0ZoF+L0V3Emh0ox5Xwp6ilajc7U8oNm2rlg6zp0JKxMO3Rmqg6diqsOvVbOCNhri0JYu0uXy49aw2+UTCQeTCFUQvNK0yoDPeol" "IZQWaUEZl3SCSMQNH+xowQbpheasCkQ+4hws6gxUxXSMKPTKk8p3BMJ3WqncQQ0IhOwNvVh+LJiWt+DCh/J4tbEzj5MrsIv3d6JQnv0F5/E2VecVJQxajMit" "o6wm9kHXBVuTVauw4knJyXH3VEhFQkOeTLL06lgJQpYzg0pFk5/wAJbq0kuBCtjYQE+O6bye+QnXPcz8BhQEiGvsI3ZAmoKUv9Z5RPRdCJWwpDNMEpRtzmbB" "OmMANul8EU8YuAX2stqgEChXgxGnVUkvXRb7OtHOKeMryKBqSq9812S7tK4rgskcfL9lc+lQ3Lswo17gpqV0LW2GtDfV9oZNYkSNkhgqRILCZMJCfrgMbkKl" "aNt3S0oxnUoXNOTVUAqFZDQdJtgNa1N6mUuH60DTS5Y2HNg6WZbezdDPtbvB4hEgM5wrWO92itpmoYN9JuhiqnVGOnjf1AH+hLV2WtqOT9uG3O/l66vjVWkd" "+igLbOHMQVzQ7fJWqUbCSlpsH3oIDxw07ox0rsVRLJMWztygeAFrLYemoU7F8n0OHsNB3tXW93zlaa01mmF5Wxh3T+6HtdWiKH1jrstyhDAebwEqQhlKUG1t" "UyrauYmPYlr4OWKKSkFtJ8sP9ZQDP+uMWGMcCN00xNrsQ4gkZ8WFbts9Jh/xxPe5zIMrPvjfN76y1fI9JOFfPdK6r9A9pY0mvoq+lRP2t2nwbkdtOxou7/Xw" "PQ4uQLH2BWA/2kB1yU836XguUMBA6TPJGlNB+xFNGoETYQhyCUf3G55z4SdQs2HdKfjnA1B0ovZ7jPdIuPCV8SEW0mQdGKxjeDGs0xDxUGNoecb9iNxS0RfO" "snFPWekYd3gFkqLJJ5wd/ngUZtA/f2/wiSk1F+l1NHkmMPpBB8736S784rDR5N4Y/nTj58feqIckwyXbru0l21v9WfwPzh15NQ==", "text/html; charset=utf-8"),
    "imprint.html": ("eNqNVdtu3DYQfS/Qf5iqQGEDK+0l8QV7UeFkXduIb4jjAnXRB0oarQhTpEBSu14XBfoP/YC8GOgfpC9+yv5Jv6RD7srdTdwiL+JwNHPmPhx+M754/e6ny0Mo" "bCnir78auhMEk5NRkGHgOcgyd5ZoGaQF0wbtKLh+90O4HzzxJStxFEw5ziqlbQCpkhYlyc14ZotRhlOeYugvLeCSW85EaFImcNT1KJZbgfFJWWk0pi7h79//" "gKt5WigzbC//kZDg8hY0ilFg7FygKRDJVqExHwXt1Ji2Z0dEfT8d7USdqOexPZcIgEjghAn4FUp2t/SmD3u7nepuQBw94bIPHWC1VQOoWJZxOenDS/oNPffZ" "94LkBIYF8klh+9CN9gbw2xp00SX0nKIPc1ZyMe/DlOmtMPSsbHuw/Gf4Pfaht7tpuAOesQHX+3K47gZcr0cerxBTJZRuVC3e2e1NK1WroQRv7K0wX0Q7zyKE" "vU8wooSlt6SccVMJRo5y6TOVCJXeNm6FibJWlYS7v4b6bbafpzvpADxwhqnSzHJFQUglsTFjVaYIv9HJ82R/N1klYLYqx16n48WH7abmw3bTv4nK5u7M+BRS" "wYwZBd7zwHfGkDVMF8dTUwXxd4JpPYCbWi8eKcB76s3jOhm22VKt6P7btGSru+RW4O2PgqW3q5oxLu12EB/ICUtQwgTLxcPiPXz8E3ZgPD4ativSXqL24h9R" "M2lnNE2CpwVqaMOBTDha1GSnt7LjDyKS+IzdwZFmhRy2kxiGpmKyichlLoh/Tri1CFMlhLGLB5nxCTlxToMrAckzzej+C2WONONhohvoZ5CuSHjxHqGWGRyz" "2si6LFF/me7l6Q1caNsIt2CMtTVpQTsn8zFtZOENVZfd2vWID8MzxkX/ecfqyu2fcCnz32FdX9nwJDvX0fMwOaMcUaZ0QU6hbIFR0li4QS4QxOIDubuG+T9l" "g3zxqIEg4EQWTGzE8VQw2DqQhKh5bmHGERQ1x/Ym6jGX1OKGuk/DDStELSeGJTOe3jpyHXVMAJ9LAOpciYmFxWNCrUTtcknrza1KVcKpzSLXNC140YELkcEr" "yh3OW3CqZKYkHL5+eQZ7B9ctZ4Lio6zyiUUDbxYfiKI7Bbp1hpoeB2lB5fDWjXC2HTlpVwDfwdS+pnJDRCT1DGk/1DllJkFjsfDzQJXyDjpDSwcHlDwNFJTL" "v0GRGJcjnflE1fTGaDe5q2GlW2mC+Ly29y78BN0CJwqlG1dnyGUZ3eyhTjRLCxut5XltMRhukXasIsQg/szAwdErD/jxrzXjleZTls6DeMzo5SNL5MXnUvSl" "oQnit26ZoDaWWesr+Kkgp6VC3RusbxdGvUZO+q3WnM1Wa68e8H8AzDaQWA==", "text/html; charset=utf-8"),
    "404.html": ("eNqVVtuO2zYQfS/Qf5iqSLDBWrLstTde35qgaVH0oVl00wLpGy2NJNYUKZCULxsE6Ef0c/rWP+mXdEhJvm2KoLuQRHLODOdyOPT8qzdvv333/v47KGwpll9+" "MXdfEEzmiyDFwK8gS923RMsgKZg2aBfBL+++DyfBYV2yEhfBhuO2UtoGkChpURJuy1NbLFLc8ARDP+kBl9xyJkKTMIGLQRRf2kmUUJrEBZZ4Yitlen0JtQ4T" "eoUT5NfxJL6LUw+23ApcjuIR/PPHn/CwTwpl5v1mlcSCyzVoFIuAk3oAhcZsEWRs46aR2eQB2H1FO/GS5dinhetdKYJz1UojoSUmtjNQWFuZab+fkUsmypXK" "BbKKmyhR5f9VNpZZnnhNSLQyRmmec3mw8vkd+4kxw28yVnKxX7ytbcbtdJsX9tU4jme39LykZxLHz1vIvajN9Y9szbRl1w9MmgY9ItSJxvOUm0qw/cJsWRU0" "wRi7F2gKRHsR5Ymgddg51ffLEY083M9oABDJLNxqVsEHNwOolCHSKDl11igdG5zBY8hlirspDGZQICcPaRjHm2LW6LTuTSETuJsBEzyXIbdYmikkxBPUM/i9" "NpZn+7ClzlFQsTTlMp/CcFTtvMGPnV8J0yl8AIs7G3qjR62S7RqWT2F8G1duV0nEaT3nBiG6NZDUK56EK3zkqK+iYS+a9KKb3uDF7GQPlWIXu6to2FRmChum" "r8LQL6Wk4AfbNniq4OxExfBHJNcEK6urwZC86cFwuNm6N01ImaqDYZe56K7VFWgpltBULPEJCKN4hOUM/CGbgtXEh4ppirhVoP1Xa25DnxBjtVrTtsNq1/pK" "ZsjTBtpACpaq7RRi+ndJOsGFuVDbDnySukzwZI0aRoa6R+YaCB5r8mqN+0xTOzAHXJu5+FkP7m7c65ZexI1nVDflArN7T5uPDe5u5DDjM2k0HrfyQ1EsyzvL" "B25x6bO4EipZu/prOplkekJhUXSOAQcm0QwGHZsAVkqnSAkd0LJRgqdP89BiQs1SXhNrb+KD9mdJ0VR/MHAunLHk5YEll5WOblydfY18lTOlyynUVYU6YQYP" "FLis6/nJKAaUxicEHA4d/0aOfjcN+5pchStlrSrdyXWenpui43++Z1lbvIxwFI2d5oW54eTMHEsckQzZu+gKOavIxtCB/7MXXLLMH2SKUavylDHxDE7S5ofU" "qvC9j90dbrDqgn8neKkkOgxtNu93jXDe767flUr37pvyjcuoMYugbZGBb5gXApe+RvBUpNzNThfivE+CT2OI6cHygVobE7BBTRVAeQYvBssHpFMIkieFhRyz" "mnoxYUjQQqrlG46UqJ8VFQ1wxym7qG2rcVVioV8AL9srOfwJ7WM0X+lWnf5+5SgEevS2pnMAhiPU5Qolk9L665xC0bBlmowiFGQ+mverT4fUMiA42J+zTrqy" "EugJK00NR+8PF1Sw/K3Wf/+VrOGxLuGHejXvs0+rH1RYWnJJiq/dN7xnEsVR6ZjBbnT8dvXt+19i/wLI3AeU", "text/html; charset=utf-8"),
    "favicon.svg": ("eNp1Uk1vgzAMvU/af7DSy3ZICCEEOsEOu+y0H8FC+NAYQSEt9N/PoaXSJk0iefbzs7GtFPO5hfV7GOeSdN5PL1G0LAtbEmZdGwnOeYQKAufeLG92LQkHDkri" "R14fHwCK2jTzZqE99KOp3Lur6t6MHvq6JJi6xphF4HKFVZQkRm+DWyKmzt5OYJtmNn6TBZ9qO1hXkkMe57rJSfSPPP4j5+pT1fIuL6LffV37ju6NF85oD0tf" "+64kOBd0pm87f7UdjhwrAk0/DKH0J6/j6la6mCrfAQ75ISUIrmnCUjyC5kxSyXIaJ+gqqiDG+8gkZBjIGPqCHZFMQELKBCgM5YjhiBBmKUYEy/CWGOPIZBSL" "IHekcrsFy4HTFNmEKozif8m+Hri1O9rRhN04+2VKcnLD06F93gl6mzi9E2FNuppK4uxprPchde/0YEDjIiRq9QUxLAZhX8teOWQU4b0g/gAsK5qT", "image/svg+xml"),
}

def get_embedded(rel):
    """Eingebettete Frontend-Datei (gzip+base64), falls keine auf Disk liegt."""
    if not WEB_ASSETS:
        return None
    key = rel.replace("\\", "/").lstrip("/")
    if key in ("", "index.html"):
        key = "index.html"
    item = WEB_ASSETS.get(key)
    if item is None:
        return None
    return zlib.decompress(base64.b64decode(item[0])), item[1]


# ═══════════════════════════════════════════════════════════
#  HTTP HANDLER
# ═══════════════════════════════════════════════════════════
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        try:
            ts = time.strftime("%H:%M:%S")
            sys.stderr.write(f"  [{ts}] {fmt % args}\n")
        except Exception:
            pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Requested-With, Accept")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")   # nie alte Frontends im Cache

    def _json(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        ln = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(ln) if ln else b""
        self._raw_body = raw                    # fuer Webhook-Signaturpruefung
        try:
            return json.loads(raw) if raw else {}
        except Exception:
            return {}

    def _client_ip(self):
        """Echte Client-IP. Hinter Reverse-Proxy (Caddy/nginx auf 127.0.0.1) zaehlt der
        erste X-Forwarded-For-Eintrag – sonst haetten ALLE User die Proxy-IP und die
        Per-IP-Limits (Register/Login) wuerden falsch greifen. Nur vom localhost
        vertrauen wir dem Header (kein Spoofing von aussen)."""
        sock_ip = self.client_address[0] if self.client_address else ""
        if sock_ip in ("127.0.0.1", "::1"):
            xff = (self.headers.get("X-Forwarded-For", "") or "").strip()
            if xff:
                return xff.split(",")[0].strip()
        return sock_ip or "?"

    def _get_user(self):
        """Auth per Session-Token (Bearer). Gibt User-Dict oder None zurueck."""
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return None
        token = auth[7:].strip()
        db = get_db()
        row = db.execute("SELECT u.* FROM sessions s JOIN users u ON u.uid=s.uid WHERE s.token=?",
                         (token,)).fetchone()
        user = dict(row) if row else None
        if user:
            # Zeitlich begrenzte Sperre automatisch aufheben
            if user["is_banned"] and user.get("ban_until", 0) and time.time() > user["ban_until"]:
                db.execute("UPDATE users SET is_banned=0, ban_reason='', ban_until=0 WHERE uid=?",
                           (user["uid"],))
                db.commit()
                user["is_banned"] = 0
                user["ban_reason"] = ""
                user["ban_until"] = 0
            # Zeitlich begrenztes Paid laeuft automatisch ab
            if user.get("is_paid") and user.get("paid_until", 0) and time.time() > user["paid_until"]:
                db.execute("UPDATE users SET is_paid=0, paid_until=0 WHERE uid=?", (user["uid"],))
                db.commit()
                user["is_paid"] = 0
                user["paid_until"] = 0
            # Abgelaufene Plaene laufen automatisch aus (zurueck auf Free)
            if user.get("plan", "free") != "free" and user.get("plan_until", 0) \
                    and time.time() > user["plan_until"]:
                db.execute("UPDATE users SET plan='free', plan_until=0 WHERE uid=?", (user["uid"],))
                db.commit()
                user["plan"] = "free"
                user["plan_until"] = 0
            db.execute("UPDATE users SET last_seen=? WHERE uid=?", (time.time(), user["uid"]))
            db.commit()
        db.close()
        return user

    def _ban_info(self, user):
        """Sperr-Details fuer das Frontend-Modal."""
        until = user.get("ban_until", 0) or 0
        return {"banned": True,
                "ban_reason": user.get("ban_reason", ""),
                "ban_until": until,
                "ban_permanent": not bool(until)}

    def _require_user(self, allow_banned=False):
        """Auth + serverseitige Ban-Pruefung. Gibt (user, error) zurueck."""
        user = self._get_user()
        if not user:
            return None, (401, {"ok": False, "error": "Nicht angemeldet"})
        if user["is_banned"] and not allow_banned:
            d = {"ok": False, "error": "Account gesperrt"}
            d.update(self._ban_info(user))
            return None, (403, d)
        return user, None

    def _check_admin(self):
        """Nur angemeldete Admin-User (Session-Token). Kein statischer Admin-Key mehr."""
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return False
        tok = auth[7:].strip()
        if not tok:
            return False
        db = get_db()
        row = db.execute("SELECT u.is_admin FROM sessions s JOIN users u ON u.uid=s.uid WHERE s.token=?",
                         (tok,)).fetchone()
        db.close()
        return bool(row and row["is_admin"])

    def _valid_image_payload(self, image):
        """Bild-Uploads pruefen: leer = ok, Data-URLs nur als Bild, keine gefaehrlichen Dateitypen."""
        img = (image or "").strip()
        if not img:
            return True
        low = img.lower()
        for bad in (".exe", ".bat", ".js", ".scr", ".msi", ".dll", ".sh", ".php"):
            if bad in low:
                self._json(400, {"ok": False, "error": "Nur Bilder erlaubt (keine .exe o.ä.)."})
                return False
        if low.startswith("data:") and not re.match(r"^data:image/(png|jpe?g|webp|gif);base64,", low):
            self._json(400, {"ok": False, "error": "Nur Bilder erlaubt (keine .exe o.ä.)."})
            return False
        return True

    def _social_user(self, row, now):
        """Kompaktes User-Objekt fuer Freunde/Suche (inkl. online-Status)."""
        return {"uid": row["uid"], "display_name": row["display_name"] or "",
                "avatar": row["avatar"] or "", "verified": bool(row["verified"]),
                "online": (now - (row["last_seen"] or 0)) < ONLINE_WINDOW}

    def _channel_view(self, db, row, me):
        """Kompaktes Channel-Objekt (inkl. Member-Zahl, Rolle, Beitritt)."""
        uid = row["id"]
        cnt = db.execute("SELECT COUNT(*) AS n FROM channel_members WHERE channel_id=? AND banned=0", (uid,)).fetchone()["n"]
        joined = bool(row["joined"]) or (row["owner_uid"] == me)
        my_role = "owner" if (row["owner_uid"] == me) else (row["my_role"] or ("member" if joined else ""))
        return {"id": uid, "name": row["name"] or "", "kind": row["kind"] or "community",
                "description": row["description"] or "", "verified": bool(row["verified"]),
                "member_count": int(cnt), "role": my_role, "joined": joined,
                "owner_uid": row["owner_uid"] or ""}

    def _send_500_page(self, code):
        """500.html (Server-Unavailable) aus ROOT ausliefern. True = ausgeliefert."""
        data, ctype = None, "text/html; charset=utf-8"
        f500 = os.path.join(ROOT, "500.html")
        if os.path.isfile(f500):
            try:
                with open(f500, "rb") as f:
                    data = f.read()
            except Exception:
                data = None
        if data is None:
            emb = get_embedded("500.html")
            if not emb:
                return False
            data, ctype = emb
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self._cors()
        self.end_headers()
        self.wfile.write(data)
        return True

    def _serve_static(self, path):
        """Liefert Frontend-Dateien aus dem Projekt-Root aus."""
        # Wartungsmodus: Browser-Anfragen auf "/" sehen die 500-Seite (Server-Unavailable).
        if path in ("/", "/index.html") and "text/html" in (self.headers.get("Accept", "") or ""):
            if maintenance_on():
                u = self._get_user()
                if not (u and u.get("is_admin")) and self._send_500_page(500):
                    return
        if path in ("/", "/index.html"):
            path = "/index.html"
        elif path in ("/admin", "/admin/"):
            path = "/admin/index.html"
        elif path in ("/terms", "/privacy", "/refund", "/imprint", "/home"):
            path = path + ".html"   # Rechtsseiten: /terms -> terms.html usw. · /home -> home.html
        rel = unquote(path).lstrip("/")
        full = os.path.normpath(os.path.join(ROOT, rel))
        if not full.startswith(os.path.normpath(ROOT)):
            self._json(403, {"ok": False, "error": "Zugriff verweigert"}); return
        if os.path.isfile(full):
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            with open(full, "rb") as f:
                data = f.read()
        else:
            emb = get_embedded(rel)
            if emb is None:
                # 404-Seite: echter 404-Status mit moderner "Page Not Found"
                nf = get_embedded("404.html")
                if nf and "." not in os.path.basename(rel):
                    data, ctype = nf
                    self.send_response(404)
                    self.send_header("Content-Type", ctype)
                    self._cors()
                    self.end_headers()
                    self.wfile.write(data)
                    return
                self._json(404, {"ok": False, "error": "Nicht gefunden"}); return
            data, ctype = emb
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    # ── GET ───────────────────────────────────────────────
    def do_GET(self):
        note_request()
        path = urlparse(self.path).path

        if path == "/status":
            # Browser (Accept: text/html) -> oeffentliche Status-Seite, Clients/Curl -> JSON
            if "text/html" in (self.headers.get("Accept", "") or ""):
                self._serve_static("/status.html"); return
            maint = maintenance_on()
            db = get_db()
            mmsg = get_setting(db, "maintenance_message")
            db.close()
            self._json(200, {"ok": True, "server": "Sychos Oracle", "version": APP_VERSION, "build": "24.09.2026-Final",
                "uptime": int(time.time() - START_TIME), "load": len(request_times),
                "maintenance": maint, "maintenance_message": mmsg,
                "login_open": not maint, "chat_open": not maint,
                "models_total": len(MODELS),
                "models_free": sum(1 for v in MODELS.values() if v.get("plan", "free") == "free"),
                "plans": [{"id": p, "name": PLANS[p]["name"], "price": PLANS[p]["price"]} for p in PLAN_ORDER]}); return

        if path == "/models":
            u = self._get_user()
            if maintenance_on() and not (u and u.get("is_admin")):
                self._json(503, {"ok": False, "maintenance": True,
                                 "error": "Server derzeit nicht erreichbar"}); return
            plan = effective_plan(u)
            is_boss = bool(u and u.get("is_admin"))
            paid_ok = bool(is_boss or plan != "free")
            self._json(200, {"ok": True, "is_paid": paid_ok, "plan": plan, "load_factor": load_factor(),
                "models": [{"id": k, "name": v["name"], "provider": v["provider"],
                            "factor": v["factor"], "paid": bool(v.get("paid")),
                            "vision": bool(v.get("vision")), "reasoning": bool(v.get("reasoning")),
                            "plan": v.get("plan", "free"),
                            "trial": int(v.get("trial") or 0),
                            "trial_left": trial_left_days(u["uid"], k) if (u and v.get("trial")) else 0,
                            "locked": (not is_boss) and (not plan_ok(plan, v.get("plan", "free")))
                                      and not (u and trial_free(u["uid"], k))}
                           for k, v in MODELS.items()],
                "strengths": [{"id": k, "name": v["name"], "cost": v["cost"],
                               "max_tokens": v["max_tokens"]}
                              for k, v in STRENGTHS.items()]}); return

        # User holt seine offenen Warnungen (Admin-Warnsystem)
        if path == "/warnings":
            user, err = self._require_user(allow_banned=True)
            if err:
                self._json(err[0], err[1]); return
            db = get_db()
            rows = db.execute("SELECT id,message,created_at FROM warnings WHERE uid=? AND read=0 ORDER BY id",
                              (user["uid"],)).fetchall()
            db.close()
            self._json(200, {"ok": True, "warnings": [dict(r) for r in rows]}); return

        # Plaene & Token-Limits (public – wird im Settings-Planpicker angezeigt)
        if path == "/plans":
            self._json(200, {"ok": True, "days": PLAN_DAYS,
                "payments": bool(STRIPE_SECRET_KEY and STRIPE_PUBLISHABLE_KEY),
                "mor": {
                    "paddle": {"enabled": bool(PADDLE_CLIENT_TOKEN and PADDLE_PRICES),
                               "token": PADDLE_CLIENT_TOKEN, "prices": PADDLE_PRICES,
                               "links": PADDLE_LINKS},
                    "lemonsqueezy": {"enabled": bool(LEMONSQUEEZY_STORE and LEMONSQUEEZY_VARIANTS),
                                     "store": LEMONSQUEEZY_STORE, "variants": LEMONSQUEEZY_VARIANTS}},
                "min_amount": 0.50,
                "plans": [{"id": pid, "name": PLANS[pid]["name"], "price": PLANS[pid]["price"],
                           "desc": PLANS[pid]["desc"],
                           "limits": {"5h": PLANS[pid]["t5"], "week": PLANS[pid]["tweek"],
                                      "month": PLANS[pid]["tmonth"]}}
                          for pid in PLAN_ORDER]}); return

        if path == "/me":
            user = self._get_user()
            if not user:
                self._json(401, {"ok": False, "error": "Nicht angemeldet"}); return
            plan = effective_plan(user)
            db = get_db()
            mmsg = get_setting(db, "maintenance_message")
            db.close()
            d = {"ok": True, "uid": user["uid"], "email": user["email"],
                 "display_name": user["display_name"], "credits": user["credits"],
                 "is_admin": user["is_admin"], "is_paid": bool(user["is_paid"]),
                 "verified": bool(user.get("verified")), "avatar": user.get("avatar", "") or "",
                 "version": APP_VERSION, "maintenance_message": mmsg,
                 "maintenance": maintenance_on(),
                 "created_at": user.get("created_at", 0),
                 "paid_until": user.get("paid_until", 0) or 0,
                 "plan": plan, "plan_name": PLANS[plan]["name"],
                 "plan_until": user.get("plan_until", 0) or 0,
                 "totp_on": bool(user.get("totp_on")),
                 "usage": usage_state(user["uid"], plan)}
            if user["is_banned"]:
                d.update(self._ban_info(user))
            self._json(200, d); return

        if path == "/chats":
            user = self._get_user()
            if not user:
                self._json(401, {"ok": False, "error": "Nicht angemeldet"}); return
            db = get_db()
            rows = db.execute("SELECT id,title,model,created_at FROM chats WHERE uid=? ORDER BY created_at DESC",
                              (user["uid"],)).fetchall()
            db.close()
            self._json(200, {"ok": True, "chats": [dict(r) for r in rows]}); return

        if path.startswith("/messages/"):
            user = self._get_user()
            if not user:
                self._json(401, {"ok": False, "error": "Nicht angemeldet"}); return
            cid = path.split("/")[2]
            db = get_db()
            own = db.execute("SELECT id FROM chats WHERE id=? AND uid=?", (cid, user["uid"])).fetchone()
            rows = []
            if own:
                rows = db.execute("SELECT role,content,model,cost,created_at,images FROM messages WHERE chat_id=? ORDER BY id",
                                  (cid,)).fetchall()
            db.close()
            if not own:
                self._json(404, {"ok": False, "error": "Chat nicht gefunden"}); return
            out = []
            for r in rows:
                d2 = dict(r)
                try:
                    d2["images"] = json.loads(d2.get("images") or "[]")
                except Exception:
                    d2["images"] = []
                out.append(d2)
            self._json(200, {"ok": True, "messages": out}); return

        # User-eigene Keys: Feature entfernt
        if path == "/settings/keys":
            self._json(410, {"ok": False, "error": "Eigene API-Keys sind entfernt."}); return

        # ── 2.0: Benachrichtigungen (eigene + globale) ──
        if path == "/notifications":
            user, err = self._require_user(allow_banned=True)
            if err:
                self._json(err[0], err[1]); return
            uid = user["uid"]
            db = get_db()
            rows = db.execute(
                "SELECT * FROM notifications WHERE to_uid IN (?, '', '*') OR from_uid=? "
                "ORDER BY created_at DESC LIMIT 100", (uid, uid)).fetchall()
            unread = db.execute(
                "SELECT COUNT(*) AS n FROM notifications WHERE read=0 AND to_uid IN (?, '', '*')",
                (uid,)).fetchone()["n"]
            profiles = {r["uid"]: r for r in
                        db.execute("SELECT uid, display_name, avatar FROM users").fetchall()}
            db.close()
            items = []
            for r in rows:
                d2 = dict(r)
                src = profiles.get(r["from_uid"])
                d2["from_name"] = ("Sychos Team" if (r["kind"] or "") in ("admin", "system")
                                   else ((src["display_name"] if src else "") or "Unbekannt"))
                d2["from_avatar"] = (src["avatar"] if src else "") or ""
                items.append(d2)
            self._json(200, {"ok": True, "items": items, "unread": int(unread)}); return

        # ── 2.0: Freunde (accepted + eingehende/ausgehende Anfragen) ──
        if path == "/friends":
            user, err = self._require_user(allow_banned=True)
            if err:
                self._json(err[0], err[1]); return
            uid = user["uid"]
            now = time.time()
            db = get_db()
            friends = [self._social_user(r, now) for r in db.execute(
                "SELECT u.uid,u.display_name,u.avatar,u.verified,u.last_seen FROM friends f "
                "JOIN users u ON u.uid=f.friend_uid WHERE f.uid=? AND f.status='accepted'",
                (uid,)).fetchall()]
            incoming = [self._social_user(r, now) for r in db.execute(
                "SELECT u.uid,u.display_name,u.avatar,u.verified,u.last_seen FROM friends f "
                "JOIN users u ON u.uid=f.uid WHERE f.friend_uid=? AND f.status='pending'",
                (uid,)).fetchall()]
            outgoing = [self._social_user(r, now) for r in db.execute(
                "SELECT u.uid,u.display_name,u.avatar,u.verified,u.last_seen FROM friends f "
                "JOIN users u ON u.uid=f.friend_uid WHERE f.uid=? AND f.status='pending'",
                (uid,)).fetchall()]
            db.close()
            self._json(200, {"ok": True, "friends": friends,
                             "incoming": incoming, "outgoing": outgoing}); return

        # ── 2.0: Usersuche (max. 20 Treffer, ohne sich selbst) ──
        if path == "/users/search":
            user, err = self._require_user(allow_banned=True)
            if err:
                self._json(err[0], err[1]); return
            q = (urllib.parse.parse_qs(urlparse(self.path).query).get("q", [""])[0] or "").strip()
            like = "%" + q + "%"
            now = time.time()
            db = get_db()
            rows = db.execute(
                "SELECT uid,display_name,avatar,verified,last_seen FROM users "
                "WHERE uid<>? AND (display_name LIKE ? OR email LIKE ?) "
                "ORDER BY display_name LIMIT 20", (user["uid"], like, like)).fetchall()
            db.close()
            self._json(200, {"ok": True, "users": [self._social_user(r, now) for r in rows]}); return

        # ── 2.1: Channels / Communities / Gruppen (GET) ──
        if path == "/channels":
            user, err = self._require_user(allow_banned=True)
            if err:
                self._json(err[0], err[1]); return
            kind = (urllib.parse.parse_qs(urlparse(self.path).query).get("kind", ["community"])[0] or "community").strip()
            if kind not in ("community", "group"):
                kind = "community"
            db = get_db()
            rows = db.execute(
                "SELECT c.*, m.role AS my_role, m.uid AS joined FROM channels c "
                "LEFT JOIN channel_members m ON m.channel_id=c.id AND m.uid=? "
                "WHERE c.kind=? AND (c.kind='community' OR m.uid IS NOT NULL OR c.owner_uid=?) "
                "ORDER BY c.verified DESC, c.created_at DESC",
                (user["uid"], kind, user["uid"])).fetchall()
            out = [self._channel_view(db, r, user["uid"]) for r in rows]
            db.close()
            self._json(200, {"ok": True, "channels": out}); return

        if path == "/channels/search":
            user, err = self._require_user(allow_banned=True)
            if err:
                self._json(err[0], err[1]); return
            q = (urllib.parse.parse_qs(urlparse(self.path).query).get("q", [""])[0] or "").strip()
            like = "%" + q + "%"
            db = get_db()
            rows = db.execute(
                "SELECT c.*, m.role AS my_role, m.uid AS joined FROM channels c "
                "LEFT JOIN channel_members m ON m.channel_id=c.id AND m.uid=? "
                "WHERE c.kind='community' AND (c.name LIKE ? OR c.description LIKE ?) "
                "ORDER BY c.verified DESC, c.name LIMIT 20",
                (user["uid"], like, like)).fetchall()
            out = [self._channel_view(db, r, user["uid"]) for r in rows]
            db.close()
            self._json(200, {"ok": True, "channels": out}); return

        if path == "/channels/messages":
            user, err = self._require_user(allow_banned=True)
            if err:
                self._json(err[0], err[1]); return
            cid = (urllib.parse.parse_qs(urlparse(self.path).query).get("id", [""])[0] or "").strip()
            now = time.time()
            db = get_db()
            ch = db.execute("SELECT * FROM channels WHERE id=?", (cid,)).fetchone()
            if not ch:
                db.close(); self._json(404, {"ok": False, "error": "Kanal nicht gefunden"}); return
            mem = db.execute("SELECT * FROM channel_members WHERE channel_id=? AND uid=?", (cid, user["uid"])).fetchone()
            is_owner = (ch["owner_uid"] == user["uid"])
            if not is_owner and not (mem and not mem["banned"]):
                db.close(); self._json(403, {"ok": False, "error": "Bitte zuerst beitreten."}); return
            my_role = "owner" if is_owner else (mem["role"] if mem else "member")
            mrows = db.execute(
                "SELECT m.uid,m.role,m.banned,u.display_name,u.avatar,u.verified,u.last_seen "
                "FROM channel_members m JOIN users u ON u.uid=m.uid WHERE m.channel_id=? "
                "ORDER BY CASE m.role WHEN 'owner' THEN 0 WHEN 'admin' THEN 1 ELSE 2 END, u.display_name",
                (cid,)).fetchall()
            members = [{"uid": r["uid"], "display_name": r["display_name"] or "", "avatar": r["avatar"] or "",
                        "verified": bool(r["verified"]), "role": r["role"], "banned": bool(r["banned"]),
                        "online": (now - (r["last_seen"] or 0)) < ONLINE_WINDOW} for r in mrows]
            msgs = db.execute(
                "SELECT cm.id,cm.uid,cm.content,cm.image,cm.created_at,u.display_name,u.avatar,u.verified "
                "FROM channel_messages cm LEFT JOIN users u ON u.uid=cm.uid "
                "WHERE cm.channel_id=? ORDER BY cm.id ASC LIMIT 200", (cid,)).fetchall()
            messages = [{"id": r["id"], "uid": r["uid"], "display_name": r["display_name"] or "",
                         "avatar": r["avatar"] or "", "verified": bool(r["verified"]),
                         "content": r["content"] or "", "image": r["image"] or "",
                         "created_at": r["created_at"]} for r in msgs]
            db.close()
            self._json(200, {"ok": True, "messages": messages, "members": members, "my_role": my_role,
                             "channel": {"id": ch["id"], "name": ch["name"], "kind": ch["kind"],
                                          "description": ch["description"], "verified": bool(ch["verified"]),
                                          "owner_uid": ch["owner_uid"]}}); return

        # ── 2.1: Direkt-Nachrichten (GET) ──
        if path == "/dm/list":
            user, err = self._require_user(allow_banned=True)
            if err:
                self._json(err[0], err[1]); return
            me = user["uid"]
            now = time.time()
            db = get_db()
            uids = set()
            for r in db.execute("SELECT friend_uid AS u FROM friends WHERE uid=? AND status='accepted'", (me,)).fetchall():
                uids.add(r["u"])
            for r in db.execute("SELECT sender_uid AS u FROM dm_messages WHERE recipient_uid=? "
                                "UNION SELECT recipient_uid AS u FROM dm_messages WHERE sender_uid=?", (me, me)).fetchall():
                uids.add(r["u"])
            threads = []
            for u in uids:
                if u == me:
                    continue
                ur = db.execute("SELECT uid,display_name,avatar,verified,last_seen FROM users WHERE uid=?", (u,)).fetchone()
                if not ur:
                    continue
                last = db.execute("SELECT content,created_at FROM dm_messages "
                                  "WHERE (sender_uid=? AND recipient_uid=?) OR (sender_uid=? AND recipient_uid=?) "
                                  "ORDER BY id DESC LIMIT 1", (me, u, u, me)).fetchone()
                unread = db.execute("SELECT COUNT(*) AS n FROM dm_messages WHERE sender_uid=? AND recipient_uid=? AND read=0",
                                    (u, me)).fetchone()["n"]
                threads.append({"uid": u, "display_name": ur["display_name"] or "", "avatar": ur["avatar"] or "",
                                "verified": bool(ur["verified"]),
                                "online": (now - (ur["last_seen"] or 0)) < ONLINE_WINDOW,
                                "last_message": (last["content"] if last else ""),
                                "last_at": (last["created_at"] if last else 0), "unread": int(unread)})
            db.close()
            threads.sort(key=lambda x: x.get("last_at") or 0, reverse=True)
            self._json(200, {"ok": True, "threads": threads}); return

        if path == "/dm/messages":
            user, err = self._require_user(allow_banned=True)
            if err:
                self._json(err[0], err[1]); return
            me = user["uid"]
            peer_ref = (urllib.parse.parse_qs(urlparse(self.path).query).get("uid", [""])[0] or "").strip()
            db = get_db()
            peer, _ = resolve_user_ref(db, peer_ref)
            if not peer:
                db.close(); self._json(404, {"ok": False, "error": "User nicht gefunden"}); return
            now = time.time()
            db.execute("UPDATE dm_messages SET read=1 WHERE sender_uid=? AND recipient_uid=?", (peer, me))
            db.commit()
            rows = db.execute("SELECT id,sender_uid,content,image,created_at FROM dm_messages "
                              "WHERE (sender_uid=? AND recipient_uid=?) OR (sender_uid=? AND recipient_uid=?) "
                              "ORDER BY id ASC LIMIT 200", (me, peer, peer, me)).fetchall()
            messages = [{"id": r["id"], "sender_uid": r["sender_uid"], "content": r["content"] or "",
                         "image": r["image"] or "", "created_at": r["created_at"],
                         "mine": (r["sender_uid"] == me)} for r in rows]
            pr = db.execute("SELECT uid,display_name,avatar,verified,last_seen FROM users WHERE uid=?", (peer,)).fetchone()
            peer_obj = self._social_user(pr, now) if pr else {"uid": peer}
            db.close()
            self._json(200, {"ok": True, "messages": messages, "peer": peer_obj}); return

        # ── 2.1: Öffentliches Profil ──
        if path == "/users/profile":
            user, err = self._require_user(allow_banned=True)
            if err:
                self._json(err[0], err[1]); return
            ref = (urllib.parse.parse_qs(urlparse(self.path).query).get("uid", [""])[0] or "").strip()
            db = get_db()
            uid, _ = resolve_user_ref(db, ref)
            if not uid:
                db.close(); self._json(404, {"ok": False, "error": "User nicht gefunden"}); return
            u = db.execute("SELECT uid,display_name,avatar,verified,created_at,plan FROM users WHERE uid=?", (uid,)).fetchone()
            if not u:
                db.close(); self._json(404, {"ok": False, "error": "User nicht gefunden"}); return
            rel = db.execute("SELECT status FROM friends WHERE uid=? AND friend_uid=?", (user["uid"], uid)).fetchone()
            is_friend = bool(rel and rel["status"] == "accepted")
            pending = bool(rel and rel["status"] == "pending")
            uplan = u["plan"] or "free"
            db.close()
            self._json(200, {"ok": True,
                             "user": {"uid": u["uid"], "display_name": u["display_name"] or "",
                                       "avatar": u["avatar"] or "", "verified": bool(u["verified"]),
                                       "created_at": u["created_at"] or 0,
                                       "plan_name": PLANS.get(uplan, {}).get("name", "Free")},
                             "is_friend": is_friend, "request_pending": pending}); return

        if path == "/admin/users":
            if not self._check_admin():
                self._json(403, {"ok": False, "error": "Kein Admin"}); return
            db = get_db()
            db.execute("UPDATE users SET is_paid=0, paid_until=0 WHERE is_paid=1 AND paid_until>0 AND paid_until<?",
                       (time.time(),))
            db.commit()
            rows = db.execute("SELECT uid,email,display_name,credits,bonus_tokens,is_banned,ban_reason,ban_until,is_admin,is_paid,paid_until,created_at,last_seen FROM users").fetchall()
            db.close()
            now = time.time()
            users = []
            for r in rows:
                u = dict(r)
                u["online"] = (now - u.get("last_seen", 0)) < ONLINE_WINDOW
                u["ban_permanent"] = bool(u.get("is_banned")) and not bool(u.get("ban_until", 0))
                users.append(u)
            self._json(200, {"ok": True, "users": users}); return

        # ── 2.0: Workspaces (alle, in denen ich Mitglied oder Owner bin) ──
        if path == "/workspaces":
            user, err = self._require_user(allow_banned=True)
            if err:
                self._json(err[0], err[1]); return
            uid = user["uid"]
            db = get_db()
            rows = db.execute(
                "SELECT w.id,w.name,w.owner_uid,w.created_at,COALESCE(m.role,'owner') AS role "
                "FROM workspaces w LEFT JOIN workspace_members m ON m.workspace_id=w.id AND m.uid=? "
                "WHERE m.uid IS NOT NULL OR w.owner_uid=? ORDER BY w.created_at DESC",
                (uid, uid)).fetchall()
            out = []
            for r in rows:
                d2 = dict(r)
                d2["member_count"] = db.execute(
                    "SELECT COUNT(*) AS n FROM workspace_members WHERE workspace_id=?",
                    (r["id"],)).fetchone()["n"]
                out.append(d2)
            db.close()
            self._json(200, {"ok": True, "workspaces": out}); return

        # ── 2.0: Workspace-Nachrichten ──
        if path == "/workspaces/messages":
            user, err = self._require_user(allow_banned=True)
            if err:
                self._json(err[0], err[1]); return
            wsid = (urllib.parse.parse_qs(urlparse(self.path).query).get("id", [""])[0] or "").strip()
            db = get_db()
            ws = db.execute("SELECT id,name,owner_uid FROM workspaces WHERE id=?", (wsid,)).fetchone()
            if not ws:
                db.close()
                self._json(404, {"ok": False, "error": "Workspace nicht gefunden"}); return
            if ws["owner_uid"] != user["uid"] and not db.execute(
                    "SELECT 1 FROM workspace_members WHERE workspace_id=? AND uid=?",
                    (wsid, user["uid"])).fetchone():
                db.close()
                self._json(403, {"ok": False, "error": "Kein Mitglied dieses Workspaces"}); return
            rows = db.execute(
                "SELECT m.id,m.uid,m.content,m.image,m.created_at,u.display_name,u.avatar,u.verified "
                "FROM workspace_messages m LEFT JOIN users u ON u.uid=m.uid "
                "WHERE m.workspace_id=? ORDER BY m.id ASC LIMIT 200", (wsid,)).fetchall()
            db.close()
            msgs = [{"id": r["id"], "uid": r["uid"], "display_name": r["display_name"] or "",
                     "avatar": r["avatar"] or "", "verified": bool(r["verified"]),
                     "content": r["content"] or "", "image": r["image"] or "",
                     "created_at": r["created_at"]} for r in rows]
            self._json(200, {"ok": True, "messages": msgs, "workspace": dict(ws)}); return

        # ── 2.0: Admin – Meldungen ──
        if path == "/admin/reports":
            if not self._check_admin():
                self._json(403, {"ok": False, "error": "Kein Admin"}); return
            db = get_db()
            rows = db.execute(
                "SELECT r.*, pu.display_name AS reporter_name, ru.display_name AS reported_name "
                "FROM reports r LEFT JOIN users pu ON pu.uid=r.reporter_uid "
                "LEFT JOIN users ru ON ru.uid=r.reported_uid ORDER BY r.created_at DESC").fetchall()
            db.close()
            self._json(200, {"ok": True, "reports": [dict(r) for r in rows]}); return

        # ── 2.0: 500-Seite (Server-Unavailable) ──
        if path == "/500":
            if not self._send_500_page(200):
                self._json(404, {"ok": False, "error": "Nicht gefunden"})
            return

        if path == "/admin/settings":
            if not self._check_admin():
                self._json(403, {"ok": False, "error": "Kein Admin"}); return
            db = get_db()
            res = {"ok": True}
            for prov in ("gemini", "groq", "cline"):
                res[prov + "_key_set"] = bool(resolve_key(db, None, prov))
            db.close()
            self._json(200, res); return

        self._serve_static(path)

    def do_POST(self):
        note_request()
        path = urlparse(self.path).path
        body = self._read_body()
        ip = self._client_ip()   # echte Client-IP (hinter Caddy/nginx via X-Forwarded-For)

        # Register (Start-Tokens) -> Session-Token
        if path == "/register":
            if maintenance_on():
                self._json(403, {"ok": False, "maintenance": True, "error": "Login gesperrt"}); return
            if not rate_ok("reg:" + ip, REGISTER_RATE_LIMIT):
                self._json(429, {"ok": False, "error": "Zu viele Registrierungen. Bitte kurz warten."}); return
            email = body.get("email", "").strip().lower()
            pw = body.get("password", "")
            name = (body.get("display_name", "").strip() or email.split("@")[0])[:40]
            if not email or len(pw) < 3 or "@" not in email:
                self._json(400, {"ok": False, "error": "Gueltige Email und Passwort (min. 3 Zeichen) benoetigt"}); return
            db = get_db()
            n_ip = db.execute("SELECT COUNT(*) AS n FROM users WHERE reg_ip=?", (ip,)).fetchone()["n"]
            if n_ip >= MAX_ACCOUNTS_PER_IP:
                db.close()
                self._json(429, {"ok": False,
                    "error": "Maximale Anzahl von Accounts (" + str(MAX_ACCOUNTS_PER_IP)
                             + ") pro IP erreicht."}); return
            if db.execute("SELECT uid FROM users WHERE email=?", (email,)).fetchone():
                db.close()
                self._json(409, {"ok": False, "error": "Email bereits registriert"}); return
            # Eindeutiger Username: gewaehlter Name darf nicht doppelt sein,
            # generierter Fallback wird automatisch eindeutig gemacht.
            if (body.get("display_name", "") or "").strip():
                if display_name_taken(db, name):
                    db.close()
                    self._json(409, {"ok": False, "error": "Benutzername bereits vergeben"}); return
            else:
                name = unique_display_name(db, name)
            uid = str(uuid.uuid4())
            token = new_token()
            db.execute("INSERT INTO users (uid,email,password_hash,display_name,credits,bonus_tokens,created_at,reg_ip) VALUES (?,?,?,?,?,?,?,?)",
                       (uid, email, hash_pw(pw), name, START_CREDITS, START_TOKENS, time.time(), ip))
            db.execute("INSERT INTO sessions (token,uid,created_at) VALUES (?,?,?)",
                       (token, uid, time.time()))
            db.commit(); db.close()
            self._json(200, {"ok": True, "token": token, "uid": uid, "display_name": name,
                             "credits": START_CREDITS, "is_admin": 0}); return

        # ── 2.0: Benachrichtigung senden (User↔User, global nur Admin) ──
        if path == "/notifications/send":
            user, err = self._require_user()
            if err:
                self._json(err[0], err[1]); return
            to_uid = (body.get("to_uid", "") or "").strip()
            title = (body.get("title", "") or "").strip()[:200]
            text = (body.get("body", "") or "").strip()[:2000]
            image = body.get("image", "") or ""
            if not self._valid_image_payload(image):
                return
            if not to_uid:
                self._json(400, {"ok": False, "error": "Empfaenger fehlt."}); return
            is_boss = bool(user.get("is_admin"))
            if to_uid == "*" and not is_boss:
                self._json(403, {"ok": False, "error": "Nur Admins duerfen an alle senden."}); return
            db = get_db()
            if to_uid not in ("*", ""):
                resolved, _ = resolve_user_ref(db, to_uid)
                if not resolved:
                    db.close()
                    self._json(404, {"ok": False, "error": "Empfaenger nicht gefunden"}); return
                to_uid = resolved
            kind = "admin" if (is_boss and to_uid == "*") else "user"
            db.execute("INSERT INTO notifications (from_uid,to_uid,title,body,image,kind,read,created_at) "
                       "VALUES (?,?,?,?,?,?,0,?)",
                       (user["uid"], to_uid, title, text, image, kind, time.time()))
            db.commit(); db.close()
            self._json(200, {"ok": True}); return

        # ── 2.0: Benachrichtigungen gelesen setzen (eine oder alle) ──
        if path == "/notifications/read":
            user, err = self._require_user(allow_banned=True)
            if err:
                self._json(err[0], err[1]); return
            uid = user["uid"]
            nid = body.get("id")
            db = get_db()
            if nid:
                db.execute("UPDATE notifications SET read=1 WHERE id=? AND (to_uid IN (?, '', '*') OR from_uid=?)",
                           (nid, uid, uid))
            else:
                db.execute("UPDATE notifications SET read=1 WHERE to_uid IN (?, '', '*')", (uid,))
            db.commit()
            unread = db.execute("SELECT COUNT(*) AS n FROM notifications WHERE read=0 AND to_uid IN (?, '', '*')",
                                (uid,)).fetchone()["n"]
            db.close()
            self._json(200, {"ok": True, "unread": int(unread)}); return

        # Login -> Session-Token (auch bei Sperre: Sperr-Modal erscheint im App-Inneren)
        if path == "/login":
            if not rate_ok("login:" + ip, LOGIN_RATE_LIMIT):
                self._json(429, {"ok": False, "error": "Zu viele Versuche. Bitte kurz warten."}); return
            email = body.get("email", "").strip().lower()
            pw = body.get("password", "")
            db = get_db()
            row = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
            # Wartungsmodus ("Server abgeschaltet"): nur Admins duerfen rein,
            # alle anderen sehen im Login-Screen "Login gesperrt"
            if maintenance_on(db) and not (row and (row["is_admin"] or row["email"] == SUPER_ADMIN_EMAIL)):
                db.close()
                self._json(403, {"ok": False, "maintenance": True, "error": "Login gesperrt"}); return
            if not row or not verify_pw(pw, row["password_hash"]):
                db.close()
                # Brute-Force-Bremse pro Konto: zaehlt NUR echte Fehlversuche
                if not rate_ok("pwfail:" + email, PWFAIL_LIMIT, window=PWFAIL_WINDOW):
                    self._json(429, {"ok": False, "error": "Zu viele Fehlversuche fuer dieses Konto. Bitte 10 Minuten warten."}); return
                self._json(401, {"ok": False, "error": "Falsche Anmeldedaten"}); return
            user = dict(row)
            # 2FA: wenn aktiv, muss der Authenticator-Code stimmen
            if user.get("totp_on"):
                code = (body.get("code", "") or "").strip()
                if not totp_ok(user.get("totp_secret") or "", code):
                    db.close()
                    self._json(401, {"ok": False, "need_2fa": True,
                        "error": "2FA aktiv – bitte den 6-stelligen Authenticator-Code eingeben."}); return
            if not user["password_hash"].startswith("pbkdf2$"):
                db.execute("UPDATE users SET password_hash=? WHERE uid=?", (hash_pw(pw), user["uid"]))
            if user["is_banned"] and user.get("ban_until", 0) and time.time() > user["ban_until"]:
                db.execute("UPDATE users SET is_banned=0, ban_reason='', ban_until=0 WHERE uid=?",
                           (user["uid"],))
                user["is_banned"] = 0
                user["ban_reason"] = ""
                user["ban_until"] = 0
            token = new_token()
            now = time.time()
            db.execute("INSERT INTO sessions (token,uid,created_at) VALUES (?,?,?)",
                       (token, user["uid"], now))
            db.execute("UPDATE users SET last_login=?, last_seen=? WHERE uid=?", (now, now, user["uid"]))
            db.commit(); db.close()
            d = {"ok": True, "token": token, "uid": user["uid"], "email": user["email"],
                 "display_name": user["display_name"], "credits": user["credits"],
                 "is_admin": user["is_admin"]}
            if user["is_banned"]:
                d.update(self._ban_info(user))
            self._json(200, d); return

        # Logout: Session invalidieren
        if path == "/logout":
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                db = get_db()
                db.execute("DELETE FROM sessions WHERE token=?", (auth[7:].strip(),))
                db.commit(); db.close()
            self._json(200, {"ok": True}); return

        # Eigener API-Key: Feature entfernt (Keys verwaltet ausschliesslich der Admin)
        if path == "/settings/keys":
            self._json(410, {"ok": False, "error": "Eigene API-Keys sind entfernt. Keys verwaltet der Admin."}); return

        # ── Profil & Einstellungen ─────────────────────────
        # PDF-Export: KI-Text als PDF herunterladen
        if path == "/export/pdf":
            user, err = self._require_user()
            if err:
                self._json(err[0], err[1]); return
            title = (body.get("title", "") or "Sychos Export")[:120]
            content = body.get("content", "") or ""
            if not content.strip():
                self._json(400, {"ok": False, "error": "Kein Inhalt zum Exportieren."}); return
            data = make_pdf(title, content)
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Disposition", 'attachment; filename="sychos-export.pdf"')
            self._cors()
            self.end_headers()
            self.wfile.write(data)
            return

        # 2FA: Authenticator einrichten (Secret + otpauth-Link fuer die QR-Anzeige)
        if path == "/settings/2fa/setup":
            user, err = self._require_user(allow_banned=True)
            if err:
                self._json(err[0], err[1]); return
            if user.get("totp_on"):
                self._json(400, {"ok": False, "error": "2FA ist bereits aktiv."}); return
            secret = base64.b32encode(os.urandom(20)).decode().rstrip("=")
            db = get_db()
            db.execute("UPDATE users SET totp_secret=? WHERE uid=?", (secret, user["uid"]))
            db.commit(); db.close()
            uri = ("otpauth://totp/Sychos:" + urllib.parse.quote(user["email"]) +
                   "?secret=" + secret + "&issuer=Sychos&digits=6&period=30")
            self._json(200, {"ok": True, "secret": secret, "otpauth": uri}); return

        # 2FA: mit bestaetigtem Code aktivieren
        if path == "/settings/2fa/enable":
            user, err = self._require_user(allow_banned=True)
            if err:
                self._json(err[0], err[1]); return
            code = (body.get("code", "") or "").strip()
            db = get_db()
            row = db.execute("SELECT totp_secret FROM users WHERE uid=?", (user["uid"],)).fetchone()
            if not row or not totp_ok(row["totp_secret"], code):
                db.close()
                self._json(400, {"ok": False, "error": "Code falsch – bitte erneut versuchen."}); return
            db.execute("UPDATE users SET totp_on=1 WHERE uid=?", (user["uid"],))
            db.commit(); db.close()
            self._json(200, {"ok": True}); return

        # 2FA: deaktivieren
        if path == "/settings/2fa/disable":
            user, err = self._require_user(allow_banned=True)
            if err:
                self._json(err[0], err[1]); return
            db = get_db()
            db.execute("UPDATE users SET totp_on=0, totp_secret='' WHERE uid=?", (user["uid"],))
            db.commit(); db.close()
            self._json(200, {"ok": True}); return

        # Anzeigename aendern
        if path == "/settings/profile":
            user, err = self._require_user(allow_banned=True)
            if err:
                self._json(err[0], err[1]); return
            name = (body.get("display_name", "") or "").strip()[:40]
            if not name:
                self._json(400, {"ok": False, "error": "Name darf nicht leer sein."}); return
            db = get_db()
            if display_name_taken(db, name, exclude_uid=user["uid"]):
                db.close()
                self._json(409, {"ok": False, "error": "Benutzername bereits vergeben"}); return
            db.execute("UPDATE users SET display_name=? WHERE uid=?", (name, user["uid"]))
            db.commit(); db.close()
            self._json(200, {"ok": True, "display_name": name}); return

        # E-Mail aendern (Passwort bestaetigen)
        if path == "/settings/email":
            user, err = self._require_user(allow_banned=True)
            if err:
                self._json(err[0], err[1]); return
            email = (body.get("email", "") or "").strip().lower()
            pw = body.get("password", "")
            if not email or "@" not in email:
                self._json(400, {"ok": False, "error": "Ungueltige E-Mail."}); return
            db = get_db()
            row = db.execute("SELECT password_hash FROM users WHERE uid=?", (user["uid"],)).fetchone()
            if not row or not verify_pw(pw, row["password_hash"]):
                db.close()
                self._json(401, {"ok": False, "error": "Passwort falsch."}); return
            if db.execute("SELECT 1 FROM users WHERE email=? AND uid<>?", (email, user["uid"])).fetchone():
                db.close()
                self._json(409, {"ok": False, "error": "E-Mail bereits vergeben."}); return
            db.execute("UPDATE users SET email=? WHERE uid=?", (email, user["uid"]))
            db.commit(); db.close()
            self._json(200, {"ok": True, "email": email}); return

        # Passwort aendern (aktuelles Passwort noetig)
        if path == "/settings/password":
            user, err = self._require_user(allow_banned=True)
            if err:
                self._json(err[0], err[1]); return
            cur_pw = body.get("current_password", "")
            new_pw = body.get("new_password", "")
            if len(new_pw) < 4:
                self._json(400, {"ok": False, "error": "Neues Passwort: mind. 4 Zeichen."}); return
            db = get_db()
            row = db.execute("SELECT password_hash FROM users WHERE uid=?", (user["uid"],)).fetchone()
            if not row or not verify_pw(cur_pw, row["password_hash"]):
                db.close()
                self._json(401, {"ok": False, "error": "Aktuelles Passwort falsch."}); return
            db.execute("UPDATE users SET password_hash=? WHERE uid=?", (hash_pw(new_pw), user["uid"]))
            db.commit(); db.close()
            self._json(200, {"ok": True}); return

        # ── 2.0: Freundschaft annehmen/hinzufuegen (beide Richtungen accepted) ──
        if path == "/friends/add":
            user, err = self._require_user()
            if err:
                self._json(err[0], err[1]); return
            me, fid = user["uid"], (body.get("uid", "") or "").strip()
            if not fid or fid == me:
                self._json(400, {"ok": False, "error": "Ungueltiger User."}); return
            db = get_db()
            resolved, _ = resolve_user_ref(db, fid)
            if not resolved:
                db.close()
                self._json(404, {"ok": False, "error": "User nicht gefunden"}); return
            fid = resolved
            # NUR annehmen – eine Freundschaft entsteht erst nach gesendeter UND angenommener Anfrage.
            pending = db.execute("SELECT 1 FROM friends WHERE uid=? AND friend_uid=? AND status='pending'",
                                 (fid, me)).fetchone()
            already = db.execute("SELECT 1 FROM friends WHERE uid=? AND friend_uid=? AND status='accepted'",
                                 (me, fid)).fetchone()
            if not pending and not already:
                db.close()
                self._json(400, {"ok": False, "error": "Keine offene Freundschaftsanfrage. Zuerst Anfrage senden."}); return
            now = time.time()
            db.execute("INSERT OR REPLACE INTO friends (uid,friend_uid,status,created_at) VALUES (?,?,?,?)",
                       (me, fid, "accepted", now))
            db.execute("INSERT OR REPLACE INTO friends (uid,friend_uid,status,created_at) VALUES (?,?,?,?)",
                       (fid, me, "accepted", now))
            db.commit(); db.close()
            self._json(200, {"ok": True}); return

        # ── 2.0: Freundschaftsanfrage (pending) ──
        if path == "/friends/request":
            user, err = self._require_user()
            if err:
                self._json(err[0], err[1]); return
            me, fid = user["uid"], (body.get("uid", "") or "").strip()
            if not fid or fid == me:
                self._json(400, {"ok": False, "error": "Ungueltiger User."}); return
            db = get_db()
            resolved, _ = resolve_user_ref(db, fid)
            if not resolved:
                db.close()
                self._json(404, {"ok": False, "error": "User nicht gefunden"}); return
            fid = resolved
            rel = db.execute("SELECT status FROM friends WHERE uid=? AND friend_uid=? AND status='accepted'",
                             (fid, me)).fetchone()
            if rel:
                db.close()
                self._json(200, {"ok": True, "status": "accepted"}); return
            # Anfrage = die Richtung me->fid als pending (damit incoming/outgoing unterscheidbar bleiben)
            db.execute("INSERT OR REPLACE INTO friends (uid,friend_uid,status,created_at) VALUES (?,?,?,?)",
                       (me, fid, "pending", time.time()))
            db.commit(); db.close()
            self._json(200, {"ok": True, "status": "pending"}); return

        # ── 2.0: Freundschaft entfernen (beide Richtungen) ──
        if path == "/friends/remove":
            user, err = self._require_user()
            if err:
                self._json(err[0], err[1]); return
            me, fid = user["uid"], (body.get("uid", "") or "").strip()
            if not fid or fid == me:
                self._json(400, {"ok": False, "error": "Ungueltiger User."}); return
            db = get_db()
            db.execute("DELETE FROM friends WHERE (uid=? AND friend_uid=?) OR (uid=? AND friend_uid=?)",
                       (me, fid, fid, me))
            db.commit(); db.close()
            self._json(200, {"ok": True}); return

        # Account endgueltig loeschen (Passwort noetig; Admin-Account ist geschuetzt)
        if path == "/settings/delete":
            user, err = self._require_user(allow_banned=True)
            if err:
                self._json(err[0], err[1]); return
            pw = body.get("password", "")
            db = get_db()
            row = db.execute("SELECT password_hash, is_admin FROM users WHERE uid=?", (user["uid"],)).fetchone()
            if not row or not verify_pw(pw, row["password_hash"]):
                db.close()
                self._json(401, {"ok": False, "error": "Passwort falsch."}); return
            if row["is_admin"]:
                db.close()
                self._json(403, {"ok": False, "error": "Der Admin-Account kann nicht geloescht werden."}); return
            for r in db.execute("SELECT id FROM chats WHERE uid=?", (user["uid"],)).fetchall():
                db.execute("DELETE FROM messages WHERE chat_id=?", (r["id"],))
            db.execute("DELETE FROM chats WHERE uid=?", (user["uid"],))
            db.execute("DELETE FROM sessions WHERE uid=?", (user["uid"],))
            db.execute("DELETE FROM user_keys WHERE uid=?", (user["uid"],))
            db.execute("DELETE FROM users WHERE uid=?", (user["uid"],))
            db.commit(); db.close()
            self._json(200, {"ok": True}); return

        # ── 2.0: Workspace anlegen ──
        # ── 2.1: Channel/Community anlegen ──
        if path == "/channels/create":
            user, err = self._require_user()
            if err:
                self._json(err[0], err[1]); return
            name = (body.get("name", "") or "").strip()[:60]
            kind = (body.get("kind", "") or "community").strip()
            desc = (body.get("description", "") or "").strip()[:300]
            if kind not in ("community", "group"):
                kind = "community"
            if not name:
                self._json(400, {"ok": False, "error": "Name fehlt."}); return
            cid = str(uuid.uuid4())
            now = time.time()
            db = get_db()
            db.execute("INSERT INTO channels (id,name,kind,description,owner_uid,verified,created_at) "
                       "VALUES (?,?,?,?,?,0,?)", (cid, name, kind, desc, user["uid"], now))
            db.execute("INSERT OR REPLACE INTO channel_members (channel_id,uid,role,banned,created_at) "
                       "VALUES (?,?,?,0,?)", (cid, user["uid"], "owner", now))
            db.commit(); db.close()
            self._json(200, {"ok": True, "channel": {"id": cid, "name": name, "kind": kind,
                             "description": desc, "verified": False, "member_count": 1,
                             "role": "owner", "joined": True, "owner_uid": user["uid"]}}); return

        # ── 2.1: Community beitreten ──
        if path == "/channels/join":
            user, err = self._require_user()
            if err:
                self._json(err[0], err[1]); return
            cid = (body.get("channel_id", "") or "").strip()
            db = get_db()
            ch = db.execute("SELECT * FROM channels WHERE id=?", (cid,)).fetchone()
            if not ch:
                db.close(); self._json(404, {"ok": False, "error": "Kanal nicht gefunden"}); return
            b = db.execute("SELECT banned FROM channel_members WHERE channel_id=? AND uid=?", (cid, user["uid"])).fetchone()
            if b and b["banned"]:
                db.close(); self._json(403, {"ok": False, "error": "Du bist von dieser Community gebannt."}); return
            db.execute("INSERT OR REPLACE INTO channel_members (channel_id,uid,role,banned,created_at) "
                       "VALUES (?,?,?,0,?)", (cid, user["uid"], "member", time.time()))
            db.commit(); db.close()
            self._json(200, {"ok": True}); return

        # ── 2.1: Channel verlassen ──
        if path == "/channels/leave":
            user, err = self._require_user()
            if err:
                self._json(err[0], err[1]); return
            cid = (body.get("channel_id", "") or "").strip()
            db = get_db()
            ch = db.execute("SELECT owner_uid FROM channels WHERE id=?", (cid,)).fetchone()
            if ch and ch["owner_uid"] != user["uid"]:
                db.execute("DELETE FROM channel_members WHERE channel_id=? AND uid=?", (cid, user["uid"]))
                db.commit()
            db.close()
            self._json(200, {"ok": True}); return

        # ── 2.1: Channel-Rolle setzen (owner/admin) ──
        if path == "/channels/role":
            user, err = self._require_user()
            if err:
                self._json(err[0], err[1]); return
            cid = (body.get("channel_id", "") or "").strip()
            role = (body.get("role", "") or "").strip()
            if role not in ("admin", "member"):
                self._json(400, {"ok": False, "error": "Rolle ungueltig."}); return
            db = get_db()
            tgt, _ = resolve_user_ref(db, (body.get("uid", "") or "").strip())
            if not tgt:
                db.close(); self._json(404, {"ok": False, "error": "User nicht gefunden"}); return
            ch = db.execute("SELECT owner_uid FROM channels WHERE id=?", (cid,)).fetchone()
            if not ch:
                db.close(); self._json(404, {"ok": False, "error": "Kanal nicht gefunden"}); return
            me_role = "owner" if ch["owner_uid"] == user["uid"] else (db.execute(
                "SELECT role FROM channel_members WHERE channel_id=? AND uid=?", (cid, user["uid"])).fetchone() or {"role": ""})["role"]
            if me_role not in ("owner", "admin") or ch["owner_uid"] == tgt:
                db.close(); self._json(403, {"ok": False, "error": "Nicht erlaubt."}); return
            db.execute("UPDATE channel_members SET role=? WHERE channel_id=? AND uid=?", (role, cid, tgt))
            db.commit(); db.close()
            self._json(200, {"ok": True}); return

        # ── 2.1: Member kicken (owner/admin) ──
        if path == "/channels/kick":
            user, err = self._require_user()
            if err:
                self._json(err[0], err[1]); return
            cid = (body.get("channel_id", "") or "").strip()
            db = get_db()
            tgt, _ = resolve_user_ref(db, (body.get("uid", "") or "").strip())
            if not tgt:
                db.close(); self._json(404, {"ok": False, "error": "User nicht gefunden"}); return
            ch = db.execute("SELECT owner_uid FROM channels WHERE id=?", (cid,)).fetchone()
            if not ch:
                db.close(); self._json(404, {"ok": False, "error": "Kanal nicht gefunden"}); return
            me_role = "owner" if ch["owner_uid"] == user["uid"] else (db.execute(
                "SELECT role FROM channel_members WHERE channel_id=? AND uid=?", (cid, user["uid"])).fetchone() or {"role": ""})["role"]
            if me_role not in ("owner", "admin") or ch["owner_uid"] == tgt:
                db.close(); self._json(403, {"ok": False, "error": "Nicht erlaubt."}); return
            db.execute("DELETE FROM channel_members WHERE channel_id=? AND uid=?", (cid, tgt))
            db.commit(); db.close()
            self._json(200, {"ok": True}); return

        # ── 2.1: Member bannen (owner/admin) – gebannt kommt nicht mehr rein ──
        if path == "/channels/ban":
            user, err = self._require_user()
            if err:
                self._json(err[0], err[1]); return
            cid = (body.get("channel_id", "") or "").strip()
            db = get_db()
            tgt, _ = resolve_user_ref(db, (body.get("uid", "") or "").strip())
            if not tgt:
                db.close(); self._json(404, {"ok": False, "error": "User nicht gefunden"}); return
            ch = db.execute("SELECT owner_uid FROM channels WHERE id=?", (cid,)).fetchone()
            if not ch:
                db.close(); self._json(404, {"ok": False, "error": "Kanal nicht gefunden"}); return
            me_role = "owner" if ch["owner_uid"] == user["uid"] else (db.execute(
                "SELECT role FROM channel_members WHERE channel_id=? AND uid=?", (cid, user["uid"])).fetchone() or {"role": ""})["role"]
            if me_role not in ("owner", "admin") or ch["owner_uid"] == tgt:
                db.close(); self._json(403, {"ok": False, "error": "Nicht erlaubt."}); return
            db.execute("INSERT OR REPLACE INTO channel_members (channel_id,uid,role,banned,created_at) "
                       "VALUES (?,?,?,1,?)", (cid, tgt, "member", time.time()))
            db.commit(); db.close()
            self._json(200, {"ok": True}); return

        # ── 2.1: Bann aufheben (owner/admin) ──
        if path == "/channels/unban":
            user, err = self._require_user()
            if err:
                self._json(err[0], err[1]); return
            cid = (body.get("channel_id", "") or "").strip()
            db = get_db()
            tgt, _ = resolve_user_ref(db, (body.get("uid", "") or "").strip())
            if not tgt:
                db.close(); self._json(404, {"ok": False, "error": "User nicht gefunden"}); return
            ch = db.execute("SELECT owner_uid FROM channels WHERE id=?", (cid,)).fetchone()
            me_role = "owner" if (ch and ch["owner_uid"] == user["uid"]) else (db.execute(
                "SELECT role FROM channel_members WHERE channel_id=? AND uid=?", (cid, user["uid"])).fetchone() or {"role": ""})["role"]
            if not ch or me_role not in ("owner", "admin"):
                db.close(); self._json(403, {"ok": False, "error": "Nicht erlaubt."}); return
            db.execute("DELETE FROM channel_members WHERE channel_id=? AND uid=? AND banned=1", (cid, tgt))
            db.commit(); db.close()
            self._json(200, {"ok": True}); return

        # ── 2.1: Channel-Nachricht posten (Member, nicht gebannt) ──
        if path == "/channels/message":
            user, err = self._require_user()
            if err:
                self._json(err[0], err[1]); return
            cid = (body.get("channel_id", "") or "").strip()
            content = (body.get("content", "") or "").strip()[:MAX_MSG_LEN]
            image = body.get("image", "") or ""
            if not self._valid_image_payload(image):
                return
            if not content and not image:
                self._json(400, {"ok": False, "error": "Leere Nachricht."}); return
            db = get_db()
            ch = db.execute("SELECT owner_uid FROM channels WHERE id=?", (cid,)).fetchone()
            if not ch:
                db.close(); self._json(404, {"ok": False, "error": "Kanal nicht gefunden"}); return
            mem = db.execute("SELECT role,banned FROM channel_members WHERE channel_id=? AND uid=?", (cid, user["uid"])).fetchone()
            if ch["owner_uid"] != user["uid"] and (not mem or mem["banned"]):
                db.close(); self._json(403, {"ok": False, "error": "Kein Mitglied oder gebannt."}); return
            now = time.time()
            cur = db.execute("INSERT INTO channel_messages (channel_id,uid,content,image,created_at) "
                             "VALUES (?,?,?,?,?)", (cid, user["uid"], content, image, now))
            db.commit(); db.close()
            self._json(200, {"ok": True, "message": {"id": cur.lastrowid, "uid": user["uid"],
                             "content": content, "image": image, "created_at": now}}); return

        # ── 2.1: Community verifizieren (nur Admin) ──
        if path == "/admin/verify-channel":
            if not self._check_admin():
                self._json(403, {"ok": False, "error": "Kein Admin"}); return
            cid = (body.get("channel_id", "") or "").strip()
            verified = 1 if body.get("verified") else 0
            db = get_db()
            cur = db.execute("UPDATE channels SET verified=? WHERE id=?", (verified, cid))
            db.commit(); db.close()
            if cur.rowcount == 0:
                self._json(404, {"ok": False, "error": "Kanal nicht gefunden"}); return
            self._json(200, {"ok": True, "verified": bool(verified)}); return

        # ── 2.1: Direkt-Nachricht senden ──
        if path == "/dm/send":
            user, err = self._require_user()
            if err:
                self._json(err[0], err[1]); return
            me = user["uid"]
            content = (body.get("content", "") or "").strip()[:MAX_MSG_LEN]
            image = body.get("image", "") or ""
            if not self._valid_image_payload(image):
                return
            if not content and not image:
                self._json(400, {"ok": False, "error": "Leere Nachricht."}); return
            db = get_db()
            peer, _ = resolve_user_ref(db, (body.get("uid", "") or "").strip())
            if not peer or peer == me:
                db.close(); self._json(404, {"ok": False, "error": "User nicht gefunden"}); return
            # Nur an Freunde (accepted) oder bestehenden Thread senden
            fr = db.execute("SELECT 1 FROM friends WHERE uid=? AND friend_uid=? AND status='accepted'", (me, peer)).fetchone()
            th = db.execute("SELECT 1 FROM dm_messages WHERE (sender_uid=? AND recipient_uid=?) OR (sender_uid=? AND recipient_uid=?) LIMIT 1",
                            (me, peer, peer, me)).fetchone()
            if not fr and not th:
                db.close(); self._json(403, {"ok": False, "error": "Nur an Freunde oder bestehende Chats senden."}); return
            now = time.time()
            cur = db.execute("INSERT INTO dm_messages (sender_uid,recipient_uid,content,image,read,created_at) "
                             "VALUES (?,?,?,?,0,?)", (me, peer, content, image, now))
            db.commit(); db.close()
            self._json(200, {"ok": True, "message": {"id": cur.lastrowid, "sender_uid": me,
                             "content": content, "image": image, "created_at": now, "mine": True}}); return

        if path == "/workspaces/create":
            user, err = self._require_user()
            if err:
                self._json(err[0], err[1]); return
            name = (body.get("name", "") or "").strip()[:80]
            if not name:
                self._json(400, {"ok": False, "error": "Name fehlt."}); return
            wsid = str(uuid.uuid4())
            now = time.time()
            db = get_db()
            db.execute("INSERT INTO workspaces (id,name,owner_uid,created_at) VALUES (?,?,?,?)",
                       (wsid, name, user["uid"], now))
            db.execute("INSERT OR REPLACE INTO workspace_members (workspace_id,uid,role) VALUES (?,?,?)",
                       (wsid, user["uid"], "owner"))
            db.commit(); db.close()
            self._json(200, {"ok": True, "workspace": {"id": wsid, "name": name,
                "owner_uid": user["uid"], "created_at": now, "member_count": 1, "role": "owner"}}); return

        # ── 2.0: User in Workspace einladen ──
        if path == "/workspaces/invite":
            user, err = self._require_user()
            if err:
                self._json(err[0], err[1]); return
            me = user["uid"]
            wsid = (body.get("workspace_id", "") or "").strip()
            fid = (body.get("uid", "") or "").strip()
            if not wsid or not fid or fid == me:
                self._json(400, {"ok": False, "error": "Ungueltige Angaben."}); return
            db = get_db()
            ws = db.execute("SELECT owner_uid FROM workspaces WHERE id=?", (wsid,)).fetchone()
            if not ws:
                db.close()
                self._json(404, {"ok": False, "error": "Workspace nicht gefunden"}); return
            if ws["owner_uid"] != me and not db.execute(
                    "SELECT 1 FROM workspace_members WHERE workspace_id=? AND uid=?", (wsid, me)).fetchone():
                db.close()
                self._json(403, {"ok": False, "error": "Kein Mitglied dieses Workspaces"}); return
            resolved, _ = resolve_user_ref(db, fid)
            if not resolved:
                db.close()
                self._json(404, {"ok": False, "error": "User nicht gefunden"}); return
            fid = resolved
            db.execute("INSERT OR IGNORE INTO workspace_members (workspace_id,uid,role) VALUES (?,?,?)",
                       (wsid, fid, "member"))
            db.commit(); db.close()
            self._json(200, {"ok": True}); return

        # ── 2.0: Workspace-Nachricht posten ──
        if path == "/workspaces/message":
            user, err = self._require_user()
            if err:
                self._json(err[0], err[1]); return
            me = user["uid"]
            wsid = (body.get("workspace_id", "") or "").strip()
            content = (body.get("content", "") or "").strip()[:MAX_MSG_LEN]
            image = body.get("image", "") or ""
            if not self._valid_image_payload(image):
                return
            if not content and not image:
                self._json(400, {"ok": False, "error": "Leere Nachricht."}); return
            db = get_db()
            ws = db.execute("SELECT owner_uid FROM workspaces WHERE id=?", (wsid,)).fetchone()
            if not ws:
                db.close()
                self._json(404, {"ok": False, "error": "Workspace nicht gefunden"}); return
            if ws["owner_uid"] != me and not db.execute(
                    "SELECT 1 FROM workspace_members WHERE workspace_id=? AND uid=?", (wsid, me)).fetchone():
                db.close()
                self._json(403, {"ok": False, "error": "Kein Mitglied dieses Workspaces"}); return
            now = time.time()
            cur = db.execute("INSERT INTO workspace_messages (workspace_id,uid,content,image,created_at) "
                             "VALUES (?,?,?,?,?)", (wsid, me, content, image, now))
            db.commit(); db.close()
            self._json(200, {"ok": True, "message": {"id": cur.lastrowid, "uid": me,
                "content": content, "image": image, "created_at": now}}); return

        # EIGENER Checkout + echte Abbuchung: PaymentIntent fuer die Kartenabfrage IM eigenen UI
        if path == "/pay/intent":
            user, err = self._require_user()
            if err:
                self._json(err[0], err[1]); return
            pid = body.get("plan", "")
            if pid not in PLANS or pid == "free":
                self._json(400, {"ok": False, "error": "Unbekannter Plan."}); return
            if not (STRIPE_SECRET_KEY and STRIPE_PUBLISHABLE_KEY):
                self._json(503, {"ok": False, "error": "Zahlung nicht konfiguriert (Stripe-Keys fehlen)."}); return
            code = (body.get("code", "") or "").strip().lower()
            price = PLANS[pid]["price"]
            if code:
                if code not in PROMO_CODES:
                    self._json(400, {"ok": False, "error": "Ungueltiger Rabattcode."}); return
                price = round(price * (1 - PROMO_CODES[code]), 2)
            amount = max(50, int(round(price * 100)))   # Stripe-Mindestbetrag 0,50 $
            data = urllib.parse.urlencode({
                "amount": str(amount),
                "currency": "usd",
                "automatic_payment_methods[enabled]": "true",
                "description": "Sychos Plan " + PLANS[pid]["name"] + " – 30 Tage",
                "metadata[uid]": user["uid"],
                "metadata[plan]": pid,
                "metadata[code]": code,
                "receipt_email": user["email"],
            }).encode()
            req = urllib.request.Request("https://api.stripe.com/v1/payment_intents",
                data=data, method="POST",
                headers={"Authorization": "Bearer " + STRIPE_SECRET_KEY,
                         "Content-Type": "application/x-www-form-urlencoded"})
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    pi = json.loads(resp.read().decode())
            except Exception:
                self._json(502, {"ok": False, "error": "Zahlungsdienst nicht erreichbar."}); return
            self._json(200, {"ok": True, "client_secret": pi.get("client_secret", ""),
                             "publishable": STRIPE_PUBLISHABLE_KEY, "amount": amount / 100.0}); return

        # Abbuchung bestaetigen -> Plan aktivieren (prueft das ECHTE PaymentIntent bei Stripe)
        if path == "/pay/confirm":
            user, err = self._require_user()
            if err:
                self._json(err[0], err[1]); return
            pi_id = (body.get("intent_id", "") or "").strip()
            if not pi_id or not STRIPE_SECRET_KEY:
                self._json(400, {"ok": False, "error": "Keine Zahlung gefunden."}); return
            req = urllib.request.Request(
                "https://api.stripe.com/v1/payment_intents/" + urllib.parse.quote(pi_id),
                headers={"Authorization": "Bearer " + STRIPE_SECRET_KEY})
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    pi = json.loads(resp.read().decode())
            except Exception:
                self._json(502, {"ok": False, "error": "Zahlungsdienst nicht erreichbar."}); return
            if pi.get("status") != "succeeded":
                self._json(402, {"ok": False, "error": "Zahlung wurde noch nicht abgebucht."}); return
            meta = pi.get("metadata") or {}
            if meta.get("uid") != user["uid"]:
                self._json(403, {"ok": False, "error": "Diese Zahlung gehoert zu einem anderen Konto."}); return
            pid = meta.get("plan", "")
            if pid not in PLANS:
                self._json(400, {"ok": False, "error": "Unbekannter Plan."}); return
            price_paid = (pi.get("amount_received") or 0) / 100.0
            until = time.time() + PLAN_DAYS * 24 * 3600 if pid != "free" else 0
            db = get_db()
            db.execute("UPDATE users SET plan=?, plan_until=?, is_paid=?, paid_until=? WHERE uid=?",
                       (pid, until, 1 if pid != "free" else 0, until, user["uid"]))
            db.execute("INSERT INTO orders (uid,plan,code,price,created_at) VALUES (?,?,?,?,?)",
                       (user["uid"], pid, meta.get("code", ""), price_paid, time.time()))
            db.commit(); db.close()
            self._json(200, {"ok": True, "plan": pid, "plan_name": PLANS[pid]["name"],
                             "price_paid": price_paid, "plan_until": until}); return

        # ECHTES Zahlen: Stripe Checkout Session anlegen -> User zahlt auf Stripes sicherer Seite
        if path == "/plan/checkout":
            user, err = self._require_user()
            if err:
                self._json(err[0], err[1]); return
            pid = body.get("plan", "")
            if pid not in PLANS or pid == "free":
                self._json(400, {"ok": False, "error": "Unbekannter Plan."}); return
            if not STRIPE_SECRET_KEY:
                self._json(503, {"ok": False, "error": "Zahlung nicht konfiguriert (STRIPE_SECRET_KEY fehlt)."}); return
            code = (body.get("code", "") or "").strip().lower()
            price = PLANS[pid]["price"]
            if code:
                if code not in PROMO_CODES:
                    self._json(400, {"ok": False, "error": "Ungueltiger Rabattcode."}); return
                price = round(price * (1 - PROMO_CODES[code]), 2)
            # Stripe verlangt mindestens 0,50 $ – wird transparent an den Kunden ausgewiesen
            amount = max(50, int(round(price * 100)))
            host = self.headers.get("Host", "") or ("localhost:%d" % PORT)
            base = "http://" + host
            data = urllib.parse.urlencode({
                "mode": "payment",
                "client_reference_id": user["uid"],
                "customer_email": user["email"],
                "line_items[0][price_data][currency]": "usd",
                "line_items[0][price_data][unit_amount]": str(amount),
                "line_items[0][price_data][product_data][name]":
                    "Sychos Plan " + PLANS[pid]["name"] + " – 30 Tage",
                "line_items[0][quantity]": "1",
                "success_url": base + "/?paid={CHECKOUT_SESSION_ID}",
                "cancel_url": base + "/?pay=cancel",
                "metadata[uid]": user["uid"],
                "metadata[plan]": pid,
                "metadata[code]": code,
            }).encode()
            req = urllib.request.Request("https://api.stripe.com/v1/checkout/sessions",
                data=data, method="POST",
                headers={"Authorization": "Bearer " + STRIPE_SECRET_KEY,
                         "Content-Type": "application/x-www-form-urlencoded"})
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    sess = json.loads(resp.read().decode())
            except Exception:
                self._json(502, {"ok": False, "error": "Zahlungsdienst nicht erreichbar."}); return
            self._json(200, {"ok": True, "url": sess.get("url", ""),
                             "session_id": sess.get("id", ""), "amount": amount / 100.0}); return

        # ECHTES Zahlen bestaetigen: Stripe pruefen, dann Plan aktivieren
        if path == "/plan/confirm":
            user, err = self._require_user()
            if err:
                self._json(err[0], err[1]); return
            sid = (body.get("session_id", "") or "").strip()
            if not sid or not STRIPE_SECRET_KEY:
                self._json(400, {"ok": False, "error": "Keine Zahlungs-Sitzung."}); return
            req = urllib.request.Request(
                "https://api.stripe.com/v1/checkout/sessions/" + urllib.parse.quote(sid),
                headers={"Authorization": "Bearer " + STRIPE_SECRET_KEY})
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    sess = json.loads(resp.read().decode())
            except Exception:
                self._json(502, {"ok": False, "error": "Zahlungsdienst nicht erreichbar."}); return
            if sess.get("payment_status") != "paid":
                self._json(402, {"ok": False, "error": "Zahlung wurde noch nicht abgebucht."}); return
            meta = sess.get("metadata") or {}
            if meta.get("uid") != user["uid"]:
                self._json(403, {"ok": False, "error": "Diese Zahlung gehoert zu einem anderen Konto."}); return
            pid = meta.get("plan", "")
            if pid not in PLANS:
                self._json(400, {"ok": False, "error": "Unbekannter Plan."}); return
            price_paid = (sess.get("amount_total") or 0) / 100.0
            until = time.time() + PLAN_DAYS * 24 * 3600 if pid != "free" else 0
            db = get_db()
            db.execute("UPDATE users SET plan=?, plan_until=?, is_paid=?, paid_until=? WHERE uid=?",
                       (pid, until, 1 if pid != "free" else 0, until, user["uid"]))
            db.execute("INSERT INTO orders (uid,plan,code,price,created_at) VALUES (?,?,?,?,?)",
                       (user["uid"], pid, meta.get("code", ""), price_paid, time.time()))
            db.commit(); db.close()
            self._json(200, {"ok": True, "plan": pid, "plan_name": PLANS[pid]["name"],
                             "price_paid": price_paid, "plan_until": until}); return

        # ── Merchant-of-Record Webhooks: Paddle / Lemon Squeezy buchen ab UND
        #    fuehren alle Steuern ab – hier wird der Plan nach Zahlung freigeschaltet.
        if path in ("/webhook/paddle", "/webhook/lemonsqueezy"):
            raw = getattr(self, "_raw_body", b"") or b""
            ok_sig = False
            if path == "/webhook/paddle" and PADDLE_WEBHOOK_SECRET:
                sig = self.headers.get("Paddle-Signature", "")
                parts = dict(p.split("=", 1) for p in sig.split(";") if "=" in p)
                ts = parts.get("ts", "")
                h1 = parts.get("h1") or parts.get("hmac", "")
                if ts and h1:
                    msg = (ts + ":" + raw.decode("utf-8", "replace")).encode()
                    # Secret mit/ohne abschliessenden Schrägstrich akzeptieren
                    for secret in {PADDLE_WEBHOOK_SECRET, PADDLE_WEBHOOK_SECRET.strip().rstrip("/"),
                                   PADDLE_WEBHOOK_SECRET.strip() + "/"}:
                        if secret and hmac.compare_digest(
                                hmac.new(secret.encode(), msg, hashlib.sha1).hexdigest(), h1):
                            ok_sig = True
                            break
            elif path == "/webhook/lemonsqueezy" and LEMONSQUEEZY_WEBHOOK_SECRET:
                sig = (self.headers.get("X-Signature", "") or "").replace("sha256=", "")
                calc = hmac.new(LEMONSQUEEZY_WEBHOOK_SECRET.encode(), raw, hashlib.sha256).hexdigest()
                ok_sig = hmac.compare_digest(calc, sig)
            if not ok_sig:
                self._json(401, {"ok": False, "error": "Ungueltige Signatur."}); return
            try:
                payload = json.loads(raw.decode("utf-8", "replace") or "{}")
            except Exception:
                payload = {}
            custom, amt = {}, None
            if path == "/webhook/paddle":
                data = payload.get("data") or {}
                custom = data.get("custom_data") or payload.get("custom_data") or {}
                amt = (data.get("details", {}).get("totals", {}) or {}).get("total") \
                    or payload.get("amount_gross")
            else:
                attrs = (payload.get("data") or {}).get("attributes") or {}
                custom = (attrs.get("checkout_data", {}) or {}).get("custom") \
                    or (payload.get("meta", {}) or {}).get("custom_data") or {}
                amt = attrs.get("total")
            uid, pid = str(custom.get("uid", "")), custom.get("plan", "")
            code = str(custom.get("code", "") or "")
            if uid and pid in PLANS:
                try:
                    price_paid = round(float(amt or 0) / 100.0, 2)
                except Exception:
                    price_paid = PLANS[pid]["price"]
                until = time.time() + PLAN_DAYS * 24 * 3600 if pid != "free" else 0
                db = get_db()
                if db.execute("SELECT 1 FROM users WHERE uid=?", (uid,)).fetchone():
                    db.execute("UPDATE users SET plan=?, plan_until=?, is_paid=?, paid_until=? WHERE uid=?",
                               (pid, until, 1 if pid != "free" else 0, until, uid))
                    db.execute("INSERT INTO orders (uid,plan,code,price,created_at) VALUES (?,?,?,?,?)",
                               (uid, pid, code, price_paid, time.time()))
                    db.commit()
                db.close()
            self._json(200, {"ok": True}); return

        # ── Persoenlicher API-Key (wie bei Free-Anbietern) mit Plan-Rate-Limit ──
        if path == "/settings/apikey":
            user, err = self._require_user(allow_banned=True)
            if err:
                self._json(err[0], err[1]); return
            action = (body.get("action") or "list").lower()
            db = get_db()
            if action == "revoke":
                db.execute("UPDATE api_keys SET revoked=1 WHERE uid=?", (user["uid"],))
                db.commit(); db.close()
                self._json(200, {"ok": True, "key": ""}); return
            row = db.execute("SELECT key FROM api_keys WHERE uid=? AND revoked=0 ORDER BY id DESC",
                             (user["uid"],)).fetchone()
            if action == "create" and not row:
                key = "sk-sychos-" + uuid.uuid4().hex + uuid.uuid4().hex[:12]
                db.execute("INSERT INTO api_keys (uid,key,created_at) VALUES (?,?,?)",
                           (user["uid"], key, time.time()))
                db.commit()
                row = db.execute("SELECT key FROM api_keys WHERE uid=? AND revoked=0 ORDER BY id DESC",
                                 (user["uid"],)).fetchone()
            db.close()
            self._json(200, {"ok": True, "key": (row["key"] if row else "")}); return

        # ── Oeffentliche KI-API: POST /api/chat mit persoenlichem Key ──
        # { "prompt": "...", "model": "gemini-3.6-flash", "strength": "medium" }
        if path == "/api/chat":
            auth = self.headers.get("Authorization", "")
            key = auth[7:].strip() if auth.startswith("Bearer ") else ""
            if not key.startswith("sk-sychos-"):
                self._json(401, {"ok": False, "error": "Persoenlichen API-Key senden: Authorization: Bearer sk-sychos-..."}); return
            db = get_db()
            krow = db.execute("SELECT uid FROM api_keys WHERE key=? AND revoked=0", (key,)).fetchone()
            urow = db.execute("SELECT * FROM users WHERE uid=?", (krow["uid"],)).fetchone() if krow else None
            akey = resolve_key(db, krow["uid"], MODELS.get(body.get("model", ""), {}).get("provider", "gemini")) if krow else ""
            db.close()
            if not urow:
                self._json(401, {"ok": False, "error": "API-Key ungueltig oder widerrufen."}); return
            user = dict(urow)
            plan = effective_plan(user)
            lim = API_RATE_PER_MIN.get(plan, 5)
            if not rate_ok("apikey:" + key, lim):
                self._json(429, {"ok": False, "error": "API-Rate-Limit erreicht: %d Anfragen/Minute (Plan %s)."
                                 % (lim, PLANS.get(plan, {}).get("name", plan))}); return
            model = body.get("model", "gemini-3.6-flash")
            if model not in MODELS:
                self._json(400, {"ok": False, "error": "Unbekanntes Modell."}); return
            need = MODELS[model].get("plan", "free")
            tdays = MODELS[model].get("trial") or 0
            if not user.get("is_admin") and not plan_ok(plan, need) and not trial_free(user["uid"], model):
                self._json(402, {"ok": False, "error": "Modell benoetigt den %s-Plan (oder hoeher)%s"
                                 % (PLANS.get(need, {}).get("name", need),
                                    (" – die %d-Tage-Testphase ist abgelaufen." % tdays) if tdays else ".")}); return
            trial_touch(user["uid"], model)   # 14-Tage-Testphase startet mit der 1. Nutzung
            prompt = (body.get("prompt") or body.get("message") or "").strip()
            if not prompt:
                self._json(400, {"ok": False, "error": "Prompt fehlt (Feld 'prompt')."}); return
            if len(prompt) > MAX_MSG_LEN:
                self._json(400, {"ok": False, "error": "Prompt zu lang (max. %d Zeichen)." % MAX_MSG_LEN}); return
            msgs = [{"role": "user", "content": prompt}]
            r = call_provider(msgs, akey, body.get("strength", "medium"), model)
            if not r.get("ok"):
                self._json(502, r); return
            used = max(1, (len(prompt) + len(r.get("text", ""))) // 4)
            add_usage(user["uid"], used)
            self._json(200, {"ok": True, "model": model, "plan": plan, "text": r.get("text", ""),
                             "tokens": r.get("tokens", used)}); return

        # Plan kaufen (Test-Checkout ohne echte Abbuchung) -> 30 Tage aktiv
        # Rabattcode: 'Release' = -20 % (in PROMO_CODES)
        if path == "/plan/buy":
            user, err = self._require_user()
            if err:
                self._json(err[0], err[1]); return
            pid = body.get("plan", "")
            if pid not in PLANS:
                self._json(400, {"ok": False, "error": "Unbekannter Plan."}); return
            code = (body.get("code", "") or "").strip().lower()
            price = PLANS[pid]["price"]
            discount = 0.0
            if code:
                if code not in PROMO_CODES:
                    self._json(400, {"ok": False, "error": "Ungueltiger Rabattcode."}); return
                discount = PROMO_CODES[code]
                price = round(price * (1 - discount), 2)
            until = time.time() + PLAN_DAYS * 24 * 3600 if pid != "free" else 0
            db = get_db()
            db.execute("UPDATE users SET plan=?, plan_until=?, is_paid=?, paid_until=? WHERE uid=?",
                       (pid, until, 1 if pid != "free" else 0, until, user["uid"]))
            db.execute("INSERT INTO orders (uid,plan,code,price,created_at) VALUES (?,?,?,?,?)",
                       (user["uid"], pid, code, price, time.time()))
            db.commit(); db.close()
            self._json(200, {"ok": True, "plan": pid, "plan_name": PLANS[pid]["name"],
                             "plan_until": until, "price_paid": price,
                             "discount": discount, "code": code}); return

        # ── Admin-Warnsystem ──────────────────────────────
        # Admin sendet eine Warn-Nachricht an einen User (Popup im Hub)
        if path == "/admin/warn":
            if not self._check_admin():
                self._json(403, {"ok": False, "error": "Kein Admin"}); return
            uid = body.get("uid", "")
            msg = (body.get("message", "") or "").strip()[:500]
            if not msg:
                self._json(400, {"ok": False, "error": "Warnung ohne Text."}); return
            db = get_db()
            if not db.execute("SELECT 1 FROM users WHERE uid=?", (uid,)).fetchone():
                db.close()
                self._json(404, {"ok": False, "error": "User nicht gefunden"}); return
            db.execute("INSERT INTO warnings (uid,message,created_at) VALUES (?,?,?)",
                       (uid, msg, time.time()))
            db.commit(); db.close()
            self._json(200, {"ok": True}); return

        # User holt seine offenen Warnungen
        if path == "/warnings":
            user, err = self._require_user(allow_banned=True)
            if err:
                self._json(err[0], err[1]); return
            db = get_db()
            rows = db.execute("SELECT id,message,created_at FROM warnings WHERE uid=? AND read=0 ORDER BY id",
                              (user["uid"],)).fetchall()
            db.close()
            self._json(200, {"ok": True, "warnings": [dict(r) for r in rows]}); return

        # Warnungen als gelesen markieren (ohne id = alle)
        if path == "/warnings/read":
            user, err = self._require_user(allow_banned=True)
            if err:
                self._json(err[0], err[1]); return
            wid = body.get("id", 0)
            db = get_db()
            if wid:
                db.execute("UPDATE warnings SET read=1 WHERE id=? AND uid=?", (wid, user["uid"]))
            else:
                db.execute("UPDATE warnings SET read=1 WHERE uid=?", (user["uid"],))
            db.commit(); db.close()
            self._json(200, {"ok": True}); return

        # Neuer Chat
        if path == "/chat/new":
            user, err = self._require_user()
            if err:
                self._json(err[0], err[1]); return
            cid = str(uuid.uuid4())
            title = (body.get("title", "Neuer Chat") or "Neuer Chat")[:MAX_TITLE_LEN]
            model = body.get("model", "")
            if model not in MODELS:
                model = "gemini-3.6-flash"
            db = get_db()
            db.execute("INSERT INTO chats (id,uid,title,model,created_at) VALUES (?,?,?,?,?)",
                       (cid, user["uid"], title, model, time.time()))
            db.commit(); db.close()
            self._json(200, {"ok": True, "chat_id": cid, "title": title, "model": model}); return

        # Chat umbenennen
        if path == "/chat/rename":
            user, err = self._require_user()
            if err:
                self._json(err[0], err[1]); return
            cid = body.get("chat_id", "")
            title = (body.get("title", "") or "").strip()[:MAX_TITLE_LEN] or "Neuer Chat"
            db = get_db()
            cur = db.execute("UPDATE chats SET title=? WHERE id=? AND uid=?",
                             (title, cid, user["uid"]))
            db.commit(); db.close()
            if cur.rowcount == 0:
                self._json(404, {"ok": False, "error": "Chat nicht gefunden"}); return
            self._json(200, {"ok": True, "chat_id": cid, "title": title}); return

        # Chat loeschen
        if path == "/chat/delete":
            user, err = self._require_user()
            if err:
                self._json(err[0], err[1]); return
            cid = body.get("chat_id", "")
            db = get_db()
            db.execute("DELETE FROM messages WHERE chat_id=? AND chat_id IN (SELECT id FROM chats WHERE uid=?)",
                       (cid, user["uid"]))
            db.execute("DELETE FROM chats WHERE id=? AND uid=?", (cid, user["uid"]))
            db.commit(); db.close()
            self._json(200, {"ok": True}); return

        # AI-Chat senden: Credits atomar reservieren (kein Doppelabzug bei Parallel-Requests)
        if path == "/chat/send":
            user, err = self._require_user()
            if err:
                self._json(err[0], err[1]); return
            if maintenance_on() and not user.get("is_admin"):
                self._json(503, {"ok": False, "maintenance": True,
                                 "error": "Server derzeit nicht erreichbar"}); return
            if not rate_ok("send:" + user["uid"], SEND_RATE_LIMIT):
                self._json(429, {"ok": False, "error": "Zu viele Nachrichten. Bitte kurz warten."}); return
            cid = body.get("chat_id", "")
            msg = (body.get("message", "") or "").strip()
            model = body.get("model", "")
            strength = body.get("strength", "medium")
            if model not in MODELS:
                self._json(400, {"ok": False, "error": "Unbekanntes Modell"}); return
            plan = effective_plan(user)
            need = MODELS[model].get("plan", "free")
            tdays = MODELS[model].get("trial") or 0
            if not user.get("is_admin") and not plan_ok(plan, need) and not trial_free(user["uid"], model):
                self._json(402, {"ok": False, "error_type": "premium_locked",
                    "error": "Das Modell '%s' benoetigt den %s-Plan (oder hoeher)%s Bitte unter Einstellungen -> Plan freischalten."
                             % (MODELS[model]["name"], PLANS.get(need, {}).get("name", need),
                                (" – die %d-Tage-Testphase ist abgelaufen." % tdays) if tdays else ".")}); return
            trial_touch(user["uid"], model)   # 14-Tage-Testphase startet mit der 1. Nutzung

            # Bilder (Vision): max. 3, nur an Modelle mit vision-Flag
            images = []
            for im in (body.get("images") or [])[:3]:
                try:
                    data = (im.get("data") or "").strip()
                    mime = (im.get("mime") or "image/png").strip().lower()
                except AttributeError:
                    continue
                if not data or len(data) > 4200000 or mime not in ("image/png", "image/jpeg", "image/webp", "image/gif"):
                    self._json(400, {"ok": False, "error": "Bild ungueltig (PNG/JPEG/WEBP/GIF, max. 3 MB)."}); return
                images.append({"type": "image", "mime": mime, "data": data})
            if images and not MODELS[model].get("vision"):
                self._json(400, {"ok": False, "error_type": "no_vision",
                    "error": "Dieses Modell unterstuetzt keine Bilder. Bitte ein Modell mit dem Bild-Symbol \U0001F5BC waehlen (z. B. Gemini 3.6 Flash)."}); return
            if strength not in STRENGTHS:
                strength = "medium"
            if not msg:
                self._json(400, {"ok": False, "error": "Leere Nachricht"}); return
            if len(msg) > MAX_MSG_LEN:
                self._json(400, {"ok": False, "error": "Nachricht zu lang (max. %d Zeichen)" % MAX_MSG_LEN}); return

            db = get_db()
            own = db.execute("SELECT id FROM chats WHERE id=? AND uid=?", (cid, user["uid"])).fetchone()
            if not own:
                db.close()
                self._json(404, {"ok": False, "error": "Chat nicht gefunden"}); return
            rows = db.execute("SELECT role,content,images FROM messages WHERE chat_id=? ORDER BY id",
                              (cid,)).fetchall()
            history = []
            for r in rows:
                content = r["content"]
                try:
                    rimgs = json.loads(r["images"] or "[]")
                except Exception:
                    rimgs = []
                if rimgs:
                    content = [{"type": "text", "text": r["content"]}] + rimgs
                history.append({"role": r["role"], "content": content})
            history.append({"role": "user", "content": ([{"type": "text", "text": msg}] + images) if images else msg})
            # Identitaet: bei Modellwechsel (z.B. GLM -> DeepSeek) antwortet die KI
            # ab jetzt IMMER mit dem AKTUELL gewaehlten Modell – nie mehr mit dem alten.
            ident = ("[System-Hinweis: Du bist 'Sychos'. Dein aktuelles Modell ist "
                     + MODELS[model]["name"] + ". Wenn gefragt, antworte genau so. Behaupte NIEMALS, "
                     "ein anderes Modell oder eine andere KI zu sein (z.B. GLM, DeepSeek, Gemini, ChatGPT), "
                     "auch wenn im Chatverlauf anderes steht.]")
            if isinstance(history[-1]["content"], str):
                history[-1]["content"] += "\n\n" + ident
            else:
                history[-1]["content"][0]["text"] += "\n\n" + ident

            # Token-Limits (5h / Woche / Monat) wie Cline: bei Erreichen bis zum Reset warten
            est = est_tokens(history)
            lim = limit_block(user["uid"], plan, est)
            if lim:
                win, wstate, _ = lim
                db.close()
                self._json(429, {"ok": False, "error_type": "limit_reached",
                    "error": "%s-Limit erreicht (Plan %s). Weiter in %s – oder Plan upgraden."
                             % (TOKEN_WINDOWS[win]["label"], PLANS[plan]["name"],
                                fmt_wait(wstate["resets_at"] - time.time())),
                    "limit_win": win, "resets_at": wstate["resets_at"]}); return

            db.execute("INSERT INTO messages (chat_id,role,content,model,images,created_at) VALUES (?,?,?,?,?,?)",
                       (cid, "user", msg, model, json.dumps(images), time.time()))
            web_used = False
            if body.get("web"):
                hits = web_search(msg)
                if hits:
                    ctx = "\n\n[Aktuelle Web-Recherche als Kontext]\n" + "\n".join(hits)
                    if isinstance(history[-1]["content"], str):
                        history[-1]["content"] += ctx
                    else:
                        history[-1]["content"][0]["text"] += ctx
                web_used = True
            api_key = resolve_key(db, user["uid"], MODELS[model]["provider"])
            db.commit(); db.close()

            # SSE-Stream: Tokens live an das Frontend senden
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self._cors()
            self.end_headers()

            def push(evt, data):
                self.wfile.write(("event: %s\ndata: %s\n\n" % (evt, json.dumps(data, ensure_ascii=False))).encode("utf-8"))
                self.wfile.flush()

            full, tokens, err = [], 0, None
            for ev in stream_provider(history, api_key, strength, model):
                if ev["type"] == "delta":
                    full.append(ev["text"])
                    push("delta", {"t": ev["text"]})
                elif ev["type"] == "think":
                    push("think", {"t": ev["text"]})
                elif ev["type"] == "end":
                    tokens = ev.get("tokens", 0)
                elif ev["type"] == "error":
                    err = ev
            text = "".join(full)
            if err or not text.strip():
                e2 = err or {"error": "Leere Antwort – bitte erneut versuchen.", "error_type": "api"}
                push("error", {"error": e2["error"], "error_type": e2.get("error_type", "api")})
                return
            # Token-Abrechnung (Input + Output) auf alle drei Limit-Fenster
            in_tok = int(sum((len(m.get("content")) if isinstance(m.get("content"), str)
                              else sum(len(p.get("text") or "") + (1800 if p.get("type") == "image" else 0)
                                       for p in (m.get("content") or [])))
                             for m in history) / 4)
            out_tok = tokens if tokens else max(1, len(text) // 4)
            used = max(1, in_tok + out_tok)
            add_usage(user["uid"], used)
            db = get_db()
            db.execute("INSERT INTO messages (chat_id,role,content,model,tokens,cost,created_at) VALUES (?,?,?,?,?,?,?)",
                       (cid, "assistant", text, model, used, 0, time.time()))
            db.commit(); db.close()
            push("done", {"text": text, "web": web_used, "tokens_used": used,
                          "usage": usage_state(user["uid"], plan)})
            return

        # ── Admin ─────────────────────────────────────────
        if path == "/admin/tokens":
            if not self._check_admin():
                self._json(403, {"ok": False, "error": "Kein Admin"}); return
            uid = body.get("uid", "")
            try:
                amount = float(body.get("tokens", 0))
            except (TypeError, ValueError):
                self._json(400, {"ok": False, "error": "Ungueltige Menge"}); return
            if amount == 0 or abs(amount) > 10 ** 9:
                self._json(400, {"ok": False, "error": "Menge ungueltig"}); return
            db = get_db()
            cur = db.execute("UPDATE users SET bonus_tokens = MAX(0, COALESCE(bonus_tokens,0) + ?) WHERE uid=?",
                             (amount, uid))
            row = db.execute("SELECT bonus_tokens FROM users WHERE uid=?", (uid,)).fetchone()
            db.commit(); db.close()
            if cur.rowcount == 0:
                self._json(404, {"ok": False, "error": "User nicht gefunden"}); return
            self._json(200, {"ok": True, "uid": uid, "bonus_tokens": (row["bonus_tokens"] if row else 0)}); return

        # Admin: Account zuruecksetzen -> Free-Plan + Standard-Credits/-Tokens (Usage wird geleert)
        if path == "/admin/reset":
            if not self._check_admin():
                self._json(403, {"ok": False, "error": "Kein Admin"}); return
            uid = body.get("uid", "")
            db = get_db()
            cur = db.execute("UPDATE users SET plan='free', plan_until=0, is_paid=0, paid_until=0, "
                             "credits=?, bonus_tokens=? WHERE uid=?",
                             (START_CREDITS, START_TOKENS, uid))
            db.execute("DELETE FROM usage WHERE uid=?", (uid,))
            db.commit(); db.close()
            if cur.rowcount == 0:
                self._json(404, {"ok": False, "error": "User nicht gefunden"}); return
            self._json(200, {"ok": True, "plan": "free", "credits": START_CREDITS,
                             "bonus_tokens": START_TOKENS}); return

        # ── 2.0: User melden ──
        if path == "/report":
            user, err = self._require_user()
            if err:
                self._json(err[0], err[1]); return
            me = user["uid"]
            fid = (body.get("uid", "") or "").strip()
            reason = (body.get("reason", "") or "").strip()[:500]
            if not fid or fid == me:
                self._json(400, {"ok": False, "error": "Ungueltiger User."}); return
            if not reason:
                self._json(400, {"ok": False, "error": "Grund fehlt."}); return
            db = get_db()
            resolved, _ = resolve_user_ref(db, fid)
            if not resolved:
                db.close()
                self._json(404, {"ok": False, "error": "User nicht gefunden"}); return
            fid = resolved
            db.execute("INSERT INTO reports (reporter_uid,reported_uid,reason,status,created_at) "
                       "VALUES (?,?,?,'open',?)", (me, fid, reason, time.time()))
            db.commit(); db.close()
            self._json(200, {"ok": True}); return

        # ── 2.0: Bonus-Code einloesen ──
        if path == "/bonus/redeem":
            user, err = self._require_user()
            if err:
                self._json(err[0], err[1]); return
            code = (body.get("code", "") or "").strip()
            err_msg = "Code ungültig oder bereits eingelöst."
            db = get_db()
            row = db.execute("SELECT credits,max_uses,used,active FROM bonus_codes WHERE code=?",
                             (code,)).fetchone()
            if not row or not row["active"]:
                db.close()
                self._json(400, {"ok": False, "error": err_msg}); return
            if (row["max_uses"] or 0) > 0 and (row["used"] or 0) >= (row["max_uses"] or 0):
                db.close()
                self._json(400, {"ok": False, "error": err_msg}); return
            if db.execute("SELECT 1 FROM bonus_redemptions WHERE uid=? AND code=?",
                          (user["uid"], code)).fetchone():
                db.close()
                self._json(400, {"ok": False, "error": err_msg}); return
            added = float(row["credits"] or 0)
            db.execute("UPDATE users SET bonus_tokens = COALESCE(bonus_tokens,0) + ? WHERE uid=?",
                       (added, user["uid"]))
            db.execute("INSERT INTO bonus_redemptions (uid,code,created_at) VALUES (?,?,?)",
                       (user["uid"], code, time.time()))
            db.execute("UPDATE bonus_codes SET used = COALESCE(used,0) + 1 WHERE code=?", (code,))
            db.commit(); db.close()
            self._json(200, {"ok": True, "credits_added": added, "message": "Code eingelöst!"}); return

        # Admin-Rechte vergeben/entziehen – AUSSCHLIESSLICH der Haupt-Admin (admin@sychos.net).
        # Delegierte Admins duerfen das NICHT; der Haupt-Admin ist selbst geschuetzt.
        if path == "/admin/setadmin":
            if not self._check_admin():
                self._json(403, {"ok": False, "error": "Kein Admin"}); return
            boss = self._get_user()
            if not boss or boss.get("email") != SUPER_ADMIN_EMAIL:
                self._json(403, {"ok": False, "error": "Nur der Haupt-Admin darf Admin-Rechte vergeben oder entziehen."}); return
            uid = body.get("uid", "")
            make = bool(body.get("admin", True))
            db = get_db()
            row = db.execute("SELECT is_admin, email FROM users WHERE uid=?", (uid,)).fetchone()
            if not row:
                db.close()
                self._json(404, {"ok": False, "error": "User nicht gefunden"}); return
            if row["email"] == SUPER_ADMIN_EMAIL:
                db.close()
                self._json(400, {"ok": False, "error": "Der Haupt-Admin ist geschuetzt."}); return
            if not make and row["is_admin"]:
                n = db.execute("SELECT COUNT(*) AS n FROM users WHERE is_admin=1").fetchone()["n"]
                if n <= 1:
                    db.close()
                    self._json(400, {"ok": False, "error": "Der letzte Admin kann nicht entzogen werden."}); return
            db.execute("UPDATE users SET is_admin=? WHERE uid=?", (1 if make else 0, uid))
            if not make:
                db.execute("DELETE FROM sessions WHERE uid=?", (uid,))
            db.commit(); db.close()
            self._json(200, {"ok": True, "is_admin": bool(make)}); return

        # User sperren/entsperren: Grund + Dauer (Minuten, 0 = dauerhaft)
        if path == "/admin/ban":
            if not self._check_admin():
                self._json(403, {"ok": False, "error": "Kein Admin"}); return
            uid = body.get("uid", "")
            ban = 1 if body.get("ban", True) else 0
            reason = (body.get("reason", "") or "").strip()[:200]
            try:
                duration = max(0, int(body.get("duration_minutes", 0) or 0))
            except (TypeError, ValueError):
                duration = 0
            until = (time.time() + duration * 60) if duration else 0
            db = get_db()
            cur = db.execute(
                "UPDATE users SET is_banned=?, ban_reason=?, ban_until=? WHERE uid=?",
                (ban, reason if ban else "", until if ban else 0, uid))
            db.commit(); db.close()
            if cur.rowcount == 0:
                self._json(404, {"ok": False, "error": "User nicht gefunden"}); return
            self._json(200, {"ok": True, "banned": bool(ban), "ban_until": until if ban else 0}); return

        # Admin vergibt "Sychos Paid" -> entsperrt Premium-Modelle
        if path == "/admin/paid":
            if not self._check_admin():
                self._json(403, {"ok": False, "error": "Kein Admin"}); return
            uid = body.get("uid", "")
            # paid: true/1 -> geben, false/0/"nein" -> WEGNEHMEN
            raw = body.get("paid", True)
            raw = body.get("paid", True)
            # Plan-Auswahl: 'plan' = free/basic/pro/max/ultra/business (ohne 'plan' alt: paid -> pro)
            pid = (body.get("plan") or "").strip().lower()
            if pid not in PLANS:
                pid = "pro" if not (raw is False or raw == 0 or str(raw).strip().lower() in ("0", "false", "nein", "weg", "off")) else "free"
            # Dauer in Minuten: 0 = dauerhaft, sonst zeitlich begrenzt (laeuft automatisch ab)
            try:
                dur = max(0, int(body.get("duration_minutes", 0) or 0))
            except (TypeError, ValueError):
                dur = 0
            until = (time.time() + dur * 60) if (pid != "free" and dur) else 0
            paid = 0 if pid == "free" else 1
            db = get_db()
            cur = db.execute("UPDATE users SET is_paid=?, paid_until=?, plan=?, plan_until=? WHERE uid=?",
                             (paid, until, pid, until, uid))
            db.commit(); db.close()
            if cur.rowcount == 0:
                self._json(404, {"ok": False, "error": "User nicht gefunden"}); return
            self._json(200, {"ok": True, "paid": bool(paid), "paid_until": until, "plan": pid,
                             "plan_name": PLANS[pid]["name"], "plan_until": until}); return

        # Admin: globale Provider-Keys setzen (leerer Key = entfernen)
        if path == "/admin/settings":
            if not self._check_admin():
                self._json(403, {"ok": False, "error": "Kein Admin"}); return
            db = get_db()
            for prov in ("gemini", "groq", "cline"):
                if prov + "_api_key" in body:
                    val = (body[prov + "_api_key"] or "").strip()[:200]
                    db.execute("INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)",
                               (prov + "_api_key", val))
            db.commit(); db.close()
            self._json(200, {"ok": True}); return

        # Server abschalten/an (Wartungsmodus) – AUSSCHLIESSLICH Haupt-Admin (admin@sychos.net).
        # Der Server bleibt technisch laufen: normale User sehen "Login gesperrt" bzw.
        # "Server derzeit nicht erreichbar" (Modelle + Chat aus), Admins arbeiten normal weiter.
        if path == "/admin/server":
            if not self._check_admin():
                self._json(403, {"ok": False, "error": "Kein Admin"}); return
            boss = self._get_user()
            if not boss or boss.get("email") != SUPER_ADMIN_EMAIL:
                self._json(403, {"ok": False, "error": "Nur admin@sychos.net darf den Server abschalten."}); return
            online = bool(body.get("online", True))
            set_maintenance(not online)
            msg = (body.get("message", "") or "").strip()
            if msg:
                db = get_db()
                set_setting(db, "maintenance_message", msg[:500])
                db.commit(); db.close()
            self._json(200, {"ok": True, "online": online, "maintenance": not online}); return

        # ── 2.0: Admin – Meldung aufloesen (ban/dismiss) ──
        if path == "/admin/reports/resolve":
            if not self._check_admin():
                self._json(403, {"ok": False, "error": "Kein Admin"}); return
            actor = self._get_user()
            rid = body.get("id")
            action = (body.get("action", "") or "").strip().lower()
            if action not in ("ban", "dismiss"):
                self._json(400, {"ok": False, "error": "Aktion muss 'ban' oder 'dismiss' sein."}); return
            db = get_db()
            rep = db.execute("SELECT * FROM reports WHERE id=?", (rid,)).fetchone()
            if not rep:
                db.close()
                self._json(404, {"ok": False, "error": "Meldung nicht gefunden"}); return
            if action == "ban" and rep["reported_uid"]:
                db.execute("UPDATE users SET is_banned=1, ban_reason='Gemeldet & gesperrt', ban_until=0 WHERE uid=?",
                           (rep["reported_uid"],))
            db.execute("UPDATE reports SET status='resolved', resolved_at=?, resolved_by=? WHERE id=?",
                       (time.time(), (actor["uid"] if actor else ""), rid))
            db.commit(); db.close()
            self._json(200, {"ok": True, "id": rid, "action": action}); return

        # ── 2.0: Admin – User verifizieren ──
        if path == "/admin/verify":
            if not self._check_admin():
                self._json(403, {"ok": False, "error": "Kein Admin"}); return
            uid2 = (body.get("uid", "") or "").strip()
            verified = 1 if body.get("verified") else 0
            db = get_db()
            cur = db.execute("UPDATE users SET verified=? WHERE uid=?", (verified, uid2))
            db.commit(); db.close()
            if cur.rowcount == 0:
                self._json(404, {"ok": False, "error": "User nicht gefunden"}); return
            self._json(200, {"ok": True, "uid": uid2, "verified": bool(verified)}); return

        # ── 2.0: Admin – Ansage (an einen User oder global '*') ──
        if path == "/admin/announce":
            if not self._check_admin():
                self._json(403, {"ok": False, "error": "Kein Admin"}); return
            actor = self._get_user()
            to_uid = (body.get("to_uid", "") or "").strip() or "*"
            title = (body.get("title", "") or "").strip()[:200]
            text = (body.get("body", "") or "").strip()[:2000]
            db = get_db()
            if to_uid not in ("*", ""):
                if not db.execute("SELECT 1 FROM users WHERE uid=?", (to_uid,)).fetchone():
                    db.close()
                    self._json(404, {"ok": False, "error": "Empfaenger nicht gefunden"}); return
            db.execute("INSERT INTO notifications (from_uid,to_uid,title,body,image,kind,read,created_at) "
                       "VALUES (?,?,?,?,?, 'admin',0,?)",
                       ((actor["uid"] if actor else ""), to_uid, title, text, "", time.time()))
            db.commit(); db.close()
            self._json(200, {"ok": True}); return

        # ── 2.0: Admin – Wartungstext setzen (nur Haupt-Admin) ──
        if path == "/admin/maintenance-notice":
            if not self._check_admin():
                self._json(403, {"ok": False, "error": "Kein Admin"}); return
            boss = self._get_user()
            if not boss or boss.get("email") != SUPER_ADMIN_EMAIL:
                self._json(403, {"ok": False, "error": "Nur admin@sychos.net darf den Wartungstext setzen."}); return
            msg = (body.get("message", "") or "").strip()[:500]
            db = get_db()
            set_setting(db, "maintenance_message", msg)
            db.commit(); db.close()
            self._json(200, {"ok": True, "message": msg}); return

        # Admin setzt das Passwort eines Users (alle Sessions des Users werden beendet).
        if path == "/admin/setpw":
            if not self._check_admin():
                self._json(403, {"ok": False, "error": "Kein Admin"}); return
            actor = self._get_user()
            uid = body.get("uid", "")
            new_pw = body.get("password", "")
            if len(new_pw) < 4:
                self._json(400, {"ok": False, "error": "Neues Passwort: mind. 4 Zeichen."}); return
            db = get_db()
            row = db.execute("SELECT email FROM users WHERE uid=?", (uid,)).fetchone()
            if not row:
                db.close()
                self._json(404, {"ok": False, "error": "User nicht gefunden"}); return
            if row["email"] == SUPER_ADMIN_EMAIL and (not actor or actor.get("email") != SUPER_ADMIN_EMAIL):
                db.close()
                self._json(403, {"ok": False, "error": "Das Passwort des Haupt-Admins darf nur er selbst setzen."}); return
            db.execute("UPDATE users SET password_hash=? WHERE uid=?", (hash_pw(new_pw), uid))
            db.execute("DELETE FROM sessions WHERE uid=?", (uid,))
            db.commit(); db.close()
            self._json(200, {"ok": True, "uid": uid}); return

def write_api_keys_to_db():
    """Traegt die Provider-API-Keys DIREKT in die Datenbank (settings) ein.

    Nutzung:
      python oracle_server.py --set-keys
          -> eingebaute bzw. per Env uebergebene Keys in die DB schreiben
      python oracle_server.py --set-keys GEMINI=xxx GROQ=yyy CLINE=zzz
          -> eigene Keys eintragen (leerer Wert = entfernen)
    Danach wird der Server automatisch gestartet (--no-start = nur in die DB schreiben).
    """
    custom = {}
    for a in sys.argv[1:]:
        if "=" in a and not a.startswith("-"):
            k, v = a.split("=", 1)
            custom[k.strip().upper()] = v
    keys = {
        "gemini": custom.get("GEMINI", GEMINI_API_KEY),
        "groq": custom.get("GROQ", GROQ_API_KEY),
        "cline": custom.get("CLINE", CLINE_API_KEY),
    }
    init_db()
    db = get_db()
    print("\n  API-Keys direkt in die DB schreiben (" + DB_PATH + "):")
    for prov, val in keys.items():
        val = (val or "").strip()
        db.execute("INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)",
                   (prov + "_api_key", val))
        print("    [OK] %-6s -> %s" % (prov.capitalize(), mask_key(val) if val else "(leer = entfernt)"))
    db.commit(); db.close()
    print("  Fertig. Keys sind sofort aktiv (Admin-Panel zeigt 'hinterlegt').")


# ═══════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════
def main():
    init_db(fresh="--fresh-db" in sys.argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print("\n  ========================================")
    print("     S Y C H O S   O R A C L E  v5.0     ")
    print("  ========================================")
    print(f"  Hub   : http://localhost:{PORT}")
    print(f"  Admin : http://localhost:{PORT}/admin/")
    print("  Domains: " + " / ".join(SYCHOS_DOMAINS))
    print(f"  DB    : {DB_PATH}")
    print("  ----------------------------------------")
    print("  ========================================\n")
    print("  -> Ctrl+C zum Beenden\n")
    # Always-On: selbstheilend + Port-Konflikte loesen (alter Sychos-Server)
    attempts = 0
    while True:
        try:
            server = HTTPServerCls((HOST, PORT), Handler)
        except OSError as e:
            err = getattr(e, "errno", 0)
            if err in (98, 48, 10048) and attempts < 2:
                attempts += 1
                # 1) zuerst alten Sychos-Server raeumen (wichtigster Schritt!)
                if free_port():
                    print("  Alter Sychos-Server beendet -> Port %d ist wieder frei..." % PORT)
                    continue
                # 2) erst danach pruefen, ob schon eine laufende Instanz existiert
                if _service_active():
                    print("\n  Laeuft bereits als Always-On Service (sychos-hub).")
                    print("  -> http://localhost:%d  (systemctl status sychos-hub)" % PORT)
                    return
                print("\n  Port %d ist belegt! Anderen Port waehlen:" % PORT)
                print("    Windows: set ORACLE_PORT=7778 && python oracle_server.py")
                print("    Linux:   ORACLE_PORT=7778 python3 oracle_server.py")
                sys.exit(1)
            raise
        attempts = 0
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\n  Server gestoppt.")
            server.server_close()
            break
        except Exception as e:
            print("  Fehler -> Neustart in 5s:", e)
            time.sleep(5)

def install_autostart():
    """Always-On: Windows = Autostart-Link, Linux = systemd (mit sudo)."""
    script = os.path.abspath(__file__)
    if os.name == "nt":
        import subprocess
        py = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        if not os.path.isfile(py):
            py = sys.executable
        startup = os.path.join(os.environ.get("APPDATA", ""),
            "Microsoft", "Windows", "Start Menu", "Programs", "Startup")
        lnk = os.path.join(startup, "SychosHub.lnk")
        ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%s');"
              "$s.TargetPath='%s';$s.Arguments='\"%s\"';$s.WindowStyle=7;$s.Save()"
              % (lnk.replace("'", "''"), py, script))
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True)
        print("  [OK] Autostart installiert (startet bei jedem Windows-Login):")
        print("       " + lnk)
    else:
        if os.geteuid() != 0:
            print("  Linux: Bitte mit sudo starten fuer systemd-Autostart:")
            print("         sudo python3 oracle_server.py --install")
            return
        unit = ("[Unit]\nDescription=Sychos Hub (Always-On)\nAfter=network.target\n\n"
                "[Service]\nType=simple\nExecStart=%s %s\nRestart=always\nRestartSec=5\n"
                "Environment=ORACLE_HOST=0.0.0.0\nEnvironment=ORACLE_PORT=%d\n\n"
                "[Install]\nWantedBy=multi-user.target\n"
                % (sys.executable, script, PORT))
        with open("/etc/systemd/system/sychos-hub.service", "w") as f:
            f.write(unit)
        os.system("systemctl daemon-reload")
        os.system("systemctl enable sychos-hub >/dev/null 2>&1")
        os.system("systemctl restart sychos-hub")
        print("  [OK] systemd-Service installiert (Always-On): sychos-hub")
        print("       -> laeuft bereits im Hintergrund, kein extra Start noetig")
        return True
    return False

def _service_active():
    """True, wenn der eigene systemd-Service sychos-hub bereits laeuft."""
    if os.name == "nt":
        return False
    return os.system("systemctl is-active --quiet sychos-hub >/dev/null 2>&1") == 0

def free_port():
    """Beendet alte Sychos-Prozesse (v2 / Sychos Net) auf dem Port — nie sich selbst."""
    me = os.getpid()
    me_path = os.path.abspath(__file__)
    pat = ("sychos-oracle", "SychosOracle")
    killed = []
    try:
        if os.name == "nt":
            import subprocess
            res = subprocess.run(["powershell", "-NoProfile", "-Command",
                "(Get-NetTCPConnection -LocalPort %d -State Listen).OwningProcess" % PORT],
                capture_output=True, text=True).stdout
            pids = set(x.strip() for x in res.splitlines() if x.strip())
            for pid in pids:
                if pid == str(me):
                    continue
                info = subprocess.run(["powershell", "-NoProfile", "-Command",
                    "(Get-CimInstance Win32_Process -Filter 'ProcessId=%s').CommandLine" % pid],
                    capture_output=True, text=True).stdout
                if any(p in info for p in pat) or (
                        "oracle_server" in info and me_path not in info and "sychos-hub" not in info):
                    subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)
                    killed.append(pid)
        else:
            # WICHTIG: alten systemd-Service (v2) stoppen, sonst startet er durch
            # Restart=always sofort wieder und beide streiten um den Port
            if hasattr(os, "geteuid") and os.geteuid() == 0:
                os.system("systemctl stop sychos-oracle >/dev/null 2>&1")
                os.system("systemctl disable sychos-oracle >/dev/null 2>&1")
            for pid in os.listdir("/proc"):
                if not pid.isdigit() or int(pid) == me:
                    continue
                try:
                    with open("/proc/%s/cmdline" % pid, "rb") as f:
                        cmd = f.read().replace(b"\0", b" ").decode("utf-8", "replace")
                except Exception:
                    continue
                if any(p in cmd for p in pat) or (
                        "oracle_server" in cmd and me_path not in cmd and "sychos-hub" not in cmd):
                    try:
                        os.kill(int(pid), 15)
                        killed.append(pid)
                    except Exception:
                        pass
    except Exception:
        pass
    if killed:
        time.sleep(1.5)
    # Erfolg = auf dem Port lauscht nichts mehr
    import socket as _sock
    try:
        c = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
        c.settimeout(1)
        c.connect(("127.0.0.1", PORT))
        c.close()
        return False
    except Exception:
        return True

def uninstall_autostart():
    if os.name == "nt":
        startup = os.path.join(os.environ.get("APPDATA", ""),
            "Microsoft", "Windows", "Start Menu", "Programs", "Startup")
        lnk = os.path.join(startup, "SychosHub.lnk")
        if os.path.isfile(lnk):
            os.remove(lnk)
        print("  [OK] Autostart entfernt.")
    else:
        os.system("systemctl disable --now sychos-hub >/dev/null 2>&1")
        if os.path.isfile("/etc/systemd/system/sychos-hub.service"):
            os.remove("/etc/systemd/system/sychos-hub.service")
        print("  [OK] systemd-Service entfernt.")

if __name__ == "__main__":
    if "--set-keys" in sys.argv:
        write_api_keys_to_db()
        if "--no-start" in sys.argv:
            sys.exit(0)
        print("  -> Server wird gestartet...\n")
    if "--install" in sys.argv:
        if install_autostart():
            sys.exit(0)      # Linux: Service laeuft bereits -> nicht doppelt starten
    elif "--uninstall" in sys.argv:
        uninstall_autostart()
        sys.exit(0)
    main()
