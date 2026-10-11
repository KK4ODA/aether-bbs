"""
gui/theme.py

Discord-inspired dark theme for Aether BBS.
Apply once at startup:  apply_theme(app)
"""

from PyQt6.QtWidgets import QApplication, QStyleFactory
from PyQt6.QtGui import QFont, QPalette, QColor
from PyQt6.QtCore import Qt

# ── Discord colour palette ────────────────────────────────────────────────────
# Background layers (darkest → lightest)
BG_DEEPEST   = "#1e1f22"   # outermost window / sidebar
BG_DARK      = "#2b2d31"   # main content area
BG_MEDIUM    = "#313338"   # panels / group boxes
BG_ELEVATED  = "#383a40"   # cards, inputs, table rows (even)
BG_HOVER     = "#404249"   # hover state
BG_SELECTED  = "#43444b"   # selected row

# Borders
BORDER_SUBTLE  = "#3b3d44"
BORDER_NORMAL  = "#4e505a"

# Text
TEXT_PRIMARY   = "#dbdee1"
TEXT_SECONDARY = "#b5bac1"
TEXT_MUTED     = "#80848e"
TEXT_LINK      = "#00a8fc"

# Accent — Discord blurple
ACCENT         = "#5865f2"
ACCENT_HOVER   = "#4752c4"
ACCENT_ACTIVE  = "#3c45a5"

# Status colours
GREEN          = "#23a55a"
GREEN_DIM      = "#1a7f42"
YELLOW         = "#f0b232"
RED            = "#f23f43"
RED_DIM        = "#b72d30"
BLUE           = "#00a8fc"

# Scrollbar
SCROLL_TRACK   = "#2b2d31"
SCROLL_HANDLE  = "#686b74"
SCROLL_HOVER   = "#888b94"
SCROLL_PRESSED = "#a0a3ab"

# Reusable QSS snippet — append to any per-tab stylesheet that overrides
# QWidget background-color, so scrollbar colours stay consistent.
SCROLLBAR_QSS = f"""
    QScrollBar:vertical {{
        background-color: {SCROLL_TRACK};
        width: 12px;
        border-radius: 6px;
        margin: 0;
    }}
    QScrollBar::handle:vertical {{
        background-color: {SCROLL_HANDLE};
        min-height: 30px;
        border-radius: 6px;
    }}
    QScrollBar::handle:vertical:hover {{
        background-color: {SCROLL_HOVER};
    }}
    QScrollBar::handle:vertical:pressed {{
        background-color: {SCROLL_PRESSED};
    }}
    QScrollBar::sub-page:vertical, QScrollBar::add-page:vertical {{
        background: none;
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
        height: 0px;
        background: none;
    }}
    QScrollBar:horizontal {{
        background-color: {SCROLL_TRACK};
        height: 12px;
        border-radius: 6px;
        margin: 0;
    }}
    QScrollBar::handle:horizontal {{
        background-color: {SCROLL_HANDLE};
        min-width: 30px;
        border-radius: 6px;
    }}
    QScrollBar::handle:horizontal:hover {{
        background-color: {SCROLL_HOVER};
    }}
    QScrollBar::handle:horizontal:pressed {{
        background-color: {SCROLL_PRESSED};
    }}
    QScrollBar::sub-page:horizontal, QScrollBar::add-page:horizontal {{
        background: none;
    }}
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
        width: 0px;
        background: none;
    }}
"""


