"""
Print dashboard: a web page for the corpus, served by FastAPI. The sidebar loads
experiments as in the arch dashboard; the Print tab plays a print with its pump
signals, the Valve step test tab shows the step test of 16 May 2025 and the
NDT-CE 2025 results. No build step: dashboard/static/ is the whole front
end, and Plotly's own script comes from the installed plotly package. The same
page runs without a server from the files dashboard/export_static.py writes.

Run:  python -m uvicorn dashboard.server:app      (from the repo root)
"""
from __future__ import annotations

import threading
from contextlib import asynccontextmanager
from pathlib import Path

import plotly
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from dashboard import data as D

STATIC = Path(__file__).resolve().parent / "static"
PLOTLY_JS = Path(plotly.__file__).parent / "package_data" / "plotly.min.js"


@asynccontextmanager
async def lifespan(_: FastAPI):
    # the width page reads every profile of the step test: about 30 s, once
    if D.has_step_test():
        threading.Thread(target=D.width_control, daemon=True).start()
    yield


app = FastAPI(title="3DCP print dashboard", docs_url=None, redoc_url=None, lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=2048)


class Specimen(BaseModel):
    name: str
    keys: list[list[str]]


# The GET routes are file-shaped (…/<id>.json) so that dashboard/export_static.py
# can write the same answers as files for a static host.
@app.get("/api/site.json")
def site():
    return D.site()


@app.get("/api/catalogue.json")
def catalogue():
    return D.catalogue()


def _corpus(fn, eid: str):
    try:
        return fn(eid, (("corpus", eid),))
    except IndexError:
        raise HTTPException(404, f"No print {eid!r} in the catalogue.")
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.get("/api/replay/{eid}.json")
def replay_one(eid: str):
    return _corpus(D.replay, eid)


@app.post("/api/upload")
def upload(file: UploadFile = File(...), era: str = Form("Auto")):
    digest = D.add_upload(file.file.read())
    return {"key": ["upload", digest, file.filename, era],
            "name": f"{Path(file.filename).stem} (upload)"}


@app.post("/api/replay")
def replay(spec: Specimen):
    keys = tuple(tuple(k) for k in spec.keys)
    try:
        return D.replay(spec.name, keys)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except KeyError:
        raise HTTPException(410, "This upload is gone (the server restarted). Upload it again.")


@app.get("/api/width-control.json")
def width_control():
    if not D.has_step_test():
        raise HTTPException(404, "This data folder has no step test: see dashboard.json in the README.")
    return D.width_control()


@app.get("/api/width-control/profiles/{layer}/{x}.json")
def scan_profiles(layer: int, x: int):
    if not 1 <= layer <= 7:
        raise HTTPException(422, "Layer must be between 1 and 7.")
    return D.scan_profiles(layer, x)


@app.get("/vendor/plotly.min.js", include_in_schema=False)
def plotly_js():
    return FileResponse(PLOTLY_JS, media_type="text/javascript",
                        headers={"Cache-Control": "public, max-age=604800"})


class CheckedStatic(StaticFiles):
    """The page's own files are checked against the server on every load: a browser
    running an old script against a newer API just shows an error."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/", CheckedStatic(directory=STATIC, html=True), name="static")
