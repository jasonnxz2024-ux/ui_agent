"""Native desktop launcher for the UAgent local web application."""
from __future__ import annotations

import socket
import sys
import threading
import time
import traceback
import webbrowser
import os

import uvicorn


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def main() -> None:
    # The console sibling and GUI --convert share the exact local compiler.
    # No server, browser UI or model service is needed to invoke the command.
    if '--convert' in sys.argv[1:2] or (os.path.basename(sys.executable).lower() == 'uagent-convert.exe' and len(sys.argv) > 1):
        # A GUI bootloader has no console streams, even when used as our
        # conversion worker. Write its progress into the job's live log.
        if sys.stdout is None:
            sys.stdout = open(os.environ.get('UAGENT_CONVERSION_LOG') or os.devnull,
                              'a', encoding='utf-8', buffering=1)
        if sys.stderr is None:
            sys.stderr = sys.stdout
        if sys.argv[1:2] == ['--convert']:
            del sys.argv[1]
        from tools.runtime_convert import main as convert_main
        raise SystemExit(convert_main())
    startup_log = os.environ.get("UAGENT_STARTUP_LOG", "").strip()

    def trace(message: str) -> None:
        if not startup_log:
            return
        try:
            with open(startup_log, "a", encoding="utf-8") as stream:
                stream.write(message + "\n")
        except OSError:
            pass

    trace("main-enter")
    # PyInstaller ``console=False`` sets stdout/stderr to None.  Uvicorn's
    # default formatter probes ``isatty()`` during logging setup, which would
    # abort the GUI before the local API can start.  Keep logging enabled but
    # give it a harmless stream and bypass the console-specific config.
    if sys.stdout is None:
        sys.stdout = open("uagent-launcher.log", "a", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open("uagent-launcher.log", "a", encoding="utf-8")
    def report_startup_error(exc: BaseException) -> None:
        message = "UAgent startup failure:\n" + "".join(traceback.format_exception(exc))
        for path in ("uagent-launcher.log", str(__import__('tempfile').gettempdir() + "/uagent-launcher.log")):
            try:
                with open(path, "a", encoding="utf-8") as stream:
                    stream.write(message + "\n")
            except OSError:
                pass

    try:
        requested_port = int(os.environ.get("UAGENT_PORT", "0"))
    except ValueError:
        requested_port = 0
    port = requested_port if 1 <= requested_port <= 65535 else _free_port()
    trace(f"port={port}")
    config = uvicorn.Config(
        "server.app:app", host="127.0.0.1", port=port,
        log_level="warning", log_config=None, access_log=False,
    )
    # Import the ASGI application before starting the background thread so a
    # missing bundled dependency is recorded instead of being reduced to the
    # generic “local service failed” message.
    try:
        trace("config-load-start")
        config.load()
        trace("config-load-done")
    except BaseException as exc:
        trace(f"config-load-error={type(exc).__name__}:{exc}")
        report_startup_error(exc)
        raise RuntimeError("UAgent 本地服务启动失败（详见 uagent-launcher.log）") from exc
    server = uvicorn.Server(config)

    def run_server() -> None:
        try:
            server.run()
        except BaseException as exc:
            trace(f"worker-error={type(exc).__name__}:{exc}")
            report_startup_error(exc)
            raise

    worker = threading.Thread(target=run_server, name="uagent-api", daemon=True)
    worker.start()
    trace("worker-started")
    deadline = time.monotonic() + 10
    while not server.started and worker.is_alive() and time.monotonic() < deadline:
        time.sleep(0.05)
    if not server.started:
        trace("server-not-started")
        error = RuntimeError("UAgent 本地服务启动失败（详见 uagent-launcher.log）")
        report_startup_error(error)
        raise error

    if os.environ.get("UAGENT_HEADLESS") == "1":
        trace("headless-join")
        # Used by release smoke tests and service-only deployments.  The
        # desktop window remains the default path for normal users.
        try:
            worker.join()
        finally:
            server.should_exit = True
        return

    url = f"http://127.0.0.1:{port}"
    try:
        import webview

        webview.create_window(
            "UAgent · React → LVGL",
            url,
            width=1080,
            height=800,
            min_size=(820, 650),
            background_color="#f5f7fb",
        )
        webview.start(debug=False)
    except ImportError:
        webbrowser.open(url)
        worker.join()
    finally:
        server.should_exit = True
        worker.join(timeout=5)


if __name__ == "__main__":
    main()