STYLESHEET = f"""
/* ── Global ──────────────────────────────────────────────────────────── */

QWidget {{
    background-color: {BG_DARK};
    color: {TEXT_PRIMARY};
    font-family: "Segoe UI", "Noto Sans", sans-serif;
    font-size: 10pt;
    selection-background-color: {ACCENT};
    selection-color: #ffffff;
    border: none;
    outline: none;
}}

QMainWindow, QDialog {{
    background-color: {BG_DEEPEST};
}}

/* ── Tab bar ─────────────────────────────────────────────────────────── */

QTabWidget::pane {{
    background-color: {BG_DARK};
    border: 1px solid {BORDER_SUBTLE};
    border-top: none;
}}

QTabBar {{
    background-color: {BG_DEEPEST};
    qproperty-drawBase: 0;
}}

QTabBar::tab {{
    background-color: transparent;
    color: {TEXT_MUTED};
    padding: 8px 16px;
    margin-right: 2px;
    border-bottom: 2px solid transparent;
    font-weight: 500;
}}

QTabBar::tab:hover {{
    color: {TEXT_SECONDARY};
    background-color: {BG_HOVER};
    border-radius: 4px 4px 0 0;
}}

QTabBar::tab:selected {{
    color: {TEXT_PRIMARY};
    border-bottom: 2px solid {ACCENT};
    font-weight: 600;
}}

/* ── Buttons ─────────────────────────────────────────────────────────── */

QPushButton {{
    background-color: {BG_ELEVATED};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER_SUBTLE};
    border-radius: 4px;
    padding: 5px 14px;
    font-weight: 500;
    min-height: 22px;
}}

QPushButton:hover {{
    background-color: {BG_HOVER};
    border-color: {BORDER_NORMAL};
}}

QPushButton:pressed {{
    background-color: {BG_SELECTED};
}}

QPushButton:disabled {{
    color: {TEXT_MUTED};
    background-color: {BG_MEDIUM};
    border-color: {BORDER_SUBTLE};
}}

QPushButton:checked {{
    background-color: {ACCENT};
    color: #ffffff;
    border-color: {ACCENT_HOVER};
}}

QPushButton:checked:hover {{
    background-color: {ACCENT_HOVER};
}}

/* Primary / accent buttons — keep existing green/red styleSheets working
   but make un-styled buttons default to Discord accent on certain names */

/* ── Inputs ──────────────────────────────────────────────────────────── */

QLineEdit, QTextEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox,
QComboBox, QDateEdit, QTimeEdit {{
    background-color: {BG_DEEPEST};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER_NORMAL};
    border-radius: 4px;
    padding: 4px 8px;
    selection-background-color: {ACCENT};
}}

QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus,
QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border-color: {ACCENT};
}}

QLineEdit:disabled, QTextEdit:disabled, QPlainTextEdit:disabled {{
    color: {TEXT_MUTED};
    background-color: {BG_MEDIUM};
}}

/* ── ComboBox ────────────────────────────────────────────────────────── */

QComboBox {{
    padding-right: 24px;
    min-height: 24px;
}}

QComboBox::drop-down {{
    border: none;
    width: 22px;
}}

QComboBox::down-arrow {{
    width: 10px;
    height: 10px;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid {TEXT_SECONDARY};
}}

QComboBox QAbstractItemView {{
    background-color: {BG_ELEVATED};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER_NORMAL};
    border-radius: 4px;
    selection-background-color: {ACCENT};
    outline: none;
    padding: 4px;
}}

QComboBox QAbstractItemView::item {{
    min-height: 24px;
    padding: 4px 8px;
    border-radius: 3px;
}}

QComboBox QAbstractItemView::item:hover {{
    background-color: {BG_HOVER};
}}

/* ── Group boxes ─────────────────────────────────────────────────────── */

QGroupBox {{
    background-color: {BG_MEDIUM};
    border: 1px solid {BORDER_SUBTLE};
    border-radius: 6px;
    margin-top: 10px;
    padding: 8px 8px 6px 8px;
    font-weight: 600;
    color: {TEXT_SECONDARY};
    font-size: 9pt;
    text-transform: uppercase;
    letter-spacing: 0.5px;
}}

QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    padding: 0 6px;
    left: 10px;
    color: {TEXT_MUTED};
}}

/* ── Tables ──────────────────────────────────────────────────────────── */

QTableWidget, QTableView {{
    background-color: {BG_MEDIUM};
    alternate-background-color: {BG_ELEVATED};
    gridline-color: {BORDER_SUBTLE};
    border: 1px solid {BORDER_SUBTLE};
    border-radius: 4px;
    color: {TEXT_PRIMARY};
}}

QTableWidget::item, QTableView::item {{
    padding: 4px 8px;
    border: none;
}}

QTableWidget::item:selected, QTableView::item:selected {{
    background-color: {ACCENT};
    color: #ffffff;
}}

QTableWidget::item:hover, QTableView::item:hover {{
    background-color: {BG_HOVER};
}}

QHeaderView::section {{
    background-color: {BG_DEEPEST};
    color: {TEXT_MUTED};
    font-weight: 600;
    font-size: 9pt;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    padding: 6px 8px;
    border: none;
    border-bottom: 1px solid {BORDER_SUBTLE};
    border-right: 1px solid {BORDER_SUBTLE};
}}

QHeaderView::section:last {{
    border-right: none;
}}

/* ── Scrollbars ──────────────────────────────────────────────────────── */

QScrollBar:vertical {{
    background-color: {SCROLL_TRACK};
    width: 12px;
    border-radius: 6px;
    margin: 0;
}}

QScrollBar::handle:vertical {{
    background-color: {SCROLL_HANDLE};
    min-height: 30px;
    border-radius: 6px;
}}

QScrollBar::handle:vertical:hover {{
    background-color: {SCROLL_HOVER};
}}

QScrollBar::handle:vertical:pressed {{
    background-color: {SCROLL_PRESSED};
}}

QScrollBar::sub-page:vertical, QScrollBar::add-page:vertical {{
    background: none;
}}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0px;
    background: none;
}}

QScrollBar:horizontal {{
    background-color: {SCROLL_TRACK};
    height: 12px;
    border-radius: 6px;
    margin: 0;
}}

QScrollBar::handle:horizontal {{
    background-color: {SCROLL_HANDLE};
    min-width: 30px;
    border-radius: 6px;
}}

QScrollBar::handle:horizontal:hover {{
    background-color: {SCROLL_HOVER};
}}

QScrollBar::handle:horizontal:pressed {{
    background-color: {SCROLL_PRESSED};
}}

QScrollBar::sub-page:horizontal, QScrollBar::add-page:horizontal {{
    background: none;
}}

QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0px;
    background: none;
}}

/* ── Splitter ────────────────────────────────────────────────────────── */

QSplitter::handle {{
    background-color: {BORDER_SUBTLE};
    width: 2px;
    height: 2px;
}}

QSplitter::handle:hover {{
    background-color: {ACCENT};
}}

/* ── Status bar ──────────────────────────────────────────────────────── */

QStatusBar {{
    background-color: {BG_DEEPEST};
    color: {TEXT_MUTED};
    border-top: 1px solid {BORDER_SUBTLE};
    font-size: 9pt;
    padding: 2px 8px;
}}

QStatusBar QLabel {{
    color: {TEXT_MUTED};
    background-color: transparent;
    font-size: 9pt;
}}

/* ── Labels ──────────────────────────────────────────────────────────── */

QLabel {{
    background-color: transparent;
    color: {TEXT_PRIMARY};
}}

/* ── Checkboxes & Radio buttons ──────────────────────────────────────── */

QCheckBox, QRadioButton {{
    color: {TEXT_PRIMARY};
    spacing: 6px;
}}

QCheckBox::indicator, QRadioButton::indicator {{
    width: 16px;
    height: 16px;
    border-radius: 3px;
    border: 2px solid {BORDER_NORMAL};
    background-color: {BG_DEEPEST};
}}

QCheckBox::indicator:checked {{
    background-color: {ACCENT};
    border-color: {ACCENT};
}}

QCheckBox::indicator:hover, QRadioButton::indicator:hover {{
    border-color: {ACCENT};
}}

QRadioButton::indicator {{
    border-radius: 8px;
}}

QRadioButton::indicator:checked {{
    background-color: {ACCENT};
    border-color: {ACCENT};
}}

/* ── Sliders ─────────────────────────────────────────────────────────── */

QSlider::groove:horizontal {{
    height: 4px;
    background-color: {BG_ELEVATED};
    border-radius: 2px;
}}

QSlider::handle:horizontal {{
    background-color: #ffffff;
    width: 14px;
    height: 14px;
    border-radius: 7px;
    margin: -5px 0;
}}

QSlider::sub-page:horizontal {{
    background-color: {ACCENT};
    border-radius: 2px;
}}

/* ── Tooltips ────────────────────────────────────────────────────────── */

QToolTip {{
    background-color: {BG_DEEPEST};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER_NORMAL};
    border-radius: 4px;
    padding: 4px 8px;
    font-size: 9pt;
}}

/* ── Scroll areas ────────────────────────────────────────────────────── */

QScrollArea {{
    border: none;
    background-color: transparent;
}}

QScrollArea > QWidget > QWidget {{
    background-color: transparent;
}}

/* ── Menu bar / menus ────────────────────────────────────────────────── */

QMenuBar {{
    background-color: {BG_DEEPEST};
    color: {TEXT_PRIMARY};
    border-bottom: 1px solid {BORDER_SUBTLE};
    padding: 2px;
}}

QMenuBar::item {{
    padding: 4px 10px;
    border-radius: 3px;
    background-color: transparent;
}}

QMenuBar::item:selected {{
    background-color: {BG_HOVER};
}}

QMenu {{
    background-color: {BG_ELEVATED};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER_NORMAL};
    border-radius: 6px;
    padding: 4px;
}}

QMenu::item {{
    padding: 6px 24px 6px 12px;
    border-radius: 3px;
}}

QMenu::item:selected {{
    background-color: {ACCENT};
    color: #ffffff;
}}

QMenu::separator {{
    height: 1px;
    background-color: {BORDER_SUBTLE};
    margin: 4px 8px;
}}

/* ── Progress bar ────────────────────────────────────────────────────── */

QProgressBar {{
    background-color: {BG_ELEVATED};
    border-radius: 4px;
    height: 8px;
    text-align: center;
    color: transparent;
}}

QProgressBar::chunk {{
    background-color: {ACCENT};
    border-radius: 4px;
}}

/* ── Spin box buttons ────────────────────────────────────────────────── */

QSpinBox::up-button, QSpinBox::down-button,
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
    background-color: {BG_ELEVATED};
    border: none;
    width: 18px;
    border-radius: 2px;
}}

QSpinBox::up-button:hover, QSpinBox::down-button:hover,
QDoubleSpinBox::up-button:hover, QDoubleSpinBox::down-button:hover {{
    background-color: {BG_HOVER};
}}

QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-bottom: 5px solid {TEXT_SECONDARY};
    width: 0; height: 0;
}}

QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid {TEXT_SECONDARY};
    width: 0; height: 0;
}}

/* ── Frame / panel separators ────────────────────────────────────────── */

QFrame[frameShape="4"],
QFrame[frameShape="5"] {{
    color: {BORDER_SUBTLE};
}}
"""


