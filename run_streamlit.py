import os
import subprocess
import sys


def main():
    port = os.getenv("PORT", "8501")
    address = os.getenv("STREAMLIT_SERVER_ADDRESS", "0.0.0.0")
    command = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        "app.py",
        "--server.address",
        address,
        "--server.port",
        str(port),
    ]
    raise SystemExit(subprocess.call(command))


if __name__ == "__main__":
    main()
