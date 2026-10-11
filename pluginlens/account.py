"""Logging in to hcli (the Hex-Rays portal), as `hcli login` does: with
Google in the browser, a code sent by email, or an API key. Logged in, hcli
also lists the plugins of the hexrays repository the account may use."""

import asyncio
import os
import threading

import ida_kernwin

from .common import QtWidgets


def _auth():
    from hcli.lib.auth import get_auth_service

    service = get_auth_service()
    service.init()
    return service


def status():
    """(logged in, text to show), from the stored credentials only: without
    the network (hcli checks them when it uses them)."""
    try:
        if os.environ.get("HCLI_API_KEY"):
            return True, "Logged in with the API key in HCLI_API_KEY."
        current = _auth().get_current_credentials()
        if current is None:
            return False, "Not logged in: hcli doesn't list the hexrays repository's plugins."
        who = getattr(current, "email", None) or getattr(current, "name", None) or "an account"
        return True, f"Logged in as {who}."
    except Exception as e:  # hcli not importable, or changed
        return False, f"hcli's login can't be checked: {type(e).__name__}: {e}"


def _finish(source, on_done):
    """Make the new credentials the default if they're the only ones, as hcli does."""
    try:
        service = _auth()
        if source is not None and len(service.list_credentials()) == 1:
            service.set_default_credentials(source.name)
    except Exception:
        pass
    on_done(source is not None)


def _in_background(work, on_done):
    """Run work() (which may use asyncio) in a daemon thread, then on_done(result) on IDA's main thread."""

    def worker():
        try:
            result, error = work(), None
        except Exception as e:  # network, hcli, the user closed the browser...
            result, error = None, f"{type(e).__name__}: {e}"

        def done():
            if error:
                ida_kernwin.warning(f"Logging in failed: {error}")
            on_done(result)
            return 0

        ida_kernwin.execute_sync(done, ida_kernwin.MFF_NOWAIT)

    threading.Thread(target=worker, name="plugin-lens-login", daemon=True).start()


def log_in(parent, on_done):
    """Ask how to log in, and do it; on_done(logged_in) when finished."""
    methods = ["Google, in the browser", "A code sent by email", "An API key (from the Hex-Rays portal)"]
    method, ok = QtWidgets.QInputDialog.getItem(parent, "Log in to hcli", "Log in with:", methods, 0, False)
    if not ok:
        return
    if method == methods[0]:
        ida_kernwin.info("Your browser opens to sign in with Google. Come back to IDA when it's done "
                         "(hcli waits up to 2 minutes).")
        _in_background(lambda: asyncio.run(_auth().login_interactive()),
                       lambda source: _finish(source, on_done))
    elif method == methods[1]:
        email, ok = QtWidgets.QInputDialog.getText(parent, "Log in to hcli", "Email address:")
        if not ok or not email.strip():
            return
        email = email.strip()

        def sent(result):
            if not result:
                on_done(False)
                return
            code, ok = QtWidgets.QInputDialog.getText(parent, "Log in to hcli", f"The code sent to {email}:")
            if not ok or not code.strip():
                on_done(False)
                return
            try:
                source = _auth().verify_otp(email, code.strip())
            except Exception as e:
                ida_kernwin.warning(f"Logging in failed: {type(e).__name__}: {e}")
                source = None
            if source is None:
                ida_kernwin.warning("That code didn't work.")
            _finish(source, on_done)

        _in_background(lambda: asyncio.run(_auth().login_otp(email)) is not False, sent)
    else:
        key, ok = QtWidgets.QInputDialog.getText(parent, "Log in to hcli", "API key:",
                                                 QtWidgets.QLineEdit.EchoMode.Password)
        if not ok or not key.strip():
            return
        _in_background(lambda: asyncio.run(_auth().add_api_key_credentials(_unique_name("api-key"), key.strip())),
                       lambda source: _finish(source, on_done))


def _unique_name(base):
    try:
        return _auth().generate_unique_name(base)
    except Exception:
        return base


def log_out(on_done):
    """Remove the current credentials, as `hcli logout` does."""
    try:
        service = _auth()
        current = service.get_current_credentials()
        if current is None:
            on_done(False)
            return
        who = getattr(current, "email", None) or current.name
        if ida_kernwin.ask_yn(ida_kernwin.ASKBTN_NO, f"Log out of hcli, removing the credentials of {who}?") \
                != ida_kernwin.ASKBTN_YES:
            return
        service.logout_current()
        service.remove_credentials(current.name)
    except Exception as e:
        ida_kernwin.warning(f"Logging out failed: {type(e).__name__}: {e}")
    on_done(False)
