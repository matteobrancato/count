"""Light / dark theme support.

Light mode is the default and produces the exact same colors the app shipped with.
Dark mode is opt-in via the toggle button in the app header (session-state based).

Usage
─────
    from .ui import theme
    theme.inject_css()        # call once at the top of main()
    theme.toggle_button()     # render the Light/Dark switch
    c = theme.colors()        # active palette dict
    if theme.is_dark(): ...
"""
from __future__ import annotations

import streamlit as st


# ── palettes ─────────────────────────────────────────────────────────────────
LIGHT: dict[str, str] = {
    "name":           "light",
    "bg":             "#ffffff",
    "surface":        "#ffffff",
    "surface_alt":    "#f7f8fa",
    "card_java_bg":   "#FFFBEC",
    "card_testim_bg": "#EBF2FF",
    "card_neutral":   "#ffffff",
    "border":         "#dddddd",
    "border_soft":    "#e8e8e8",
    "text":           "#1a1f36",
    "text_2":         "#5e6677",
    "text_muted":     "#888888",
    "accent_red":     "#C00000",
    "grid":           "#f0f0f0",
    "axis_label":     "#333333",
}

DARK: dict[str, str] = {
    "name":           "dark",
    "bg":             "#1a1d24",   # warm dark grey, not pure black
    "surface":        "#252932",
    "surface_alt":    "#1f222a",
    "card_java_bg":   "#3a3322",   # warm dark amber (echoes #FFFBEC)
    "card_testim_bg": "#1f2738",   # dark indigo  (echoes #EBF2FF)
    "card_neutral":   "#252932",
    "border":         "#3a3f4a",
    "border_soft":    "#2e333d",
    "text":           "#e8eaed",
    "text_2":         "#b4bcc8",
    "text_muted":     "#7e8696",
    "accent_red":     "#FF6B6B",   # softened red for contrast on dark
    "grid":           "#2e333d",
    "axis_label":     "#b4bcc8",
}


# ── state helpers ────────────────────────────────────────────────────────────
def is_dark() -> bool:
    return bool(st.session_state.get("dark_mode", False))


def colors() -> dict[str, str]:
    return DARK if is_dark() else LIGHT


# ── widgets ──────────────────────────────────────────────────────────────────
def toggle_button() -> None:
    """Render a Light/Dark toggle button.  Flips session state and reruns."""
    if "dark_mode" not in st.session_state:
        st.session_state.dark_mode = False
    label = "☀️ Light" if is_dark() else "🌙 Dark"
    if st.button(
        label,
        key="theme_toggle_btn",
        use_container_width=True,
        help="Toggle between light and dark theme",
    ):
        st.session_state.dark_mode = not is_dark()
        st.rerun()


