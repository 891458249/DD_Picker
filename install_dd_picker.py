# -*- coding: utf-8 -*-
"""
DD Picker - Drag & Drop Installer for Maya

Drag this file into the Maya viewport to install, reinstall, or uninstall.
Supports Maya 2022+ (Python 3).
"""

import os
import sys
import shutil
import traceback

import maya.cmds as cmds
import maya.mel as mel

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
PLUGIN_NAME = "DD Picker"
PLUGIN_VERSION = "1.0.0"
MODULE_NAME = "dd_picker"                       # the .py we ship
SHELF_NAME = "DD_Tools"                         # shelf tab name
SHELF_BUTTON_LABEL = "DDPkr"                    # short label on the button
SHELF_BUTTON_ANNOTATION = "DD Picker - 2D Control Panel"
SHELF_BUTTON_COMMAND = """
import dd_picker
dd_picker.show()
"""

# Icon embedded as a simple Maya built-in icon fallback
SHELF_BUTTON_IMAGE = "pickOtherObj.png"


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------
def _source_dir():
    """Directory where this installer script lives."""
    return os.path.dirname(os.path.abspath(__file__))


def _source_module():
    """Path to the plugin source file next to this installer."""
    return os.path.join(_source_dir(), MODULE_NAME + ".py")


def _maya_scripts_dir():
    """Return the user's Maya scripts directory for the current version."""
    version = cmds.about(version=True)          # e.g. "2024"
    base = os.environ.get("MAYA_APP_DIR", "")
    if not base:
        if sys.platform == "win32":
            base = os.path.join(os.environ["USERPROFILE"], "Documents", "maya")
        elif sys.platform == "darwin":
            base = os.path.join(os.path.expanduser("~"), "Library", "Preferences",
                                "Autodesk", "maya")
        else:
            base = os.path.join(os.path.expanduser("~"), "maya")
    scripts_dir = os.path.join(base, version, "scripts")
    return scripts_dir


def _installed_module():
    """Path where the plugin module would be installed."""
    return os.path.join(_maya_scripts_dir(), MODULE_NAME + ".py")


def _is_installed():
    """Check if DD Picker is already installed."""
    return os.path.isfile(_installed_module())


# ---------------------------------------------------------------------------
# Shelf helpers
# ---------------------------------------------------------------------------
def _get_or_create_shelf():
    """Return the shelf layout for our shelf tab, creating it if needed."""
    top_shelf = mel.eval("$__dd_tmp = $gShelfTopLevel")

    if cmds.shelfTabLayout(top_shelf, query=True, childArray=True):
        existing = cmds.shelfTabLayout(top_shelf, query=True, childArray=True)
        if SHELF_NAME in existing:
            return top_shelf + "|" + SHELF_NAME

    cmds.shelfLayout(SHELF_NAME, parent=top_shelf)
    return top_shelf + "|" + SHELF_NAME


def _find_shelf_button():
    """Find our shelf button if it exists. Returns button name or None."""
    top_shelf = mel.eval("$__dd_tmp = $gShelfTopLevel")
    existing_tabs = cmds.shelfTabLayout(top_shelf, query=True, childArray=True) or []

    if SHELF_NAME not in existing_tabs:
        return None

    shelf_path = top_shelf + "|" + SHELF_NAME
    buttons = cmds.shelfLayout(shelf_path, query=True, childArray=True) or []

    for btn in buttons:
        full_btn = shelf_path + "|" + btn
        try:
            label = cmds.shelfButton(full_btn, query=True, label=True)
            if label == SHELF_BUTTON_LABEL:
                return full_btn
        except Exception:
            continue
    return None


def _add_shelf_button():
    """Add a shelf button for DD Picker."""
    shelf = _get_or_create_shelf()

    # Remove old button if exists
    old_btn = _find_shelf_button()
    if old_btn:
        cmds.deleteUI(old_btn)

    cmds.shelfButton(
        parent=shelf,
        label=SHELF_BUTTON_LABEL,
        annotation=SHELF_BUTTON_ANNOTATION,
        image1=SHELF_BUTTON_IMAGE,
        command=SHELF_BUTTON_COMMAND,
        sourceType="python",
    )


def _remove_shelf_button():
    """Remove the DD Picker shelf button if it exists."""
    btn = _find_shelf_button()
    if btn:
        cmds.deleteUI(btn)


