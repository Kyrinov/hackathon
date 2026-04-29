"""Streamlit Community Cloud entrypoint.

Streamlit Cloud sets the working directory to the repo root but does not
add it to sys.path. Local runs use `PYTHONPATH=. streamlit run app/main.py`
to side-step the same issue. This shim makes both work without thinking
about it: import this file as the deployment's main file in the Streamlit
Cloud UI.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.main import main  # noqa: E402

if __name__ == "__main__":
    main()
