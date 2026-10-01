"""Run browser/npm and AiBuilder/Windows-SDL validation in an isolated copy.

This is deliberately an orchestration entry point: browser evidence and the
strict validator remain the single sources of truth for their respective
comparisons.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from core.browser_layout import capture_layout, close_browser_runtime
from core.capability import plan_capabilities
from core.model_provider import (ModelSettings, analyze_visual_regression,
                                 apply_agent_suggestions, load_settings)
from core.validation import validate_project
from core.pipeline import analyze
from generator.compiler import compile_lvgl, write_aibuilder_custom


_EXCLUDED = {".git", "node_modules", "dist", "cross-validation.json", "openhmi"}


def _discover_c_compiler() -> Path | None:
    """Find a host C compiler without persisting a machine-specific path."""
    explicit = os.environ.get("UAGENT_CC") or os.environ.get("CC")
    if explicit and Path(explicit).is_file():
        return Path(explicit).resolve()
    on_path = shutil.which("gcc") or shutil.which("clang")
    if on_path:
        return Path(on_path).resolve()
    roots = [Path(value) for value in (
        os.environ.get("UAGENT_UIBUILDER_ROOT", ""),
        r"D:\UIBuilder", r"C:\UIBuilder",
    ) if value]
    return next((root / "tool" / "mingw" / "bin" / "gcc.exe" for root in roots
                 if (root / "tool" / "mingw" / "bin" / "gcc.exe").is_file()), None)


def _build_snapshot_simulator(project_root: Path) -> tuple[Path | None, str | None]:
    """Configure and rebuild the generated SDL harness for the current round."""
    simulator = project_root / "simulator"
    build = simulator / "build"
    cmake = os.environ.get("UAGENT_CMAKE", "cmake")
    compiler = _discover_c_compiler()
    build_env = os.environ.copy()
    configure = [cmake, "-S", str(simulator), "-B", str(build)]
    if compiler is not None:
        build_env["PATH"] = str(compiler.parent) + os.pathsep + build_env.get("PATH", "")
        configure.append(f"-DCMAKE_C_COMPILER={compiler}")
    # Reconfigure every round. An incomplete tree can retain CMakeCache.txt
    # while losing build.ninja or its Makefile; the cache alone is not proof
    # that the generated SDL project is buildable.
    commands = [configure]
    commands.append([cmake, "--build", str(build), "--config", "Release"])
    log_parts = []
    try:
        for command in commands:
            result = subprocess.run(command, cwd=project_root, env=build_env, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                    errors="replace", timeout=180)
            log_parts.append(result.stdout or "")
            if result.returncode:
                message = "\n".join(log_parts)[-6000:]
                (simulator / "uagent-build.log").write_text(message, encoding="utf-8")
                return None, f"simulator_build_failed: {message[-1200:]}"
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"simulator_build_failed: {exc}"
    message = "\n".join(log_parts)
    (simulator / "uagent-build.log").write_text(message, encoding="utf-8")
    candidates = [build / "main.exe", build / "Release" / "main.exe", build / "main"]
    executable = next((path for path in candidates if path.is_file()), None)
    return (executable, None) if executable else (None, "simulator_build_failed: executable missing after build")


def _round_comparison(validation: dict[str, Any]) -> dict[str, str] | None:
    """Embed one auditable Browser/SDL pair for the desktop result dialog."""
    report_file = validation.get("report_file")
    if not report_file:
        return None
    evidence_root = Path(str(report_file)).parent
    references = sorted((evidence_root / "reference").glob("*.png"))
    simulators = sorted((evidence_root / "simulator").glob("*.png"))
    if not references or not simulators:
        return None

    def data_uri(path: Path) -> str:
        return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")

    return {
        "browser_name": references[0].name,
        "simulator_name": simulators[0].name,
        "browser_image": data_uri(references[0]),
        "simulator_image": data_uri(simulators[0]),
    }


def _project_resolution(project: Path, browser: dict[str, Any]) -> tuple[int, int]:
    try:
        root = ET.parse(project).getroot()
        resolution = root.find("./resolution")
        if resolution is not None:
            return int(resolution.findtext("width", "0")), int(resolution.findtext("height", "0"))
    except (OSError, ET.ParseError, TypeError, ValueError):
        pass
    viewport = (browser.get("data") or {}).get("viewport") or {}
    return int(viewport.get("width", 1024)), int(viewport.get("height", 600))


def _copy_source(source: Path, staging: Path) -> None:
    def ignore(_directory: str, names: list[str]) -> set[str]:
        return {name for name in names if name in _EXCLUDED or name.startswith("uagent_build_")}
    shutil.copytree(source, staging, dirs_exist_ok=True, ignore=ignore)


def generate_snapshot_harness(project_root: Path, snapshot: Path) -> dict[str, Path]:
    """Generate a simulator entry from the current snapshot, never UIBuilder C."""
    root = ET.parse(snapshot).getroot()
    out = project_root / "simulator"; out.mkdir(parents=True, exist_ok=True)
    # custom.c is normally compiled by AiBuilder beside its generated screen
    # objects.  The standalone SDL harness has no such generated runtime, so
    # provide the minimal generic screen-manager contract it references.
    custom_source = project_root / "ui_builder" / "custom" / "custom.c"
    custom_text = custom_source.read_text(encoding="utf-8") if custom_source.is_file() else ""
    custom_header = custom_source.with_suffix('.h')
    runtime_owns_scene = custom_header.is_file() and '#define UAGENT_RUNTIME_OWNS_SCENE 1' in custom_header.read_text(encoding='utf-8')
    if runtime_owns_scene:
        # The runtime compiler owns the full object tree. Replaying the
        # legacy snapshot as well duplicates pages, controls and paint.
        root = ET.Element('ailv-app')
    screen_names = sorted(set(re.findall(r"\b(screen_[A-Za-z0-9_]+)_get\s*\(", custom_text)))
    screen_declarations = "\n".join(f"typedef struct {{ lv_obj_t *obj; }} {name}_t;" for name in screen_names)
    manager_fields = "\n".join(f"    {name}_t {name};" for name in screen_names)
    accessors = "\n".join(
        f"{name}_t *{name}_get(ui_manager_t *ui) {{ if(!ui->{name}.obj) ui->{name}.obj = lv_obj_create(NULL); return &ui->{name}; }}\n"
        f"void {name}_create(ui_manager_t *ui) {{ (void){name}_get(ui); }}"
        for name in screen_names
    )
    first_screen = screen_names[0] if screen_names else None
    screen_header = project_root / "ui_builder" / "ui_objects.h"
    screen_header.parent.mkdir(parents=True, exist_ok=True)
    screen_header.write_text(
        "#ifndef UAGENT_SIMULATOR_UI_OBJECTS_H\n#define UAGENT_SIMULATOR_UI_OBJECTS_H\n"
        '#include "lvgl.h"\n#include "aic_ui.h"\n'
        "#if defined(LVGL_VERSION_MAJOR) && LVGL_VERSION_MAJOR >= 9\n#define lv_img_class lv_image_class\n#endif\n" +
        screen_declarations + "\n"
        "typedef struct {\n" + manager_fields + "\n} ui_manager_t;\n"
        "extern ui_manager_t ui_manager;\n"
        "void ui_objects_bind_primary(lv_obj_t *screen);\n" +
        "\n".join(f"{name}_t *{name}_get(ui_manager_t *ui);\nvoid {name}_create(ui_manager_t *ui);" for name in screen_names) +
        "\n#endif\n", encoding="utf-8", newline="\n")
    (out / "ui_objects.c").write_text(
        '#include "ui_objects.h"\nui_manager_t ui_manager;\n'
        "void ui_objects_bind_primary(lv_obj_t *screen) {\n" +
        (f"    ui_manager.{first_screen}.obj = screen;\n" if first_screen else "    (void)screen;\n") +
        "}\n" + accessors + "\n", encoding="utf-8", newline="\n")
    (out / "custom_harness.c").write_text(
        '#include "aic_ui.h"\n#include "../ui_builder/custom/custom.c"\n',
        encoding="utf-8", newline="\n")
    supported = {"1", "2", "5", "6", "8", "12", "28"}
    widgets: list[dict[str, Any]] = []
    # Snapshot harnesses must follow the exported canvas.  Prefer an explicit
    # Page/root size, then the largest root-like widget; only use the browser
    # sized default when the snapshot carries no geometry at all.
    canvas_w, canvas_h = 1024, 600
    canvas_candidates: list[tuple[int, int]] = []
    for element in root.iter():
        for keys in (("width", "height"),):
            try:
                if element.get(keys[0]) and element.get(keys[1]):
                    canvas_candidates.append((int(float(element.get(keys[0]))), int(float(element.get(keys[1])))))
            except (TypeError, ValueError):
                pass
    for node in root.iter("Widget"):
        typ = node.get("type", "")
        if typ not in supported:
            raise ValueError(f"snapshot harness unsupported widget type: {typ}")
        normal = node.find("Normal"); attr = node.find("Attribute")
        pos = (normal.findtext("postion", "0,0") if normal is not None else "0,0").split(",")
        size = (normal.findtext("size", "1,1") if normal is not None else "1,1").split(",")
        w, h = int(float(size[0])), int(float(size[1]))
        if w <= 0 or h <= 0:
            continue
        if typ == "28" and w >= canvas_w and h >= canvas_h:
            canvas_w, canvas_h = w, h
        if typ == "28":
            canvas_candidates.append((w, h))
        styles: dict[str, dict[str, str]] = {}
        for part in node.findall("./Style/Part"):
            state = part.find("./State[@name='Default']")
            if state is None:
                state = part.find("State")
            if state is not None:
                styles[str(part.get("name", "Main"))] = {child.tag: child.text or "" for child in state}
        widgets.append({
            "id": node.get("id", ""), "parent": node.get("parent-id", ""), "type": typ,
            "x": int(float(pos[0])), "y": int(float(pos[1])), "w": w, "h": h,
            "attr": {child.tag: child.text or "" for child in attr} if attr is not None else {},
            "styles": styles,
        })

    def cint(value: str, default: int = 0) -> int:
        try: return int(float(value))
        except (TypeError, ValueError): return default

    def color(value: str) -> str:
        match = str(value or "").lstrip("#")
        return f"lv_color_hex(0x{match})" if len(match) in {6, 8} else "lv_color_hex(0x000000)"

    if canvas_candidates:
        canvas_w, canvas_h = max(canvas_candidates, key=lambda pair: pair[0] * pair[1])
    projects = list(project_root.glob('*.aicpro'))
    if projects:
        resolution = ET.parse(projects[0]).getroot().find('resolution')
        if resolution is not None:
            canvas_w = int(resolution.findtext('width', str(canvas_w)))
            canvas_h = int(resolution.findtext('height', str(canvas_h)))
    lines = ['#include "uagent_snapshot.h"', '#include "lvgl.h"', '#include "aic_ui.h"', '']
    lines += ['void uagent_snapshot_build(lv_obj_t * parent) {']
    variables: dict[str, str] = {}
    for i, widget in enumerate(widgets):
        typ, x, y, w, h = (widget[k] for k in ("type", "x", "y", "w", "h"))
        attr, styles = widget["attr"], widget["styles"]
        ctor = {"1":"lv_label_create", "2":"lv_btn_create", "5":"lv_img_create", "6":"lv_bar_create", "8":"lv_slider_create", "12":"lv_arc_create", "28":"lv_obj_create"}[typ]
        var = f"snapshot_{i}"
        parent_var = variables.get(widget["parent"], "parent")
        variables[widget["id"]] = var
        lines += [f"    lv_obj_t * {var} = {ctor}({parent_var});", f"    lv_obj_set_pos({var}, {x}, {y});", f"    lv_obj_set_size({var}, {w}, {h});"]
        lines += [f"    lv_obj_set_scrollbar_mode({var}, LV_SCROLLBAR_MODE_OFF);",
                  f"    lv_obj_clear_flag({var}, LV_OBJ_FLAG_SCROLLABLE);"]
        if typ in {"2", "28"}:
            lines += [f"    lv_obj_set_style_pad_all({var}, 0, LV_PART_MAIN);",
                      f"    lv_obj_set_style_border_width({var}, 0, LV_PART_MAIN);"]
        text = attr.get("text", "")
        if text and typ == "1": lines.append(f"    lv_label_set_text({var}, {json.dumps(text, ensure_ascii=False)});")
        if typ == "2":
            lines.append(f"    lv_obj_t * {var}_label = lv_label_create({var}); lv_label_set_text({var}_label, {json.dumps(text, ensure_ascii=False)}); lv_obj_center({var}_label);")
        if typ == "5" and attr.get("src"):
            lines.append(f"    lv_img_set_src({var}, LVGL_IMAGE_PATH({attr['src']}));")
        minimum, maximum, value = cint(attr.get("min-value", "0")), cint(attr.get("max-value", "100"), 100), cint(attr.get("value", "0"))
        if typ == "8":
            lines += [f"    lv_slider_set_range({var}, {minimum}, {maximum});", f"    lv_slider_set_value({var}, {value}, LV_ANIM_OFF);"]
        if typ == "6":
            lines += [f"    lv_bar_set_range({var}, {minimum}, {maximum});", f"    lv_bar_set_value({var}, {value}, LV_ANIM_OFF);"]
        if typ == "12":
            lines += [f"    lv_arc_set_range({var}, {minimum}, {maximum});", f"    lv_arc_set_value({var}, {value});",
                      f"    lv_arc_set_bg_angles({var}, {cint(attr.get('bg-angle-start', '135'), 135)}, {cint(attr.get('bg-angle-end', '405'), 405)});",
                      f"    lv_arc_set_angles({var}, {cint(attr.get('angle-start', '135'), 135)}, {cint(attr.get('angle-end', '405'), 405)});"]
        for part_name, selector in (("Main", "LV_PART_MAIN"), ("Indicator", "LV_PART_INDICATOR"), ("Knob", "LV_PART_KNOB")):
            style = styles.get(part_name, {})
            for key, function in (("bg-color", "bg_color"), ("text-color", "text_color"), ("border-color", "border_color"), ("arc-color", "arc_color")):
                if style.get(key): lines.append(f"    lv_obj_set_style_{function}({var}, {color(style[key])}, {selector});")
            for key, function in (("bg-opa", "bg_opa"), ("border-width", "border_width"), ("radius", "radius"), ("arc-width", "arc_width")):
                if style.get(key) not in (None, ""): lines.append(f"    lv_obj_set_style_{function}({var}, {cint(style[key])}, {selector});")
            if part_name == "Main" and style.get("text-font-size"):
                requested = cint(style["text-font-size"], 14)
                nearest = min((8, 10, 12, 14, 16, 20), key=lambda size: abs(size - requested))
                font = "lv_font_simsun_16_cjk" if any(ord(ch) > 127 for ch in text) else f"lv_font_montserrat_{nearest}"
                lines.append(f"    lv_obj_set_style_text_font({var}, &{font}, {selector});")
    lines.append('}')
    header = out / "uagent_snapshot.h"; source = out / "uagent_snapshot.c"
    aic_ui = out / "aic_ui.h"
    header.write_text('#ifndef UAGENT_SNAPSHOT_H\n#define UAGENT_SNAPSHOT_H\n#include "lvgl.h"\nvoid uagent_snapshot_build(lv_obj_t * parent);\n#endif\n', encoding="utf-8")
    source.write_text("\n".join(lines) + "\n", encoding="utf-8")
    aic_ui.write_text("""#ifndef UAGENT_AIC_UI_H
