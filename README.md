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

### Search and examples

Each new text query (or change of search mode) replaces the previous element
selection. The periodic table automatically selects elements shared by **every**
text match: a material ID or unique formula selects that material's elements;
partial/ambiguous searches select only their common elements; no matches or an
empty query select none. This cannot narrow the text matches. Subsequent
periodic-table clicks refine that text result set. Other filters remain active;
Reset clears all filters, and saved links preserve their explicit element choices.
Pressing Enter explicitly starts a fresh text search, even when its text is
unchanged or empty.

Selecting an example clears conflicting filters, searches its exact MP ID,
resets pagination, selects its result row, and loads its details and crystal.
Loading spinners track the slow index/material/viewer/battery callbacks, including
the response stores rather than only the final display callbacks.


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
Set `CLUSTER_EXPLORER_REQUIRE_EXAMPLE_HIGHLIGHTING=1` when the example cache bundle
is configured to require positive highlighting checks for all three examples.

The 10 workflows cover startup/index completeness, search/elements/paging/sorting,
scientific filters/ranges, examples/details/links, flat-band and automatic battery
data, crystal interaction/reset, downloads, saved links/session isolation,
responsive pinned controls, and invalid-state/stale-content handling. Expected
counts come from the complete live export rather than a fixed material count.

## Highlighting and staging

Highlighting uses an optional, viewer-only clusterMemberships table with
materialId, clusterIndex, memberIndex, siteIndex, imageA, imageB, and imageC.
Member order is explicit. Image offsets are lattice translations. Limits allow
32 clusters, 16 atoms per cluster, site indices 0–255, images −2…2, and 512 atom
rows per material. Integral API numbers and numeric strings are accepted;
fractional, malformed and out-of-range values are rejected.

Each clusters row stores its certified point-group symbol. It must exist in
clusterPointGroups; repeated symbols do not imply a unique X-label assignment.
Mean distance comes from that same cluster row. Original cluster values and row
order are preserved when the column is added. Legacy tables without the column
remain readable. Memberships are neither displayed nor included in ordinary
scientific-table exports.

Table presence enables highlighting. Main data may contain
highlightingUnavailableReason, but no ClusterHighlighting object, structure hash
or source-table hashes are required. Ordinary API table identity and pagination
checks remain. Scientific fingerprints and provenance belong to the offline audit.

data.coreStructureId is stored with an mp- prefix, including encoded task IDs.
The app retrieves that task's final structure for the viewer and CIF/JSON
downloads; current Summary properties are fetched independently. Spin moments
come from the same task's OUTCAR. One MP client is reused per worker and structures
remain lazy and cached. Preparation batches task reads and verifies returned-ID
coverage. Failed pinned-task reads never substitute Summary geometry.

### Local preview before upload

Request the regenerated cluster-explorer-example-highlighting.zip containing
mp-29950, mp-4474 and mp-673706. Extract outside the repository and set:

    export CLUSTER_EXPLORER_HIGHLIGHT_DIR="/absolute/path/to/cluster-explorer-example-highlighting/materials"
    uv run --locked python run_app.py

The local JSONs contain both source tables plus memberships to preview the new
symbol column before migration. Original source values must agree with the live
contribution. They contain no structures or credentials and must not be uploaded
as attachments. Old cache formats are rejected. A published unavailable record
or incomplete migration cannot fall back to local preview data.

Missing or invalid data shows "Cluster highlighting isn't available for this
material". The ordinary viewer remains usable where a structure can be fetched.

## References

- [Cluster Finder](https://github.com/RajbanulAkhond/Cluster_Finder)
- [Cluster paper](https://arxiv.org/abs/2601.04460)
- [Flat-band source](https://doi.org/10.1038/s41524-024-01220-x)
- [MPShips contributor guide](https://github.com/materialsproject/MPShips/tree/mpships2)

This work was supported by the U.S. Department of Energy, Office of Science,
Basic Energy Sciences, under the Early Career Research Program Award DE-SC0026069.
