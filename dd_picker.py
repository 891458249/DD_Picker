# -*- coding: utf-8 -*-
"""
DD Picker - A 2D Control Panel for Maya
Similar to MG Picker, provides a zoomable/pannable canvas for character picking.

Supports Maya 2022+ (Python 3), compatible with PySide2 and PySide6.

Usage:
    import dd_picker
    dd_picker.show()
"""

from __future__ import annotations

import sys
import traceback
from typing import Optional

# ---------------------------------------------------------------------------
# Qt compatibility layer – PySide2 (Maya 2022-2024) / PySide6 (Maya 2025+)
# ---------------------------------------------------------------------------
try:
    from PySide6 import QtCore, QtGui, QtWidgets
    from PySide6.QtCore import Qt
    PYSIDE_VERSION = 6
except ImportError:
    from PySide2 import QtCore, QtGui, QtWidgets
    from PySide2.QtCore import Qt
    PYSIDE_VERSION = 2

import maya.cmds as cmds
import maya.OpenMayaUI as omui
from maya.app.general.mayaMixin import MayaQWidgetDockableMixin

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
WINDOW_TITLE = "DD Picker"
WORKSPACE_CONTROL_NAME = "DDPickerWorkspaceControl"

CANVAS_BG_COLOR = QtGui.QColor(50, 50, 50)
GRID_COLOR_MAJOR = QtGui.QColor(70, 70, 70)
GRID_COLOR_MINOR = QtGui.QColor(60, 60, 60)
GRID_SIZE_MINOR = 20
GRID_SIZE_MAJOR = 100

ZOOM_MIN = 0.1
ZOOM_MAX = 10.0
ZOOM_FACTOR = 1.15