#define UAGENT_AIC_UI_H
/* Minimal compatibility surface for the standalone snapshot harness. */
#ifndef LVGL_STORAGE_PATH
#define LVGL_STORAGE_PATH ""
#endif
#ifndef UAGENT_STRINGIZE
#define UAGENT_STRINGIZE_INNER(value) #value
#define UAGENT_STRINGIZE(value) UAGENT_STRINGIZE_INNER(value)
#endif
#ifndef LVGL_IMAGE_PATH
#define LVGL_IMAGE_PATH(path) LVGL_DIR UAGENT_STRINGIZE(path)
#endif
#ifndef LVGL_DIR
#define LVGL_DIR ""
#endif
#endif
""", encoding="utf-8", newline="\n")
    # Keep the standalone snapshot executable independent of FreeType while
    # preserving simulator/lv_conf.h for AiBuilder's full ui_builder build.
    project_conf = out / "lv_conf.h"
    harness_conf = out / "lv_conf_uagent.h"
    if project_conf.is_file():
        conf_text = project_conf.read_text(encoding="utf-8", errors="ignore")
        conf_text = re.sub(r"^(\s*#\s*define\s+LV_USE_FREETYPE)\s+1\b", r"\1 0", conf_text, flags=re.M)
        conf_text = re.sub(r"^(\s*#\s*define\s+LV_USE_FFMPEG)\s+1\b", r"\1 0", conf_text, flags=re.M)
        harness_conf.write_text(conf_text, encoding="utf-8", newline="\n")
    (out / "main.c").write_text(f'''#define _DEFAULT_SOURCE
#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include <unistd.h>
#ifdef _WIN32
#include <windows.h>
#endif
#include "lvgl/lvgl.h"
#include LV_SDL_INCLUDE_PATH
#include "uagent_snapshot.h"
#include "ui_objects.h"
#include "custom.h"
#undef main
static void uagent_test_pump(unsigned milliseconds) {{
    unsigned start=SDL_GetTicks();
    do {{lv_timer_handler();SDL_Delay(5);}} while(SDL_GetTicks()-start<milliseconds);
}}
static void uagent_test_pointer(int x,int y,int end_x,int end_y) {{
    /* This harness creates exactly one SDL window. Inject into its event
       queue so hit-testing, scrolling and slider dragging are exercised. */
    SDL_Event event={{0}};event.type=SDL_MOUSEBUTTONDOWN;
    event.button.windowID=SDL_GetWindowID(SDL_GetWindowFromID(1));
    event.button.button=SDL_BUTTON_LEFT;event.button.x=x;event.button.y=y;
    SDL_PushEvent(&event);uagent_test_pump(60);
    for(int step=1;step<=8;step++) {{
        event.type=SDL_MOUSEMOTION;event.motion.windowID=SDL_GetWindowID(SDL_GetWindowFromID(1));
        event.motion.state=SDL_BUTTON_LMASK;event.motion.x=x+(end_x-x)*step/8;event.motion.y=y+(end_y-y)*step/8;
        SDL_PushEvent(&event);uagent_test_pump(20);
    }}
    event.type=SDL_MOUSEBUTTONUP;event.button.windowID=SDL_GetWindowID(SDL_GetWindowFromID(1));
    event.button.button=SDL_BUTTON_LEFT;event.button.x=end_x;event.button.y=end_y;
    SDL_PushEvent(&event);uagent_test_pump(100);
}}
static int uagent_write_snapshot(lv_display_t * display) {{
    const char * path = getenv("UAGENT_SIMULATOR_CAPTURE");
    if(!path || !*path) return 0;
    lv_draw_buf_t * buf = lv_display_get_buf_active(display);
    if(!buf || !buf->data) {{ fprintf(stderr, "uagent snapshot: display buffer unavailable\\n"); return 0; }}
    char temporary[2048];snprintf(temporary,sizeof(temporary),"%s.tmp",path);
    FILE * out = fopen(temporary, "wb");
    if(!out) {{ fprintf(stderr, "uagent snapshot: fopen failed for %s\\n", path); return 0; }}
    const uint32_t row = (uint32_t)buf->header.w * 4U;
    const uint32_t image_size = row * (uint32_t)buf->header.h;
    const uint32_t file_size = 54U + image_size;
    uint8_t header[54] = {{0}};
    header[0] = 'B'; header[1] = 'M';
    *(uint32_t *)(header + 2) = file_size;
    *(uint32_t *)(header + 10) = 54U;
    *(uint32_t *)(header + 14) = 40U;
    *(int32_t *)(header + 18) = (int32_t)buf->header.w;
    *(int32_t *)(header + 22) = -(int32_t)buf->header.h;
    *(uint16_t *)(header + 26) = 1; *(uint16_t *)(header + 28) = 32;
    *(uint32_t *)(header + 30) = 0; *(uint32_t *)(header + 34) = image_size;
    fwrite(header, 1, sizeof(header), out);
    for(uint32_t y = 0; y < (uint32_t)buf->header.h; y++)
        fwrite(buf->data + y * buf->header.stride, 1, row, out);
    fclose(out);
#ifdef _WIN32
    if(!MoveFileExA(temporary,path,MOVEFILE_REPLACE_EXISTING|MOVEFILE_WRITE_THROUGH)) return 0;
#else
    if(rename(temporary,path)) return 0;
#endif
    fprintf(stderr, "uagent snapshot: wrote %s (%ux%u)\\n", path, buf->header.w, buf->header.h);
    return 1;
}}
int main(void) {{
    lv_init();
    lv_group_set_default(lv_group_create());
    lv_display_t * display = lv_sdl_window_create({canvas_w}, {canvas_h});
    lv_indev_t * mouse = lv_sdl_mouse_create();
    lv_indev_set_group(mouse, lv_group_get_default());
    lv_indev_set_display(mouse, display);
    lv_display_set_default(display);
    lv_obj_t * screen = lv_obj_create(NULL);
    lv_scr_load(screen);
    ui_objects_bind_primary(screen);
#ifndef UAGENT_RUNTIME_OWNS_SCENE
    uagent_snapshot_build(screen);
#endif
    /* Leave a tiny startup marker so capture failures distinguish an
       unentered event loop from a snapshot API failure. */
    const char * capture_path = getenv("UAGENT_SIMULATOR_CAPTURE");
    if(capture_path && *capture_path) {{
        FILE * marker = fopen(capture_path, "wb");
        if(marker) {{ fputs("UAGENT_PENDING", marker); fclose(marker); }}
        else fprintf(stderr, "uagent snapshot: startup fopen failed for %s\\n", capture_path);
    }}
    custom_init();
#ifdef UAGENT_RUNTIME_OWNS_SCENE
    const char * action_list = getenv("UAGENT_TEST_ACTIONS");
    if(action_list && *action_list) {{
        char * actions = strdup(action_list);
        char * cursor = actions;
        while(cursor && *cursor) {{
            char * next = strchr(cursor,';'); if(next) *next++=0;
            char * value = strchr(cursor,'|'); if(value) *value++=0;
            lv_obj_update_layout(lv_screen_active());
            if(!strcmp(cursor,"@click")||!strcmp(cursor,"@drag")) {{
                int x=0,y=0,end_x=0,end_y=0;int count=value?sscanf(value,"%d,%d,%d,%d",&x,&y,&end_x,&end_y):0;
                if(count==2) {{end_x=x;end_y=y;}}
                if(count==2||count==4) uagent_test_pointer(x,y,end_x,end_y);
                else fprintf(stderr,"invalid pointer action\\n");
            }} else if(!uagent_runtime_action(cursor,value?atof(value):0)) fprintf(stderr,"unknown action: %s\\n",cursor);
            lv_timer_handler(); cursor=next;
        }}
        free(actions);
    }}
    const char *state_file=getenv("UAGENT_TEST_STATE_FILE");
    if(state_file && *state_file) uagent_runtime_dump(state_file);
#endif
    if(capture_path && *capture_path) {{
        FILE * ready = fopen(capture_path, "wb");
        if(ready) {{ fputs("UAGENT_READY", ready); fclose(ready); }}
    }}
    unsigned frame_count = 0;
    while(1) {{
        lv_timer_handler();
        /* The validator removes the previous file between interaction states. */
        capture_path = getenv("UAGENT_SIMULATOR_CAPTURE");
        if(capture_path && frame_count++ >= 5) {{
            frame_count = 0;
            uagent_write_snapshot(display);
        }}
        usleep(5000);
    }}
    return 0;
}}
''', encoding="utf-8")
    (out / "CMakeLists.txt").write_text('''cmake_minimum_required(VERSION 3.16)
project(uagent_snapshot C)
set(CMAKE_C_STANDARD 11)
file(GLOB_RECURSE LVGL_SRC "lvgl/src/*.c")
file(GLOB_RECURSE AIC_WIDGETS_SRC "aic_widgets/*.c")
add_executable(main main.c ui_objects.c uagent_snapshot.c custom_harness.c ${LVGL_SRC} ${AIC_WIDGETS_SRC})
target_include_directories(main PRIVATE . lvgl aic_widgets ../ui_builder ../ui_builder/custom)
# LVGL stringizes LV_CONF_PATH itself. Passing an already quoted absolute path
# makes GCC include a literal \"path\" token and fail with Invalid argument.
target_compile_definitions(main PRIVATE LV_CONF_PATH=lv_conf_uagent.h LVGL_STORAGE_PATH="${CMAKE_CURRENT_SOURCE_DIR}/../resources/image/" LVGL_DIR="L:${CMAKE_CURRENT_SOURCE_DIR}/../resources/image/")
target_link_libraries(main PRIVATE SDL2)
set_target_properties(main PROPERTIES RUNTIME_OUTPUT_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}/build")
file(COPY "${CMAKE_CURRENT_SOURCE_DIR}/lib/" DESTINATION "${CMAKE_CURRENT_SOURCE_DIR}/build")
''', encoding="utf-8")
    return {"header": header, "source": source, "aic_ui": aic_ui, "lv_conf": harness_conf,
            "main": out / "main.c", "cmake": out / "CMakeLists.txt"}


def _json_summary(source: Path, staging: Path | None, output: Path,
                  browser: dict[str, Any], validation: dict[str, Any] | None,
                  generated: list[str], rounds: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "source": str(source),
        "staging_used": str(staging) if staging else None,
        "output": str(output),
        "browser": {
            "status": browser.get("status", "unavailable"),
            "screens": len(browser.get("screens", [])) if isinstance(browser, dict) else 0,
            "error": browser.get("reason") or browser.get("error") if isinstance(browser, dict) else None,
        },
        "validation": {
            "status": (validation or {}).get("status", "failed"),
            "completed": bool((validation or {}).get("completed", False)),
            "stages": (validation or {}).get("stages", []),
            "errors": (validation or {}).get("errors", ["validation did not run"]),
            "deterministic_gate": (validation or {}).get("deterministic_gate"),
        },
        "rounds": rounds or [],
        "export_ready": bool((validation or {}).get("completed", False) and
                             ((validation or {}).get("deterministic_gate") or {}).get("passed", False)),
        "generated_files": generated,
    }


def cross_validate(source: str | Path, output: str | Path, *, simulator_exe: str | Path | None = None,
                   model: str = "gpt-5.5", settings: ModelSettings | None = None,
                   api_key: str | None = None, max_rounds: int = 5,
                   visible_browser: bool = False, confidence_threshold: float = .90) -> dict[str, Any]:
    source_path, output_path = Path(source).resolve(), Path(output).resolve()
    if not source_path.is_dir():
        raise ValueError(f"source is not a directory: {source_path}")
    output_path.mkdir(parents=True, exist_ok=True)
    browser: dict[str, Any] = {"status": "unavailable", "screens": []}
    validation: dict[str, Any] | None = None
    generated: list[str] = []
    rounds: list[dict[str, Any]] = []
    runtime_id: str | None = None
    resolved_api_key = api_key or (settings.api_key() if settings is not None else None)
    max_rounds = max(1, min(int(max_rounds), 8))
    if abs(float(confidence_threshold) - .90) > 1e-9:
        raise ValueError("deterministic confidence threshold is fixed at 0.90")
    # Keep the isolated browser source under the output tree. On Windows the
    # system TEMP path can use an 8.3 alias (for example JASONG~1); Vite/libuv
    # then compares it with the expanded path and aborts its file watcher.
    runtime_root = output_path / "openhmi" / "runtime"
    runtime_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="uagent-cross-", dir=runtime_root) as temp_name:
        staging = Path(temp_name) / "source"
        _copy_source(source_path, staging)
        previous_simulator = os.environ.get("UAGENT_SIMULATOR_EXE")
        if simulator_exe:
            os.environ["UAGENT_SIMULATOR_EXE"] = str(Path(simulator_exe).resolve())
        else:
            os.environ.pop("UAGENT_SIMULATOR_EXE", None)
        try:
            captured = capture_layout(staging, viewport=(1024, 600), visible=visible_browser,
                                      keep_open=visible_browser)
            browser = captured if isinstance(captured, dict) else {"status": "unavailable", "screens": [], "reason": "capture returned no evidence"}
            runtime_id = (browser.get("browser_runtime") or {}).get("id")
        except Exception as exc:
            browser = {"status": "browser-interaction-failed", "screens": [], "reason": str(exc)}
        # Make the evidence available to the scaffold as a verified fallback;
        # this is inside staging and cannot mutate the user's source.
        if browser.get("screens"):
            (staging / "browser-layout-v2.json").write_text(json.dumps(browser, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            model_obj = analyze(staging)
            # Keep the capture that was just validated as the single source of
            # browser geometry for planning and compilation.
            try:
                model_obj.browser_evidence = browser
                plan_capabilities(model_obj, browser)
            except (AttributeError, TypeError):
                # Keep lightweight orchestration tests/mocks compatible; real
                # ReactProjectModel instances always support both operations.
                pass
            for round_index in range(max_rounds):
                bundle = compile_lvgl(model_obj)
                paths = write_aibuilder_custom(bundle, output_path / "ui_builder" / "custom",
                                               model=model_obj, include_runtime=True)
                snapshots = list(output_path.glob("*.snapshot"))
                if snapshots:
                    paths.update(generate_snapshot_harness(output_path, snapshots[0]))
                generated = sorted(str(Path(path).resolve()) for path in paths.values())
                built_simulator, build_error = _build_snapshot_simulator(output_path)
                if build_error or not built_simulator or not paths.get("project") or not paths.get("snapshot") or not paths.get("browser_layout"):
                    validation = {"status": "failed", "completed": False, "stages": [],
                                  "errors": [build_error or "validation inputs missing"],
                                  "deterministic_gate": {"threshold": .90, "confidence": 0.0,
                                                         "passed": False, "hard_blockers": [build_error or "validation inputs missing"]}}
                else:
                    os.environ["UAGENT_SIMULATOR_EXE"] = str(built_simulator.resolve())
                    width, height = _project_resolution(Path(paths["project"]), browser)
                    validation = validate_project(output_path, Path(paths["browser_layout"]),
                                                  Path(paths["snapshot"]), width, height,
                                                  run_simulator=True, model=model_obj)
                gate = validation.get("deterministic_gate") or {}
                round_result: dict[str, Any] = {
                    "round": round_index + 1, "confidence": float(gate.get("confidence", 0) or 0),
                    "threshold": .90, "passed": bool(gate.get("passed", False)),
                    "errors": validation.get("errors", []), "agent": {"status": "not_run"},
                }
                comparison = _round_comparison(validation)
                if comparison:
                    round_result["comparison"] = comparison
                rounds.append(round_result)
                if round_result["passed"]:
                    break
                if settings is None or not settings.agent_is_enabled or not resolved_api_key:
                    round_result["stop_reason"] = "agent_not_configured"
                    break
                candidates = [{"target_id": key, "source_id": value.source_id,
                               "support": value.support, "adapter": value.adapter,
                               "confidence": value.confidence, "evidence_ids": value.evidence_ids}
                              for key, value in model_obj.capability_plan.items()]
                advice = analyze_visual_regression(settings, validation, candidates, resolved_api_key)
                round_result["agent"] = advice
                applied = apply_agent_suggestions(model_obj, advice.get("suggestions", []), browser,
                                                  browser_verified=browser.get("status") == "browser-interaction")
                round_result["repair"] = applied
                if not applied.get("applied"):
                    round_result["stop_reason"] = "no_auditable_repair"
                    break
        except Exception as exc:
            validation = {"status": "failed", "completed": False, "stages": [], "errors": [str(exc)]}
        # The per-round builder and validator make a freshly rebuilt simulator
        # a hard requirement. Never accept a report that did not run them.
        if validation is None:
            validation = {"status": "failed", "completed": False, "stages": [],
                          "errors": ["simulator_missing: SDL validation did not run"]}
        if previous_simulator is None:
            os.environ.pop("UAGENT_SIMULATOR_EXE", None)
        else:
            os.environ["UAGENT_SIMULATOR_EXE"] = previous_simulator
        close_browser_runtime(runtime_id)
    summary = _json_summary(source_path, staging, output_path, browser, validation, generated, rounds)
    (output_path / "cross-validation.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Isolated browser/npm + AiBuilder/SDL cross-validation")
    parser.add_argument("source")
    parser.add_argument("output")
    parser.add_argument("--simulator-exe")
    parser.add_argument("--model", default="gpt-5.5")
    parser.add_argument("--settings")
    parser.add_argument("--api-key")
    parser.add_argument("--max-rounds", type=int, default=5)
    parser.add_argument("--visible-browser", action="store_true")
    args = parser.parse_args()
    try:
        settings = load_settings(Path(args.settings)) if args.settings else None
        summary = cross_validate(args.source, args.output, simulator_exe=args.simulator_exe, model=args.model,
                                 settings=settings, api_key=args.api_key, max_rounds=args.max_rounds,
                                 visible_browser=args.visible_browser)
    except Exception as exc:
        print(json.dumps({"source": str(Path(args.source).resolve()), "output": str(Path(args.output).resolve()),
                          "browser": {"status": "failed", "screens": 0, "error": str(exc)},
                          "validation": {"status": "failed", "completed": False, "stages": [], "errors": [str(exc)]},
                          "generated_files": []}, ensure_ascii=False))
        return 1
    return 0 if summary["browser"]["screens"] and summary["validation"]["completed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
