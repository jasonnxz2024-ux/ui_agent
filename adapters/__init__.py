"""UAgent LVGL/UIBuilder adapter collection."""

from .lvgl import (
    CarouselLVGLAdapter,
    GaugeLVGLAdapter,
    DropdownLVGLAdapter,
    RechartsLVGLAdapter,
    ScrollLVGLAdapter,
    TableLVGLAdapter,
    adapt_component,
    adapt_components,
)

__all__ = [
    "CarouselLVGLAdapter", "GaugeLVGLAdapter", "DropdownLVGLAdapter", "RechartsLVGLAdapter",
    "ScrollLVGLAdapter", "TableLVGLAdapter", "adapt_component", "adapt_components",
]
