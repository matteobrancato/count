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
    "java_bg":       "#FEF6E7",   # amber (Java / Selenium)
    "testim_bg":     "#EAEEFF",   # indigo (TestIM)
    "playwright_bg": "#E7F6EE",   # green (TypeScript / Playwright)
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
[data-testid="stTab"] [data-testid="stMarkdownContainer"],
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
/* Streamlit 1.59 tabs: `[data-testid="stTab"]` in a `[role="tablist"]`, the
   active one with aria-selected and a `.react-aria-SelectionIndicator` child.
   (These rules targeted the old baseweb tabs until 2026-10-02, and had silently
   stopped applying.)  The label lives in a markdown <p>, which sets its own
   colour, so the colour is set on it. */
[role="tablist"] {{ gap: 2px; }}
[data-testid="stTab"] {{
    border-radius: 9px 9px 0 0;
    padding-left: 14px;
    padding-right: 14px;
    transition: background .15s ease;
}}
[data-testid="stTab"] [data-testid="stMarkdownContainer"] p {{
    color: {c['muted']};
    font-weight: 600;
    /* Emoji sit taller than the text they follow; without room of their own the
       line box crops them and the label reads as cut off. */
    line-height: 1.5;
    transition: color .15s ease;
}}
[data-testid="stTab"]:hover {{ background: {c['brand_soft']}; }}
[data-testid="stTab"]:hover [data-testid="stMarkdownContainer"] p {{ color: {c['ink']}; }}
[data-testid="stTab"][aria-selected="true"] [data-testid="stMarkdownContainer"] p {{
    color: {c['brand']};
}}
[data-testid="stTab"] .react-aria-SelectionIndicator {{
    background-color: {c['brand']} !important;
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
/* Primary-kind buttons keep the brand fill — a form's primary submit included:
   the rule above gives every form submit a white surface, and it used to win
   over `type="primary"`, leaving a form's one call to action looking like a
   secondary button. */
.stButton > button[kind="primary"],
[data-testid="stFormSubmitButton"] button[kind="primaryFormSubmit"] {{
    background: {c['brand']};
    border-color: {c['brand']};
    color: #fff;
}}
.stButton > button[kind="primary"]:hover,
[data-testid="stFormSubmitButton"] button[kind="primaryFormSubmit"]:hover {{
    background: {c['brand_strong']};
    border-color: {c['brand_strong']};
    color: #fff;
}}
/* The label is a <p>, which the global paragraph colour reaches first: slate
   text on the brand fill.  Same fix the chat submit arrow needed. */
.stButton > button[kind="primary"] p,
[data-testid="stFormSubmitButton"] button[kind="primaryFormSubmit"] p {{
    color: #fff !important;
}}
/* Tertiary buttons read as quiet text actions ("Clear result"), not as boxes. */
.stButton > button[kind="tertiary"],
.stButton > button[kind="tertiary"]:hover {{
    background: transparent;
    border-color: transparent;
    box-shadow: none;
    transform: none;
}}
.stButton > button[kind="tertiary"] p {{ color: {c['muted']} !important; }}
.stButton > button[kind="tertiary"]:hover p {{ color: {c['brand']} !important; }}

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

/* ── Group KPI chips (right end of the top bar) ───────────────────────────────
   The two cross-BU figures for managers — chips with RAG dots. */
.kpi-row {{
    display: flex;
    flex-wrap: nowrap;             /* one clean line, always */
    align-items: center;
    gap: 8px;
    overflow-x: auto;              /* worst case: scrolls, never wraps ugly */
    scrollbar-width: none;
}}
.kpi-row::-webkit-scrollbar {{ display: none; }}
.kpi-chip {{
    display: inline-flex;
    align-items: center;
    gap: 6px;
    flex: 0 0 auto;
    background: {c['canvas']};
    border: 1px solid {c['border']};
    border-radius: 999px;
    padding: 5px 11px;
    font-size: 12px;
    color: {c['text']};
    white-space: nowrap;
    cursor: default;
}}
.kpi-chip b {{ color: {c['ink']}; font-weight: 750; }}
.kpi-chip .kpi-sub {{ color: {c['muted']}; font-size: 11px; }}
/* Skeleton placeholder — reserves the strip's space during the first load so
   the page doesn't shift when the real chips arrive. */
.kpi-skeleton {{
    display: inline-flex;
    height: 28px;
    width: 170px;
    flex: 0 0 auto;
    border-radius: 999px;
    background: linear-gradient(90deg, {c['canvas']} 25%, {c['border']} 50%, {c['canvas']} 75%);
    background-size: 200% 100%;
    animation: kpiShimmer 1.4s ease infinite;
}}
@keyframes kpiShimmer {{
    from {{ background-position: 200% 0; }}
    to   {{ background-position: -200% 0; }}
}}

/* ── Top bar: brand (title + KPI chips) · scope + BU controls ────────────────
   One row of chrome above the tabs (it replaced a KPI card and a filter card).
   Wraps on a narrow window instead of overflowing. */
.st-key-topbar, .st-key-topbar_controls {{
    flex-wrap: wrap !important;
    row-gap: 8px !important;
}}
.st-key-topbar {{ padding: 2px 0 6px; }}
.st-key-topbar [data-testid="stElementContainer"] {{ margin: 0 !important; }}
.st-key-topbar [data-testid="stSelectbox"] {{ margin: 0 !important; }}
/* The KPI chips are the line under the title: tight to it, and a size
   smaller than the title so the two read as one block. */
.st-key-brand_text {{ gap: 4px !important; }}
.st-key-brand_text .kpi-chip {{ padding: 1px 9px; font-size: 11.5px; gap: 5px; }}
/* Four chips are wider than the title: the line keeps the title's block
   narrow enough for the controls to stay on the same row, and scrolls
   sideways (trackpad, shift + wheel) with its end faded so the scroll reads. */
.st-key-brand_text .kpi-row {{
    max-width: 430px;
    overflow-x: auto;
    scrollbar-width: none;
    -webkit-mask-image: linear-gradient(to right, #000 88%, transparent);
    mask-image: linear-gradient(to right, #000 88%, transparent);
    padding-right: 28px;
}}
.st-key-brand_text .kpi-skeleton {{ height: 20px; width: 130px; }}

/* ── Secondary export button (right-aligned under the summary table) ────────
   A quiet action: small, muted, brand-tinted only on hover. */
/* Summary-table CSV export — a text label sitting on the legend row, not a
   button: it is a secondary action and a filled control dominated the row. */
.st-key-summary_export button {{
    background: transparent !important;
    border: none !important;
    box-shadow: none !important;
    padding: 0 !important;
    min-height: 0 !important;
    height: auto !important;
}}
.st-key-summary_export button p {{
    font-size: 12px !important;
    font-weight: 500 !important;
    color: {c['muted']} !important;
    margin: 0 !important;
}}
.st-key-summary_export button:hover {{
    transform: none !important;
    box-shadow: none !important;
}}
.st-key-summary_export button:hover p {{
    color: {c['brand']} !important;
    text-decoration: underline !important;
}}

/* ── Utility-bar disclosures (methodology · data quality) ───────────────────
   Popover triggers rendered as plain text links: no border, no background,
   underline on hover.  They used to be full-width expanders competing with the
   KPI strip for attention. */
.st-key-freshness [data-testid="stPopover"] button {{
    background: transparent !important;
    border: none !important;
    box-shadow: none !important;
    padding: 0 !important;
    min-height: 0 !important;
    height: auto !important;
    color: {c['muted']} !important;
    white-space: nowrap !important;
}}
/* Centre the label inside the trigger box: Streamlit's button line-height
   (1.6 × 16px) is taller than our 11px label, which parked the text ~2px below
   the row's centre line. */
.st-key-freshness [data-testid="stPopover"] button,
.st-key-summary_export button {{
    display: flex !important;
    align-items: center !important;
    line-height: 1 !important;
}}
.st-key-freshness [data-testid="stPopover"] button p {{
    font-size: 11px !important;
    color: {c['muted']} !important;
    font-weight: 500 !important;
    margin: 0 !important;
}}
.st-key-freshness [data-testid="stPopover"] button:hover p {{
    color: {c['brand']} !important;
    text-decoration: underline !important;
}}
/* The Overview trigger is a button (it opens a dialog), dressed exactly like
   the two popover links next to it. */
.st-key-freshness [class*="st-key-overview_open"] {{ width: auto !important; }}
.st-key-freshness [class*="st-key-overview_open"] button {{
    background: transparent !important;
    border: none !important;
    box-shadow: none !important;
    padding: 0 !important;
    min-height: 0 !important;
    height: auto !important;
    display: flex !important;
    align-items: center !important;
    line-height: 1 !important;
}}
.st-key-freshness [class*="st-key-overview_open"] button p {{
    font-size: 11px !important;
    color: {c['muted']} !important;
    font-weight: 500 !important;
    margin: 0 !important;
    line-height: 20px !important;
    white-space: nowrap !important;
}}
.st-key-freshness [class*="st-key-overview_open"] button:hover,
.st-key-freshness [class*="st-key-overview_open"] button:focus,
.st-key-freshness [class*="st-key-overview_open"] button:focus-visible,
.st-key-freshness [class*="st-key-overview_open"] button:active {{
    transform: none !important;
    box-shadow: none !important;
    outline: none !important;
    background: transparent !important;
}}
.st-key-freshness [class*="st-key-overview_open"] button:hover p {{
    color: {c['brand']} !important;
    text-decoration: underline !important;
}}
/* Hide Streamlit's caret so the trigger reads as text, not as a control. */
.st-key-freshness [data-testid="stPopover"] button svg,
.st-key-freshness [data-testid="stPopover"] button [data-testid="stIconMaterial"] {{
    display: none !important;
}}
/* ONE optical line for the two utility rows (tab-bar bar + summary legend).
   Streamlit centres each item's BOX, but a popover trigger, a markdown label
   and a bare glyph button have different line boxes, so their text landed on
   three different heights.  Giving every text carrier the same line box lines
   the glyphs up (measured spread: 8px → <1px). */
.st-key-freshness [data-testid="stMarkdownContainer"] p,
.st-key-freshness [data-testid="stPopover"] button p,
.st-key-freshness [class*="st-key-refresh_mini"] button,
.st-key-freshness [class*="st-key-refresh_mini"] button p,
.st-key-summary_export [data-testid="stMarkdownContainer"] p,
.st-key-summary_export button p {{
    line-height: 20px !important;
}}
/* The panels themselves: roomy, readable cards (the chat popover styling is
   scoped to the chat form, so it never reaches these). */
[data-testid="stPopoverBody"]:has(.dq-panel),
[data-testid="stPopoverBody"]:has(.methodology-panel) {{
    min-width: 560px;
    max-width: min(760px, 94vw);
    max-height: min(640px, 78vh);
    overflow-y: auto;
}}
[data-testid="stPopoverBody"] table {{ font-size: 12.5px; }}

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
    /* Layout (one centred row) comes from st.container(horizontal=True,
       vertical_alignment="center") — do NOT re-declare flex here: overriding
       Streamlit's own flex is what knocked the labels out of alignment. */
}}
/* Narrow viewports: the tab bar wins the row — drop the utility bar below it
   instead of letting the two overlap. */
@media (max-width: 1250px) {{
    /* Narrow screens: let the KPI chips wrap onto a second line.  The row is
       `overflow-x: auto` with the scrollbar hidden, so on a laptop the last
       chip sat outside the card with nothing to suggest it could be scrolled
       to — silently truncated KPIs.  Two short lines beat one hidden one. */
    .kpi-row {{ flex-wrap: wrap !important; overflow-x: visible !important; }}
    /* The container is created with width="content", so BOTH it and Streamlit's
       layout wrapper hug the content — they must span the row for the bar to
       align right once it is back in the normal flow. */
    [data-testid="stLayoutWrapper"]:has(> .st-key-freshness) {{
        width: 100% !important;
    }}
    .st-key-freshness {{
        position: static !important;
        width: 100% !important;
        justify-content: flex-end !important;
        margin: 4px 0 6px !important;
    }}
}}
/* The mini ↻ is the last item of the bar, in normal flow (the bar is a flex
   row now, so no absolute anchoring is needed). */
.st-key-freshness [class*="st-key-refresh_mini"] {{
    width: auto !important;
    margin: 0 0 0 -8px !important;   /* tighten against "Updated …" */
    line-height: 1 !important;
}}
/* Bare ↻ glyph — no circle, border or background (the previous circle rendered
   oval because Streamlit's default button min-height beat our height).  Always
   visible (it used to appear only on hover, so nobody knew it was there); a
   quarter turn on hover says what it does. */
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
    transition: transform .25s cubic-bezier(0.34, 1.3, 0.5, 1),
                color .15s ease !important;
}}
[class*="st-key-refresh_mini"] button:focus,
[class*="st-key-refresh_mini"] button:focus-visible,
[class*="st-key-refresh_mini"] button:active {{
    box-shadow: none !important;
    outline: none !important;
    background: transparent !important;
}}
.st-key-freshness [class*="st-key-refresh_mini"] button:hover {{
    color: {c['brand']} !important;
    background: transparent !important;
    transform: rotate(90deg);
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

/* ── Checkbox accents ─────────────────────────────────────────────────────── */
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

/* ── Warm-up status box: livelier loading (texts stay) ────────────────────────
   1. An indeterminate gradient bar sweeps along the top edge (Linear/GitHub
      style) while the box exists.
   2. Each step line slides+fades in as it appears.
   Scoped to `.st-key-warmup_status`, which only exists during the first load. */
.st-key-warmup_status {{
    position: relative;
    overflow: hidden;
    border-radius: 12px;
}}
.st-key-warmup_status::before {{
    content: "";
    position: absolute;
    top: 0; left: 0; right: 0;
    height: 3px;
    z-index: 5;
    border-radius: 3px 3px 0 0;
    background: linear-gradient(90deg,
        transparent 0%, {c['brand']} 30%, #FF4B4B 55%, transparent 80%);
    background-size: 220% 100%;
    animation: warmupSweep 1.5s linear infinite;
    pointer-events: none;
}}
@keyframes warmupSweep {{
    from {{ background-position: 220% 0; }}
    to   {{ background-position: -220% 0; }}
}}
.st-key-warmup_status [data-testid="stMarkdownContainer"] {{
    animation: warmupStepIn 0.45s cubic-bezier(0.22, 0.61, 0.36, 1) both;
}}
@keyframes warmupStepIn {{
    from {{ opacity: 0; transform: translateX(-10px); }}
    to   {{ opacity: 1; transform: translateX(0); }}
}}

/* ── Backlog "All Business Units" summary table (premium, custom HTML) ────────
   Same data as the native dataframe (kept in an expander) but presentation-grade
   for managers: RAG coverage bar, tidy number typography, hover rows. */
.bl-summary {{
    overflow-x: auto;
    border: 1px solid {c['border']};
    border-radius: 14px;
    box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
    background: {c['surface']};
}}
.bl-summary table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
    font-variant-numeric: tabular-nums;
}}
.bl-summary th {{
    text-align: right;
    padding: 9px 10px;
    font-size: 10.5px;
    font-weight: 700;
    color: {c['muted']};
    text-transform: uppercase;
    letter-spacing: 0.03em;
    border-bottom: 1px solid {c['border']};
    /* Two-word headers wrap instead of forcing their column wide: "PARTIALLY
       AUTOMATED" on one line was the single biggest reason the table needed a
       horizontal scrollbar.  Bottom-aligned so one-line and two-line headers
       sit on the same baseline as the numbers below them. */
    white-space: normal;
    line-height: 1.25;
    vertical-align: bottom;
    background: {c['canvas']};
    position: sticky;
    top: 0;
}}
.bl-summary td {{
    text-align: right;
    padding: 11px 10px;
    color: {c['text']};
    border-bottom: 1px solid {c['grid']};
    white-space: nowrap;   /* numbers must never wrap */
}}
/* Breathing room against the rounded border, without paying for it on all
   thirteen columns. */
.bl-summary th:first-child, .bl-summary td:first-child {{ padding-left: 15px; }}
.bl-summary th:last-child,  .bl-summary td:last-child  {{ padding-right: 15px; }}
.bl-summary tr:last-child td {{ border-bottom: none; }}
.bl-summary tbody tr {{ transition: background .12s ease; }}
.bl-summary tbody tr:hover td {{ background: {c['canvas']}; }}
/* The BU the global filter is on — just its name underlined.  A tinted row
   with an accent bar shouted louder than the data it was pointing at. */
.bl-summary tbody tr.sel .bu {{
    text-decoration: underline;
    text-decoration-color: {c['brand']};
    text-underline-offset: 3px;
    text-decoration-thickness: 2px;
}}
.bl-summary th.l, .bl-summary td.l {{ text-align: left; }}
.bl-summary .bu {{ font-weight: 700; color: {c['ink']}; }}
.bl-summary .strong {{ font-weight: 700; color: {c['ink']}; }}
.bl-summary .mut {{ color: {c['muted']}; }}
/* Backlog health, table-weight: the card's pill without its tinted background,
   which would compete with the coverage bars on every row. */
.bl-summary .bl-pct {{
    display: block;
    font-size: 10.5px;
    font-weight: 700;
    line-height: 1.2;
    margin-top: 1px;
    cursor: help;
}}
.bl-summary .scope-pill {{
    display: inline-block;
    font-size: 11px;
    color: {c['muted']};
    background: {c['canvas']};
    border: 1px solid {c['border']};
    border-radius: 6px;
    padding: 1px 8px;
}}
.bl-summary .cov-wrap {{
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 118px;
}}
.bl-summary .cov-track {{
    flex: 1;
    height: 7px;
    border-radius: 4px;
    background: {c['grid']};
    overflow: hidden;
}}
.bl-summary .cov-fill {{ height: 100%; border-radius: 4px; }}
/* Backlog · To be Updated · Partially, stacked in one cell.  A two-column grid
   so the labels align left and the figures right, which is what keeps three
   numbers in a row readable instead of a pile. */
.bl-summary .stack {{
    display: inline-grid;
    /* Fixed tracks: verdict · label · figure.  Sized to the widest each will
       ever hold ("47.3%", "TO BE UPDATED", "1,419") so no row can resize a
       column and pull the block sideways. */
    grid-template-columns: 32px 78px 42px;
    gap: 0 6px;
    line-height: 1.15;
}}
.bl-summary .stack u {{
    text-decoration: none;
    text-align: right;
    align-self: center;
}}
/* `.bl-pct` is a block in the Backlog cell; inline here, so it sits beside the
   word it qualifies instead of opening a line of its own. */
.bl-summary .stack .bl-pct {{
    display: inline;
    font-size: 9.5px;
}}
.bl-summary .stack i {{
    font-style: normal;
    font-size: 9.5px;
    font-weight: 700;
    letter-spacing: 0.02em;
    text-transform: uppercase;
    color: {c['muted']};
    text-align: left;
    align-self: center;
    white-space: nowrap;
}}
.bl-summary .stack b {{
    font-weight: 600;
    font-size: 12px;
    color: {c['text']};
    text-align: right;
    font-variant-numeric: tabular-nums;
}}
/* Coverage with the partial gaps out of the baseline — a second, quieter line
   under the bar, right-aligned with the percentage above it.  Same treatment as
   `.bl-pct` in the Backlog cell: present for whoever looks for it, never loud
   enough to be mistaken for the headline number. */
.bl-summary .cov-ex {{
    display: block;
    margin-top: 1px;
    font-size: 10.5px;
    line-height: 1.2;
    color: {c['muted']};
    text-align: right;
    cursor: help;
}}
.bl-summary .cov-val {{ font-weight: 700; font-size: 12.5px; min-width: 46px; text-align: right; }}
/* Automation save: the unit under the header, and the effort it is part of
   under the figure — both quieter than the number, like `.cov-ex`, but with no
   tooltip behind them, so no help cursor either. */
.bl-summary .th-sub {{
    display: block;
    margin-top: 2px;
    font-size: 10px;
    font-weight: 500;
    letter-spacing: 0;
    text-transform: none;
}}
/* Automation save, split by device: label · figure, one line each. */
.bl-summary .save-split {{
    display: inline-grid;
    grid-template-columns: auto auto;
    gap: 0 8px;
    align-items: baseline;
}}
.bl-summary .save-split i {{
    font-style: normal;
    font-size: 9.5px;
    font-weight: 700;
    letter-spacing: 0.02em;
    text-transform: uppercase;
    color: {c['muted']};
    text-align: left;
}}
.bl-summary .save-split b {{
    font-weight: 700;
    color: {c['ink']};
    text-align: right;
    font-variant-numeric: tabular-nums;
}}
.bl-summary .sub {{
    display: block;
    margin-top: 1px;
    font-size: 10.5px;
    line-height: 1.2;
    font-weight: 500;
    color: {c['muted']};
}}

/* ── Coverage chart panels — a fixed-height head so both charts start on the
   same line, whatever width the captions wrap at. ──────────────────────────── */
.cov-panel-head {{
    display: block;
    height: 56px;
    margin: 6px 0 2px;
}}
.cov-panel-title {{
    display: block;
    font-weight: 700;
    font-size: 15px;
    color: {c['ink']};
    border-left: 3px solid {c['brand']};
    padding-left: 10px;
    line-height: 1.35;
}}
.cov-panel-sub {{
    display: block;
    font-size: 12px;
    color: {c['muted']};
    padding-left: 13px;
    margin-top: 3px;
    line-height: 1.4;
}}

/* Segmented controls: same reason as the tabs — a cropped glyph reads as a
   rendering fault, and these labels are read at a glance. */
[data-testid="stButtonGroup"] button p {{ line-height: 1.5 !important; }}

/* Granularity picker — a quiet, secondary control next to the view radio.
   (st.segmented_control renders as data-testid="stButtonGroup".) */
.st-key-cov_gran_row [data-testid="stButtonGroup"] button p {{
    font-size: 12.5px !important;
}}

/* Overview BU filter — a quiet text-link trigger above the numbers, never a
   competitor to them. */
.st-key-ov_filter {{ margin: 0 0 6px; }}
.st-key-ov_filter [data-testid="stPopover"] button {{
    background: transparent !important;
    border: none !important;
    box-shadow: none !important;
    padding: 0 !important;
    min-height: 0 !important;
}}
.st-key-ov_filter [data-testid="stPopover"] button p {{
    font-size: 12.5px !important;
    color: {c['muted']} !important;
    font-weight: 500 !important;
}}
.st-key-ov_filter [data-testid="stPopover"] button:hover p {{
    color: {c['brand']} !important;
    text-decoration: underline !important;
}}
[data-testid="stPopoverBody"]:has([class*="st-key-ov_"]) {{
    min-width: 320px;
    max-height: min(560px, 70vh);
    overflow-y: auto;
}}

/* ── Clickable stat tiles ───────────────────────────────────────────────────
   Streamlit cannot bind a callback to custom HTML, so each tile holds its
   download button stretched invisibly across it: the card IS the click target
   and no second control appears.  The button keeps its label for screen
   readers and its tooltip on hover. */
[class*="st-key-tile_"] {{
    position: relative !important;
}}
/* Streamlit wraps every widget in a `position: relative` element container, so
   an absolutely-positioned button resolves its offsets against THAT (a 16x0
   box) instead of the tile.  Stretch the element container itself and the
   button fills the card. */
[class*="st-key-tile_"] [data-testid="stElementContainer"]:has([data-testid="stDownloadButton"]) {{
    position: absolute !important;
    top: 0 !important;
    left: 0 !important;
    width: 100% !important;
    height: 100% !important;
    margin: 0 !important;
    z-index: 3;
}}
[class*="st-key-tile_"] [data-testid="stDownloadButton"] {{
    width: 100% !important;
    height: 100% !important;
    margin: 0 !important;
}}
[class*="st-key-tile_"] [data-testid="stDownloadButton"] button {{
    width: 100% !important;
    height: 100% !important;
    min-height: 0 !important;
    padding: 0 !important;
    opacity: 0 !important;
    cursor: pointer !important;
    border: none !important;
    background: transparent !important;
    box-shadow: none !important;
    transform: none !important;
}}
/* Hover: lift the card so the tile reads as actionable. */
[class*="st-key-tile_"]:hover .stat-card {{
    border-color: {c['brand']} !important;
    box-shadow: 0 4px 14px rgba(46, 91, 255, 0.16) !important;
    transform: translateY(-1px);
}}
[class*="st-key-tile_"] .stat-card {{
    transition: border-color .15s ease, box-shadow .15s ease, transform .12s ease;
}}
/* Keyboard focus must stay visible even though the button is transparent. */
[class*="st-key-tile_"] [data-testid="stDownloadButton"] button:focus-visible {{
    opacity: 1 !important;
    outline: 2px solid {c['brand']} !important;
    outline-offset: -2px;
    background: rgba(46, 91, 255, 0.06) !important;
    color: transparent !important;
}}

/* ── Trim Streamlit's default footer (purely decorative) ──────────────────── */
footer {{ visibility: hidden; height: 0; }}

/* ── Gentle entrance animation (all data tabs) ────────────────────────────────
   TIME-based fade on every block of a tab's body (`*_anim` containers) — it
   always completes in half a second, by construction.

   OPACITY ONLY — deliberately no `transform`.  A transform on an ancestor
   makes `position: fixed` descendants resolve against that ancestor instead of
   the viewport, which BROKE the dataframe/chart fullscreen overlay every time
   a fragment rerun restarted the animation.  Opacity creates only a stacking
   context (harmless for fixed positioning), so fullscreen works everywhere.

   Deliberately NOT scroll-driven either: `animation-timeline: view()` proved
   unreliable with Streamlit's dynamic DOM (blocks frozen mid-animation). */
@media (prefers-reduced-motion: no-preference) {{
  @keyframes blockIn {{
    from {{ opacity: 0; }}
    to   {{ opacity: 1; }}
  }}
  [class*="st-key-"][class*="_anim"] [data-testid="stElementContainer"] {{
    animation: blockIn 0.4s ease both;
  }}
}}
</style>
"""


# ── health thresholds (RAG) ───────────────────────────────────────────────────
# Shared by the KPI strip, the Backlog All-BU table and the Coverage headlines,
# so a colour always means the same thing everywhere.
COVERAGE_TARGET = 80.0   # 🟢 at/above target
COVERAGE_WARN   = 60.0   # 🟡 at/above, 🔴 below
BACKLOG_OK_PCT     = 3.0  # 🟢 backlog ≤ 3% of baseline rows (same rule as the card badge)
BACKLOG_WARN_PCT   = 6.0  # 🟡 ≤ 6%, 🔴 above


def coverage_health(pct: float) -> tuple[str, str]:
    """(emoji, colour) for a coverage % against the group targets."""
    if pct >= COVERAGE_TARGET:
        return "🟢", COLORS["success"]
    if pct >= COVERAGE_WARN:
        return "🟡", COLORS["warning"]
    return "🔴", COLORS["danger"]


def backlog_health(pct: float) -> tuple[str, str]:
    """(emoji, colour) for backlog as % of baseline rows."""
    if pct <= BACKLOG_OK_PCT:
        return "🟢", COLORS["success"]
    if pct <= BACKLOG_WARN_PCT:
        return "🟡", COLORS["warning"]
    return "🔴", COLORS["danger"]


def section_title(text: str, *, top: int = 6) -> None:
    """The ONE section heading used across every tab.

    Sections used to mix a branded left-accent heading with plain `#### markdown`
    ones, so the same level of hierarchy looked different from tab to tab.  This
    is the single implementation — import and call it instead of `st.markdown`.
    """
    import streamlit as _st
    _st.markdown(
        f'<div style="font-weight:700;font-size:15px;color:{COLORS["ink"]};'
        f'border-left:3px solid {COLORS["brand"]};padding-left:10px;'
        f'margin:{top}px 0 10px">{text}</div>',
        unsafe_allow_html=True,
    )


def inject(extra: str = "") -> None:
    """Inject the global design-system CSS, plus `extra` (e.g. subtabs_css),
    in ONE element: each st.markdown adds a gap to the top of the page.
    Call once at the top of main()."""
    st.markdown(_css() + extra, unsafe_allow_html=True)


def subtabs_css(parent: int, children: tuple[int, ...]) -> str:
    """Tabs `children` shown only while their group (`parent` or one of
    them) is open, sliding in beside the parent.

    Streamlit has no nested tabs.  Every tab is a sibling in one tab list,
    `[data-testid="stTab"][data-key="<index>"]` with `aria-selected`
    (Streamlit 1.59), so `:has()` can tell whether the group is open.  If a
    later Streamlit drops `data-key`, nothing matches and the sub-tabs are
    simply always visible."""
    c = COLORS

    def tab(i: int) -> str:
        return f'[data-testid="stTab"][data-key="{i}"]'

    def open_(tabs) -> str:
        return ", ".join(f'> {tab(i)}[aria-selected="true"]' for i in tabs)

    kids = ":is(" + ", ".join(tab(i) for i in children) + ")"
    delays = "\n".join(f'[role="tablist"] > {tab(i)} {{ animation-delay: {n * 60}ms; }}'
                       for n, i in enumerate(children))
    return f"""<style>
[role="tablist"]:not(:has({open_((parent, *children))})) > {kids} {{ display: none; }}
[role="tablist"] > {kids} {{
    animation: count-subtab-in .34s cubic-bezier(0.2, 0.8, 0.2, 1) both;
}}
[role="tablist"] > {kids} [data-testid="stMarkdownContainer"] p {{ font-size: 13px; }}
{delays}
/* A hairline before the first one says they belong to the tab on their left. */
[role="tablist"] > {tab(children[0])} {{ margin-left: 2px; position: relative; }}
[role="tablist"] > {tab(children[0])}::before {{
    content: ""; position: absolute; left: -2px; top: 30%; height: 40%;
    border-left: 1px solid {c['border']};
}}
/* The parent keeps the brand colour while one of its sub-tabs is open. */
[role="tablist"]:has({open_(children)}) > {tab(parent)} [data-testid="stMarkdownContainer"] p {{
    color: {c['brand']};
}}
@keyframes count-subtab-in {{
    from {{ opacity: 0; transform: translateX(-8px); }}
    to   {{ opacity: 1; transform: none; }}
}}
@media (prefers-reduced-motion: reduce) {{
    [role="tablist"] > {kids} {{ animation: none; }}
}}
</style>"""


def stat_card(col, label: str, n: int | str, u: int | None = None, *,
              badge_html: str = "") -> None:
    """The ONE metric card of the dashboard — Backlog and Leakage share it.

    Identical markup everywhere means identical height, which is what keeps a
    row of cards (and any captions under them) aligned.  *u* adds the
    unique-cases caption; pass None where that number would be misleading (a
    figure that is not a count of cases, such as the Leakage tab's incidents).
    *n* is a count, or an already formatted figure such as a ratio ("12.6%").
    """
    import streamlit as _st  # noqa: F401
    col.markdown(
        f"<div class='stat-card' style='background:{COLORS['surface']};"
        f"border:1px solid {COLORS['border']};border-radius:14px;padding:16px 18px;"
        f"box-shadow:0 1px 2px rgba(15,23,42,0.04),0 1px 3px rgba(15,23,42,0.05)'>"
        f"<div style='color:{COLORS['muted']};font-weight:600;font-size:13.5px;"
        f"letter-spacing:0.01em;white-space:nowrap'>{label}</div>"
        f"<div style='display:flex;align-items:center;gap:9px;margin-top:6px'>"
        f"<span style='color:{COLORS['ink']};font-weight:750;font-size:34px;"
        f"line-height:1.15'>{n if isinstance(n, str) else f'{n:,}'}</span>"
        f"{badge_html}</div></div>",
        unsafe_allow_html=True,
    )
    if u is not None:
        col.caption(f"{u:,} {'case' if u == 1 else 'cases'}")
