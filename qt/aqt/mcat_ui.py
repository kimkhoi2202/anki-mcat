# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Shared Qt helpers for the MCAT desktop panels (Readiness / Coverage / Weak
Topics). Keeps the panels to just their content HTML."""

from __future__ import annotations

import html as _html

# Accent + semantic colors that read on both light and dark Qt themes.
ACCENT = "#5b6cf0"
GOOD = "#1f9d55"
WARN = "#c77d0a"
BAD = "#c0271f"
MUTED = "#888"


def esc(text: str) -> str:
    return _html.escape(str(text))


def bar(fraction: float, color: str) -> str:
    """A thin inline progress bar (0..1) as an HTML table (Qt-rich-text safe)."""
    pct = max(0, min(100, round(fraction * 100)))
    return (
        f"<table cellspacing='0' cellpadding='0' width='100%' "
        f"style='margin:4px 0'><tr>"
        f"<td style='background:{color};height:7px;width:{pct}%'></td>"
        f"<td style='background:#3a3a3a;height:7px'></td></tr></table>"
    )


def show_html(mw, title: str, body_html: str, width: int = 580, height: int = 600) -> None:  # type: ignore[no-untyped-def]
    """Present read-only rich text in a modal, scrollable Close dialog."""
    from aqt.qt import (
        QDialog,
        QDialogButtonBox,
        QTextBrowser,
        QVBoxLayout,
        qconnect,
    )
    from aqt.utils import disable_help_button

    dialog = QDialog(mw)
    dialog.setWindowTitle(title)
    disable_help_button(dialog)
    mw.garbage_collect_on_dialog_finish(dialog)

    layout = QVBoxLayout(dialog)
    browser = QTextBrowser()
    browser.setOpenExternalLinks(False)
    browser.setHtml(f"<div style='font-size:13px; line-height:1.4'>{body_html}</div>")
    layout.addWidget(browser)

    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
    qconnect(buttons.rejected, dialog.reject)
    qconnect(buttons.accepted, dialog.accept)
    layout.addWidget(buttons)

    dialog.resize(width, height)
    dialog.exec()
