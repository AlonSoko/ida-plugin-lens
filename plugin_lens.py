"""Plugin Lens: icons, groups, versions and updates for IDA's plugins.

Edit > Plugins:
- Icons: every entry without an icon gets, in order, an image you put in
  <IDA user directory>/plugin-lens/ named after the menu entry (an empty
  "<entry>.none" leaves it without one), the logo the plugin declares in its
  ida-plugin.json ("logoPath"), or an icon from this plugin's icons/ folder.
  Icons a plugin sets itself are left alone.
- Groups: IDA's own plugins, plugins installed with hcli (split by the
  repository they came from, or a local file) and plugins installed
  manually, each under a header.
- Versions after each name, tagged out-dated or manual.
- "Update plugins (N)..." when hcli has newer versions.
- "Plugin marketplace...": a window to browse the plugins in hcli's
  repositories (filter, sort, see their details, install them), and to list
  every installed plugin (update, uninstall or locate them).

hcli is asked through its own Python code, in the background, each time IDA
starts (IDA must be able to import hcli).

Settings, in <IDA user directory>/plugin-lens/settings.json:
  {"group_menu": false}     keep IDA's own order
  {"show_versions": false}  don't show the versions
  {"hcli_sources": false}   don't ask hcli: one group for all hcli plugins
  {"update_action": false}  no "Update plugins" entry when updates exist
  {"fetch_logos": false}    the marketplace downloads no logos
"""

import os
import sys

import ida_idaapi
import ida_kernwin

# the package next to this file
_HERE = os.path.dirname(os.path.realpath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from pluginlens.common import PLUGIN_NAME  # noqa: E402
from pluginlens.library import MarketplaceForm  # noqa: E402
from pluginlens.menu import (  # noqa: E402
    MARKETPLACE_ACTION, MARKETPLACE_LABEL, UPDATE_ACTION, Handler, MenuHooks,
)

SUBVIEWS_MENU = "View/Open subviews/"


class PluginLens(ida_idaapi.plugin_t):
    flags = ida_idaapi.PLUGIN_FIX | ida_idaapi.PLUGIN_HIDE
    comment = "Icons, groups, versions and updates for the entries in Edit > Plugins"
    help = "Uses each plugin's logo, or your own images in <IDA user directory>/plugin-lens/"
    wanted_name = PLUGIN_NAME
    wanted_hotkey = ""

    def init(self):
        if not ida_kernwin.is_idaq():
            return ida_idaapi.PLUGIN_SKIP  # no menus without the GUI
        self.hooks = MenuHooks()
        ida_kernwin.register_action(ida_kernwin.action_desc_t(
            UPDATE_ACTION, "Update plugins…", Handler(self.hooks.update_plugins), "",
            "Update the plugins hcli has newer versions of", -1,
        ))
        ida_kernwin.register_action(ida_kernwin.action_desc_t(
            MARKETPLACE_ACTION, MARKETPLACE_LABEL, Handler(lambda: MarketplaceForm.open(self.hooks)), "",
            "Browse and install plugins from hcli's repositories; update, uninstall or locate the installed ones", -1,
        ))
        ida_kernwin.attach_action_to_menu(SUBVIEWS_MENU, MARKETPLACE_ACTION, ida_kernwin.SETMENU_APP)
        self.hooks.hook()
        return ida_idaapi.PLUGIN_KEEP

    def run(self, arg):
        MarketplaceForm.open(self.hooks)

    def term(self):
        # A PLUGIN_FIX plugin is unloaded only when IDA exits, after its UI is
        # gone: UI calls here (free_custom_icon, detach_action_from_menu, ...)
        # would crash IDA. IDA releases the icons and actions itself.
        hooks = getattr(self, "hooks", None)
        if hooks is not None:
            hooks.unhook()


def PLUGIN_ENTRY():
    return PluginLens()
