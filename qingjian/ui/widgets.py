"""Small reusable pieces the screens are assembled from."""
from __future__ import annotations

import re
from pathlib import Path

from PySide6.QtCore import (QEasingCurve, QPointF, QRect, QRectF, QSize, Qt, QTimer,
                            QVariantAnimation, Signal)
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (QAbstractButton, QButtonGroup, QFrame, QHBoxLayout, QLabel,
                               QPushButton, QSizePolicy, QToolButton, QVBoxLayout, QWidget)

from ..core.i18n import tr
from . import icons, theme


def separator(vertical: bool = True, length: int = 20) -> QFrame:
    line = QFrame()
    line.setObjectName("separator")
    if vertical:
        line.setFixedWidth(1)
        line.setFixedHeight(length)
    else:
        line.setFixedHeight(1)
    return line


def stretch(layout) -> None:
    layout.addStretch(1)


def icon_button(name: str, tooltip: str = "", size: int = 18, object_name: str = "iconButton",
                color: str = theme.PAPER_DIM) -> QToolButton:
    button = QToolButton()
    button.setObjectName(object_name)
    button.setIcon(icons.icon(name, size, color))
    button.setIconSize(QSize(size, size))
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    if tooltip:
        button.setToolTip(tooltip)
    return button


def text_button(text: str, object_name: str = "compactButton", icon_name: str = "",
                color: str = theme.PAPER_DIM) -> QPushButton:
    button = QPushButton(text)
    button.setObjectName(object_name)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    if icon_name:
        button.setIcon(icons.icon(icon_name, 15, color))
        button.setIconSize(QSize(15, 15))
    return button


def caption(text: str = "", object_name: str = "caption") -> QLabel:
    label = QLabel(text)
    label.setObjectName(object_name)
    label.setWordWrap(True)
    return label


def elide(label: QLabel, text: str, width: int | None = None) -> None:
    """Set *text* on *label*, shortened in the middle to fit."""
    metrics = label.fontMetrics()
    available = width if width is not None else max(40, label.width())
    label.setText(metrics.elidedText(str(text), Qt.TextElideMode.ElideMiddle, available))
    label.setToolTip(str(text))


class ElidedLabel(QLabel):
    """A one-line label that shortens itself to fit instead of widening its row.

    `text()` still returns everything, so nothing reading the label loses words.
    """

    def __init__(self, text: str = "", object_name: str = "",
                 mode: Qt.TextElideMode = Qt.TextElideMode.ElideRight,
                 parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        if object_name:
            self.setObjectName(object_name)
        self._mode = mode
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(24)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setFont(self.font())
        painter.setPen(self.palette().color(self.foregroundRole()))
        rect = self.contentsRect()
        shown = self.fontMetrics().elidedText(self.text(), self._mode, rect.width())
        painter.drawText(rect, int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                         shown)
        painter.end()


def keyboard_focus(widget: QWidget) -> bool:
    """True when *widget* has focus that the keyboard put there.

    A ring on a control the user merely clicked, or that took focus when the
    window opened, reads as an accent rather than as a place in the tab order.
    """
    window = widget.window()
    return widget.hasFocus() and window is not None and window.testAttribute(
        Qt.WidgetAttribute.WA_KeyboardFocusChange)


_DIGITS = re.compile(r"(\d[\d,.]*)")


def draw_counted(painter: QPainter, rect: QRectF, text: str, pixels: int, colour: str,
                 align: Qt.AlignmentFlag = Qt.AlignmentFlag.AlignLeft,
                 weight: QFont.Weight = QFont.Weight.DemiBold) -> float:
    """Draw *text* with its numbers in the tabular numeral face. Returns the width.

    Counts tick while the user works; set in the proportional interface face
    they would nudge everything beside them sideways on every change.
    """
    parts = [part for part in _DIGITS.split(str(text)) if part]
    fonts = []
    width = 0.0
    for part in parts:
        if _DIGITS.fullmatch(part):
            font = theme.numeral_font(pixels + 1, weight)
            advance = theme.font_metrics("numeral", pixels + 1, weight).horizontalAdvance(part)
        else:
            font = theme.ui_font(pixels, QFont.Weight.Normal)
            advance = theme.font_metrics("ui", pixels, QFont.Weight.Normal).horizontalAdvance(part)
        fonts.append((part, font, advance))
        width += advance
    if align & Qt.AlignmentFlag.AlignRight:
        x = rect.right() - width
    elif align & Qt.AlignmentFlag.AlignHCenter:
        x = rect.center().x() - width / 2
    else:
        x = rect.left()
    painter.save()
    painter.setPen(QColor(colour))
    for part, font, advance in fonts:
        painter.setFont(font)
        painter.drawText(QRectF(x, rect.top(), advance + 2, rect.height()),
                         int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), part)
        x += advance
    painter.restore()
    return width


