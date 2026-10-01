"""Mandatory acceptance gate for browser-to-AiBuilder generation.

The validator is project-agnostic.  It compares interaction states, DOM
semantics and the generated snapshot, then optionally runs an already-built
simulator executable and performs visual comparison.  A project is never
reported as complete unless every required stage passes.
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageGrab, ImageStat

from core.capability import source_browser_plan_counts
from core.dynamic_validation import discover_canvas, overflow_evidence, region_gate, region_manifest


# These are acceptance policy, not sample-specific tuning.  Keep them
# explicit in the report so a consumer can audit why a region passed.
DEFAULT_REGION_VISUAL_POLICY: dict[str, dict[str, float]] = {
    "header": {"pixel": 0.12, "edge": 0.22, "coverage": 0.30},
    "gauge": {"pixel": 0.10, "edge": 0.20, "coverage": 0.25},
    "control": {"pixel": 0.12, "edge": 0.24, "coverage": 0.30},
    "status": {"pixel": 0.12, "edge": 0.24, "coverage": 0.30},
    "background": {"pixel": 0.12, "edge": 0.30, "coverage": 0.35},
}


def _region_box_mask(size: tuple[int, int], region: dict[str, Any]):
    """Return a mask for the union of manifest boxes, clipped to the image."""
    from PIL import ImageDraw
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    for box in region.get("boxes", []) or []:
        x, y = float(box.get("x", 0)), float(box.get("y", 0))
        w, h = float(box.get("width", 0)), float(box.get("height", 0))
        if w > 0 and h > 0:
            draw.rectangle((round(x), round(y), round(x + w) - 1, round(y + h) - 1), fill=255)
    return mask


def _masked_mean(image, mask) -> float:
    values = list(image.getdata())
    active = [sum(px) / (len(px) * 255) if isinstance(px, tuple) else px / 255
              for px, m in zip(values, mask.getdata()) if m]
    return sum(active) / len(active) if active else 0.0


def _edge_image(image):
    from PIL import ImageFilter
    return image.convert("L").filter(ImageFilter.FIND_EDGES)


def region_visual_metrics(reference: Path | str, actual: Path | str,
                          manifest: dict[str, Any],
                          policy: dict[str, dict[str, float]] | None = None) -> dict[str, Any]:
    """Compare browser and simulator pixels inside each semantic region.

    ``coverage`` is the difference in non-background (ink) occupancy, rather
    than DOM area.  This catches a completely missing gauge even when the
    rest of the screen is nearly identical.
    """
    policy = policy or DEFAULT_REGION_VISUAL_POLICY
    with Image.open(reference).convert("RGB") as ref0, Image.open(actual).convert("RGB") as got0:
        size = ref0.size
        ref, got = ref0, got0.resize(size)
        ref_gray, got_gray = ref.convert("L"), got.convert("L")
        # Use the reference corner as the canvas background estimate. This is
        # deterministic and avoids assuming a particular theme color.
        bg = ref.getpixel((0, 0))
        def ink(image):
            return image.convert("RGB").point(lambda v: 255 if abs(v - bg[0]) > 16 else 0)
        ref_ink, got_ink = ink(ref), ink(got)
        ref_edges, got_edges = _edge_image(ref), _edge_image(got)
        result: dict[str, Any] = {}
        for name, region in (manifest.get("regions") or {}).items():
            if region.get("status") != "measured":
                result[name] = {"status": "N/A", "bbox": region.get("union_bounds"),
                                "boxes": region.get("boxes", []), "scores": None,
                                "threshold": policy.get(name), "passed": None,
                                "reason": "region absent in browser evidence"}
                continue
            mask = _region_box_mask(size, region)
            pixels = ImageChops.difference(ref, got)
            edges = ImageChops.difference(ref_edges, got_edges)
            pixel = _masked_mean(pixels, mask)
            edge = _masked_mean(edges, mask)
            ref_occ = _masked_mean(ref_ink, mask)
            got_occ = _masked_mean(got_ink, mask)
            coverage = abs(ref_occ - got_occ)
            limits = policy.get(name, {"pixel": 0.12, "edge": 0.24, "coverage": 0.30})
            scores = {"pixel": round(pixel, 5), "edge": round(edge, 5),
                      "coverage": round(coverage, 5), "reference_ink": round(ref_occ, 5),
                      "simulator_ink": round(got_occ, 5)}
            failed = [key for key in ("pixel", "edge", "coverage") if scores[key] > limits[key]]
            result[name] = {"status": "measured", "bbox": region.get("union_bounds"),
                            "boxes": region.get("boxes", []), "scores": scores,
                            "threshold": limits, "passed": not failed,
                            "reason": "ok" if not failed else "exceeded: " + ", ".join(failed)}
    return result


def _rgb(value: str | None) -> tuple[int, int, int] | None:
    if not value:
        return None
    value = value.strip()
    if re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        return tuple(int(value[i:i + 2], 16) for i in (1, 3, 5))
    match = re.fullmatch(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)(?:\s*,[^)]*)?\)", value)
    return tuple(int(match.group(i)) for i in (1, 2, 3)) if match else None


def _same_color(left: str | None, right: str | None, tolerance: int = 3) -> bool:
    a, b = _rgb(left), _rgb(right)
    return bool(a and b and max(abs(x - y) for x, y in zip(a, b)) <= tolerance)


def _screen_name(path: list[str], index: int) -> str:
    value = str(path[-1] if path else f"state_{index + 1:02d}").strip() or f"state_{index + 1:02d}"
    # Interaction labels often contain times, slashes or translated text.
    # Keep evidence export valid on Windows and deterministic everywhere.
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", value)
    return value.rstrip(" .")[:80] or f"state_{index + 1:02d}"


def _frame(nodes: list[dict[str, Any]], width: int, height: int) -> dict[str, float] | None:
    candidates = [n["rect"] for n in nodes if isinstance(n.get("rect"), dict)
                  and abs(float(n["rect"].get("width", 0)) - width) <= 2
                  and abs(float(n["rect"].get("height", 0)) - height) <= 2]
    return candidates[0] if candidates else None


def _export_references(layout: dict[str, Any], target: Path, width: int, height: int) -> list[Path]:
    target.mkdir(parents=True, exist_ok=True)
    result: list[Path] = []
    for index, screen in enumerate(layout.get("screens", [])):
        encoded = screen.get("screenshot_png_base64")
        if not encoded:
            continue
        raw = target / f"{index + 1:02d}_{_screen_name(screen.get('path', []), index)}_viewport.png"
        raw.write_bytes(base64.b64decode(encoded))
        data = screen.get("data", {})
        frame = _frame(data.get("nodes", []), width, height)
        output = target / f"{index + 1:02d}_{_screen_name(screen.get('path', []), index)}.png"
        with Image.open(raw) as image:
            if frame:
                box = (round(frame["x"]), round(frame["y"]),
                       round(frame["x"] + frame["width"]), round(frame["y"] + frame["height"]))
                image.crop(box).save(output)
            else:
                image.resize((width, height)).save(output)
        result.append(output)
    return result


def _find_simulator(project_root: Path) -> Path | None:
    explicit = os.environ.get("UAGENT_SIMULATOR_EXE")
    if explicit and Path(explicit).is_file():
        return Path(explicit)
    candidates = []
    for pattern in ("simulator/build/**/*.exe", "build/**/*.exe", "simulator/*.exe"):
        candidates.extend(project_root.glob(pattern))
    excluded = {"cmake.exe", "ffmpeg.exe", "ffprobe.exe", "make.exe", "mingw32-make.exe"}
    return next((p for p in candidates if p.name.lower() not in excluded), None)


def _window_rect(pid: int) -> tuple[int, int, int, int, int] | None:
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try: ctypes.windll.user32.SetProcessDPIAware()
        except Exception: pass
    found: list[tuple[int, int, int, int, int]] = []
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    @callback_type
    def callback(hwnd, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            # Capture the client area only. Window borders/title bars and DPI
            # virtualization otherwise shift ImageGrab to a neighboring window.
            rect = wintypes.RECT()
            point = wintypes.POINT(0, 0)
            if user32.GetClientRect(hwnd, ctypes.byref(rect)) and user32.ClientToScreen(hwnd, ctypes.byref(point)):
                user32.ShowWindow(hwnd, 5)
                user32.SetForegroundWindow(hwnd)
                found.append((int(hwnd), point.x, point.y, point.x + rect.right, point.y + rect.bottom))
        return True

    user32.EnumWindows(callback, 0)
    # Tuple is (HWND, left, top, right, bottom); score by client area, not
    # by subtracting the HWND integer from the coordinates.
    return max(found, key=lambda r: max(0, r[3] - r[1]) * max(0, r[4] - r[2])) if found else None


def _run_simulator(project_root: Path, target: Path, captures: list[dict[str, Any]], width: int, height: int) -> tuple[list[Path], str | None]:
    executable = _find_simulator(project_root)
    if executable is None:
        return [], "未找到已构建的 AiBuilder 模拟器；需要 UIBuilder 先生成并构建 simulator"
    target.mkdir(parents=True, exist_ok=True)
    # SDL desktop capture is unavailable in some Windows/DWM sessions.  The
    # generated harness can export the LVGL scene directly to this path while
    # retaining its real SDL window and event loop.
    internal_capture = target / "_internal_frame.bmp"
    try: internal_capture.unlink()
    except OSError: pass
    simulator_env = os.environ.copy()
    simulator_env["UAGENT_SIMULATOR_CAPTURE"] = str(internal_capture)
    process = subprocess.Popen([str(executable)], cwd=str(executable.parent), env=simulator_env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.time() + 12
        rect = None
        while time.time() < deadline and rect is None:
            if process.poll() is not None:
                return [], f"simulator_terminated: 模拟器在截图窗口出现前退出（exit={process.returncode}）"
            rect = _window_rect(process.pid)
            time.sleep(0.2)
        if rect is None:
            return [], "模拟器已启动，但未找到可截图窗口"
        screenshots = []
        hwnd, left, top, right, bottom = rect
        internal_protocol_seen = internal_capture.is_file()
        ctypes = __import__("ctypes")
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        # ctypes otherwise defaults pointer parameters to c_int.  On 64-bit
        # Windows that turns HWND_TOPMOST (-1) into 0x00000000FFFFFFFF, so
        # SetWindowPos quietly fails and a desktop capture is taken instead.
        user32.SetWindowPos.argtypes = [
            wintypes.HWND, wintypes.HWND,
            wintypes.INT, wintypes.INT, wintypes.INT, wintypes.INT,
            wintypes.UINT,
        ]
        user32.SetWindowPos.restype = wintypes.BOOL
        # Foreground activation can be denied by Windows' foreground-lock
        # policy. Temporarily raise the simulator above all windows so the
        # captured client rectangle contains only the LVGL surface.
        HWND_TOPMOST = wintypes.HWND(-1)
        HWND_NOTOPMOST = wintypes.HWND(-2)
        SWP_NOSIZE = 0x0001
        SWP_NOMOVE = 0x0002
        SWP_SHOWWINDOW = 0x0040
        if not user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                   SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW):
            raise OSError("SetWindowPos(HWND_TOPMOST) failed")
        time.sleep(0.8)
        def click_state(capture: dict[str, Any]) -> None:
            path = capture.get("path", [])
            nodes = capture.get("data", {}).get("nodes", [])
            frame = _frame(nodes, width, height)
            trigger = capture.get("sync_trigger") or {}
            identity = str(trigger.get("identity") or "")
            if identity:
                candidates = [n for n in nodes if str(n.get("identity") or "") == identity or
                              identity in {str((n.get("attrs") or {}).get(key) or "")
                                           for key in ("id", "data-openhmi-id", "data-figma-node-id")}]
            else:
                if not path:
                    return
                target_name = str(path[-1]).casefold()
                candidates = [n for n in nodes if n.get("tag") == "button"
                              and str(n.get("text", "")).strip().casefold() == target_name]
                if not candidates:
                    candidates = [n for n in nodes if n.get("tag") == "button"
                                  and target_name in str(n.get("className", "")).casefold()]
            if not candidates:
                return
            node = candidates[-1]; r = node.get("rect", {})
            fx, fy = (float(frame.get("x", 0)), float(frame.get("y", 0))) if frame else (0.0, 0.0)
            cx = int(round((float(r.get("x", 0)) - fx) * width / float(frame.get("width", width))))
            cy = int(round((float(r.get("y", 0)) - fy) * height / float(frame.get("height", height))))
            user32.SetCursorPos(left + cx, top + cy)
            user32.mouse_event(2, 0, 0, 0, 0)
            user32.mouse_event(4, 0, 0, 0, 0)
            time.sleep(.45)
        def capture_client(path: Path) -> None:
            """Capture the HWND itself, not whatever happens to occlude it."""
            import ctypes
            from ctypes import wintypes
            gdi32 = ctypes.windll.gdi32
            client = wintypes.RECT()
            user32.GetClientRect(hwnd, ctypes.byref(client))
            capture_w, capture_h = max(1, client.right), max(1, client.bottom)
            window_dc = user32.GetDC(hwnd)
            memory_dc = gdi32.CreateCompatibleDC(window_dc)
            bitmap = gdi32.CreateCompatibleBitmap(window_dc, capture_w, capture_h)
            previous = gdi32.SelectObject(memory_dc, bitmap)
            try:
                # PW_CLIENTONLY | PW_RENDERFULLCONTENT.  PrintWindow remains
                # correct when another application covers the simulator.
                if not user32.PrintWindow(hwnd, memory_dc, 0x00000003):
                    raise OSError("PrintWindow failed")
                class BITMAPINFOHEADER(ctypes.Structure):
                    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                                ("biClrImportant", wintypes.DWORD)]
                class BITMAPINFO(ctypes.Structure):
                    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]
                info = BITMAPINFO()
                info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
                info.bmiHeader.biWidth = capture_w
                info.bmiHeader.biHeight = -capture_h
                info.bmiHeader.biPlanes = 1
                info.bmiHeader.biBitCount = 32
                info.bmiHeader.biCompression = 0
                pixels = ctypes.create_string_buffer(capture_w * capture_h * 4)
                if not gdi32.GetDIBits(memory_dc, bitmap, 0, capture_h, pixels,
                                       ctypes.byref(info), 0):
                    raise OSError("GetDIBits failed")
                Image.frombuffer("RGB", (capture_w, capture_h), pixels, "raw", "BGRX", 0, 1).save(path)
            finally:
                gdi32.SelectObject(memory_dc, previous)
                gdi32.DeleteObject(bitmap)
                gdi32.DeleteDC(memory_dc)
                user32.ReleaseDC(hwnd, window_dc)
        def capture_with_retry(path: Path) -> bool:
            """Wait briefly for a painted frame and recover SDL blank prints."""
            attempts = 12
            for attempt in range(attempts):
                user32.BringWindowToTop(hwnd)
                user32.SetForegroundWindow(hwnd)
                time.sleep(0.05)
                try:
                    capture_client(path)
                except (OSError, ValueError):
                    pass
                if not _is_blank_frame(path):
                    return True
                # PrintWindow may succeed while SDL's backing surface is
                # still clear.  ImageGrab is a useful second path when the
                # window is visible, followed by a bounded repaint wait.
                try:
                    foreground_pid = wintypes.DWORD()
                    user32.GetWindowThreadProcessId(user32.GetForegroundWindow(),
                                                    ctypes.byref(foreground_pid))
                    if foreground_pid.value != process.pid:
                        # Another application can take focus between
                        # SetWindowPos and ImageGrab. Never score its pixels
                        # as if they came from the SDL simulator.
                        raise OSError("simulator window is not foreground")
                    # Capture the visible client rectangle while the SDL
                    # window remains topmost.  This is the reliable path for
                    # SDL surfaces that return a blank PrintWindow bitmap.
                    ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True).save(path)
                except (OSError, ValueError):
                    pass
                if not _is_blank_frame(path):
                    return True
                if attempt + 1 < attempts:
                    time.sleep(0.15)
            # Leave the last capture on disk; the validator will report its
            # visual mismatch instead of silently accepting an empty frame.
            return False

        previous_control: dict[str, Any] | None = None
        for index in range(max(1, len(captures))):
            if previous_control is not None:
                # Browser control samples are captured from a clean state.
                # Toggle the previous SDL control back before the next case.
                click_state(previous_control)
                previous_control = None
            if index > 0:
                click_state(captures[index])
                if (captures[index].get("sync_trigger") or {}).get("identity"):
                    previous_control = captures[index]
            path = target / f"{index + 1:02d}.png"
            # A failed current capture must never inherit a non-blank image
            # from an earlier validation run.
            try: path.unlink()
            except FileNotFoundError: pass
            if index > 0:
                try: internal_capture.unlink()
                except OSError: pass
            # Prefer the deterministic in-process LVGL snapshot.  The file is
            # recreated by the harness after the click/state settles.
            internal_deadline = time.time() + 3
            while time.time() < internal_deadline:
                if process.poll() is not None:
                    break
                try:
                    if internal_capture.stat().st_size > 54:
                        with Image.open(internal_capture) as probe:
                            probe.verify()
                        break
                except (FileNotFoundError, OSError, ValueError):
                    pass
                time.sleep(0.05)
            internal_ok = False
            if internal_capture.is_file():
                try:
                    with Image.open(internal_capture) as image:
                        image.convert("RGB").save(path)
                    internal_ok = not _is_blank_frame(path)
                except (OSError, ValueError):
                    pass
            if not internal_ok and internal_protocol_seen:
                return screenshots, (
                    f"simulator_internal_frame_invalid: SDL harness did not export a valid "
                    f"frame for state {index + 1}"
                )
            if not internal_ok:
                # Fall back to HWND/desktop capture for older harnesses.
                try: internal_capture.unlink()
                except OSError: pass
            if not internal_ok and not capture_with_retry(path):
                return screenshots, f"simulator_blank_frame: 截图在有限重试后仍为空或近空白（state={index + 1}）"
            screenshots.append(path)
        user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
        return screenshots, None
    finally:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()


def _visual_score(reference: Path, actual: Path, width: int, height: int) -> float:
    with Image.open(reference).convert("RGB") as ref, Image.open(actual).convert("RGB") as got:
        got = got.resize((width, height))
        ref = ref.resize((width, height))
        stat = ImageStat.Stat(ImageChops.difference(ref, got))
        return round(sum(stat.mean) / (3 * 255), 5)


def _is_blank_frame(path: Path, *, max_channel: int = 3,
                    max_ink_ratio: float = 0.001, max_mean: float = 1.5) -> bool:
    """Identify a capture that contains only the unrendered clear color.

    SDL windows can report success from ``PrintWindow`` before their surface
    has been painted.  This deliberately uses image content, not a project
    name or expected text, so it is safe for any generated simulator.
    """
    try:
        with Image.open(path).convert("RGB") as image:
            extrema = image.getextrema()
            mean = sum(ImageStat.Stat(image).mean) / 3
            pixels = list(image.getdata())
            ink = sum(1 for pixel in pixels if max(pixel) > max_channel)
            return max(channel[1] for channel in extrema) <= max_channel or (
                mean <= max_mean and ink / max(1, len(pixels)) <= max_ink_ratio
            )
    except (OSError, ValueError):
        return True


def validate_project(project_root: Path, layout_file: Path, snapshot_file: Path,
                     width: int, height: int, *, run_simulator: bool = True,
                     model: Any | None = None) -> dict[str, Any]:
    """Validate and persist a strict, machine-readable acceptance report."""
    validation_root = project_root / "openhmi" / "validation"
    validation_root.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    warnings: list[str] = []
    stages: list[dict[str, Any]] = []
    try:
        layout = json.loads(layout_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        layout = {}
        errors.append(f"DOM evidence unavailable: {exc}")
    captures = list(layout.get("screens", []))
    browser_ok = layout.get("status") == "browser-interaction" and bool(captures)
    stages.append({"stage": "browser-interaction", "passed": browser_ok, "states": len(captures)})
    if not browser_ok:
        errors.append("浏览器交互采集未完成，禁止标记完成")
    state_contract = layout.get("state_contract") if isinstance(layout.get("state_contract"), list) else []
    contract_ids = [str(item.get("state_id", "")) for item in state_contract if isinstance(item, dict)]
    contract_ok = len(state_contract) == len(captures) and len(contract_ids) == len(set(contract_ids))
    stages.append({"stage": "synchronized-state-contract", "passed": contract_ok,
                   "screens": len(captures), "contract_states": len(state_contract),
                   "controls": sum(len(item.get("controls", [])) for item in state_contract if isinstance(item, dict))})
    if not contract_ok:
        errors.append("Browser 状态缺少稳定的同步测试契约")
    # State samples are metadata variants of one Screen, never extra screens.
    sample_count = sum(len(c.get("state_samples", [])) for c in captures if isinstance(c, dict))
    stages.append({"stage": "state-samples", "passed": sample_count <= max(1, len(captures) * 8),
                   "screens": len(captures), "samples": sample_count, "max_per_screen": 8})
    region_blockers: list[str] = []
    manifests: list[dict[str, Any]] = []
    for capture in captures:
        data = capture.get("data", {}) if isinstance(capture, dict) else {}
        manifest = data.get("region_manifest") if isinstance(data, dict) else None
        if not isinstance(manifest, dict):
            manifest = region_manifest(data.get("nodes", []) if isinstance(data, dict) else [], discover_canvas(data))
        manifests.append(manifest)
        region_blockers.extend(region_gate(manifest))
        if isinstance(data, dict):
            data["overflow"] = overflow_evidence(data)
    region_stage = {"stage": "region-visual-metrics", "passed": not region_blockers,
                    "blockers": region_blockers,
                    "regions": ["header", "gauge", "control", "status", "background"],
                    "metrics": [], "policy": DEFAULT_REGION_VISUAL_POLICY}
    stages.append(region_stage)
    errors.extend(f"区域视觉验收失败：{item}" for item in region_blockers)

    try:
        root = ET.parse(snapshot_file).getroot()
        screens = root.findall(".//Screen")
    except (OSError, ET.ParseError) as exc:
        screens = []
        errors.append(f"Snapshot XML invalid: {exc}")
    project_files = sorted(project_root.glob("*.aicpro"))
    resolution_values: tuple[int, int] | None = None
    if project_files:
        try:
            project_xml = ET.parse(project_files[0]).getroot()
            resolution = project_xml.find("./resolution")
            resolution_values = (int(resolution.findtext("./width", "0")),
                                 int(resolution.findtext("./height", "0"))) if resolution is not None else None
        except (OSError, ET.ParseError, TypeError, ValueError):
            resolution_values = None
    resolution_ok = resolution_values == (width, height)
    stages.append({"stage": "resolution", "passed": resolution_ok,
                   "expected": [width, height],
                   "actual": list(resolution_values) if resolution_values else None})
    if not resolution_ok:
        errors.append("AiBuilder project resolution does not match the detected canvas")
    state_ok = bool(screens) and len(screens) == len(captures)
    stages.append({"stage": "state-to-screen", "passed": state_ok,
                   "browser_states": len(captures), "snapshot_screens": len(screens)})
    if not state_ok:
        errors.append("浏览器状态数与 AiBuilder Screen 数不一致")

    state_reports = []
    for index, screen in enumerate(screens):
        capture = captures[index] if index < len(captures) else {}
        nodes = capture.get("data", {}).get("nodes", [])
        frame = _frame(nodes, width, height)
        def in_frame(node: dict[str, Any]) -> bool:
            if not frame:
                return True
            rect = node.get("rect") or {}
            x, y = float(rect.get("x", 0)), float(rect.get("y", 0))
            w, h = float(rect.get("width", 0)), float(rect.get("height", 0))
            return x < frame["x"] + width and y < frame["y"] + height \
                and x + w > frame["x"] and y + h > frame["y"]
        def is_gauge(node: dict[str, Any]) -> bool:
            """Recognize SVG gauges even when the source omits ARIA metadata."""
            if str(node.get("role", "")).casefold() == "meter":
                return True
            if str(node.get("tag", "")).casefold() != "svg":
                return False
            rect = node.get("rect") or {}
            if float(rect.get("width", 0)) < 24 or float(rect.get("height", 0)) < 24:
                return False
            attrs = node.get("attrs") or {}
            svg = str(node.get("svg") or "")
            return bool(any(key in attrs for key in ("stroke-dasharray", "stroke-dashoffset"))
                        or re.search(r"stroke-dash(?:array|offset)|strokeLinecap|stroke-linecap", svg, re.I))
        expected_gauges = sum(1 for n in nodes if is_gauge(n))
        # A full-canvas SVG can contain several semantic instruments. Use the
        # browser semantic count instead of counting only its root <svg> node.
        try:
            browser_count = source_browser_plan_counts(model)["browser"].get("Gauge") if model is not None else None
            if isinstance(browser_count, int) and browser_count > expected_gauges:
                expected_gauges = browser_count
        except (TypeError, KeyError):
            pass
        actual_gauges = sum(1 for w in screen.findall(".//Widget") if w.get("type") == "12")
        expected_text = list(dict.fromkeys(str(n.get("text", "")).strip() for n in nodes
                                           if in_frame(n) and str(n.get("text", "")).strip() and len(str(n.get("text", "")).strip()) <= 120))
        actual_text = [str(w.findtext("./Attribute/text") or "").strip() for w in screen.findall(".//Widget")]
        missing_text = [text for text in expected_text if text not in actual_text]
        invisible = []
        overflow = []
        seen_names: set[str] = set()
        duplicates = []
        for widget in screen.findall(".//Widget"):
            name = widget.findtext("./Normal/name") or widget.get("name", "")
            if name in seen_names:
                duplicates.append(name)
            seen_names.add(name)
            text = widget.findtext("./Attribute/text") or ""
            state = widget.find("./Style/Part/State")
            if text and state is not None and _same_color(state.findtext("text-color"), state.findtext("bg-color")):
                invisible.append(text)
            position = widget.findtext("./Normal/postion") or "0,0"
            size = widget.findtext("./Normal/size") or "0,0"
            try:
                x, y = (int(float(v)) for v in position.split(",")); w, h = (int(float(v)) for v in size.split(","))
                if x < 0 or y < 0 or x + w > width or y + h > height:
                    overflow.append({"name": name, "box": [x, y, w, h]})
            except ValueError:
                warnings.append(f"无法解析控件坐标：{name}")
        expected_gauge_count = expected_gauges
        gauge_receipt = 0
        # Custom GaugeAdapter units paint source-backed gauges in custom.c and
        # therefore do not necessarily appear as snapshot type=12 widgets.
        # Treat the persisted audit units as a materialization receipt.
        audit_file = project_root / "ui_builder" / "custom" / "uagent-audit.json"
        try:
            audit = json.loads(audit_file.read_text(encoding="utf-8"))
            units = audit.get("units", []) if isinstance(audit, dict) else []
            gauge_receipt = sum(1 for unit in units if isinstance(unit, dict)
                                and unit.get("adapter") == "GaugeAdapter")
        except (OSError, ValueError, TypeError):
            gauge_receipt = 0
        effective_snapshot_gauges = max(actual_gauges, gauge_receipt)
        if model is not None:
            planned_gauges = source_browser_plan_counts(model)["plan"].get("Gauge")
            if isinstance(planned_gauges, int):
                expected_gauge_count = planned_gauges
        if expected_gauge_count != effective_snapshot_gauges:
            errors.append(f"状态 {index + 1} Gauge 数量不一致：DOM={expected_gauges}, snapshot={actual_gauges}")
        if missing_text:
            errors.append(f"状态 {index + 1} 缺少 {len(missing_text)} 个可见文本节点")
        if invisible:
            errors.append(f"状态 {index + 1} 存在前景/背景同色文字：{', '.join(invisible[:8])}")
        if duplicates:
            errors.append(f"状态 {index + 1} 存在重复控件身份")
        if overflow:
            errors.append(f"状态 {index + 1} 存在 {len(overflow)} 个越界控件")
        state_reports.append({"index": index, "path": capture.get("path", []),
                              "dom_nodes": len(nodes), "expected_gauges": expected_gauges,
                              "planned_gauges": expected_gauge_count,
                              "snapshot_gauges": actual_gauges, "invisible_text": invisible,
                              "gauge_receipt": gauge_receipt,
                              "missing_text": missing_text, "duplicates": duplicates, "overflow": overflow})

    if model is not None:
        counts = source_browser_plan_counts(model)
        count_errors: list[str] = []
        for effect_id, effect in model.visual_effects.items():
            if effect_id not in model.capability_plan:
                count_errors.append(f"{effect_id}: source visual effect has no capability decision")
        for category in ("Screen", "Gauge", "Arc", "Navigation", "Scroll", "VisualEffect"):
            source_count = counts["source"].get(category, 0)
            browser_count = counts["browser"].get(category)
            plan_count = counts["plan"].get(category, 0)
            if category != "VisualEffect" and isinstance(source_count, int) and isinstance(plan_count, int) and source_count > plan_count:
                count_errors.append(f"{category} source={source_count} > plan={plan_count}")
            if category != "VisualEffect" and isinstance(browser_count, int) and isinstance(plan_count, int) and browser_count > plan_count:
                count_errors.append(f"{category} browser={browser_count} > plan={plan_count}")
        count_errors.extend(str(item) for item in getattr(model.render_plan, "blockers", []))
        counts_stage = {
            category: {"source": counts["source"].get(category),
                       "browser": counts["browser"].get(category),
                       "plan": counts["plan"].get(category)}
            for category in ("Screen", "Gauge", "Arc", "Navigation", "Scroll", "VisualEffect")
        }
        stages.append({"stage": "source-browser-plan", "passed": not count_errors,
                       "counts": counts_stage, "blockers": count_errors})
        if count_errors:
            errors.extend(f"能力规划验收失败：{item}" for item in count_errors)

    visual_captures: list[dict[str, Any]] = []
    visual_manifests: list[dict[str, Any]] = []
    for index, capture in enumerate(captures):
        visual_captures.append(capture)
        visual_manifests.append(manifests[index] if index < len(manifests) else {"regions": {}})
        for sample in capture.get("state_samples", []):
            trigger = sample.get("trigger") if isinstance(sample, dict) else None
            if not isinstance(trigger, dict) or trigger.get("kind") != "click" or not sample.get("screenshot_png_base64"):
                continue
            sample_data = sample.get("data") if isinstance(sample.get("data"), dict) else capture.get("data", {})
            visual_captures.append({"path": capture.get("path", []), "data": sample_data,
                                    "screenshot_png_base64": sample.get("screenshot_png_base64"),
                                    "sync_trigger": trigger})
            visual_manifests.append(sample_data.get("region_manifest") or {"regions": {}})
    visual_layout = {**layout, "screens": visual_captures}
    references = _export_references(visual_layout, validation_root / "reference", width, height)
    simulator_images: list[Path] = []
    simulator_error = None
    if run_simulator:
        simulator_images, simulator_error = _run_simulator(project_root, validation_root / "simulator", visual_captures, width, height)
    visual_scores = [_visual_score(ref, got, width, height)
                     for ref, got in zip(references, simulator_images)]
    # Compare each semantic crop after both screenshot sets exist.  A local
    # failure is a blocker even when the global mean remains under 0.08.
    region_metrics = []
    if references and simulator_images:
        for index, (ref, got) in enumerate(zip(references, simulator_images)):
            manifest = visual_manifests[index] if index < len(visual_manifests) else {"regions": {}}
            metrics = region_visual_metrics(ref, got, manifest, DEFAULT_REGION_VISUAL_POLICY)
            region_metrics.append({"state": index, "regions": metrics})
            for name, item in metrics.items():
                if item.get("passed") is False:
                    region_blockers.append(f"state:{index + 1}:region:{name}:{item.get('reason')}")
                    errors.append(f"区域视觉验收失败：状态 {index + 1} {name} {item.get('reason')}")
    region_stage["metrics"] = region_metrics
    region_stage["blockers"] = region_blockers
    region_stage["passed"] = not region_blockers
    states_distinct = len(simulator_images) <= 1 or not all(
        _visual_score(simulator_images[0], image, width, height) < 0.001
        for image in simulator_images[1:]
    )
    if not states_distinct:
        errors.append("模拟器各状态截图完全相同；Tab 切换事件未生效，禁止完成")
    visual_ok = bool(references) and len(simulator_images) == len(references) and all(score <= 0.08 for score in visual_scores)
    stages.append({"stage": "simulator-visual-compare", "passed": visual_ok,
                   "reference_images": len(references), "simulator_images": len(simulator_images),
                   "difference_scores": visual_scores, "reason": simulator_error})
    if run_simulator and not visual_ok:
        errors.append(simulator_error or "模拟器截图与 Chrome 参考图差异超过阈值")

    # A deterministic confidence score makes repeated validation comparable
    # across providers and runs. The model may diagnose failures, but it can
    # never award this score or bypass a structural/interaction blocker.
    visual_similarity = (sum(max(0.0, 1.0 - score) for score in visual_scores) / len(visual_scores)
                         if visual_scores and len(visual_scores) == len(references) else 0.0)
    measured_regions = [item for state in region_metrics for item in state.get("regions", {}).values()
                        if item.get("status") == "measured" and isinstance(item.get("scores"), dict)]
    region_similarity = (sum(max(0.0, 1.0 - sum(float(item["scores"].get(key, 1.0))
                                                   for key in ("pixel", "edge", "coverage")) / 3.0)
                             for item in measured_regions) / len(measured_regions)
                         if measured_regions else 0.0)
    structural_stages = [stage for stage in stages if stage.get("stage") not in
                         {"simulator-visual-compare", "region-visual-metrics"}]
    structural_score = (sum(1.0 for stage in structural_stages if stage.get("passed")) /
                        len(structural_stages) if structural_stages else 0.0)
    interaction_score = 1.0 if browser_ok and state_ok and states_distinct else 0.0
    confidence = round(0.50 * visual_similarity + 0.20 * region_similarity +
                       0.15 * structural_score + 0.15 * interaction_score, 5)
    threshold = 0.90
    hard_blockers = list(dict.fromkeys(errors))
    deterministic_passed = confidence >= threshold and not hard_blockers
    deterministic_gate = {
        "threshold": threshold, "confidence": confidence, "passed": deterministic_passed,
        "components": {"visual": round(visual_similarity, 5),
                       "regions": round(region_similarity, 5),
                       "structure": round(structural_score, 5),
                       "interaction": round(interaction_score, 5)},
        "weights": {"visual": 0.50, "regions": 0.20, "structure": 0.15, "interaction": 0.15},
        "hard_blockers": hard_blockers,
    }
    stages.append({"stage": "deterministic-confidence", **deterministic_gate})

    report = {"schema": "uagent.acceptance/v2", "status": "passed" if deterministic_passed else "failed",
              "completed": deterministic_passed, "strict": True, "stages": stages,
              "states": state_reports, "errors": errors, "warnings": warnings,
              "deterministic_gate": deterministic_gate,
              "counts": (source_browser_plan_counts(model) if model is not None else None),
              "policy": {"position_tolerance_px": 2, "color_tolerance": 3,
                         "visual_difference_max": 0.08,
                         "deterministic_confidence_min": threshold,
                         "region_visual": DEFAULT_REGION_VISUAL_POLICY}}
    report_file = validation_root / "report.json"
    report_file.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    report["report_file"] = str(report_file)
    return report
