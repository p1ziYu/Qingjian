"""Localized Qt prompts used throughout the interface."""
from __future__ import annotations

from PySide6.QtWidgets import QInputDialog, QMessageBox

from ..core.i18n import tr

_BUTTON_TEXT = {
    QMessageBox.StandardButton.Ok: "ok",
    QMessageBox.StandardButton.Cancel: "cancel",
    QMessageBox.StandardButton.Yes: "yes",
    QMessageBox.StandardButton.No: "no",
}


def _message(parent, title: str, text: str, icon: QMessageBox.Icon,
             buttons: QMessageBox.StandardButton,
             default: QMessageBox.StandardButton = QMessageBox.StandardButton.NoButton
             ) -> QMessageBox.StandardButton:
    box = QMessageBox(icon, title, text, buttons, parent)
    if default != QMessageBox.StandardButton.NoButton:
        box.setDefaultButton(default)
    for button, key in _BUTTON_TEXT.items():
        widget = box.button(button)
        if widget is not None:
            widget.setText(tr(key))
    box.exec()
    clicked = box.clickedButton()
    return (box.standardButton(clicked) if clicked is not None
            else QMessageBox.StandardButton.NoButton)


def ask(parent, title: str, text: str,
        buttons: QMessageBox.StandardButton = (QMessageBox.StandardButton.Yes
                                                 | QMessageBox.StandardButton.No),
        default: QMessageBox.StandardButton = QMessageBox.StandardButton.No
        ) -> QMessageBox.StandardButton:
    return _message(parent, title, text, QMessageBox.Icon.Question, buttons, default)


def warning(parent, title: str, text: str,
            buttons: QMessageBox.StandardButton = QMessageBox.StandardButton.Ok,
            default: QMessageBox.StandardButton = QMessageBox.StandardButton.NoButton
            ) -> QMessageBox.StandardButton:
    return _message(parent, title, text, QMessageBox.Icon.Warning, buttons, default)


def information(parent, title: str, text: str) -> QMessageBox.StandardButton:
    return _message(parent, title, text, QMessageBox.Icon.Information,
                    QMessageBox.StandardButton.Ok)


def critical(parent, title: str, text: str) -> QMessageBox.StandardButton:
    return _message(parent, title, text, QMessageBox.Icon.Critical,
                    QMessageBox.StandardButton.Ok)


def get_text(parent, title: str, label: str, text: str = "") -> tuple[str, bool]:
    dialog = QInputDialog(parent)
    dialog.setWindowTitle(title)
    dialog.setLabelText(label)
    dialog.setTextValue(text)
    dialog.setOkButtonText(tr("ok"))
    dialog.setCancelButtonText(tr("cancel"))
    accepted = dialog.exec() == QInputDialog.DialogCode.Accepted
    return dialog.textValue(), accepted
