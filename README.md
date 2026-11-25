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

## Remaining staging checks

Cluster highlighting uses an optional, viewer-only `clusterMemberships` table.
Each atom has an explicit row with `materialId`, `clusterIndex`, `memberIndex`,
`siteIndex`, `imageA`, `imageB`, `imageC`, and `pointGroupRow`. Member indices
preserve the certified atom order within each cluster. Signed image values are
integer lattice translations. The app lazily fetches only the selected
material's table and checks MPContribs' native table identity/checksum, structure
fingerprint, and source-table bindings. `data.coreStructureId` pins the final
structure of a specific Core-origin task; the viewer and CIF/JSON downloads
retrieve it from `get_rester().materials.tasks.search(task_ids=[...])`.
The current Summary API supplies property metadata independently. Elements come from the pinned structure;
the optional Spin display reads site magnetic moments from that same task's
OUTCAR output, without changing the bound geometry.
point groups and distances come from the existing tables. The new table is not
rendered in the app or included in normal scientific-table exports. The original
`clusters` and `clusterPointGroups` tables remain unchanged. Unavailable/stale data shows
"Cluster highlighting isn't available for this material" and leave the normal
crystal view usable. Legacy contributions without a pin use Summary geometry;
a failed pinned-task request never substitutes a newer Summary structure.

Before tables are uploaded, set `CLUSTER_EXPLORER_HIGHLIGHT_DIR` to the
external package's `materials/` directory for local testing. These JSON files
are offline caches, not attachments or upload artifacts. Use the
`core-structure-highlight-v2` package; older compact caches are rejected. A remote
highlighting record is authoritative and never falls back to an older local
file. Local data and credentials are not included in the deployable app.

### Testing highlighting on another machine

Until the memberships are published to MPContribs, a normal clone has no
highlighting data. Request `cluster-explorer-example-highlighting.zip` from the
authors. It contains only the validated membership caches for `mp-29950`,
`mp-4474`, and `mp-673706`, not structures or credentials. Extract it outside
this repository, then start the app with the existing API key in your environment:

```sh
export CLUSTER_EXPLORER_HIGHLIGHT_DIR="/absolute/path/to/cluster-explorer-example-highlighting/materials"
uv run --locked python run_app.py
```

Choose one of those examples and leave **Cluster atoms** enabled. Structures
come directly from MP's task API using each saved `coreStructureId`.
Point groups and mean distances come
from the existing contribution tables; the cache supplies only atom membership
and validation metadata. A structure/table mismatch keeps highlighting off rather
than applying potentially incorrect site indices. Other materials can still show
the unavailability warning. No JSON attachment or duplicate structure upload is
needed for this local demonstration.

## References

- [Cluster Finder](https://github.com/RajbanulAkhond/Cluster_Finder)
- [Cluster paper](https://arxiv.org/abs/2601.04460)
- [Flat-band source](https://doi.org/10.1038/s41524-024-01220-x)
- [MPShips contributor guide](https://github.com/materialsproject/MPShips/tree/mpships2)

This work was supported by the U.S. Department of Energy, Office of Science,
Basic Energy Sciences, under the Early Career Research Program Award DE-SC0026069.
