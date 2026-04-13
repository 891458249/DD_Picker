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
        """Start panning on MMB or Alt+LMB."""
        if (event.button() == Qt.MiddleButton or
                (event.button() == Qt.LeftButton and event.modifiers() & Qt.AltModifier)):
            self._panning = True
            self._pan_start = event.pos()
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        """Pan the view while the pan gesture is active."""
        if self._panning:
            delta = event.pos() - self._pan_start
            self._pan_start = event.pos()
            self.translate(delta.x() / self._current_zoom,
                           delta.y() / self._current_zoom)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        """End panning."""
        if self._panning and event.button() in (Qt.MiddleButton, Qt.LeftButton):
            self._panning = False
            self.setCursor(Qt.ArrowCursor)
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
        """Add placeholder items to demonstrate the canvas."""
        colors = [
            (QtGui.QColor(80, 140, 200), "Head",  0, -120),
            (QtGui.QColor(200, 80, 80),  "L_Arm", -100, 0),
            (QtGui.QColor(80, 200, 80),  "R_Arm", 100, 0),
            (QtGui.QColor(200, 180, 60), "Body",  0, 0),
            (QtGui.QColor(80, 140, 200), "L_Leg", -50, 120),
            (QtGui.QColor(80, 140, 200), "R_Leg", 50, 120),
        ]
        for color, label, x, y in colors:
            item = PickerButtonItem(label, color)
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
# Picker Button Item (demo interactive item)
# ---------------------------------------------------------------------------
class PickerButtonItem(QtWidgets.QGraphicsItem):
    """A clickable button on the picker canvas that selects a Maya control."""

    WIDTH = 60
    HEIGHT = 30
    CORNER_RADIUS = 6

    def __init__(self, label: str, color: QtGui.QColor, parent: Optional[QtWidgets.QGraphicsItem] = None):
        super().__init__(parent)
        self._label = label
        self._color = color
        self._hover = False

        self.setAcceptHoverEvents(True)
        self.setFlag(QtWidgets.QGraphicsItem.ItemIsSelectable, True)

    def boundingRect(self) -> QtCore.QRectF:  # noqa: N802
        return QtCore.QRectF(-self.WIDTH / 2, -self.HEIGHT / 2,
                              self.WIDTH, self.HEIGHT)

    def paint(self, painter: QtGui.QPainter,
              option: QtWidgets.QStyleOptionGraphicsItem,
              widget: Optional[QtWidgets.QWidget] = None) -> None:
        rect = self.boundingRect()

        # Background
        bg = self._color.lighter(130) if self._hover else self._color
        if self.isSelected():
            bg = self._color.lighter(160)
        painter.setBrush(bg)
        painter.setPen(QtGui.QPen(self._color.darker(150), 1.5))
        painter.drawRoundedRect(rect, self.CORNER_RADIUS, self.CORNER_RADIUS)

        # Label
        painter.setPen(Qt.white)
        font = painter.font()
        font.setPointSize(9)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(rect, Qt.AlignCenter, self._label)

    def hoverEnterEvent(self, event) -> None:  # noqa: N802
        self._hover = True
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event) -> None:  # noqa: N802
        self._hover = False
        self.update()
        super().hoverLeaveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            modifiers = event.modifiers()
            # Ctrl/Shift for add/toggle selection
            if modifiers & Qt.ControlModifier or modifiers & Qt.ShiftModifier:
                self._select_control(add=True)
            else:
                self._select_control(add=False)
            event.accept()
            return
        super().mousePressEvent(event)

    def _select_control(self, add: bool = False) -> None:
        """Select the Maya object matching this button's label."""
        try:
            if cmds.objExists(self._label):
                if add:
                    cmds.select(self._label, add=True)
                else:
                    cmds.select(self._label, replace=True)
            else:
                cmds.warning(f"DD Picker: Object '{self._label}' not found in scene.")
        except Exception:
            traceback.print_exc()


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
