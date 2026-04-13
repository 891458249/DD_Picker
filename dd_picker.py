# -*- coding: utf-8 -*-
"""
DD Picker - A 2D Picker Control Panel for Maya
===============================================

A professional character-picker tool with Design / Animation dual-mode
workflow, background image support, snap-to-grid, and resolution-independent
logical coordinates.

Supports Maya 2022+ (Python 3).  Compatible with PySide2 **and** PySide6.

Quick start (Maya Script Editor)::

    import dd_picker
    dd_picker.show()
"""

from __future__ import annotations

import sys
import math
import traceback
from enum import Enum, auto
from typing import Optional, List

# ---------------------------------------------------------------------------
# Qt compatibility shim
# ---------------------------------------------------------------------------
try:
    from PySide6 import QtCore, QtGui, QtWidgets
    from PySide6.QtCore import Qt, Signal
    PYSIDE_VERSION = 6
except ImportError:
    from PySide2 import QtCore, QtGui, QtWidgets
    from PySide2.QtCore import Qt, Signal
    PYSIDE_VERSION = 2

import maya.cmds as cmds
import maya.mel as mel
from maya.app.general.mayaMixin import MayaQWidgetDockableMixin


# ╔═════════════════════════════════════════════════════════════════════════╗
# ║  CONSTANTS & ENUMS                                                     ║
# ╚═════════════════════════════════════════════════════════════════════════╝

WINDOW_TITLE = "DD Picker"
WORKSPACE_CONTROL_NAME = "DDPickerWorkspaceControl"

# Logical coordinate space — every layout is authored in this virtual canvas.
# The view maps this space onto whatever physical resolution the monitor has.
LOGICAL_WIDTH = 1000.0
LOGICAL_HEIGHT = 1000.0

# Grid
GRID_SIZE = 20                                   # logical units
SNAP_THRESHOLD = GRID_SIZE / 2.0

# Colours
COL_CANVAS_BG     = QtGui.QColor(42, 42, 42)
COL_GRID_MINOR    = QtGui.QColor(52, 52, 52)
COL_GRID_MAJOR    = QtGui.QColor(62, 62, 62)
COL_GRID_ORIGIN   = QtGui.QColor(80, 80, 80)
COL_SNAP_GUIDE    = QtGui.QColor(100, 200, 255, 100)

COL_ITEM_DEFAULT  = QtGui.QColor(68, 68, 68)
COL_ITEM_HOVER    = QtGui.QColor(88, 88, 88)
COL_ITEM_SELECTED = QtGui.QColor(210, 180, 50)
COL_BORDER_DEFAULT  = QtGui.QColor(90, 90, 90)
COL_BORDER_HOVER    = QtGui.QColor(180, 180, 180)
COL_BORDER_SELECTED = QtGui.QColor(240, 210, 60)
COL_TEXT_DEFAULT   = QtGui.QColor(200, 200, 200)
COL_TEXT_SELECTED  = QtGui.QColor(30, 30, 30)

ZOOM_MIN = 0.1
ZOOM_MAX = 10.0
ZOOM_FACTOR = 1.15
CORNER_RADIUS = 6


class PickerMode(Enum):
    """Interaction mode for the picker."""
    DESIGN    = auto()   # items are movable / resizable / rotatable
    ANIMATION = auto()   # items are locked; clicks trigger Maya selection


# ╔═════════════════════════════════════════════════════════════════════════╗
# ║  GRID & COORDINATE HELPERS                                             ║
# ╚═════════════════════════════════════════════════════════════════════════╝

def snap_value(value: float, grid: float = GRID_SIZE) -> float:
    """Snap *value* to the nearest grid increment."""
    return round(value / grid) * grid


def snap_point(point: QtCore.QPointF, grid: float = GRID_SIZE) -> QtCore.QPointF:
    """Snap a QPointF to the nearest grid intersection."""
    return QtCore.QPointF(snap_value(point.x(), grid),
                          snap_value(point.y(), grid))


# ╔═════════════════════════════════════════════════════════════════════════╗
# ║  RESIZE HANDLE  (design-mode only)                                     ║
# ╚═════════════════════════════════════════════════════════════════════════╝

