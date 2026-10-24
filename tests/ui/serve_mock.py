"""Runs the real ATLAS app on 127.0.0.1:8801 with a MOCKED internet (browser UI testing only).

Usage: uv run python tests/ui/serve_mock.py   (then, in another terminal: python3 tests/ui/run_ui_checks.py)
"""
import os, sys, tempfile
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
os.environ["ATLAS_DATA_DIR"] = tempfile.mkdtemp(prefix="atlas-ui-")      # throwaway data dir
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "tests"))
import httpx, uvicorn
import conftest
from atlas.server import create_app

w = conftest.World()
mk = lambda follow=True: httpx.Client(transport=httpx.MockTransport(w.handler), follow_redirects=follow)
app = create_app(engine_kwargs={"search_client": mk(), "fetch_client": mk(False), "llm_client": mk(), "resolver": conftest.fake_resolver})
uvicorn.run(app, host="127.0.0.1", port=8801, log_level="warning")