# ── global CSS injection ─────────────────────────────────────────────────────
def inject_css() -> None:
    """Inject global CSS for dark mode.  No-op in light mode (Streamlit default)."""
    if not is_dark():
        return
    c = colors()
    st.markdown(
        f"""
        <style>
        /* ── base canvas ───────────────────────────────────────────── */
        .stApp, .main, .block-container, [data-testid="stAppViewContainer"] {{
            background-color: {c['bg']} !important;
            color: {c['text']};
        }}
        [data-testid="stHeader"] {{
            background-color: {c['bg']};
        }}

        /* ── typography ────────────────────────────────────────────── */
        body, p, span, label, li,
        .stMarkdown, .stMarkdown p, .stMarkdown li, .stMarkdown span,
        h1, h2, h3, h4, h5, h6,
        [data-testid="stMarkdownContainer"] *:not(code):not(pre) {{
            color: {c['text']};
        }}
        [data-testid="stCaptionContainer"], [data-testid="stCaption"],
        [data-testid="stCaptionContainer"] *, small,
        .stCaption, .stCaption * {{
            color: {c['text_muted']} !important;
        }}
        /* Subheader / section markers under tables */
        [data-testid="stMarkdownContainer"] small {{
            color: {c['text_muted']} !important;
        }}

        /* ── metric cards ─────────────────────────────────────────── */
        [data-testid="stMetric"] {{
            background-color: {c['surface']};
            border: 1px solid {c['border']};
            border-radius: 8px;
            padding: 10px;
        }}
        [data-testid="stMetricValue"] {{ color: {c['text']}; }}
        [data-testid="stMetricLabel"], [data-testid="stMetricLabel"] * {{
            color: {c['text_2']};
        }}

        /* ── expander ─────────────────────────────────────────────── */
        [data-testid="stExpander"] {{
            background-color: {c['surface']};
            border: 1px solid {c['border']};
            border-radius: 8px;
        }}
        [data-testid="stExpander"] summary,
        [data-testid="stExpander"] summary * {{
            color: {c['text']} !important;
        }}

        /* ── dataframe (Glide Data Grid CSS variables) ────────────── */
        [data-testid="stDataFrame"], [data-testid="stTable"] {{
            background-color: {c['surface']} !important;
            border-radius: 6px;
            --gdg-bg-cell: {c['surface']};
            --gdg-bg-cell-medium: {c['surface_alt']};
            --gdg-bg-header: {c['surface_alt']};
            --gdg-bg-header-has-focus: {c['border_soft']};
            --gdg-bg-header-hovered: {c['border_soft']};
            --gdg-bg-bubble: {c['surface_alt']};
            --gdg-bg-bubble-selected: {c['border_soft']};
            --gdg-bg-search-result: #4a3d1a;
            --gdg-text-dark: {c['text']};
            --gdg-text-medium: {c['text_2']};
            --gdg-text-light: {c['text_muted']};
            --gdg-text-bubble: {c['text']};
            --gdg-text-header: {c['text']};
            --gdg-text-group-header: {c['text']};
            --gdg-text-header-selected: #ffffff;
            --gdg-accent-color: #ED7D31;
            --gdg-accent-light: rgba(237,125,49,0.15);
            --gdg-accent-fg: #ffffff;
            --gdg-border-color: {c['border']};
            --gdg-horizontal-border-color: {c['border_soft']};
            --gdg-drilldown-border: {c['border']};
            --gdg-link-color: #6FA4FF;
            --gdg-bg-icon-header: {c['surface_alt']};
            --gdg-fg-icon-header: {c['text_2']};
        }}
        /* Catch the inner canvas-wrapper that Streamlit/Glide uses */
        [data-testid="stDataFrame"] > div,
        [data-testid="stDataFrame"] [data-testid="stElementToolbar"],
        [data-testid="stTable"] table,
        [data-testid="stTable"] th,
        [data-testid="stTable"] td {{
            background-color: {c['surface']};
            color: {c['text']};
            border-color: {c['border_soft']};
        }}

        /* ── tabs ─────────────────────────────────────────────────── */
        [data-baseweb="tab-list"] {{ border-bottom-color: {c['border']}; }}
        [data-baseweb="tab-list"] button {{ color: {c['text_2']} !important; }}
        [data-baseweb="tab-list"] button[aria-selected="true"] {{
            color: {c['text']} !important;
            border-bottom-color: #ED7D31 !important;
        }}

        /* ── inputs ───────────────────────────────────────────────── */
        [data-baseweb="select"] > div,
        [data-baseweb="input"] > div,
        .stTextInput input, .stNumberInput input,
        .stSelectbox > div, .stMultiSelect > div, .stDateInput > div {{
            background-color: {c['surface']} !important;
            color: {c['text']} !important;
            border-color: {c['border']} !important;
        }}
        [data-baseweb="popover"] {{
            background-color: {c['surface']} !important;
            color: {c['text']} !important;
        }}

        /* ── buttons ──────────────────────────────────────────────── */
        .stButton button {{
            background-color: {c['surface']};
            color: {c['text']};
            border: 1px solid {c['border']};
        }}
        .stButton button:hover {{
            background-color: {c['surface_alt']};
            border-color: {c['text_2']};
            color: {c['text']};
        }}

        /* ── alerts ───────────────────────────────────────────────── */
        [data-testid="stAlertContainer"] {{
            background-color: {c['surface']} !important;
            color: {c['text']} !important;
            border-color: {c['border']} !important;
        }}

        /* ── code / divider ───────────────────────────────────────── */
        code, pre {{
            background-color: {c['surface_alt']} !important;
            color: {c['text']};
        }}
        hr {{
            border-color: {c['border']} !important;
            border-width: 1px;
        }}
        </style>
        """,
        unsafe_allow_html=True,
    )