class _ResizeHandle(QtWidgets.QGraphicsRectItem):
    """Tiny corner handle that lets the user resize a PickerItem."""

    SIZE = 8

    def __init__(self, parent: "PickerItem"):
        half = self.SIZE / 2
        super().__init__(-half, -half, self.SIZE, self.SIZE, parent)
        self._parent_item = parent
        self.setBrush(QtGui.QColor(255, 255, 255, 180))
        self.setPen(QtGui.QPen(Qt.NoPen))
        self.setFlag(QtWidgets.QGraphicsItem.ItemIsMovable, False)
        self.setCursor(Qt.SizeFDiagCursor)
        self.setAcceptHoverEvents(True)
        self.setVisible(False)
        self._drag_origin = QtCore.QPointF()
        self._orig_size = QtCore.QSizeF()

    def reposition(self) -> None:
        """Place handle at bottom-right of parent bounding rect."""
        r = self._parent_item.boundingRect()
        self.setPos(r.right(), r.bottom())

    # -- interaction --------------------------------------------------------
    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._drag_origin = event.scenePos()
            self._orig_size = QtCore.QSizeF(self._parent_item._width,
                                            self._parent_item._height)
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        delta = event.scenePos() - self._drag_origin
        new_w = max(20, self._orig_size.width() + delta.x())
        new_h = max(14, self._orig_size.height() + delta.y())
        if self._parent_item._snap_enabled:
            new_w = snap_value(new_w)
            new_h = snap_value(new_h)
        self._parent_item.resize(new_w, new_h)
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        event.accept()


# ╔═════════════════════════════════════════════════════════════════════════╗
# ║  PICKER ITEM                                                           ║
# ╚═════════════════════════════════════════════════════════════════════════╝

