import streamlit as st
import pandas as pd
import sqlite3
import os
import time
import requests
import hashlib
from datetime import datetime
from dhanhq import dhanhq
import streamlit_analytics2 as streamlit_analytics
try:
    from dhanhq import DhanContext
except ImportError:
    DhanContext = None  # dhanhq < 2.2.0 — dhanhq(client_id, access_token) is used directly instead

# ─────────────────────────────────────────────────────────────────────────────
# PAGE CONFIG
# ─────────────────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Free Dhan Automated Trading Bot",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ─────────────────────────────────────────────────────────────────────────────
# 1. DATABASE  — /data on Render (persistent disk), fallback to ./data locally
# ─────────────────────────────────────────────────────────────────────────────
_DATA_DIR = os.getenv("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
try:
    os.makedirs(_DATA_DIR, exist_ok=True)
except OSError:
    _DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
    os.makedirs(_DATA_DIR, exist_ok=True)
DB_FILE = os.path.join(_DATA_DIR, "dhan_trades.db")

def init_db():
    try:
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute('''
            CREATE TABLE IF NOT EXISTS trade_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT, prompt_hash TEXT, symbol TEXT,
                timeframe TEXT, action TEXT, quantity INTEGER,
                tp_pct REAL, sl_pct REAL, order_id TEXT, status TEXT
            )
        ''')
        cur.execute('''
            CREATE TABLE IF NOT EXISTS strategy_cache (
                prompt_hash TEXT PRIMARY KEY, plain_text_prompt TEXT,
                compiled_code TEXT, created_at TEXT
            )
        ''')
        conn.commit()
        conn.close()
    except Exception as e:
        st.error(f"DB init error: {e}")

def get_cached_code(prompt_text):
    norm = " ".join(prompt_text.strip().lower().split())
    h = hashlib.sha256(norm.encode()).hexdigest()
    try:
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute("SELECT compiled_code FROM strategy_cache WHERE prompt_hash=?", (h,))
        row = cur.fetchone()
        conn.close()
        return (row[0], h, True) if row else (None, h, False)
    except Exception:
        return (None, h, False)

def save_code_to_cache(prompt_hash, prompt_text, compiled_code):
    norm = " ".join(prompt_text.strip().lower().split())
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.execute('''
            INSERT OR REPLACE INTO strategy_cache
            (prompt_hash, plain_text_prompt, compiled_code, created_at) VALUES (?,?,?,?)
        ''', (prompt_hash, norm, compiled_code, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
        conn.close()
    except Exception as e:
        st.warning(f"Cache save warning: {e}")

def log_trade(prompt_hash, symbol, timeframe, action, quantity, tp, sl, order_id, status):
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.execute('''
            INSERT INTO trade_log
            (timestamp,prompt_hash,symbol,timeframe,action,quantity,tp_pct,sl_pct,order_id,status)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        ''', (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
              prompt_hash, symbol, timeframe, action, quantity, tp, sl, str(order_id), status))
        conn.commit()
        conn.close()
    except Exception as e:
        st.warning(f"Trade log warning: {e}")

def fetch_trade_history():
    try:
        if not os.path.exists(DB_FILE):
            return pd.DataFrame()
        conn = sqlite3.connect(DB_FILE)
        df = pd.read_sql_query("SELECT * FROM trade_log ORDER BY id DESC", conn)
        conn.close()
        return df
    except Exception:
        return pd.DataFrame()

init_db()

# ─────────────────────────────────────────────────────────────────────────────
# 2. SESSION STATE
#    mt5_initialized removed (MT5 dismantled).
#    FIX #4 — dhan_client and dhan_client_key added for connection pooling.
# ─────────────────────────────────────────────────────────────────────────────
for key, default in {
    "trading_active":        False,
    "current_compiled_code": "",
    "active_hash":           "INACTIVE",
    "analysis_success":      False,
    "signal_history":        [],
    # FIX #4 — pooled Dhan client and the credential key it was built from
    "dhan_client":           None,
    "dhan_client_key":       "",
    # AI provider config — user supplies their own key, picks any provider
    "sb_ai_provider":        "Gemini",
    "sb_ai_key":             "",
}.items():
    if key not in st.session_state:
        st.session_state[key] = default

# ─────────────────────────────────────────────────────────────────────────────
# 3. CSS — Dhan Purple Theme · Sole Dashboard
# ─────────────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap');

/* ── hide all native chrome ── */
header[data-testid="stHeader"] { display:none !important; }
#MainMenu, footer { display:none !important; }
.stAppDeployButton, [data-testid="stAppDeployButton"],
button[title="View fullscreen"], [data-testid="StyledFullScreenButton"] { display:none !important; }
[data-testid="stSidebar"], .stSidebar { display:none !important; }
[data-testid="collapsedControl"], button[data-testid="baseButton-headerNoPadding"],
.stSidebarCollapsedControl, section[data-testid="stSidebarCollapsedControl"] {
    display: none !important;
}

/* ── tokens ── */
:root {
    --p: #6B21A8; --p-mid: #7C3AED; --p-light: #EDE9FE; --p-xlight: #F5F3FF;
    --bg: #DDDAE6; --surface: #FFFFFF; --border: #E2DDED;
    --text: #1C1B2E; --muted: #6E6A85; --label: #9491A8;
    --green: #059669; --green-bg: #D1FAE5;
    --red: #DC2626; --red-bg: #FEE2E2; --yellow: #D97706;
    --sans: 'Inter', system-ui, sans-serif;
    --mono: 'JetBrains Mono', 'Courier New', monospace;
    --card-shadow: 0 8px 40px rgba(0,0,0,.13), 0 2px 8px rgba(0,0,0,.07);
}

/* ── dashboard background ── */
.stApp { background: var(--bg) !important; color: var(--text) !important; font-family: var(--sans) !important; }

/* ── elevated card container ── */
.block-container {
    background: var(--surface) !important;
    border-radius: 24px !important;
    max-width: 1100px !important;
    margin: 2rem auto !important;
    padding: 2rem 2.5rem 2.5rem !important;
    box-shadow: var(--card-shadow) !important;
}
@media (max-width: 768px) {
    .block-container {
        border-radius: 14px !important;
        margin: 0.75rem !important;
        padding: 1rem 1rem 1.5rem !important;
    }
}

h1,h2,h3,h4,h5,h6 { font-family: var(--sans) !important; color: var(--text) !important; margin: 0 0 .1rem !important; letter-spacing: -.2px; }
p, label, span, div { font-family: var(--sans) !important; }

/* ── topbar ── */
.dhan-topbar {
    background: linear-gradient(135deg, #5B21B6 0%, #6B21A8 60%, #4C1D95 100%);
    padding: 16px 24px 18px; border-radius: 16px; margin: 0 0 1.4rem 0;
    display: flex; align-items: center; justify-content: space-between;
    box-shadow: 0 4px 18px rgba(107,33,168,.28);
}
.dhan-topbar-title { font-size: 20px; font-weight: 800; color: #fff; letter-spacing: -.3px; }
.dhan-topbar-sub { font-size: 11px; color: rgba(255,255,255,.72); font-weight: 500; letter-spacing: .6px; margin-top: 2px; }
.dhan-live-dot {
    display: inline-block; width: 8px; height: 8px; background: #34D399;
    border-radius: 50%; margin-right: 6px; box-shadow: 0 0 0 2px rgba(52,211,153,.35);
    animation: pulse 1.8s infinite;
}
@keyframes pulse {
    0%,100% { box-shadow: 0 0 0 2px rgba(52,211,153,.35); }
    50% { box-shadow: 0 0 0 5px rgba(52,211,153,.1); }
}
.dhan-topbar-badge {
    background: rgba(255,255,255,.15); border: 1px solid rgba(255,255,255,.25);
    color: #fff; font-size: 11px; font-weight: 700; padding: 4px 12px;
    border-radius: 20px; letter-spacing: .5px;
}

/* ── section headers ── */
.dhan-section-header {
    background: var(--surface); border: 1px solid var(--border);
    border-top: 3px solid var(--p); padding: 10px 16px;
    border-radius: 12px 12px 0 0; margin-bottom: -1px;
}
.dhan-section-header h3 { font-size: 13px !important; font-weight: 700 !important; color: var(--p) !important; text-transform: uppercase; letter-spacing: .8px; margin: 0 !important; }

/* ── config section headers (formerly sidebar headers) ── */
.dhan-config-header { background: var(--p-xlight); border-left: 3px solid var(--p); padding: 8px 12px; border-radius: 8px; margin: 14px 0 10px; }
.dhan-config-header h3 { font-size: 11px !important; font-weight: 700 !important; color: var(--p) !important; text-transform: uppercase; letter-spacing: .9px; margin: 0 !important; }

/* ── inputs ── */
div[data-baseweb="input"] input, .stNumberInput input {
    background: #FAFAFA !important; color: var(--text) !important;
    border: 1px solid var(--border) !important; border-radius: 8px !important;
    font-size: 14px !important; font-family: var(--sans) !important;
    transition: border-color .15s ease, box-shadow .15s ease;
}
div[data-baseweb="input"] input:focus, .stNumberInput input:focus {
    border-color: var(--p) !important; box-shadow: 0 0 0 2px rgba(107,33,168,.12) !important; background: #fff !important;
}
div[data-baseweb="select"] { background-color: transparent !important; }
div[data-baseweb="select"] > div {
    background-color: #FAFAFA !important; border: 1px solid var(--border) !important;
    border-radius: 8px !important; color: var(--text) !important;
    transition: border-color .15s ease;
}
.stTextInput label, .stNumberInput label, .stSelectbox label, .stTextArea label {
    color: var(--muted) !important; font-size: 11px !important; font-weight: 600 !important;
    text-transform: uppercase !important; letter-spacing: .7px !important;
}

/* ── strategy canvas ── */
.strategy-canvas { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 6px 14px 14px; margin-bottom: 14px; }
.strategy-canvas-label { font-size: 10px; font-weight: 700; color: var(--p); letter-spacing: 1.4px; text-transform: uppercase; padding: 8px 0 8px; }
div[data-baseweb="textarea"] textarea {
    background: #F5F3FF !important; border: 1.5px solid #E2DDED !important;
    border-radius: 8px !important; color: #4C1D95 !important;
    font-family: var(--mono) !important; font-size: 13px !important;
    line-height: 1.7 !important; min-height: 160px; padding: 12px !important;
    transition: border-color .15s ease, box-shadow .15s ease;
}
div[data-baseweb="textarea"] textarea:focus { border-color: var(--p) !important; box-shadow: 0 0 0 2px rgba(107,33,168,.15) !important; background: #FFFFFF !important; }

/* ── buttons ── */
.stButton > button {
    background: linear-gradient(135deg, var(--p-mid) 0%, var(--p) 100%) !important;
    color: #fff !important; border: none !important; border-radius: 10px !important;
    font-family: var(--sans) !important; font-weight: 700 !important; font-size: 13px !important;
    letter-spacing: .6px !important; text-transform: uppercase !important;
    padding: 11px 20px !important; box-shadow: 0 4px 14px rgba(107,33,168,.25) !important;
    transition: all .15s ease;
}
.stButton > button:hover { transform: translateY(-1px); box-shadow: 0 6px 20px rgba(107,33,168,.38) !important; }

/* ── modern pill tabs ── */
div[data-baseweb="tab-list"] {
    background: #F3F4F6 !important;
    border-bottom: none !important;
    border-radius: 12px !important;
    padding: 4px !important;
    gap: 2px !important;
    margin-bottom: 1.2rem !important;
}
button[data-baseweb="tab"] {
    background: transparent !important;
    color: var(--muted) !important;
    font-size: 13px !important; font-weight: 600 !important;
    padding: 9px 22px !important;
    border: none !important;
    border-radius: 9px !important;
    text-transform: uppercase; letter-spacing: .4px;
    transition: all .15s ease;
}
button[data-baseweb="tab"][aria-selected="true"] {
    background: var(--surface) !important;
    color: var(--p) !important;
    box-shadow: 0 1px 4px rgba(0,0,0,.10) !important;
}

/* ── metric cards ── */
.metric-card { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 14px 18px; margin-bottom: 12px; box-shadow: 0 1px 4px rgba(0,0,0,.04); }
.metric-card-accent { border-top: 3px solid var(--p); }
.metric-card-green { border-top: 3px solid var(--green); }
.metric-card-red { border-top: 3px solid var(--red); }
.metric-label { color: var(--label); font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 5px; }
.metric-value { color: var(--text); font-size: 17px; font-weight: 700; font-family: var(--mono); }
.signal-buy { background: var(--green-bg); color: var(--green); padding: 2px 10px; border-radius: 20px; font-weight: 700; font-size: 13px; display: inline-block; }
.signal-sell { background: var(--red-bg); color: var(--red); padding: 2px 10px; border-radius: 20px; font-weight: 700; font-size: 13px; display: inline-block; }
.signal-hold { background: #FEF3C7; color: var(--yellow); padding: 2px 10px; border-radius: 20px; font-weight: 700; font-size: 13px; display: inline-block; }

/* ── misc ── */
div[data-testid="stAlert"] { background: var(--surface) !important; border: 1px solid var(--border) !important; border-radius: 10px !important; }
.stDataFrame, [data-testid="stDataFrame"] { background: var(--surface) !important; border: 1px solid var(--border) !important; border-radius: 12px !important; overflow: hidden; }
.stDataFrame th { background: var(--p-xlight) !important; color: var(--p) !important; font-size: 11px !important; font-weight: 700 !important; text-transform: uppercase !important; letter-spacing: .6px !important; }
details[data-testid="stExpander"] { background: var(--surface) !important; border: 1px solid var(--border) !important; border-radius: 10px !important; }
details[data-testid="stExpander"] summary { color: var(--muted) !important; font-size: 12px !important; font-weight: 600 !important; text-transform: uppercase; letter-spacing: .5px; }
::-webkit-scrollbar { width: 5px; height: 5px; }
::-webkit-scrollbar-track { background: var(--bg); }
::-webkit-scrollbar-thumb { background: rgba(107,33,168,.3); border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: var(--p); }
</style>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────────────────────
# 4. HEADER
# ─────────────────────────────────────────────────────────────────────────────
st.markdown("""
<div class="dhan-topbar">
  <div>
    <div class="dhan-topbar-title">⚡ Dhan AI Terminal</div>
    <div class="dhan-topbar-sub"><span class="dhan-live-dot"></span>LIVE · AI-Powered Algo Engine · NSE / BSE</div>
  </div>
  <div class="dhan-topbar-badge">AUTO TRADING</div>
</div>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────────────────────
# 5. AI STRATEGY COMPILER — BYOK (Bring Your Own Key), multi-provider
# ─────────────────────────────────────────────────────────────────────────────
SYSTEM_INSTRUCTION = (
    "You are an elite quantitative developer. "
    "Generate raw Python code containing exactly one function named `check_signal(client, symbol, timeframe)`. "
    "The function receives a dhanhq client object, a symbol string, and a timeframe string. "
    "It must return the string 'BUY', 'SELL', or 'HOLD'. "
    "Import any libraries you need inside the function. "
    "Do NOT include markdown code fences, comments, or explanations. "
    "Return ONLY plain executable Python lines."
)

# Provider registry — add new providers here without touching call sites.
AI_PROVIDERS = {
    "Gemini":    {"model": "gemini-2.5-flash-lite", "key_hint": "AIza…"},
    "OpenAI":    {"model": "gpt-4o-mini",            "key_hint": "sk-…"},
    "Anthropic": {"model": "claude-sonnet-4-6",      "key_hint": "sk-ant-…"},
    "DeepSeek":  {"model": "deepseek-chat",          "key_hint": "sk-…"},
}

def _strip_code_fences(raw: str) -> str:
    if "```python" in raw:
        raw = raw.split("```python")[1].split("```")[0]
    elif "```" in raw:
        raw = raw.split("```")[1].split("```")[0]
    return raw.strip()

def _compile_via_gemini(plain_prompt: str, api_key: str):
    model = AI_PROVIDERS["Gemini"]["model"]
    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    payload = {
        "system_instruction": {"parts": [{"text": SYSTEM_INSTRUCTION}]},
        "contents": [{"parts": [{"text": plain_prompt}]}],
        "generationConfig": {"temperature": 0.0, "maxOutputTokens": 1024},
    }
    resp = requests.post(endpoint, params={"key": api_key}, json=payload, timeout=30)
    if resp.status_code != 200:
        return None, f"Gemini API error {resp.status_code}: {resp.text[:300]}"
    raw = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    return _strip_code_fences(raw), None

def _compile_via_openai(plain_prompt: str, api_key: str):
    model = AI_PROVIDERS["OpenAI"]["model"]
    endpoint = "https://api.openai.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": model,
        "temperature": 0.0,
        "max_tokens": 1024,
        "messages": [
            {"role": "system", "content": SYSTEM_INSTRUCTION},
            {"role": "user", "content": plain_prompt},
        ],
    }
    resp = requests.post(endpoint, headers=headers, json=payload, timeout=30)
    if resp.status_code != 200:
        return None, f"OpenAI API error {resp.status_code}: {resp.text[:300]}"
    raw = resp.json()["choices"][0]["message"]["content"]
    return _strip_code_fences(raw), None

def _compile_via_anthropic(plain_prompt: str, api_key: str):
    model = AI_PROVIDERS["Anthropic"]["model"]
    endpoint = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "max_tokens": 1024,
        "temperature": 0.0,
        "system": SYSTEM_INSTRUCTION,
        "messages": [{"role": "user", "content": plain_prompt}],
    }
    resp = requests.post(endpoint, headers=headers, json=payload, timeout=30)
    if resp.status_code != 200:
        return None, f"Anthropic API error {resp.status_code}: {resp.text[:300]}"
    blocks = resp.json().get("content", [])
    raw = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    return _strip_code_fences(raw), None

def _compile_via_deepseek(plain_prompt: str, api_key: str):
    model = AI_PROVIDERS["DeepSeek"]["model"]
    endpoint = "https://api.deepseek.com/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": model,
        "temperature": 0.0,
        "max_tokens": 1024,
        "messages": [
            {"role": "system", "content": SYSTEM_INSTRUCTION},
            {"role": "user", "content": plain_prompt},
        ],
    }
    resp = requests.post(endpoint, headers=headers, json=payload, timeout=30)
    if resp.status_code != 200:
        return None, f"DeepSeek API error {resp.status_code}: {resp.text[:300]}"
    raw = resp.json()["choices"][0]["message"]["content"]
    return _strip_code_fences(raw), None

_PROVIDER_DISPATCH = {
    "Gemini":    _compile_via_gemini,
    "OpenAI":    _compile_via_openai,
    "Anthropic": _compile_via_anthropic,
    "DeepSeek":  _compile_via_deepseek,
}

def compile_prompt_via_ai(plain_prompt: str, provider: str, api_key: str):
    """Dispatches strategy compilation to whichever AI provider the user picked,
    using the key the user entered in the Config tab (BYOK — never a server-side env var)."""
    if not api_key:
        return None, f"{provider} API key required. Add it in the Config tab."
    handler = _PROVIDER_DISPATCH.get(provider)
    if handler is None:
        return None, f"Unsupported AI provider: {provider}"
    try:
        return handler(plain_prompt, api_key)
    except Exception as exc:
        return None, str(exc)

# FIX #1 — Seeded exec scope: the live dhanhq client, symbol, and timeframe are
# injected so check_signal(client, symbol, timeframe) resolves without NameError.
def run_in_memory_strategy(code_string: str, client, symbol: str, timeframe: str):
    try:
        import pandas as _pd
        import numpy as _np
        seeded_globals = {
            "__builtins__": __builtins__,
            "pd": _pd,
            "np": _np,
        }
        local_scope = {}
        exec(code_string, seeded_globals, local_scope)
        if "check_signal" in local_scope:
            result = local_scope["check_signal"](client, symbol, timeframe)
            if isinstance(result, str):
                result = result.strip().upper()
            if result not in ("BUY", "SELL", "HOLD"):
                result = "HOLD"
            return result, None
        return "HOLD", "check_signal() not found in generated code."
    except Exception as exc:
        return None, str(exc)

# FIX #4 — Pooled Dhan client factory: authenticate once; only rebuild when
# credentials actually change, eliminating per-tick auth handshakes.
def get_or_create_dhan_client(client_id: str, access_token: str):
    cache_key = f"{client_id}:{access_token}"
    if (st.session_state.dhan_client is None
            or st.session_state.dhan_client_key != cache_key):
        if DhanContext is not None:
            # dhanhq >= 2.2.0 — requires a DhanContext wrapper
            st.session_state.dhan_client = dhanhq(DhanContext(client_id, access_token))
        else:
            # dhanhq < 2.2.0 — client_id/access_token passed directly
            st.session_state.dhan_client = dhanhq(client_id, access_token)
        st.session_state.dhan_client_key = cache_key
    return st.session_state.dhan_client

# ─────────────────────────────────────────────────────────────────────────────
# 6. MAIN TABS
# ─────────────────────────────────────────────────────────────────────────────
with streamlit_analytics.track():
    tab_config, tab_engine, tab_records = st.tabs(["⚙️  Config", "🚀  Engine", "📋  Records"])
    
    # ─── CONFIG TAB ───────────────────────────────────────────────────────────────
    with tab_config:
        st.warning("Keep this browser tab open and maintain a stable internet connection while the engine is running. Closing or refreshing the tab, losing connectivity, or putting your device to sleep can stop automated trading immediately.")
        st.markdown('<div class="dhan-config-header" style="margin-top: 0;"><h3>🔑 Dhan Credentials</h3></div>', unsafe_allow_html=True)
        user_client_id   = st.text_input("Client ID", type="password", placeholder="Dhan Client ID", key="dhan_client_id")
        user_access_token = st.text_input("Access Token", type="password", placeholder="Dhan Access Token", key="dhan_token")
        st.markdown('<div class="dhan-config-header"><h3>📈 Asset Config</h3></div>', unsafe_allow_html=True)
        target_symbol = st.text_input("Symbol", value="RELIANCE", key="dhan_symbol")
        security_id   = st.text_input("Security ID", value="1333", key="dhan_sec_id")
        exchange_seg  = st.selectbox("Exchange", ["NSE_EQ", "NSE_FNO", "BSE_EQ"], key="dhan_exchange_select")
        st.markdown('<div class="dhan-config-header"><h3>🤖 AI Provider (Bring Your Own Key)</h3></div>', unsafe_allow_html=True)
        st.session_state.sb_ai_provider = st.selectbox(
            "AI Provider",
            list(AI_PROVIDERS.keys()),
            index=list(AI_PROVIDERS.keys()).index(st.session_state.sb_ai_provider),
            key="dhan_cfg_ai_provider",
        )
        _provider_info = AI_PROVIDERS[st.session_state.sb_ai_provider]
        st.session_state.sb_ai_key = st.text_input(
            f"{st.session_state.sb_ai_provider} API Key",
            value=st.session_state.sb_ai_key,
            placeholder=_provider_info["key_hint"],
            type="password",
            key="dhan_cfg_ai_key",
        )
        st.caption(f"Model used: `{_provider_info['model']}` · Your key is kept only in this browser session, never stored server-side.")
        st.markdown('<div class="dhan-config-header"><h3>⚙️ Engine Status</h3></div>', unsafe_allow_html=True)
        status_color = "#059669" if st.session_state.trading_active else "#9491A8"
        status_label = "🟢 RUNNING" if st.session_state.trading_active else "⚪ IDLE"
        st.markdown(f'<div style="background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:16px;text-align:center;"><span style="color:{status_color};font-weight:700;font-size:13px;">{status_label}</span></div>', unsafe_allow_html=True)
    
    # ─────────────────────────────────────────────────────────────────────────────
    # 7. ENGINE TAB
    # ─────────────────────────────────────────────────────────────────────────────
    with tab_engine:
        # Read config values from session state so Engine tab always has current values
        _client_id    = st.session_state.get("dhan_client_id", "")
        _access_token = st.session_state.get("dhan_token", "")
        _symbol       = st.session_state.get("dhan_symbol", "RELIANCE")
        _security_id  = st.session_state.get("dhan_sec_id", "1333")
        _exchange_seg = st.session_state.get("dhan_exchange_select", "NSE_EQ")
        _ai_provider  = st.session_state.sb_ai_provider
        _ai_api_key   = st.session_state.sb_ai_key
    
        st.markdown('<div class="dhan-section-header"><h3>⚙️ Trading Parameters</h3></div>', unsafe_allow_html=True)
        c1, c2 = st.columns(2)
        with c1:
            timeframe = st.selectbox("Timeframe", ["1 Min", "5 Min", "15 Min", "1 Hour", "Daily"], key="dhan_tf_select")
            tp_pct    = st.number_input("Take Profit (%)", min_value=0.0, value=1.5, step=0.1, key="dhan_tp")
        with c2:
            trade_qty = st.number_input("Quantity", min_value=1, value=1, key="dhan_qty")
            sl_pct    = st.number_input("Stop Loss (%)", min_value=0.0, value=1.0, step=0.1, key="dhan_sl")
    
        st.markdown("<br>", unsafe_allow_html=True)
        st.markdown('<div class="dhan-section-header"><h3>📝 Strategy Prompt</h3></div>', unsafe_allow_html=True)
        st.markdown('<div class="strategy-canvas"><div class="strategy-canvas-label">Describe your strategy in plain English</div>', unsafe_allow_html=True)
        MACD_PLACEHOLDER = "# Strategy: MACD Crossover\n# Buy when MACD line crosses above the Signal line.\n# Sell when MACD line crosses below the Signal line.\n# Use default parameters (12, 26, 9) for MACD calculation.\n"
        user_prompt = st.text_area("", value=MACD_PLACEHOLDER, height=180, label_visibility="collapsed", key="dhan_strategy")
        st.markdown("</div>", unsafe_allow_html=True)
    
        btn1, btn2 = st.columns(2)
        with btn1:
            if st.button("▶  START AUTO TRADING", use_container_width=True, key="dhan_start"):
                if not _client_id or not _access_token:
                    st.error("⛔ Dhan credentials required before starting.")
                elif not _ai_api_key:
                    st.error(f"⛔ {_ai_provider} API key required before starting. Add it in the Config tab.")
                else:
                    cached_code, prompt_hash, is_cached = get_cached_code(user_prompt)
                    st.session_state.active_hash = prompt_hash
                    if is_cached:
                        st.session_state.current_compiled_code = cached_code
                        st.session_state.trading_active = True
                        st.session_state.analysis_success = True
                        st.success("✅ Loaded strategy from cache.")
                    else:
                        with st.spinner(f"{_ai_provider} is compiling your strategy…"):
                            compiled, err = compile_prompt_via_ai(user_prompt, _ai_provider, _ai_api_key)
                        if err:
                            st.error(f"⛔ {_ai_provider} Error: {err}")
                        else:
                            save_code_to_cache(prompt_hash, user_prompt, compiled)
                            st.session_state.current_compiled_code = compiled
                            st.session_state.trading_active = True
                            st.session_state.analysis_success = True
                            st.success("✅ Strategy compiled and ready.")
        with btn2:
            if st.button("⏹  STOP AUTO TRADING", use_container_width=True, key="dhan_stop"):
                st.session_state.trading_active = False
                st.session_state.analysis_success = False
                # Release the pooled client on stop
                st.session_state.dhan_client = None
                st.session_state.dhan_client_key = ""
                st.warning("Trading paused. Strategy code preserved in session.")
    
        if st.session_state.current_compiled_code:
            with st.expander("🛠  View Compiled Strategy Code"):
                st.code(st.session_state.current_compiled_code, language="python")
    
        # FIX #2 — Non-blocking polling loop via @st.fragment(run_every=…).
        # The fragment owns its own rerun cycle so STOP state changes propagate
        # immediately and Render health-check pings are never blocked.
        @st.fragment(run_every=5)
        def _live_engine_fragment():
            if not st.session_state.trading_active or not st.session_state.current_compiled_code:
                return
    
            # FIX #4 — reuse pooled Dhan client; only re-authenticates when creds change
            try:
                dhan_client = get_or_create_dhan_client(_client_id, _access_token)
            except Exception as exc:
                st.error(f"Dhan connection error: {exc}")
                st.session_state.trading_active = False
                return
    
            slot_symbol  = st.empty()
            slot_signal  = st.empty()
            slot_counter = st.empty()
    
            # FIX #1 — pass dhan_client, symbol, and timeframe into seeded exec scope
            signal_output, runtime_err = run_in_memory_strategy(
                st.session_state.current_compiled_code,
                dhan_client,
                _symbol,
                timeframe,
            )
    
            if runtime_err:
                st.error(f"⛔ Execution error: {runtime_err}")
                st.session_state.trading_active = False
                return
    
            ts = datetime.now().strftime("%H:%M:%S")
            slot_symbol.markdown(
                f'<div class="metric-card metric-card-accent">'
                f'<div class="metric-label">Polling Target</div>'
                f'<div class="metric-value">📍 {_symbol} &nbsp;·&nbsp; {timeframe}</div></div>',
                unsafe_allow_html=True,
            )
    
            sig_class  = "metric-card-green" if signal_output == "BUY" else "metric-card-red" if signal_output == "SELL" else "metric-card-accent"
            badge_html = f'<span class="signal-{signal_output.lower()}">{signal_output}</span>'
            slot_signal.markdown(
                f'<div class="metric-card {sig_class}">'
                f'<div class="metric-label">AI Signal &nbsp;·&nbsp; {ts}</div>'
                f'<div class="metric-value">{badge_html}</div></div>',
                unsafe_allow_html=True,
            )
            st.session_state.signal_history.append({"time": ts, "signal": signal_output})
    
            if signal_output in ("BUY", "SELL"):
                p_mode = "BO" if (tp_pct > 0 or sl_pct > 0) else "INTRADAY"
                try:
                    order_resp = dhan_client.place_order(
                        tag="AI_Dhan_Terminal", transaction_type=signal_output,
                        exchange_segment=_exchange_seg, product_type=p_mode,
                        order_type="MARKET", validity="DAY", quantity=int(trade_qty),
                        security_id=str(_security_id), price=0, trigger_price=0,
                        disclosed_quantity=0, after_market_order=False, amo_time="OPEN",
                        bo_profit_value=float(tp_pct), bo_stop_loss_Value=float(sl_pct),
                    )
                    order_id = order_resp.get("data", {}).get("orderId", "N/A")
                    log_trade(st.session_state.active_hash, _symbol, timeframe,
                              signal_output, trade_qty, tp_pct, sl_pct, order_id, "SUCCESS")
                    st.toast(f"🎯 {signal_output} {trade_qty}× {_symbol} placed", icon="✅")
                except Exception as order_err:
                    log_trade(st.session_state.active_hash, _symbol, timeframe,
                              signal_output, trade_qty, tp_pct, sl_pct, "FAILED", str(order_err))
                    st.error(f"Order error: {order_err}")
    
            slot_counter.markdown(
                f'<div style="text-align:center;color:var(--label);font-size:12px;padding:6px;">'
                f'Next poll in 5s · Tick #{len(st.session_state.signal_history)}</div>',
                unsafe_allow_html=True,
            )
    
        if st.session_state.trading_active and st.session_state.current_compiled_code:
            st.markdown("<br>", unsafe_allow_html=True)
            st.markdown('<div class="dhan-section-header"><h3>📡 Live Engine</h3></div>', unsafe_allow_html=True)
            _live_engine_fragment()
    
    # ─────────────────────────────────────────────────────────────────────────────
    # 8. RECORDS TAB
    # ─────────────────────────────────────────────────────────────────────────────
    with tab_records:
        if st.button("🔄 Refresh", key="dhan_refresh"):
            st.rerun()
        hist_df = fetch_trade_history()
        if not hist_df.empty:
            st.dataframe(hist_df, use_container_width=True)
            st.markdown("<br>", unsafe_allow_html=True)
            total  = len(hist_df)
            buys   = len(hist_df[hist_df["action"] == "BUY"])
            sells  = len(hist_df[hist_df["action"] == "SELL"])
            errors = len(hist_df[hist_df["status"] != "SUCCESS"])
            s1, s2, s3, s4 = st.columns(4)
            for col, label, val, cls in [
                (s1, "Total Trades", total,  "metric-card-accent"),
                (s2, "BUY Orders",   buys,   "metric-card-green"),
                (s3, "SELL Orders",  sells,  "metric-card-red"),
                (s4, "Errors",       errors, "metric-card-accent"),
            ]:
                with col:
                    st.markdown(
                        f'<div class="metric-card {cls}"><div class="metric-label">{label}</div>'
                        f'<div class="metric-value">{val}</div></div>',
                        unsafe_allow_html=True,
                    )
        else:
            st.info("No trades logged yet. Start the engine to begin recording.")