# ── Reusable button accent styles ──────────────────────────────────────────
# Use these instead of defining BTN_GREEN / BTN_RED locally in each module.
BTN_SUCCESS = f"background-color:{GREEN_DIM};color:white;border-color:{GREEN};"
BTN_DANGER  = f"background-color:{RED_DIM};color:white;border-color:{RED};"
BTN_WARNING = f"background-color:#9A6700;color:white;border-color:{YELLOW};"
BTN_ACCENT  = f"background-color:{ACCENT};color:white;border-color:{ACCENT_HOVER};"
BTN_NORMAL  = ""  # revert to global stylesheet defaults

# ── Indicator dot styles (12 × 12 px circles) ─────────────────────────────
_IND = "border-radius:6px;min-width:12px;max-width:12px;min-height:12px;max-height:12px;"
IND_GREEN  = f"background:{GREEN};{_IND}"
IND_RED    = f"background:{RED};{_IND}"
IND_AMBER  = f"background:{YELLOW};{_IND}"
IND_OFF    = f"background:{BORDER_SUBTLE};{_IND}"

# ── Status colours (process/connection state indicators) ───────────────────
STATE_COLORS = {
    "running":      GREEN,
    "stopped":      "#95A5A6",
    "crashed":      RED,
    "connecting":   YELLOW,
    "connected":    GREEN,
    "disconnected": "#95A5A6",
    "busy":         "#E67E22",
    "ptt":          RED,
    "idle":         BLUE,
    "simulator":    BLUE,
}