class PickerItem(QtWidgets.QGraphicsObject):
    """Core interactive button that lives on the picker canvas.

    In **Design mode** the item can be dragged, resized and rotated.
    In **Animation mode** clicks trigger ``maya.cmds.select``.
    """

    def __init__(
        self,
        label: str = "",
        maya_nodes: Optional[List[str]] = None,
        width: float = 60.0,
        height: float = 30.0,
        parent: Optional[QtWidgets.QGraphicsItem] = None,
    ):
        super().__init__(parent)
        self._label: str = label
        self._maya_nodes: List[str] = list(maya_nodes) if maya_nodes else []
        self._width: float = width
        self._height: float = height
        self._hover: bool = False
        self._mode: PickerMode = PickerMode.ANIMATION
        self._snap_enabled: bool = True
        self._drag_offset = QtCore.QPointF()

        self.setAcceptHoverEvents(True)
        self.setFlag(QtWidgets.QGraphicsItem.ItemIsSelectable, True)
        self.setFlag(QtWidgets.QGraphicsItem.ItemSendsGeometryChanges, True)

        # Resize handle (hidden until Design mode + selected)
        self._handle = _ResizeHandle(self)

    # -- properties ---------------------------------------------------------
    @property
    def label(self) -> str:
        return self._label

    @label.setter
    def label(self, value: str) -> None:
        self._label = value
        self.update()

    @property
    def maya_nodes(self) -> List[str]:
        return self._maya_nodes

    @maya_nodes.setter
    def maya_nodes(self, value: List[str]) -> None:
        self._maya_nodes = list(value)

    # -- mode switching -----------------------------------------------------
    def set_mode(self, mode: PickerMode) -> None:
        self._mode = mode
        is_design = (mode == PickerMode.DESIGN)
        self.setFlag(QtWidgets.QGraphicsItem.ItemIsMovable, is_design)
        self._handle.setVisible(is_design and self.isSelected())
        self.update()

    # -- geometry -----------------------------------------------------------
    def resize(self, w: float, h: float) -> None:
        self.prepareGeometryChange()
        self._width = w
        self._height = h
        self._handle.reposition()
        self.update()

    def boundingRect(self) -> QtCore.QRectF:
        return QtCore.QRectF(-self._width / 2, -self._height / 2,
                              self._width, self._height)

    def shape(self) -> QtGui.QPainterPath:
        p = QtGui.QPainterPath()
        p.addRoundedRect(self.boundingRect(), CORNER_RADIUS, CORNER_RADIUS)
        return p

    # -- painting -----------------------------------------------------------
    def paint(self, painter: QtGui.QPainter,
              option: QtWidgets.QStyleOptionGraphicsItem,
              widget: Optional[QtWidgets.QWidget] = None) -> None:
        rect = self.boundingRect()
        selected = self.isSelected()

        # fill colour
        if selected:
            bg = COL_ITEM_SELECTED
        elif self._hover:
            bg = COL_ITEM_HOVER
        else:
            bg = COL_ITEM_DEFAULT

        # border
        if selected:
            border, pw = COL_BORDER_SELECTED, 2.0
        elif self._hover:
            border, pw = COL_BORDER_HOVER, 1.5
        else:
            border, pw = COL_BORDER_DEFAULT, 1.0

        painter.setPen(QtGui.QPen(border, pw))
        painter.setBrush(bg)
        painter.drawRoundedRect(rect, CORNER_RADIUS, CORNER_RADIUS)

        # label
        painter.setPen(COL_TEXT_SELECTED if selected else COL_TEXT_DEFAULT)
        font = painter.font()
        font.setPointSize(9)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(rect, Qt.AlignCenter, self._label)

        # design-mode indicator
        if self._mode == PickerMode.DESIGN:
            painter.setPen(QtGui.QPen(QtGui.QColor(120, 200, 255, 60), 0.5,
                                       Qt.DashLine))
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(rect)

    # -- hover events -------------------------------------------------------
    def hoverEnterEvent(self, event) -> None:
        self._hover = True
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event) -> None:
        self._hover = False
        self.update()
        super().hoverLeaveEvent(event)

    # -- selection change (show/hide resize handle) -------------------------
    def itemChange(self, change, value):
        if change == QtWidgets.QGraphicsItem.ItemSelectedHasChanged:
            self._handle.setVisible(
                bool(value) and self._mode == PickerMode.DESIGN)
            self._handle.reposition()
        if (change == QtWidgets.QGraphicsItem.ItemPositionChange
                and self._mode == PickerMode.DESIGN and self._snap_enabled):
            return snap_point(value)
        return super().itemChange(change, value)

    # -- mouse events -------------------------------------------------------
    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            if self._mode == PickerMode.ANIMATION:
                add = bool(event.modifiers() & (Qt.ShiftModifier | Qt.ControlModifier))
                self._select_maya_nodes(add=add)
                # update scene selection
                if add:
                    self.setSelected(not self.isSelected())
                else:
                    if self.scene():
                        for it in self.scene().selectedItems():
                            if it is not self:
                                it.setSelected(False)
                    self.setSelected(True)
                event.accept()
                return
            # Design mode — default drag behaviour via ItemIsMovable flag
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        """Double-click in Design mode opens a rename dialog."""
        if self._mode == PickerMode.DESIGN and event.button() == Qt.LeftButton:
            new_label, ok = QtWidgets.QInputDialog.getText(
                None, "Rename Item", "Label:", text=self._label)
            if ok and new_label:
                self.label = new_label
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event) -> None:
        """Right-click context menu (Design mode)."""
        if self._mode != PickerMode.DESIGN:
            return

        menu = QtWidgets.QMenu()
        act_rename = menu.addAction("Rename")
        act_nodes  = menu.addAction("Edit Maya Nodes...")
        menu.addSeparator()
        act_rot_cw  = menu.addAction("Rotate +15\u00b0")
        act_rot_ccw = menu.addAction("Rotate \u221215\u00b0")
        act_rot_0   = menu.addAction("Reset Rotation")
        menu.addSeparator()
        act_del = menu.addAction("Delete")

        chosen = menu.exec_(event.screenPos())
        if chosen == act_rename:
            new_label, ok = QtWidgets.QInputDialog.getText(
                None, "Rename", "Label:", text=self._label)
            if ok and new_label:
                self.label = new_label
        elif chosen == act_nodes:
            txt, ok = QtWidgets.QInputDialog.getText(
                None, "Maya Nodes",
                "Comma-separated node names:",
                text=", ".join(self._maya_nodes))
            if ok:
                self._maya_nodes = [n.strip() for n in txt.split(",") if n.strip()]
        elif chosen == act_rot_cw:
            self.setRotation(self.rotation() + 15)
        elif chosen == act_rot_ccw:
            self.setRotation(self.rotation() - 15)
        elif chosen == act_rot_0:
            self.setRotation(0)
        elif chosen == act_del:
            self.scene().removeItem(self)

    # -- Maya selection helper ----------------------------------------------
    def _select_maya_nodes(self, add: bool = False) -> None:
        if not self._maya_nodes:
            return
        try:
            existing = [n for n in self._maya_nodes if cmds.objExists(n)]
            missing  = [n for n in self._maya_nodes if n not in existing]
            if missing:
                cmds.warning("DD Picker: not found — " + ", ".join(missing))
            if existing:
                cmds.select(existing, add=add, replace=not add)
        except Exception:
            traceback.print_exc()

    # -- serialisation helpers (for future save/load) -----------------------
    def to_dict(self) -> dict:
        return dict(
            label=self._label,
            maya_nodes=self._maya_nodes,
            x=self.pos().x(), y=self.pos().y(),
            w=self._width, h=self._height,
            rotation=self.rotation(),
        )

    @classmethod
    def from_dict(cls, data: dict) -> "PickerItem":
        item = cls(
            label=data.get("label", ""),
            maya_nodes=data.get("maya_nodes", []),
            width=data.get("w", 60),
            height=data.get("h", 30),
        )
        item.setPos(data.get("x", 0), data.get("y", 0))
        item.setRotation(data.get("rotation", 0))
        return item


