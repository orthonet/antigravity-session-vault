import streamlit as st
import sqlite3
import json
import os
from pathlib import Path
from datetime import datetime, timezone
import pandas as pd

from config import (
    BACKUP_BASE_DIR,
    BACKUP_CONVERSATIONS_DIR,
    BACKUP_BRAIN_DIR,
    BACKUP_CATALOG_DB,
    DAEMON_HEARTBEAT_FILE,
    LIVE_BASE_DIR,
    LIVE_SUMMARIES_DB,
    LOGO_PATH
)
from core.catalog import CatalogManager
from core.db_extractor import extract_chat_timeline, extract_file_payloads
from core.restorer import restore_conversation
from core.importer import scan_offsite_source, execute_import
from core.backup_engine import sync_live_to_backup
from core.classifier import format_display_title
from core.search_engine import search_backup_conversations
from core.daemon import get_daemon_live_status, start_daemon_process, stop_daemon_process
from core.pin_sentry import run_pin_sentry

st.set_page_config(
    page_title="Antigravity Session Vault",
    page_icon=str(LOGO_PATH) if LOGO_PATH.exists() else "🛡️",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Theme Engine: support URL query params (?theme=dark), session state, and Auto-detect
query_theme = st.query_params.get("theme", "")
if isinstance(query_theme, list):
    query_theme = query_theme[0] if query_theme else ""
if query_theme.lower() == "dark":
    st.session_state["vault_theme_override"] = "Dark"
elif query_theme.lower() == "light":
    st.session_state["vault_theme_override"] = "Light"

theme_choice = st.session_state.get("vault_theme_override", "Auto (Detect)")
override_css = ""
if theme_choice == "Dark":
    override_css = """
    :root, html, body, [data-testid="stAppViewContainer"], .stApp {
        --vault-header-bg: #0e1117 !important;
        --vault-border: #31333f !important;
        --vault-divider: #262730 !important;
        --vault-shadow: rgba(0, 0, 0, 0.35) !important;
        --vault-metric-bg: rgba(255, 255, 255, 0.05) !important;
        --vault-metric-border: rgba(255, 255, 255, 0.12) !important;
        --vault-metric-label: #94a3b8 !important;
        --vault-metric-val: #f8fafc !important;
        --vault-badge-complete-bg: rgba(34, 197, 94, 0.15) !important;
        --vault-badge-complete-text: #4ade80 !important;
        --vault-badge-evicted-bg: rgba(148, 163, 184, 0.15) !important;
        --vault-badge-evicted-text: #94a3b8 !important;
        --vault-badge-pinned-bg: rgba(245, 158, 11, 0.15) !important;
        --vault-badge-pinned-text: #fbbf24 !important;
        --vault-badge-queue-bg: rgba(168, 85, 247, 0.15) !important;
        --vault-badge-queue-text: #c084fc !important;
        --vault-badge-interactive-bg: rgba(14, 165, 233, 0.15) !important;
        --vault-badge-interactive-text: #38bdf8 !important;
        --vault-bubble-user-bg: #161e2e !important;
        --vault-bubble-user-border: #3b82f6 !important;
        --vault-bubble-assistant-bg: #131720 !important;
        --vault-bubble-assistant-border: #10b981 !important;
        --vault-bubble-tool-bg: #11141c !important;
        --vault-bubble-tool-border: #f59e0b !important;
        --vault-bubble-text: #f1f5f9 !important;
        --vault-bubble-subtext: #94a3b8 !important;
        --vault-snippet-bg: #161922 !important;
        --vault-snippet-border: #3b4252 !important;
        --vault-snippet-text: #e2e8f0 !important;
    }
    .stApp, [data-testid="stAppViewContainer"] {
        background-color: #0e1117 !important;
        color: #fafafa !important;
    }
    .stApp h1, .stApp h2, .stApp h3, .stApp p, .stApp span, .stApp label {
        color: #f1f5f9;
    }
    [data-testid="stSidebar"] {
        background-color: #1a1d24 !important;
    }
    [data-testid="stSidebar"] h1,
    [data-testid="stSidebar"] h2,
    [data-testid="stSidebar"] h3,
    [data-testid="stSidebar"] p,
    [data-testid="stSidebar"] span,
    [data-testid="stSidebar"] label,
    [data-testid="stSidebar"] strong {
        color: #e2e8f0 !important;
    }
    [data-testid="stSidebar"] .stCaption,
    [data-testid="stSidebar"] [data-testid="stCaptionContainer"] {
        color: #94a3b8 !important;
    }
    button[data-testid="baseButton-secondary"],
    button[kind="secondary"] {
        background-color: #262730 !important;
        color: #f1f5f9 !important;
        border-color: #3b4252 !important;
    }
    div[data-baseweb="select"] > div {
        background-color: #1e222a !important;
        color: #f1f5f9 !important;
        border-color: #3b4252 !important;
    }
    input, textarea {
        background-color: #1e222a !important;
        color: #f1f5f9 !important;
        border-color: #3b4252 !important;
    }
    """
elif theme_choice == "Light":
    override_css = """
    :root, html, body, [data-testid="stAppViewContainer"], .stApp {
        --vault-header-bg: #ffffff !important;
        --vault-border: #e2e8f0 !important;
        --vault-divider: #e2e8f0 !important;
        --vault-shadow: rgba(0, 0, 0, 0.03) !important;
        --vault-metric-bg: #f8fafc !important;
        --vault-metric-border: #e2e8f0 !important;
        --vault-metric-label: #475569 !important;
        --vault-metric-val: #0f172a !important;
        --vault-badge-complete-bg: #dcfce7 !important;
        --vault-badge-complete-text: #15803d !important;
        --vault-badge-evicted-bg: #f1f5f9 !important;
        --vault-badge-evicted-text: #64748b !important;
        --vault-badge-pinned-bg: #fef3c7 !important;
        --vault-badge-pinned-text: #b45309 !important;
        --vault-badge-queue-bg: #ede9fe !important;
        --vault-badge-queue-text: #6d28d9 !important;
        --vault-badge-interactive-bg: #e0f2fe !important;
        --vault-badge-interactive-text: #0369a1 !important;
        --vault-bubble-user-bg: #eff6ff !important;
        --vault-bubble-user-border: #2563eb !important;
        --vault-bubble-assistant-bg: #ffffff !important;
        --vault-bubble-assistant-border: #10b981 !important;
        --vault-bubble-tool-bg: #f8fafc !important;
        --vault-bubble-tool-border: #f59e0b !important;
        --vault-bubble-text: #1e293b !important;
        --vault-bubble-subtext: #64748b !important;
        --vault-snippet-bg: #f8fafc !important;
        --vault-snippet-border: #cbd5e1 !important;
        --vault-snippet-text: #334155 !important;
    }
    .stApp, [data-testid="stAppViewContainer"] {
        background-color: #ffffff !important;
        color: #1F2328 !important;
    }
    [data-testid="stSidebar"] {
        background-color: #f8fafc !important;
    }
    """

if override_css:
    st.markdown(f"<style>{override_css}</style>", unsafe_allow_html=True)

# Custom CSS for polished typography, badges, and layout with full Light & Dark mode support
st.markdown("""
<style>
    /* ========================================================= */
    /* THEME ENGINE: SEMANTIC DESIGN TOKENS (LIGHT & DARK MODES) */
    /* ========================================================= */
    :root {
        --vault-header-bg: #ffffff;
        --vault-border: #e2e8f0;
        --vault-divider: #e2e8f0;
        --vault-shadow: rgba(0, 0, 0, 0.03);

        /* Sidebar Metric Cards */
        --vault-metric-bg: #f8fafc;
        --vault-metric-border: #e2e8f0;
        --vault-metric-label: #475569;
        --vault-metric-val: #0f172a;

        /* Status Badges */
        --vault-badge-complete-bg: #dcfce7;
        --vault-badge-complete-text: #15803d;
        --vault-badge-evicted-bg: #f1f5f9;
        --vault-badge-evicted-text: #64748b;
        --vault-badge-pinned-bg: #fef3c7;
        --vault-badge-pinned-text: #b45309;
        --vault-badge-queue-bg: #ede9fe;
        --vault-badge-queue-text: #6d28d9;
        --vault-badge-interactive-bg: #e0f2fe;
        --vault-badge-interactive-text: #0369a1;

        /* Chat Timeline Bubbles */
        --vault-bubble-user-bg: #eff6ff;
        --vault-bubble-user-border: #2563eb;
        --vault-bubble-assistant-bg: #ffffff;
        --vault-bubble-assistant-border: #10b981;
        --vault-bubble-tool-bg: #f8fafc;
        --vault-bubble-tool-border: #f59e0b;
        --vault-bubble-text: #1e293b;
        --vault-bubble-subtext: #64748b;

        /* Search Snippet Highlights */
        --vault-snippet-bg: #f8fafc;
        --vault-snippet-border: #cbd5e1;
        --vault-snippet-text: #334155;
    }

    /* Dark Mode Tokens triggered by data-theme="dark" attribute OR OS prefers-color-scheme */
    :root[data-theme="dark"],
    html[data-theme="dark"],
    body[data-theme="dark"],
    [data-theme="dark"] {
        --vault-header-bg: #0e1117;
        --vault-border: #31333f;
        --vault-divider: #262730;
        --vault-shadow: rgba(0, 0, 0, 0.35);

        /* Sidebar Metric Cards: translucent dark glass style */
        --vault-metric-bg: rgba(255, 255, 255, 0.05);
        --vault-metric-border: rgba(255, 255, 255, 0.12);
        --vault-metric-label: #94a3b8;
        --vault-metric-val: #f8fafc;

        /* Status Badges: dark translucent tint with vibrant readable text */
        --vault-badge-complete-bg: rgba(34, 197, 94, 0.15);
        --vault-badge-complete-text: #4ade80;
        --vault-badge-evicted-bg: rgba(148, 163, 184, 0.15);
        --vault-badge-evicted-text: #94a3b8;
        --vault-badge-pinned-bg: rgba(245, 158, 11, 0.15);
        --vault-badge-pinned-text: #fbbf24;
        --vault-badge-queue-bg: rgba(168, 85, 247, 0.15);
        --vault-badge-queue-text: #c084fc;
        --vault-badge-interactive-bg: rgba(14, 165, 233, 0.15);
        --vault-badge-interactive-text: #38bdf8;

        /* Chat Timeline Bubbles: dark sleek surfaces */
        --vault-bubble-user-bg: #161e2e;
        --vault-bubble-user-border: #3b82f6;
        --vault-bubble-assistant-bg: #131720;
        --vault-bubble-assistant-border: #10b981;
        --vault-bubble-tool-bg: #11141c;
        --vault-bubble-tool-border: #f59e0b;
        --vault-bubble-text: #f1f5f9;
        --vault-bubble-subtext: #94a3b8;

        /* Search Snippet Highlights */
        --vault-snippet-bg: #161922;
        --vault-snippet-border: #3b4252;
        --vault-snippet-text: #e2e8f0;
    }

    @media (prefers-color-scheme: dark) {
        :root:not([data-theme="light"]) {
            --vault-header-bg: #0e1117;
            --vault-border: #31333f;
            --vault-divider: #262730;
            --vault-shadow: rgba(0, 0, 0, 0.35);

            /* Sidebar Metric Cards: translucent dark glass style */
            --vault-metric-bg: rgba(255, 255, 255, 0.05);
            --vault-metric-border: rgba(255, 255, 255, 0.12);
            --vault-metric-label: #94a3b8;
            --vault-metric-val: #f8fafc;

            /* Status Badges: dark translucent tint with vibrant readable text */
            --vault-badge-complete-bg: rgba(34, 197, 94, 0.15);
            --vault-badge-complete-text: #4ade80;
            --vault-badge-evicted-bg: rgba(148, 163, 184, 0.15);
            --vault-badge-evicted-text: #94a3b8;
            --vault-badge-pinned-bg: rgba(245, 158, 11, 0.15);
            --vault-badge-pinned-text: #fbbf24;
            --vault-badge-queue-bg: rgba(168, 85, 247, 0.15);
            --vault-badge-queue-text: #c084fc;
            --vault-badge-interactive-bg: rgba(14, 165, 233, 0.15);
            --vault-badge-interactive-text: #38bdf8;

            /* Chat Timeline Bubbles: dark sleek surfaces */
            --vault-bubble-user-bg: #161e2e;
            --vault-bubble-user-border: #3b82f6;
            --vault-bubble-assistant-bg: #131720;
            --vault-bubble-assistant-border: #10b981;
            --vault-bubble-tool-bg: #11141c;
            --vault-bubble-tool-border: #f59e0b;
            --vault-bubble-text: #f1f5f9;
            --vault-bubble-subtext: #94a3b8;

            /* Search Snippet Highlights */
            --vault-snippet-bg: #161922;
            --vault-snippet-border: #3b4252;
            --vault-snippet-text: #e2e8f0;
        }
    }

    /* Main app container padding optimization to align with fixed top header */
    .block-container {
        padding-top: 42px !important;
        padding-bottom: 2rem !important;
        padding-left: 2rem !important;
        padding-right: 2rem !important;
        max-width: 98% !important;
    }

    /* Fixed Persistent System Header: Ultra-slim 38px bar without bloated padding */
    header[data-testid="stHeader"] {
        height: 38px !important;
        min-height: 38px !important;
        max-height: 38px !important;
        padding: 0 !important;
        background-color: var(--vault-header-bg) !important;
        border-bottom: 1px solid var(--vault-border) !important;
        z-index: 999990 !important;
    }

    header[data-testid="stHeader"] [data-testid="stToolbar"] {
        right: 1.5rem !important;
        top: 0 !important;
        height: 38px !important;
        min-height: 38px !important;
        max-height: 38px !important;
        display: flex !important;
        align-items: center !important;
    }

    /* Persistent Sticky Navigation Tab Bar */
    div[data-testid="stTabs"] {
        overflow: visible !important;
    }

    /* Target the tablist directly (supports react-aria in Streamlit 1.64+ as well as legacy BaseWeb) */
    div[data-testid="stTabs"] [role="tablist"],
    [data-testid="stTabs"] [role="tablist"],
    [data-testid="stTabs"] [data-baseweb="tab-list"] {
        position: sticky !important;
        top: 38px !important; /* Docks EXACTLY flush against the 38px fixed header with 0 gap and 0 overlap */
        z-index: 990 !important;
        background-color: var(--vault-header-bg) !important;
        padding-top: 4px !important;
        padding-bottom: 4px !important;
        margin-bottom: 0.5rem !important;
        border-bottom: 1px solid var(--vault-border) !important;
        box-shadow: 0 2px 4px var(--vault-shadow) !important;
        width: 100% !important;
    }

    [data-testid="stTabsScrollLeft"],
    [data-testid="stTabsScrollRight"] {
        position: sticky !important;
        top: 38px !important;
        z-index: 995 !important;
    }

    /* Ensure dropdown popovers sit above sticky tabs */
    div[data-baseweb="popover"],
    div[data-baseweb="menu"],
    [data-testid="stSelectbox"] div[role="listbox"] {
        z-index: 999999 !important;
    }

    /* Sidebar top padding optimization & Container Context */
    [data-testid="stSidebar"] {
        container-type: inline-size;
        container-name: stsidebar;
    }

    [data-testid="stSidebarContent"] {
        padding-top: 0.75rem !important;
        padding-bottom: 1rem !important;
    }

    /* Sidebar Metrics: Responsive flex-wrap grid (2-column on standard width, 1-column on narrow sidebar) */
    .sidebar-metrics-container [data-testid="stHorizontalBlock"] {
        flex-wrap: wrap !important;
        gap: 6px !important;
    }
    .sidebar-metrics-container [data-testid="stHorizontalBlock"] > [data-testid="column"] {
        min-width: 135px !important;
        flex: 1 1 135px !important;
    }

    /* Container query: collapse to single vertical column when sidebar is dragged to narrow/minimum width */
    @container stsidebar (max-width: 310px) {
        .sidebar-metrics-container [data-testid="stHorizontalBlock"] > [data-testid="column"] {
            min-width: 100% !important;
            width: 100% !important;
            flex: 1 1 100% !important;
            margin-bottom: 2px !important;
        }
    }

    /* Compact Metric Card Styling in Sidebar */
    [data-testid="stSidebar"] [data-testid="stMetric"] {
        background-color: var(--vault-metric-bg) !important;
        border: 1px solid var(--vault-metric-border) !important;
        border-radius: 6px;
        padding: 5px 8px !important;
        margin-bottom: 4px !important;
    }
    [data-testid="stSidebar"] [data-testid="stMetricValue"] {
        font-size: 1.25rem !important;
        font-weight: 700 !important;
        line-height: 1.1 !important;
        color: var(--vault-metric-val) !important;
    }
    [data-testid="stSidebar"] [data-testid="stMetricLabel"] {
        font-size: 0.78rem !important;
        font-weight: 500 !important;
        margin-bottom: -2px !important;
        color: var(--vault-metric-label) !important;
    }

    /* Badges */
    .metric-card {
        background-color: var(--vault-metric-bg);
        border-radius: 8px;
        padding: 14px 18px;
        border-left: 4px solid #4f46e5;
        border: 1px solid var(--vault-border);
        box-shadow: 0 1px 3px var(--vault-shadow);
    }
    .badge-complete, .badge-active {
        background-color: var(--vault-badge-complete-bg);
        color: var(--vault-badge-complete-text);
        padding: 3px 8px;
        border-radius: 4px;
        font-weight: 600;
        font-size: 0.8rem;
    }
    .badge-safeguarded {
        background-color: rgba(16, 185, 129, 0.15);
        color: #10b981;
        border: 1px solid rgba(16, 185, 129, 0.4);
        padding: 2px 7px;
        border-radius: 4px;
        font-weight: 600;
        font-size: 0.8rem;
    }
    .badge-offsite {
        background-color: var(--vault-badge-complete-bg);
        color: var(--vault-badge-complete-text);
        padding: 3px 8px;
        border-radius: 4px;
        font-weight: 600;
        font-size: 0.8rem;
    }
    .badge-evicted {
        background-color: var(--vault-badge-evicted-bg);
        color: var(--vault-badge-evicted-text);
        padding: 3px 8px;
        border-radius: 4px;
        font-weight: 600;
        font-size: 0.8rem;
    }
    .badge-pinned {
        background-color: var(--vault-badge-pinned-bg);
        color: var(--vault-badge-pinned-text);
        padding: 3px 8px;
        border-radius: 4px;
        font-weight: 600;
        font-size: 0.8rem;
    }
    .badge-queue {
        background-color: var(--vault-badge-queue-bg);
        color: var(--vault-badge-queue-text);
        padding: 3px 8px;
        border-radius: 4px;
        font-weight: 600;
        font-size: 0.8rem;
    }
    .badge-interactive {
        background-color: var(--vault-badge-interactive-bg);
        color: var(--vault-badge-interactive-text);
        padding: 3px 8px;
        border-radius: 4px;
        font-weight: 600;
        font-size: 0.8rem;
    }
    .chat-bubble-user {
        background-color: var(--vault-bubble-user-bg);
        border-left: 4px solid var(--vault-bubble-user-border);
        color: var(--vault-bubble-text);
        padding: 12px 16px;
        border-radius: 6px;
        margin-bottom: 12px;
        overflow-wrap: anywhere !important;
        word-break: break-word !important;
    }
    .chat-bubble-assistant {
        background-color: var(--vault-bubble-assistant-bg);
        border-left: 4px solid var(--vault-bubble-assistant-border);
        border-top: 1px solid var(--vault-border);
        border-right: 1px solid var(--vault-border);
        border-bottom: 1px solid var(--vault-border);
        color: var(--vault-bubble-text);
        padding: 10px 14px;
        border-radius: 6px;
        margin-bottom: 6px;
    }
    .chat-bubble-tool {
        background-color: var(--vault-bubble-tool-bg);
        border-left: 4px solid var(--vault-bubble-tool-border);
        border-top: 1px solid var(--vault-border);
        border-right: 1px solid var(--vault-border);
        border-bottom: 1px solid var(--vault-border);
        color: var(--vault-bubble-subtext);
        padding: 10px 14px;
        border-radius: 6px;
        margin-bottom: 12px;
        font-size: 0.9rem;
        overflow-wrap: anywhere !important;
        word-break: break-word !important;
    }

    /* Search Snippet Highlight */
    .search-snippet-hit {
        background-color: var(--vault-snippet-bg);
        border-left: 3px solid var(--vault-snippet-border);
        padding: 6px 10px;
        margin: 3px 0 6px 0;
        border-radius: 4px;
        font-size: 0.92rem;
        color: var(--vault-snippet-text);
        line-height: 1.4;
    }

    /* Section Break Lines (st.divider and hr): Halved vertical padding and margins */
    hr,
    [data-testid="stDivider"],
    div[data-testid="stMarkdownContainer"] > hr {
        margin-top: 0.65rem !important;
        margin-bottom: 0.65rem !important;
        padding-top: 0 !important;
        padding-bottom: 0 !important;
        border: none !important;
        border-top: 1px solid var(--vault-divider) !important;
    }

    [data-testid="stSidebar"] hr,
    [data-testid="stSidebar"] [data-testid="stDivider"],
    [data-testid="stSidebar"] div[data-testid="stMarkdownContainer"] > hr {
        margin-top: 0.45rem !important;
        margin-bottom: 0.45rem !important;
        padding-top: 0 !important;
        padding-bottom: 0 !important;
    }

    div[data-testid="stElementContainer"]:has(hr),
    div[data-testid="stElementContainer"]:has([data-testid="stDivider"]) {
        padding-top: 0 !important;
        padding-bottom: 0 !important;
    }

    /* Subtle Divider */
    .vault-divider {
        margin: 4px 0 !important;
        border: none;
        border-top: 1px solid var(--vault-divider) !important;
    }

    /* Responsive Chat Timeline Wrapper */
    .chat-timeline-wrapper {
        max-width: 100% !important;
        overflow-x: hidden !important;
    }
    .chat-timeline-wrapper h1 {
        font-size: 1.25rem !important;
        font-weight: 700 !important;
        margin-top: 0.5rem !important;
        margin-bottom: 0.3rem !important;
        line-height: 1.3 !important;
    }
    .chat-timeline-wrapper h2 {
        font-size: 1.12rem !important;
        font-weight: 600 !important;
        margin-top: 0.4rem !important;
        margin-bottom: 0.25rem !important;
        line-height: 1.3 !important;
    }
    .chat-timeline-wrapper h3 {
        font-size: 1.02rem !important;
        font-weight: 600 !important;
        margin-top: 0.3rem !important;
        margin-bottom: 0.2rem !important;
    }
    .chat-timeline-wrapper p, .chat-timeline-wrapper li, .chat-timeline-wrapper ul, .chat-timeline-wrapper ol {
        word-break: break-word !important;
        overflow-wrap: anywhere !important;
        max-width: 100% !important;
    }
    .chat-timeline-wrapper pre, .chat-timeline-wrapper code {
        white-space: pre-wrap !important;
        word-break: break-all !important;
        max-width: 100% !important;
        overflow-x: auto !important;
    }
</style>
""", unsafe_allow_html=True)

# Client-side Theme Observer: sync data-theme attribute with computed Streamlit theme
st.html("""
<script>
(function() {
    function detectAndSyncTheme() {
        try {
            const doc = window.parent.document.documentElement || document.documentElement;
            const app = (window.parent.document.querySelector && window.parent.document.querySelector('.stApp')) || 
                        document.querySelector('.stApp') ||
                        (window.parent.document.body || document.body);
            if (!doc || !app) return;

            let isDark = false;
            let themeSource = 'auto';

            try {
                const ls = window.parent.localStorage || localStorage;
                const savedTheme = ls.getItem('stActiveTheme-/-v2');
                if (savedTheme) {
                    const parsed = savedTheme.toLowerCase();
                    if (parsed.includes('dark')) {
                        isDark = true;
                        themeSource = 'localStorage';
                    } else if (parsed.includes('light')) {
                        isDark = false;
                        themeSource = 'localStorage';
                    }
                }
            } catch(e) {}

            if (themeSource === 'auto') {
                const computed = window.getComputedStyle(app);
                const bg = computed.backgroundColor;
                const rgb = bg.match(/\\d+/g);
                if (rgb && rgb.length >= 3) {
                    const lum = 0.299 * parseInt(rgb[0]) + 0.587 * parseInt(rgb[1]) + 0.114 * parseInt(rgb[2]);
                    isDark = lum < 128;
                }
            }

            const currentTheme = doc.getAttribute('data-theme');
            const targetTheme = isDark ? 'dark' : 'light';
            if (currentTheme !== targetTheme) {
                doc.setAttribute('data-theme', targetTheme);
            }
        } catch (err) {
            console.warn('[VaultThemeSync] error:', err);
        }
    }

    detectAndSyncTheme();
    if (!window.__vault_theme_observer_attached) {
        window.__vault_theme_observer_attached = true;
        setInterval(detectAndSyncTheme, 800);
    }
})();
</script>
""", unsafe_allow_javascript=True)

catalog = CatalogManager()

def open_conversation_viewer(cid: str):
    """Callback executed before rerun to safely switch tabs without widget lifecycle conflict."""
    st.session_state["selected_cid"] = cid
    st.session_state["main_nav_tab"] = "📖 Transcript & Plan Viewer"

def navigate_to_explorer():
    """Callback executed before rerun to safely switch navigation tab to Explorer without widget lifecycle conflict."""
    st.session_state["main_nav_tab"] = "📂 Conversation Explorer"

def dismiss_import_receipt():
    """Callback executed before rerun to safely dismiss the import receipt banner."""
    st.session_state.pop("import_receipt", None)

def execute_restore_callback(cid: str, target_workspace: Optional[str] = None):
    """Callback executed before rerun to restore a session safely without widget lifecycle conflict."""
    try:
        if not target_workspace:
            ws_from_state = st.session_state.get("viewer_target_ws")
            if ws_from_state and ws_from_state != "(Keep Original)":
                target_workspace = ws_from_state
        res = restore_conversation(cid, target_workspace_uri=target_workspace)
        st.session_state["restore_action_feedback"] = res
    except Exception as e:
        st.session_state["restore_action_feedback"] = {
            "success": False,
            "error": str(e),
            "conversation_id": cid
        }

def toggle_pinned_callback(cid: str, current_pinned: bool):
    """Callback executed before rerun to toggle pinned status without widget lifecycle conflict."""
    new_pinned = not current_pinned
    catalog.update_pinned_status(cid, new_pinned)
    if new_pinned:
        # Check if conversation is evicted or missing from live, and auto-resurrect immediately
        conv = catalog.get_conversation_by_id(cid)
        if conv and (conv.get("is_evicted_from_live") or conv.get("retention_status") != "complete_active"):
            if conv.get("has_db_file"):
                try:
                    res = restore_conversation(cid, overwrite_live=True)
                    st.session_state["restore_action_feedback"] = res
                except Exception as e:
                    st.session_state["restore_action_feedback"] = {"success": False, "error": str(e), "conversation_id": cid}

def format_local_timestamp(iso_str: str) -> str:
    """Converts a raw UTC ISO timestamp from Antigravity DB to local time with timezone."""
    if not iso_str:
        return "Unknown"
    try:
        cleaned = str(iso_str).strip().replace("Z", "+00:00")
        if " " in cleaned and "T" not in cleaned:
            dt_utc = datetime.fromisoformat(cleaned).replace(tzinfo=timezone.utc)
        else:
            dt_utc = datetime.fromisoformat(cleaned)
            if dt_utc.tzinfo is None:
                dt_utc = dt_utc.replace(tzinfo=timezone.utc)
        dt_local = dt_utc.astimezone()
        tz_abbr = dt_local.strftime("%Z")
        return dt_local.strftime("%Y-%m-%d %H:%M:%S") + (f" ({tz_abbr})" if tz_abbr else "")
    except Exception:
        return str(iso_str)[:19]

# Sidebar: App Stats and Daemon Monitor
with st.sidebar:
    if LOGO_PATH.exists():
        st.image(str(LOGO_PATH), width=70)
    st.title("Session Vault")
    st.caption("Bypassing Antigravity's Rolling Eviction Cap")

    @st.fragment(run_every=15)
    def render_sidebar_daemon_controls():
        # Check Daemon Live Status via Process ID verification
        daemon_status, heartbeat_detail, hb = get_daemon_live_status()

        st.markdown(f"**Sync Daemon**: {daemon_status}")
        st.caption(f"Status: {heartbeat_detail}")

        metrics = catalog.get_summary_metrics()
        pinned_total = metrics.get('pinned', 11)
        st.caption(f"🛡️ **Pin Sentry**: Active ({pinned_total} Protected)")

        # Sidebar Control Buttons
        col_d1, col_d2 = st.columns([1, 1.3])
        stop_clicked = False
        start_clicked = False
        with col_d1:
            if daemon_status.startswith("🟢"):
                stop_clicked = st.button("⏹️ Stop", key="sidebar_stop_daemon_btn", width="stretch", help="Stop background continuous sync daemon")
            else:
                start_clicked = st.button("▶️ Start", key="sidebar_start_daemon_btn", width="stretch", help="Start background continuous sync daemon")
        with col_d2:
            sync_clicked = st.button("🔄 Sync Live Now", width="stretch", help="Sync live sessions to vault & run Pin Sentry protection")

        # Full-width action handling & intermediate progress (outside narrow columns)
        if stop_clicked:
            ok = stop_daemon_process()
            if ok:
                st.session_state["sidebar_action_feedback"] = {"type": "info", "message": "Sync daemon stopped cleanly."}
                st.toast("Sync daemon stopped", icon="⏹️")
            else:
                st.session_state["sidebar_action_feedback"] = {"type": "error", "message": "Error stopping sync daemon."}
            st.rerun()

        if start_clicked:
            ok, res = start_daemon_process(30)
            if ok:
                st.session_state["sidebar_action_feedback"] = {"type": "success", "message": f"Sync daemon started (PID {res})."}
                st.toast(f"Sync daemon active (PID {res})", icon="🟢")
            else:
                st.session_state["sidebar_action_feedback"] = {"type": "error", "message": f"Failed to start daemon: {res}"}
                st.toast("Failed to start daemon", icon="❌")
            st.rerun()

        if sync_clicked:
            with st.spinner("Syncing sessions & running Pin Sentry..."):
                res = sync_live_to_backup(auto_protect_pinned=True)
                if res.get("success"):
                    dbs = res.get('synced_dbs', 0)
                    brains = res.get('synced_brains', 0)
                    ps = res.get("pin_sentry", {})
                    resurrected = ps.get("auto_resurrected", 0)
                    refreshed = ps.get("refreshed", 0)

                    msg = f"Synced {dbs} DBs & {brains} brains."
                    if resurrected > 0:
                        msg += f" 🛡️ Resurrected {resurrected} pinned sessions to Live!"
                    elif refreshed > 0:
                        msg += f" 🛡️ Refreshed {refreshed} pinned keep-alive timestamps."
                    else:
                        msg += f" (All {pinned_total} pinned sessions active in AGY)"
                    st.session_state["sidebar_action_feedback"] = {"type": "success", "message": msg}
                    st.toast(msg, icon="✅")
                    st.rerun()
                else:
                    err_msg = res.get("error", "Unknown sync error")
                    st.session_state["sidebar_action_feedback"] = {"type": "error", "message": f"Sync failed: {err_msg}"}
                    st.rerun()

        # Full-width feedback banner underneath both buttons in sidebar
        if "sidebar_action_feedback" in st.session_state:
            fb = st.session_state.pop("sidebar_action_feedback")
            fb_type = fb.get("type", "info")
            fb_msg = fb.get("message", "")
            if fb_type == "success":
                st.success(fb_msg)
            elif fb_type == "error":
                st.error(fb_msg)
            else:
                st.info(fb_msg)

    render_sidebar_daemon_controls()

    st.divider()

    # Metrics Summary (Responsive 2-column grid that wraps to 1-column on narrow sidebar)
    metrics = catalog.get_summary_metrics()
    st.markdown('<div class="sidebar-metrics-container">', unsafe_allow_html=True)
    st.metric("Total Catalogued", f"{metrics['total']:,}")

    col_m1, col_m2 = st.columns(2)
    with col_m1:
        st.metric("🟢 Active in AGY", f"{metrics['complete_active']:,}")
        st.metric("📋 Metadata Only", f"{metrics['metadata_only_evicted']:,}")
        st.metric("⭐ Pinned", f"{metrics['pinned']:,}")
    with col_m2:
        st.metric("🛡️ Safeguarded", f"{metrics.get('safeguarded_evicted', 0):,}")
        st.metric("📥 Offsite Restored", f"{metrics.get('imported_from_offsite', 0):,}")
        st.metric("💼 Workspaces", f"{metrics.get('workspaces', 0):,}")
    st.markdown('</div>', unsafe_allow_html=True)

    st.divider()

    st.caption(f"**Archive Path**: `{BACKUP_BASE_DIR}`")

# Main Navigation Tabs
NAV_TABS = [
    "📂 Conversation Explorer",
    "📖 Transcript & Plan Viewer",
    "🔍 Global Search",
    "📥 Offsite Backup Importer",
    "⚙️ Health & Settings"
]

if "main_nav_tab" not in st.session_state:
    st.session_state["main_nav_tab"] = NAV_TABS[0]

tab_explore, tab_viewer, tab_search, tab_importer, tab_settings = st.tabs(
    NAV_TABS,
    key="main_nav_tab",
    on_change="rerun"
)

# -------------------------------------------------------------
# TAB 1: CONVERSATION EXPLORER
# -------------------------------------------------------------
with tab_explore:
    st.subheader("Conversation Catalog & Management")

    if "restore_action_feedback" in st.session_state:
        fb = st.session_state.pop("restore_action_feedback")
        if fb.get("success"):
            st.toast(f"⚡ {fb.get('message')}", icon="✅")
            st.success(f"⚡ **Restored to Slot #1:** {fb.get('message')}")
        else:
            st.error(f"❌ **Restoration Failed:** {fb.get('error')}")

    # Fetch dynamic counts for filter dropdown options
    ret_counts = catalog.get_retention_counts()
    cat_counts = catalog.get_category_counts()
    ws_counts_data = catalog.get_workspace_counts()
    ws_count_map = {item["workspace"]: item["count"] for item in ws_counts_data}
    total_db_count = ret_counts.get("total", 0)

    # Filters Row
    col_ret, col_cat, col_ws = st.columns([1.2, 1.2, 1.6])
    with col_ret:
        ret_filter = st.selectbox(
            "Retention Status",
            ["All", "complete_active", "safeguarded_evicted", "imported_from_offsite", "metadata_only_evicted"],
            key="explorer_ret_filter",
            format_func=lambda x: {
                "All": f"All Retention States ({total_db_count:,})",
                "complete_active": f"🟢 Active in Antigravity ({ret_counts.get('complete_active', 0):,})",
                "safeguarded_evicted": f"🛡️ Safeguarded in Vault ({ret_counts.get('safeguarded_evicted', 0):,})",
                "imported_from_offsite": f"📥 Recovered from Offsite ({ret_counts.get('imported_from_offsite', 0):,})",
                "metadata_only_evicted": f"📋 Metadata Only (No Backup) ({ret_counts.get('metadata_only_evicted', 0):,})"
            }.get(x, x)
        )

    with col_cat:
        cat_filter = st.selectbox(
            "Category",
            ["All", "interactive", "automated_queue", "pinned"],
            key="explorer_cat_filter",
            format_func=lambda x: {
                "All": f"All Categories ({total_db_count:,})",
                "interactive": f"👤 Interactive Human ({cat_counts.get('interactive', 0):,})",
                "automated_queue": f"🤖 Automated Queue / Cron ({cat_counts.get('automated_queue', 0):,})",
                "pinned": f"⭐ Pinned Conversations ({cat_counts.get('pinned', 0):,})"
            }.get(x, x)
        )

    with col_ws:
        workspaces = ["All"] + [item["workspace"] for item in ws_counts_data]
        ws_filter = st.selectbox(
            "Workspace",
            workspaces,
            key="explorer_ws_filter",
            format_func=lambda x: f"All Workspaces ({total_db_count:,})" if x == "All" else f"{x.replace('file:///', '').replace('%3A', ':')} ({ws_count_map.get(x, 0):,})"
        )

    col_search, col_sort = st.columns([2.5, 1])
    with col_search:
        search_query = st.text_input("🔍 Search by Title, Conversation ID, or Notes", key="explorer_search_query", placeholder="e.g. Content Generator, Journal Abstract, UUID...")
    with col_sort:
        sort_choice = st.selectbox(
            "Sort Order",
            [
                ("last_modified_time DESC", "Date (Newest First)"),
                ("last_modified_time ASC", "Date (Oldest First)"),
                ("step_count DESC", "Steps (Highest First)"),
                ("title ASC", "Title (A-Z)")
            ],
            key="explorer_sort_choice",
            format_func=lambda x: x[1]
        )

    # Initialize pagination state
    if "explorer_page" not in st.session_state:
        st.session_state["explorer_page"] = 1
    if "explorer_page_size" not in st.session_state:
        st.session_state["explorer_page_size"] = 25

    # Reset page on filter changes
    current_filter_sig = f"{ret_filter}_{cat_filter}_{ws_filter}_{search_query.strip()}_{sort_choice[0]}"
    if st.session_state.get("last_explorer_filter_sig") != current_filter_sig:
        st.session_state["explorer_page"] = 1
        st.session_state["last_explorer_filter_sig"] = current_filter_sig

    # Query matching records count and calculate pagination
    total_matches = catalog.count_conversations(ret_filter, cat_filter, ws_filter, search_query)
    page_size = st.session_state["explorer_page_size"]
    total_pages = max(1, (total_matches + page_size - 1) // page_size)

    # Bound current_page within [1, total_pages]
    current_page = max(1, min(st.session_state["explorer_page"], total_pages))
    st.session_state["explorer_page"] = current_page

    offset = (current_page - 1) * page_size
    start_item = offset + 1 if total_matches > 0 else 0
    end_item = min(offset + page_size, total_matches)

    st.write(f"Showing **{start_item}–{end_item}** of **{total_matches:,}** matching conversations &nbsp;•&nbsp; Page **{current_page}** of **{total_pages}**")

    conversations = catalog.query_conversations(
        retention_filter=ret_filter,
        category_filter=cat_filter,
        workspace_filter=ws_filter,
        search_query=search_query,
        order_by=sort_choice[0],
        limit=page_size,
        offset=offset
    )

    if not conversations:
        st.info("No conversations match your filter or search criteria.")

    # Render Conversation Cards / List
    for conv in conversations:
        cid = conv["conversation_id"]
        raw_title = conv["title"]
        raw_preview = conv["preview"]
        title = format_display_title(raw_title, raw_preview)
        steps = conv["step_count"]
        lmt = conv["last_modified_time"]
        ws = conv["workspace_uris"] or ""
        ret_status = conv["retention_status"]
        category = conv["category"]
        is_pinned = bool(conv["is_pinned"])
        has_db = bool(conv["has_db_file"])
        is_evicted = bool(conv.get("is_evicted_from_live"))
        has_fallback_prompt = not (raw_title and raw_title.strip()) and bool(raw_preview and raw_preview.strip())

        # Clean workspace path
        ws_clean = ws.replace('["file:///', '').replace('"]', '').replace('%3A', ':') if ws else "Default"

        with st.container():
            c_main, c_actions = st.columns([3.5, 1.5])
            with c_main:
                # Badge row
                badges_html = ""
                if ret_status == "complete_active":
                    badges_html += '<span class="badge-active">🟢 Active in AGY</span> '
                elif ret_status == "safeguarded_evicted":
                    badges_html += '<span class="badge-safeguarded">🛡️ Safeguarded (Evicted)</span> '
                elif ret_status == "imported_from_offsite":
                    badges_html += '<span class="badge-offsite">📥 Offsite Restored</span> '
                else:
                    badges_html += '<span class="badge-evicted">📋 Metadata Only</span> '

                if is_pinned:
                    badges_html += '<span class="badge-pinned">⭐ Pinned</span> '
                if category == "automated_queue":
                    badges_html += '<span class="badge-queue">🤖 Queue</span> '
                elif category == "interactive":
                    badges_html += '<span class="badge-interactive">👤 Interactive</span> '

                st.markdown(f"### {title}")
                st.markdown(badges_html, unsafe_allow_html=True)
                st.caption(f"**ID**: `{cid}` | **Steps**: `{steps}` | **Modified**: `{format_local_timestamp(lmt)}` | **Workspace**: `{ws_clean}`")

                if has_fallback_prompt and len(raw_preview.strip()) > len(title):
                    with st.expander("💬 View Initial Prompt Details", expanded=False):
                        st.caption(raw_preview.strip())

                if conv.get("user_notes"):
                    st.info(f"📝 *Note*: {conv['user_notes']}")

            with c_actions:
                st.write("") # spacing
                col_btn1, col_btn2 = st.columns(2)

                with col_btn1:
                    # View button
                    st.button(
                        "📖 View",
                        key=f"view_{cid}",
                        width="stretch",
                        on_click=open_conversation_viewer,
                        args=(cid,)
                    )

                with col_btn2:
                    if not is_evicted and ret_status == "complete_active":
                        st.button(
                            "🟢 Active in AGY",
                            key=f"act_{cid}",
                            width="stretch",
                            disabled=True,
                            help="Session is already active in Antigravity"
                        )
                    elif has_db:
                        st.button(
                            "⚡ Restore to Live",
                            key=f"res_{cid}",
                            width="stretch",
                            help="Restore trajectory DB and brain folder into Antigravity sidebar",
                            on_click=execute_restore_callback,
                            args=(cid,)
                        )
                    else:
                        st.button(
                            "⚡ Restore",
                            key=f"res_{cid}",
                            width="stretch",
                            disabled=True,
                            help="Data evicted prior to backup. Import from offsite backup to restore."
                        )

            st.markdown("<hr class='vault-divider'>", unsafe_allow_html=True)

    # Pagination Controls at bottom
    if total_matches > 0:
        st.markdown("<br>", unsafe_allow_html=True)
        with st.container(border=True):
            col_page_status, col_first, col_prev, col_next, col_last, col_size = st.columns(
                [2.2, 0.9, 0.9, 0.9, 0.9, 1.4],
                vertical_alignment="center"
            )
            with col_page_status:
                st.caption(f"Page **{current_page}** of **{total_pages}** &nbsp;({total_matches:,} items)")

            with col_first:
                if st.button("⏮️ First", disabled=(current_page <= 1), key="btn_page_first", width="stretch"):
                    st.session_state["explorer_page"] = 1
                    st.rerun()

            with col_prev:
                if st.button("◀️ Prev", disabled=(current_page <= 1), key="btn_page_prev", width="stretch"):
                    st.session_state["explorer_page"] = max(1, current_page - 1)
                    st.rerun()

            with col_next:
                if st.button("Next ▶️", disabled=(current_page >= total_pages), key="btn_page_next", width="stretch"):
                    st.session_state["explorer_page"] = min(total_pages, current_page + 1)
                    st.rerun()

            with col_last:
                if st.button("Last ⏭️", disabled=(current_page >= total_pages), key="btn_page_last", width="stretch"):
                    st.session_state["explorer_page"] = total_pages
                    st.rerun()

            with col_size:
                page_size_options = [25, 50, 100]
                cur_size_idx = page_size_options.index(st.session_state["explorer_page_size"]) if st.session_state["explorer_page_size"] in page_size_options else 0
                new_page_size = st.selectbox(
                    "Page size",
                    options=page_size_options,
                    index=cur_size_idx,
                    key="select_page_size",
                    label_visibility="collapsed"
                )
                if new_page_size != st.session_state["explorer_page_size"]:
                    st.session_state["explorer_page_size"] = new_page_size
                    st.session_state["explorer_page"] = 1
                    st.rerun()

# -------------------------------------------------------------
# TAB 2: TRANSCRIPT & PLAN VIEWER
# -------------------------------------------------------------
with tab_viewer:
    selected_cid = st.session_state.get("selected_cid", None)

    # Fetch conversations for dropdown
    all_viewable = catalog.query_conversations(limit=1000, order_by="last_modified_time DESC")
    options_map = {
        c["conversation_id"]: f"{format_display_title(c['title'], c['preview'], max_len=60)} ({c['conversation_id'][:8]}...) - {c['retention_status']}"
        for c in all_viewable
    }

    # CRITICAL: Always ensure selected_cid is present in options_map so it is NEVER lost or overridden!
    if selected_cid and selected_cid not in options_map:
        sel_meta = catalog.get_conversation_by_id(selected_cid)
        if sel_meta:
            options_map = {
                selected_cid: f"{format_display_title(sel_meta.get('title'), sel_meta.get('preview'), max_len=60)} ({selected_cid[:8]}...) - {sel_meta.get('retention_status')}",
                **options_map
            }

    options_keys = list(options_map.keys())

    # Fall back to first available if selected_cid is missing
    if not selected_cid or selected_cid not in options_map:
        selected_cid = options_keys[0] if options_keys else None
        st.session_state["selected_cid"] = selected_cid

    col_select, col_refresh = st.columns([4, 1])
    with col_select:
        current_selection = st.selectbox(
            "Select Conversation to View",
            options=options_keys,
            index=options_keys.index(selected_cid) if (selected_cid and selected_cid in options_keys) else 0,
            format_func=lambda x: options_map.get(x, x)
        )
        if current_selection and current_selection != st.session_state.get("selected_cid"):
            st.session_state["selected_cid"] = current_selection
            st.rerun()

    with col_refresh:
        st.write("") # spacing
        if st.button("🔄 Refresh View", width="stretch"):
            st.rerun()

    if selected_cid:
        conv_meta = catalog.get_conversation_by_id(selected_cid)
        if conv_meta:
            raw_title = conv_meta.get("title")
            raw_preview = conv_meta.get("preview")
            title = format_display_title(raw_title, raw_preview)
            has_db = bool(conv_meta.get("has_db_file"))
            has_brain = bool(conv_meta.get("has_brain_folder"))
            is_evicted = bool(conv_meta.get("is_evicted_from_live"))
            ret_status = conv_meta.get("retention_status")

            # Header info
            st.markdown(f"## {title}")
            st.caption(f"**Conversation ID**: `{selected_cid}` | **Steps**: `{conv_meta.get('step_count')}` | **Last Modified**: `{format_local_timestamp(conv_meta.get('last_modified_time'))}`")

            if not raw_title and raw_preview and len(raw_preview.strip()) > len(title):
                with st.expander("💬 View Initial Prompt Details", expanded=False):
                    st.caption(raw_preview.strip())

            # Feedback banner if restored
            if "restore_action_feedback" in st.session_state:
                fb = st.session_state.pop("restore_action_feedback")
                if fb.get("already_active"):
                    st.info(f"ℹ️ {fb.get('message')}")
                elif fb.get("success"):
                    st.toast(f"⚡ {fb.get('message')}", icon="✅")
                    st.success(f"⚡ **Restored to Slot #1:** {fb.get('message')}")
                else:
                    st.error(f"❌ **Restoration Failed:** {fb.get('error')}")

            # Quick Actions for current conversation
            c_act1, c_act2, c_act3 = st.columns([1.5, 1.5, 2.5])
            with c_act1:
                if not is_evicted and ret_status == "complete_active":
                    st.button(
                        "🟢 Active in AGY",
                        key="viewer_act_btn",
                        width="stretch",
                        disabled=True,
                        help="Session is already active in Antigravity"
                    )
                elif has_db:
                    st.button(
                        "⚡ Restore to Sidebar #1",
                        key="viewer_restore_btn",
                        width="stretch",
                        help="Restore trajectory DB and brain folder into Antigravity sidebar",
                        on_click=execute_restore_callback,
                        args=(selected_cid,)
                    )
                else:
                    st.info("Files evicted. Load from offsite backup to restore.")

            with c_act2:
                # Toggle pinned
                curr_pinned = bool(conv_meta.get("is_pinned"))
                pin_label = "⭐ Unpin Session" if curr_pinned else "⭐ Pin Session"
                st.button(
                    pin_label,
                    key="viewer_pin_btn",
                    width="stretch",
                    on_click=toggle_pinned_callback,
                    args=(selected_cid, curr_pinned)
                )

            with c_act3:
                # Parse known workspace for autopopulation
                known_ws = ""
                raw_ws = conv_meta.get("workspace_uris")
                if raw_ws:
                    try:
                        parsed_ws = json.loads(raw_ws)
                        if isinstance(parsed_ws, list) and len(parsed_ws) > 0:
                            known_ws = str(parsed_ws[0])
                        elif isinstance(parsed_ws, str):
                            known_ws = str(parsed_ws)
                    except Exception:
                        known_ws = str(raw_ws).strip('[]"\'')

                if is_evicted and has_db:
                    all_ws = [w["workspace"] for w in catalog.get_workspace_counts() if w.get("workspace")]
                    if known_ws and known_ws not in all_ws:
                        all_ws = [known_ws] + all_ws

                    if known_ws:
                        ws_options = [known_ws] + [w for w in all_ws if w != known_ws]
                    else:
                        ws_options = ["(None / Default)"] + all_ws

                    st.selectbox(
                        "Target Workspace",
                        options=ws_options,
                        index=0,
                        key="viewer_target_ws",
                        help="Autopopulated with the session's recorded workspace. You can reassign to an active project workspace."
                    )
                elif not is_evicted:
                    ws_display = known_ws.replace('file:///', '').replace('%3A', ':') if known_ws else "Default"
                    st.caption(f"**Workspace**: `{ws_display}`")

            st.divider()

            # Paths to search for data
            backup_db = BACKUP_CONVERSATIONS_DIR / f"{selected_cid}.db"
            backup_brain = BACKUP_BRAIN_DIR / selected_cid

            subtab_plans, subtab_timeline = st.tabs(["📜 Extracted Plans & Files", "💬 Interactive Chat Timeline"])

            # SUBTAB A: EXTRACTED PLANS & FILES
            with subtab_plans:
                st.subheader("Plans & Files Generated via `write_to_file`")
                files = extract_file_payloads(db_path=backup_db, brain_dir=backup_brain)
                if files:
                        st.write(f"Extracted **{len(files)}** files generated or modified during this session:")

                        # File selection tabs
                        file_names = [f["filename"] for f in files]
                        selected_file_name = st.selectbox("Select Generated File / Plan", file_names)

                        selected_file = next((f for f in files if f["filename"] == selected_file_name), None)
                        if selected_file:
                            st.markdown(f"#### `{selected_file['filename']}`")
                            st.caption(f"Target Path: `{selected_file['target_path']}` | Size: `{selected_file['size_bytes']} bytes` | Written at Step: `{selected_file['step_index']}`")
                            if selected_file.get("summary"):
                                st.info(f"**Artifact Summary**: {selected_file['summary']}")

                            # Display content
                            content = selected_file["content"]
                            if selected_file["filename"].endswith(".md"):
                                st.markdown("---")
                                st.markdown(content)
                            else:
                                ext = selected_file["filename"].split(".")[-1] if "." in selected_file["filename"] else "text"
                                st.code(content, language=ext)

                            st.download_button(
                                label=f"⬇️ Download {selected_file['filename']}",
                                data=content,
                                file_name=selected_file['filename'],
                                mime="text/plain"
                            )
                elif backup_db.exists() or backup_brain.exists():
                    st.info("No file write payloads found in this session.")
                else:
                    st.warning("Trajectory database file (.db) and brain folder are not present for this session. (Metadata only record).")

            # SUBTAB B: CHAT TIMELINE
            with subtab_timeline:
                st.subheader("Full Chronological Chat History")
                timeline = extract_chat_timeline(
                    db_path=backup_db if backup_db.exists() else None,
                    brain_dir=backup_brain if backup_brain.exists() else None
                )

                if timeline:
                    st.write(f"Total Steps Extracted: **{len(timeline)}**")
                    st.markdown('<div class="chat-timeline-wrapper">', unsafe_allow_html=True)
                    for step in timeline:
                        s_idx = step["step_index"]
                        sender = step["sender"]
                        content = step.get("content", "")
                        thinking = step.get("thinking", "")
                        tool_calls = step.get("tool_calls", [])

                        if sender == "User":
                            st.markdown(f"""
                            <div class="chat-bubble-user">
                                <strong>👤 User</strong> (Step {s_idx})<br>
                                <div style="margin-top: 4px;">{content}</div>
                            </div>
                            """, unsafe_allow_html=True)
                        elif sender in ("Tool Execution", "System / Tool Result"):
                            # Check if tool result is a file edit diff
                            if "[diff_block_start]" in content:
                                parts = content.split("[diff_block_start]")
                                tool_desc = parts[0].strip()
                                diff_text = "[diff_block_start]" + parts[1]
                                st.markdown(f"""
                                <div class="chat-bubble-tool">
                                    <strong>⚙️ Tool Execution Result</strong> (Step {s_idx})<br>
                                    <span style="color: #64748b; font-size: 0.85rem;">{tool_desc}</span>
                                </div>
                                """, unsafe_allow_html=True)
                                with st.expander("📄 View File Edit Diff", expanded=False):
                                    st.code(diff_text, language="diff")
                            else:
                                st.markdown(f"""
                                <div class="chat-bubble-tool">
                                    <strong>⚙️ Tool Result</strong> (Step {s_idx})<br>
                                    <div style="margin-top: 4px; font-size: 0.88rem; line-height: 1.4;">{content}</div>
                                </div>
                                """, unsafe_allow_html=True)
                        else:
                            st.markdown(f"""
                            <div class="chat-bubble-assistant">
                                <strong>🤖 Antigravity Agent</strong> (Step {s_idx})
                            </div>
                            """, unsafe_allow_html=True)

                            if thinking:
                                with st.expander("💭 Agent Chain-of-Thought / Reasoning", expanded=False):
                                    st.markdown(thinking)

                            if content:
                                if "[diff_block_start]" in content:
                                    parts = content.split("[diff_block_start]")
                                    tool_desc = parts[0].strip()
                                    diff_text = "[diff_block_start]" + parts[1]
                                    if tool_desc:
                                        st.markdown(tool_desc)
                                    with st.expander("📄 View File Edit Diff", expanded=False):
                                        st.code(diff_text, language="diff")
                                else:
                                    st.markdown(content)

                            if tool_calls:
                                with st.expander(f"🛠️ Tool Invocations ({len(tool_calls)})", expanded=False):
                                    st.json(tool_calls)
                    st.markdown('</div>', unsafe_allow_html=True)
                else:
                    st.info("No message timeline available for this conversation (session metadata only).")

# -------------------------------------------------------------
# TAB 3: GLOBAL FULL-TEXT SEARCH
# -------------------------------------------------------------
with tab_search:
    st.subheader("Search Across All Transcripts and Generated Files")
    st.caption("Search through titles, user prompts, assistant answers, and generated plans/deliverables with instant context highlighting.")

    # Form to batch search inputs and avoid keystroke reruns
    with st.form("global_search_form"):
        search_kw = st.text_input(
            "Enter Search Term",
            value=st.session_state.get("global_search_kw", ""),
            placeholder="e.g. architecture, implementation_plan, refactor, error, database..."
        )

        scope_choice = st.radio(
            "Search Scope",
            ["All Content", "Generated Plans & Files Only", "Transcripts & Prompts Only"],
            horizontal=True,
            index=0
        )

        c_ws, c_lim = st.columns([3, 1])
        with c_ws:
            ws_counts = catalog.get_workspace_counts()
            ws_choices = ["All Workspaces"] + [w["workspace"] for w in ws_counts]
            selected_ws = st.selectbox(
                "Filter by Workspace",
                ws_choices,
                format_func=lambda x: "All Workspaces" if x == "All Workspaces" else f"{x.replace('file:///', '').replace('%3A', ':')}"
            )
        with c_lim:
            limit_choice = st.selectbox("Max Matches", [25, 50, 100], index=0)

        search_submitted = st.form_submit_button("🔍 Search", type="primary")

    if search_submitted:
        st.session_state["global_search_kw"] = search_kw.strip()
        st.session_state["global_search_scope"] = scope_choice
        st.session_state["global_search_ws"] = selected_ws
        st.session_state["global_search_limit"] = limit_choice

        if len(search_kw.strip()) < 2:
            st.warning("Please enter at least 2 characters to search.")
            st.session_state["global_search_results"] = None
        else:
            with st.spinner("Searching transcripts and artifacts across backups..."):
                search_data = search_backup_conversations(
                    query=search_kw.strip(),
                    catalog=catalog,
                    scope=scope_choice,
                    workspace_filter=selected_ws if selected_ws != "All Workspaces" else None,
                    max_results=limit_choice
                )
                st.session_state["global_search_results"] = search_data

    # Render results from session state
    search_data = st.session_state.get("global_search_results")
    if search_data:
        res_list = search_data.get("results", [])
        scanned = search_data.get("scanned_count", 0)
        elapsed = search_data.get("elapsed_time", 0.0)
        stopped_early = search_data.get("stopped_early", False)

        if res_list:
            st.success(f"Found **{len(res_list)}** matching conversations (scanned **{scanned}** backups in **{elapsed:.2f}s**)")
            if stopped_early:
                st.info(f"⚡ Search reached your limit of **{limit_choice}** matching conversations and stopped early. Narrow by workspace or increase the match limit to search further.")

            for conv in res_list:
                cid = conv["conversation_id"]
                title = conv["title"]
                ws = conv["workspace"]
                date = conv["date"]
                ret_status = conv["retention_status"]
                cat = conv["category"]
                hits = conv.get("hits", [])

                with st.container(border=True):
                    c_title, c_btn = st.columns([3.8, 1.2])
                    with c_title:
                        # Badges
                        b_html = ""
                        if ret_status == "complete_active":
                            b_html += '<span class="badge-active">🟢 Active in AGY</span> '
                        elif ret_status == "safeguarded_evicted":
                            b_html += '<span class="badge-safeguarded">🛡️ Safeguarded (Evicted)</span> '
                        elif ret_status == "imported_from_offsite":
                            b_html += '<span class="badge-offsite">📥 Offsite Restored</span> '
                        else:
                            b_html += '<span class="badge-evicted">📋 Metadata Only</span> '

                        if cat == "automated_queue":
                            b_html += '<span class="badge-queue">🤖 Queue</span> '
                        elif cat == "interactive":
                            b_html += '<span class="badge-interactive">👤 Interactive</span> '

                        st.markdown(f"### {title}")
                        st.markdown(b_html, unsafe_allow_html=True)
                        st.caption(f"📁 **Workspace**: `{ws}` &nbsp;|&nbsp; 📅 **Date**: `{format_local_timestamp(date)}` &nbsp;|&nbsp; **ID**: `{cid}`")
                    with c_btn:
                        st.write("")
                        st.button(
                            "📖 View Match",
                            key=f"search_view_{cid}",
                            width="stretch",
                            on_click=open_conversation_viewer,
                            args=(cid,)
                        )

                    # Highlighted snippet hits
                    if hits:
                        st.markdown(f"**Matching Highlights ({len(hits)} occurrences):**")
                        # Top 3 hits
                        for h in hits[:3]:
                            st.markdown(
                                f"- **{h['label']}**<br>"
                                f"<div class='search-snippet-hit'>"
                                f"{h['snippet']}"
                                f"</div>",
                                unsafe_allow_html=True
                            )

                        # If more than 3 hits, show in expander
                        if len(hits) > 3:
                            with st.expander(f"Show {len(hits) - 3} more matching occurrences in this conversation...", expanded=False):
                                for h in hits[3:]:
                                    st.markdown(
                                        f"- **{h['label']}**<br>"
                                        f"<div class='search-snippet-hit'>"
                                        f"{h['snippet']}"
                                        f"</div>",
                                        unsafe_allow_html=True
                                    )
        else:
            st.info("No matching content found for this query in the selected scope.")

# -------------------------------------------------------------
# TAB 4: OFFSITE BACKUP IMPORTER
# -------------------------------------------------------------
with tab_importer:
    evicted_in_catalog = catalog.get_summary_metrics().get("metadata_only_evicted", 0)
    st.subheader("📥 Offsite / Nightly Backup Importer")
    st.markdown(f"""
    If you have offsite nightly backups, external drives, or archived folders containing Antigravity `conversations/` (`*.db`) 
    or `brain/` folders, you can import them here. The dashboard automatically matches them against your 
    **{evicted_in_catalog:,} evicted sessions**, upgrades their status to **Complete**, and restores their full chat transcripts, 
    code diffs, and generated plans!
    """)

    # Persistent source path via session state
    if "importer_source_input" not in st.session_state:
        st.session_state["importer_source_input"] = ""

    col_inp, col_btn = st.columns([4, 1.2])
    with col_inp:
        source_input = st.text_input(
            "Path to Offsite Backup Folder",
            value=st.session_state["importer_source_input"],
            placeholder=r"e.g. D:\Acronis\antigravity-2026-09-20 or E:\Backups\conversations",
            help="Provide the root directory containing recovered *.db files or brain subfolders."
        )
        st.session_state["importer_source_input"] = source_input
    with col_btn:
        st.write("")
        scan_clicked = st.button("🔍 Scan Source Folder", width="stretch", type="primary")

    if scan_clicked:
        if not source_input.strip():
            st.warning("Please provide a folder path to scan.")
        else:
            p = Path(source_input.strip())
            with st.spinner(f"Scanning '{p}' for recoverable Antigravity conversations..."):
                scan_res = scan_offsite_source(p)
                if not scan_res.get("valid"):
                    st.error(scan_res.get("error"))
                else:
                    st.session_state["scan_result"] = scan_res
                    if "import_receipt" in st.session_state:
                        del st.session_state["import_receipt"]
                    st.success(f"Scan complete! Discovered {scan_res['total_discovered']:,} conversation files.")

    # Render Ingestion Receipt if recently executed
    if "import_receipt" in st.session_state:
        receipt = st.session_state["import_receipt"]
        with st.container(border=True):
            st.markdown("### 🎉 Ingestion Completed Successfully!")
            c_r1, c_r2, c_r3, c_r4 = st.columns(4)
            c_r1.metric("Evicted Sessions Upgraded", f"{receipt['upgraded_records']:,}")
            c_r2.metric("New Records Added", f"{receipt['new_records']:,}")
            c_r3.metric("Databases Restored", f"{receipt['imported_dbs']:,}")
            c_r4.metric("Brain Folders Synced", f"{receipt['imported_brains']:,}")

            if receipt.get("failed_items"):
                st.warning(f"{len(receipt['failed_items'])} items encountered notices during ingestion.")
                with st.expander("View Details of Failed Items"):
                    for f_it in receipt["failed_items"]:
                        st.write(f"- `{f_it['conversation_id']}`: {f_it['error']}")

            c_b1, c_b2 = st.columns(2)
            with c_b1:
                st.button(
                    "📂 View Upgraded Sessions in Explorer",
                    key="btn_view_upgraded_explorer",
                    width="stretch",
                    on_click=navigate_to_explorer
                )
            with c_b2:
                st.button(
                    "✖️ Dismiss Receipt",
                    key="btn_dismiss_receipt",
                    width="stretch",
                    on_click=dismiss_import_receipt
                )

    scan_res = st.session_state.get("scan_result", None)
    if scan_res and scan_res.get("valid"):
        st.divider()

        # 1. Fully Reconciled KPI Metrics (100% of discovered sessions accounted for with delta verification)
        col_m1, col_m2, col_m3, col_m4 = st.columns(4)
        col_m1.metric("Total Discovered in Backup", f"{scan_res['total_discovered']:,}")
        col_m2.metric(
            "Actionable Evicted Upgrades",
            f"{scan_res['matching_evicted']:,}",
            delta=f"{scan_res['matching_evicted']} Ready to Ingest" if scan_res['matching_evicted'] > 0 else None
        )
        newer_cnt = scan_res.get("newer_content", 0) + scan_res.get("missing_assets", 0)
        col_m3.metric(
            "Brand New / Updated Sessions",
            f"{(scan_res['brand_new'] + newer_cnt):,}",
            delta=f"{newer_cnt} Updates Available" if newer_cnt > 0 else (f"{scan_res['brand_new']} New" if scan_res['brand_new'] > 0 else None)
        )
        col_m4.metric(
            "Verified Identical (Up-to-Date)",
            f"{scan_res['already_archived']:,}",
            delta="Verified Byte-Identical" if scan_res['already_archived'] > 0 else None,
            delta_color="off"
        )

        # 2. Scope Clarity & Explanatory Callout
        actionable_count = scan_res.get("actionable_count", 0)
        items = scan_res.get("items", [])

        with st.container(border=True):
            if actionable_count > 0:
                st.markdown(f"""
                **📊 Ingestion Scope & Delta Verification:**
                - **{scan_res['matching_evicted']:,} sessions** match evicted catalog records and will be upgraded from metadata stubs to **Complete fidelity** (chat transcripts, code diffs, and text plans will become immediately viewable).
                - **{scan_res.get('newer_content', 0):,} sessions** have newer turns, larger databases, or modified transcripts in the backup than the current vault copy.
                - **{scan_res['brand_new']:,} sessions** are brand new uncatalogued conversations.
                - **{scan_res['already_archived']:,} sessions** are **verified byte-identical** to the vault copy (same SQLite database size, modification timestamps, and transcript length).
                
                💡 **Delta Verification**: Every session was individually verified against archive file sizes and timestamps. Clicking **"Ingest Actionable Sessions"** will safely process only the **{actionable_count:,} sessions** that need restoration or updates, skipping the **{scan_res['already_archived']:,} sessions** verified to be identical.
                """)
            else:
                st.success(f"""
                ✅ **All {scan_res['total_discovered']:,} discovered sessions are verified byte-identical and up-to-date in your vault!**
                File sizes, timestamps, and step counts match the archive. There are no evicted or modified sessions to ingest.
                """)

        # 3. Interactive Filter & Table Controls
        st.write("### Discovered Sessions Breakdown")
        col_filt, col_search_in_table = st.columns([2.7, 1.3])

        filter_keys = ["actionable", "all", "identical"]
        newer_cnt = scan_res.get("newer_content", 0) + scan_res.get("missing_assets", 0) + scan_res.get("brand_new", 0)
        if newer_cnt > 0 and scan_res.get("matching_evicted", 0) > 0:
            filter_keys.append("updates")

        filter_labels = {
            "actionable": f"🎯 Actionable Only ({actionable_count:,})",
            "all": f"📋 All Discovered ({scan_res['total_discovered']:,})",
            "identical": f"✅ Verified Identical ({scan_res['already_archived']:,})",
            "updates": f"⚡ Updates / New ({newer_cnt:,})"
        }

        default_key = "actionable" if actionable_count > 0 else "all"

        with col_filt:
            selected_filter = st.segmented_control(
                "Filter View",
                filter_keys,
                format_func=lambda k: filter_labels.get(k, k),
                default=default_key,
                key="importer_segmented_filter_view",
                label_visibility="collapsed"
            )

        with col_search_in_table:
            table_search = st.text_input(
                "Filter by title or ID",
                placeholder="🔍 Search title, ID, workspace...",
                key="importer_table_search_input",
                label_visibility="collapsed"
            )

        # Active filter fallback if user clicks the active option to toggle off
        active_filter = selected_filter or default_key

        # Apply Table Filtering strictly by key
        if active_filter == "actionable":
            filtered_items = [it for it in items if it.get("is_actionable")]
        elif active_filter == "identical":
            filtered_items = [it for it in items if not it.get("is_actionable")]
        elif active_filter == "updates":
            filtered_items = [it for it in items if it.get("action_type") in ("UPDATE_NEWER_DATA", "ENRICH_ASSET", "NEW_SESSION")]
        else: # "all"
            filtered_items = items

        if table_search and table_search.strip():
            kw = table_search.strip().lower()
            filtered_items = [
                it for it in filtered_items
                if kw in it.get("title", "").lower() 
                or kw in it.get("conversation_id", "").lower() 
                or kw in it.get("workspace", "").lower()
                or kw in it.get("action_label", "").lower()
                or kw in it.get("status_label", "").lower()
            ]

        st.caption(f"Displaying **{len(filtered_items):,}** of **{len(items):,}** discovered sessions ({filter_labels.get(active_filter, active_filter)})")

        if filtered_items:
            df_preview = pd.DataFrame([
                {
                    "Action": it["action_label"],
                    "Title": format_display_title(it.get("title"), None),
                    "DB": "✅" if it["has_db"] else "❌",
                    "Brain": "✅" if it["has_brain"] else "❌",
                    "Current Vault Status": it["archive_retention"],
                    "Conversation ID": it["conversation_id"],
                }
                for it in filtered_items
            ])
            st.dataframe(
                df_preview,
                width="stretch",
                column_config={
                    "Action": st.column_config.TextColumn("Planned Action", width="medium"),
                    "Title": st.column_config.TextColumn("Session Title", width="large"),
                    "DB": st.column_config.TextColumn("DB", width="small"),
                    "Brain": st.column_config.TextColumn("Brain", width="small"),
                    "Current Vault Status": st.column_config.TextColumn("Current Vault Status", width="medium"),
                    "Conversation ID": st.column_config.TextColumn("Conversation ID", width="large"),
                },
                hide_index=True,
                key=f"discovered_df_{active_filter}"
            )
        else:
            st.info(f"No sessions match the filter '{filter_labels.get(active_filter, active_filter)}'.")


        # 4. Ingestion Action Execution
        st.write("")
        if actionable_count > 0:
            if st.button(
                f"🚀 Ingest {actionable_count} Actionable Sessions (Recommended)",
                type="primary",
                width="stretch"
            ):
                target_items = [it for it in items if it.get("is_actionable")]
                prog_bar = st.progress(0.0)
                status_txt = st.empty()

                def update_import_progress(current, total, cid, title):
                    pct = min(1.0, current / max(1, total))
                    prog_bar.progress(pct)
                    clean_title = title[:45] + "..." if len(title) > 45 else (title or cid[:8])
                    status_txt.markdown(f"**Ingesting ({current}/{total}):** `{clean_title}`")

                import_res = execute_import(target_items, progress_callback=update_import_progress)
                prog_bar.progress(1.0)
                status_txt.empty()

                st.session_state["import_receipt"] = import_res
                # Re-scan to refresh live metrics
                if source_input.strip():
                    st.session_state["scan_result"] = scan_offsite_source(Path(source_input.strip()))
                st.rerun()

            st.caption(
                f"💡 **Safety Guarantee**: Only ingests the **{actionable_count}** actionable sessions. "
                f"Skips all **{scan_res['already_archived']}** sessions that already have full fidelity in your vault."
            )

        # Advanced Disaster Recovery Option
        with st.expander("⚙️ Advanced Ingestion Options (Force Re-sync / Full Overwrite)", expanded=False):
            st.markdown(f"""
            **Force Full Re-sync**: This will re-copy all **{len(items):,}** sessions from the backup source, overwriting 
            existing database files and re-syncing brain directories even if they are already archived.
            Use this only if you suspect local archive files are corrupted or you need a complete disaster recovery re-import.
            """)
            if st.button(f"🔄 Force Re-import All {len(items):,} Discovered Sessions", type="secondary", width="stretch"):
                prog_bar = st.progress(0.0)
                status_txt = st.empty()

                def update_force_progress(current, total, cid, title):
                    pct = min(1.0, current / max(1, total))
                    prog_bar.progress(pct)
                    clean_title = title[:45] + "..." if len(title) > 45 else (title or cid[:8])
                    status_txt.markdown(f"**Force Ingesting ({current}/{total}):** `{clean_title}`")

                import_res = execute_import(items, progress_callback=update_force_progress)
                prog_bar.progress(1.0)
                status_txt.empty()

                st.session_state["import_receipt"] = import_res
                if source_input.strip():
                    st.session_state["scan_result"] = scan_offsite_source(Path(source_input.strip()))
                st.rerun()


# -------------------------------------------------------------
# TAB 5: HEALTH & SETTINGS
# -------------------------------------------------------------
with tab_settings:
    st.subheader("System Health & Storage Settings")

    col_h1, col_h2 = st.columns(2)
    with col_h1:
        st.markdown("### 💾 Storage Allocation")
        st.write(f"**Backup Archive Location**: `{BACKUP_BASE_DIR}`")
        st.write(f"**Live Antigravity Location**: `{LIVE_BASE_DIR}`")

        # Disk space check
        import shutil as sys_shutil
        d_usage = sys_shutil.disk_usage(BACKUP_BASE_DIR.anchor or "D:")
        free_gb = d_usage.free / (1024**3)
        total_gb = d_usage.total / (1024**3)
        st.progress(min(1.0, (total_gb - free_gb) / total_gb))
        st.caption(f"Drive Space: {free_gb:.1f} GB Free of {total_gb:.1f} GB Total")

    with col_h2:
        st.markdown("### 🔄 Daemon Service Setup")
        d_status, d_detail, _ = get_daemon_live_status()
        st.write(f"**Daemon Status**: {d_status} &nbsp;(`{d_detail}`)")

        col_act1, col_act2 = st.columns(2)
        with col_act1:
            if d_status.startswith("🟢"):
                if st.button("⏹️ Stop Daemon Process", key="tab5_stop_daemon_btn", width="stretch"):
                    stop_daemon_process()
                    st.rerun()
            else:
                if st.button("▶️ Start Background Daemon", key="tab5_start_daemon_btn", width="stretch"):
                    ok, res = start_daemon_process(30)
                    if not ok:
                        st.error(f"Failed to start daemon: {res}")
                    else:
                        st.rerun()
        with col_act2:
            if st.button("🔄 Restart Daemon", key="tab5_restart_daemon_btn", width="stretch"):
                stop_daemon_process()
                time.sleep(0.5)
                ok, res = start_daemon_process(30)
                if not ok:
                    st.error(f"Failed to restart daemon: {res}")
                else:
                    st.rerun()

        st.caption("The daemon runs autonomously every 30s in the background, executing continuous backup sweeps and Pin Sentry defense.")

    st.divider()

    st.markdown("### 🛡️ Vault Pin Sentry (Anti-Re-Eviction Defense)")
    col_ps1, col_ps2 = st.columns([2, 1.2])
    with col_ps1:
        st.markdown("""
        **Continuous Pinned Session Immunity**:
        Antigravity's internal language server process evicts sessions beyond ~500 based purely on `last_modified_time`,
        ignoring the `pinned` flag. In environments with autonomous queue tasks, 100–140 sessions are generated daily,
        causing earlier pinned conversations to be purged every 3.5 days.
        
        The **Vault Pin Sentry** provides dual-layer immunity:
        1. **Preventive Keep-Alive**: Automatically refreshes the timestamp and file metadata of active pinned sessions before they sink past rank #350.
        2. **Self-Healing Auto-Resurrection**: Instantly detects if Antigravity has purged a pinned session and restores its trajectory `.db` and `brain/` folder from the Vault.
        """)
    with col_ps2:
        pinned_count = metrics.get('pinned', 11)
        with sqlite3.connect(BACKUP_CATALOG_DB) as c_conn:
            c_cur = c_conn.cursor()
            c_cur.execute("SELECT COUNT(*) FROM backed_up_conversations WHERE is_pinned = 1 AND is_evicted_from_live = 0 AND retention_status = 'complete_active'")
            pinned_live_count = c_cur.fetchone()[0]

        st.metric("⭐ Protected Pinned Sessions", f"{pinned_count}")
        st.metric("🟢 Active in Antigravity", f"{pinned_live_count} / {pinned_count}")
        if st.button("🛡️ Run Pin Sentry Protection Now", key="btn_run_sentry_now", width="stretch"):
            with st.spinner("Executing Pin Sentry protection..."):
                s_res = run_pin_sentry()
                resurrected = s_res.get("auto_resurrected", 0)
                refreshed = s_res.get("refreshed", 0)
                msg = f"🛡️ Pin Sentry verified {s_res.get('protected_count', 0)} pinned sessions!"
                if resurrected > 0:
                    msg += f" Auto-resurrected {resurrected} to Live."
                elif refreshed > 0:
                    msg += f" Refreshed {refreshed} keep-alive timestamps."
                else:
                    msg += " All pinned sessions are active and healthy."
                st.toast(msg, icon="✅")
                st.success(msg)
                st.rerun()

    st.divider()
    st.markdown("### 📤 Export Catalog Data")
    if st.button("Export Full Catalog to JSON"):
        all_records = catalog.query_conversations(limit=5000)
        json_str = json.dumps(all_records, indent=2, default=str)
        st.download_button(
            label="⬇️ Download Catalog JSON",
            data=json_str,
            file_name=f"antigravity_catalog_export_{datetime.now().strftime('%Y%m%d')}.json",
            mime="application/json"
        )
