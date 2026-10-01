"""Layout parser selection for Figma Make projects.

The registry is deliberately small at first: existing generation remains
backwards compatible while projects receive an explicit strategy/profile that
future adapters can implement without changing the main pipeline.
"""
from dataclasses import dataclass
from typing import Any
from pathlib import Path
import re


@dataclass(frozen=True)
class ParserProfile:
    parser_id: str
    confidence: float
    evidence: tuple[str, ...]

@dataclass(frozen=True)
class BrowserPlan:
    navigation: tuple[str, ...] = ()
    reset_label: str | None = None
    top_level_navigation: tuple[str, ...] = ()

class ProjectParser:
    parser_id = "generic-dom"
    confidence = .35
    evidence = ("no specialized markers",)
    @property
    def profile(self): return ParserProfile(self.parser_id, self.confidence, self.evidence)
    def browser_plan(self): return BrowserPlan()
    def page_keywords(self): return {}
    def expand_controls(self, component: Any, controls: list[dict[str, Any]]): return controls
    def materialize_page_geometry(self, **kwargs): return None

class _MarkerParser(ProjectParser):
    def __init__(self, parser_id, confidence, evidence): self.parser_id, self.confidence, self.evidence = parser_id, confidence, evidence
    def browser_plan(self):
        if self.parser_id == "print-device": return BrowserPlan(reset_label="Home")
        if self.parser_id == "meter": return BrowserPlan(navigation=("dashboard", "settings"), reset_label="dashboard")
        return BrowserPlan()
    def page_keywords(self):
        if self.parser_id != "print-device": return {}
        return {"HomePage": (), "PaperStatus": ("纸张",), "InkStatus": ("墨水",), "SettingsPage": ("设置",), "BasicSettings": ("设置", "基本设置"), "LanguageSettings": ("设置", "基本设置", "语言"), "TimeSettings": ("设置", "基本设置", "时间"), "NetworkSettings": ("设置", "基本设置", "网络"), "MaintenancePage": ("设置", "维护"), "CutterSetting": ("设置", "切刀")}


def detect_parser(source_dir: Path) -> ParserProfile:
    files = [
        path for pattern in ("*.tsx", "*.ts")
        for path in source_dir.rglob(pattern)
        if "node_modules" not in path.parts and "dist" not in path.parts
    ]
    text = "\n".join(p.read_text(encoding="utf-8", errors="ignore") for p in files[:400])
    low = text.lower()
    if "paperstatus" in low and "inkstatus" in low and ("w-[320px]" in low or "320x240" in low):
        return ParserProfile("print-device", .98, ("PaperStatus/InkStatus routes", "fixed 320x240 device frame"))
    if "fortune" in low or "crackcookie" in low or "framer-motion" in low:
        return ParserProfile("interactive-scene", .92, ("stateful motion scene", "cookie/fortune markers"))
    if "pupilposition" in low or "eyecard" in low or "mousemove" in low:
        return ParserProfile("interactive-vector", .94, ("pointer-driven vector", "fixed 204px eye component"))
    if "tab-btn" in low and "setpage" in low and "usesensorvalues" in low and re.search(r"width\s*[:=]\s*320[\s\S]{0,240}height\s*[:=]\s*240", text):
        return ParserProfile("meter", .99, ("320x240 root", "tab state navigation", "sensor timer", "SVG gauges"))
    if "recharts" in low or "piechart" in low or "linechart" in low:
        return ParserProfile("responsive-dashboard", .93, ("chart library", "dashboard composition"))
    return ParserProfile("generic-dom", .35, ("no specialized markers",))

def select_parser(source_dir: Path) -> ProjectParser:
    profile = detect_parser(source_dir)
    return _MarkerParser(profile.parser_id, profile.confidence, profile.evidence)