# ╔═════════════════════════════════════════════════════════════════════════╗
# ║  BACKGROUND IMAGE ITEM                                                 ║
# ╚═════════════════════════════════════════════════════════════════════════╝

class BackgroundImageItem(QtWidgets.QGraphicsPixmapItem):
    """Holds a reference image behind the picker buttons.

    Supports runtime opacity and scale adjustments.
    Always rendered behind every other item (lowest Z).
    """

    def __init__(self, pixmap: QtGui.QPixmap,
                 parent: Optional[QtWidgets.QGraphicsItem] = None):
        super().__init__(pixmap, parent)
        self.setZValue(-1000)
        self.setFlag(QtWidgets.QGraphicsItem.ItemIsSelectable, False)
        self.setFlag(QtWidgets.QGraphicsItem.ItemIsMovable, False)
        self.setShapeMode(QtWidgets.QGraphicsPixmapItem.BoundingRectShape)
        self._base_pixmap = pixmap
        self._opacity_value: float = 0.5
        self.setOpacity(self._opacity_value)
        # Centre the image on the origin
        self.setOffset(-pixmap.width() / 2.0, -pixmap.height() / 2.0)

    def set_opacity_value(self, value: float) -> None:
        self._opacity_value = max(0.0, min(1.0, value))
        self.setOpacity(self._opacity_value)

    def set_image_scale(self, factor: float) -> None:
        factor = max(0.05, min(10.0, factor))
        self.setScale(factor)

    def set_movable(self, movable: bool) -> None:
        self.setFlag(QtWidgets.QGraphicsItem.ItemIsMovable, movable)


# ╔═════════════════════════════════════════════════════════════════════════╗
# ║  PICKER SCENE                                                          ║
# ╚═════════════════════════════════════════════════════════════════════════╝

class PickerScene(QtWidgets.QGraphicsScene):
    """Scene that holds PickerItems and optional background images."""

    mode_changed = Signal(PickerMode)

    def __init__(self, parent: Optional[QtCore.QObject] = None):
        super().__init__(parent)
        self.setSceneRect(-LOGICAL_WIDTH / 2, -LOGICAL_HEIGHT / 2,
                          LOGICAL_WIDTH, LOGICAL_HEIGHT)
        self._mode: PickerMode = PickerMode.ANIMATION
        self._bg_item: Optional[BackgroundImageItem] = None

    # -- mode ---------------------------------------------------------------
    @property
    def mode(self) -> PickerMode:
        return self._mode

    def set_mode(self, mode: PickerMode) -> None:
        self._mode = mode
        for item in self.items():
            if isinstance(item, PickerItem):
                item.set_mode(mode)
        if self._bg_item:
            self._bg_item.set_movable(mode == PickerMode.DESIGN)
        self.mode_changed.emit(mode)
        self.update()

    # -- background image ---------------------------------------------------
    def set_background_image(self, path: str) -> Optional[BackgroundImageItem]:
        """Load an image file as the canvas background."""
        pixmap = QtGui.QPixmap(path)
        if pixmap.isNull():
            cmds.warning("DD Picker: failed to load image — " + path)
            return None
        self.remove_background_image()
        self._bg_item = BackgroundImageItem(pixmap)
        self._bg_item.set_movable(self._mode == PickerMode.DESIGN)
        self.addItem(self._bg_item)
        return self._bg_item

    def remove_background_image(self) -> None:
        if self._bg_item is not None:
            self.removeItem(self._bg_item)
            self._bg_item = None

    @property
    def background_image(self) -> Optional[BackgroundImageItem]:
        return self._bg_item

    # -- picker items -------------------------------------------------------
    def picker_items(self) -> List[PickerItem]:
        return [i for i in self.items() if isinstance(i, PickerItem)]

    def add_picker_item(self, label: str, maya_nodes: List[str],
                        x: float = 0, y: float = 0,
                        w: float = 60, h: float = 30) -> PickerItem:
        item = PickerItem(label=label, maya_nodes=maya_nodes,
                          width=w, height=h)
        item.setPos(x, y)
        item.set_mode(self._mode)
        self.addItem(item)
        return item


