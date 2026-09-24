"""The photo-lab counter: colours, type, the base style and the stylesheet.

Sorting is filing prints into envelopes at a lab counter, so the interface is
built from that counter's materials. The window is a warm near-black counter
with a darker mat under the print; photographs sit on it as prints with a paper
border; the ten keys are kraft envelopes; the current and selected frames are
boxed in red grease pencil, the way chosen frames are marked on a contact sheet.

Two rules keep the states apart: grease pencil means "this one" (the current
frame, a selection, the active tab) and a paper fill means "the thing to do"
(the primary button). Every number is set in Bahnschrift with tabular figures,
so a counter never shifts as it ticks; everything else is the system UI face.
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPalette, QPen
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QProxyStyle, QStyle,
                               QStyleFactory)

# -- the counter --------------------------------------------------------
COUNTER = "#1C1916"          # the window
MAT = "#141210"              # under the print and the contact sheet
RAISED = "#25211D"           # fields, menus, panels
RAISED_HOVER = "#2E2924"
PRESSED = "#3A332C"          # pressed controls, chosen rows
LINE = "#352F29"             # hairlines that only divide
LINE_CONTROL = "#7A6E60"     # outlines that identify a control (3:1 on the counter)

# -- paper --------------------------------------------------------------
PAPER = "#EFE9DD"            # print borders, primary text, the primary button
PAPER_BRIGHT = "#F7F2E8"
PAPER_PRESSED = "#DCD4C6"
PAPER_DIM = "#BDB3A4"        # secondary text
FAINT = "#978C7E"            # captions and placeholders (4.5:1 on fields)
DISABLED = "#6A5F53"

# -- envelopes and ink --------------------------------------------------
KRAFT = "#BD9A6F"
KRAFT_HOVER = "#C7A67C"
KRAFT_EDGE = "#8E7050"
KRAFT_RULE = "#7A5E42"
INK = "#2A1F16"              # printed ink on kraft and paper
INK_SOFT = "#5E4F40"         # secondary ink on paper
BALLPOINT = "#1A2B63"        # handwriting on kraft
BALLPOINT_LIGHT = "#8DA6E8"  # focus and success on the counter
STAMP = "#7E1F19"            # the stamp ring on kraft
GREASE = "#E0594D"           # grease pencil: current, selected, errors
AMBER = "#D9A866"            # warnings

#: Colour labels are dot stickers, in the order Alt+1..5 assigns them.
LABEL_COLOURS = {
    "red": "#E5534B",
    "yellow": "#E2B340",
    "green": "#57AB5A",
    "blue": "#539BF5",
    "purple": "#986EE2",
}

UI_FAMILY = "Microsoft YaHei UI"
NUMERAL_FAMILY = "Bahnschrift"
MONO_STACK = '"Cascadia Mono", "Consolas", monospace'

#: Height, compact height, base font and padding per density.
#: English strings run about 60% longer than Chinese, which is what "roomy"
#: buys room for.
DENSITY = {
    "compact": {"button": 28, "compact": 24, "font": 12, "pad": 11},
    "standard": {"button": 32, "compact": 27, "font": 13, "pad": 14},
    "roomy": {"button": 36, "compact": 30, "font": 14, "pad": 17},
}


def metrics(density: str = "standard") -> dict:
    return DENSITY.get(density, DENSITY["standard"])


#: Fonts are asked for on every paint of a counter; building one resolves the
#: family each time, so each size and weight is built once.
_FONTS: dict[tuple, QFont] = {}
_METRICS: dict[tuple, QFontMetricsF] = {}


def ui_font(pixels: int, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
    key = ("ui", int(pixels), weight)
    font = _FONTS.get(key)
    if font is None:
        font = QFont(UI_FAMILY)
        font.setPixelSize(max(6, int(pixels)))
        font.setWeight(weight)
        _FONTS[key] = font
    return QFont(font)


def numeral_font(pixels: int, weight: QFont.Weight = QFont.Weight.DemiBold,
                 condensed: bool = False) -> QFont:
    """Bahnschrift with tabular figures: counts that tick never jump sideways."""
    key = ("numeral", int(pixels), weight, condensed)
    font = _FONTS.get(key)
    if font is None:
        font = QFont(NUMERAL_FAMILY)
        font.setFamilies([NUMERAL_FAMILY, "Segoe UI", UI_FAMILY])
        font.setPixelSize(max(6, int(pixels)))
        font.setWeight(weight)
        if condensed:
            font.setStretch(QFont.Stretch.SemiCondensed)
        try:
            font.setFeature(QFont.Tag("tnum"), 1)
        except (AttributeError, TypeError):     # pragma: no cover - Qt before 6.7
            pass
        _FONTS[key] = font
    return QFont(font)


def font_metrics(kind: str, pixels: int, weight: QFont.Weight) -> QFontMetricsF:
    """Metrics for `ui_font` or `numeral_font`, kept alongside the font itself."""
    key = (kind, int(pixels), weight)
    metrics = _METRICS.get(key)
    if metrics is None:
        font = numeral_font(pixels, weight) if kind == "numeral" else ui_font(pixels, weight)
        metrics = QFontMetricsF(font)
        _METRICS[key] = metrics
    return metrics


def palette() -> QPalette:
    """What Fusion draws with wherever the stylesheet does not reach."""
    result = QPalette()
    roles = {
        QPalette.ColorRole.Window: COUNTER, QPalette.ColorRole.WindowText: PAPER,
        QPalette.ColorRole.Base: RAISED, QPalette.ColorRole.AlternateBase: "#211D1A",
        QPalette.ColorRole.Text: PAPER, QPalette.ColorRole.Button: RAISED,
        QPalette.ColorRole.ButtonText: PAPER, QPalette.ColorRole.BrightText: PAPER_BRIGHT,
        QPalette.ColorRole.Highlight: PRESSED, QPalette.ColorRole.HighlightedText: PAPER,
        QPalette.ColorRole.PlaceholderText: FAINT, QPalette.ColorRole.ToolTipBase: PAPER,
        QPalette.ColorRole.ToolTipText: INK, QPalette.ColorRole.Link: BALLPOINT_LIGHT,
        QPalette.ColorRole.Light: PRESSED, QPalette.ColorRole.Midlight: RAISED_HOVER,
        QPalette.ColorRole.Mid: LINE_CONTROL, QPalette.ColorRole.Dark: MAT,
        QPalette.ColorRole.Shadow: "#000000",
    }
    for role, colour in roles.items():
        result.setColor(role, QColor(colour))
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text,
                 QPalette.ColorRole.ButtonText):
        result.setColor(QPalette.ColorGroup.Disabled, role, QColor(DISABLED))
    return result


class CounterStyle(QProxyStyle):
    """Fusion, with the small marks drawn in the counter's own hand.

    A stylesheet can colour a check box but cannot draw a tick without an image
    file, and the dark palette leaves Fusion's own boxes nearly invisible.
    """

    def __init__(self) -> None:
        super().__init__(QStyleFactory.create("Fusion"))

    def pixelMetric(self, metric, option=None, widget=None) -> int:  # noqa: N802 - Qt naming
        if metric in (QStyle.PixelMetric.PM_IndicatorWidth,
                      QStyle.PixelMetric.PM_IndicatorHeight):
            return 16
        return super().pixelMetric(metric, option, widget)

    def drawPrimitive(self, element, option, painter, widget=None) -> None:  # noqa: N802
        state = option.state
        enabled = bool(state & QStyle.StateFlag.State_Enabled)
        if element == QStyle.PrimitiveElement.PE_IndicatorCheckBox:
            self._check_box(painter, QRectF(option.rect), state, enabled)
            return
        if element in _ARROWS:
            colour = QColor(PAPER_DIM if enabled else DISABLED)
            _chevron(painter, QRectF(option.rect), _ARROWS[element], colour)
            return
        if element == QStyle.PrimitiveElement.PE_FrameFocusRect:
            # Only where the keyboard moved focus: a ring left on whatever was
            # clicked, or on a dialog's first button, reads as an accent.
            if not state & QStyle.StateFlag.State_KeyboardFocusChange:
                return
            if isinstance(widget, QAbstractItemView):
                # The cell the keyboard is on: a paper hairline, quieter than a
                # control's ring, since the row's own fill already marks the choice.
                painter.save()
                painter.setPen(QPen(QColor(PAPER_DIM), 1))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRect(QRectF(option.rect).adjusted(0.5, 0.5, -1.5, -1.5))
                painter.restore()
                return
            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(QPen(QColor(BALLPOINT_LIGHT), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(QRectF(option.rect).adjusted(0.5, 0.5, -0.5, -0.5), 3, 3)
            painter.restore()
            return
        super().drawPrimitive(element, option, painter, widget)

    @staticmethod
    def _check_box(painter: QPainter, rect: QRectF, state, enabled: bool) -> None:
        side = min(rect.width(), rect.height()) - 1
        box = QRectF(rect.center().x() - side / 2, rect.center().y() - side / 2, side, side)
        on = bool(state & QStyle.StateFlag.State_On)
        partial = bool(state & QStyle.StateFlag.State_NoChange)
        hover = bool(state & QStyle.StateFlag.State_MouseOver)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if not enabled:
            painter.setOpacity(0.45)
        if on or partial:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(PAPER if not hover else PAPER_BRIGHT))
            painter.drawRoundedRect(box, 3, 3)
            pen = QPen(QColor(INK), max(1.6, side / 8))
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            if partial:
                y = box.center().y()
                painter.drawLine(QPointF(box.left() + side * 0.28, y),
                                 QPointF(box.right() - side * 0.28, y))
            else:
                tick = QPainterPath(QPointF(box.left() + side * 0.24, box.top() + side * 0.53))
                tick.lineTo(QPointF(box.left() + side * 0.43, box.top() + side * 0.71))
                tick.lineTo(QPointF(box.left() + side * 0.77, box.top() + side * 0.31))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawPath(tick)
        else:
            painter.setPen(QPen(QColor(PAPER_DIM if hover else LINE_CONTROL), 1))
            painter.setBrush(QColor(RAISED))
            painter.drawRoundedRect(box.adjusted(0.5, 0.5, -0.5, -0.5), 3, 3)
        painter.restore()


_ARROWS = {
    QStyle.PrimitiveElement.PE_IndicatorArrowDown: "down",
    QStyle.PrimitiveElement.PE_IndicatorArrowUp: "up",
    QStyle.PrimitiveElement.PE_IndicatorArrowLeft: "left",
    QStyle.PrimitiveElement.PE_IndicatorArrowRight: "right",
    QStyle.PrimitiveElement.PE_IndicatorSpinDown: "down",
    QStyle.PrimitiveElement.PE_IndicatorSpinUp: "up",
}


def _chevron(painter: QPainter, rect: QRectF, direction: str, colour: QColor) -> None:
    size = max(4.0, min(rect.width(), rect.height()) * 0.5)
    half = size / 2
    c = rect.center()
    points = {
        "down": [(-half, -half / 2), (0, half / 2), (half, -half / 2)],
        "up": [(-half, half / 2), (0, -half / 2), (half, half / 2)],
        "left": [(half / 2, -half), (-half / 2, 0), (half / 2, half)],
        "right": [(-half / 2, -half), (half / 2, 0), (-half / 2, half)],
    }[direction]
    path = QPainterPath(QPointF(c.x() + points[0][0], c.y() + points[0][1]))
    for dx, dy in points[1:]:
        path.lineTo(QPointF(c.x() + dx, c.y() + dy))
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pen = QPen(colour, 1.5)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawPath(path)
    painter.restore()


def apply(app: QApplication, density: str = "standard") -> None:
    """Style, palette, font and stylesheet, in the order Qt needs them."""
    if app.property("qingjianThemeDensity") == density:
        return
    if not isinstance(app.style(), CounterStyle):
        app.setStyle(CounterStyle())
    app.setPalette(palette())
    app.setFont(ui_font(metrics(density)["font"]))
    app.setStyleSheet(stylesheet(density))
    app.setProperty("qingjianThemeDensity", density)


def stylesheet(density: str = "standard") -> str:
    m = metrics(density)
    f = m["font"]
    return f"""
