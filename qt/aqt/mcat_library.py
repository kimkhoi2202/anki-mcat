# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Desktop MCAT Speedrun Library: browse and one-tap import curated decks.

Lists decks from the public Supabase "Library" (a read-only catalog table plus a
public storage bucket of .apkg files) and imports the chosen deck straight into
the collection with scheduling enabled - so the readiness score and
points-at-stake have data immediately. This is read-only against the backend:
the app only GETs the catalog and downloads files; uploads are admin-only.
"""

from __future__ import annotations

import json
import os
import tempfile
import urllib.request
from dataclasses import dataclass, field

# Public Supabase project. The anon key is designed to ship in clients: it only
# grants the public-read access the Library's row-level-security policies allow
# (read the catalog + download from the public bucket); it cannot write.
SUPABASE_URL = "https://jscreeiypfopowtquriu.supabase.co"
SUPABASE_ANON_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImpzY3JlZWl5cGZvcG93dHF1cml1Iiwicm9sZSI6"
    "ImFub24iLCJpYXQiOjE3ODI5MzM2OTAsImV4cCI6MjA5ODUwOTY5MH0."
    "PIhKqznEhNLilhR2-bs60qetVzvWRcx1SrlZHZdZ7yM"
)
_TABLE = "decks"
_BUCKET = "decks"
_TIMEOUT = 30


@dataclass
class LibraryDeck:
    id: str
    title: str
    description: str
    card_count: int
    sections: list[str] = field(default_factory=list)
    storage_path: str = ""


def fetch_decks() -> list[LibraryDeck]:
    """GET the public deck catalog (read-only)."""
    url = (
        f"{SUPABASE_URL}/rest/v1/{_TABLE}"
        "?select=id,title,description,card_count,sections,storage_path"
        "&order=created_at.desc"
    )
    req = urllib.request.Request(
        url,
        headers={
            "apikey": SUPABASE_ANON_KEY,
            "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        rows = json.loads(resp.read().decode("utf-8"))
    return [
        LibraryDeck(
            id=str(r.get("id", "")),
            title=r.get("title") or "(untitled)",
            description=r.get("description") or "",
            card_count=int(r.get("card_count") or 0),
            sections=list(r.get("sections") or []),
            storage_path=r.get("storage_path") or "",
        )
        for r in rows
    ]


def download_deck(storage_path: str) -> str:
    """Download a deck's .apkg from the public bucket to a temp file; return path."""
    url = f"{SUPABASE_URL}/storage/v1/object/public/{_BUCKET}/{storage_path}"
    fd, tmp = tempfile.mkstemp(prefix="mcat_library_", suffix=".apkg")
    os.close(fd)
    with urllib.request.urlopen(url, timeout=_TIMEOUT) as resp, open(tmp, "wb") as fh:
        fh.write(resp.read())
    return tmp


def import_apkg(col, path: str):  # type: ignore[no-untyped-def]
    """Import a downloaded .apkg with scheduling (carries FSRS state + history)."""
    from anki.collection import ImportAnkiPackageRequest

    presets = col._backend.get_import_anki_package_presets()
    presets.with_scheduling = True
    return col.import_anki_package(
        ImportAnkiPackageRequest(package_path=path, options=presets)
    )


def show(mw) -> None:  # type: ignore[no-untyped-def]
    """Open the MCAT Speedrun Library browser for the given main window."""
    from aqt.operations import CollectionOp, QueryOp
    from aqt.qt import (
        QDialog,
        QDialogButtonBox,
        QLabel,
        QListWidget,
        QListWidgetItem,
        QVBoxLayout,
        qconnect,
    )
    from aqt.utils import disable_help_button, showWarning, tooltip

    dialog = QDialog(mw)
    dialog.setWindowTitle("MCAT Speedrun Library")
    disable_help_button(dialog)
    mw.garbage_collect_on_dialog_finish(dialog)

    layout = QVBoxLayout(dialog)
    layout.addWidget(
        QLabel(
            "Curated MCAT decks. Pick one and import it — scheduling is included, "
            "so your readiness score and weak-topics show right away."
        )
    )

    listw = QListWidget()
    layout.addWidget(listw)

    detail = QLabel("")
    detail.setWordWrap(True)
    detail.setStyleSheet("color:#888")
    layout.addWidget(detail)

    buttons = QDialogButtonBox()
    import_btn = buttons.addButton(
        "Download & Import", QDialogButtonBox.ButtonRole.ActionRole
    )
    refresh_btn = buttons.addButton("Refresh", QDialogButtonBox.ButtonRole.ActionRole)
    close_btn = buttons.addButton(QDialogButtonBox.StandardButton.Close)
    import_btn.setEnabled(False)
    layout.addWidget(buttons)

    decks: list[LibraryDeck] = []

    def selected() -> LibraryDeck | None:
        row = listw.currentRow()
        return decks[row] if 0 <= row < len(decks) else None

    def on_select() -> None:
        d = selected()
        import_btn.setEnabled(d is not None and bool(d.storage_path))
        if d:
            secs = ", ".join(d.sections) if d.sections else "—"
            detail.setText(f"{d.description}\n\n{d.card_count} cards · {secs}")
        else:
            detail.setText("")

    def populate(loaded: list[LibraryDeck]) -> None:
        nonlocal decks
        decks = loaded
        listw.clear()
        for d in decks:
            QListWidgetItem(f"{d.title}  ({d.card_count} cards)", listw)
        if decks:
            listw.setCurrentRow(0)
        else:
            detail.setText("The Library is empty.")
        on_select()

    def on_err(err: Exception) -> None:
        showWarning(f"Could not reach the MCAT Library:\n{err}", parent=dialog)

    def load() -> None:
        QueryOp(
            parent=dialog, op=lambda _: fetch_decks(), success=populate
        ).with_progress("Loading MCAT Library…").failure(on_err).run_in_background()

    def do_import() -> None:
        d = selected()
        if not d or not d.storage_path:
            return
        import_btn.setEnabled(False)

        def after_download(tmp: str) -> None:
            def op(col):  # type: ignore[no-untyped-def]
                try:
                    return import_apkg(col, tmp)
                finally:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass

            def imported(_log) -> None:  # type: ignore[no-untyped-def]
                tooltip(
                    f"Imported “{d.title}”. Select the deck, then Tools › MCAT Readiness.",
                    parent=mw,
                )
                dialog.accept()

            CollectionOp(parent=dialog, op=op).success(imported).failure(
                lambda e: (import_btn.setEnabled(True), on_err(e))
            ).run_in_background()

        QueryOp(
            parent=dialog,
            op=lambda _: download_deck(d.storage_path),
            success=after_download,
        ).with_progress(f"Downloading {d.title}…").failure(
            lambda e: (import_btn.setEnabled(True), on_err(e))
        ).run_in_background()

    qconnect(listw.currentRowChanged, lambda _r: on_select())
    qconnect(listw.itemDoubleClicked, lambda _i: do_import())
    qconnect(import_btn.clicked, do_import)
    qconnect(refresh_btn.clicked, load)
    qconnect(close_btn.clicked, dialog.reject)

    dialog.resize(540, 480)
    load()
    dialog.exec()