# ╔═════════════════════════════════════════════════════════════════════════╗
# ║  PICKER GRAPHICS VIEW                                                  ║
# ╚═════════════════════════════════════════════════════════════════════════╝

class PickerGraphicsView(QtWidgets.QGraphicsView):
    """Custom view with zoom, pan, grid drawing, and rubber-band selection."""

    zoom_changed = Signal(float)

    def __init__(self, scene: PickerScene,
                 parent: Optional[QtWidgets.QWidget] = None):
        super().__init__(scene, parent)

        self.setRenderHints(
            QtGui.QPainter.Antialiasing
            | QtGui.QPainter.SmoothPixmapTransform
        )
        self.setViewportUpdateMode(
            QtWidgets.QGraphicsView.FullViewportUpdate)
        self.setTransformationAnchor(
            QtWidgets.QGraphicsView.NoAnchor)
        self.setResizeAnchor(
            QtWidgets.QGraphicsView.NoAnchor)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setBackgroundBrush(COL_CANVAS_BG)
        self.setSceneRect(scene.sceneRect())

        self._zoom: float = 1.0
        self._panning: bool = False
        self._pan_start = QtCore.QPoint()

        # rubber-band
        self._rb: Optional[QtWidgets.QRubberBand] = None
        self._rb_active: bool = False
        self._rb_origin = QtCore.QPoint()

    # -- zoom ---------------------------------------------------------------
    def current_zoom(self) -> float:
        return self._zoom

    def _apply_zoom(self, factor: float, centre: QtCore.QPoint) -> None:
        new_zoom = self._zoom * factor
        if not (ZOOM_MIN <= new_zoom <= ZOOM_MAX):
            return
        old_scene = self.mapToScene(centre)
        self.scale(factor, factor)
        self._zoom = new_zoom
        new_scene = self.mapToScene(centre)
        delta = new_scene - old_scene
        self.translate(delta.x(), delta.y())
        self.zoom_changed.emit(self._zoom)

    def reset_view(self) -> None:
        self.resetTransform()
        self._zoom = 1.0
        self.centerOn(0, 0)
        self.zoom_changed.emit(self._zoom)

    def fit_all(self) -> None:
        scene = self.scene()
        if not scene:
            return
        bounds = scene.itemsBoundingRect().adjusted(-40, -40, 40, 40)
        if bounds.isEmpty():
            return
        self.fitInView(bounds, Qt.KeepAspectRatio)
        self._zoom = self.transform().m11()
        self.zoom_changed.emit(self._zoom)

    # -- rubber-band helpers ------------------------------------------------
    def _rb_start(self, pos: QtCore.QPoint) -> None:
        self._rb_active = True
        self._rb_origin = pos
        if self._rb is None:
            self._rb = QtWidgets.QRubberBand(
                QtWidgets.QRubberBand.Rectangle, self.viewport())
        self._rb.setGeometry(QtCore.QRect(pos, QtCore.QSize()))
        self._rb.show()

    def _rb_update(self, pos: QtCore.QPoint) -> None:
        self._rb.setGeometry(
            QtCore.QRect(self._rb_origin, pos).normalized())

    def _rb_finish(self, mods) -> None:
        self._rb.hide()
        self._rb_active = False

        rect = self._rb.geometry()
        scene_rect = QtCore.QRectF(self.mapToScene(rect.topLeft()),
                                   self.mapToScene(rect.bottomRight()))

        add = bool(mods & (Qt.ShiftModifier | Qt.ControlModifier))
        if not add:
            self.scene().clearSelection()

        for item in self.scene().items(scene_rect, Qt.IntersectsItemShape):
            if isinstance(item, PickerItem):
                item.setSelected(True)

        _sync_maya_selection(self.scene())

    # -- event overrides ----------------------------------------------------
    def wheelEvent(self, event) -> None:
        angle = event.angleDelta().y()
        factor = ZOOM_FACTOR if angle > 0 else 1.0 / ZOOM_FACTOR
        pos = (event.position().toPoint() if PYSIDE_VERSION == 6
               else event.pos())
        self._apply_zoom(factor, pos)

    def mousePressEvent(self, event) -> None:
        btn, mods = event.button(), event.modifiers()

        # Pan — MMB or Alt+LMB
        if btn == Qt.MiddleButton or (btn == Qt.LeftButton and mods & Qt.AltModifier):
            self._panning = True
            self._pan_start = event.pos()
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()
            return

        # LMB on empty space → rubber-band (Animation mode only)
        if btn == Qt.LeftButton:
            picker_scene = self.scene()
            if (isinstance(picker_scene, PickerScene)
                    and picker_scene.mode == PickerMode.ANIMATION
                    and self.itemAt(event.pos()) is None):
                self._rb_start(event.pos())
                if not (mods & (Qt.ShiftModifier | Qt.ControlModifier)):
                    picker_scene.clearSelection()
                    cmds.select(clear=True)
                event.accept()
                return

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._panning:
            delta = event.pos() - self._pan_start
            self._pan_start = event.pos()
            self.translate(delta.x() / self._zoom,
                           delta.y() / self._zoom)
            event.accept()
            return
        if self._rb_active:
            self._rb_update(event.pos())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._panning and event.button() in (Qt.MiddleButton, Qt.LeftButton):
            self._panning = False
            self.setCursor(Qt.ArrowCursor)
            event.accept()
            return
        if self._rb_active and event.button() == Qt.LeftButton:
            self._rb_finish(event.modifiers())
            event.accept()
            return
        super().mouseReleaseEvent(event)

    # -- background grid ----------------------------------------------------
    def drawBackground(self, painter: QtGui.QPainter,
                       rect: QtCore.QRectF) -> None:
        super().drawBackground(painter, rect)

        scene = self.scene()
        if not isinstance(scene, PickerScene):
            return

        # Only draw grid when in Design mode
        if scene.mode != PickerMode.DESIGN:
            return

        gs = GRID_SIZE
        major = gs * 5

        left  = int(math.floor(rect.left() / gs)) * gs
        top   = int(math.floor(rect.top() / gs)) * gs
        right = rect.right()
        bottom = rect.bottom()

        minor_lines: list = []
        major_lines: list = []
        origin_lines: list = []

        x = left
        while x <= right:
            line = QtCore.QLineF(x, rect.top(), x, rect.bottom())
            if x == 0:
                origin_lines.append(line)
            elif x % major == 0:
                major_lines.append(line)
            else:
                minor_lines.append(line)
            x += gs

        y = top
        while y <= bottom:
            line = QtCore.QLineF(rect.left(), y, rect.right(), y)
            if y == 0:
                origin_lines.append(line)
            elif y % major == 0:
                major_lines.append(line)
            else:
                minor_lines.append(line)
            y += gs

        if minor_lines:
            painter.setPen(QtGui.QPen(COL_GRID_MINOR, 0.5))
            painter.drawLines(minor_lines)
        if major_lines:
            painter.setPen(QtGui.QPen(COL_GRID_MAJOR, 1.0))
            painter.drawLines(major_lines)
        if origin_lines:
            painter.setPen(QtGui.QPen(COL_GRID_ORIGIN, 1.5))
            painter.drawLines(origin_lines)