class CountLabel(QWidget):
    """A line of text whose numbers are set in the tabular numeral face."""

    def __init__(self, text: str = "", pixels: int = 13, colour: str = theme.PAPER,
                 align: Qt.AlignmentFlag = Qt.AlignmentFlag.AlignRight,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._text = text
        self._pixels = pixels
        self._colour = colour
        self._align = align
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

    def text(self) -> str:
        return self._text

    def setText(self, text: str) -> None:  # noqa: N802 - mirrors QLabel
        if text != self._text:
            # Tabular figures: only a change in length changes the width, and
            # asking the row to lay out again on every flip is wasted work.
            if len(text) != len(self._text):
                self.updateGeometry()
            self._text = text
            self.update()

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        metrics = theme.font_metrics("numeral", self._pixels + 1, QFont.Weight.DemiBold)
        width = int(metrics.horizontalAdvance(self._text or "0")) + 8
        return QSize(max(24, width), max(20, int(metrics.height()) + 4))

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        draw_counted(painter, QRectF(self.rect()).adjusted(2, 0, -2, 0), self._text,
                     self._pixels, self._colour if self.isEnabled() else theme.DISABLED,
                     self._align)
        painter.end()


class Segmented(QFrame):
    """A row of mutually exclusive buttons carrying a value each."""

    changed = Signal(str)

    def __init__(self, options: list[tuple[str, str]] | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("segmentBar")
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(2)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._buttons: dict[str, QPushButton] = {}
        self._quiet = False
        self._group.buttonClicked.connect(self._emit)
        if options:
            self.set_options(options)

    def set_options(self, options: list[tuple[str, str]], icons_for: dict | None = None) -> None:
        current = self.value()
        for button in list(self._buttons.values()):
            self._group.removeButton(button)
            self._layout.removeWidget(button)
            # Hidden at once: a deferred delete can lag behind the next paint.
            button.hide()
            button.deleteLater()
        self._buttons.clear()
        for value, label in options:
            button = QPushButton(label)
            button.setAutoDefault(False)
            button.setObjectName("segment")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setProperty("value", value)
            name = (icons_for or {}).get(value)
            if name:
                button.setIcon(icons.icon(name, 14, theme.PAPER_DIM))
                button.setIconSize(QSize(14, 14))
            self._group.addButton(button)
            self._layout.addWidget(button)
            self._buttons[value] = button
        if current in self._buttons:
            self.set_value(current, quiet=True)
        elif options:
            self.set_value(options[0][0], quiet=True)

    def _emit(self, button: QPushButton) -> None:
        if not self._quiet:
            self.changed.emit(str(button.property("value")))

    def value(self) -> str:
        for value, button in self._buttons.items():
            if button.isChecked():
                return value
        return ""

    def set_value(self, value: str, quiet: bool = False) -> None:
        button = self._buttons.get(value)
        if button is None:
            return
        self._quiet = quiet
        button.setChecked(True)
        self._quiet = False


def set_primary_button(dialog: QWidget, primary: QPushButton) -> None:
    """Make Enter predictable throughout a dialog's button tree."""
    primary.setDefault(True)
    for button in dialog.findChildren(QPushButton):
        button.setAutoDefault(button is primary)


class Switch(QAbstractButton):
    """A toggle drawn rather than styled, so it looks the same everywhere.

    On is paper with an ink knob, like a ticked box on a printed form.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(38, 22)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        return QSize(38, 22)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        on = self.isChecked()
        if not self.isEnabled():
            painter.setOpacity(0.45)
        rect = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        radius = rect.height() / 2
        if on:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(theme.PAPER))
        else:
            painter.setPen(QPen(QColor(theme.PAPER_DIM if self.underMouse()
                                       else theme.LINE_CONTROL), 1))
            painter.setBrush(QColor(theme.RAISED))
        painter.drawRoundedRect(rect, radius, radius)
        knob = self.height() - 8
        x = self.width() - knob - 4 if on else 4
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(theme.INK if on else theme.FAINT))
        painter.drawEllipse(QRectF(x, 4, knob, knob))
        if keyboard_focus(self):
            painter.setPen(QPen(QColor(theme.BALLPOINT_LIGHT), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect.adjusted(-0.5, -0.5, 0.5, 0.5), radius, radius)
        painter.end()


def _star_path(rect: QRectF) -> QPainterPath:
    points = [(12, 2.4), (14.6, 8.6), (21.2, 9.2), (16.2, 13.6), (17.6, 20.2),
              (12, 16.8), (6.4, 20.2), (7.8, 13.6), (2.8, 9.2), (9.4, 8.6)]
    scale = min(rect.width(), rect.height()) / 24.0
    left = rect.center().x() - 12 * scale
    top = rect.center().y() - 12 * scale
    path = QPainterPath(QPointF(left + points[0][0] * scale, top + points[0][1] * scale))
    for x, y in points[1:]:
        path.lineTo(QPointF(left + x * scale, top + y * scale))
    path.closeSubpath()
    return path


class _Star(QAbstractButton):
    def __init__(self, index: int, owner: "StarRating") -> None:
        super().__init__(owner)
        self.index = index
        self.owner = owner
        self.setFixedSize(20, 22)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setToolTip(str(index))

    def enterEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self.owner.hover(self.index)

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self.owner.hover(0)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        path = _star_path(QRectF(self.rect()).adjusted(2, 3, -2, -3))
        value, hovered = self.owner.value(), self.owner.hovered
        if hovered:
            lit, colour = self.index <= hovered, theme.PAPER_BRIGHT
        else:
            lit, colour = self.index <= value, theme.PAPER
        if lit:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(colour))
        else:
            painter.setPen(QPen(QColor(theme.LINE_CONTROL), 1.2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)
        if keyboard_focus(self):
            painter.setPen(QPen(QColor(theme.BALLPOINT_LIGHT), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 3, 3)
        painter.end()


class StarRating(QWidget):
    """Five paper stars; clicking the lit one again clears the rating."""

    rated = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._value = 0
        self.hovered = 0
        self._stars: list[_Star] = []
        for index in range(1, 6):
            star = _Star(index, self)
            star.clicked.connect(lambda _=False, value=index: self._clicked(value))
            layout.addWidget(star)
            self._stars.append(star)

    def hover(self, index: int) -> None:
        self.hovered = index
        for star in self._stars:
            star.update()

    def _clicked(self, value: int) -> None:
        new = 0 if value == self._value else value
        self.set_value(new)
        self.rated.emit(new)

    def value(self) -> int:
        return self._value

    def set_value(self, value: int) -> None:
        self._value = max(0, min(5, int(value or 0)))
        for star in self._stars:
            star.update()


class _Sticker(QAbstractButton):
    """One round dot sticker, the kind labs stick on envelopes to sort them."""

    def __init__(self, name: str, colour: str, owner: "LabelSwatches") -> None:
        super().__init__(owner)
        self.name = name
        self.colour = colour
        self.owner = owner
        self.setFixedSize(22, 22)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        centre = QPointF(self.width() / 2, self.height() / 2)
        chosen = self.owner.value() == self.name and bool(self.name)
        if self.name:
            radius = 6.5 if not self.underMouse() else 7.2
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(self.colour))
            painter.drawEllipse(centre, radius, radius)
            if chosen:
                painter.setPen(QPen(QColor(theme.PAPER), 1.6))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawEllipse(centre, 9.6, 9.6)
        else:
            pen = QPen(QColor(theme.PAPER_DIM if self.underMouse() else theme.LINE_CONTROL), 1)
            pen.setDashPattern([2, 2])
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(centre, 6.5, 6.5)
        if keyboard_focus(self):
            painter.setPen(QPen(QColor(theme.BALLPOINT_LIGHT), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(centre, 10.4, 10.4)
        painter.end()


class LabelSwatches(QWidget):
    """The five colour stickers plus one that peels the sticker off."""

    labelled = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(1)
        self._value = ""
        self._stickers: list[_Sticker] = []
        for name, colour in theme.LABEL_COLOURS.items():
            sticker = _Sticker(name, colour, self)
            sticker.setToolTip(name)
            sticker.clicked.connect(lambda _=False, value=name: self._clicked(value))
            layout.addWidget(sticker)
            self._stickers.append(sticker)
        clear = _Sticker("", "", self)
        clear.setToolTip(tr("none"))
        clear.clicked.connect(lambda: self._clicked(""))
        layout.addWidget(clear)
        self._stickers.append(clear)
        layout.addStretch(1)

    def _clicked(self, value: str) -> None:
        new = "" if value == self._value else value
        self.set_value(new)
        self.labelled.emit(new)

    def value(self) -> str:
        return self._value

    def set_value(self, value: str) -> None:
        self._value = value if value in theme.LABEL_COLOURS else ""
        for sticker in self._stickers:
            sticker.update()


class FolderSlip(QAbstractButton):
    """The folder being sorted, drawn as a pickup slip: count stub, perforation, folder.

    Clicking it chooses another folder.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._folder = ""
        self._count_text = ""
        self._empty_text = ""

    def set_folder(self, folder: str, count_text: str, empty_text: str) -> None:
        self._folder = folder
        self._count_text = count_text
        self._empty_text = empty_text
        self.setToolTip(folder or empty_text)
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        return QSize(420, 32)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        return QSize(180, 32)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        hover = self.underMouse()
        body = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setPen(QPen(QColor(theme.PAPER_DIM if hover else theme.LINE_CONTROL), 1))
        painter.setBrush(QColor(theme.RAISED_HOVER if hover else theme.RAISED))
        painter.drawRoundedRect(body, 3, 3)

        stub_text = self._count_text or "—"
        metrics = QFontMetrics(theme.numeral_font(14))
        stub_width = max(52, metrics.horizontalAdvance(stub_text) + 26)
        draw_counted(painter, QRectF(body.left(), body.top(), stub_width, body.height()),
                     stub_text, 12, theme.PAPER, Qt.AlignmentFlag.AlignHCenter)
        # The perforation, with the two half-moon bites a torn slip keeps.
        x = body.left() + stub_width
        pen = QPen(QColor(theme.LINE_CONTROL), 1)
        pen.setDashPattern([1.5, 2.5])
        painter.setPen(pen)
        painter.drawLine(QPointF(x, body.top() + 5), QPointF(x, body.bottom() - 5))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(theme.COUNTER))
        painter.drawEllipse(QPointF(x, body.top() - 0.5), 3.5, 3.5)
        painter.drawEllipse(QPointF(x, body.bottom() + 0.5), 3.5, 3.5)

        left = x + 10
        glyph = icons.pixmap("folder", 15, theme.PAPER_DIM if self._folder else theme.FAINT)
        painter.drawPixmap(int(left), int(body.center().y() - 7.5), glyph)
        left += 22
        room = body.right() - 10 - left
        if not self._folder:
            painter.setFont(theme.ui_font(12))
            painter.setPen(QColor(theme.FAINT))
            painter.drawText(QRectF(left, body.top(), room, body.height()),
                             int(Qt.AlignmentFlag.AlignVCenter), QFontMetrics(painter.font())
                             .elidedText(self._empty_text, Qt.TextElideMode.ElideRight,
                                         int(room)))
        else:
            name = Path(self._folder).name or self._folder
            name_font = theme.ui_font(13, QFont.Weight.DemiBold)
            name_metrics = QFontMetrics(name_font)
            # The folder's own name comes first; its location only gets what is left.
            name_text = name_metrics.elidedText(name, Qt.TextElideMode.ElideMiddle, int(room))
            painter.setFont(name_font)
            painter.setPen(QColor(theme.PAPER))
            painter.drawText(QRectF(left, body.top(), room, body.height()),
                             int(Qt.AlignmentFlag.AlignVCenter), name_text)
            left += name_metrics.horizontalAdvance(name_text) + 10
            path_font = theme.ui_font(11)
            painter.setFont(path_font)
            painter.setPen(QColor(theme.FAINT))
            rest = body.right() - 10 - left
            if rest > 30:
                painter.drawText(QRectF(left, body.top(), rest, body.height()),
                                 int(Qt.AlignmentFlag.AlignVCenter),
                                 QFontMetrics(path_font).elidedText(
                                     str(Path(self._folder).parent),
                                     Qt.TextElideMode.ElideMiddle, int(rest)))
        if keyboard_focus(self):
            painter.setPen(QPen(QColor(theme.BALLPOINT_LIGHT), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(body, 3, 3)
        painter.end()


class BindingCard(QWidget):
    """One of the ten keys, drawn as a kraft photo envelope.

    The key is printed large in the corner, the destination is written on the
    form rule in ballpoint, and the count says how many prints went in. Filing a
    print makes the envelope show a stamp ring for a moment (`receive`).
    """

    activated = Signal(int)
    folder_requested = Signal(int)

    HEIGHT = 70

    def __init__(self, index: int, parent: QWidget | None = None, motion: bool = True) -> None:
        super().__init__(parent)
        self.index = index
        #: False when Windows' "show animations" is off: lift and stamp change at once.
        self.motion = motion
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(self.HEIGHT)
        self.setMouseTracking(True)
        self._key = ""
        self._name = ""
        self._detail = ""
        self._badge = ""
        self._count = 0
        self._configured = False
        self._written = True             # a folder the user chose, not an action name
        self._pressed = False
        self._lift = 0.0
        self._stamp = 0.0
        self._lift_animation = QVariantAnimation(self)
        self._lift_animation.setDuration(110)
        self._lift_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._lift_animation.valueChanged.connect(self._set_lift)
        self._stamp_animation = QVariantAnimation(self)
        self._stamp_animation.setDuration(420)
        self._stamp_animation.setStartValue(1.0)
        self._stamp_animation.setEndValue(0.0)
        self._stamp_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._stamp_animation.valueChanged.connect(self._set_stamp)
        #: Set by a double click, whose closing release is not a second click.
        self._release_ends_double_click = False

    # -- geometry --------------------------------------------------------
    def sizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        return QSize(140, self.height())

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        return QSize(72, self.height())

    def set_envelope_height(self, height: int) -> None:
        self.setFixedHeight(max(56, int(height)))

    def mouth(self) -> QRect:
        """Where a print goes in: the notch at the top, in this widget's coordinates."""
        return QRect(self.width() // 2 - 12, 4, 24, 14)

    # -- animation -------------------------------------------------------
    def _set_lift(self, value) -> None:
        self._lift = float(value)
        self.update()

    def _set_stamp(self, value) -> None:
        self._stamp = float(value)
        self.update()

    def _animate_lift(self, target: float) -> None:
        self._lift_animation.stop()
        if not self.motion:
            self._set_lift(target)
            return
        self._lift_animation.setStartValue(self._lift)
        self._lift_animation.setEndValue(target)
        self._lift_animation.start()

    def receive(self) -> None:
        """A print just went in: stamp it."""
        self._stamp_animation.stop()
        if self.motion:
            self._stamp_animation.start()
            return
        # Without animation the stamp still says which envelope took the print,
        # for as long as the fade would have lasted.
        self._set_stamp(1.0)
        QTimer.singleShot(self._stamp_animation.duration(), self, self._clear_stamp)

    def _clear_stamp(self) -> None:
        self._set_stamp(0.0)

    def enterEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self._animate_lift(1.0)

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self._pressed = False
        self._animate_lift(0.0)

    # -- mouse -----------------------------------------------------------
    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressed = True
            self.update()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 - Qt naming
        # Qt delivers press, release, press, double-click, release. Acting on
        # both releases filed two photographs, and changing the folder here came
        # after the first of them had already gone. The folder is a right-click.
        if event.button() == Qt.MouseButton.LeftButton:
            self._release_ends_double_click = True

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt naming
        was_pressed = self._pressed
        self._pressed = False
        self.update()
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if self._release_ends_double_click:
            self._release_ends_double_click = False
            return
        if was_pressed and self.rect().contains(event.position().toPoint()):
            self.activated.emit(self.index)

    def contextMenuEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self.folder_requested.emit(self.index)

    # -- content ---------------------------------------------------------
    def update_binding(self, binding, count: int = 0, action_label: str = "",
                       badge_text: str = "") -> None:
        self._key = binding.key or "?"
        needs = binding.needs_folder()
        self._configured = binding.is_configured()
        if binding.folder:
            self._name = Path(binding.folder).name or binding.folder
            self._written = True
        elif needs:
            self._name = tr("header.choose_folder")
            self._written = False
        else:
            self._name = action_label
            self._written = False
        detail = binding.folder or ""
        if binding.path_template:
            detail = f"{detail}\\{binding.path_template}" if detail else binding.path_template
        self._detail = detail or action_label
        self._badge = badge_text
        self._count = int(count or 0)
        self.setToolTip(f"{self._detail}\n{tr('card.tip')}" if self._detail else tr("card.tip"))
        self.update()

    # -- drawing ---------------------------------------------------------
    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        rise = -2.0 * self._lift + (1.0 if self._pressed else 0.0)
        body = QRectF(1.5, 6.5 + rise, self.width() - 3, self.height() - 10)
        shape = QPainterPath()
        shape.addRoundedRect(body, 3, 3)
        notch = QPainterPath()
        notch.addEllipse(QPointF(body.center().x(), body.top()), 14.0, 6.5)
        shape = shape.subtracted(notch)

        if self._configured:
            # A soft shadow that deepens as the envelope lifts off the counter.
            rest = body.translated(0, -rise)
            for step, alpha in ((3.0, 34), (1.5, 46)):
                shadow = QColor(0, 0, 0, int(alpha * (0.6 + 0.8 * self._lift)))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(shadow)
                painter.drawRoundedRect(rest.adjusted(1, step + 8, -1, step), 3, 3)
            painter.fillPath(shape, QColor(theme.KRAFT_HOVER if self._lift > 0.5
                                           else theme.KRAFT))
            painter.save()
            painter.setClipPath(shape)
            painter.fillRect(QRectF(body.left(), body.bottom() - 2, body.width(), 2),
                             QColor(theme.KRAFT_EDGE))
            painter.restore()
            ink, name_ink, rule = theme.INK, theme.BALLPOINT, theme.KRAFT_RULE
        else:
            pen = QPen(QColor(theme.KRAFT_EDGE if self._lift < 0.5 else theme.KRAFT), 1)
            pen.setDashPattern([3, 3])
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(shape)
            ink, name_ink, rule = theme.KRAFT, theme.FAINT, theme.LINE_CONTROL

        inset = 10.0
        # The key, printed large in the corner.
        key_pixels = int(body.height() * 0.40)
        key_font = theme.numeral_font(key_pixels, QFont.Weight.DemiBold, condensed=True)
        key_metrics = QFontMetrics(key_font)
        while key_pixels > 12 and key_metrics.horizontalAdvance(self._key) > body.width() * 0.42:
            key_pixels -= 2
            key_font = theme.numeral_font(key_pixels, QFont.Weight.DemiBold, condensed=True)
            key_metrics = QFontMetrics(key_font)
        key_rect = QRectF(body.left() + inset, body.top() + 4, body.width() * 0.45,
                          key_metrics.height())
        painter.setFont(key_font)
        painter.setPen(QColor(ink))
        painter.drawText(key_rect, int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop),
                         self._key)
        if self._stamp > 0.01:
            advance = key_metrics.horizontalAdvance(self._key)
            centre = QPointF(key_rect.left() + advance / 2,
                             key_rect.top() + key_metrics.ascent() * 0.62)
            radius = max(advance, key_metrics.ascent()) * 0.62 + 5 + 3 * (1 - self._stamp)
            stamp = QColor(theme.STAMP if self._configured else theme.GREASE)
            stamp.setAlphaF(min(1.0, self._stamp * 1.3))
            painter.setPen(QPen(stamp, 2.0))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(centre, radius, radius * 0.92)

        # A printed box for anything that is not a plain move.
        badge_font = theme.ui_font(10, QFont.Weight.Bold)
        badge_metrics = QFontMetrics(badge_font)
        # A box that would have to be cut short says less than no box: the
        # name on the rule already tells what the key does.
        if self._badge and badge_metrics.horizontalAdvance(self._badge) <= body.width() * 0.5:
            text = self._badge
            width = badge_metrics.horizontalAdvance(text) + 8
            box = QRectF(body.right() - inset - width, body.top() + 7, width,
                         badge_metrics.height() + 1)
            painter.setFont(badge_font)
            painter.setPen(QPen(QColor(ink), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(box.adjusted(0.5, 0.5, -0.5, -0.5))
            painter.drawText(box, int(Qt.AlignmentFlag.AlignCenter), text)

        # The destination on its form rule, and how many went in.
        line_y = body.bottom() - 9
        count_width = 0.0
        if self._count:
            count_font = theme.numeral_font(15, QFont.Weight.DemiBold)
            count_text = str(self._count)
            count_width = QFontMetrics(count_font).horizontalAdvance(count_text)
            painter.setFont(count_font)
            painter.setPen(QColor(theme.STAMP if self._stamp > 0.35 else name_ink))
            painter.drawText(QRectF(body.right() - inset - count_width - 2, line_y - 20,
                                    count_width + 2, 20),
                             int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom),
                             count_text)
        name_font = theme.ui_font(13 if self._written else 12,
                                  QFont.Weight.Normal if self._written else QFont.Weight.DemiBold)
        room = body.width() - 2 * inset - (count_width + 8 if count_width else 0)
        name = QFontMetrics(name_font).elidedText(self._name, Qt.TextElideMode.ElideMiddle,
                                                  max(10, int(room)))
        painter.setFont(name_font)
        painter.setPen(QColor(name_ink if self._written or not self._configured else ink))
        painter.drawText(QRectF(body.left() + inset, line_y - 20, room, 20),
                         int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom), name)
        rule_pen = QPen(QColor(rule), 1)
        rule_pen.setDashPattern([1, 2.5])
        painter.setPen(rule_pen)
        painter.drawLine(QPointF(body.left() + inset, line_y + 2.5),
                         QPointF(body.right() - inset, line_y + 2.5))
        painter.end()


class FlyingPrint(QWidget):
    """The copy of a filed print on its way into an envelope: paper border, picture."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._pixmap = QPixmap()
        self.hide()

    def set_pixmap(self, pixmap: QPixmap) -> None:
        self._pixmap = pixmap
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        rect = QRectF(self.rect())
        border = max(1.0, min(rect.width(), rect.height()) * 0.0175)
        painter.fillRect(rect, QColor(theme.PAPER))
        if not self._pixmap.isNull():
            painter.drawPixmap(rect.adjusted(border, border, -border, -border), self._pixmap,
                               QRectF(self._pixmap.rect()))
        painter.end()


class SectionCard(QFrame):
    """A titled block used throughout the settings screens."""

    def __init__(self, title: str, icon_name: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("settingsCard")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 14, 18, 12)
        outer.setSpacing(8)
        header = QHBoxLayout()
        header.setSpacing(8)
        if icon_name:
            badge = QLabel()
            badge.setPixmap(icons.pixmap(icon_name, 16, theme.PAPER_DIM))
            badge.setFixedSize(18, 18)
            badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            header.addWidget(badge)
        label = QLabel(title)
        label.setObjectName("sectionTitle")
        header.addWidget(label, 1)
        outer.addLayout(header)
        self.body = QVBoxLayout()
        self.body.setSpacing(2)
        outer.addLayout(self.body)

    def add_row(self, widget: QWidget) -> QWidget:
        self.body.addWidget(widget)
        return widget


class SettingRow(QWidget):
    """Label, optional explanation, and one control on the right."""

    def __init__(self, title: str, description: str = "", control: QWidget | None = None,
                 first: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        if not first:
            outer.addWidget(separator(vertical=False))
        row = QHBoxLayout()
        row.setContentsMargins(0, 11, 0, 11)
        row.setSpacing(16)
        texts = QVBoxLayout()
        texts.setSpacing(3)
        label = QLabel(title)
        label.setObjectName("rowTitle")
        label.setWordWrap(True)
        texts.addWidget(label)
        if description:
            texts.addWidget(caption(description))
        row.addLayout(texts, 1)
        if control is not None:
            control.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
            row.addWidget(control, 0, Qt.AlignmentFlag.AlignTop)
        outer.addLayout(row)
        self.control = control