# ── Disabled widget style (dark-theme safe) ────────────────────────────────
# NEVER use #f0f0f0 or #aaa — those are light-theme colours.
DISABLED_COMBO_QSS = f"QComboBox {{ color: {TEXT_MUTED}; background: {BG_MEDIUM}; }}"
DISABLED_BTN_QSS   = f"QPushButton {{ color: {TEXT_MUTED}; background: {BG_MEDIUM}; }}"

# ── Spacing tokens (8 px base grid) ───────────────────────────────────────
SPACE_XS = 4
SPACE_SM = 8
SPACE_MD = 12
SPACE_LG = 16
SPACE_XL = 24

# ── Helper text style (used on QLabel annotations across settings pages) ──
HELPER_QSS = f"color:{TEXT_MUTED}; font-size:9pt;"
WARNING_QSS = f"color:{YELLOW};"


def apply_theme(app: QApplication) -> None:
    """Apply the Discord-inspired dark theme to the entire application."""
    # Fusion is required for full QSS control over scrollbar colours.
    # The default Windows style engine ignores most QSS colour rules
    # for scrollbar sub-controls (handle, sub-page, add-page).
    app.setStyle(QStyleFactory.create("Fusion"))
    app.setStyleSheet(STYLESHEET)

    # Set the application-level font
    font = QFont("Segoe UI", 10)
    font.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
    app.setFont(font)

    # Set QPalette for widgets that don't fully respect stylesheets
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window,          QColor(BG_DARK))
    palette.setColor(QPalette.ColorRole.WindowText,      QColor(TEXT_PRIMARY))
    palette.setColor(QPalette.ColorRole.Base,            QColor(BG_DEEPEST))
    palette.setColor(QPalette.ColorRole.AlternateBase,   QColor(BG_ELEVATED))
    palette.setColor(QPalette.ColorRole.Text,            QColor(TEXT_PRIMARY))
    palette.setColor(QPalette.ColorRole.BrightText,      QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.Button,          QColor(BG_ELEVATED))
    palette.setColor(QPalette.ColorRole.ButtonText,      QColor(TEXT_PRIMARY))
    palette.setColor(QPalette.ColorRole.Highlight,       QColor(ACCENT))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.Link,            QColor(TEXT_LINK))
    palette.setColor(QPalette.ColorRole.ToolTipBase,     QColor(BG_DEEPEST))
    palette.setColor(QPalette.ColorRole.ToolTipText,     QColor(TEXT_PRIMARY))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(TEXT_MUTED))

    # Disabled variants
    palette.setColor(QPalette.ColorGroup.Disabled,
                     QPalette.ColorRole.Text,       QColor(TEXT_MUTED))
    palette.setColor(QPalette.ColorGroup.Disabled,
                     QPalette.ColorRole.ButtonText, QColor(TEXT_MUTED))
    palette.setColor(QPalette.ColorGroup.Disabled,
                     QPalette.ColorRole.WindowText, QColor(TEXT_MUTED))

    app.setPalette(palette)