# ---------------------------------------------------------------------------
# Canvas – QGraphicsView with zoom / pan / grid
# ---------------------------------------------------------------------------
class PickerGraphicsView(QtWidgets.QGraphicsView):
    """Zoomable, pannable graphics view with a grid background."""

    def __init__(self, scene: QtWidgets.QGraphicsScene, parent: Optional[QtWidgets.QWidget] = None):
        super().__init__(scene, parent)

        # Rendering
        self.setRenderHints(
            QtGui.QPainter.Antialiasing | QtGui.QPainter.SmoothPixmapTransform
        )
        self.setViewportUpdateMode(QtWidgets.QGraphicsView.FullViewportUpdate)
        self.setTransformationAnchor(QtWidgets.QGraphicsView.NoAnchor)
        self.setResizeAnchor(QtWidgets.QGraphicsView.NoAnchor)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        # Interaction state
        self._panning = False
        self._pan_start: QtCore.QPoint = QtCore.QPoint()
        self._current_zoom: float = 1.0

        # Rubber-band (marquee) selection state
        self._rubber_band: Optional[QtWidgets.QRubberBand] = None
        self._rubber_band_active = False
        self._rubber_band_origin: QtCore.QPoint = QtCore.QPoint()

        self.setBackgroundBrush(CANVAS_BG_COLOR)
        self.setSceneRect(-5000, -5000, 10000, 10000)

    # -- Zoom ---------------------------------------------------------------
    def _zoom(self, factor: float, center: QtCore.QPoint) -> None:
        """Apply *factor* zoom centred on viewport *center* point."""
        new_zoom = self._current_zoom * factor
        if new_zoom < ZOOM_MIN or new_zoom > ZOOM_MAX:
            return

        # Map the center point to scene coords *before* scaling.
        old_pos = self.mapToScene(center)

        self.scale(factor, factor)
        self._current_zoom = new_zoom

        # Map the same viewport point *after* scaling, then adjust.
        new_pos = self.mapToScene(center)
        delta = new_pos - old_pos
        self.translate(delta.x(), delta.y())

    # -- Rubber-band (marquee) selection ------------------------------------
    def _start_rubber_band(self, pos: QtCore.QPoint) -> None:
        self._rubber_band_active = True
        self._rubber_band_origin = pos
        if self._rubber_band is None:
            self._rubber_band = QtWidgets.QRubberBand(
                QtWidgets.QRubberBand.Rectangle, self.viewport())
        self._rubber_band.setGeometry(QtCore.QRect(pos, QtCore.QSize()))
        self._rubber_band.show()

    def _update_rubber_band(self, pos: QtCore.QPoint) -> None:
        self._rubber_band.setGeometry(
            QtCore.QRect(self._rubber_band_origin, pos).normalized())

    def _finish_rubber_band(self, modifiers) -> None:
        self._rubber_band.hide()
        self._rubber_band_active = False

        # Determine the selection rect in scene coordinates
        rect = self._rubber_band.geometry()
        scene_rect = QtCore.QRectF(
            self.mapToScene(rect.topLeft()),
            self.mapToScene(rect.bottomRight()),
        )

        add = bool(modifiers & (Qt.ShiftModifier | Qt.ControlModifier))
        if not add:
            self.scene().clearSelection()

        # Select all PickerItems that intersect the rect
        for item in self.scene().items(scene_rect, Qt.IntersectsItemShape):
            if isinstance(item, PickerItem):
                item.setSelected(True)

        # Sync to Maya
        _sync_maya_selection_from_scene(self.scene())

    # -- Events -------------------------------------------------------------
    def wheelEvent(self, event) -> None:  # noqa: N802
        """Zoom with the mouse wheel, centred on the cursor."""
        if PYSIDE_VERSION == 6:
            angle = event.angleDelta().y()
        else:
            angle = event.angleDelta().y()

        factor = ZOOM_FACTOR if angle > 0 else 1.0 / ZOOM_FACTOR

        if PYSIDE_VERSION == 6:
            pos = event.position().toPoint()
        else:
            pos = event.pos()

        self._zoom(factor, pos)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        """MMB / Alt+LMB → pan.  LMB on empty canvas → rubber-band select."""
        btn = event.button()
        mods = event.modifiers()

        # Pan: MMB or Alt+LMB
        if btn == Qt.MiddleButton or (btn == Qt.LeftButton and mods & Qt.AltModifier):
            self._panning = True
            self._pan_start = event.pos()
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()
            return

        # LMB on empty space → start rubber-band
        if btn == Qt.LeftButton:
            item_under = self.itemAt(event.pos())
            if item_under is None:
                self._start_rubber_band(event.pos())
                if not (mods & (Qt.ShiftModifier | Qt.ControlModifier)):
                    self.scene().clearSelection()
                    cmds.select(clear=True)
                event.accept()
                return

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        """Pan or update rubber-band."""
        if self._panning:
            delta = event.pos() - self._pan_start
            self._pan_start = event.pos()
            self.translate(delta.x() / self._current_zoom,
                           delta.y() / self._current_zoom)
            event.accept()
            return
        if self._rubber_band_active:
            self._update_rubber_band(event.pos())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        """Finish pan or rubber-band."""
        if self._panning and event.button() in (Qt.MiddleButton, Qt.LeftButton):
            self._panning = False
            self.setCursor(Qt.ArrowCursor)
            event.accept()
            return
        if self._rubber_band_active and event.button() == Qt.LeftButton:
            self._finish_rubber_band(event.modifiers())
            event.accept()
            return
        super().mouseReleaseEvent(event)

    # -- Grid background ----------------------------------------------------
    def drawBackground(self, painter: QtGui.QPainter, rect: QtCore.QRectF) -> None:  # noqa: N802
        """Draw a grid on the canvas background."""
        super().drawBackground(painter, rect)

        left = int(rect.left()) - (int(rect.left()) % GRID_SIZE_MINOR)
        top = int(rect.top()) - (int(rect.top()) % GRID_SIZE_MINOR)

        # Collect grid lines
        minor_lines: list = []
        major_lines: list = []

        x = left
        while x <= rect.right():
            if x % GRID_SIZE_MAJOR == 0:
                major_lines.append(QtCore.QLineF(x, rect.top(), x, rect.bottom()))
            else:
                minor_lines.append(QtCore.QLineF(x, rect.top(), x, rect.bottom()))
            x += GRID_SIZE_MINOR

        y = top
        while y <= rect.bottom():
            if y % GRID_SIZE_MAJOR == 0:
                major_lines.append(QtCore.QLineF(y=rect.left(), x1=0, x2=0, y1=0)
                                   if False else
                                   QtCore.QLineF(rect.left(), y, rect.right(), y))
            else:
                minor_lines.append(QtCore.QLineF(rect.left(), y, rect.right(), y))
            y += GRID_SIZE_MINOR

        # Draw minor grid
        pen = QtGui.QPen(GRID_COLOR_MINOR, 0.5)
        painter.setPen(pen)
        if minor_lines:
            painter.drawLines(minor_lines)

        # Draw major grid
        pen = QtGui.QPen(GRID_COLOR_MAJOR, 1.0)
        painter.setPen(pen)
        if major_lines:
            painter.drawLines(major_lines)

    # -- Helpers ------------------------------------------------------------
    def reset_view(self) -> None:
        """Reset zoom and pan to default."""
        self.resetTransform()
        self._current_zoom = 1.0
        self.centerOn(0, 0)