QWidget#root, QDialog {{ background: {COUNTER}; }}
QToolTip {{ background: {PAPER}; color: {INK}; border: 1px solid #CFC6B6; padding: 5px 8px; }}

QFrame#stage {{ background: {MAT}; border: none; border-radius: 3px; }}
QFrame#settingsCard {{ background: #211D1A; border: 1px solid {LINE}; border-radius: 4px; }}
QFrame#settingsRow {{ background: #211D1A; border: 1px solid {LINE}; border-radius: 4px; }}
QLabel#note {{ background: #211D1A; border: 1px solid {LINE}; border-radius: 4px;
    padding: 10px 12px; color: {PAPER_DIM}; font-size: {f - 2}px; }}
QLabel#pathBox {{ background: {RAISED}; border: 1px solid {LINE}; border-radius: 4px;
    padding: 9px 12px; }}
QFrame#videoControls {{ background: {COUNTER}; border: none; border-top: 1px solid {LINE}; }}
QFrame#separator {{ background: {LINE}; border: none; }}

QLabel#brand {{ font-size: {f + 5}px; font-weight: 800; color: {PAPER}; }}
/* Qt stylesheets have no letter-spacing; the window sets it on the font. */
QLabel#brandSubtitle {{ font-size: 8px; color: {FAINT}; }}
QLabel#dialogTitle {{ font-size: {f + 7}px; font-weight: 700; color: {PAPER}; }}
QLabel#dialogSubtitle, QLabel#caption {{ font-size: {f - 2}px; color: {FAINT}; }}
QLabel#sectionTitle {{ font-size: {f + 1}px; font-weight: 700; color: {PAPER}; }}
QLabel#rowTitle {{ font-weight: 600; color: {PAPER}; }}
QLabel#filename {{ font-size: {f}px; font-weight: 700; color: {PAPER}; }}
QLabel#fileDetail {{ font-size: {f - 2}px; color: {PAPER_DIM}; }}
QLabel#mono, QLineEdit#mono {{ font-family: {MONO_STACK}; color: {PAPER_DIM}; }}
QLabel#tag {{ border: 1px solid {LINE_CONTROL}; border-radius: 3px; padding: 1px 6px;
    color: {PAPER_DIM}; font-size: {f - 3}px; font-weight: 700; }}
