"""Restarting IDA, so that newly installed or updated plugins are loaded."""

import os
import sys

import ida_diskio

from .common import PLUGIN_NAME, QtCore, QtGui, QtWidgets


def ask_restart(text):
    """Show text with one button, "Restart IDA now", and restart IDA."""
    box = QtWidgets.QMessageBox(QtWidgets.QApplication.activeWindow())
    box.setWindowTitle(PLUGIN_NAME)
    box.setIcon(QtWidgets.QMessageBox.Icon.Information)
    box.setText(text)
    restart = box.addButton("Restart IDA now", QtWidgets.QMessageBox.ButtonRole.AcceptRole)
    box.setDefaultButton(restart)
    box.setEscapeButton(restart)  # Esc or closing the box restarts too
    box.exec()
    restart_ida()


RELAUNCH_WAIT = 60  # seconds the helper waits for IDA to exit before giving up


def restart_ida():
    """Quit IDA as File > Exit does and start it again on the same database.

    A detached helper waits for this IDA process to exit, then starts IDA:
    on macOS with `open -a` (as the Dock does), elsewhere by running IDA.
    If IDA doesn't exit within RELAUNCH_WAIT seconds (e.g. the user cancelled
    the save dialog), the helper gives up instead of starting IDA later.
    """
    import shlex
    import subprocess

    import idc

    idb = idc.get_idb_path() or ""
    ida_dir = ida_diskio.idadir("")
    pid = os.getpid()
    if sys.platform == "win32":
        exe = os.path.join(ida_dir, "ida.exe")
        start = f"Start-Process -FilePath '{exe}'" + (f" -ArgumentList '\"{idb}\"'" if idb else "")
        script = (f"Wait-Process -Id {pid} -Timeout {RELAUNCH_WAIT} -ErrorAction SilentlyContinue; "
                  f"if (-not (Get-Process -Id {pid} -ErrorAction SilentlyContinue)) {{ {start} }}")
        command = ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", script]
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(command, creationflags=flags, close_fds=True)
    else:
        if sys.platform == "darwin":
            app = os.path.dirname(os.path.dirname(os.path.normpath(ida_dir)))  # IDA ....app
            launch = f"open -a {shlex.quote(app)}" + (f" {shlex.quote(idb)}" if idb else "")
        else:
            launch = shlex.quote(os.path.join(ida_dir, "ida")) + (f" {shlex.quote(idb)}" if idb else "") + " &"
        script = (f"i=0; while kill -0 {pid} 2>/dev/null; do "
                  f"[ $i -ge {RELAUNCH_WAIT} ] && exit 0; sleep 1; i=$((i+1)); done; {launch}")
        subprocess.Popen(["/bin/sh", "-c", script], start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _quit_ida()


def _quit_ida():
    """Quit IDA as File > Exit does (saving as the user chose), once control
    is back in Qt's event loop.

    IDA must not quit while Python code is running: asking it to from Python
    (qexit, or process_ui_action("QuitIDA")) tears IDA down under the Python
    call, and IDA crashes on the way out. So Exit's menu item is clicked
    through a queued Qt call, which runs from the event loop, after this
    Python code has returned.
    """
    app = QtWidgets.QApplication.instance()
    windows = [w for w in app.topLevelWidgets() if isinstance(w, QtWidgets.QMainWindow)]
    action_class = getattr(QtGui, "QAction", None) or QtWidgets.QAction  # Qt 6, Qt 5
    quit_role = getattr(action_class, "MenuRole", action_class).QuitRole
    queued = getattr(QtCore.Qt, "ConnectionType", QtCore.Qt).QueuedConnection
    for window in windows:
        for action in window.actions() + window.findChildren(action_class):
            if action.menuRole() == quit_role and action.isEnabled():
                QtCore.QMetaObject.invokeMethod(action, "trigger", queued)
                return
    for window in windows:  # no Exit item found: close the main window, which quits IDA too
        QtCore.QMetaObject.invokeMethod(window, "close", queued)
        return
