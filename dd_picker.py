# -*- coding: utf-8 -*-
"""
DD Picker - A 2D Picker Control Panel for Maya
===============================================

A professional character-picker tool with Design / Animation dual-mode
workflow, background image support, snap-to-grid, resolution-independent
logical coordinates, and full undo/redo support.

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
from typing import Optional, List, Dict, Any

# ---------------------------------------------------------------------------
# Qt compatibility shim
# ---------------------------------------------------------------------------
try:
    from PySide6 import QtCore, QtGui, QtWidgets
    from PySide6.QtCore import Qt, Signal, QTimer, QPropertyAnimation, \
        QEasingCurve, Property
    PYSIDE_VERSION = 6
except ImportError:
    from PySide2 import QtCore, QtGui, QtWidgets
    from PySide2.QtCore import Qt, Signal, QTimer, QPropertyAnimation, \
        QEasingCurve, Property
    PYSIDE_VERSION = 2

import maya.cmds as cmds
import maya.mel as mel
import maya.api.OpenMaya as om2
from maya.app.general.mayaMixin import MayaQWidgetDockableMixin


# ╔═════════════════════════════════════════════════════════════════════════╗
# ║  CONSTANTS & ENUMS                                                     ║
# ╚═════════════════════════════════════════════════════════════════════════╝

WINDOW_TITLE = "DD Picker"
WORKSPACE_CONTROL_NAME = "DDPickerWorkspaceControl"

LOGICAL_WIDTH = 1000.0
LOGICAL_HEIGHT = 1000.0

GRID_SIZE = 20
SNAP_THRESHOLD = GRID_SIZE / 2.0

COL_CANVAS_BG       = QtGui.QColor(42, 42, 42)
COL_GRID_MINOR      = QtGui.QColor(52, 52, 52)
COL_GRID_MAJOR      = QtGui.QColor(62, 62, 62)
COL_GRID_ORIGIN     = QtGui.QColor(80, 80, 80)

COL_ITEM_DEFAULT    = QtGui.QColor(68, 68, 68)
COL_ITEM_HOVER      = QtGui.QColor(88, 88, 88)
COL_ITEM_SELECTED   = QtGui.QColor(210, 180, 50)
COL_BORDER_DEFAULT  = QtGui.QColor(90, 90, 90)
COL_BORDER_HOVER    = QtGui.QColor(180, 180, 180)
COL_BORDER_SELECTED = QtGui.QColor(240, 210, 60)
COL_GLOW            = QtGui.QColor(140, 210, 255, 160)
COL_TEXT_DEFAULT    = QtGui.QColor(200, 200, 200)
COL_TEXT_SELECTED   = QtGui.QColor(30, 30, 30)

ZOOM_MIN = 0.1
ZOOM_MAX = 10.0
ZOOM_FACTOR = 1.15
CORNER_RADIUS = 6


class PickerMode(Enum):
    DESIGN    = auto()
    ANIMATION = auto()


class ItemShape(Enum):
    RECT     = "rect"
    ELLIPSE  = "ellipse"
    POLYGON  = "polygon"


# ╔═════════════════════════════════════════════════════════════════════════╗
# ║  HELPERS                                                                ║
# ╚═════════════════════════════════════════════════════════════════════════╝

def snap_value(value: float, grid: float = GRID_SIZE) -> float:
    return round(value / grid) * grid


def snap_point(point: QtCore.QPointF, grid: float = GRID_SIZE) -> QtCore.QPointF:
    return QtCore.QPointF(snap_value(point.x(), grid),
                          snap_value(point.y(), grid))


def resolve_namespace(node_name: str, namespace: str) -> str:
    if not namespace:
        return node_name
    base = node_name.rsplit(":", 1)[-1]
    return "{}:{}".format(namespace, base)


# ╔═════════════════════════════════════════════════════════════════════════╗
# ║  UNDO COMMANDS                                                          ║
# ╚═════════════════════════════════════════════════════════════════════════╝

class MoveItemCommand(QtWidgets.QUndoCommand):
    """Undo/redo moving one or more items."""

    def __init__(self, items_and_old_pos: List[tuple], description: str = "Move"):
        super().__init__(description)
        # [(item, old_pos_QPointF, new_pos_QPointF), ...]
        self._data = [(item, QtCore.QPointF(old), QtCore.QPointF(item.pos()))
                      for item, old in items_and_old_pos]

    def redo(self) -> None:
        for item, _old, new in self._data:
            item.setPos(new)

    def undo(self) -> None:
        for item, old, _new in self._data:
            item.setPos(old)


class ResizeItemCommand(QtWidgets.QUndoCommand):
    """Undo/redo resizing an item."""

    def __init__(self, item: "PickerItem",
                 old_w: float, old_h: float,
                 new_w: float, new_h: float):
        super().__init__("Resize '{}'".format(item.label))
        self._item = item
        self._old_w, self._old_h = old_w, old_h
        self._new_w, self._new_h = new_w, new_h

    def redo(self) -> None:
        self._item.resize(self._new_w, self._new_h)

    def undo(self) -> None:
        self._item.resize(self._old_w, self._old_h)


class RotateItemCommand(QtWidgets.QUndoCommand):
    """Undo/redo rotating an item."""

    def __init__(self, item: "PickerItem",
                 old_angle: float, new_angle: float):
        super().__init__("Rotate '{}'".format(item.label))
        self._item = item
        self._old = old_angle
        self._new = new_angle

    def redo(self) -> None:
        self._item.setRotation(self._new)

    def undo(self) -> None:
        self._item.setRotation(self._old)


class PropertyChangeCommand(QtWidgets.QUndoCommand):
    """Generic undo/redo for any single property change on a PickerItem."""

    def __init__(self, item: "PickerItem", attr: str,
                 old_value: Any, new_value: Any, description: str = ""):
        desc = description or "Change {} on '{}'".format(attr, item.label)
        super().__init__(desc)
        self._item = item
        self._attr = attr
        self._old = old_value
        self._new = new_value

    def redo(self) -> None:
        setattr(self._item, self._attr, self._new)
        self._item.update()

    def undo(self) -> None:
        setattr(self._item, self._attr, self._old)
        self._item.update()


class ChangeShapeCommand(QtWidgets.QUndoCommand):
    """Undo/redo changing shape type (and polygon points)."""

    def __init__(self, item: "PickerItem",
                 old_shape: ItemShape, old_poly: List[List[float]],
                 new_shape: ItemShape, new_poly: List[List[float]]):
        super().__init__("Change shape on '{}'".format(item.label))
        self._item = item
        self._old_shape, self._old_poly = old_shape, list(old_poly)
        self._new_shape, self._new_poly = new_shape, list(new_poly)

    def redo(self) -> None:
        self._item.prepareGeometryChange()
        self._item._shape_type = self._new_shape
        self._item._polygon_points = list(self._new_poly)
        self._item.update()

    def undo(self) -> None:
        self._item.prepareGeometryChange()
        self._item._shape_type = self._old_shape
        self._item._polygon_points = list(self._old_poly)
        self._item.update()


class AddItemCommand(QtWidgets.QUndoCommand):
    """Undo/redo adding a PickerItem to the scene."""

    def __init__(self, scene: "PickerScene", item: "PickerItem"):
        super().__init__("Add '{}'".format(item.label))
        self._scene = scene
        self._item = item

    def redo(self) -> None:
        self._item.set_mode(self._scene.mode)
        self._scene.addItem(self._item)

    def undo(self) -> None:
        self._scene.removeItem(self._item)


class DeleteItemCommand(QtWidgets.QUndoCommand):
    """Undo/redo deleting a PickerItem from the scene."""

    def __init__(self, scene: "PickerScene", item: "PickerItem"):
        super().__init__("Delete '{}'".format(item.label))
        self._scene = scene
        self._item = item

    def redo(self) -> None:
        self._scene.removeItem(self._item)

    def undo(self) -> None:
        self._item.set_mode(self._scene.mode)
        self._scene.addItem(self._item)


# ╔═════════════════════════════════════════════════════════════════════════╗
# ║  TOAST NOTIFICATION                                                     ║
# ╚═════════════════════════════════════════════════════════════════════════╝

class _ToastLabel(QtWidgets.QLabel):
    def __init__(self, parent: QtWidgets.QWidget):
        super().__init__(parent)
        self.setAlignment(Qt.AlignCenter)
        self.setStyleSheet(
            "QLabel {"
            "  background: rgba(200, 60, 60, 200);"
            "  color: #fff;"
            "  border-radius: 6px;"
            "  padding: 6px 14px;"
            "  font-weight: bold;"
            "  font-size: 11px;"
            "}"
        )
        self.setVisible(False)
        self._effect = QtWidgets.QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._effect)
        self._effect.setOpacity(0.0)

        self._anim = QPropertyAnimation(self._effect, b"opacity", self)
        self._anim.setEasingCurve(QEasingCurve.InOutQuad)

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._fade_out)

    def show_message(self, text: str, duration_ms: int = 2000) -> None:
        self.setText(text)
        self.adjustSize()
        pw = self.parent().width() if self.parent() else 300
        ph = self.parent().height() if self.parent() else 300
        self.move((pw - self.width()) // 2, ph - self.height() - 20)
        self.setVisible(True)
        self._anim.stop()
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.setDuration(200)
        self._anim.start()
        self._hide_timer.start(duration_ms)

    def _fade_out(self) -> None:
        self._anim.stop()
        self._anim.setStartValue(1.0)
        self._anim.setEndValue(0.0)
        self._anim.setDuration(400)
        self._anim.finished.connect(self._on_faded)
        self._anim.start()

    def _on_faded(self) -> None:
        self.setVisible(False)
        try:
            self._anim.finished.disconnect(self._on_faded)
        except RuntimeError:
            pass


# ╔═════════════════════════════════════════════════════════════════════════╗
# ║  RESIZE HANDLE                                                          ║
# ╚═════════════════════════════════════════════════════════════════════════╝

class _ResizeHandle(QtWidgets.QGraphicsRectItem):
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
        self._orig_w: float = 0
        self._orig_h: float = 0

    def reposition(self) -> None:
        r = self._parent_item.boundingRect()
        self.setPos(r.right(), r.bottom())

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._drag_origin = event.scenePos()
            self._orig_w = self._parent_item._width
            self._orig_h = self._parent_item._height
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        delta = event.scenePos() - self._drag_origin
        new_w = max(20, self._orig_w + delta.x())
        new_h = max(14, self._orig_h + delta.y())
        if self._parent_item._snap_enabled:
            new_w = snap_value(new_w)
            new_h = snap_value(new_h)
        self._parent_item.resize(new_w, new_h)
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            new_w = self._parent_item._width
            new_h = self._parent_item._height
            if (new_w != self._orig_w or new_h != self._orig_h):
                stack = self._parent_item._undo_stack()
                if stack:
                    stack.push(ResizeItemCommand(
                        self._parent_item,
                        self._orig_w, self._orig_h,
                        new_w, new_h))
        event.accept()


# ╔═════════════════════════════════════════════════════════════════════════╗
# ║  PICKER ITEM                                                           ║
# ╚═════════════════════════════════════════════════════════════════════════╝

class PickerItem(QtWidgets.QGraphicsObject):
    """Core interactive button on the picker canvas.

    All Design-mode mutations are routed through the scene's QUndoStack
    so that every change can be undone with Ctrl+Z.
    """

    # --- Qt property for glow animation ------------------------------------
    def _get_glow(self) -> float:
        return self._glow_value

    def _set_glow(self, v: float) -> None:
        self._glow_value = v
        self.update()

    glow_strength = Property(float, _get_glow, _set_glow)

    def __init__(
        self,
        label: str = "",
        maya_nodes: Optional[List[str]] = None,
        width: float = 60.0,
        height: float = 30.0,
        shape: ItemShape = ItemShape.RECT,
        polygon_points: Optional[List[List[float]]] = None,
        command_on_click: str = "",
        namespace: str = "",
        parent: Optional[QtWidgets.QGraphicsItem] = None,
    ):
        super().__init__(parent)
        self._label: str = label
        self._maya_nodes: List[str] = list(maya_nodes) if maya_nodes else []
        self._width: float = width
        self._height: float = height
        self._shape_type: ItemShape = shape
        self._polygon_points: List[List[float]] = list(polygon_points or [])
        self._command_on_click: str = command_on_click
        self._namespace: str = namespace

        self._hover: bool = False
        self._mode: PickerMode = PickerMode.ANIMATION
        self._snap_enabled: bool = True
        self._glow_value: float = 0.0

        # Drag tracking for undo
        self._drag_start_pos: Optional[QtCore.QPointF] = None

        self.setAcceptHoverEvents(True)
        self.setFlag(QtWidgets.QGraphicsItem.ItemIsSelectable, True)
        self.setFlag(QtWidgets.QGraphicsItem.ItemSendsGeometryChanges, True)

        self._handle = _ResizeHandle(self)

        self._glow_anim = QPropertyAnimation(self, b"glow_strength", self)
        self._glow_anim.setDuration(200)
        self._glow_anim.setEasingCurve(QEasingCurve.InOutQuad)

    # ── undo stack accessor ────────────────────────────────────────────────
    def _undo_stack(self) -> Optional[QtWidgets.QUndoStack]:
        scene = self.scene()
        if scene and hasattr(scene, "undo_stack"):
            return scene.undo_stack
        return None

    # ── properties ─────────────────────────────────────────────────────────
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

    @property
    def command_on_click(self) -> str:
        return self._command_on_click

    @command_on_click.setter
    def command_on_click(self, value: str) -> None:
        self._command_on_click = value

    @property
    def namespace(self) -> str:
        return self._namespace

    @namespace.setter
    def namespace(self, value: str) -> None:
        self._namespace = value

    @property
    def shape_type(self) -> ItemShape:
        return self._shape_type

    def resolved_nodes(self) -> List[str]:
        return [resolve_namespace(n, self._namespace)
                for n in self._maya_nodes]

    # ── mode switching ─────────────────────────────────────────────────────
    def set_mode(self, mode: PickerMode) -> None:
        self._mode = mode
        is_design = (mode == PickerMode.DESIGN)
        self.setFlag(QtWidgets.QGraphicsItem.ItemIsMovable, is_design)
        self._handle.setVisible(is_design and self.isSelected())
        self.update()

    # ── geometry ───────────────────────────────────────────────────────────
    def resize(self, w: float, h: float) -> None:
        self.prepareGeometryChange()
        self._width = w
        self._height = h
        self._handle.reposition()
        self.update()

    def boundingRect(self) -> QtCore.QRectF:
        pad = 4.0
        return QtCore.QRectF(-self._width / 2 - pad, -self._height / 2 - pad,
                              self._width + pad * 2, self._height + pad * 2)

    def _base_rect(self) -> QtCore.QRectF:
        return QtCore.QRectF(-self._width / 2, -self._height / 2,
                              self._width, self._height)

    def _build_path(self) -> QtGui.QPainterPath:
        p = QtGui.QPainterPath()
        r = self._base_rect()
        if self._shape_type == ItemShape.ELLIPSE:
            p.addEllipse(r)
        elif self._shape_type == ItemShape.POLYGON and self._polygon_points:
            poly = QtGui.QPolygonF(
                [QtCore.QPointF(pt[0], pt[1]) for pt in self._polygon_points])
            p.addPolygon(poly)
            p.closeSubpath()
        else:
            p.addRoundedRect(r, CORNER_RADIUS, CORNER_RADIUS)
        return p

    def shape(self) -> QtGui.QPainterPath:
        return self._build_path()

    # ── painting ───────────────────────────────────────────────────────────
    def paint(self, painter: QtGui.QPainter,
              option: QtWidgets.QStyleOptionGraphicsItem,
              widget: Optional[QtWidgets.QWidget] = None) -> None:
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        path = self._build_path()
        selected = self.isSelected()

        if self._glow_value > 0.01:
            glow_col = QtGui.QColor(COL_GLOW)
            glow_col.setAlphaF(self._glow_value * 0.6)
            glow_pen = QtGui.QPen(glow_col, 3.0 + self._glow_value * 3.0)
            glow_pen.setJoinStyle(Qt.RoundJoin)
            painter.setPen(glow_pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawPath(path)

        if selected:
            bg = COL_ITEM_SELECTED
        elif self._hover:
            bg = COL_ITEM_HOVER
        else:
            bg = COL_ITEM_DEFAULT

        if selected:
            border, pw = COL_BORDER_SELECTED, 2.0
        elif self._hover:
            border, pw = COL_BORDER_HOVER, 1.5
        else:
            border, pw = COL_BORDER_DEFAULT, 1.0

        painter.setPen(QtGui.QPen(border, pw))
        painter.setBrush(bg)
        painter.drawPath(path)

        painter.setPen(COL_TEXT_SELECTED if selected else COL_TEXT_DEFAULT)
        font = painter.font()
        font.setPointSize(9)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(self._base_rect(), Qt.AlignCenter, self._label)

        if self._mode == PickerMode.DESIGN:
            painter.setPen(QtGui.QPen(QtGui.QColor(120, 200, 255, 60),
                                       0.5, Qt.DashLine))
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(self._base_rect())

    # ── hover events ───────────────────────────────────────────────────────
    def hoverEnterEvent(self, event) -> None:
        self._hover = True
        self._glow_anim.stop()
        self._glow_anim.setStartValue(self._glow_value)
        self._glow_anim.setEndValue(1.0)
        self._glow_anim.start()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event) -> None:
        self._hover = False
        self._glow_anim.stop()
        self._glow_anim.setStartValue(self._glow_value)
        self._glow_anim.setEndValue(0.0)
        self._glow_anim.start()
        super().hoverLeaveEvent(event)

    # ── selection / snap ───────────────────────────────────────────────────
    def itemChange(self, change, value):
        if change == QtWidgets.QGraphicsItem.ItemSelectedHasChanged:
            self._handle.setVisible(
                bool(value) and self._mode == PickerMode.DESIGN)
            self._handle.reposition()
        if (change == QtWidgets.QGraphicsItem.ItemPositionChange
                and self._mode == PickerMode.DESIGN and self._snap_enabled):
            return snap_point(value)
        return super().itemChange(change, value)

    # ── mouse events ──────────────────────────────────────────────────────
    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            if self._mode == PickerMode.ANIMATION:
                add = bool(event.modifiers()
                           & (Qt.ShiftModifier | Qt.ControlModifier))
                self._do_click_action(add=add)
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
            # Design mode — record start position for undo
            self._drag_start_pos = QtCore.QPointF(self.pos())
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if (event.button() == Qt.LeftButton
                and self._mode == PickerMode.DESIGN
                and self._drag_start_pos is not None):
            new_pos = self.pos()
            if new_pos != self._drag_start_pos:
                # Collect all items that were dragged together (selected)
                pairs: List[tuple] = []
                for it in (self.scene().selectedItems() if self.scene() else []):
                    if isinstance(it, PickerItem) and it._drag_start_pos is not None:
                        pairs.append((it, it._drag_start_pos))
                        it._drag_start_pos = None
                if not pairs:
                    pairs = [(self, self._drag_start_pos)]
                stack = self._undo_stack()
                if stack:
                    # Don't re-execute; items are already at new positions
                    cmd = MoveItemCommand(pairs, "Move")
                    stack.push(cmd)
            self._drag_start_pos = None
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if self._mode == PickerMode.DESIGN and event.button() == Qt.LeftButton:
            new_label, ok = QtWidgets.QInputDialog.getText(
                None, "Rename Item", "Label:", text=self._label)
            if ok and new_label and new_label != self._label:
                stack = self._undo_stack()
                if stack:
                    stack.push(PropertyChangeCommand(
                        self, "_label", self._label, new_label, "Rename"))
                else:
                    self.label = new_label
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    # ── context menu (all actions go through undo) ─────────────────────────
    def contextMenuEvent(self, event) -> None:
        if self._mode != PickerMode.DESIGN:
            return
        stack = self._undo_stack()

        menu = QtWidgets.QMenu()
        act_rename  = menu.addAction("Rename")
        act_nodes   = menu.addAction("Edit Maya Nodes...")
        act_cmd     = menu.addAction("Edit Click Command...")
        act_ns      = menu.addAction("Set Namespace...")
        act_shape   = menu.addMenu("Shape")
        act_s_rect  = act_shape.addAction("Rectangle")
        act_s_ellip = act_shape.addAction("Ellipse")
        act_s_poly  = act_shape.addAction("Polygon (edit points)...")
        menu.addSeparator()
        act_rot_cw  = menu.addAction("Rotate +15\u00b0")
        act_rot_ccw = menu.addAction("Rotate \u221215\u00b0")
        act_rot_0   = menu.addAction("Reset Rotation")
        menu.addSeparator()
        act_del = menu.addAction("Delete")

        chosen = menu.exec_(event.screenPos())
        if chosen is None:
            return

        if chosen == act_rename:
            t, ok = QtWidgets.QInputDialog.getText(
                None, "Rename", "Label:", text=self._label)
            if ok and t and t != self._label:
                if stack:
                    stack.push(PropertyChangeCommand(
                        self, "_label", self._label, t, "Rename"))
                else:
                    self.label = t

        elif chosen == act_nodes:
            t, ok = QtWidgets.QInputDialog.getText(
                None, "Maya Nodes", "Comma-separated:",
                text=", ".join(self._maya_nodes))
            if ok:
                new_nodes = [n.strip() for n in t.split(",") if n.strip()]
                if new_nodes != self._maya_nodes:
                    if stack:
                        stack.push(PropertyChangeCommand(
                            self, "_maya_nodes",
                            list(self._maya_nodes), new_nodes, "Edit Nodes"))
                    else:
                        self._maya_nodes = new_nodes

        elif chosen == act_cmd:
            t, ok = QtWidgets.QInputDialog.getMultiLineText(
                None, "Click Command", "Python script:",
                self._command_on_click)
            if ok and t != self._command_on_click:
                if stack:
                    stack.push(PropertyChangeCommand(
                        self, "_command_on_click",
                        self._command_on_click, t, "Edit Command"))
                else:
                    self._command_on_click = t

        elif chosen == act_ns:
            t, ok = QtWidgets.QInputDialog.getText(
                None, "Namespace", "Namespace (empty = none):",
                text=self._namespace)
            if ok:
                ns = t.strip()
                if ns != self._namespace:
                    if stack:
                        stack.push(PropertyChangeCommand(
                            self, "_namespace",
                            self._namespace, ns, "Set Namespace"))
                    else:
                        self._namespace = ns

        elif chosen == act_s_rect:
            self._undoable_set_shape(stack, ItemShape.RECT, [])

        elif chosen == act_s_ellip:
            self._undoable_set_shape(stack, ItemShape.ELLIPSE, [])

        elif chosen == act_s_poly:
            self._edit_polygon_points_undoable(stack)

        elif chosen == act_rot_cw:
            old = self.rotation()
            if stack:
                stack.push(RotateItemCommand(self, old, old + 15))
            else:
                self.setRotation(old + 15)

        elif chosen == act_rot_ccw:
            old = self.rotation()
            if stack:
                stack.push(RotateItemCommand(self, old, old - 15))
            else:
                self.setRotation(old - 15)

        elif chosen == act_rot_0:
            old = self.rotation()
            if old != 0 and stack:
                stack.push(RotateItemCommand(self, old, 0))
            else:
                self.setRotation(0)

        elif chosen == act_del:
            scene = self.scene()
            if stack and scene:
                stack.push(DeleteItemCommand(scene, self))
            elif scene:
                scene.removeItem(self)

    def _undoable_set_shape(self, stack: Optional[QtWidgets.QUndoStack],
                            shape: ItemShape,
                            poly: List[List[float]]) -> None:
        old_shape = self._shape_type
        old_poly = list(self._polygon_points)
        if shape == old_shape and poly == old_poly:
            return
        if stack:
            stack.push(ChangeShapeCommand(
                self, old_shape, old_poly, shape, poly))
        else:
            self.prepareGeometryChange()
            self._shape_type = shape
            self._polygon_points = list(poly)
            self.update()

    def _edit_polygon_points_undoable(
            self, stack: Optional[QtWidgets.QUndoStack]) -> None:
        current = str(self._polygon_points) if self._polygon_points else ""
        hint = ("Enter points as [[x1,y1],[x2,y2],...]\n"
                "Coordinates are relative to item centre.")
        t, ok = QtWidgets.QInputDialog.getMultiLineText(
            None, "Polygon Points", hint, current)
        if not ok:
            return
        try:
            import ast
            pts = ast.literal_eval(t)
            if isinstance(pts, list) and all(
                    isinstance(p, (list, tuple)) and len(p) == 2 for p in pts):
                new_poly = [[float(p[0]), float(p[1])] for p in pts]
                self._undoable_set_shape(stack, ItemShape.POLYGON, new_poly)
            else:
                raise ValueError
        except Exception:
            cmds.warning("DD Picker: invalid polygon points format")

    # ── click action (Animation mode — not undoable) ──────────────────────
    def _do_click_action(self, add: bool = False) -> None:
        nodes = self.resolved_nodes()
        if nodes:
            existing = [n for n in nodes if cmds.objExists(n)]
            missing  = [n for n in nodes if n not in existing]
            if missing:
                self._show_toast("Not found: " + ", ".join(missing))
            if existing:
                try:
                    cmds.select(existing, add=add, replace=not add)
                except Exception:
                    traceback.print_exc()

        if self._command_on_click:
            try:
                exec(self._command_on_click, {"__builtins__": __builtins__,
                                              "cmds": cmds, "mel": mel})
            except Exception as exc:
                self._show_toast("Script error: {}".format(exc))
                traceback.print_exc()

    def _show_toast(self, msg: str) -> None:
        view = self.scene().views()[0] if self.scene() and self.scene().views() else None
        if view and hasattr(view, "_toast"):
            view._toast.show_message(msg)
        else:
            cmds.warning("DD Picker: " + msg)

    # ── serialisation ─────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        d: Dict[str, Any] = dict(
            label=self._label,
            maya_nodes=self._maya_nodes,
            x=self.pos().x(), y=self.pos().y(),
            w=self._width, h=self._height,
            rotation=self.rotation(),
            shape=self._shape_type.value,
            namespace=self._namespace,
        )
        if self._command_on_click:
            d["command_on_click"] = self._command_on_click
        if self._polygon_points:
            d["polygon_points"] = self._polygon_points
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "PickerItem":
        shape_str = data.get("shape", "rect")
        try:
            shape = ItemShape(shape_str)
        except ValueError:
            shape = ItemShape.RECT
        item = cls(
            label=data.get("label", ""),
            maya_nodes=data.get("maya_nodes", []),
            width=data.get("w", 60),
            height=data.get("h", 30),
            shape=shape,
            polygon_points=data.get("polygon_points"),
            command_on_click=data.get("command_on_click", ""),
            namespace=data.get("namespace", ""),
        )
        item.setPos(data.get("x", 0), data.get("y", 0))
        item.setRotation(data.get("rotation", 0))
        return item


# ╔═════════════════════════════════════════════════════════════════════════╗
# ║  BACKGROUND IMAGE ITEM                                                 ║
# ╚═════════════════════════════════════════════════════════════════════════╝

class BackgroundImageItem(QtWidgets.QGraphicsPixmapItem):
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
        self.setOffset(-pixmap.width() / 2.0, -pixmap.height() / 2.0)

    def set_opacity_value(self, value: float) -> None:
        self._opacity_value = max(0.0, min(1.0, value))
        self.setOpacity(self._opacity_value)

    def set_image_scale(self, factor: float) -> None:
        self.setScale(max(0.05, min(10.0, factor)))

    def set_movable(self, movable: bool) -> None:
        self.setFlag(QtWidgets.QGraphicsItem.ItemIsMovable, movable)


# ╔═════════════════════════════════════════════════════════════════════════╗
# ║  MAYA CALLBACK MANAGER  (singleton)                                     ║
# ╚═════════════════════════════════════════════════════════════════════════╝

class MayaCallbackManager:
    _instance: Optional["MayaCallbackManager"] = None
    _initialized: bool = False

    def __new__(cls) -> "MayaCallbackManager":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return
        self._initialized = True
        self._callback_id: Optional[int] = None
        self._scene: Optional[PickerScene] = None
        self._suppress: bool = False

    def register(self, scene: "PickerScene") -> None:
        self.unregister()
        self._scene = scene
        try:
            self._callback_id = om2.MEventMessage.addEventCallback(
                "SelectionChanged", self._on_maya_selection_changed)
        except Exception:
            traceback.print_exc()
            self._callback_id = None

    def unregister(self) -> None:
        if self._callback_id is not None:
            try:
                om2.MMessage.removeCallback(self._callback_id)
            except Exception:
                pass
            self._callback_id = None
        self._scene = None

    @property
    def suppress(self) -> bool:
        return self._suppress

    @suppress.setter
    def suppress(self, value: bool) -> None:
        self._suppress = value

    def _on_maya_selection_changed(self, *_args) -> None:
        if self._suppress or self._scene is None:
            return
        try:
            sel = set(cmds.ls(selection=True, long=False) or [])
            for item in self._scene.items():
                if not isinstance(item, PickerItem):
                    continue
                resolved = set(item.resolved_nodes())
                should_select = bool(resolved & sel)
                if item.isSelected() != should_select:
                    item.setSelected(should_select)
        except Exception:
            traceback.print_exc()


_callback_mgr = MayaCallbackManager()


# ╔═════════════════════════════════════════════════════════════════════════╗
# ║  PICKER SCENE                                                          ║
# ╚═════════════════════════════════════════════════════════════════════════╝

class PickerScene(QtWidgets.QGraphicsScene):
    mode_changed = Signal(PickerMode)

    def __init__(self, parent: Optional[QtCore.QObject] = None):
        super().__init__(parent)
        self._mode: PickerMode = PickerMode.ANIMATION
        self._bg_item: Optional[BackgroundImageItem] = None
        self.undo_stack = QtWidgets.QUndoStack(self)

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
        pixmap = QtGui.QPixmap(path)
        if pixmap.isNull():
            cmds.warning("DD Picker: failed to load image — " + path)
            return None
        return self.set_background_pixmap(pixmap)

    def set_background_pixmap(self, pixmap: QtGui.QPixmap) -> Optional[BackgroundImageItem]:
        """Set a QPixmap directly as the canvas background."""
        if pixmap.isNull():
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

    def add_picker_item(
        self, label: str, maya_nodes: List[str],
        x: float = 0, y: float = 0,
        w: float = 60, h: float = 30,
        shape: ItemShape = ItemShape.RECT,
        polygon_points: Optional[List[List[float]]] = None,
        command_on_click: str = "",
        namespace: str = "",
        undoable: bool = False,
    ) -> PickerItem:
        item = PickerItem(
            label=label, maya_nodes=maya_nodes, width=w, height=h,
            shape=shape, polygon_points=polygon_points,
            command_on_click=command_on_click, namespace=namespace,
        )
        item.setPos(x, y)
        item.set_mode(self._mode)
        if undoable:
            self.undo_stack.push(AddItemCommand(self, item))
        else:
            self.addItem(item)
        return item


# ╔═════════════════════════════════════════════════════════════════════════╗
# ║  PICKER GRAPHICS VIEW                                                  ║
# ╚═════════════════════════════════════════════════════════════════════════╝

class PickerGraphicsView(QtWidgets.QGraphicsView):
    zoom_changed = Signal(float)

    def __init__(self, scene: PickerScene,
                 parent: Optional[QtWidgets.QWidget] = None):
        super().__init__(scene, parent)

        self.setRenderHints(
            QtGui.QPainter.Antialiasing
            | QtGui.QPainter.SmoothPixmapTransform)
        self.setViewportUpdateMode(
            QtWidgets.QGraphicsView.FullViewportUpdate)
        self.setTransformationAnchor(
            QtWidgets.QGraphicsView.NoAnchor)
        self.setResizeAnchor(
            QtWidgets.QGraphicsView.NoAnchor)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setDragMode(QtWidgets.QGraphicsView.NoDrag)
        self.setBackgroundBrush(COL_CANVAS_BG)
        self.setSceneRect(-1e10, -1e10, 2e10, 2e10)

        self._zoom: float = 1.0
        self._panning: bool = False
        self._pan_start = QtCore.QPoint()

        self._rb: Optional[QtWidgets.QRubberBand] = None
        self._rb_active: bool = False
        self._rb_origin = QtCore.QPoint()

        self._toast = _ToastLabel(self)

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

    # -- rubber-band --------------------------------------------------------
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

    # -- events -------------------------------------------------------------
    def wheelEvent(self, event) -> None:
        angle = event.angleDelta().y()
        factor = ZOOM_FACTOR if angle > 0 else 1.0 / ZOOM_FACTOR
        pos = (event.position().toPoint() if PYSIDE_VERSION == 6
               else event.pos())
        self._apply_zoom(factor, pos)

    def mousePressEvent(self, event) -> None:
        btn, mods = event.button(), event.modifiers()
        if btn == Qt.MiddleButton or (btn == Qt.LeftButton and mods & Qt.AltModifier):
            self._panning = True
            self._pan_start = event.pos()
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()
            return
        if btn == Qt.LeftButton:
            picker_scene = self.scene()
            if (isinstance(picker_scene, PickerScene)
                    and picker_scene.mode == PickerMode.ANIMATION
                    and self.itemAt(event.pos()) is None):
                self._rb_start(event.pos())
                if not (mods & (Qt.ShiftModifier | Qt.ControlModifier)):
                    picker_scene.clearSelection()
                    _callback_mgr.suppress = True
                    cmds.select(clear=True)
                    _callback_mgr.suppress = False
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

    # -- grid ---------------------------------------------------------------
    def drawBackground(self, painter: QtGui.QPainter,
                       rect: QtCore.QRectF) -> None:
        super().drawBackground(painter, rect)
        scene = self.scene()
        if not isinstance(scene, PickerScene):
            return
        if scene.mode != PickerMode.DESIGN:
            return

        gs = GRID_SIZE
        major = gs * 5
        left = int(math.floor(rect.left() / gs)) * gs
        top  = int(math.floor(rect.top()  / gs)) * gs

        minor_lines: list = []
        major_lines: list = []
        origin_lines: list = []

        x = left
        while x <= rect.right():
            line = QtCore.QLineF(x, rect.top(), x, rect.bottom())
            if x == 0:
                origin_lines.append(line)
            elif x % major == 0:
                major_lines.append(line)
            else:
                minor_lines.append(line)
            x += gs

        y = top
        while y <= rect.bottom():
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
# ║  SELECTION SYNC                                                         ║
# ╚═════════════════════════════════════════════════════════════════════════╝

def _sync_maya_selection(scene: QtWidgets.QGraphicsScene) -> None:
    nodes: List[str] = []
    for item in scene.selectedItems():
        if isinstance(item, PickerItem):
            nodes.extend(item.resolved_nodes())
    _callback_mgr.suppress = True
    try:
        if nodes:
            existing = [n for n in nodes if cmds.objExists(n)]
            if existing:
                cmds.select(existing, replace=True)
        else:
            cmds.select(clear=True)
    except Exception:
        traceback.print_exc()
    finally:
        _callback_mgr.suppress = False


# ╔═════════════════════════════════════════════════════════════════════════╗
# ║  MAIN WINDOW                                                           ║
# ╚═════════════════════════════════════════════════════════════════════════╝

class DDPickerWindow(MayaQWidgetDockableMixin, QtWidgets.QMainWindow):
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

        _callback_mgr.register(self._scene)

    # -- UI -----------------------------------------------------------------
    def _build_ui(self) -> None:
        central = QtWidgets.QWidget(self)
        self.setCentralWidget(central)
        root = QtWidgets.QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # toolbar
        tb = QtWidgets.QHBoxLayout()
        tb.setContentsMargins(6, 4, 6, 4)

        self._btn_mode = QtWidgets.QPushButton("Animation Mode")
        self._btn_mode.setCheckable(True)
        self._btn_mode.setFixedHeight(24)
        self._btn_mode.setToolTip("Toggle Design / Animation mode")
        self._btn_mode.clicked.connect(self._on_toggle_mode)
        tb.addWidget(self._btn_mode)
        tb.addSpacing(8)

        self._chk_snap = QtWidgets.QCheckBox("Snap")
        self._chk_snap.setChecked(True)
        self._chk_snap.setToolTip("Snap to grid (Design mode)")
        self._chk_snap.toggled.connect(self._on_snap_toggled)
        tb.addWidget(self._chk_snap)
        tb.addSpacing(8)

        self._lbl_zoom = QtWidgets.QLabel("100 %")
        self._lbl_zoom.setFixedWidth(52)
        tb.addWidget(self._lbl_zoom)
        tb.addStretch()

        btn_reset = QtWidgets.QPushButton("Reset")
        btn_reset.setFixedHeight(24)
        btn_reset.clicked.connect(self._on_reset_view)
        tb.addWidget(btn_reset)

        btn_fit = QtWidgets.QPushButton("Fit")
        btn_fit.setFixedHeight(24)
        btn_fit.clicked.connect(self._view.fit_all)
        tb.addWidget(btn_fit)

        root.addLayout(tb)

        # background controls
        bg_row = QtWidgets.QHBoxLayout()
        bg_row.setContentsMargins(6, 0, 6, 4)

        btn_load_bg = QtWidgets.QPushButton("Load BG")
        btn_load_bg.setFixedHeight(22)
        btn_load_bg.clicked.connect(self._on_load_bg)
        bg_row.addWidget(btn_load_bg)

        btn_screenshot_bg = QtWidgets.QPushButton("Screenshot BG")
        btn_screenshot_bg.setFixedHeight(22)
        btn_screenshot_bg.setToolTip("Capture Maya viewport as background")
        btn_screenshot_bg.clicked.connect(self._on_screenshot_bg)
        bg_row.addWidget(btn_screenshot_bg)

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

        root.addWidget(self._view)

    def _build_menubar(self) -> None:
        mb = self.menuBar()

        # -- Edit (Undo / Redo) --
        m_edit = mb.addMenu("Edit")
        undo_stack = self._scene.undo_stack

        self._act_undo = undo_stack.createUndoAction(self, "Undo")
        self._act_undo.setShortcut(QtGui.QKeySequence.Undo)
        m_edit.addAction(self._act_undo)

        self._act_redo = undo_stack.createRedoAction(self, "Redo")
        self._act_redo.setShortcut(QtGui.QKeySequence.Redo)
        m_edit.addAction(self._act_redo)

        # -- File --
        m_file = mb.addMenu("File")
        m_file.addAction("Load Background Image...", self._on_load_bg)
        m_file.addAction("Screenshot as Background", self._on_screenshot_bg)
        m_file.addAction("Remove Background Image",  self._on_remove_bg)

        # -- View --
        m_view = mb.addMenu("View")
        m_view.addAction("Reset View",  self._on_reset_view)
        m_view.addAction("Fit All",     self._view.fit_all)

        # -- Item --
        m_item = mb.addMenu("Item")
        m_item.addAction("Add Rectangle...",
                         lambda: self._on_add_item(ItemShape.RECT))
        m_item.addAction("Add Ellipse...",
                         lambda: self._on_add_item(ItemShape.ELLIPSE))

    def _connect_signals(self) -> None:
        self._view.zoom_changed.connect(self._on_zoom_changed)

    # -- demo ---------------------------------------------------------------
    def _add_demo_items(self) -> None:
        demos = [
            ("Head",   ["head_ctrl"],       0, -140, 50, 50, ItemShape.ELLIPSE),
            ("Neck",   ["neck_ctrl"],       0, -100, 50, 22, ItemShape.RECT),
            ("Body",   ["body_ctrl"],       0,    0, 70, 30, ItemShape.RECT),
            ("L_Clav", ["L_clavicle_ctrl"],-80, -60, 60, 22, ItemShape.RECT),
            ("R_Clav", ["R_clavicle_ctrl"], 80, -60, 60, 22, ItemShape.RECT),
            ("L_Arm",  ["L_arm_ctrl"],    -120,   0, 60, 28, ItemShape.RECT),
            ("R_Arm",  ["R_arm_ctrl"],     120,   0, 60, 28, ItemShape.RECT),
            ("L_Hand", ["L_hand_ctrl"],   -120,  60, 50, 50, ItemShape.ELLIPSE),
            ("R_Hand", ["R_hand_ctrl"],    120,  60, 50, 50, ItemShape.ELLIPSE),
            ("Hip",    ["hip_ctrl"],         0,  60, 60, 26, ItemShape.RECT),
            ("L_Leg",  ["L_leg_ctrl"],     -50, 120, 60, 28, ItemShape.RECT),
            ("R_Leg",  ["R_leg_ctrl"],      50, 120, 60, 28, ItemShape.RECT),
            ("L_Foot", ["L_foot_ctrl"],    -50, 180, 60, 28, ItemShape.RECT),
            ("R_Foot", ["R_foot_ctrl"],     50, 180, 60, 28, ItemShape.RECT),
        ]
        for label, nodes, x, y, w, h, shp in demos:
            self._scene.add_picker_item(label, nodes, x, y, w, h, shape=shp)

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

    def _on_screenshot_bg(self) -> None:
        """Capture the active Maya viewport and use it as the background."""
        import tempfile
        import os

        # Choose source
        sources = ["Active Viewport"]
        # Check if playblast-based full panel capture is possible
        panels = cmds.getPanel(visiblePanels=True) or []
        model_panels = [p for p in panels if cmds.getPanel(typeOf=p) == "modelPanel"]
        if model_panels:
            for mp in model_panels:
                cam = cmds.modelPanel(mp, query=True, camera=True)
                sources.append("{} ({})".format(mp, cam))

        source, ok = QtWidgets.QInputDialog.getItem(
            self, "Screenshot Background",
            "Capture source:",
            sources, 0, False)
        if not ok:
            return

        tmp_dir = tempfile.gettempdir()
        tmp_path = os.path.join(tmp_dir, "dd_picker_screenshot.png")

        try:
            if source == "Active Viewport":
                # Use playblast to capture the active viewport
                panel = cmds.getPanel(withFocus=True)
                if not panel or cmds.getPanel(typeOf=panel) != "modelPanel":
                    # Fall back to first visible modelPanel
                    if model_panels:
                        panel = model_panels[0]
                    else:
                        self._view._toast.show_message(
                            "No model panel found")
                        return
                cmds.setFocus(panel)
                result = cmds.playblast(
                    frame=cmds.currentTime(query=True),
                    format="image",
                    compression="png",
                    quality=100,
                    widthHeight=[1920, 1080],
                    showOrnaments=False,
                    viewer=False,
                    filename=tmp_path.replace(".png", ""),
                    completeFilename=tmp_path,
                    percent=100,
                    offScreen=True,
                    clearCache=True,
                    forceOverwrite=True,
                )
            else:
                # Specific model panel selected
                chosen_panel = source.split(" (")[0]
                cmds.setFocus(chosen_panel)
                result = cmds.playblast(
                    frame=cmds.currentTime(query=True),
                    format="image",
                    compression="png",
                    quality=100,
                    widthHeight=[1920, 1080],
                    showOrnaments=False,
                    viewer=False,
                    filename=tmp_path.replace(".png", ""),
                    completeFilename=tmp_path,
                    percent=100,
                    offScreen=True,
                    clearCache=True,
                    forceOverwrite=True,
                )

            if not os.path.isfile(tmp_path):
                self._view._toast.show_message("Screenshot failed")
                return

            pixmap = QtGui.QPixmap(tmp_path)
            if pixmap.isNull():
                self._view._toast.show_message("Failed to load screenshot")
                return

            bg = self._scene.set_background_pixmap(pixmap)
            if bg:
                self._sld_opacity.setValue(50)
                self._spn_bg_scale.setValue(1.0)

            # Clean up temp file
            try:
                os.remove(tmp_path)
            except OSError:
                pass

        except Exception as exc:
            self._view._toast.show_message("Screenshot error: {}".format(exc))
            traceback.print_exc()

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
    def _on_add_item(self, shape: ItemShape = ItemShape.RECT) -> None:
        label, ok = QtWidgets.QInputDialog.getText(
            self, "Add Picker Item", "Label:")
        if not ok or not label:
            return
        nodes_str, ok = QtWidgets.QInputDialog.getText(
            self, "Maya Nodes", "Comma-separated Maya node names:")
        if not ok:
            return
        nodes = [n.strip() for n in nodes_str.split(",") if n.strip()]
        centre = self._view.mapToScene(
            self._view.viewport().rect().center())
        self._scene.add_picker_item(
            label, nodes, centre.x(), centre.y(),
            shape=shape, undoable=True)

    # -- lifecycle ----------------------------------------------------------
    def dockCloseEventTriggered(self) -> None:
        _callback_mgr.unregister()
        DDPickerWindow._instance = None
        super().dockCloseEventTriggered()


# ╔═════════════════════════════════════════════════════════════════════════╗
# ║  PUBLIC API                                                             ║
# ╚═════════════════════════════════════════════════════════════════════════╝

def show() -> DDPickerWindow:
    _close_existing()
    win = DDPickerWindow()
    win.show(dockable=True, area="right", allowedArea="all",
             floating=True, width=650, height=550)
    DDPickerWindow._instance = win
    return win


def _close_existing() -> None:
    _callback_mgr.unregister()
    if DDPickerWindow._instance is not None:
        try:
            DDPickerWindow._instance.close()
        except Exception:
            pass
        DDPickerWindow._instance = None
    if cmds.workspaceControl(WORKSPACE_CONTROL_NAME, exists=True):
        cmds.deleteUI(WORKSPACE_CONTROL_NAME)


def add_picker_item(
    label: str, maya_nodes: List[str],
    x: float = 0, y: float = 0,
    w: float = 60, h: float = 30,
    shape: ItemShape = ItemShape.RECT,
    polygon_points: Optional[List[List[float]]] = None,
    command_on_click: str = "",
    namespace: str = "",
) -> Optional[PickerItem]:
    """Add a PickerItem to the active picker scene.

    Examples::

        dd_picker.show()
        dd_picker.add_picker_item("Body", ["body_ctrl"], 0, 0)
        dd_picker.add_picker_item("Head", ["head_ctrl"], 0, -100,
                                  50, 50, shape=dd_picker.ItemShape.ELLIPSE)
    """
    win = DDPickerWindow._instance
    if win is None:
        cmds.warning("DD Picker: call dd_picker.show() first.")
        return None
    return win._scene.add_picker_item(
        label, maya_nodes, x, y, w, h,
        shape=shape, polygon_points=polygon_points,
        command_on_click=command_on_click, namespace=namespace)