QLabel#markTag {{ border: 1px solid {GREASE}; border-radius: 3px; padding: 1px 7px;
    color: {GREASE}; font-size: {f - 2}px; font-weight: 700; }}
QLabel#statusBar {{ color: {FAINT}; font-size: {f - 2}px; }}
QLabel#statusBar[tone="success"] {{ color: {BALLPOINT_LIGHT}; }}
QLabel#statusBar[tone="warning"] {{ color: {AMBER}; }}
QLabel#statusBar[tone="error"] {{ color: {GREASE}; }}
QLabel#timeLabel {{ color: {PAPER_DIM}; font-size: {f - 3}px; min-width: 86px; }}
QLabel#slipTitle {{ font-size: {f + 5}px; font-weight: 700; color: {INK}; }}
QLabel#slipText {{ font-size: {f - 1}px; color: {INK_SOFT}; }}
QLabel#slipHint {{ font-size: {f - 2}px; color: {INK_SOFT}; }}

QLineEdit, QSpinBox, QDoubleSpinBox {{ background: {RAISED}; border: 1px solid {LINE_CONTROL};
    border-radius: 4px; padding: 4px 9px; color: {PAPER};
    selection-background-color: {PAPER_DIM}; selection-color: {INK}; }}
QLineEdit:hover, QSpinBox:hover, QDoubleSpinBox:hover {{ border-color: {PAPER_DIM}; }}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus {{ border-color: {BALLPOINT_LIGHT}; }}
QLineEdit:disabled, QSpinBox:disabled {{ color: {DISABLED}; border-color: {LINE}; background: transparent; }}
QLineEdit#search {{ padding: 2px 8px; min-height: {m['compact'] - 6}px; }}

