"""MPShips application for the public Cluster Materials contribution project."""

import copy
import csv
import hashlib
import io
import json
import logging
import math
import os
import re
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
from datetime import datetime, timezone
from functools import lru_cache
from numbers import Real
from pathlib import Path
from threading import Condition
from urllib.parse import parse_qs, urlencode

import dash  # Required MPShips import.
import dash_ag_grid as dag
import dash_mp_components as mpc
from cachetools import TTLCache, cached
import crystal_toolkit.renderables  # noqa: F401 - registers pymatgen scene renderers.
from crystal_toolkit.core.legend import Legend
from dash import Input, Output, State, ctx, dcc, html
from dash.exceptions import PreventUpdate
from mpships_infra import get_rester, MPShipsApp
from monty.json import MontyEncoder
from emmet.core.mpid import AlphaID
from pydantic import BaseModel, ConfigDict, Field
from pymatgen.core import Composition, Element, Structure
from pymatgen.core.graphs import StructureGraph
from pymatgen.core.local_env import JmolNN


class ClusterExplorer(MPShipsApp):
    def ships_setup(self, *args, **kwargs):
        self.project_key = "cluster_materials"
        # Server-side only. Never ship local data or a workstation path in assets.
        self.highlight_directory = os.environ.get("CLUSTER_EXPLORER_HIGHLIGHT_DIR", "")

        def scientific_tokens(value, kind="text"):
            """Presentation tokens only; never alter stored scientific values.

            Explicit kinds prevent IDs, dimensionalities and cluster counts from
            being treated as chemical formulae. Text remains escaped by React.
            """
            if isinstance(value, (list, tuple)):
                tokens = []
                for index, item in enumerate(value):
                    if index:
                        tokens.append({"text": ", ", "script": "text"})
                    tokens.extend(scientific_tokens(item, kind))
                return tokens or [{"text": "Unknown", "script": "text"}]
            text = "Unknown" if value is None else str(value)
            if kind == "point_group":
                if "," in text:
                    return scientific_tokens([part.strip() for part in text.split(",")], kind)
                match = re.fullmatch(r"([CDS])_?((?:\d+|[*∞])[hvd]?|[is])|([TOIK])_?([hd])", text)
                if match:
                    return [{"text": match[1] or match[3], "script": "text"},
                            {"text": (match[2] or match[4]).replace("*", "∞"), "script": "sub"}]
            if kind == "number":
                match = re.fullmatch(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+))[eE]([+-]?\d+)", text)
                if match:
                    return [{"text": match[1] + " × 10", "script": "text"},
                            {"text": str(int(match[2])).replace("-", "−"), "script": "sup"}]
            if kind == "unit":
                text = text.replace("μB", "μ_B").replace("mu_B", "μ_B")
                text = re.sub(r"/(atom|cell|g)\b", r" \1^{-1}", text)
            if kind in {"formula", "ion", "unit"}:
                # Charges are explicit (^2-, ^{3+}); the working-ion field also
                # permits unambiguous monatomic ion strings such as Fe3+.
                ion = re.fullmatch(r"([A-Z][a-z]?)(\d*[+−-])", text) if kind == "ion" else None
                if ion:
                    return [{"text": ion[1], "script": "text"}, {"text": ion[2].replace("-", "−"), "script": "sup"}]
                pattern = r"\^\{[^{}]+\}|\^[\d+−-]+|_\{[^{}]+\}|_(?:[A-Za-z]+|\d+(?:\.\d+)?)"
                if kind in {"formula", "ion"}:
                    pattern += r"|(?<=[A-Za-z)\]])\d+(?:\.\d+)?(?:[-–−]\d+(?:\.\d+)?)?|[+−-]$"
            elif kind == "space_group":
                # In P2_12_12_1, each underscore applies to ONE screw-axis
                # digit, not to the following rotation axis as well.
                pattern = r"_\{\d+\}|_\d|-[12346]"
            else:
                return [{"text": text, "script": "text"}]
            tokens, offset = [], 0
            for match in re.finditer(pattern, text):
                if match.start() > offset:
                    tokens.append({"text": text[offset:match.start()], "script": "text"})
                part = match[0]
                script = "sub"
                if kind == "space_group" and part.startswith("-"):
                    script, part = "overline", part[1:]
                elif part[0] in "_^":
                    script, part = ("sup" if part[0] == "^" else "sub"), part[1:].strip("{}")
                elif part in {"+", "-", "−"}:
                    script = "sup"
                tokens.append({"text": part.replace("-", "−") if script == "sup" else part, "script": script})
                offset = match.end()
            if offset < len(text):
                tokens.append({"text": text[offset:], "script": "text"})
            return tokens or [{"text": text, "script": "text"}]

        def scientific_text(value, kind="text"):
            tokens = scientific_tokens(value, kind)
            constructors = {"sub": html.Sub, "sup": html.Sup}
            children = [constructors[token["script"]](token["text"]) if token["script"] in constructors
                        else html.Span(token["text"], className="ce-overline") if token["script"] == "overline"
                        else token["text"] for token in tokens]
            return html.Span(children, className="ce-scientific", title=str(value) if value is not None else None)

        self.scientific_tokens = scientific_tokens
        self.scientific_text = scientific_text
        self.numeric_filters = (
            ("bandGap", "Band gap (eV)", "band_gap"),
            ("hull", "Energy above hull (eV/atom)", "energy_above_hull"),
            ("magnetization", "Magnetization (μB/cell)", "total_magnetization"),
            ("formation", "Formation energy (eV/atom)", "formation_energy_per_atom"),
        )
        self.choice_filters = (
            ("size", "Cluster size"), ("pointGroup", "Cluster point group"),
            ("spaceGroup", "Sub-lattice space group"), ("connectivity", "Connectivity"),
            ("polar", "Polar class"), ("piezoelectric", "Piezoelectric class"),
            ("enantiomorphic", "Enantiomorphic class"),
        )
        self.table_labels = {
            "materialId": "MP ID", "size": "Atoms", "averageDistance": "Mean distance (Å)",
            "elements": "Elements", "isExtended": "Extended", "isShared": "Shared",
            "label": "Cluster label", "symbol": "Cluster point group",
        }

        def viewer_draw_options(representation):
            restricted = {"Wireframe": "atoms", "Space Filling": "bonds"}.get(representation)
            return [{"label": html.Span(label, title="Not shown in this style." if key == restricted else None),
                     "value": key, "disabled": key == restricted} for label, key in [
                ("Atoms", "atoms"), ("Bonds", "bonds"), ("Unit cell", "unit_cell"),
                ("Legend", "legend"), ("Labels", "labels"), ("Axes (x/y/z)", "axes"),
                ("Cluster atoms", "clusters"),
                ("Spin", "magmoms"), ("Auto rotate", "rotate"),
            ]]
        self.viewer_draw_options = viewer_draw_options

    def ships_layout(self):
        def field(label, control, title=None, className="field"):
            return html.Div([html.Label(self.scientific_text(label, "unit"), className="label", title=title,
                                       htmlFor=control.id if isinstance(control, dcc.Input) else None), control], className=className)

        def dropdown(key, options, value="all", **kwargs):
            return dcc.Dropdown(id=self.id(key), options=[{"label": a, "value": b} for a, b in options],
                                value=value, clearable=False, optionHeight=44, maxHeight=264, **kwargs)

        bool_options = [("Any", "all"), ("Yes", "true"), ("No", "false"), ("Unknown", "unknown")]
        choice_options = {
            "connectivity": [("Any", "all"), ("Isolated", "isolated"),
                             ("Extended", "extended"), ("Shared", "shared")],
            **{key: bool_options for key in ["polar", "piezoelectric", "enantiomorphic"]},
        }
        advanced = [field(label, dropdown(key, choice_options.get(key, [("Any", "all")]), disabled=True))
                    for key, label in self.choice_filters]
        ranges = [html.Div([
            html.Label(self.scientific_text(label, "unit"), className="label",
                       title="Full ranges include unknown values; narrowed ranges exclude them."),
            dcc.RangeSlider(id=self.id(key + "Range"), min=0, max=1, step=.001,
                value=[0, 1], marks={}, allowCross=False, updatemode="mouseup", disabled=True,
                className="ce-property-slider"),
            html.Div(id=self.id(key + "RangeValue"), className="ce-range-value"),
        ], className="field ce-property-range", role="group", **{"aria-label": label})
            for key, label, _ in self.numeric_filters]

        return html.Div([
            html.Header([
                html.Div([
                    html.H1("Cluster Explorer", className="title is-2"),
                    html.P("Transition-metal clusters in Materials Project crystals, with cluster geometry, symmetry, dimensionality, and flat-band data.", className="subtitle is-6"),
                ]),
            ], className="ce-header"),
            html.Div(id=self.id("project-status"), className="ce-project-status"),
            html.Div(id=self.id("index-warning"), className="help"),
            html.Div(id=self.id("url-warning"), className="help", role="status"),
            html.Div([
                html.Aside([
                    html.Section([
                        field("Examples", dcc.Dropdown(
                            id=self.id("example"), value=None, placeholder="Choose…", optionHeight=44, maxHeight=264,
                            options=[{
                                "label": html.Span([self.scientific_text(formula, "formula"), f" · {mid}"]),
                                "value": mid, "search": f"{formula} {mid}",
                            } for mid, formula in [
                                ("mp-29950", "Nb3Cl8"), ("mp-4474", "V4GaS8"),
                                ("mp-673706", "Li7(Mo3Se4)4"),
                            ]],
                        )),
                        html.Button("Reset filters", id=self.id("reset"), className="button is-light is-fullwidth"),
                    ], className="ce-filter-actions box", **{"aria-label": "Filter actions and examples"}),
                    html.Section([
                        html.H2("Find materials", className="title is-5"),
                        field("Search", dcc.Input(
                            id=self.id("search"), type="text", debounce=0.35, className="input",
                            placeholder="e.g. Nb3Cl8, mp-1003545, Mn-O", maxLength=120,
                        )),
                        field("Search in", dropdown("searchMode", [
                            ("All fields", "all"), ("Formula", "formula"),
                            ("MP ID", "identifier"), ("Chemical system", "chemical"),
                        ])),
                        field("Additional data", dcc.Dropdown(
                            id=self.id("availability"), value="all", clearable=False, optionHeight=44, maxHeight=264,
                            options=[{"label": a, "value": b} for a, b in [
                                ("All materials", "all"), ("Flat-band materials", "flat"),
                                ("Battery materials", "battery"), ("Flat bands + battery", "both"),
                            ]],
                        )),
                        field("Sub-lattice dimension", dcc.Dropdown(
                            id=self.id("dimension"), value=[], multi=True, placeholder="Any", optionHeight=44, maxHeight=264,
                            options=[{"label": d, "value": d} for d in ["0D", "1D", "2D", "3D"]],
                        )),
                        html.Details([html.Summary("Clusters & symmetry"), *advanced], className="ce-filter-group"),
                        html.Details([html.Summary("Property ranges"), *ranges], className="ce-filter-group"),
                        html.Div(id=self.id("elements-label"), className="help"),
                    ], className="ce-filters box"),
                ], className="ce-sidebar"),
                html.Main([
                    dcc.Loading([dcc.Store(id=self.id("index-ready")),
                        html.Div(id=self.id("statistics"), className="ce-statistics", role="status")],
                        id=self.id("index-loading"), type="dot", color="#328086", delay_show=200,
                        parent_style={"minHeight": "3rem"},
                        target_components={self.id("index-ready"): "data", self.id("statistics"): "children"}),
                    html.Details([
                        html.Summary("Elements", className="ce-periodic-summary", title="Match all selected elements."),
                        html.Div(mpc.PeriodicContext(mpc.PeriodicTableInput(
                            id=self.id("periodic"), maxElementSelectable=8, forceTableLayout="compact",
                        ), id=self.id("periodic-context"), enabledElements=[], forwardOuterChange=True),
                            id=self.id("periodic-host")),
                    ], className="box ce-periodic", open=True),
                    html.Section([
                        html.Div([
                            html.H2("Matched cluster materials", className="title is-4"),
                            html.Span(id=self.id("result-count"), className="tag is-light"),
                        ], className="ce-section-heading"),
                        html.Div([
                            field("Sort by", dropdown("sort", [
                                ("MP ID", "identifier"), ("Formula", "formula"),
                                ("Clusters", "numberOfClusters"), ("Cluster size", "smallestClusterSize"),
                                ("Band gap", "band_gap"), ("Energy above hull", "energy_above_hull"),
                                ("Minimum mean distance", "minimumAverageDistance"),
                            ], value="identifier")),
                            field("Direction", dropdown("direction", [("Ascending", "asc"), ("Descending", "desc")], value="asc")),
                        ], className="ce-sort"),
                        dcc.Loading(dag.AgGrid(
                            id=self.id("results"), rowData=[],
                            columnDefs=[
                                {"field": "identifier", "headerName": "MP ID", "minWidth": 140},
                                {"field": "formula", "headerName": "Formula", "minWidth": 140,
                                 "cellRenderer": "CEScientificText"},
                                {"field": "numberOfClusters", "headerName": "Clusters", "minWidth": 100, "maxWidth": 110},
                                {"field": "clusterSizes", "headerName": "Cluster sizes", "minWidth": 130,
                                 "headerTooltip": "One size per contributed cluster; repeated sizes are retained.",
                                 "wrapText": True, "autoHeight": True,
                                 "valueFormatter": {"function": "params.value == null || params.value.length === 0 ? '—' : params.value.join(', ')"}},
                                {"field": "pointGroups", "headerName": "Cluster point groups", "minWidth": 150,
                                 "headerTooltip": "Each distinct recorded point group is shown once; not a one-to-one assignment to individual clusters.",
                                 "wrapText": True, "autoHeight": True,
                                 "cellRenderer": "CEScientificText",
                                 "valueFormatter": {"function": "params.value == null || params.value.length === 0 ? '—' : params.value.join(', ')"}},
                                {"field": "predictedDimensionality", "headerName": "Dimension", "minWidth": 110, "maxWidth": 120},
                                {"field": "energy_above_hull", "headerName": "Energy above the hull", "minWidth": 160,
                                 "cellRenderer": "CEScientificText",
                                 "headerTooltip": "Energy above hull in eV/atom."},
                            ],
                            defaultColDef={"flex": 1, "sortable": False, "resizable": True,
                                           "wrapHeaderText": True, "autoHeaderHeight": True},
                            getRowId="params.data.id",
                            dashGridOptions={"rowSelection": {"mode": "singleRow", "checkboxes": False,
                                                               "enableClickSelection": True}},
                            className="ag-theme-quartz", style={"height": "360px"},
                        ), id=self.id("results-loading"), type="dot", color="#328086", delay_show=200,
                            target_components={self.id("results"): "rowData"}),
                        html.Div([
                            html.Button("Previous", id=self.id("previous"), className="button is-small", disabled=True),
                            html.Span(id=self.id("page-label")),
                            html.Button("Next", id=self.id("next"), className="button is-small"),
                            html.Button("Download CSV", id=self.id("export"), className="button is-small is-link is-light", title="All matching materials."),
                            html.Button("Download ZIP", id=self.id("export-tables"), className="button is-small", title="All matching materials, contribution tables and source metadata."),
                        ], className="ce-pagination"),
                        html.A("Share search", id=self.id("share-search"), className="ce-share"),
                        html.Div(id=self.id("export-status"), className="help", role="status"),
                    ], className="box"),
                ]),
            ], className="ce-search-layout"),
            html.Section([
                html.Div([
                    html.H2("Selected cluster material", className="title is-4"),
                    html.Span(id=self.id("selected-badge"), className="tag is-link is-light"),
                ], className="ce-section-heading"),
                dcc.Loading([dcc.Store(id=self.id("material-response")), html.Div(id=self.id("material-details"))],
                    id=self.id("material-loading"), type="dot", color="#328086", delay_show=200,
                    parent_style={"minHeight": "3rem"},
                    target_components={self.id("material-response"): "data", self.id("material-details"): "children"}),
                html.Div(id=self.id("structure-status"), className="ce-structure-status"),
                html.Details([
                    html.Summary("Viewer settings"),
                    html.Div([
                        html.Fieldset([
                            html.Legend("Supercell", className="label"),
                            html.Div([field(axis.upper(), dcc.Input(id=self.id("supercell-" + axis), type="number", min=1, max=3, step=1, value=1,
                                debounce=True, className="input")) for axis in "xyz"], className="ce-supercell-axes"),
                        ], className="ce-supercell"),
                        field("Style", dropdown("representation", [("Default", "Default"), ("Ball & stick", "Ball and Stick"),
                            ("Space filling", "Space Filling"), ("Wireframe", "Wireframe")], value="Default")),
                        field("Colors", dropdown("colors", [("VESTA", "VESTA"), ("Jmol", "Jmol"), ("Colorblind-safe", "accessible")], value="VESTA")),
                        field("View", dropdown("camera", [("Z-up", "Perspective"), ("Along X", "Cartesian X"),
                            ("Along Y", "Cartesian Y"), ("Along Z", "Cartesian Z")], value="Perspective")),
                        field("Atom size", dcc.Slider(id=self.id("atom-scale"), min=.1, max=2, step=.1, value=1, marks={.1: ".1", 1: "1", 2: "2"}), className="field ce-viewer-scale"),
                        field("Bond width", dcc.Slider(id=self.id("bond-scale"), min=.5, max=4, step=.1, value=1, marks={.5: ".5", 1: "1", 4: "4"}), className="field ce-viewer-scale"),
                        html.Div([html.Label("Show", className="label"), dcc.Checklist(id=self.id("draw"), options=self.viewer_draw_options("Default"),
                            value=["atoms", "bonds", "unit_cell", "legend", "clusters"], className="ce-checklist",
                            labelStyle={"display": "flex", "alignItems": "center", "gap": ".4rem"})], className="field ce-viewer-display"),
                    ], className="ce-viewer-controls"),
                    html.Div([
                        html.Button("Reset viewer", id=self.id("reset-viewer"), className="button is-small"),
                        html.Button("Download CIF", id=self.id("export-cif"), className="button is-small"),
                        html.Button("Structure JSON", id=self.id("export-structure"), className="button is-small", title="Download the displayed structure."),
                        html.Button("Contribution JSON", id=self.id("export-material"), className="button is-small", title="Download this contribution."),
                    ], className="ce-viewer-actions"),
                ], className="ce-filter-group"),
                dcc.Loading([dcc.Store(id=self.id("viewer-response")), html.Div(mpc.CrystalToolkitScene([
                    None, html.Div([
                        html.Img(id=self.id("orientation-axes"), className="ce-orientation-axes", alt="Cartesian x/y/z orientation",
                                 style={"display": "none"}),
                        html.Div(id=self.id("crystal_legend_container")),
                    ], className="ce-viewer-bottom"),
                ], id=self.id("crystal_scene"), data={"name": "cluster-explorer-crystal", "contents": []}, sceneSize="100%", axisView="HIDDEN",
                    className="ce-crystal-scene",
                    # Orbit controls use the current canvas size on each gesture.
                    # Trackball controls retain zero-size bounds when mounted hidden.
                    settings={"renderer": "webgl", "controls": "orbit", "enableZoom": True,
                              "extractAxis": False, "secondaryObjectView": False},
                    showExportButton=False, showImageButton=True, showPositionButton=False),
                         id=self.id("viewer-wrapper"), className="ce-viewer", style={"display": "none"},
                         **{"data-scene-key": ""},
                         **{"aria-label": "Interactive crystal: drag to rotate, Shift-drag to move, scroll to zoom; double-click an atom to change its element color."})],
                    id=self.id("viewer-loading"), type="dot", color="#328086", delay_show=200,
                    parent_style={"minHeight": "3rem"}, target_components={self.id("viewer-response"): "data"}),
                html.Div(id=self.id("viewer-status"), className="help", role="status"),
                html.Div(id=self.id("highlight-status"), className="help ce-highlight-status", role="status"),
                html.Dialog([
                    html.H3(["Element color: ", html.Span(id=self.id("color-element-name"))],
                            id=self.id("color-dialog-title"), className="title is-5"),
                    html.Label("Color", htmlFor=self.id("element-color-input"), className="label"),
                    html.Div(id=self.id("element-color-input-container")),
                    html.Div([
                        html.Button("Apply", id=self.id("apply-element-color"), type="button", className="button is-link is-small"),
                        html.Button("Reset color", id=self.id("reset-element-color"), type="button", className="button is-small"),
                        html.Button("Cancel", id=self.id("cancel-element-color"), type="button", className="button is-small"),
                    ], className="ce-color-dialog-actions"),
                ], id=self.id("color-dialog"), className="ce-color-dialog",
                   **{"aria-labelledby": self.id("color-dialog-title")}),
                html.Div(id=self.id("contribution-tables")),
                dcc.Loading([dcc.Store(id=self.id("battery-response")),
                    html.Section(html.Div(id=self.id("battery-details")),
                        id=self.id("battery-section"), className="ce-table-section", **{"aria-live": "polite"})],
                    id=self.id("battery-loading"), type="dot", color="#328086", delay_show=200,
                    parent_style={"minHeight": "3rem"},
                    target_components={self.id("battery-response"): "data", self.id("battery-details"): "children"}),
            ], className="box ce-material"),
            html.Footer([
                html.Details([
                    html.Summary("Data notes"),
                    html.P(id=self.id("data-availability")),
                    html.P("Cluster atoms are highlighted; the rest is muted and translucent. Double-click an atom to change its element color. Point groups describe cluster types; bonds are for display."),
                    html.P("Battery filters use the contributed snapshot; battery tables show current MP records. Full property ranges include unknown values; narrowed ranges exclude them."),
                ], className="ce-data-notes"),
                html.P([
                    html.A("Cluster Finder", href="https://github.com/RajbanulAkhond/Cluster_Finder", target="_blank", rel="noopener noreferrer"),
                    " · ", html.A("Cluster paper", href="https://arxiv.org/abs/2601.04460", target="_blank", rel="noopener noreferrer"),
                    " · ", html.A("Flat-band reference: Neves et al. (2024)", href="https://doi.org/10.1038/s41524-024-01220-x", target="_blank", rel="noopener noreferrer"),
                ]),
                html.P("This work was supported by the U.S. Department of Energy, Office of Science, Basic Energy Sciences, under the Early Career Research Program Award DE-SC0026069."),
            ], className="ce-footer"),
            dcc.Store(id=self.id("page"), data=0),
            dcc.Store(id=self.id("selection"), data="mp-1003545"),
            dcc.Store(id=self.id("range-context")),
            dcc.Store(id=self.id("periodic-request")),
            dcc.Store(id=self.id("search-context")),
            dcc.Store(id=self.id("viewer-applied")),
            dcc.Store(id=self.id("element-colors"), data={}, storage_type="memory"),
            dcc.Download(id=self.id("download")),
            dcc.Download(id=self.id("structure-download")),
            dcc.Download(id=self.id("image-download")),
            dcc.Interval(id=self.id("rotation-tick"), interval=150, disabled=True),
        ], className="ce-app")

    def ships_callbacks(self, app, cache):
        project_key = self.project_key
        scientific_tokens, scientific_text = self.scientific_tokens, self.scientific_text
        table_labels = self.table_labels
        highlight_directory = self.highlight_directory
        logger = logging.getLogger(__name__)
        numeric_specs, choice_specs = self.numeric_filters, self.choice_filters
        page_size = 50

        class ClusterRow(BaseModel):
            model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
            materialId: str
            size: int = Field(ge=2, le=4096)
            averageDistance: float = Field(gt=0)
            elements: str
            isExtended: bool | None = None
            isShared: bool | None = None

        class PointGroupRow(BaseModel):
            model_config = ConfigDict(extra="forbid")
            materialId: str
            label: str = Field(min_length=1, max_length=64)
            symbol: str = Field(min_length=1, max_length=32)

        def scalar(value):
            return value.get("value") if isinstance(value, dict) and "value" in value else value

        def number(value):
            value = scalar(value)
            if value is None or isinstance(value, bool):
                return None
            result = float(value)
            if not math.isfinite(result):
                raise ValueError("Nonfinite scientific value")
            return result

        def flag(value):
            value = scalar(value)
            if value is None:
                return None
            if isinstance(value, bool):
                return value
            if isinstance(value, str) and value.casefold() in {"yes", "true", "y", "no", "false", "n"}:
                return value.casefold() in {"yes", "true", "y"}
            raise ValueError("Invalid boolean data flag")

        def identifier(value):
            if not isinstance(value, str) or not re.fullmatch(r"mp-(?:\d{1,12}|[a-z]{1,16})", value):
                raise ValueError("Invalid material identifier")
            return value

        def alias(value):
            return str(AlphaID(identifier(str(value)), padlen=8))

        def display(value):
            if isinstance(value, dict):
                return value.get("display", str(scalar(value)))
            if isinstance(value, bool):
                return "Yes" if value else "No"
            if isinstance(value, float):
                return f"{value:.6g}"
            return "Unknown" if value is None else str(value)

        def icsd_links(native):
            database_ids = (native or {}).get("database_IDs") or {}
            values = database_ids.get("icsd", []) if isinstance(database_ids, dict) else []
            if not isinstance(values, (list, tuple)):
                return []
            links, seen = [], set()
            for value in values:
                if isinstance(value, bool) or not isinstance(value, (str, int)):
                    continue
                match = re.fullmatch(r"(?:icsd-)?([0-9]{1,12})", str(value).strip(), re.IGNORECASE)
                if not match:
                    continue
                number = str(int(match[1]))
                if number == "0" or number in seen:
                    continue
                seen.add(number)
                links.append(html.A(f"ICSD {number}",
                    href=f"https://www.ccdc.cam.ac.uk/structures/Search?Ccdcid=%20{number}&DatabaseToSearch=Published",
                    target="_blank", rel="noopener noreferrer"))
            return links

        def availability_note(projection, rows):
            missing_native = len(projection["rows"]) - projection["nativeKnown"]
            missing_hull = sum(row["energy_above_hull"] is None for row in rows)
            notes = []
            if missing_native:
                notes.append(f"MP properties unavailable for {missing_native:,} material{'s' if missing_native != 1 else ''} overall.")
            if missing_hull:
                notes.append(f"Hull unavailable for {missing_hull:,} current match{'es' if missing_hull != 1 else ''}.")
            return " ".join(notes)

        def property_domains(rows):
            domains = {}
            for key, _, column in numeric_specs:
                known = [number(row.get(column)) for row in rows if row.get(column) is not None]
                if not known:
                    domains[key] = {"min": 0, "max": 1, "step": .001, "available": False}
                    continue
                low, high = min(known), max(known)
                if low == high:
                    low, high = low - .5, high + .5
                step = 10 ** math.floor(math.log10((high - low) / 1000))
                digits = max(0, -int(math.floor(math.log10(step))))
                domains[key] = {"min": round(math.floor(low / step) * step, digits),
                                "max": round(math.ceil(high / step) * step, digits),
                                "step": step, "available": True}
            return domains

        def slider_settings(filters, domains):
            values, properties, context = [], [], {}
            superscript = str.maketrans("0123456789+-−", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁻")

            def label(value):
                return "".join(token["text"].translate(superscript) if token["script"] == "sup" else token["text"]
                               for token in scientific_tokens(f"{value:.4g}", "number"))

            for key, _, _ in numeric_specs:
                domain = domains.get(key, {"min": 0, "max": 1, "step": .001, "available": False})
                bounds = [filters[key + "Min"], filters[key + "Max"]]
                # Expand for existing links rather than silently clamp a range.
                low = min([domain["min"]] + [value for value in bounds if value is not None])
                high = max([domain["max"]] + [value for value in bounds if value is not None])
                selected = [low if bounds[0] is None else bounds[0], high if bounds[1] is None else bounds[1]]
                marks = {low: label(low), high: label(high)} if domain["available"] else {low: "Unavailable"}
                values.append(selected)
                properties.extend([low, high, domain["step"], marks, not domain["available"]])
                # Per-browser restoration state preserves exact legacy bounds,
                # including explicit endpoint bounds and out-of-domain links.
                context[key] = {"limits": [low, high], "value": selected, "bounds": bounds}
            return values, properties, context

        def elements(state):
            values = (state or {}).get("enabledElements", [])
            if not isinstance(values, list) or len(values) > 8:
                raise ValueError("Invalid element selection")
            return sorted({str(Element(symbol)) for symbol in values})

        def parse_tables(record, tables_by_id):
            parsed = {}
            metadata = record.get("tables", [])
            names = [t["name"] for t in metadata]
            if len(names) != len(set(names)) or not {"clusters", "clusterPointGroups"} <= set(names) or set(names) - {"clusters", "clusterPointGroups", "clusterMemberships"}:
                raise ValueError("Expected both named contribution tables")
            for meta in metadata:
                if meta["name"] == "clusterMemberships":
                    continue  # Viewer-only: do not bulk-load or render this table.
                raw = tables_by_id[meta["id"]]
                if raw["md5"] != meta["md5"] or raw["name"] != meta["name"]:
                    raise ValueError("Table checksum or name mismatch")
                columns, data = raw["columns"], raw["data"]
                if len(data) != meta["total_data_rows"] or any(len(row) != len(columns) for row in data):
                    raise ValueError("Incomplete table")
                if list(map(str, raw["index"])) != [str(i) for i in range(len(data))]:
                    raise ValueError("Source table row positions changed")
                rows = []
                for values in data:
                    row = dict(zip(columns, values))
                    if row.get("materialId") != record["identifier"]:
                        raise ValueError("Cross-material table association")
                    if meta["name"] == "clusters":
                        row["size"] = number(row["size"])
                        row["averageDistance"] = number(row["averageDistance"])
                        for key in ("isExtended", "isShared"):
                            row[key] = flag(row[key])
                        row = ClusterRow.model_validate(row).model_dump()
                        symbols = row["elements"].split(",")
                        if len(symbols) != row["size"] or any(str(Element(s)) != s for s in symbols):
                            raise ValueError("Cluster elements/size mismatch")
                        if row["isExtended"] is True and row["isShared"] is True:
                            raise ValueError("Conflicting connectivity flags")
                    else:
                        row = PointGroupRow.model_validate(row).model_dump()
                    rows.append(row)
                parsed[meta["name"]] = rows
            clusters = parsed["clusters"]
            if len(clusters) != number(record["data"]["numberOfClusters"]):
                raise ValueError("Cluster count mismatch")
            if not math.isclose(min(c["averageDistance"] for c in clusters),
                                number(record["data"]["minimumAverageDistance"]), abs_tol=1e-6, rel_tol=1e-9):
                raise ValueError("Minimum distance mismatch")
            labels = [g["label"] for g in parsed["clusterPointGroups"]]
            if not labels or len(set(labels)) != len(labels) or len(labels) > len(clusters):
                raise ValueError("Invalid unique cluster-type labels")
            return parsed

        def contribution_records(client):
            # paginate=True first downloads a second, ID-only index and then
            # re-queries those IDs. Page the public project directly instead.
            operation = client.contributions.queryContributions
            per_page = min(1500, operation.params["per_page"].param_spec["maximum"])
            fields = ["id", "identifier", "formula", "data", "tables", "last_modified"]

            def page(number):
                return operation(project=project_key, is_public=True, per_page=per_page,
                    page=number, _fields=fields, _sort="+id",
                    _request_options={"connect_timeout": 10, "timeout": 120}).result()

            first = page(1)
            total = first["total_count"]
            if not isinstance(total, int) or isinstance(total, bool) or not 0 < total <= 50000:
                raise ValueError("Project exceeds the reviewed in-memory search budget or is empty")
            count = math.ceil(total / per_page)
            with ThreadPoolExecutor(max_workers=4) as pool:
                pages = [first, *pool.map(page, range(2, count + 1))]
            records = []
            for index, answer in enumerate(pages):
                if answer["total_count"] != total or len(answer["data"]) != min(per_page, total - index * per_page):
                    raise ValueError("Incomplete or changing contribution index")
                records.extend(answer["data"])
            if len({r["id"] for r in records}) != total or len({r["identifier"] for r in records}) != total:
                raise ValueError("Duplicate contribution index")
            for record in records:
                # Bravado decodes date-time fields; the old paginated client
                # left their ISO strings intact. Preserve provenance/fingerprints.
                if isinstance(record.get("last_modified"), datetime):
                    record["last_modified"] = record["last_modified"].isoformat()
            return records

        def native_batch(batch, summary):
            native, errors = {}, 0
            fields = ["material_id", "formula_pretty", "deprecated", "band_gap", "energy_above_hull",
                      "formation_energy_per_atom", "total_magnetization", "symmetry"]
            wanted = {alias(mid) for mid in batch}
            try:
                # Keep the deprecated fallback and exact AlphaID association.
                for deprecated in [False, True]:
                    missing = [mid for mid in batch if alias(mid) not in native]
                    if not missing:
                        break
                    docs = summary.search(material_ids=missing, deprecated=deprecated, fields=fields)
                    for doc in docs:
                        key = alias(doc["material_id"])
                        if key not in wanted or key in native:
                            raise ValueError("Unexpected or duplicate native material alias")
                        native[key] = dict(doc)
            except Exception as error:
                errors = 1
                logger.warning("Native index batch unavailable: %s", type(error).__name__)
            return native, errors

        @cache.memoize(timeout=1800)
        @cached(TTLCache(maxsize=1, ttl=1800), condition=Condition())
        def data_index():
            started = time.perf_counter()
            timings = {}
            rester = get_rester(use_document_model=False, mute_progress_bars=True)
            client = rester.contribs
            timings["client"] = round(time.perf_counter() - started, 3)
            stage = time.perf_counter()
            records = contribution_records(client)
            timings["contributions"] = round(time.perf_counter() - stage, 3)
            table_ids = [t["id"] for r in records for t in r["tables"] if t["name"] in {"clusters", "clusterPointGroups"}]
            if len(set(table_ids)) != len(table_ids):
                raise ValueError("A child table is associated with multiple contributions")

            def table_batch(ids):
                result, page = [], 1
                while True:
                    answer = client.tables.queryTables(
                        id__in=ids, per_page=100, page=page, data_per_page=1000, _fields=["_all"],
                        _request_options={"connect_timeout": 10, "timeout": 120},
                    ).result()
                    result.extend(answer["data"])
                    if not answer.get("has_more"):
                        break
                    page += 1
                    if page > 100:
                        raise ValueError("Excessive table pagination")
                if {r["id"] for r in result} != set(ids) or len(result) != len(ids):
                    raise ValueError("Incomplete table batch")
                for row in result:
                    if len(row["data"]) != row["total_data_rows"]:
                        frame = client.get_table(row["id"])
                        if frame.attrs.get("md5") != row["md5"] or frame.attrs.get("name") != row["name"]:
                            raise ValueError("Source table changed during pagination")
                        row["data"] = frame.astype(object).where(frame.notna(), None).values.tolist()
                        row["columns"] = list(frame.columns)
                        row["index"] = list(frame.index)
                return result

            batches = [table_ids[i:i + 100] for i in range(0, len(table_ids), 100)]
            ids = [r["identifier"] for r in records]
            native_batches = [ids[i:i + 400] for i in range(0, len(ids), 400)]
            # Material endpoints are lazily constructed; initialize once before
            # the workers read them. Network I/O shares one four-worker budget.
            summary = rester.materials.summary
            stage = time.perf_counter()
            table_futures, native_futures = [], []
            with ThreadPoolExecutor(max_workers=4) as pool:
                # Interleave both kinds so neither waits for the other to finish.
                for index in range(max(len(batches), len(native_batches))):
                    if index < len(native_batches):
                        native_futures.append(pool.submit(native_batch, native_batches[index], summary))
                    if index < len(batches):
                        table_futures.append(pool.submit(table_batch, batches[index]))
                tables = {table["id"]: table for future in table_futures for table in future.result()}
                native, native_errors = {}, 0
                for future in native_futures:
                    docs, errors = future.result()
                    native_errors += errors
                    for key, doc in docs.items():
                        if key in native:
                            raise ValueError("Duplicate native material alias")
                        native[key] = doc
            timings["tablesAndNative"] = round(time.perf_counter() - stage, 3)
            stage = time.perf_counter()
            rows = []
            for record in records:
                mid = identifier(record["identifier"])
                data, child = record["data"], parse_tables(record, tables)
                doc = native.get(alias(mid), {})
                formula = doc.get("formula_pretty") or record.get("formula") or mid
                composition = sorted(str(e) for e in Composition(formula).elements)
                clusters, groups = child["clusters"], child["clusterPointGroups"]
                isolated = (False if any(c["isExtended"] is True or c["isShared"] is True for c in clusters)
                            else True if all(c["isExtended"] is False and c["isShared"] is False for c in clusters) else None)
                flat = data.get("flatBand") or {}
                row = {
                    "id": record["id"], "identifier": mid, "formula": formula, "nativeIdentifier": str(doc.get("material_id", mid)),
                    "compoundSystem": data["compoundSystem"], "numberOfClusters": int(number(data["numberOfClusters"])),
                    "clusterLatticeSpaceGroup": data["clusterLatticeSpaceGroup"],
                    "predictedDimensionality": data["predictedDimensionality"],
                    "minimumAverageDistance": number(data["minimumAverageDistance"]),
                    "hasFlatData": bool(flat), "hasBatteryData": flag(data.get("hasBatteryData")),
                    "isPolar": flag(data.get("isPolar")), "isPiezoelectric": flag(data.get("isPiezoelectric")),
                    "isEnantiomorphic": flag(data.get("isEnantiomorphic")), "allIsolated": isolated,
                    "elements": composition, "clusters": clusters, "clusterPointGroups": groups,
                    "clusterSizes": ",".join(str(v) for v in sorted({c["size"] for c in clusters})),
                    "smallestClusterSize": min(c["size"] for c in clusters),
                    "pointGroups": ",".join(sorted({g["symbol"] for g in groups})),
                    "flatBand": {k: scalar(v) for k, v in flat.items()},
                    "deprecated": doc.get("deprecated"), "nativeAvailable": bool(doc),
                    "nativeSymmetry": doc.get("symmetry"), "lastModified": str(record.get("last_modified", "")),
                    "tableChecksums": {t["name"]: t["md5"] for t in record["tables"]},
                    "clusterHighlighting": data.get("clusterHighlighting"),
                    "membershipTable": next((t for t in record["tables"] if t["name"] == "clusterMemberships"), None),
                }
                for key in ["band_gap", "energy_above_hull", "formation_energy_per_atom", "total_magnetization"]:
                    row[key] = number(doc.get(key))
                rows.append(row)
            rows.sort(key=lambda row: row["identifier"])
            revision = hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()
            timings["projection"] = round(time.perf_counter() - stage, 3)
            return {"rows": tuple(rows), "revision": revision, "builtAt": datetime.now(timezone.utc).isoformat(),
                    "nativeKnown": sum(r["nativeAvailable"] for r in rows), "nativeErrors": native_errors,
                    "stageSeconds": timings,
                    "rangeDomains": property_domains(rows),
                    "coldSeconds": round(time.perf_counter() - started, 3)}

        def normalize(raw):
            if not isinstance(raw, dict):
                raise ValueError("Invalid filter state")
            result = {key: raw.get(key, default) for key, default in {
                "query": "", "searchMode": "all", "availability": "all", "dimension": [],
                "sort": "identifier", "direction": "asc", **{k: "all" for k, _ in choice_specs},
                **{key + bound: None for key, _, _ in numeric_specs for bound in ["Min", "Max"]},
            }.items()}
            if not isinstance(result["query"], str) or len(result["query"]) > 120:
                raise ValueError("Search is limited to 120 characters")
            result["query"] = result["query"].strip()
            selected = raw.get("elements", [])
            result["elements"] = elements({"enabledElements": selected})
            if result["searchMode"] not in {"all", "formula", "identifier", "chemical"}:
                raise ValueError("Invalid search mode")
            if result["availability"] not in {"all", "flat", "battery", "both"}:
                raise ValueError("Invalid additional-data filter")
            dims = result["dimension"]
            if dims in (None, "all"):
                dims = []
            if not isinstance(dims, list) or len(dims) > 4 or any(d not in {"0D", "1D", "2D", "3D"} for d in dims):
                raise ValueError("Invalid dimensionalities")
            result["dimension"] = sorted(set(dims))
            if result["sort"] not in {"identifier", "formula", "numberOfClusters", "smallestClusterSize",
                                      "band_gap", "energy_above_hull", "minimumAverageDistance"} or result["direction"] not in {"asc", "desc"}:
                raise ValueError("Invalid sort")
            for key, _ in choice_specs:
                if not isinstance(result[key], str) or len(result[key]) > 64:
                    raise ValueError("Invalid categorical filter")
            for key in ["polar", "piezoelectric", "enantiomorphic"]:
                if result[key] not in {"all", "true", "false", "unknown"}:
                    raise ValueError("Invalid three-state boolean filter")
            if result["connectivity"] not in {"all", "isolated", "extended", "shared"}:
                raise ValueError("Invalid connectivity")
            if result["size"] != "all":
                value = number(result["size"])
                if value is None or value != int(value) or not 2 <= value <= 4096:
                    raise ValueError("Cluster size must be an integer")
                result["size"] = str(int(value))
            for key, _, _ in numeric_specs:
                for bound in ["Min", "Max"]:
                    value = result[key + bound]
                    value = None if value in ("", None) else number(value)
                    if value is not None and abs(value) > 1e6:
                        raise ValueError("Numeric range outside supported bounds")
                    result[key + bound] = value
                if result[key + "Min"] is not None and result[key + "Max"] is not None and result[key + "Min"] > result[key + "Max"]:
                    raise ValueError("Minimum must not exceed maximum")
            return result

        def boolean_match(value, expected):
            return expected == "all" or (expected == "unknown" and value is None) or (expected == "true" and value is True) or (expected == "false" and value is False)

        def in_range(value, low, high):
            return (low is None and high is None) or (value is not None and (low is None or value >= low) and (high is None or value <= high))

        def text_rows(query, mode):
            """The text search always starts from the complete contributed index."""
            rows = data_index()["rows"]
            query = (query or "").strip().casefold()
            if not query:
                return list(rows)
            if mode in {"all", "identifier"}:
                try:
                    key = alias(query)
                    exact = [row for row in rows if alias(row["identifier"]) == key]
                    if exact:
                        return exact
                except ValueError:
                    pass
            if mode == "chemical":
                try:
                    wanted = {str(Element(symbol.strip())) for symbol in query.title().split("-")}
                except ValueError:
                    return []
                return [row for row in rows if wanted == set(row["elements"])]
            columns = [mode] if mode in {"formula", "identifier"} else [
                "identifier", "nativeIdentifier", "formula", "compoundSystem", "clusterLatticeSpaceGroup", "pointGroups"]
            if mode == "identifier":
                columns.append("nativeIdentifier")
            return [row for row in rows if any(query in str(row[column]).casefold() for column in columns)]

        def text_elements(query, mode):
            if not (query or "").strip():
                return []
            rows = text_rows(query, mode)
            # An intersection, not a union or guessed formula parse: every text
            # match already contains these elements, so auto-selection is neutral.
            common = set.intersection(*(set(row["elements"]) for row in rows)) if rows else set()
            return sorted(common) if len(common) <= 8 else []

        def matches(row, filters):
            if not set(filters["elements"]).issubset(row["elements"]):
                return False
            if filters["availability"] in {"flat", "both"} and not row["hasFlatData"]:
                return False
            if filters["availability"] in {"battery", "both"} and row["hasBatteryData"] is not True:
                return False
            if filters["dimension"] and row["predictedDimensionality"] not in filters["dimension"]:
                return False
            if filters["spaceGroup"] != "all" and row["clusterLatticeSpaceGroup"] != filters["spaceGroup"]:
                return False
            if filters["pointGroup"] != "all" and filters["pointGroup"] not in {g["symbol"] for g in row["clusterPointGroups"]}:
                return False
            for key, column in [("polar", "isPolar"), ("piezoelectric", "isPiezoelectric"), ("enantiomorphic", "isEnantiomorphic")]:
                if not boolean_match(row[column], filters[key]):
                    return False
            for key, _, column in numeric_specs:
                if not in_range(row[column], filters[key + "Min"], filters[key + "Max"]):
                    return False
            for cluster in row["clusters"]:
                if filters["size"] != "all" and cluster["size"] != int(filters["size"]):
                    continue
                connectivity = filters["connectivity"]
                if connectivity == "isolated" and not (cluster["isExtended"] is False and cluster["isShared"] is False):
                    continue
                if connectivity == "extended" and cluster["isExtended"] is not True:
                    continue
                if connectivity == "shared" and cluster["isShared"] is not True:
                    continue
                return True
            return False

        def matching_rows(filters):
            filters = normalize(filters)
            rows = [row for row in text_rows(filters["query"], filters["searchMode"]) if matches(row, filters)]
            sort_key, reverse = filters.get("sort", "identifier"), filters.get("direction") == "desc"
            known = [r for r in rows if r[sort_key] is not None]
            unknown = [r for r in rows if r[sort_key] is None]
            return sorted(known, key=lambda r: (r[sort_key], r["identifier"]), reverse=reverse) + sorted(unknown, key=lambda r: r["identifier"])

        control_keys = ["query", "searchMode", "availability", "dimension", "sort", "direction"] + [k for k, _ in choice_specs] + [
            key + bound for key, _, _ in numeric_specs for bound in ["Min", "Max"]]
        ui_control_keys = [key for key in control_keys if key not in {name + bound for name, _, _ in numeric_specs for bound in ["Min", "Max"]}] + [
            key + "Range" for key, _, _ in numeric_specs]
        control_ids = [self.id("search" if key == "query" else key) for key in ui_control_keys]
        filter_inputs = [Input(control_id, "value") for control_id in control_ids] + [
            Input(self.id("range-context"), "data"), Input(self.id("search-context"), "data")]
        filter_states = [State(control_id, "value") for control_id in control_ids] + [
            State(self.id("range-context"), "data"), State(self.id("search-context"), "data")]

        def form_state(values, range_context, search_context=None):
            if range_context is not None and not isinstance(range_context, dict):
                raise ValueError("Invalid property range context")
            raw = dict(zip(ui_control_keys, values))
            for key, _, _ in numeric_specs:
                selected, saved = raw.pop(key + "Range", None), (range_context or {}).get(key)
                if not saved:
                    bounds = [None, None]
                else:
                    if not isinstance(saved, dict) or any(not isinstance(saved.get(part), list) or len(saved[part]) != 2
                                                         for part in ["limits", "value", "bounds"]):
                        raise ValueError("Invalid property range context")
                    if not isinstance(selected, list) or len(selected) != 2 or any(number(v) is None for v in selected):
                        raise ValueError("Invalid property range")
                    selected = [number(v) for v in selected]
                    low, high = [number(v) for v in saved["limits"]]
                    if low is None or high is None:
                        raise ValueError("Invalid property range limits")
                    if not low <= selected[0] <= selected[1] <= high:
                        raise ValueError("Property range is outside slider limits")
                    bounds = (saved["bounds"] if selected == saved["value"] else
                              [None if selected[0] == low else selected[0], None if selected[1] == high else selected[1]])
                raw[key + "Min"], raw[key + "Max"] = bounds
            key = [(raw.get("query") or "").strip(), raw.get("searchMode") or "all"]
            # A new query and the periodic-table redraw are separate callbacks.
            # Never let the old element selection narrow the new query meanwhile.
            selected = (elements({"enabledElements": search_context.get("elements", [])})
                        if (search_context or {}).get("key") == key
                        else text_elements(*key))
            return normalize(raw | {"elements": selected})

        for key, _, _ in numeric_specs:
            # Keep value feedback instant during dragging without API callbacks.
            # Readouts avoid overlapping tooltips when the handles are close.
            app.clientside_callback(
                """function(value, dragging) {
                    const trigger=dash_clientside.callback_context.triggered[0]?.prop_id || '';
                    const range=trigger.endsWith('.drag_value') ? dragging : value;
                    if(!Array.isArray(range) || range.length!==2) return '';
                    const format=v=>Number(v).toLocaleString(undefined,{maximumSignificantDigits:6});
                    return format(range[0])+' – '+format(range[1]);
                }""",
                Output(self.id(key + "RangeValue"), "children"),
                Input(self.id(key + "Range"), "value"), Input(self.id(key + "Range"), "drag_value"),
            )

        def url_state(url_hash):
            if len(url_hash or "") > 8192:
                raise ValueError("Search link is too long")
            params = parse_qs((url_hash or "").lstrip("#"), max_num_fields=80)
            selected = identifier(params.get("material", ["mp-1003545"])[0])
            if params.get("v", ["1"])[0] != "1":
                raise ValueError("Unsupported search-link version")
            raw = {key: params[key][0] for key in control_keys if key in params}
            raw["elements"] = params.get("elements", [""])[0].split(",") if params.get("elements", [""])[0] else []
            raw["dimension"] = params.get("dimension", [""])[0].split(",") if params.get("dimension", [""])[0] else []
            return normalize(raw), selected

        def retired_filter_warning(url_hash):
            params = parse_qs((url_hash or "").lstrip("#"), max_num_fields=80)
            if any(any(value != "all" for value in params.get(key, []))
                   for key in ["pair", "isolated", "flatElement", "flatDecay",
                               "countMin", "countMax", "minimumMin", "minimumMax",
                               "distanceMin", "distanceMax", "flatCountMin", "flatCountMax"]):
                return "Some filters in this saved search are no longer available and were cleared."
            return ""

        def share_url(filters, selected=None):
            params = {"v": "1"}
            defaults = normalize({})
            for key, value in filters.items():
                if value != defaults.get(key) and value is not None:
                    params[key] = ",".join(value) if isinstance(value, list) else value
            if selected:
                params["material"] = identifier(selected)
            return dash.get_relative_path("/") + "#" + urlencode(params)

        @app.callback(
            Output(self.id("index-ready"), "data"), Output(self.id("index-warning"), "children"),
            *[Output(self.id(k), "options") for k in ["size", "pointGroup", "spaceGroup"]],
            *[Output(self.id(k), "disabled") for k, _ in choice_specs],
            Input(self.id("index-ready"), "id"),
        )
        def initialize(_):
            try:
                projection = data_index()
                rows = projection["rows"]
                options = [
                    [str(v) for v in sorted({c["size"] for r in rows for c in r["clusters"]})],
                    sorted({g["symbol"] for r in rows for g in r["clusterPointGroups"]}),
                    sorted({r["clusterLatticeSpaceGroup"] for r in rows}),
                ]
                options = [[{"label": "Any", "value": "all"}] + [
                    {"label": scientific_text(v, "point_group" if index == 1 else "space_group"),
                     "value": v, "search": v + " " + "".join(t["text"] for t in scientific_tokens(v, "point_group" if index == 1 else "space_group"))}
                    if index in {1, 2} else {"label": v, "value": v} for v in values]
                    for index, values in enumerate(options)]
                return ({"revision": projection["revision"], "ranges": projection.get("rangeDomains") or property_domains(rows)}, "", *options,
                        *[False for _ in choice_specs])
            except Exception as error:
                logger.warning("Scientific index unavailable: %s", type(error).__name__)
                return ({"error": True}, "The complete scientific index could not be verified. No partial results are shown. Check the API connection and reload.",
                        *[[{"label": "Any", "value": "all"}] for _ in range(3)],
                        *[True for _ in choice_specs])

        @app.callback(
            *[Output(control_id, "value") for control_id in control_ids],
            *[Output(self.id(key + "Range"), prop) for key, _, _ in numeric_specs for prop in ["min", "max", "step", "marks", "disabled"]],
            Output(self.id("range-context"), "data"),
            Output(self.id("periodic-request"), "data"), Output(self.id("url-warning"), "children"),
            Output(self.id("example"), "value"),
            Input(self.id("reset"), "n_clicks"), Input("mp-url", "hash"), Input(self.id("index-ready"), "data"),
            Input(self.id("example"), "value"),
        )
        def restore(reset_clicks, url_hash, ready, example):
            if ctx.triggered_id == self.id("example") and not example:
                raise PreventUpdate
            try:
                resetting = ctx.triggered_id == self.id("reset")
                choosing_example = ctx.triggered_id == self.id("example")
                filters = (normalize({"query": identifier(example), "searchMode": "identifier"}) if choosing_example
                           else normalize({}) if resetting else url_state(url_hash)[0])
                warning = "" if resetting or choosing_example else retired_filter_warning(url_hash)
                if choosing_example or (filters["query"] and "elements" not in parse_qs((url_hash or "").lstrip("#"))):
                    filters["elements"] = text_elements(filters["query"], filters["searchMode"])
            except (ValueError, TypeError):
                filters, warning = normalize({}), "This search link is invalid or unsupported. Default filters are shown."
            request = {"key": [filters["query"], filters["searchMode"]],
                       "elements": filters["elements"], "revision": str(time.monotonic_ns())}
            values, properties, range_context = slider_settings(filters, (ready or {}).get("ranges", {}))
            return (*[filters[key] for key in ui_control_keys if not key.endswith("Range")], *values,
                    *properties, range_context, request, warning, example if choosing_example else None)

        @app.callback(
            Output(self.id("periodic-context"), "enabledElements"), Output(self.id("search-context"), "data"),
            Input(self.id("search"), "value"), Input(self.id("searchMode"), "value"),
            Input(self.id("periodic-request"), "data"), Input(self.id("periodic"), "state"),
            Input(self.id("search"), "n_submit"),
            State(self.id("search-context"), "data"),
        )
        def synchronize_elements(query, mode, request, periodic_state, _submitted, previous):
            key = [(query or "").strip(), mode or "all"]
            request, previous = request or {}, previous or {}
            if request.get("key") == key and request.get("revision") != previous.get("request"):
                selected = elements({"enabledElements": request.get("elements", [])})
            elif previous.get("key") == key and ctx.triggered_id == self.id("periodic"):
                selected = elements(periodic_state)
                if selected == previous.get("elements"):
                    raise PreventUpdate
                # Keep the provider controlled, including manual edits. Otherwise
                # returning to the same automatic list can leave old UI choices.
                return selected, {**previous, "elements": selected}
            elif ctx.triggered_id in {self.id("search"), self.id("searchMode")} or previous.get("key") != key:
                # Even A -> B -> A edits replace prior manual choices; do not
                # mistake a returned-to query for an unchanged filter session.
                selected = text_elements(*key)
            else:
                raise PreventUpdate
            context = {"key": key, "request": request.get("revision"), "elements": selected}
            return selected, context

        def result_row(row):
            # Keep one size per cluster, including repeats. Point-group records
            # describe labels, not individual clusters: summarize distinct symbols
            # consistently without changing the underlying label records.
            point_groups = sorted({group["symbol"] for group in row["clusterPointGroups"]})
            fields = ["id", "identifier", "formula", "numberOfClusters", "predictedDimensionality", "energy_above_hull"]
            return {**{key: row[key] for key in fields},
                    "clusterSizes": [cluster["size"] for cluster in row["clusters"]],
                    "pointGroups": point_groups,
                    "scientificParts": {
                        "formula": scientific_tokens(row["formula"], "formula"),
                        "pointGroups": scientific_tokens(point_groups, "point_group"),
                        "energy_above_hull": scientific_tokens(display(row["energy_above_hull"]), "number"),
                    }}

        @app.callback(
            Output(self.id("results"), "rowData"), Output(self.id("result-count"), "children"),
            Output(self.id("page"), "data"), Output(self.id("page-label"), "children"),
            Output(self.id("previous"), "disabled"), Output(self.id("next"), "disabled"),
            Output(self.id("project-status"), "children"), Output(self.id("elements-label"), "children"),
            Output(self.id("statistics"), "children"),
            Output(self.id("data-availability"), "children"),
            *filter_inputs, Input(self.id("index-ready"), "data"),
            Input(self.id("previous"), "n_clicks"), Input(self.id("next"), "n_clicks"),
            State(self.id("page"), "data"), State(self.id("selection"), "data"),
        )
        def search(*args):
            values = args[:len(ui_control_keys)]
            range_context, search_context, ready, previous, next_click, page, selected = args[len(ui_control_keys):]
            if not ready:
                return [], "Loading…", 0, "", True, True, "", "", [], ""
            try:
                if ready.get("error"):
                    raise ValueError("Index unavailable")
                filters = form_state(values, range_context, search_context)
                rows, projection = matching_rows(filters), data_index()
                page = max(0, min(int(page or 0), max(0, (len(rows) - 1) // page_size)))
                if ctx.triggered_id == self.id("next"):
                    page = min(page + 1, max(0, (len(rows) - 1) // page_size))
                elif ctx.triggered_id == self.id("previous"):
                    page = max(0, page - 1)
                else:
                    page = 0
                block = rows[page * page_size:(page + 1) * page_size]
                grid = [result_row(row) for row in block]
                stats = [
                    ("Cluster materials", len(rows)), ("Search pairs", len({r["compoundSystem"] for r in rows})),
                    ("Isolated clusters", sum(r["allIsolated"] is True for r in rows)),
                    ("Stable materials", sum(r["energy_above_hull"] is not None and r["energy_above_hull"] < .01 for r in rows)),
                    ("Flat-band materials", sum(r["hasFlatData"] for r in rows)),
                    ("Battery materials", sum(r["hasBatteryData"] is True for r in rows)),
                ]
                stat_notes = {
                    "Isolated clusters": "Materials for which every contributed cluster is isolated.",
                    "Stable materials": "Materials with known energy above hull < 0.01 eV/atom; unknown values are excluded.",
                }
                cards = []
                for label, value in stats:
                    caption = scientific_text(label, "unit")
                    if label in stat_notes:
                        caption.title = stat_notes[label]
                    cards.append(html.Div([caption, html.Strong(f"{value:,}")],
                                          className="ce-stat", title=stat_notes.get(label)))
                return (grid, f"{len(rows):,} matches", page, f"Page {page + 1} of {max(1, (len(rows) + page_size - 1) // page_size)}",
                        page == 0, (page + 1) * page_size >= len(rows),
                        "",
                        "Elements: " + ", ".join(filters["elements"]) if filters["elements"] else "",
                        cards, availability_note(projection, rows))
            except Exception as error:
                logger.warning("Search failed: %s", type(error).__name__)
                message = "Unable to load the verified MP index." if ready.get("error") else "Invalid filter: check numeric bounds and selections."
                return [], "Unavailable", 0, "", True, True, message, "", [], ""

        app.clientside_callback(
            "function(rows,selected) {return (rows||[]).filter(row=>row.identifier===selected);}",
            Output(self.id("results"), "selectedRows"), Input(self.id("results"), "rowData"),
            Input(self.id("selection"), "data"),
        )

        @app.callback(
            Output(self.id("share-search"), "href"), *filter_inputs,
            Input(self.id("selection"), "data"),
        )
        def search_link(*args):
            try:
                return share_url(form_state(args[:-3], args[-3], args[-2]), args[-1])
            except (ValueError, TypeError):
                return ""

        @app.callback(
            Output(self.id("selection"), "data"),
            Input(self.id("results"), "cellClicked"), Input(self.id("example"), "value"), Input("mp-url", "hash"),
        )
        def select(cell, example, url_hash):
            if ctx.triggered_id == self.id("results") and cell:
                value = next((r["identifier"] for r in data_index()["rows"] if r["id"] == cell.get("rowId")), "invalid")
            elif ctx.triggered_id == self.id("example"):
                if not example:
                    raise PreventUpdate
                value = example
            else:
                try:
                    value = url_state(url_hash)[1]
                except (TypeError, ValueError):
                    value = "mp-1003545"
            try:
                value = identifier(value)
            except ValueError:
                value = "invalid"
            return value

        @cache.memoize(timeout=1800)
        @cached(TTLCache(maxsize=32, ttl=1800), condition=Condition())
        def material_payload(mid):
            identifier(mid)
            projection = data_index()
            row = next((r for r in projection["rows"] if r["identifier"] == mid), None)
            if row is None:
                raise ValueError("Material is not in the public contributed dataset")
            native, notice = None, ""
            try:
                rester = get_rester(use_document_model=False, mute_progress_bars=True)
                for deprecated in [False, True]:
                    docs = rester.materials.summary.search(material_ids=[mid], deprecated=deprecated,
                        fields=["material_id", "formula_pretty", "deprecated", "structure", "band_gap", "energy_above_hull",
                                "formation_energy_per_atom", "total_magnetization", "symmetry", "database_IDs"])
                    if docs:
                        if len(docs) != 1 or alias(docs[0]["material_id"]) != alias(mid):
                            raise ValueError("Ambiguous material association")
                        native = dict(docs[0])
                        structure = native.get("structure")
                        if isinstance(structure, Structure):
                            native["structure"] = structure.as_dict()
                        if native.get("deprecated"):
                            notice = "Deprecated MP record. The original contribution identifier is retained; native MP may return an encoded alias."
                        break
            except Exception as error:
                logger.warning("Native material read failed: %s", type(error).__name__)
                notice = "Native MP structure could not be retrieved. Contributed results remain accessible."
            return {"identifier": mid, "row": copy.deepcopy(row), "native": native, "notice": notice,
                    "revision": projection["revision"], "builtAt": projection["builtAt"]}

        @app.callback(Output(self.id("material-response"), "data"), Input(self.id("selection"), "data"))
        def fetch_material(mid):
            if not mid:
                raise PreventUpdate
            try:
                return material_payload(mid)
            except Exception as error:
                logger.warning("Material read failed: %s", type(error).__name__)
                return {"identifier": mid, "error": True}

        @app.callback(
            Output(self.id("material-details"), "children"), Output(self.id("contribution-tables"), "children"),
            Output(self.id("structure-status"), "children"), Output(self.id("selected-badge"), "children"),
            Input(self.id("material-response"), "data"), Input(self.id("selection"), "data"),
        )
        def render_material(payload, mid):
            if not payload or payload.get("identifier") != mid:
                if ctx.triggered_id == self.id("selection"):
                    return "Loading selected material…", [], "", mid or ""
                raise PreventUpdate
            if payload.get("error"):
                return html.Div("This material could not be loaded from the contributed dataset. Check the identifier and connection.",
                                className="notification is-warning"), [], "", mid
            row, native = payload["row"], payload.get("native") or {}

            def properties(entries):
                return html.Dl([html.Div([
                    html.Dt(scientific_text(entry[0], "unit"), title=entry[3] if len(entry) > 3 else None),
                    html.Dd(scientific_text(display(entry[1]), entry[2] if len(entry) > 2 else "number")),
                ]) for entry in entries], className="ce-properties")

            def external(label, url):
                return html.A(label, href=url, target="_blank", rel="noopener noreferrer")

            cards = [
                html.Div([
                    html.H3(scientific_text(row["formula"], "formula"), className="title is-3"),
                    html.Div([
                        external("Open MP material", f"https://materialsproject.org/materials/{row['nativeIdentifier']}"),
                        external("Open contribution", f"https://next-gen.materialsproject.org/contribs/contributions/{row['id']}"),
                        html.A("Share this material", href=share_url(normalize({}), mid)),
                        *icsd_links(native),
                    ], className="ce-links"),
                    properties([
                        ("MP ID", mid),
                        ("Search pair", row["compoundSystem"]), ("Clusters", row["numberOfClusters"]),
                        ("Cluster sizes", row["clusterSizes"]), ("Cluster point groups", row["pointGroups"], "point_group", "Each distinct recorded point group is shown once; not one entry per cluster."),
                        ("Sub-lattice space group", row["clusterLatticeSpaceGroup"], "space_group"),
                        ("Sub-lattice dimension", row["predictedDimensionality"]),
                        ("Min. mean distance (Å)", row["minimumAverageDistance"]),
                        ("Polar class", row["isPolar"]), ("Piezoelectric class", row["isPiezoelectric"], "text", "Permitted by crystal symmetry; not a measured response."),
                        ("Enantiomorphic class", row["isEnantiomorphic"]),
                    ]),
                ], className="ce-detail-card"),
                html.Div([
                    html.H3("Materials Project", className="title is-5"),
                    properties([
                        ("Band gap (eV)", row["band_gap"]), ("Energy above hull (eV/atom)", row["energy_above_hull"]),
                        ("Formation energy (eV/atom)", row["formation_energy_per_atom"]),
                        ("Total magnetization (μB/cell)", row["total_magnetization"]),
                        ("Crystal space group", (row.get("nativeSymmetry") or {}).get("symbol"), "space_group"),
                        ("Battery material", row["hasBatteryData"], "text", "Based on the contributed battery snapshot; current MP battery records are fetched separately."),
                    ]),
                    external("Open MP Battery Explorer", "https://materialsproject.org/batteries"),
                ], className="ce-detail-card"),
            ]
            if row["hasFlatData"]:
                flat = row["flatBand"]
                labels = {"sublatticeElement": "Sublattice element", "numberOfFlatBands": "Flat bands", "sitesInSublattice": "Sites",
                          "latticeDimensionalities": "Dimensions", "latticeIds": "Lattice IDs", "remainsFlatWithDecay": "Flat with decay"}
                cards.append(html.Div([
                    html.H3("Flat bands", className="title is-5"),
                    properties([(label, flag(flat.get(key)) if key == "remainsFlatWithDecay" else flat.get(key)) for key, label in labels.items()]),
                    external("Neves et al. (2024)", "https://doi.org/10.1038/s41524-024-01220-x"),
                ], className="ce-detail-card"))
            tables = []
            for name in ["clusters", "clusterPointGroups"]:
                rows = row[name]
                columns = [column for column in rows[0] if column != "materialId"]
                tables.append(html.Section([
                    html.H3("Clusters" if name == "clusters" else "Cluster point groups", className="title is-5"),
                    html.Div(html.Table([
                        html.Thead(html.Tr([html.Th(scientific_text(table_labels[column], "unit")) for column in columns])),
                        html.Tbody([html.Tr([html.Td(scientific_text(display(record[column]), "point_group" if name == "clusterPointGroups" and column == "symbol" else "number")) for column in columns]) for record in rows]),
                    ], className="table is-fullwidth is-striped"), className="table-container"),
                ], className="ce-table-section", **{"data-table": name}))
            return html.Div(cards, className="ce-detail-grid"), tables, payload.get("notice") or "", mid

        def csv_text(records, columns):
            output = io.StringIO()
            writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            for row in records:
                safe = {}
                for key in columns:
                    value = row.get(key)
                    if isinstance(value, (dict, list, tuple)):
                        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
                    if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
                        value = "'" + value
                    safe[key] = value
                writer.writerow(safe)
            return output.getvalue()

        @app.callback(
            Output(self.id("download"), "data"), Output(self.id("export-status"), "children"),
            Input(self.id("export"), "n_clicks"), Input(self.id("export-tables"), "n_clicks"),
            *filter_states, prevent_initial_call=True,
        )
        def export(csv_clicks, table_clicks, *args):
            try:
                filters = form_state(args[:-2], args[-2], args[-1])
                rows, projection = matching_rows(filters), data_index()
                columns = ["id", "identifier", "nativeIdentifier", "formula", "compoundSystem", "elements", "numberOfClusters",
                           "clusterSizes", "pointGroups", "clusterLatticeSpaceGroup", "predictedDimensionality", "minimumAverageDistance",
                           "allIsolated", "isPolar", "isPiezoelectric", "isEnantiomorphic", "hasFlatData", "hasBatteryData",
                           "band_gap", "energy_above_hull", "formation_energy_per_atom", "total_magnetization",
                           "nativeAvailable", "deprecated", "flatBand"]
                materials = csv_text(rows, columns)
                if ctx.triggered_id == self.id("export"):
                    return dcc.send_string(materials, "cluster_explorer_matches.csv"), f"Last export: all {len(rows):,} materials matching the filters at download time."
                manifest = {
                    "project": project_key, "schema": "cluster-explorer-export-v1", "indexRevision": projection["revision"],
                    "indexBuiltAt": projection["builtAt"], "exportedAt": datetime.now(timezone.utc).isoformat(),
                    "diagnostics": {"coldIndexSeconds": projection["coldSeconds"], "nativeBatchErrors": projection["nativeErrors"]},
                    "filters": filters, "materials": len(rows), "nativeKnown": sum(r["nativeAvailable"] for r in rows),
                    "units": {"averageDistance": "angstrom", "minimumAverageDistance": "angstrom", "band_gap": "eV",
                              "energy_above_hull": "eV/atom", "formation_energy_per_atom": "eV/atom", "total_magnetization": "mu_B/cell"},
                    "semantics": "Size and connectivity match one cluster row; point groups are material-level coexistence.",
                    "references": ["https://github.com/RajbanulAkhond/Cluster_Finder", "https://arxiv.org/abs/2601.04460", "https://doi.org/10.1038/s41524-024-01220-x"],
                    "sources": [{"identifier": r["identifier"], "contributionId": r["id"], "lastModified": r["lastModified"], "tableChecksums": r["tableChecksums"]} for r in rows],
                }
                buffer = io.BytesIO()
                with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                    archive.writestr("materials.csv", materials)
                    for name, fields in [("clusters", ["materialId", "size", "averageDistance", "elements", "isExtended", "isShared"]),
                                         ("clusterPointGroups", ["materialId", "label", "symbol"])]:
                        archive.writestr(name + ".csv", csv_text([child for r in rows for child in r[name]], fields))
                    archive.writestr("provenance.json", json.dumps(manifest, indent=2))
                return dcc.send_bytes(buffer.getvalue(), "cluster_explorer_matches.zip"), f"Last export: {len(rows):,} materials with both tables and provenance."
            except Exception as error:
                logger.warning("Export unavailable: %s", type(error).__name__)
                return dash.no_update, "Export failed: verify filters and index availability."

        def supercell_dimensions(values):
            if len(values) != 3 or any(isinstance(v, bool) or v is None or number(v) != int(number(v)) or not 1 <= int(v) <= 3 for v in values):
                raise ValueError("Supercell dimensions must be integers from 1 to 3")
            return tuple(int(v) for v in values)

        def parse_site_references(value):
            """Same bounded, ordered grammar as the Lux ClusterMembership model."""
            if not isinstance(value, str) or not 0 < len(value) <= 200000:
                raise ValueError("Invalid site references")
            tokens = value.split(";")
            if len(tokens) > 20000:
                raise ValueError("Too many cluster members")
            members = []
            for token in tokens:
                match = re.fullmatch(r"(0|[1-9][0-9]*)(?:@\((-?(?:0|[1-9][0-9]*)),(-?(?:0|[1-9][0-9]*)),(-?(?:0|[1-9][0-9]*))\))?", token.strip())
                if match is None:
                    raise ValueError("Invalid site/image notation")
                member = tuple(int(v) if v is not None else 0 for v in match.groups())
                if any(abs(v) > 100000 for v in member):
                    raise ValueError("Site/image exceeds the reviewed budget")
                members.append(member)
            if len(members) != len(set(members)):
                raise ValueError("Duplicate cluster member")
            return members

        def normalize_memberships(rows, mid):
            if not isinstance(rows, list) or not 0 < len(rows) <= 20000:
                raise ValueError("Missing or oversized memberships")
            clean = []
            for row in rows:
                if set(row) != {"materialId", "clusterIndex", "siteReferences", "pointGroupRow"} or row["materialId"] != mid:
                    raise ValueError("Cross-material or malformed membership")
                indices = []
                for key in ("clusterIndex", "pointGroupRow"):
                    value = number(row[key])
                    if isinstance(scalar(row[key]), bool) or value is None or value != int(value) or not 0 <= value <= 100000:
                        raise ValueError("Invalid membership index")
                    indices.append(int(value))
                refs = parse_site_references(row["siteReferences"])
                text = "; ".join(str(i) + (f"@({a},{b},{c})" if (a,b,c) != (0,0,0) else "") for i,a,b,c in refs)
                clean.append({"materialId": mid, "clusterIndex": indices[0], "siteReferences": text, "pointGroupRow": indices[1]})
            if [r["clusterIndex"] for r in clean] != list(range(len(clean))):
                raise ValueError("Missing, reordered, or duplicate membership instances")
            return clean

        @cached(TTLCache(maxsize=32, ttl=900), condition=Condition())
        def remote_membership_rows(table_id, checksum, count):
            """Fetch only the selected material's viewer table, never at startup."""
            if not re.fullmatch(r"[0-9a-f]{24}", table_id) or not re.fullmatch(r"[0-9a-f]{32}", checksum) or isinstance(count, bool) or not isinstance(count, int) or not 0 < count <= 20000:
                raise ValueError("Invalid membership table identity or size")
            client = get_rester(use_document_model=False, mute_progress_bars=True).contribs
            answer = client.tables.queryTables(id__in=[table_id], per_page=1, data_per_page=1000,
                _fields=["_all"], _request_options={"connect_timeout": 10, "timeout": 60}).result()
            if len(answer["data"]) != 1:
                raise ValueError("Missing membership table")
            raw = answer["data"][0]
            if raw["id"] != table_id or raw["name"] != "clusterMemberships" or raw["md5"] != checksum or raw["total_data_rows"] != count:
                raise ValueError("Membership table changed")
            columns, values, index = raw["columns"], raw["data"], raw["index"]
            if len(values) != count:
                frame = client.get_table(table_id)
                if frame.attrs.get("md5") != checksum or frame.attrs.get("name") != "clusterMemberships":
                    raise ValueError("Membership pagination source changed")
                columns, values, index = list(frame.columns), frame.values.tolist(), list(frame.index)
            if columns != ["materialId", "clusterIndex", "siteReferences", "pointGroupRow"] or len(values) != count or list(map(str,index)) != [str(i) for i in range(count)] or any(len(v) != len(columns) for v in values):
                raise ValueError("Incomplete or unexpected membership table")
            return [dict(zip(columns, v)) for v in values]

        def validate_highlight_file(record, mid):
            if not isinstance(record, dict) or record.get("project") != project_key or record.get("materialId") != mid or record.get("policyVersion") != "summary-highlight-v1" or record.get("structureHashAlgorithm") != "ordered-structure-sha256-v1":
                raise ValueError("Unsupported or cross-material local cache")
            if not isinstance(record.get("clusterHighlighting"), dict):
                raise ValueError("Missing highlighting metadata")
            return record

        def highlight_record(mid, row):
            """Remote metadata/table are authoritative; never mask a broken migration."""
            metadata, table = row.get("clusterHighlighting"), row.get("membershipTable")
            if metadata is not None or table is not None:
                if not isinstance(metadata, dict):
                    raise ValueError("Missing membership metadata")
                metadata = {k: scalar(v) for k,v in metadata.items()}
                if flag(metadata.get("isAvailable")) is not True:
                    raise ValueError("Highlighting withheld")
                if not isinstance(table, dict):
                    raise ValueError("Missing membership table")
                memberships = remote_membership_rows(table["id"], table["md5"], table["total_data_rows"])
                return {"clusterHighlighting": metadata, "clusterMemberships": memberships}
            if not highlight_directory:
                raise ValueError("No highlighting data source")
            identifier(mid)
            root = Path(highlight_directory).expanduser().resolve()
            path = (root / (mid + ".json")).resolve()
            if path.parent != root:
                raise ValueError("Invalid local cache path")
            with path.open("rb") as stream:
                content = stream.read(2000001)
            if len(content) > 2000000:
                raise ValueError("Local cache exceeds reviewed size budget")
            return validate_highlight_file(json.loads(content), mid)

        def verified_cluster_details(mid, row, structure):
            """Check certified membership mapping; derive symbols/distances from source tables."""
            try:
                record = highlight_record(mid, row)
                meta = record["clusterHighlighting"]
                if flag(meta.get("isAvailable")) is not True or meta.get("policyVersion") != "summary-highlight-v1" or meta.get("reason") is not None:
                    raise ValueError("Highlighting withheld")
                hashed = {"algorithm": "ordered-structure-sha256-v1",
                    "lattice": [[float(v) for v in vector] for vector in structure.lattice.matrix],
                    "sites": [{"species": sorted([[str(e),float(n)] for e,n in s.species.items()]),
                               "fractional": [float(v) for v in s.frac_coords]} for s in structure]}
                digest = hashlib.sha256(json.dumps(hashed,sort_keys=True,separators=(",",":"),allow_nan=False).encode()).hexdigest()
                checksums = row["tableChecksums"]
                if meta.get("structureHash") != digest or isinstance(scalar(meta.get("numberOfSites")), bool) or number(meta.get("numberOfSites")) != len(structure) or meta.get("clustersTableMd5") != checksums["clusters"] or meta.get("pointGroupsTableMd5") != checksums["clusterPointGroups"]:
                    raise ValueError("Highlighting source changed")
                memberships = normalize_memberships(record["clusterMemberships"], mid)
                clusters, groups = row["clusters"], row["clusterPointGroups"]
                if len(memberships) != len(clusters):
                    raise ValueError("Membership instance coverage differs")
                details, symbols = {}, []
                for membership in memberships:
                    index, group_row = membership["clusterIndex"], membership["pointGroupRow"]
                    if group_row >= len(groups):
                        raise ValueError("Point-group reference out of range")
                    symbol = groups[group_row]["symbol"]
                    refs = parse_site_references(membership["siteReferences"])
                    if any(site >= len(structure) or not structure[site].is_ordered for site,*_ in refs):
                        raise ValueError("Membership site out of bounds or disordered")
                    elements = [structure[site].specie.symbol for site,*_ in refs]
                    cluster = clusters[index]
                    if len(refs) != cluster["size"] or sorted(elements) != sorted(cluster["elements"].split(",")):
                        raise ValueError("Cluster size/composition mismatch")
                    symbols.append(symbol)
                    for site,a,b,c in refs:
                        position = structure.lattice.get_cartesian_coords(structure[site].frac_coords + [a,b,c])
                        details.setdefault(site, []).append({
                            "clusterIndex": index, "pointGroup": symbol,
                            "averageDistance": number(cluster["averageDistance"]), "position": position.tolist(),
                        })
                expected = [g["symbol"] for g in groups]
                if set(symbols) != set(expected) or Counter(expected) - Counter(symbols):
                    raise ValueError("Point-group symbol coverage differs")
                return details, ""
            except Exception as error:
                logger.debug("Cluster highlighting unavailable (%s): %s",mid,type(error).__name__)
                return {}, "Cluster highlighting isn't available for this material"

        def verified_cluster_sites(mid, row, structure):
            details, warning = verified_cluster_details(mid, row, structure)
            return set(details), warning

        @cache.memoize(timeout=900)
        @cached(TTLCache(maxsize=12, ttl=900), condition=Condition())
        def display_graph(mid, dimensions):
            payload = material_payload(mid)
            raw = (payload.get("native") or {}).get("structure")
            if not raw:
                raise ValueError("Native structure is unavailable")
            structure = Structure.from_dict(raw)
            structure.add_site_property("_ce_parent_site", list(range(len(structure))))
            if len(structure) * math.prod(dimensions) > 2000:
                raise ValueError("Supercell exceeds the 2,000-site preview limit")
            if any(v != 1 for v in dimensions):
                structure.make_supercell(dimensions)
            # Use the current public constructor, not CT's removed with_* path.
            graph = StructureGraph.from_local_env_strategy(structure, JmolNN())
            if graph.graph.number_of_edges() > 24000:
                raise ValueError("Structure exceeds the 24,000-edge preview limit")
            return graph.as_dict()

        @lru_cache(maxsize=16)
        def context_sphere_mesh(segments, rings, phi_start=0, phi_length=math.tau):
            """Immutable smooth sphere triangles for the public surface primitive.

            The pinned renderer ignores opacity on spheres/cylinders, but honors
            it on surfaces. Cache only unit geometry, never material/user state.
            """
            grid = [[(math.sin(math.pi * i / rings) * math.cos(phi_start + phi_length * j / segments),
                      math.sin(math.pi * i / rings) * math.sin(phi_start + phi_length * j / segments),
                      math.cos(math.pi * i / rings)) for j in range(segments + 1)] for i in range(rings + 1)]
            triangles = []
            for i in range(rings):
                for j in range(segments):
                    if i < rings - 1:
                        triangles.extend((grid[i][j], grid[i + 1][j], grid[i + 1][j + 1]))
                    if i > 0:
                        triangles.extend((grid[i][j], grid[i + 1][j + 1], grid[i][j + 1]))
            return tuple(triangles)

        def normalize_element_colors(value):
            if value is None:
                return {}
            if not isinstance(value, dict) or len(value) > 118:
                raise ValueError("Invalid element color overrides")
            for symbol, color in value.items():
                if not isinstance(symbol, str) or str(Element(symbol)) != symbol or not isinstance(color, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
                    raise ValueError("Element colors must be valid element symbols and hex colors")
            return {symbol: color.lower() for symbol, color in value.items()}

        def scene_from_graph(graph, representation, colors, atom_scale, bond_scale, draw, cluster_sites=frozenset(), cluster_details=None, element_colors=None):
            cluster_details = cluster_details or {}
            element_colors = normalize_element_colors(element_colors)
            if representation not in {"Default", "Ball and Stick", "Space Filling", "Wireframe"} or colors not in {"VESTA", "Jmol", "accessible"}:
                raise ValueError("Invalid display style")
            atom_scale, bond_scale = number(atom_scale), number(bond_scale)
            if atom_scale is None or bond_scale is None or not .1 <= atom_scale <= 2 or not .5 <= bond_scale <= 4:
                raise ValueError("Invalid atom/bond scale")
            if not isinstance(draw, list) or not set(draw).issubset({"atoms", "bonds", "unit_cell", "legend", "labels", "axes", "magmoms", "rotate", "clusters"}):
                raise ValueError("Invalid display options")
            class ViewerLegend(Legend):
                def get_color(self, sp, site=None):
                    return element_colors.get(sp.symbol) or super().get_color(sp, site=site)

            legend = ViewerLegend(graph.structure, color_scheme=colors,
                            radius_scheme="van_der_waals" if representation == "Space Filling" else "uniform")
            highlighting = bool(cluster_sites and "clusters" in draw and "atoms" in draw and representation != "Wireframe")
            scene = graph.get_scene(legend=legend, draw_image_atoms=True, bonded_sites_outside_unit_cell=False,
                                    hide_incomplete_edges=True, bond_radius=.1 * bond_scale,
                                    site_get_scene_kwargs={"draw_magmoms": "magmoms" in draw, "draw_polyhedra": False,
                                                           "retain_atom_idx": True},
                                    explicitly_calculate_polyhedra_hull=False).to_json()

            def muted_color(color):
                if not isinstance(color, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
                    color = "#c8cdd4"
                return "#" + "".join(f"{round(.5 * int(color[i:i + 2], 16) + .5 * gray):02x}"
                                      for i, gray in zip((1, 3, 5), (200, 205, 212)))

            def context_surface(node):
                """Retain picking metadata and real transparency, not a CSS fade.

                Bound tessellation in larger supercells. Cylinder ends meet atom
                cores or the adjacent half-bond, so no extra caps are necessary.
                """
                positions, normals = [], []
                if node["type"] == "spheres":
                    segments, rings = (16, 10) if len(graph.structure) <= 128 else (10, 6)
                    unit = context_sphere_mesh(segments, rings, node.get("phiStart", 0), node.get("phiEnd", math.tau))
                    radius = node["radius"]
                    for center in node["positions"]:
                        positions.extend([[round(c + radius * n, 6) for c, n in zip(center, normal)] for normal in unit])
                        normals.extend(unit)
                    reference = "muted-context-atoms"
                else:
                    pairs = node["positionPairs"]
                    segments = 12 if len(pairs) <= 512 else 6
                    circle = [(math.cos(math.tau * i / segments), math.sin(math.tau * i / segments)) for i in range(segments)]
                    for index, (start, end) in enumerate(pairs):
                        delta = [b - a for a, b in zip(start, end)]
                        length = math.sqrt(sum(v * v for v in delta))
                        if length < 1e-10:
                            continue
                        axis = [v / length for v in delta]
                        helper = [1, 0, 0] if abs(axis[0]) < .9 else [0, 1, 0]
                        u = [axis[1] * helper[2] - axis[2] * helper[1], axis[2] * helper[0] - axis[0] * helper[2], axis[0] * helper[1] - axis[1] * helper[0]]
                        norm = math.sqrt(sum(v * v for v in u))
                        u = [v / norm for v in u]
                        v = [axis[1] * u[2] - axis[2] * u[1], axis[2] * u[0] - axis[0] * u[2], axis[0] * u[1] - axis[1] * u[0]]
                        radial = [[c * a + s * b for a, b in zip(u, v)] for c, s in circle]
                        radii = []
                        for key in ("radiusBottom", "radiusTop"):
                            value = node.get(key, node.get("radius", 1))
                            radii.append(value[index] if isinstance(value, list) else value)
                        ends = [[[c + radius * n for c, n in zip(center, normal)] for normal in radial]
                                for center, radius in zip((start, end), radii)]
                        for i in range(segments):
                            j = (i + 1) % segments
                            for side, point in ((0, i), (1, i), (1, j), (0, i), (1, j), (0, j)):
                                positions.append([round(x, 6) for x in ends[side][point]])
                                normals.append(radial[point])
                    reference = "muted-context-bonds"
                node["color"] = muted_color(node.get("color"))
                node.update(type="surface", positions=positions, normals=[[round(v, 6) for v in n] for n in normals],
                            opacity=.9 if reference == "muted-context-atoms" else .65, reference=reference)
                for key in ("radius", "radiusTop", "radiusBottom", "positionPairs", "phiStart", "phiEnd"):
                    node.pop(key, None)

            def same_cluster_bond(pair, metadata):
                indices = metadata.get("atom_idx", []) if isinstance(metadata, dict) else []
                if len(indices) != 2:
                    return False
                left, right = [graph.structure[i].properties["_ce_parent_site"] for i in indices]
                # CT renders each bond as atom -> midpoint. Recover its full
                # displacement and compare member images, so inter-cluster bonds
                # and the wrong periodic copies are never made opaque by color.
                displacement = [2 * (b - a) for a, b in zip(*pair)]
                return any(a["clusterIndex"] == b["clusterIndex"] and
                           math.dist(displacement, [y - x for x, y in zip(a["position"], b["position"])]) < 1e-6
                           for a in cluster_details.get(left, []) for b in cluster_details.get(right, []))

            def split_bonds(node):
                groups = {}
                metadata = node.get("_meta", [])
                for i, pair in enumerate(node["positionPairs"]):
                    meta = metadata[i] if i < len(metadata) else {}
                    color = None
                    if same_cluster_bond(pair, meta):
                        element = graph.structure[meta["atom_idx"][0]].specie.symbol
                        color = element_colors.get(element, "#f07818")
                    groups.setdefault(color, []).append(i)
                result = []
                for color, indices in groups.items():
                    if not indices:
                        continue
                    part = copy.deepcopy(node)
                    part["positionPairs"] = [node["positionPairs"][i] for i in indices]
                    part["_meta"] = [metadata[i] if i < len(metadata) else {} for i in indices]
                    for key in ("radiusTop", "radiusBottom"):
                        if isinstance(node.get(key), list):
                            part[key] = [node[key][i] for i in indices]
                    if color is not None:
                        part.update(reference="cluster-bonds", color=color, opacity=1)
                    result.append(part)
                return result

            def style_node(node, parent=""):
                name = node.get("name", parent)
                if name in {"atoms", "bonds", "unit_cell"}:
                    node["visible"] = name in draw and not (name == "atoms" and representation == "Wireframe") and not (name == "bonds" and representation == "Space Filling")
                if node.get("type") == "spheres" and node.get("radius") is not None:
                    node["radius"] *= atom_scale * (.8 if representation == "Ball and Stick" else 1)
                    # CT's documented retained atom index follows each rendered
                    # periodic image. Map it to the verified parent, not its color
                    # or a nearest-position guess (which can confuse equal elements).
                    index = node["_meta"][0]["atom_idx"][0]
                    parent_site = graph.structure[index].properties["_ce_parent_site"]
                    selected = parent_site in cluster_sites
                    # Publish only our own atom tag through the component's public
                    # selectedObject prop. Bonds never receive an editable tag.
                    match = re.match(r"([A-Z][a-z]?)", node.get("tooltip", ""))
                    element = match[1] if match and match[1] in {sp.symbol for sp in graph.structure[index].species} else None
                    if element:
                        node.update(ceElement=element, ceColor=element_colors.get(element,
                            "#f07818" if highlighting and selected else node["color"]))
                    node["tooltip"] = re.sub(r" \(_ce_parent_site = \d+\)", "", node.get("tooltip", ""))
                    if selected and cluster_details.get(parent_site):
                        # One entry per cluster instance, even when an atom has
                        # multiple periodic member images in the same cluster.
                        memberships = {m["clusterIndex"]: m for m in cluster_details[parent_site]}
                        node["tooltip"] = "\n\n".join(
                            f"{graph.structure[index].species_string} · Cluster {cluster + 1}\n"
                            f"Cluster point group: {m['pointGroup']}\n"
                            f"Mean distance: {m['averageDistance']:.4f} Å"
                            for cluster, m in sorted(memberships.items()))
                    if highlighting:
                        if selected:
                            node["color"] = element_colors.get(element, "#f07818")
                            node["radius"] *= 1.12 if representation == "Space Filling" else 1.25
                            if not cluster_details.get(parent_site):
                                node["tooltip"] += " · Cluster atom"
                        else:
                            context_surface(node)
                if highlighting and name == "bonds" and node.get("type") == "cylinders":
                    if node.get("reference") != "cluster-bonds":
                        # Half-width context bonds; the width slider still scales
                        # both kinds, while intra-cluster bonds keep full width.
                        for key in ("radius", "radiusTop", "radiusBottom"):
                            if key in node:
                                value = node[key]
                                node[key] = [v * .5 for v in value] if isinstance(value, list) else value * .5
                        context_surface(node)
                if name == "magmoms" and node.get("type") == "arrows":
                    node["color"] = "#000000"
                if "contents" in node:
                    node["contents"] = [child for child in node["contents"] if child.get("name") != "polyhedra"]
                    if highlighting and name == "bonds":
                        node["contents"] = [part for child in node["contents"] for part in
                                            (split_bonds(child) if child.get("type") == "cylinders" else [child])]
                for child in node.get("contents", []):
                    style_node(child, name)
            style_node(scene)
            scene["name"] = "cluster-explorer-crystal"
            if "labels" in draw:
                # The installed renderer positions label primitives through scene
                # origins; it ignores the Label.position field. Match atom origins.
                scene["contents"].append({"name": "labels", "origin": scene["origin"], "contents": [
                    {"name": f"site-label-{i}", "origin": site.coords.tolist(),
                     "contents": [{"type": "labels", "label": site.species_string}]}
                    for i, site in enumerate(list(graph.structure)[:256])
                ]})
            # The renderer's orbit controller has a fixed Y-up convention.
            # Map display coordinates (x,y,z) -> (x,z,-y), so physical Cartesian
            # z is its up axis. Never rotate the source Structure or exports.
            def viewer_coordinates(value):
                # A batch of three vectors is not itself an x/y/z vector.
                if len(value) == 3 and all(isinstance(v, Real) and number(v) is not None for v in value):
                    return [float(value[0]), float(value[2]), -float(value[1])]
                return [viewer_coordinates(v) for v in value]

            def rotate_display(node):
                for key in ("origin", "positions", "positionPairs", "controlPoints", "rotate_to", "normals"):
                    if key in node and node[key] is not None:
                        node[key] = viewer_coordinates(node[key])
                for child in node.get("contents", []):
                    rotate_display(child)
            rotate_display(scene)
            cluster_elements, background_elements = set(), {}
            for site in graph.structure:
                in_cluster = highlighting and site.properties["_ce_parent_site"] in cluster_sites
                for sp in site.species:
                    if in_cluster:
                        cluster_elements.add(sp.symbol)
                    else:
                        color = legend.get_color(sp, site=site)
                        background_elements.setdefault(sp.symbol, muted_color(color) if highlighting else color)
            chips = []
            if "legend" in draw:
                if cluster_elements:
                    swatches = {}
                    for symbol in sorted(cluster_elements):
                        swatches.setdefault(element_colors.get(symbol, "#f07818"), []).append(symbol)
                    chips.append(html.Span([
                        *[html.Span("●", title=", ".join(symbols), style={"color": color, "marginRight": ".2rem"})
                          for color, symbols in swatches.items()],
                        " Cluster atoms (" + ", ".join(sorted(cluster_elements)) + ")",
                    ], className="ce-element-chip"))
                chips.extend(html.Span([html.Span("●", style={"color": color, "opacity": .9 if highlighting else 1}),
                                        " " + symbol], className="ce-element-chip")
                             for symbol, color in sorted(background_elements.items()))
            return scene, chips

        @app.callback(
            Output(self.id("viewer-response"), "data"),
            Input(self.id("material-response"), "data"), Input(self.id("selection"), "data"),
            *[Input(self.id("supercell-" + axis), "value") for axis in "xyz"],
            Input(self.id("representation"), "value"), Input(self.id("colors"), "value"),
            Input(self.id("atom-scale"), "value"), Input(self.id("bond-scale"), "value"), Input(self.id("draw"), "value"),
            Input(self.id("element-colors"), "data"),
        )
        def render_crystal(payload, mid, x, y, z, representation, colors, atom_scale, bond_scale, draw, element_colors):
            color_pairs = sorted(element_colors.items()) if isinstance(element_colors, dict) else []
            response = {"identifier": mid, "params": [x, y, z, representation, colors, atom_scale, bond_scale, draw, color_pairs]}
            if not payload or payload.get("identifier") != mid:
                if ctx.triggered_id == self.id("selection"):
                    return {**response, "error": "Loading selected crystal…"}
                raise PreventUpdate
            if payload.get("error"):
                return {**response, "error": "No crystal is available for this selection."}
            try:
                dims = supercell_dimensions([x, y, z])
                source = material_payload(mid)
                raw = (source.get("native") or {}).get("structure")
                if raw:
                    cluster_details, highlight_warning = verified_cluster_details(mid, source["row"], Structure.from_dict(raw))
                else:
                    cluster_details, highlight_warning = {}, "Cluster highlighting isn't available for this material"
                cluster_sites = set(cluster_details)
                graph = StructureGraph.from_dict(copy.deepcopy(display_graph(mid, dims)))
                scene, chips = scene_from_graph(graph, representation, colors, atom_scale, bond_scale, draw, cluster_sites, cluster_details, element_colors)
                status = f"{len(graph.structure):,} sites · supercell {dims[0]}×{dims[1]}×{dims[2]}"
                if "labels" in draw and len(graph.structure) > 256:
                    status += " · labels limited to the first 256 sites"
                if "magmoms" in draw and "magmom" not in graph.structure.site_properties:
                    status += " · site magnetic moments unavailable"
                # Browser-side updates use this key to reject stale responses,
                # clean obsolete label overlays and preserve camera-only state.
                scene_key = hashlib.sha256(json.dumps([mid, dims, scene], sort_keys=True, cls=MontyEncoder).encode()).hexdigest()
                def tag_atoms(node):
                    if node.get("ceElement"):
                        node["ceViewTag"] = scene_key
                    for child in node.get("contents", []):
                        tag_atoms(child)
                tag_atoms(scene)
                return {**response, "scene": scene, "sceneKey": scene_key,
                        "legend": chips, "status": status, "rotate": "rotate" in draw,
                        "highlightWarning": highlight_warning, "highlightedParentSites": len(cluster_sites)}
            except ValueError as error:
                return {**response, "error": str(error)}
            except Exception as error:
                logger.warning("Crystal rendering failed: %s", type(error).__name__)
                return {**response, "error": "Crystal preview is unavailable. Contributed data remain accessible."}

        @app.callback(Output(self.id("highlight-status"), "children"),
            Input(self.id("viewer-response"), "data"), Input(self.id("selection"), "data"))
        def highlight_notice(response, mid):
            if not response or response.get("identifier") != mid:
                return ""
            return response.get("highlightWarning", "Cluster highlighting isn't available for this material" if response.get("error") else "")

        app.clientside_callback(
            dash.ClientsideFunction(namespace="cluster_explorer", function_name="presentViewer"),
            Output(self.id("crystal_scene"), "data"), Output(self.id("crystal_legend_container"), "children"),
            Output(self.id("viewer-wrapper"), "style"), Output(self.id("viewer-status"), "children"),
            Output(self.id("rotation-tick"), "disabled"),
            Output(self.id("crystal_scene"), "toggleVisibility"), Output(self.id("crystal_scene"), "customCameraState"),
            Output(self.id("viewer-applied"), "data"), Output(self.id("viewer-wrapper"), "data-scene-key"),
            Input(self.id("viewer-response"), "data"), Input(self.id("selection"), "data"),
            *[Input(self.id("supercell-" + axis), "value") for axis in "xyz"],
            Input(self.id("representation"), "value"), Input(self.id("colors"), "value"),
            Input(self.id("atom-scale"), "value"), Input(self.id("bond-scale"), "value"), Input(self.id("draw"), "value"),
            Input(self.id("element-colors"), "data"), Input(self.id("crystal_scene"), "selectedObject"),
            Input(self.id("rotation-tick"), "n_intervals"), Input(self.id("camera"), "value"),
            Input(self.id("reset-viewer"), "n_clicks"), State(self.id("crystal_scene"), "currentCameraState"),
            State(self.id("viewer-applied"), "data"),
        )

        app.clientside_callback(
            dash.ClientsideFunction(namespace="cluster_explorer", function_name="orientationCompass"),
            Output(self.id("orientation-axes"), "src"), Output(self.id("orientation-axes"), "style"),
            Input(self.id("crystal_scene"), "currentCameraState"), Input(self.id("draw"), "value"),
        )

        @app.callback(
            *[Output(self.id("supercell-" + axis), "value") for axis in "xyz"],
            Output(self.id("representation"), "value"), Output(self.id("colors"), "value"),
            Output(self.id("atom-scale"), "value"), Output(self.id("bond-scale"), "value"),
            Output(self.id("draw"), "value"), Output(self.id("camera"), "value"),
            Output(self.id("element-colors"), "data"),
            Input(self.id("reset-viewer"), "n_clicks"), prevent_initial_call=True,
        )
        def reset_viewer(_):
            return 1, 1, 1, "Default", "VESTA", 1, 1, ["atoms", "bonds", "unit_cell", "legend", "clusters"], "Perspective", {}

        @app.callback(Output(self.id("draw"), "options"), Input(self.id("representation"), "value"))
        def viewer_options(representation):
            return self.viewer_draw_options(representation)

        @app.callback(
            Output(self.id("structure-download"), "data"),
            Input(self.id("export-cif"), "n_clicks"), Input(self.id("export-structure"), "n_clicks"),
            Input(self.id("export-material"), "n_clicks"), State(self.id("selection"), "data"),
            *[State(self.id("supercell-" + axis), "value") for axis in "xyz"], prevent_initial_call=True,
        )
        def structure_export(cif_clicks, structure_clicks, material_clicks, mid, x, y, z):
            try:
                if ctx.triggered_id == self.id("export-material"):
                    payload = material_payload(identifier(mid))
                    return dcc.send_string(json.dumps({k: v for k, v in payload.items() if k != "native"}, indent=2), f"{mid}_contribution.json")
                dims = supercell_dimensions([x, y, z])
                structure = StructureGraph.from_dict(copy.deepcopy(display_graph(identifier(mid), dims))).structure
                structure.remove_site_property("_ce_parent_site")
                if ctx.triggered_id == self.id("export-cif"):
                    return dcc.send_string(structure.to(fmt="cif"), f"{mid}_{x}x{y}x{z}.cif")
                return dcc.send_string(json.dumps(structure.as_dict()), f"{mid}_{x}x{y}x{z}.json")
            except Exception:
                raise PreventUpdate

        @app.callback(
            Output(self.id("image-download"), "data"), Input(self.id("crystal_scene"), "imageDataTimestamp"),
            State(self.id("crystal_scene"), "imageData"), State(self.id("selection"), "data"), prevent_initial_call=True,
        )
        def download_image(timestamp, data, mid):
            if not data or not isinstance(data, str) or len(data) > 25000000:
                raise PreventUpdate
            return {"content": data.split(",")[-1], "filename": f"{identifier(mid)}_crystal.png", "type": "image/png", "base64": True}

        battery_fields = ["material_ids", "battery_formula", "average_voltage", "capacity_grav", "working_ion", "thermo_type"]

        def battery_row(doc, kind):
            # Retain distinct electrode models/working ions, not just one
            # arbitrary value for a material. Quantities remain native MP data.
            return {"type": kind, "battery_formula": str(doc.get("battery_formula") or "Unknown"),
                    "working_ion": str(doc.get("working_ion") or "Unknown"),
                    "thermo_type": str(doc.get("thermo_type") or "Unknown"),
                    "average_voltage": number(doc.get("average_voltage")),
                    "capacity_grav": number(doc.get("capacity_grav"))}

        @cache.memoize(timeout=900)
        @cached(TTLCache(maxsize=32, ttl=900), condition=Condition())
        def insertion_battery_rows(mid):
            row = material_payload(mid)["row"]
            route = get_rester(use_document_model=False, mute_progress_bars=True).materials.insertion_electrodes
            docs = route.search(formula=row["formula"], fields=battery_fields, chunk_size=100, num_chunks=5)
            if len(docs) >= 500:
                raise ValueError("Insertion lookup exceeds its bounded result limit")
            return [battery_row(doc, "Insertion") for doc in docs
                    if any(alias(str(value)) == alias(mid) for value in (doc.get("material_ids") or []))]

        @cache.memoize(timeout=900)
        @cached(TTLCache(maxsize=1, ttl=900), condition=Condition())
        def conversion_battery_index(revision):
            projection = data_index()
            if projection["revision"] != revision:
                raise ValueError("Contribution index changed during battery lookup")
            wanted = {alias(row["identifier"]) for row in projection["rows"]}
            route = get_rester(use_document_model=False, mute_progress_bars=True).materials.conversion_electrodes
            total = route.count()
            if not isinstance(total, int) or not 0 <= total <= 50000:
                raise ValueError("Conversion collection exceeds its reviewed lookup budget")
            if not total:
                return {}
            # Conversion formula/material filters are not supported by this
            # client. Read the slim collection once, with explicit bounded REST
            # pagination. num_chunks must not be None: that switches this client
            # to a full S3/Delta download and potentially writes a local dataset.
            docs = route.search(fields=battery_fields, chunk_size=1000, num_chunks=math.ceil(total / 1000))
            if len(docs) != total or route.count() != total:
                raise ValueError("Incomplete or changing conversion collection")
            result = {}
            for doc in docs:
                matches = {alias(str(value)) for value in (doc.get("material_ids") or [])} & wanted
                if matches:
                    row = battery_row(doc, "Conversion")
                    for key in matches:
                        result.setdefault(key, []).append(row)
            return result

        def battery_payload(mid):
            identifier(mid)
            projection = data_index()
            if not any(row["identifier"] == mid for row in projection["rows"]):
                raise ValueError("Material is not in the public contributed dataset")
            rows, warnings = [], []
            for kind, fetch in [
                ("Insertion", lambda: insertion_battery_rows(mid)),
                ("Conversion", lambda: conversion_battery_index(projection["revision"]).get(alias(mid), [])),
            ]:
                try:
                    rows.extend(copy.deepcopy(fetch()))
                except Exception as error:
                    logger.warning("%s battery lookup unavailable: %s", kind, type(error).__name__)
                    warnings.append(f"{kind} battery data could not be fully retrieved; the results below may be incomplete.")
            # Failed requests are not cached as empty/no-battery results. The
            # successful per-route caches remain reusable across selections.
            return {"rows": rows, "warnings": warnings}

        @app.callback(
            Output(self.id("battery-response"), "data"), Input(self.id("material-response"), "data"),
            Input(self.id("selection"), "data"),
        )
        def load_battery(material, mid):
            if not material or material.get("identifier") != mid:
                raise PreventUpdate
            if material.get("error"):
                return {"identifier": mid, "unavailable": True}
            try:
                return {"identifier": mid, **battery_payload(identifier(mid))}
            except Exception as error:
                logger.warning("Battery lookup unavailable: %s", type(error).__name__)
                return {"identifier": mid, "error": True}

        @app.callback(
            Output(self.id("battery-details"), "children"), Input(self.id("battery-response"), "data"),
            Input(self.id("selection"), "data"),
        )
        def render_battery(response, mid):
            if not response or response.get("identifier") != mid:
                if ctx.triggered_id in {self.id("selection"), None}:
                    return html.Div([html.P("Loading battery data…", className="help"),
                                     html.Progress(className="progress is-small", **{"aria-label": "Loading battery data"})])
                raise PreventUpdate
            if response.get("unavailable"):
                return []
            if response.get("error"):
                return html.P("Battery data could not be retrieved.", className="help")
            docs = response["rows"]
            warnings = [html.P(message, className="help") for message in response.get("warnings", [])]
            if not docs:
                message = ("No battery records could be confirmed because a lookup is unavailable."
                           if warnings else "No current MP battery records.")
                return html.Div([html.P(message, className="help"), *warnings])
            fields = [("type", "Type"), ("battery_formula", "Formula"), ("working_ion", "Ion"),
                      ("thermo_type", "Energy model"), ("average_voltage", "Voltage (V)"), ("capacity_grav", "Capacity (mAh/g)")]
            return html.Div([
                html.H3("Battery data", className="title is-5", title=f"{len(docs)} current MP electrode models, matched by material ID."),
                *warnings,
                html.Div(html.Table([html.Thead(html.Tr([html.Th(scientific_text(label, "unit")) for key, label in fields])),
                                    html.Tbody([html.Tr([html.Td(scientific_text(display(doc.get(key)), "formula" if key == "battery_formula" else "ion" if key == "working_ion" else "number")) for key, label in fields]) for doc in docs])],
                                   className="table is-fullwidth is-striped"), className="table-container"),
            ])
