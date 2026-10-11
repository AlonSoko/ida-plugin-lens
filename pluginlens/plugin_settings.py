"""Editing the settings a plugin declares in its ida-plugin.json, as
`hcli plugin config <plugin> set/del` does (hcli keeps them in
ida-config.json, where the plugin reads them)."""

import re

import ida_kernwin

from . import hcli
from .common import PLUGIN_NAME, QtWidgets, metadata

DEFAULT_CHOICE = "(default)"


def declared(folder):
    """The settings the plugin in folder declares ([] if none)."""
    settings = metadata(folder).get("settings") if folder else None
    return [s for s in settings or [] if isinstance(s, dict) and s.get("key")]


def _current(name, key):
    """(value set explicitly or None, value in effect or None), from hcli."""
    from hcli.lib.ida.plugin.settings import get_plugin_setting, has_plugin_setting

    try:
        explicit = get_plugin_setting(name, key) if has_plugin_setting(name, key) else None
    except (KeyError, ValueError):
        explicit = None
    try:
        effective = get_plugin_setting(name, key)
    except (KeyError, ValueError):
        effective = None
    return explicit, effective


class SettingsDialog(QtWidgets.QDialog):
    def __init__(self, title, hcli_name, settings, parent=None):
        super().__init__(parent)
        self.hcli_name = hcli_name
        self.settings = settings
        self.widgets = {}  # key -> (widget, explicit value)
        self.setWindowTitle(f"{title}: settings")
        self.setMinimumWidth(560)
        layout = QtWidgets.QVBoxLayout(self)
        intro = QtWidgets.QLabel(
            "Kept by hcli in ida-config.json. An empty field uses the plugin's default. "
            "The plugin reads them when it loads: restart IDA to apply a change.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        form = QtWidgets.QFormLayout()
        for setting in settings:
            key = setting["key"]
            explicit, effective = _current(hcli_name, key)
            widget = self._widget(setting, explicit)
            tip = setting.get("documentation") or ""
            if setting.get("validation_pattern"):
                tip += f"\n\nMust match: {setting['validation_pattern']}"
            widget.setToolTip(tip.strip() or key)
            label = QtWidgets.QLabel(setting.get("name") or key)
            label.setToolTip(f"{key}" + (f"\n{tip.strip()}" if tip.strip() else ""))
            form.addRow(label, widget)
            documentation = (setting.get("documentation") or "").strip()
            if documentation and documentation.casefold() != (setting.get("name") or key).strip().casefold():
                doc = QtWidgets.QLabel(documentation)
                doc.setWordWrap(True)
                doc.setEnabled(False)  # greyed, as a hint
                form.addRow("", doc)
            self.widgets[key] = (widget, explicit)
        layout.addLayout(form)
        buttons = QtWidgets.QDialogButtonBox()
        save = buttons.addButton(QtWidgets.QDialogButtonBox.StandardButton.Save)
        buttons.addButton(QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        save.clicked.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.changed = False

    def _widget(self, setting, explicit):
        default = setting.get("default")
        if setting.get("type") == "boolean":
            box = QtWidgets.QComboBox()
            box.addItem(f"{DEFAULT_CHOICE}: {'on' if default else 'off'}" if default is not None else DEFAULT_CHOICE,
                        None)
            box.addItem("on", True)
            box.addItem("off", False)
            box.setCurrentIndex(max(0, box.findData(explicit)) if explicit is not None else 0)
            return box
        if setting.get("choices"):
            box = QtWidgets.QComboBox()
            box.addItem(f"{DEFAULT_CHOICE}: {default}" if default not in (None, "") else DEFAULT_CHOICE, None)
            for choice in setting["choices"]:
                box.addItem(str(choice), str(choice))
            box.setCurrentIndex(max(0, box.findData(explicit)) if explicit is not None else 0)
            return box
        edit = QtWidgets.QLineEdit("" if explicit is None else str(explicit))
        if default not in (None, ""):
            edit.setPlaceholderText(f"default: {default}")
        elif setting.get("required"):
            edit.setPlaceholderText("required")
        if setting.get("secret"):
            edit.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        return edit

    def _value(self, widget):
        """The value entered: None for the default, else a string or bool."""
        if isinstance(widget, QtWidgets.QComboBox):
            return widget.currentData()
        text = widget.text().strip()
        return text or None

    def _save(self):
        errors, done = [], 0
        for setting in self.settings:
            key = setting["key"]
            widget, explicit = self.widgets[key]
            value = self._value(widget)
            if value == explicit:
                continue
            pattern = setting.get("validation_pattern")
            if isinstance(value, str) and pattern and not re.fullmatch(pattern, value):
                errors.append(f"{setting.get('name') or key}: must match {pattern}")
                continue
            if value is None:
                args = ("plugin", "config", self.hcli_name, "del", key)
            else:
                text = ("true" if value else "false") if isinstance(value, bool) else value
                args = ("plugin", "config", self.hcli_name, "set", key, text)
            try:
                ok, output = hcli.run(*args)
            except Exception as e:  # hcli not importable, or changed
                ok, output = False, f"{type(e).__name__}: {e}"
            shown = " ".join(args[:4]) + (" ••••" if setting.get("secret") and value is not None else
                                          (" " + " ".join(args[4:]) if len(args) > 4 else ""))
            ida_kernwin.msg(f"[{PLUGIN_NAME}] hcli {shown}\n{output if not setting.get('secret') else ''}\n")
            if ok and "Error" not in output:
                done += 1
            else:
                errors.append(f"{setting.get('name') or key}: {output.strip() or 'failed'}")
        self.changed = done > 0
        if errors:
            ida_kernwin.warning("Not saved:\n\n" + "\n".join(errors))
            if not done:
                return
        self.accept()


def edit_settings(parent, title, hcli_name, folder):
    """Show the plugin's settings to edit; True if any changed."""
    settings = declared(folder)
    if not settings:
        ida_kernwin.info(f"{title} declares no settings.")
        return False
    dialog = SettingsDialog(title, hcli_name, settings, parent)
    dialog.exec()
    return dialog.changed