# ---------------------------------------------------------------------------
# Install / Uninstall
# ---------------------------------------------------------------------------
def _install():
    """Copy plugin files to Maya scripts dir and add shelf button."""
    src = _source_module()
    if not os.path.isfile(src):
        cmds.error(
            "{} not found next to the installer.\n"
            "Expected: {}".format(MODULE_NAME + ".py", src)
        )
        return False

    dst_dir = _maya_scripts_dir()
    os.makedirs(dst_dir, exist_ok=True)

    dst = _installed_module()
    shutil.copy2(src, dst)

    _add_shelf_button()

    # Force reload if it was previously imported
    if MODULE_NAME in sys.modules:
        del sys.modules[MODULE_NAME]

    cmds.confirmDialog(
        title=PLUGIN_NAME,
        message=(
            "{plugin} v{ver} installed successfully!\n\n"
            "Installed to:\n{path}\n\n"
            "A shelf button \"{btn}\" has been added to the \"{shelf}\" shelf.\n"
            "Click it or run:\n"
            "    import dd_picker; dd_picker.show()"
        ).format(
            plugin=PLUGIN_NAME, ver=PLUGIN_VERSION,
            path=dst, btn=SHELF_BUTTON_LABEL, shelf=SHELF_NAME,
        ),
        button=["OK"],
        defaultButton="OK",
    )
    return True


def _uninstall():
    """Remove plugin files and shelf button."""
    dst = _installed_module()
    removed_file = False

    if os.path.isfile(dst):
        os.remove(dst)
        removed_file = True

    # Also clean .pyc / __pycache__
    pyc = dst + "c"
    if os.path.isfile(pyc):
        os.remove(pyc)

    cache_dir = os.path.join(_maya_scripts_dir(), "__pycache__")
    if os.path.isdir(cache_dir):
        for f in os.listdir(cache_dir):
            if f.startswith(MODULE_NAME + "."):
                os.remove(os.path.join(cache_dir, f))

    _remove_shelf_button()

    # Unload module from memory
    if MODULE_NAME in sys.modules:
        del sys.modules[MODULE_NAME]

    if removed_file:
        cmds.confirmDialog(
            title=PLUGIN_NAME,
            message="{} has been uninstalled.\n\nRemoved:\n{}".format(
                PLUGIN_NAME, dst
            ),
            button=["OK"],
            defaultButton="OK",
        )
    else:
        cmds.confirmDialog(
            title=PLUGIN_NAME,
            message="{} was not installed.".format(PLUGIN_NAME),
            button=["OK"],
            defaultButton="OK",
        )


# ---------------------------------------------------------------------------
# Installer dialog
# ---------------------------------------------------------------------------
def _show_installer_dialog():
    """Show a dialog that offers Install, Reinstall, or Uninstall."""
    installed = _is_installed()

    if installed:
        result = cmds.confirmDialog(
            title="{} Installer".format(PLUGIN_NAME),
            message=(
                "{plugin} v{ver} is currently installed.\n\n"
                "Installed at:\n{path}\n\n"
                "What would you like to do?"
            ).format(
                plugin=PLUGIN_NAME, ver=PLUGIN_VERSION,
                path=_installed_module(),
            ),
            button=["Reinstall", "Uninstall", "Cancel"],
            defaultButton="Reinstall",
            cancelButton="Cancel",
            dismissString="Cancel",
        )

        if result == "Reinstall":
            _install()
        elif result == "Uninstall":
            _uninstall()
    else:
        result = cmds.confirmDialog(
            title="{} Installer".format(PLUGIN_NAME),
            message=(
                "Install {plugin} v{ver}?\n\n"
                "This will:\n"
                "  - Copy {module}.py to your Maya scripts directory\n"
                "  - Add a \"{btn}\" button to the \"{shelf}\" shelf"
            ).format(
                plugin=PLUGIN_NAME, ver=PLUGIN_VERSION,
                module=MODULE_NAME, btn=SHELF_BUTTON_LABEL, shelf=SHELF_NAME,
            ),
            button=["Install", "Cancel"],
            defaultButton="Install",
            cancelButton="Cancel",
            dismissString="Cancel",
        )

        if result == "Install":
            _install()


# ---------------------------------------------------------------------------
# Maya drag-and-drop entry point
# ---------------------------------------------------------------------------
def onMayaDroppedPythonFile(*args, **kwargs):
    """Called by Maya when this .py file is dragged into the viewport."""
    try:
        _show_installer_dialog()
    except Exception:
        traceback.print_exc()
        cmds.confirmDialog(
            title="{} - Error".format(PLUGIN_NAME),
            message="Installation failed. See Script Editor for details.",
            button=["OK"],
        )