# ╔═════════════════════════════════════════════════════════════════════════╗
# ║  MAYA SELECTION SYNC                                                    ║
# ╚═════════════════════════════════════════════════════════════════════════╝

def _sync_maya_selection(scene: QtWidgets.QGraphicsScene) -> None:
    """Push the current scene selection to Maya."""
    nodes: List[str] = []
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


# ╔═════════════════════════════════════════════════════════════════════════╗
# ║  MAIN WINDOW                                                           ║
# ╚═════════════════════════════════════════════════════════════════════════╝

class DDPickerWindow(MayaQWidgetDockableMixin, QtWidgets.QMainWindow):
    """Top-level dockable window."""

    _instance: Optional["DDPickerWindow"] = None

    def __init__(self, parent: Optional[QtWidgets.QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle(WINDOW_TITLE)
        self.setMinimumSize(480, 360)

        self._scene = PickerScene(self)
        self._view = PickerGraphicsView(self._scene, self)

        self._build_ui()
        self._build_menubar()
        self._connect_signals()
        self._add_demo_items()

    # -- UI construction ----------------------------------------------------
    def _build_ui(self) -> None:
        central = QtWidgets.QWidget(self)
        self.setCentralWidget(central)
        root = QtWidgets.QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ---- toolbar ------------------------------------------------------
        tb = QtWidgets.QHBoxLayout()
        tb.setContentsMargins(6, 4, 6, 4)

        # mode toggle
        self._btn_mode = QtWidgets.QPushButton("Animation Mode")
        self._btn_mode.setCheckable(True)
        self._btn_mode.setFixedHeight(24)
        self._btn_mode.setToolTip("Toggle Design / Animation mode")
        self._btn_mode.clicked.connect(self._on_toggle_mode)
        tb.addWidget(self._btn_mode)

        tb.addSpacing(8)

        # snap toggle
        self._chk_snap = QtWidgets.QCheckBox("Snap")
        self._chk_snap.setChecked(True)
        self._chk_snap.setToolTip("Snap to grid (Design mode)")
        self._chk_snap.toggled.connect(self._on_snap_toggled)
        tb.addWidget(self._chk_snap)

        tb.addSpacing(8)

        # zoom label
        self._lbl_zoom = QtWidgets.QLabel("100 %")
        self._lbl_zoom.setFixedWidth(52)
        tb.addWidget(self._lbl_zoom)

        tb.addStretch()

        # reset view
        btn_reset = QtWidgets.QPushButton("Reset")
        btn_reset.setFixedHeight(24)
        btn_reset.clicked.connect(self._on_reset_view)
        tb.addWidget(btn_reset)

        # fit
        btn_fit = QtWidgets.QPushButton("Fit")
        btn_fit.setFixedHeight(24)
        btn_fit.clicked.connect(self._view.fit_all)
        tb.addWidget(btn_fit)

        root.addLayout(tb)

        # ---- background controls (collapsible row) ------------------------
        bg_row = QtWidgets.QHBoxLayout()
        bg_row.setContentsMargins(6, 0, 6, 4)

        btn_load_bg = QtWidgets.QPushButton("Load BG")
        btn_load_bg.setFixedHeight(22)
        btn_load_bg.clicked.connect(self._on_load_bg)
        bg_row.addWidget(btn_load_bg)

        btn_remove_bg = QtWidgets.QPushButton("Remove BG")
        btn_remove_bg.setFixedHeight(22)
        btn_remove_bg.clicked.connect(self._on_remove_bg)
        bg_row.addWidget(btn_remove_bg)

        bg_row.addSpacing(8)
        bg_row.addWidget(QtWidgets.QLabel("Opacity:"))
        self._sld_opacity = QtWidgets.QSlider(Qt.Horizontal)
        self._sld_opacity.setRange(0, 100)
        self._sld_opacity.setValue(50)
        self._sld_opacity.setFixedWidth(100)
        self._sld_opacity.valueChanged.connect(self._on_bg_opacity)
        bg_row.addWidget(self._sld_opacity)

        bg_row.addSpacing(8)
        bg_row.addWidget(QtWidgets.QLabel("Scale:"))
        self._spn_bg_scale = QtWidgets.QDoubleSpinBox()
        self._spn_bg_scale.setRange(0.05, 10.0)
        self._spn_bg_scale.setSingleStep(0.05)
        self._spn_bg_scale.setValue(1.0)
        self._spn_bg_scale.setFixedWidth(70)
        self._spn_bg_scale.valueChanged.connect(self._on_bg_scale)
        bg_row.addWidget(self._spn_bg_scale)

        bg_row.addStretch()
        root.addLayout(bg_row)

        # ---- canvas -------------------------------------------------------
        root.addWidget(self._view)

    def _build_menubar(self) -> None:
        mb = self.menuBar()

        # -- File --
        m_file = mb.addMenu("File")
        m_file.addAction("Load Background Image...", self._on_load_bg)
        m_file.addAction("Remove Background Image",  self._on_remove_bg)

        # -- View --
        m_view = mb.addMenu("View")
        m_view.addAction("Reset View",  self._on_reset_view)
        m_view.addAction("Fit All",     self._view.fit_all)

        # -- Item --
        m_item = mb.addMenu("Item")
        m_item.addAction("Add Item...", self._on_add_item)

    def _connect_signals(self) -> None:
        self._view.zoom_changed.connect(self._on_zoom_changed)

    # -- demo ---------------------------------------------------------------
    def _add_demo_items(self) -> None:
        demos = [
            ("Head",   ["head_ctrl"],      0, -140, 60, 28),
            ("Neck",   ["neck_ctrl"],      0, -100, 50, 22),
            ("Body",   ["body_ctrl"],      0,    0, 70, 30),
            ("L_Clav", ["L_clavicle_ctrl"], -80, -60, 60, 22),
            ("R_Clav", ["R_clavicle_ctrl"],  80, -60, 60, 22),
            ("L_Arm",  ["L_arm_ctrl"],   -120,   0, 60, 28),
            ("R_Arm",  ["R_arm_ctrl"],    120,   0, 60, 28),
            ("L_Hand", ["L_hand_ctrl"],  -120,  60, 60, 28),
            ("R_Hand", ["R_hand_ctrl"],   120,  60, 60, 28),
            ("Hip",    ["hip_ctrl"],        0,  60, 60, 26),
            ("L_Leg",  ["L_leg_ctrl"],    -50, 120, 60, 28),
            ("R_Leg",  ["R_leg_ctrl"],     50, 120, 60, 28),
            ("L_Foot", ["L_foot_ctrl"],   -50, 180, 60, 28),
            ("R_Foot", ["R_foot_ctrl"],    50, 180, 60, 28),
        ]
        for label, nodes, x, y, w, h in demos:
            self._scene.add_picker_item(label, nodes, x, y, w, h)

    # -- slots: mode --------------------------------------------------------
    def _on_toggle_mode(self, checked: bool) -> None:
        if checked:
            self._scene.set_mode(PickerMode.DESIGN)
            self._btn_mode.setText("Design Mode")
        else:
            self._scene.set_mode(PickerMode.ANIMATION)
            self._btn_mode.setText("Animation Mode")

    def _on_snap_toggled(self, on: bool) -> None:
        for item in self._scene.picker_items():
            item._snap_enabled = on

    # -- slots: view --------------------------------------------------------
    def _on_reset_view(self) -> None:
        self._view.reset_view()

    def _on_zoom_changed(self, value: float) -> None:
        self._lbl_zoom.setText(f"{int(value * 100)} %")

    # -- slots: background --------------------------------------------------
    def _on_load_bg(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Select Background Image", "",
            "Images (*.png *.jpg *.jpeg *.bmp *.tga);;All Files (*)")
        if path:
            bg = self._scene.set_background_image(path)
            if bg:
                self._sld_opacity.setValue(50)
                self._spn_bg_scale.setValue(1.0)

    def _on_remove_bg(self) -> None:
        self._scene.remove_background_image()

    def _on_bg_opacity(self, value: int) -> None:
        bg = self._scene.background_image
        if bg:
            bg.set_opacity_value(value / 100.0)

    def _on_bg_scale(self, value: float) -> None:
        bg = self._scene.background_image
        if bg:
            bg.set_image_scale(value)

    # -- slots: items -------------------------------------------------------
    def _on_add_item(self) -> None:
        """Prompt to add a new PickerItem at the centre of the view."""
        label, ok = QtWidgets.QInputDialog.getText(
            self, "Add Picker Item", "Label:")
        if not ok or not label:
            return
        nodes_str, ok = QtWidgets.QInputDialog.getText(
            self, "Maya Nodes",
            "Comma-separated Maya node names:")
        if not ok:
            return
        nodes = [n.strip() for n in nodes_str.split(",") if n.strip()]
        centre = self._view.mapToScene(
            self._view.viewport().rect().center())
        self._scene.add_picker_item(label, nodes, centre.x(), centre.y())

    # -- lifecycle ----------------------------------------------------------
    def dockCloseEventTriggered(self) -> None:
        DDPickerWindow._instance = None
        super().dockCloseEventTriggered()


# ╔═════════════════════════════════════════════════════════════════════════╗
# ║  PUBLIC API                                                             ║
# ╚═════════════════════════════════════════════════════════════════════════╝

def show() -> DDPickerWindow:
    """Open (or re-open) the DD Picker window."""
    _close_existing()
    win = DDPickerWindow()
    win.show(dockable=True, area="right", allowedArea="all",
             floating=True, width=650, height=550)
    DDPickerWindow._instance = win
    return win


def _close_existing() -> None:
    if DDPickerWindow._instance is not None:
        try:
            DDPickerWindow._instance.close()
        except Exception:
            pass
        DDPickerWindow._instance = None
    if cmds.workspaceControl(WORKSPACE_CONTROL_NAME, exists=True):
        cmds.deleteUI(WORKSPACE_CONTROL_NAME)


def add_picker_item(label: str, maya_nodes: List[str],
                    x: float = 0, y: float = 0,
                    w: float = 60, h: float = 30) -> Optional[PickerItem]:
    """Convenience wrapper — add an item to the active picker scene.

    Example::

        dd_picker.show()
        dd_picker.add_picker_item("L_FK_Arm", ["L_arm_FK_ctrl"], -150, 20)
    """
    win = DDPickerWindow._instance
    if win is None:
        cmds.warning("DD Picker: call dd_picker.show() first.")
        return None
    return win._scene.add_picker_item(label, maya_nodes, x, y, w, h)
