"""Entry point for the packaged standalone app (see build-windows.yml).

Not used for local development — that's `./run.sh`, which runs app.py
directly with Flask's debug reloader. This module is what PyInstaller
bundles into a double-clickable executable: it starts the same Flask app
without the reloader (which doesn't play well with a frozen executable),
opens the browser once the server is ready, and keeps the console window
open with a plain-language status message so a non-technical user knows
it's running and how to stop it.
"""
import encodings.idna  # noqa: F401 - socket.getaddrinfo() lazy-imports this;
# PyInstaller's static analysis misses it, so binding the server crashes
# with "LookupError: unknown encoding: idna" unless it's imported up front.
import socket
import threading
import time
import webbrowser

from app import app

HOST = "127.0.0.1"
PORT = 5050


def _wait_for_server_then_open_browser():
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            with socket.create_connection((HOST, PORT), timeout=0.5):
                break
        except OSError:
            time.sleep(0.2)
    webbrowser.open(f"http://{HOST}:{PORT}")


def main():
    print("=" * 60)
    print("  Fix Split Tables in PDF")
    print("=" * 60)
    print()
    print("  Starting up... your browser will open automatically.")
    print("  If it doesn't, go to: http://127.0.0.1:5050")
    print()
    print("  Keep this window open while you use the app.")
    print("  Close this window (or press Ctrl+C) to stop it.")
    print()

    threading.Thread(target=_wait_for_server_then_open_browser, daemon=True).start()
    app.run(host=HOST, port=PORT, debug=False, threaded=True)


if __name__ == "__main__":
    main()
