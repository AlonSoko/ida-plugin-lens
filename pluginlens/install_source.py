"""Installing a plugin from a URL, a .zip file or a folder, with hcli."""

import json
import os
import zipfile

import ida_kernwin

from . import operations
from .common import QtWidgets, metadata

HINT = "https://github.com/owner/repo, a .zip URL, a .zip file or a plugin folder"


def describe(source):
    """(text for the confirmation, error or None) for what the user entered.

    hcli takes: a GitHub repository URL (it installs the latest release's
    .zip), any https:// URL of a .zip, a local .zip, or a folder with an
    ida-plugin.json.
    """
    if source.startswith("https://"):
        return f"From: {source}", None
    if source.startswith("http://"):
        return None, "Only https:// URLs are accepted."
    path = os.path.expanduser(source)
    if os.path.isdir(path):
        if not os.path.isfile(os.path.join(path, "ida-plugin.json")):
            return None, "This folder has no ida-plugin.json, so it isn't a plugin hcli can install."
        md = metadata(path)
        return _plugin_text(md, f"From the folder: {path}"), None
    if os.path.isfile(path):
        if not path.lower().endswith(".zip"):
            return None, "A file must be a .zip."
        try:
            with zipfile.ZipFile(path) as archive:
                found = []
                for member in archive.namelist():
                    if member.endswith("ida-plugin.json"):
                        try:
                            found.append(json.loads(archive.read(member)).get("plugin", {}))
                        except (ValueError, AttributeError):
                            pass
        except (OSError, zipfile.BadZipFile):
            return None, "This .zip can't be read."
        if not found:
            return None, "This .zip has no ida-plugin.json, so it isn't a plugin hcli can install."
        return _plugin_text(found[0], f"From the file: {path}"), None
    return None, "No such file or folder, and not an https:// URL."


def _plugin_text(md, origin):
    name = md.get("name") or "?"
    version = md.get("version") or "?"
    lines = [f"{name} {version}", origin]
    authors = [a.get("name") for a in md.get("authors") or [] if isinstance(a, dict) and a.get("name")]
    if authors:
        lines.append(f"Authors: {', '.join(authors)}")
    if md.get("pythonDependencies"):
        lines.append(f"Python packages pip installs: {', '.join(map(str, md['pythonDependencies']))}")
    return "\n".join(lines)


class SourceDialog(QtWidgets.QDialog):
    """Asks for a URL, a .zip file or a folder."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Install a plugin from a URL or file")
        self.setMinimumWidth(560)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(QtWidgets.QLabel(
            "Install with hcli from a GitHub repository (its latest release), "
            "the URL of a .zip, a .zip file, or a plugin folder:"))
        row = QtWidgets.QHBoxLayout()
        self.source = QtWidgets.QLineEdit()
        self.source.setPlaceholderText(HINT)
        self.source.textChanged.connect(self._check)
        row.addWidget(self.source, 1)
        zip_button = QtWidgets.QPushButton("Choose .zip…")
        zip_button.clicked.connect(self._choose_zip)
        row.addWidget(zip_button)
        folder_button = QtWidgets.QPushButton("Choose folder…")
        folder_button.clicked.connect(self._choose_folder)
        row.addWidget(folder_button)
        layout.addLayout(row)
        self.error = QtWidgets.QLabel()
        self.error.setWordWrap(True)
        layout.addWidget(self.error)
        self.upgrade = QtWidgets.QCheckBox("Update it if it's already installed")
        self.upgrade.setChecked(True)
        self.upgrade.setToolTip("hcli plugin install --upgrade: replaces an older installed version")
        layout.addWidget(self.upgrade)
        buttons = QtWidgets.QDialogButtonBox()
        self.ok = buttons.addButton(QtWidgets.QDialogButtonBox.StandardButton.Ok)
        buttons.addButton(QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        self.ok.setText("Install…")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._check()

    def _choose_zip(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Plugin .zip", os.path.expanduser("~"), "Zip (*.zip)")
        if path:
            self.source.setText(path)

    def _choose_folder(self):
        path = QtWidgets.QFileDialog.getExistingDirectory(self, "Plugin folder", os.path.expanduser("~"))
        if path:
            self.source.setText(path)

    def _check(self, *_):
        text = self.source.text().strip()
        error = describe(text)[1] if text else None
        self.error.setText(error or "")
        self.error.setVisible(bool(error))
        self.ok.setEnabled(bool(text) and not error)


def install_from_source(parent, on_status):
    """Ask for a URL or file, confirm, and install it with hcli."""
    dialog = SourceDialog(parent)
    if not dialog.exec():
        return
    source = dialog.source.text().strip()
    text, error = describe(source)
    if error:
        ida_kernwin.warning(error)
        return
    question = "\n".join([
        "Install this plugin with hcli?", "", text, "",
        "A plugin runs inside IDA with access to your files: install only the ones you trust.",
        "IDA restarts afterwards to load it.",
    ])
    if ida_kernwin.ask_yn(ida_kernwin.ASKBTN_NO, question) != ida_kernwin.ASKBTN_YES:
        return
    path = os.path.expanduser(source)
    argument = path if os.path.exists(path) else source
    args = ("--upgrade", argument) if dialog.upgrade.isChecked() else (argument,)
    operations.perform([(operations.INSTALL, None, args)], on_status=on_status,
                       titles={argument: os.path.basename(argument.rstrip("/")) or argument})