QPushButton {{ min-height: {m['button']}px; padding: 0 {m['pad']}px; border-radius: 4px;
    font-weight: 400; background: {RAISED}; border: 1px solid {LINE_CONTROL}; color: {PAPER}; }}
QPushButton:hover {{ background: {RAISED_HOVER}; border-color: {PAPER_DIM}; }}
QPushButton:pressed {{ background: {PRESSED}; }}
QPushButton:disabled {{ background: transparent; border-color: {LINE}; color: {DISABLED}; }}
QPushButton#primaryButton {{ background: {PAPER}; border: 1px solid {PAPER}; color: {INK};
    font-weight: 700; }}
QPushButton#primaryButton:hover {{ background: {PAPER_BRIGHT}; border-color: {PAPER_BRIGHT}; }}
QPushButton#primaryButton:pressed {{ background: {PAPER_PRESSED}; }}
QPushButton#primaryButton:disabled {{ background: {PRESSED}; border-color: {PRESSED}; color: {FAINT}; }}
QPushButton#inkButton {{ background: {INK}; border: 1px solid {INK}; color: {PAPER};
    font-weight: 700; padding: 0 18px; }}
QPushButton#inkButton:hover {{ background: #3A2C20; border-color: #3A2C20; }}
QPushButton#compactButton {{ min-height: {m['compact']}px; padding: 0 10px; font-size: {f - 1}px; }}
QPushButton#quietButton {{ min-height: {m['compact']}px; padding: 0 8px; font-size: {f - 1}px;
    background: transparent; border: 1px solid transparent; color: {PAPER_DIM}; }}
