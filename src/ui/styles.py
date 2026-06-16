"""Global design system — a single, polished, professional look for the app.

This module is purely cosmetic.  It exposes:

  * ``COLORS``      — design tokens reused by the custom-HTML cards and the
                      Altair chart palettes, so chrome and data stay in sync.
  * ``PIE_PALETTE`` — a harmonious categorical palette for the Coverage charts.
  * ``inject()``    — one call, at the very top of ``app.main()``, that injects
                      the global CSS.  Idempotent and additive: it only sets
                      colours, type, spacing, radius and shadow — it never hides
                      content, never repositions elements, and never touches
                      icon fonts.  Functionality is unaffected.

Design language
───────────────
  * Typeface: Inter (graceful fallback to the system sans stack).
  * Brand: a confident indigo-blue (#2E5BFF) for chrome / focus / accents.
  * Neutrals: a cool slate ramp for crisp text and soft, low-contrast borders.
  * Surfaces: white cards on a faint canvas, 14px radius, soft layered shadows.
  * Data: blue / amber / slate for device series; a 12-step categorical palette
    for area breakdowns — distinct from the brand so encodings never read as UI.
"""
from __future__ import annotations

import streamlit as st

# ── design tokens ─────────────────────────────────────────────────────────────
COLORS: dict[str, str] = {
    # brand
    "brand":        "#2E5BFF",
    "brand_strong": "#1E40FF",
    "brand_soft":   "#EAEEFF",
    # neutrals (cool slate ramp)
    "ink":      "#0F172A",   # strongest text / headings
    "text":     "#334155",   # body text
    "muted":    "#64748B",   # secondary text / captions
    "faint":    "#94A3B8",   # tertiary / hints
    "border":   "#E6EAF1",   # hairline borders
    "border_2": "#D7DEEA",   # stronger borders
    "surface":  "#FFFFFF",   # card background
    "canvas":   "#F7F9FC",   # page background tint
    "grid":     "#EEF2F8",   # chart gridlines
    # device series (chart encodings)
    "mobile":      "#3B82F6",
    "desktop":     "#F59E0B",
    "unspecified": "#94A3B8",
    # semantic
    "success": "#16A34A",
    "warning": "#F59E0B",
    "danger":  "#DC2626",
    # soft card tints
    "java_bg":   "#FEF6E7",
    "testim_bg": "#EAEEFF",
}

# Categorical palette for area/section breakdowns (pie + bars).  Vivid yet
# harmonious and distinct from the indigo brand.  Ordered so CONSECUTIVE entries
# differ in both hue and luminance — pie/bar slices are coloured by rank order,
# so neighbours stay distinguishable (the pie also draws a surface-coloured
# stroke between slices for extra separation).
PIE_PALETTE: list[str] = [
    "#2E5BFF", "#F59E0B", "#16A34A", "#8B5CF6", "#06B6D4", "#EC4899",
    "#64748B", "#EF4444", "#14B8A6", "#6366F1", "#F97316", "#84CC16",
]


# ── global CSS ────────────────────────────────────────────────────────────────
def _css() -> str:
    c = COLORS
    return f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');

/* ── Typeface ──────────────────────────────────────────────────────────────
   Applied to safe text-bearing selectors only — never bare <span>/<i>, so
   Streamlit's Material icon glyphs keep their own font. */
html, body, .stApp {{
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
}}
[data-testid="stMarkdownContainer"], [data-testid="stMarkdownContainer"] *:not(code):not(pre),
[data-testid="stMetricValue"], [data-testid="stMetricLabel"],
[data-testid="stWidgetLabel"] *, .stButton button p, .stButton button div,
[data-baseweb="tab"] [data-testid="stMarkdownContainer"],
h1, h2, h3, h4, h5, h6 {{
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif !important;
}}

/* ── Canvas & layout ──────────────────────────────────────────────────────── */
.stApp {{ background: {c['canvas']}; }}
[data-testid="stHeader"] {{ background: transparent; }}
.block-container {{
    max-width: 1440px;
    padding-top: 2.4rem;
    padding-bottom: 4rem;
}}

