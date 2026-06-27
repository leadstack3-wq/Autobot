import streamlit as st
import pandas as pd
import sqlite3
import os
import time
import requests
import hashlib
from datetime import datetime
import ccxt

# ─── EXCHANGE REGISTRY — every exchange ccxt supports, binance pinned first ───
CCXT_EXCHANGES = sorted(ccxt.exchanges, key=lambda x: (x != "binance", x))

st.set_page_config(
    page_title="CCXT AI Terminal",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ─── DATABASE ─────────────────────────────────────────────────────────────────
_DATA_DIR = os.getenv("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
try:
    os.makedirs(_DATA_DIR, exist_ok=True)
except OSError:
    _DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
    os.makedirs(_DATA_DIR, exist_ok=True)
DB_FILE = os.path.join(_DATA_DIR, "ccxt_trades.db")

def init_db():
    try:
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute('''CREATE TABLE IF NOT EXISTS trade_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, prompt_hash TEXT,
            symbol TEXT, timeframe TEXT, action TEXT, quantity REAL,
            tp_pct REAL, sl_pct REAL, order_id TEXT, status TEXT)''')
        cur.execute('''CREATE TABLE IF NOT EXISTS strategy_cache (
            prompt_hash TEXT PRIMARY KEY, plain_text_prompt TEXT,
            compiled_code TEXT, created_at TEXT)''')
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
        conn.execute('''INSERT OR REPLACE INTO strategy_cache
            (prompt_hash, plain_text_prompt, compiled_code, created_at) VALUES (?,?,?,?)''',
            (prompt_hash, norm, compiled_code, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
        conn.close()
    except Exception as e:
        st.warning(f"Cache save warning: {e}")

def log_trade(prompt_hash, symbol, timeframe, action, quantity, tp, sl, order_id, status):
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.execute('''INSERT INTO trade_log
            (timestamp,prompt_hash,symbol,timeframe,action,quantity,tp_pct,sl_pct,order_id,status)
            VALUES (?,?,?,?,?,?,?,?,?,?)''',
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
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

# ─── SESSION STATE ────────────────────────────────────────────────────────────
_DEFAULTS = {
    "trading_active":        False,
    "current_compiled_code": "",
    "active_hash":           "INACTIVE",
    "analysis_success":      False,
    "signal_history":        [],
    "ccxt_markets_loaded":   False,
    # FIX #4 — pooled exchange object and the key it was built from
    "ccxt_exchange":         None,
    "ccxt_exchange_key":     "",
    # config tab inputs — stored here so reruns read from state, not re-render widgets
    "sb_exchange":           "binance",
    "sb_env":                "Testnet / Paper",
    "sb_api_key":            "",
    "sb_secret_key":         "",
    "sb_symbol":             "BTC/USDT",
    # AI provider config — user supplies their own key, picks any provider
    "sb_ai_provider":        "Gemini",
    "sb_ai_key":             "",
}
for k, v in _DEFAULTS.items():
    if k not in st.session_state:
        st.session_state[k] = v

# ─── CSS — CCXT Blue Theme · Sole Dashboard ───────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap');

/* ── hide all native chrome ── */
header[data-testid="stHeader"] { display:none !important; }
#MainMenu, footer { display:none !important; }
.stAppDeployButton,[data-testid="stAppDeployButton"],
button[title="View fullscreen"],[data-testid="StyledFullScreenButton"] { display:none !important; }
[data-testid="stSidebar"], .stSidebar { display:none !important; }
[data-testid="collapsedControl"],button[data-testid="baseButton-headerNoPadding"],
.stSidebarCollapsedControl,section[data-testid="stSidebarCollapsedControl"] {
    display: none !important;
}

:root {
    --p:#1E3A8A; --p-mid:#2563EB; --p-light:#DBEAFE; --p-xlight:#EFF6FF;
    --bg:#D6DCE8; --surface:#FFFFFF; --border:#E2E8F0;
    --text:#0F172A; --muted:#64748B; --label:#94A3B8;
    --green:#059669; --green-bg:#D1FAE5;
    --red:#DC2626; --red-bg:#FEE2E2; --yellow:#D97706;
    --sans:'Inter',system-ui,sans-serif; --mono:'JetBrains Mono','Courier New',monospace;
    --card-shadow: 0 8px 40px rgba(0,0,0,.13), 0 2px 8px rgba(0,0,0,.07);
}

/* ── dashboard background ── */
.stApp { background:var(--bg) !important; color:var(--text) !important; font-family:var(--sans) !important; font-size:17px !important; }

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

h1,h2,h3,h4,h5,h6 { font-family:var(--sans) !important; color:var(--text) !important; margin:0 0 .1rem !important; }
p,label,span,div { font-family:var(--sans) !important; font-size:17px; }

/* ── topbar ── */
.ccxt-topbar {
    background:linear-gradient(135deg,#1E3A8A 0%,#2563EB 60%,#1D4ED8 100%);
    padding:16px 24px 18px; border-radius:16px; margin:0 0 1.4rem;
    display:flex; align-items:center; justify-content:space-between;
    box-shadow:0 4px 18px rgba(37,99,235,.25);
}
.ccxt-topbar-title { font-size:24px; font-weight:800; color:#fff; }
.ccxt-topbar-sub { font-size:15px; color:rgba(255,255,255,.85); font-weight:500; letter-spacing:.6px; margin-top:2px; }
.ccxt-live-dot {
    display:inline-block; width:8px; height:8px; background:#34D399;
    border-radius:50%; margin-right:6px; box-shadow:0 0 0 2px rgba(52,211,153,.35);
    animation:pulse 1.8s infinite;
}
@keyframes pulse {
    0%,100% { box-shadow:0 0 0 2px rgba(52,211,153,.35); }
    50% { box-shadow:0 0 0 5px rgba(52,211,153,.1); }
}
.ccxt-topbar-badge {
    background:rgba(255,255,255,.2); border:1px solid rgba(255,255,255,.3);
    color:#fff; font-size:15px; font-weight:700; padding:4px 12px; border-radius:20px; letter-spacing:.5px;
}

/* ── section headers ── */
.ccxt-section-header { background:var(--surface); border:1px solid var(--border); border-top:3px solid var(--p); padding:10px 16px; border-radius:12px 12px 0 0; margin-bottom:-1px; }
.ccxt-section-header h3 { font-size:15px !important; font-weight:700 !important; color:var(--p) !important; text-transform:uppercase; letter-spacing:.8px; margin:0 !important; }

/* ── config section headers (formerly sidebar headers) ── */
.ccxt-config-header { background:var(--p-xlight); border-left:3px solid var(--p); padding:8px 12px; border-radius:8px; margin:14px 0 10px; }
.ccxt-config-header h3 { font-size:15px !important; font-weight:700 !important; color:var(--p) !important; text-transform:uppercase; letter-spacing:.9px; margin:0 !important; }

/* ── inputs ── */
div[data-baseweb="input"] input,.stNumberInput input {
    background:#FAFAFA !important; color:var(--text) !important;
    border:1px solid var(--border) !important; border-radius:8px !important; font-size:16px !important;
}
div[data-baseweb="input"] input:focus,.stNumberInput input:focus {
    border-color:var(--p) !important; box-shadow:0 0 0 2px rgba(37,99,235,.12) !important; background:#fff !important;
}
div[data-baseweb="select"]>div {
    background:#FAFAFA !important; border:1px solid var(--border) !important;
    border-radius:8px !important; color:var(--text) !important; font-size:16px !important;
}
.stTextInput label,.stNumberInput label,.stSelectbox label,.stTextArea label {
    color:var(--muted) !important; font-size:15px !important; font-weight:600 !important;
    text-transform:uppercase !important; letter-spacing:.7px !important;
}

/* ── strategy canvas ── */
.strategy-canvas { background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:6px 14px 14px; margin-bottom:14px; }
.strategy-canvas-label { font-size:14px; font-weight:700; color:var(--p); letter-spacing:1.4px; text-transform:uppercase; padding:8px 0; }
div[data-baseweb="textarea"] textarea {
    background:#EFF6FF !important; border:1.5px solid #BFDBFE !important; border-radius:8px !important;
    color:#1E3A8A !important; font-family:var(--mono) !important; font-size:15px !important;
    line-height:1.7 !important; min-height:160px; padding:12px !important;
}
div[data-baseweb="textarea"] textarea:focus { border-color:var(--p) !important; box-shadow:0 0 0 2px rgba(37,99,235,.15) !important; background:#FFFFFF !important; }

/* ── buttons ── */
.stButton > button {
    background:linear-gradient(135deg,var(--p-mid) 0%,var(--p) 100%) !important;
    color:#fff !important; border:none !important; border-radius:10px !important;
    font-weight:700 !important; font-size:15px !important; letter-spacing:.6px !important;
    text-transform:uppercase !important; padding:11px 20px !important;
    box-shadow:0 4px 14px rgba(37,99,235,.25) !important;
}
.stButton > button:hover { box-shadow:0 6px 20px rgba(37,99,235,.38) !important; }

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
    font-size: 15px !important; font-weight: 600 !important;
    padding: 9px 22px !important;
    border: none !important;
    border-radius: 9px !important;
    text-transform: uppercase;
}
button[data-baseweb="tab"][aria-selected="true"] {
    background: var(--surface) !important;
    color: var(--p) !important;
    box-shadow: 0 1px 4px rgba(0,0,0,.10) !important;
}

/* ── metric cards ── */
.metric-card { background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:14px 18px; margin-bottom:12px; box-shadow: 0 1px 4px rgba(0,0,0,.04); }
.metric-card-accent { border-top:3px solid var(--p); }
.metric-card-green { border-top:3px solid var(--green); }
.metric-card-red { border-top:3px solid var(--red); }
.metric-label { color:var(--label); font-size:14px; font-weight:700; text-transform:uppercase; letter-spacing:1px; margin-bottom:5px; }
.metric-value { color:var(--text); font-size:20px; font-weight:700; font-family:var(--mono); }
.signal-buy { background:var(--green-bg); color:var(--green); padding:2px 10px; border-radius:20px; font-weight:700; font-size:15px; display:inline-block; }
.signal-sell { background:var(--red-bg); color:var(--red); padding:2px 10px; border-radius:20px; font-weight:700; font-size:15px; display:inline-block; }
.signal-hold { background:#FEF3C7; color:var(--yellow); padding:2px 10px; border-radius:20px; font-weight:700; font-size:15px; display:inline-block; }

/* ── misc ── */
div[data-testid="stAlert"] { background:var(--surface) !important; border:1px solid var(--border) !important; border-radius:10px !important; font-size:16px !important; }
.stDataFrame,[data-testid="stDataFrame"] { background:var(--surface) !important; border:1px solid var(--border) !important; border-radius:12px !important; overflow:hidden; font-size:15px !important; }
.stDataFrame th { background:var(--p-xlight) !important; color:var(--p) !important; font-size:15px !important; font-weight:700 !important; text-transform:uppercase !important; }
details[data-testid="stExpander"] { background:var(--surface) !important; border:1px solid var(--border) !important; border-radius:10px !important; }
details[data-testid="stExpander"] summary { color:var(--muted) !important; font-size:16px !important; font-weight:600 !important; text-transform:uppercase; letter-spacing:.5px; }
::-webkit-scrollbar { width:5px; height:5px; }
::-webkit-scrollbar-track { background:var(--bg); }
::-webkit-scrollbar-thumb { background:rgba(37,99,235,.3); border-radius:3px; }
::-webkit-scrollbar-thumb:hover { background:var(--p); }
</style>
""", unsafe_allow_html=True)

# ─── HEADER ───────────────────────────────────────────────────────────────────
st.markdown("""
<div class="ccxt-topbar">
  <div>
    <div class="ccxt-topbar-title">⚡ CCXT AI Terminal</div>
    <div class="ccxt-topbar-sub"><span class="ccxt-live-dot"></span>LIVE · AI-Powered Algo Engine · Any CCXT Exchange</div>
  </div>
  <div class="ccxt-topbar-badge">AUTO TRADING</div>
</div>
""", unsafe_allow_html=True)

# ─── AI STRATEGY COMPILER — BYOK (Bring Your Own Key), multi-provider ─────────
SYSTEM_INSTRUCTION = (
    "You are an elite quantitative developer. "
    "Generate raw Python code containing exactly one function named `check_signal(exchange, symbol, timeframe)`. "
    "The function receives a ccxt exchange instance, a symbol string (e.g. 'BTC/USDT'), and a timeframe string (e.g. '5m'). "
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
    "DeepSeek":  {"model": "deepseek-v4-flash",          "key_hint": "sk-…"},
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

# FIX #1 — Seeded exec scope: the live ccxt exchange object, symbol, and
# timeframe are injected into the execution namespace so that
# check_signal(exchange, symbol, timeframe) resolves without NameError.
def run_in_memory_strategy(code_string: str, exchange, symbol: str, timeframe: str):
    try:
        import pandas as _pd
        import numpy as _np
        seeded_globals = {
            "__builtins__": __builtins__,
            "pd": _pd,
            "np": _np,
            "ccxt": ccxt,
        }
        local_scope = {}
        exec(code_string, seeded_globals, local_scope)
        if "check_signal" in local_scope:
            result = local_scope["check_signal"](exchange, symbol, timeframe)
            if isinstance(result, str):
                result = result.strip().upper()
            if result not in ("BUY", "SELL", "HOLD"):
                result = "HOLD"
            return result, None
        return "HOLD", "check_signal() not found in generated code."
    except Exception as exc:
        return None, str(exc)

# FIX #4 — Pooled exchange factory: build the ccxt exchange once and reuse it;
# only reconstruct when exchange ID, credentials, or environment changes.
def get_or_create_ccxt_exchange(exchange_id: str, api_key: str, secret_key: str, sandbox: bool):
    cache_key = f"{exchange_id}:{api_key}:{secret_key}:{sandbox}"
    if (st.session_state.ccxt_exchange is None
            or st.session_state.ccxt_exchange_key != cache_key):
        exchange_class = getattr(ccxt, exchange_id)
        exchange = exchange_class({
            "apiKey": api_key,
            "secret": secret_key,
            "enableRateLimit": True,
        })
        if sandbox:
            exchange.set_sandbox_mode(True)
        st.session_state.ccxt_exchange = exchange
        st.session_state.ccxt_exchange_key = cache_key
        st.session_state.ccxt_markets_loaded = False  # force market reload on new client
    return st.session_state.ccxt_exchange

# ─── CONFIG TAB fragment — widget keys never duplicate on rerun ───────────────
@st.fragment
def render_config_tab():
    st.warning("Keep this browser tab open and maintain a stable internet connection while the engine is running. Closing or refreshing the tab, losing connectivity, or putting your device to sleep can stop automated trading immediately.")
    st.markdown('<div class="ccxt-config-header" style="margin-top: 0;"><h3>🔑 API Credentials</h3></div>', unsafe_allow_html=True)
    st.session_state.sb_exchange = st.selectbox(
        "Exchange", CCXT_EXCHANGES,
        index=CCXT_EXCHANGES.index(st.session_state.sb_exchange),
        key="ccxt_cfg_exchange",
        help="Any exchange supported by the ccxt library. Spot trading only — make sure the exchange and API key support the order types you plan to use.",
    )
    st.session_state.sb_env = st.selectbox(
        "Environment", ["Testnet / Paper", "Live"],
        index=["Testnet / Paper", "Live"].index(st.session_state.sb_env),
        key="ccxt_cfg_env",
    )
    st.session_state.sb_api_key = st.text_input(
        "API Key", value=st.session_state.sb_api_key, placeholder="API Key",
        type="default", key="ccxt_cfg_api_key",
    )
    st.session_state.sb_secret_key = st.text_input(
        "Secret Key", value=st.session_state.sb_secret_key, placeholder="Secret Key",
        type="password", key="ccxt_cfg_secret",
    )
    st.markdown('<div class="ccxt-config-header"><h3>📈 Asset Config</h3></div>', unsafe_allow_html=True)
    st.session_state.sb_symbol = st.text_input(
        "Symbol", value=st.session_state.sb_symbol, key="ccxt_cfg_symbol",
    )
    st.markdown('<div class="ccxt-config-header"><h3>🤖 AI Provider (Bring Your Own Key)</h3></div>', unsafe_allow_html=True)
    st.session_state.sb_ai_provider = st.selectbox(
        "AI Provider",
        list(AI_PROVIDERS.keys()),
        index=list(AI_PROVIDERS.keys()).index(st.session_state.sb_ai_provider),
        key="ccxt_cfg_ai_provider",
    )
    _provider_info = AI_PROVIDERS[st.session_state.sb_ai_provider]
    st.session_state.sb_ai_key = st.text_input(
        f"{st.session_state.sb_ai_provider} API Key",
        value=st.session_state.sb_ai_key,
        placeholder=_provider_info["key_hint"],
        type="password",
        key="ccxt_cfg_ai_key",
    )
    st.caption(f"Model used: `{_provider_info['model']}` · Your key is kept only in this browser session, never stored server-side.")
    st.markdown('<div class="ccxt-config-header"><h3>⚙️ Engine Status</h3></div>', unsafe_allow_html=True)
    status_color = "#059669" if st.session_state.trading_active else "#94A3B8"
    status_label = "🟢 RUNNING" if st.session_state.trading_active else "⚪ IDLE"
    st.markdown(
        f'<div style="background:var(--surface);border:1px solid var(--border);'
        f'border-radius:14px;padding:16px;text-align:center;">'
        f'<span style="color:{status_color};font-weight:700;font-size:15px;">{status_label}</span></div>',
        unsafe_allow_html=True,
    )

# ─── MAIN TABS ────────────────────────────────────────────────────────────────
tab_config, tab_engine, tab_records = st.tabs(["⚙️  Config", "🚀  Engine", "📋  Records"])

with tab_config:
    render_config_tab()

# Read config values from session_state — never from widget return values directly
exchange_id     = st.session_state.sb_exchange
api_env         = st.session_state.sb_env
user_api_key    = st.session_state.sb_api_key
user_secret_key = st.session_state.sb_secret_key
target_symbol   = st.session_state.sb_symbol
is_sandbox      = (api_env == "Testnet / Paper")
ai_provider     = st.session_state.sb_ai_provider
ai_api_key      = st.session_state.sb_ai_key

# ─── ENGINE TAB ───────────────────────────────────────────────────────────────
with tab_engine:
    st.markdown('<div class="ccxt-section-header"><h3>⚙️ Trading Parameters</h3></div>', unsafe_allow_html=True)
    c1, c2 = st.columns(2)
    with c1:
        timeframe = st.selectbox("Timeframe", ["1m", "5m", "15m", "1h", "1d"], key="ccxt_tf")
        tp_pct    = st.number_input("Take Profit (%)", min_value=0.0, value=1.5, step=0.1, key="ccxt_tp")
    with c2:
        trade_qty = st.number_input("Quantity (Crypto)", min_value=0.0001, value=0.01, step=0.001, format="%.4f", key="ccxt_qty")
        sl_pct    = st.number_input("Stop Loss (%)", min_value=0.0, value=1.0, step=0.1, key="ccxt_sl")

    st.markdown("<br>", unsafe_allow_html=True)
    st.markdown('<div class="ccxt-section-header"><h3>📝 Strategy Prompt</h3></div>', unsafe_allow_html=True)
    st.markdown('<div class="strategy-canvas"><div class="strategy-canvas-label">Describe your strategy in plain English</div>', unsafe_allow_html=True)
    MACD_PLACEHOLDER = "# Strategy: MACD Crossover\n# Buy when MACD line crosses above the Signal line.\n# Sell when MACD line crosses below the Signal line.\n# Use default parameters (12, 26, 9) for MACD calculation.\n"
    user_prompt = st.text_area("", value=MACD_PLACEHOLDER, height=180, label_visibility="collapsed", key="ccxt_strategy")
    st.markdown("</div>", unsafe_allow_html=True)

    btn1, btn2 = st.columns(2)
    with btn1:
        if st.button("▶  START AUTO TRADING", use_container_width=True, key="ccxt_start"):
            if not user_api_key or not user_secret_key:
                st.error("⛔ Exchange API & Secret keys required before starting.")
            elif not ai_api_key:
                st.error(f"⛔ {ai_provider} API key required before starting. Add it in the Config tab.")
            else:
                st.session_state.ccxt_markets_loaded = False
                cached_code, prompt_hash, is_cached = get_cached_code(user_prompt)
                st.session_state.active_hash = prompt_hash
                if is_cached:
                    st.session_state.current_compiled_code = cached_code
                    st.session_state.trading_active = True
                    st.session_state.analysis_success = True
                    st.success("✅ Loaded strategy from cache.")
                else:
                    with st.spinner(f"{ai_provider} is compiling your strategy…"):
                        compiled, err = compile_prompt_via_ai(user_prompt, ai_provider, ai_api_key)
                    if err:
                        st.error(f"⛔ {ai_provider} Error: {err}")
                    else:
                        save_code_to_cache(prompt_hash, user_prompt, compiled)
                        st.session_state.current_compiled_code = compiled
                        st.session_state.trading_active = True
                        st.session_state.analysis_success = True
                        st.success("✅ Strategy compiled and ready.")
    with btn2:
        if st.button("⏹  STOP AUTO TRADING", use_container_width=True, key="ccxt_stop"):
            st.session_state.trading_active = False
            st.session_state.analysis_success = False
            st.session_state.ccxt_markets_loaded = False
            # Release pooled client on stop
            st.session_state.ccxt_exchange = None
            st.session_state.ccxt_exchange_key = ""
            st.warning("Trading paused. API Engine disconnected.")

    if st.session_state.current_compiled_code:
        with st.expander("🛠  View Compiled Strategy Code"):
            st.code(st.session_state.current_compiled_code, language="python")

    # FIX #2 — Non-blocking polling loop via @st.fragment(run_every=…).
    # The fragment reruns on its own cadence so the STOP button is always
    # responsive and Render health-check pings reach the main thread freely.
    @st.fragment(run_every=5)
    def _live_engine_fragment():
        if not st.session_state.trading_active or not st.session_state.current_compiled_code:
            return

        # FIX #4 — reuse pooled exchange; only reconnects when creds change
        try:
            exchange = get_or_create_ccxt_exchange(
                exchange_id, user_api_key, user_secret_key, is_sandbox
            )
        except Exception as exc:
            st.error(f"CCXT init failed: {exc}")
            st.session_state.trading_active = False
            return

        # Load markets once per session (not every tick)
        if not st.session_state.ccxt_markets_loaded:
            try:
                with st.spinner(f"Connecting to {exchange_id.capitalize()}…"):
                    exchange.load_markets()
                if target_symbol not in exchange.markets:
                    st.error(f"Symbol '{target_symbol}' not found on {exchange_id.capitalize()}.")
                    st.session_state.trading_active = False
                    return
                st.session_state.ccxt_markets_loaded = True
            except Exception as exc:
                st.error(f"CCXT connection failed: {exc}")
                st.session_state.trading_active = False
                return

        slot_symbol  = st.empty()
        slot_signal  = st.empty()
        slot_counter = st.empty()

        # FIX #1 — pass exchange, symbol, and timeframe into seeded exec scope
        signal_output, runtime_err = run_in_memory_strategy(
            st.session_state.current_compiled_code,
            exchange,
            target_symbol,
            timeframe,
        )

        if runtime_err:
            st.error(f"⛔ Execution error: {runtime_err}")
            st.session_state.trading_active = False
            return

        ts = datetime.now().strftime("%H:%M:%S")
        slot_symbol.markdown(
            f'<div class="metric-card metric-card-accent">'
            f'<div class="metric-label">Polling Target ({exchange_id.upper()})</div>'
            f'<div class="metric-value">📍 {target_symbol} &nbsp;·&nbsp; {timeframe}</div></div>',
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
            try:
                ticker        = exchange.fetch_ticker(target_symbol)
                current_price = ticker['last']
                if current_price is None:
                    raise ValueError("Could not fetch current price from ticker.")
                side        = "buy" if signal_output == "BUY" else "sell"
                order_params = {}
                if tp_pct > 0:
                    tp_price = current_price * (1 + tp_pct / 100) if side == "buy" else current_price * (1 - tp_pct / 100)
                    order_params['takeProfit'] = round(tp_price, 4)
                if sl_pct > 0:
                    sl_price = current_price * (1 - sl_pct / 100) if side == "buy" else current_price * (1 + sl_pct / 100)
                    order_params['stopLoss'] = round(sl_price, 4)
                order_resp = exchange.create_order(
                    symbol=target_symbol, type='market', side=side,
                    amount=float(trade_qty), params=order_params,
                )
                log_trade(st.session_state.active_hash, target_symbol, timeframe,
                          signal_output, trade_qty, tp_pct, sl_pct,
                          str(order_resp.get('id', 'N/A')), "SUCCESS")
                st.toast(f"🎯 {signal_output} {trade_qty}× {target_symbol} placed", icon="✅")
            except Exception as order_err:
                log_trade(st.session_state.active_hash, target_symbol, timeframe,
                          signal_output, trade_qty, tp_pct, sl_pct, "FAILED", str(order_err))
                st.error(f"Order error: {order_err}")

        slot_counter.markdown(
            f'<div style="text-align:center;color:var(--label);font-size:14px;padding:6px;">'
            f'Next poll in 5s · Tick #{len(st.session_state.signal_history)}</div>',
            unsafe_allow_html=True,
        )

    if st.session_state.trading_active and st.session_state.current_compiled_code:
        st.markdown("<br>", unsafe_allow_html=True)
        st.markdown('<div class="ccxt-section-header"><h3>📡 Live Engine</h3></div>', unsafe_allow_html=True)
        _live_engine_fragment()

# ─── RECORDS TAB ──────────────────────────────────────────────────────────────
with tab_records:
    if st.button("🔄 Refresh", key="ccxt_refresh"):
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
