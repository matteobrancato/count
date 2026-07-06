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

/* Chat input submit arrow — Dexter red (matches the FAB), the panel's primary
   action. */
[class*="st-key-ai_chat_form_"] [data-testid="stFormSubmitButton"] button {{
    background: linear-gradient(135deg, #FF6B6B 0%, #E63E3E 100%) !important;
    border: none !important;
    color: #fff !important;
    border-radius: 12px !important;
    font-weight: 700 !important;
    box-shadow: 0 2px 8px rgba(255, 75, 75, 0.30) !important;
    transition: filter .15s ease, box-shadow .15s ease !important;
}}
[class*="st-key-ai_chat_form_"] [data-testid="stFormSubmitButton"] button:hover {{
    filter: brightness(1.06) !important;
    box-shadow: 0 4px 14px rgba(255, 75, 75, 0.42) !important;
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

/* ── Global scope + BU control bar (between header and tabs) ─────────────────
   One standardized selector every tab reads from — a light filter card. */
.st-key-global_filter {{
    background: {c['surface']};
    border: 1px solid {c['border']};
    border-radius: 14px;
    padding: 10px 16px 2px;
    margin: 2px 0 6px;
    box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
}}
.st-key-global_filter [data-testid="stRadio"] label p {{
    font-size: 13.5px;
    font-weight: 600;
}}

/* ── Data-freshness label — pinned to the top-right of the tab bar ───────────
   `tabs_zone` wraps the tab bar (position:relative).  The freshness label is
   absolutely pinned to its top-right, level with the tabs.  Hovering the label
   slides in a tiny ↻ button that refreshes just the numbers. */
.st-key-tabs_zone {{ position: relative !important; }}
.st-key-freshness {{
    position: absolute !important;
    top: 26px !important;          /* low in the tab row, flush above the grey line */
    right: 0 !important;
    width: auto !important;
    z-index: 20 !important;
}}
/* The mini ↻ is ABSOLUTELY pinned just LEFT of the label — no reliance on
   Streamlit's wrapper flex (which stacked it under the label).  It stays out
   of the flow, so the container is exactly the label's size. */
.st-key-freshness [class*="st-key-refresh_mini"] {{
    position: absolute !important;
    right: calc(100% + 8px) !important;
    /* Anchor to the label's TEXT line (which starts at the container top) —
       centring on the container height drifted upward because the wrapper is
       taller than the visible text.  11px text vs 15px glyph → -2px offset. */
    top: -2px !important;
    width: auto !important;
    transform: none !important;
    margin: 0 !important;
    line-height: 1 !important;
}}
/* Bare ↻ glyph — no circle, border or background (the previous circle rendered
   oval because Streamlit's default button min-height beat our height).  Hidden
   by default; fades/scales in when the label area is hovered.  (Hovering the
   glyph itself keeps the parent :hover alive.) */
[class*="st-key-refresh_mini"] button {{
    width: auto !important;
    min-width: 0 !important;
    height: auto !important;
    min-height: 0 !important;
    padding: 1px 2px !important;
    border: none !important;
    border-radius: 6px !important;
    background: transparent !important;
    color: {c['muted']} !important;
    font-size: 15px !important;
    line-height: 1 !important;
    box-shadow: none !important;
    outline: none !important;
    opacity: 0;
    transform: scale(0.6);
    transition: opacity .16s ease, transform .2s cubic-bezier(0.34, 1.3, 0.5, 1),
                color .15s ease !important;
}}
[class*="st-key-refresh_mini"] button:focus,
[class*="st-key-refresh_mini"] button:focus-visible,
[class*="st-key-refresh_mini"] button:active {{
    box-shadow: none !important;
    outline: none !important;
    background: transparent !important;
}}
.st-key-freshness:hover [class*="st-key-refresh_mini"] button {{
    opacity: 1;
    transform: scale(1);
}}
.st-key-freshness [class*="st-key-refresh_mini"] button:hover {{
    color: {c['brand']} !important;
    background: transparent !important;
    transform: scale(1.12) rotate(90deg);
}}
[class*="st-key-refresh_mini"] button p {{ color: inherit !important; font-size: inherit !important; }}

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
/* Hand-built stat cards (Backlog tab detail) — match the metric-card hover. */
.stat-card {{ transition: box-shadow .18s ease, transform .18s ease; }}
.stat-card:hover {{
    box-shadow: 0 6px 18px rgba(15, 23, 42, 0.08) !important;
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

/* ── Apple-style scroll-reveal (all data tabs) ────────────────────────────────
   Pure CSS scroll-driven animation — each block fades + rises as it scrolls into
   view, so long pages feel alive instead of heavy.  Applies to any container
   whose key ends in `_anim` (each tab wraps its body in one).  Gated behind
   @supports so browsers WITHOUT scroll timelines (Safari/Firefox today) just
   render everything normally — content can never get stuck invisible.  Anything
   already on screen at load is past its entry range, so it shows instantly. */
@supports (animation-timeline: view()) {{
  @media (prefers-reduced-motion: no-preference) {{
    @keyframes covReveal {{
      from {{ opacity: 0; transform: translateY(64px) scale(0.96); filter: blur(6px); }}
      60%  {{ filter: blur(0); }}
      to   {{ opacity: 1; transform: translateY(0) scale(1); filter: blur(0); }}
    }}
    /* Entry-based range: the reveal completes as the element finishes ENTERING
       the viewport.  (The previous `cover 45%` endpoint was unreachable for the
       last blocks of a page — the scroll ends before they travel that far — so
       they stayed stuck half-blurred.)  entry 85% leaves a little margin so
       even the final block sharpens just before max scroll. */
    [class*="st-key-"][class*="_anim"] [data-testid="stElementContainer"] {{
      animation: covReveal cubic-bezier(0.22, 0.61, 0.36, 1) both;
      animation-timeline: view();
      animation-range: entry 0% entry 85%;
    }}
  }}
}}
</style>
"""


def inject() -> None:
    """Inject the global design-system CSS.  Call once at the top of main()."""
    st.markdown(_css(), unsafe_allow_html=True)