/* ── Headings ─────────────────────────────────────────────────────────────── */
h1, h2, h3, h4, h5, h6 {{
    color: {c['ink']};
    letter-spacing: -0.018em;
    font-weight: 700;
}}
h1 {{ font-weight: 800; letter-spacing: -0.03em; }}

/* Body text + captions */
[data-testid="stMarkdownContainer"] p, [data-testid="stMarkdownContainer"] li {{
    color: {c['text']};
}}
[data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] * {{
    color: {c['muted']} !important;
}}

/* ── Tabs — clean underline navigation ────────────────────────────────────── */
/* Hide Streamlit's native grey baseline and draw a single one on the tab-list
   itself, so the coloured active-highlight sits flush on the same line. */
[data-baseweb="tab-list"] {{
    gap: 2px;
    margin-bottom: 8px;
    border-bottom: 1px solid {c['border']};
}}
[data-baseweb="tab-border"] {{
    display: none !important;
}}
[data-baseweb="tab"] {{
    border-radius: 9px 9px 0 0;
    padding: 9px 16px;
    color: {c['muted']};
    font-weight: 600;
    font-size: 14px;
    transition: color .15s ease, background .15s ease;
}}
[data-baseweb="tab"]:hover {{
    color: {c['ink']};
    background: {c['brand_soft']};
}}
[data-baseweb="tab"][aria-selected="true"] {{
    color: {c['brand']};
}}
[data-baseweb="tab-highlight"] {{
    background-color: {c['brand']} !important;
    height: 3px !important;
    border-radius: 3px 3px 0 0;
}}

/* ── Buttons ──────────────────────────────────────────────────────────────── */
.stButton > button, .stDownloadButton > button, [data-testid="stFormSubmitButton"] button {{
    border-radius: 10px;
    font-weight: 600;
    border: 1px solid {c['border_2']};
    background: {c['surface']};
    color: {c['ink']};
    box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
    transition: border-color .15s ease, box-shadow .15s ease, transform .12s ease, background .15s ease;
}}
.stButton > button:hover, .stDownloadButton > button:hover, [data-testid="stFormSubmitButton"] button:hover {{
    border-color: {c['brand']};
    color: {c['brand']};
    box-shadow: 0 3px 10px rgba(46, 91, 255, 0.14);
    transform: translateY(-1px);
}}
.stButton > button:active, [data-testid="stFormSubmitButton"] button:active {{
    transform: translateY(0);
}}
/* Primary-kind buttons keep the brand fill */
.stButton > button[kind="primary"] {{
    background: {c['brand']};
    border-color: {c['brand']};
    color: #fff;
}}
.stButton > button[kind="primary"]:hover {{
    background: {c['brand_strong']};
    border-color: {c['brand_strong']};
    color: #fff;
}}

/* ── AI-assistant suggestion chips ────────────────────────────────────────── */
/* Each chip carries a unique `st-key-ai_sugg_*` class, so this styling is fully
   self-contained per chip — hover can never bleed across siblings.  A soft
   tinted card with a left-aligned label and a clear brand hover. */
[class*="st-key-ai_sugg_"] button {{
    background: {c['brand_soft']} !important;
    border: 1px solid {c['border']} !important;
    border-radius: 12px !important;
    color: {c['ink']} !important;
    font-weight: 600 !important;
    font-size: 13px !important;
    line-height: 1.25 !important;
    text-align: left !important;
    justify-content: flex-start !important;
    align-items: center !important;     /* uniform vertical centring */
    white-space: normal !important;     /* wrap instead of clipping */
    min-height: 52px !important;        /* all four chips the same height */
    padding: 10px 14px !important;
    box-shadow: none !important;
    transition: background .15s ease, border-color .15s ease, color .15s ease, box-shadow .15s ease !important;
}}
[class*="st-key-ai_sugg_"] button:hover {{
    background: #fff !important;
    border-color: {c['brand']} !important;
    color: {c['brand_strong']} !important;
    box-shadow: 0 4px 14px rgba(46, 91, 255, 0.16) !important;
    transform: none !important;
}}
[class*="st-key-ai_sugg_"] button p {{ color: inherit !important; }}