QPushButton#quietButton:hover {{ background: {RAISED_HOVER}; color: {PAPER}; border-color: {LINE}; }}
QPushButton#quietButton:disabled {{ color: {DISABLED}; background: transparent; border-color: transparent; }}
QPushButton#dangerButton {{ background: transparent; border: 1px solid {GREASE}; color: {GREASE}; }}
QPushButton#dangerButton:hover {{ background: #3A201C; }}
QPushButton#warningButton {{ min-height: {m['compact']}px; padding: 0 10px; font-size: {f - 1}px;
    background: transparent; border: 1px solid {AMBER}; color: {AMBER}; }}
QPushButton#reviewButton {{ min-height: {m['compact']}px; padding: 0 10px; font-size: {f - 1}px; }}
QPushButton#reviewButton[active="true"] {{ border: 1px solid {GREASE}; color: {PAPER}; }}

QPushButton#segment {{ min-height: {m['compact'] - 4}px; padding: 0 11px; border-radius: 0;
    border: none; border-bottom: 2px solid transparent; background: transparent;
    color: {PAPER_DIM}; font-size: {f - 1}px; font-weight: 600; }}
QPushButton#segment:hover {{ color: {PAPER}; background: {RAISED_HOVER}; }}
QPushButton#segment:checked {{ color: {PAPER}; border-bottom: 2px solid {GREASE}; font-weight: 700; }}
QFrame#segmentBar {{ background: transparent; border: none; border-bottom: 1px solid {LINE}; }}

QToolButton {{ color: {PAPER_DIM}; }}
QToolButton#iconButton, QToolButton#navButton {{
    background: transparent; border: 1px solid transparent; border-radius: 4px;
    min-width: {m['compact']}px; min-height: {m['compact']}px; }}
QToolButton#iconButton:hover, QToolButton#navButton:hover {{
    background: {RAISED_HOVER}; border-color: {LINE}; }}
QToolButton#iconButton:pressed, QToolButton#navButton:pressed,
QToolButton#iconButton:checked {{ background: {PRESSED}; }}
QToolButton#iconButton:focus, QToolButton#navButton:focus {{
    border-color: {BALLPOINT_LIGHT}; }}
QToolButton#menuButton {{ background: transparent; border: 1px solid transparent; border-radius: 4px;
    min-height: {m['compact']}px; padding: 0 8px; color: {PAPER_DIM}; font-size: {f - 1}px; }}
QToolButton#menuButton:hover {{ background: {RAISED_HOVER}; border-color: {LINE}; color: {PAPER}; }}
QToolButton#menuButton::menu-indicator {{ image: none; width: 0; }}
QToolButton#roundButton {{ background: transparent; border: 1px solid {LINE_CONTROL}; border-radius: 4px;
    min-width: 30px; min-height: 30px; }}
QToolButton#roundButton:hover {{ background: {RAISED_HOVER}; border-color: {PAPER_DIM}; }}

QComboBox {{ background: {RAISED}; border: 1px solid {LINE_CONTROL}; border-radius: 4px;
    padding: 2px 8px; color: {PAPER}; min-height: {m['compact'] - 6}px; }}
QComboBox:hover {{ border-color: {PAPER_DIM}; }}
QComboBox QAbstractItemView {{ background: {RAISED}; border: 1px solid {LINE_CONTROL};
    color: {PAPER}; selection-background-color: {PRESSED}; selection-color: {PAPER}; outline: 0; }}