# ---------------------------------------------------------------------------
# Scene
# ---------------------------------------------------------------------------
class PickerScene(QtWidgets.QGraphicsScene):
    """The scene that holds all picker items."""

    def __init__(self, parent: Optional[QtCore.QObject] = None):
        super().__init__(parent)
        self.setSceneRect(-5000, -5000, 10000, 10000)


# ---------------------------------------------------------------------------
# Main Window (dockable)
# ---------------------------------------------------------------------------
class DDPickerWindow(MayaQWidgetDockableMixin, QtWidgets.QMainWindow):
    """Dockable Maya window hosting the DD Picker canvas."""

    _instance: Optional["DDPickerWindow"] = None

    def __init__(self, parent: Optional[QtWidgets.QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle(WINDOW_TITLE)
        self.setMinimumSize(400, 300)

        self._build_ui()
        self._build_menu()

    # -- UI -----------------------------------------------------------------
    def _build_ui(self) -> None:
        central = QtWidgets.QWidget(self)
        self.setCentralWidget(central)

        layout = QtWidgets.QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Toolbar
        toolbar = QtWidgets.QHBoxLayout()
        toolbar.setContentsMargins(4, 4, 4, 4)

        self._btn_reset = QtWidgets.QPushButton("Reset View")
        self._btn_reset.setFixedHeight(24)
        self._btn_reset.clicked.connect(self._on_reset_view)
        toolbar.addWidget(self._btn_reset)

        self._lbl_zoom = QtWidgets.QLabel("Zoom: 100%")
        self._lbl_zoom.setFixedHeight(24)
        toolbar.addWidget(self._lbl_zoom)

        toolbar.addStretch()
        layout.addLayout(toolbar)

        # Scene + View
        self._scene = PickerScene(self)
        self._view = PickerGraphicsView(self._scene, self)
        layout.addWidget(self._view)

        # Add a demo item so the canvas isn't empty
        self._add_demo_items()

    def _build_menu(self) -> None:
        menubar = self.menuBar()

        view_menu = menubar.addMenu("View")
        act_reset = view_menu.addAction("Reset View")
        act_reset.triggered.connect(self._on_reset_view)

        act_fit = view_menu.addAction("Fit All")
        act_fit.triggered.connect(self._on_fit_all)

    def _add_demo_items(self) -> None:
        """Add placeholder PickerItems to demonstrate the canvas."""
        demos = [
            # (label, maya_nodes,           x,    y,   w,  h)
            ("Head",  ["head_ctrl"],          0, -120,  60, 28),
            ("Body",  ["body_ctrl"],          0,    0,  70, 30),
            ("L_Arm", ["L_arm_ctrl"],      -100,    0,  60, 28),
            ("R_Arm", ["R_arm_ctrl"],       100,    0,  60, 28),
            ("L_Hand",["L_hand_ctrl"],     -100,   60,  60, 28),
            ("R_Hand",["R_hand_ctrl"],      100,   60,  60, 28),
            ("L_Leg", ["L_leg_ctrl"],       -50,  120,  60, 28),
            ("R_Leg", ["R_leg_ctrl"],        50,  120,  60, 28),
        ]
        for label, nodes, x, y, w, h in demos:
            item = PickerItem(label=label, maya_nodes=nodes, width=w, height=h)
            item.setPos(x, y)
            self._scene.addItem(item)

    # -- Slots --------------------------------------------------------------
    def _on_reset_view(self) -> None:
        self._view.reset_view()
        self._lbl_zoom.setText("Zoom: 100%")

    def _on_fit_all(self) -> None:
        bounds = self._scene.itemsBoundingRect().adjusted(-50, -50, 50, 50)
        self._view.fitInView(bounds, Qt.KeepAspectRatio)
        self._view._current_zoom = self._view.transform().m11()
        self._lbl_zoom.setText(f"Zoom: {int(self._view._current_zoom * 100)}%")

    # -- Override -----------------------------------------------------------
    def dockCloseEventTriggered(self) -> None:  # noqa: N802
        """Called when the dockable widget is closed."""
        DDPickerWindow._instance = None
        super().dockCloseEventTriggered()


# ---------------------------------------------------------------------------
# Picker Item – core interactive button component
# ---------------------------------------------------------------------------
class PickerItem(QtWidgets.QGraphicsObject):
    """A clickable picker button that selects associated Maya nodes.

    Attributes:
        maya_nodes: List of Maya node names associated with this button.
        label:      Display text on the button.
    """

    # --- Visual defaults ---------------------------------------------------
    CORNER_RADIUS = 6

    COLOR_DEFAULT = QtGui.QColor(68, 68, 68)
    COLOR_HOVER = QtGui.QColor(88, 88, 88)
    COLOR_SELECTED = QtGui.QColor(210, 180, 50)

    BORDER_DEFAULT = QtGui.QColor(90, 90, 90)
    BORDER_HOVER = QtGui.QColor(180, 180, 180)
    BORDER_SELECTED = QtGui.QColor(240, 210, 60)

    TEXT_DEFAULT = QtGui.QColor(200, 200, 200)
    TEXT_SELECTED = QtGui.QColor(30, 30, 30)

    def __init__(
        self,
        label: str,
        maya_nodes: Optional[list] = None,
        width: float = 60,
        height: float = 30,
        parent: Optional[QtWidgets.QGraphicsItem] = None,
    ):
        super().__init__(parent)
        self._label = label
        self._maya_nodes: list = list(maya_nodes) if maya_nodes else []
        self._width = width
        self._height = height
        self._hover = False

        self.setAcceptHoverEvents(True)
        self.setFlag(QtWidgets.QGraphicsItem.ItemIsSelectable, True)

    # --- Properties --------------------------------------------------------
    @property
    def label(self) -> str:
        return self._label

    @label.setter
    def label(self, value: str) -> None:
        self._label = value
        self.update()

    @property
    def maya_nodes(self) -> list:
        return self._maya_nodes

    @maya_nodes.setter
    def maya_nodes(self, value: list) -> None:
        self._maya_nodes = list(value)

    # --- QGraphicsItem overrides -------------------------------------------
    def boundingRect(self) -> QtCore.QRectF:  # noqa: N802
        return QtCore.QRectF(-self._width / 2, -self._height / 2,
                              self._width, self._height)

    def paint(self, painter: QtGui.QPainter,
              option: QtWidgets.QStyleOptionGraphicsItem,
              widget: Optional[QtWidgets.QWidget] = None) -> None:
        rect = self.boundingRect()
        selected = self.isSelected()

        # Fill
        if selected:
            bg = self.COLOR_SELECTED
        elif self._hover:
            bg = self.COLOR_HOVER
        else:
            bg = self.COLOR_DEFAULT

        # Border
        if selected:
            border = self.BORDER_SELECTED
            pen_width = 2.0
        elif self._hover:
            border = self.BORDER_HOVER
            pen_width = 1.5
        else:
            border = self.BORDER_DEFAULT
            pen_width = 1.0

        painter.setPen(QtGui.QPen(border, pen_width))
        painter.setBrush(bg)
        painter.drawRoundedRect(rect, self.CORNER_RADIUS, self.CORNER_RADIUS)

        # Label
        painter.setPen(self.TEXT_SELECTED if selected else self.TEXT_DEFAULT)
        font = painter.font()
        font.setPointSize(9)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(rect, Qt.AlignCenter, self._label)

    def shape(self) -> QtGui.QPainterPath:
        """Precise hit-test shape matching the rounded rect."""
        path = QtGui.QPainterPath()
        path.addRoundedRect(self.boundingRect(),
                            self.CORNER_RADIUS, self.CORNER_RADIUS)
        return path

    # --- Hover events ------------------------------------------------------
    def hoverEnterEvent(self, event) -> None:  # noqa: N802
        self._hover = True
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event) -> None:  # noqa: N802
        self._hover = False
        self.update()
        super().hoverLeaveEvent(event)

    # --- Click → Maya selection --------------------------------------------
    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            add = bool(event.modifiers() & (Qt.ShiftModifier | Qt.ControlModifier))
            self._select_maya_nodes(add=add)
            # Toggle QGraphicsScene selection state to match
            if add:
                self.setSelected(not self.isSelected())
            else:
                # Deselect all others, select this one
                if self.scene():
                    for item in self.scene().selectedItems():
                        if item is not self:
                            item.setSelected(False)
                self.setSelected(True)
            event.accept()
            return
        super().mousePressEvent(event)

    def _select_maya_nodes(self, add: bool = False) -> None:
        """Run maya.cmds.select for associated nodes."""
        if not self._maya_nodes:
            return
        try:
            existing = [n for n in self._maya_nodes if cmds.objExists(n)]
            missing = [n for n in self._maya_nodes if n not in existing]
            if missing:
                cmds.warning("DD Picker: nodes not found: {}".format(
                    ", ".join(missing)))
            if existing:
                cmds.select(existing, add=add, replace=not add)
        except Exception:
            traceback.print_exc()


# ---------------------------------------------------------------------------
# Selection sync helper
# ---------------------------------------------------------------------------
def _sync_maya_selection_from_scene(scene: QtWidgets.QGraphicsScene) -> None:
    """Collect all selected PickerItems and select their Maya nodes."""
    nodes: list = []
    for item in scene.selectedItems():
        if isinstance(item, PickerItem):
            nodes.extend(item.maya_nodes)
    try:
        if nodes:
            existing = [n for n in nodes if cmds.objExists(n)]
            if existing:
                cmds.select(existing, replace=True)
        else:
            cmds.select(clear=True)
    except Exception:
        traceback.print_exc()


# ---------------------------------------------------------------------------
# Public API – add_picker_item
# ---------------------------------------------------------------------------
def add_picker_item(
    label: str,
    maya_nodes: list,
    x: float = 0,
    y: float = 0,
    w: float = 60,
    h: float = 30,
) -> Optional[PickerItem]:
    """Add a PickerItem to the active DD Picker scene.

    Args:
        label:      Display text on the button.
        maya_nodes:  List of Maya node names to select on click.
        x, y:       Position on the canvas (scene coordinates).
        w, h:       Width and height of the button.

    Returns:
        The created PickerItem, or None if the window is not open.

    Example::

        import dd_picker
        dd_picker.show()
        dd_picker.add_picker_item("L_Hand", ["L_hand_ctrl"], -120, 60, 70, 28)
        dd_picker.add_picker_item("R_Hand", ["R_hand_ctrl"],  120, 60, 70, 28)
    """
    win = DDPickerWindow._instance
    if win is None:
        cmds.warning("DD Picker: Window is not open. Call dd_picker.show() first.")
        return None
    item = PickerItem(label=label, maya_nodes=maya_nodes, width=w, height=h)
    item.setPos(x, y)
    win._scene.addItem(item)
    return item


# ---------------------------------------------------------------------------
# Public API – show / close
# ---------------------------------------------------------------------------
def show() -> DDPickerWindow:
    """Show the DD Picker window. Creates it if needed, otherwise raises existing."""
    _close_existing()

    win = DDPickerWindow()
    win.show(dockable=True, area="right",
             allowedArea="all",
             floating=True,
             width=600, height=500)

    DDPickerWindow._instance = win
    return win


def _close_existing() -> None:
    """Close any previously opened instance."""
    if DDPickerWindow._instance is not None:
        try:
            DDPickerWindow._instance.close()
        except Exception:
            pass
        DDPickerWindow._instance = None

    # Clean up workspace control if it lingers
    ctrl_name = WORKSPACE_CONTROL_NAME
    if cmds.workspaceControl(ctrl_name, exists=True):
        cmds.deleteUI(ctrl_name)