/* Chat input submit arrow — brand-filled so it reads as the primary action. */
[class*="st-key-ai_chat_form_"] [data-testid="stFormSubmitButton"] button {{
    background: {c['brand']} !important;
    border-color: {c['brand']} !important;
    color: #fff !important;
    border-radius: 10px !important;
}}
[class*="st-key-ai_chat_form_"] [data-testid="stFormSubmitButton"] button:hover {{
    background: {c['brand_strong']} !important;
    border-color: {c['brand_strong']} !important;
    color: #fff !important;
}}
[class*="st-key-ai_chat_form_"] [data-testid="stFormSubmitButton"] button p {{ color: #fff !important; }}

/* "Delete chat" — a quiet text link (not a chunky button), destructive-red on
   hover.  Sits in the chat header, right-aligned. */
[class*="st-key-ai_delete_chat"] button {{
    background: transparent !important;
    border: none !important;
    box-shadow: none !important;
    color: {c['muted']} !important;
    font-size: 12.5px !important;
    font-weight: 600 !important;
    min-height: 0 !important;
    padding: 2px 4px !important;
    justify-content: flex-end !important;
    transition: color .15s ease !important;
}}
[class*="st-key-ai_delete_chat"] button:hover {{
    background: transparent !important;
    color: {c['danger']} !important;
    transform: none !important;
}}
[class*="st-key-ai_delete_chat"] button:active {{ background: transparent !important; }}
[class*="st-key-ai_delete_chat"] button p {{ color: inherit !important; }}

/* ── Header "Refresh Numbers" — modern brand-gradient pill ─────────────────── */
[class*="st-key-refresh_numbers"] button {{
    background: linear-gradient(135deg, {c['brand']} 0%, {c['brand_strong']} 100%) !important;
    border: none !important;
    border-radius: 12px !important;
    color: #fff !important;
    font-weight: 600 !important;
    letter-spacing: 0.01em !important;
    box-shadow: 0 2px 10px rgba(46, 91, 255, 0.28) !important;
    transition: box-shadow .18s ease, transform .12s ease, filter .15s ease !important;
}}
[class*="st-key-refresh_numbers"] button:hover {{
    box-shadow: 0 5px 18px rgba(46, 91, 255, 0.38) !important;
    transform: translateY(-1px) !important;
    filter: brightness(1.05) !important;
}}
[class*="st-key-refresh_numbers"] button:active {{ transform: translateY(0) !important; }}
[class*="st-key-refresh_numbers"] button p {{ color: #fff !important; }}

/* ── Metric cards ─────────────────────────────────────────────────────────── */
[data-testid="stMetric"] {{
    background: {c['surface']};
    border: 1px solid {c['border']};
    border-radius: 14px;
    padding: 16px 18px;
    box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04), 0 1px 3px rgba(15, 23, 42, 0.05);
    transition: box-shadow .18s ease, transform .18s ease;
}}
[data-testid="stMetric"]:hover {{
    box-shadow: 0 6px 18px rgba(15, 23, 42, 0.08);
    transform: translateY(-1px);
}}
[data-testid="stMetricValue"] {{ color: {c['ink']}; font-weight: 750; }}
[data-testid="stMetricLabel"], [data-testid="stMetricLabel"] * {{
    color: {c['muted']};
    font-weight: 600;
    letter-spacing: 0.01em;
}}
[data-testid="stMetricDelta"] {{ font-weight: 600; }}

/* ── Expanders ────────────────────────────────────────────────────────────── */
[data-testid="stExpander"] {{
    border: 1px solid {c['border']};
    border-radius: 12px;
    background: {c['surface']};
    box-shadow: 0 1px 2px rgba(15, 23, 42, 0.03);
}}
[data-testid="stExpander"] summary {{
    font-weight: 600;
    color: {c['text']};
    border-radius: 12px;
}}
[data-testid="stExpander"] summary:hover {{ color: {c['brand']}; }}

/* ── Dataframes & tables ──────────────────────────────────────────────────── */
[data-testid="stDataFrame"], [data-testid="stTable"] {{
    border: 1px solid {c['border']};
    border-radius: 12px;
    overflow: hidden;
    box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04), 0 1px 3px rgba(15, 23, 42, 0.04);
}}

/* ── Inputs (select / multiselect / text / number / slider) ───────────────── */
[data-baseweb="select"] > div, [data-baseweb="input"], [data-baseweb="base-input"],
.stTextInput input, .stNumberInput input, .stDateInput input {{
    border-radius: 10px !important;
    border-color: {c['border_2']} !important;
}}
[data-baseweb="select"]:focus-within > div,
.stTextInput div:focus-within, .stNumberInput div:focus-within {{
    border-color: {c['brand']} !important;
    box-shadow: 0 0 0 3px {c['brand_soft']} !important;
}}
/* Multiselect chips */
[data-baseweb="tag"] {{
    background: {c['brand_soft']} !important;
    color: {c['brand_strong']} !important;
    border-radius: 8px !important;
    font-weight: 600;
}}
[data-baseweb="tag"] span[role="presentation"] svg {{ fill: {c['brand_strong']}; }}
/* Slider track + handle */
[data-testid="stSlider"] [data-baseweb="slider"] div[role="slider"] {{
    background: {c['brand']} !important;
    border-color: {c['brand']} !important;
}}

/* ── Dropdown menus (selectbox / multiselect option panels) ───────────────── */
/* Scoped to the menu/listbox inside a baseweb popover, so it never touches the
   AI-assistant popover (which contains no menu/listbox). */
[data-baseweb="popover"] [data-baseweb="menu"],
[data-baseweb="popover"] ul[role="listbox"] {{
    background: {c['surface']} !important;
    border: 1px solid {c['border']} !important;
    border-radius: 10px !important;
    box-shadow: 0 8px 28px rgba(15, 23, 42, 0.14) !important;
    padding: 4px !important;
}}
[data-baseweb="popover"] li[role="option"] {{
    border-radius: 7px !important;
    color: {c['text']} !important;
}}
[data-baseweb="popover"] li[role="option"]:hover,
[data-baseweb="popover"] li[aria-selected="true"] {{
    background: {c['brand_soft']} !important;
    color: {c['brand_strong']} !important;
}}

/* ── Spinner — transparent so it blends into the canvas (no white block) ───── */
[data-testid="stSpinner"] {{ background: transparent !important; }}
[data-testid="stSpinner"] > div {{ background: transparent !important; }}

/* ── Radio / checkbox accents ─────────────────────────────────────────────── */
[data-testid="stRadio"] label[data-baseweb="radio"] div:first-child,
[data-testid="stCheckbox"] label span:first-child {{
    border-color: {c['border_2']};
}}

/* ── Dividers ─────────────────────────────────────────────────────────────── */
hr, [data-testid="stDivider"] {{ border-color: {c['border']} !important; }}

/* ── Alerts (info / success / warning / error) — softer, rounded ──────────── */
[data-testid="stAlert"], [data-testid="stAlertContainer"] {{
    border-radius: 12px;
    border: 1px solid {c['border']};
}}

/* ── Code / inline code ───────────────────────────────────────────────────── */
code {{
    background: {c['brand_soft']};
    color: {c['brand_strong']};
    border-radius: 6px;
    padding: 1px 6px;
    font-size: 0.86em;
}}

/* ── Links ────────────────────────────────────────────────────────────────── */
a, a:visited {{ color: {c['brand']}; text-decoration: none; }}
a:hover {{ color: {c['brand_strong']}; text-decoration: underline; }}

/* ── Scrollbars (webkit) ──────────────────────────────────────────────────── */
::-webkit-scrollbar {{ width: 10px; height: 10px; }}
::-webkit-scrollbar-track {{ background: transparent; }}
::-webkit-scrollbar-thumb {{
    background: #CBD5E1;
    border-radius: 8px;
    border: 2px solid {c['canvas']};
}}
::-webkit-scrollbar-thumb:hover {{ background: {c['faint']}; }}

/* ── Trim Streamlit's default footer (purely decorative) ──────────────────── */
footer {{ visibility: hidden; height: 0; }}
</style>
"""


def inject() -> None:
    """Inject the global design-system CSS.  Call once at the top of main()."""
    st.markdown(_css(), unsafe_allow_html=True)