QCheckBox {{ color: {PAPER_DIM}; spacing: 7px; }}
QCheckBox:hover {{ color: {PAPER}; }}
QRadioButton {{ color: {PAPER_DIM}; spacing: 8px; }}

QProgressBar {{ background: {LINE}; border: none; border-radius: 2px; max-height: 4px;
    text-align: center; color: transparent; }}
QProgressBar::chunk {{ background: {KRAFT}; border-radius: 2px; }}
QProgressBar#quotaBar::chunk {{ background: {PAPER_DIM}; }}
QProgressBar#quotaBar[full="true"]::chunk {{ background: {AMBER}; }}
QProgressDialog QProgressBar {{ max-height: 6px; }}

QSlider::groove:horizontal {{ height: 3px; background: {PRESSED}; border-radius: 1px; }}
QSlider::sub-page:horizontal {{ background: {PAPER_DIM}; border-radius: 1px; }}
QSlider::handle:horizontal {{ background: {PAPER}; width: 12px; margin: -5px 0; border-radius: 6px; }}
QSlider::handle:horizontal:hover {{ background: {PAPER_BRIGHT}; }}

QTableWidget, QTextEdit, QPlainTextEdit {{ background: {COUNTER}; alternate-background-color: #211D1A;
    border: 1px solid {LINE}; gridline-color: {LINE}; color: {PAPER};
    selection-background-color: {PRESSED}; selection-color: {PAPER}; }}
QHeaderView::section {{ background: {RAISED}; color: {PAPER_DIM}; padding: 6px; border: none;
    border-right: 1px solid {LINE}; border-bottom: 1px solid {LINE}; font-weight: 600; }}
QTableCornerButton::section {{ background: {RAISED}; border: none; }}
QListWidget {{ background: {COUNTER}; border: none; color: {PAPER}; outline: 0; }}
QListWidget#grid {{ background: {MAT}; border: none; padding: 8px; }}
QListWidget#filmstrip {{ background: transparent; border: none; }}
QListWidget#filmstrip QScrollBar:horizontal {{ height: 7px; margin: 1px 0 0 0; }}
QListWidget#filmstrip QScrollBar::handle:horizontal {{ background: {LINE}; }}
QListWidget#filmstrip QScrollBar::handle:horizontal:hover {{ background: {LINE_CONTROL}; }}
QListWidget#navList {{ background: transparent; font-size: {f}px; }}
QListWidget#navList::item {{ padding: 8px 10px; border-radius: 4px; color: {PAPER_DIM}; }}
QListWidget#navList::item:hover {{ background: {RAISED_HOVER}; color: {PAPER}; }}
QListWidget#navList::item:selected {{ background: {PRESSED}; color: {PAPER}; }}
QListWidget#previewList {{ font-size: {f - 1}px; }}
QListWidget#previewList::item {{ padding: 5px 8px; color: {PAPER_DIM}; }}
QSplitter::handle {{ background: {LINE}; }}

QScrollArea {{ background: transparent; border: none; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {PRESSED}; border-radius: 3px; min-height: 32px; }}
QScrollBar::handle:vertical:hover {{ background: {LINE_CONTROL}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {PRESSED}; border-radius: 3px; min-width: 32px; }}
QScrollBar::handle:horizontal:hover {{ background: {LINE_CONTROL}; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}

QTabWidget::pane {{ border: 1px solid {LINE}; border-radius: 4px; }}
QTabBar::tab {{ background: transparent; color: {PAPER_DIM}; padding: 7px 14px;
    border-bottom: 2px solid transparent; }}
QTabBar::tab:selected {{ color: {PAPER}; border-bottom: 2px solid {GREASE}; }}
QMenu {{ background: {RAISED}; border: 1px solid {LINE_CONTROL}; padding: 4px; color: {PAPER}; }}
QMenu::item {{ padding: 6px 22px 6px 12px; border-radius: 3px; }}
QMenu::item:selected {{ background: {PRESSED}; }}
QMenu::item:disabled {{ color: {DISABLED}; }}
QMenu::separator {{ height: 1px; background: {LINE}; margin: 4px 6px; }}
QMessageBox QLabel {{ color: {PAPER}; }}
"""
