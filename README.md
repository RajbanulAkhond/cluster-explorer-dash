# Cluster Explorer (Dash)

Explore transition-metal clusters in Materials Project crystals. The application
combines the public MPContribs `cluster_materials` project and its `clusters` and
`clusterPointGroups` tables with current native MP properties, structures and
electrode data. 

## Install and run locally

Use Python 3.12. Direct dependencies are pinned in `pyproject.toml`; `uv.lock`
locks transitive dependencies. From this directory:

```sh
uv sync --locked --python 3.12 --extra serve
```

Set `MP_API_KEY` in the server environment using your local secret-management
method. Do not put it in a source file, command-line argument or browser store.

```sh
uv run --locked python run_app.py
```

Open http://127.0.0.1:8050/. The generated `run_app.py` is a **development**
launcher: it enables Dash debug mode and is not a production command.

For a standalone WSGI smoke deployment, import its `server` without executing
the development launcher:

```sh
uv run --locked --extra serve gunicorn --bind 127.0.0.1:8052 \
  --workers 1 --threads 4 --timeout 180 run_app:server
```

This keeps debug/reloader off. Use a managed reverse proxy/TLS and the MP team's
production container for public deployment.

## Application structure

| File or hook | Responsibility |
| --- | --- |
| `run_app.py`, `src/cluster_explorer/app.py` | Preserved MPShips launcher and app factory wiring. |
| `pages/cluster_explorer.py`: `ships_setup` | Read-only configuration, scientific formatting and control definitions. |
| `ships_layout` | Search sidebar, result grid, selected material, crystal viewer, tables and downloads. |
| `ships_callbacks` | API reads, validated joins, caches, filters, selection, exports, viewer scenes and battery data. |
| `assets/scientific_text.js` | Escaped React rendering of scientific tokens inside AG Grid. |
| `assets/viewer_controls.js` | Camera state, stale-scene guards, redraw/label cleanup and the Cartesian compass. |
| `assets/cluster_explorer.css`, `cluster_header_pattern.svg` | App-scoped layout, classic MP palette, responsive controls and header pattern. |
| `tests/test_workflows.py` | Exactly 10 read-only browser acceptance workflows, excluded from the runtime wheel. |

All Python app logic remains in MPShips' three supported hooks. The browser
components are supplied by Dash/AG Grid and `dash-mp-components`.

## Data and interaction workflow

1. Page the public contribution project, retrieve both child tables, and query
   native MP summary properties through `mpships_infra.get_rester()`.
2. Validate identifiers, table checksums, row counts and cluster consistency.
   Build a cached search index; do not display a partial contribution index as complete.
3. Search, filter, sort and page on the server. Send only the displayed result
   page to the grid. Each browser owns its filter/selection/camera state.
4. Selecting a row or example loads material details and the current MP structure.
   The crystal view uses pymatgen/JmolNN display bonds, not the Cluster Finder graph.
5. Retrieve native insertion/conversion electrode models automatically for the
   selected material. Snapshot membership remains distinct from current records.
6. Export all matches as CSV, or both child tables plus provenance as ZIP.
   CIF/JSON structure downloads retain source coordinates and the chosen supercell.


## Tests

Install testing dependencies separately from production dependencies:

```sh
uv sync --locked --python 3.12 --extra test
uv run --locked --extra test python -m playwright install chromium --only-shell
uv run --locked --extra test python -m unittest discover -s tests -v
```

The app must already be running. Set `CLUSTER_EXPLORER_BASE_URL` to test a different
host or the actual MP staging prefix. Browser downloads/screenshots go to a
temporary directory, never into the repository. No test uploads contributions.

The 10 workflows cover startup/index completeness, search/elements/paging/sorting,
scientific filters/ranges, examples/details/links, flat-band and automatic battery
data, crystal interaction/reset, downloads, saved links/session isolation,
responsive pinned controls, and invalid-state/stale-content handling. Expected
counts come from the complete live export rather than a fixed material count.

## Remaining staging checks

Cluster highlighting uses an optional, viewer-only `clusterMemberships` table.
Its four columns are `materialId`, `clusterIndex`, `siteReferences`, and
`pointGroupRow`. References such as `2@(1,0,-1)` include periodic offsets;
bare indices mean zero offsets. The app lazily fetches only the selected
material's table and checks MPContribs' native table identity/checksum, structure
fingerprint, and source-table bindings. Elements come from the summary structure;
point groups and distances come from the existing tables. The new table is not
rendered in the app or included in normal scientific-table exports. The original
`clusters` and `clusterPointGroups` tables remain unchanged. Unavailable/stale data shows
"Cluster highlighting isn't available for this material" and leave the normal
crystal view usable.

Before tables are uploaded, set `CLUSTER_EXPLORER_HIGHLIGHT_DIR` to the
external package's `materials/` directory for local testing. These JSON files
are offline caches, not attachments or upload artifacts. A remote
highlighting record is authoritative and never falls back to an older local
file. Local data and credentials are not included in the deployable app.

## References

- [Cluster Finder](https://github.com/RajbanulAkhond/Cluster_Finder)
- [Cluster paper](https://arxiv.org/abs/2601.04460)
- [Flat-band source](https://doi.org/10.1038/s41524-024-01220-x)
- [MPShips contributor guide](https://github.com/materialsproject/MPShips/tree/mpships2)

This work was supported by the U.S. Department of Energy, Office of Science,
Basic Energy Sciences, under the Early Career Research Program Award DE-SC0026069.
