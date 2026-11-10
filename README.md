# 3DCP print monitor and dashboard

Software for recording, monitoring and replaying 3D concrete printing runs.

| Folder | What it does |
|---|---|
| `monitor/` | printmon: acquisition fan-out, live viewer, lossless recorder and storage for the printer's pump, gantry and laser profile scanner. See [monitor/README.md](monitor/README.md). |
| `dashboard/` | A web page with two tabs: Print (a logged print replayed in 3D with its pump signals and scan widths) and Valve step test (a water valve step test and the results of the NDT-CE 2025 paper). Served by FastAPI, or exported as a static site. |
| `analysis/` | The specimen model and the replay video behind the dashboard. |
| `ingest/` | Converts raw pump and scan logs to typed Parquet files with a DuckDB catalogue. |

## Dashboard

Install, from the repository root:

```powershell
py -3.11 -m venv venv
venv\Scripts\pip install -r requirements.txt
```

Download the published dataset ([doi:10.5281/zenodo.24117435](https://doi.org/10.5281/zenodo.24117435)) into a
folder, or let `fetch_dataset.py` do it with an md5 check of every file:

```powershell
venv\Scripts\python fetch_dataset.py 10.5281/zenodo.24117435 dataset
```

The dashboard lists the prints it finds in that folder: up to six as buttons in
the top bar, more in a sidebar with a calendar.

```powershell
$env:PRINT_DATA = "C:\path\to\dataset"
venv\Scripts\python -m uvicorn dashboard.server:app
```

Open the address uvicorn prints.

The charts show what was measured, not interpolated: a bead width where both edges
of the bead were found on a laser profile, as boxes over 15 s of profiles. Only the
colour of the 3D bead between scan sections is interpolated, and the page says so.
The Print tab draws no conclusions.

### Static site

The same page runs without a server from plain files:

```powershell
venv\Scripts\python -m dashboard.export_static --out site
```

`site/` holds the page, Plotly, one JSON file per print (about 1 MB each) and the
step test's files, for any static host. The public copy is built that way by
`.github/workflows/pages.yml`: it downloads the dataset from Zenodo with
`fetch_dataset.py`, exports the site and deploys it to GitHub Pages. The job runs
once the repository variable `ZENODO_RECORD` holds the dataset's record id or
DOI, with Pages set to deploy from GitHub Actions (Settings, Pages, Source). A
second variable, `PAGES_DOMAIN`, writes the `CNAME` file when the site has a
custom domain; the domain itself is entered under Settings, Pages.

### dashboard.json

The page's settings live in `dashboard/dashboard.json` in this repository, so names
and DOIs can change while the published data stays fixed. A copy in the data folder
takes precedence. Every key is optional:

```json
{"board": "<experiment id>", "arch": "<experiment id>",
 "default": "<experiment id>",
 "title": "3DCP print dashboard",
 "doi": "10.5281/zenodo.0000000", "code_doi": "10.5281/zenodo.0000001",
 "prints": {"<experiment id>": {"title": "Arch, May 2025", "mortar": "A"}},
 "notes": ["..."]}
```

`board` and `arch` name the step test and the arch printed after it, for the
Valve step test tab; without them that tab is off. `default` is the print the
page opens on. `prints` gives each print a
display name and a mortar id. `doi`, `code_doi` and `notes` fill the footer;
without `notes` the page shows its own method notes.

## Water-to-solids ratio (W/S)

The dashboard and the replay video show an estimated water-to-solids ratio. It
is for display only. This project does not interpret it, and any interpretation
is up to the user.

The value is an estimate, not a measurement. It comes from the measured water
flow and the screw's on/off signal, with a constant solids rate set in
`analysis/water.py` and a stirred-tank model of the mixing chamber (Versteege,
Wolfs and Salet, 2025, Automation in Construction 177, 106327).

## Replay video

```powershell
venv\Scripts\python analysis\replay_video.py <experiment id> --seconds 40
```

## Citing

See [CITATION.cff](CITATION.cff). Dataset: [doi:10.5281/zenodo.24117435](https://doi.org/10.5281/zenodo.24117435).
The width control results are from doi:10.58286/31704.

## Licence

The code is under the licence in [LICENSE](LICENSE). The dataset has its own
licence, stated on its Zenodo record.
