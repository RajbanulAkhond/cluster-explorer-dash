"""Ten read-only acceptance workflows against a running Cluster Explorer.

Install the app's ``test`` extra and Chromium. Set CLUSTER_EXPLORER_BASE_URL
for a staging deployment; downloads and screenshots stay in a temporary folder.
No contribution uploads or other remote writes are performed.
"""

import csv
import io
import json
import os
import re
import tempfile
import unittest
import zipfile
from pathlib import Path
from urllib.parse import unquote, urljoin
from xml.etree import ElementTree

from playwright.sync_api import expect, sync_playwright


class MainWorkflows(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = os.environ.get("CLUSTER_EXPLORER_BASE_URL", "http://127.0.0.1:8050/").rstrip("/") + "/"
        cls.artifacts = Path(tempfile.mkdtemp(prefix="cluster-explorer-acceptance-"))
        cls.runtime = sync_playwright().start()
        cls.browser = cls.runtime.chromium.launch(headless=True, args=[
            "--use-angle=swiftshader", "--enable-unsafe-swiftshader",
        ])
        cls.baseline = None
        print(f"\nRead-only app tests: {cls.base}\nArtifacts: {cls.artifacts}", flush=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.runtime.stop()

    def setUp(self):
        self.context = self.browser.new_context(viewport={"width": 1440, "height": 1100}, accept_downloads=True)
        self.page = self.context.new_page()
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.on("response", lambda response: self.errors.append(f"Callback HTTP {response.status}")
                     if response.status >= 400 and "_dash-update-component" in response.url else None)
        self.open()
        if self.__class__.baseline is None:
            data = self.download("Download ZIP")
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                self.__class__.baseline = {
                    name: list(csv.DictReader(io.StringIO(archive.read(name + ".csv").decode())))
                    for name in ["materials", "clusters", "clusterPointGroups"]
                }
                self.baseline["provenance"] = json.loads(archive.read("provenance.json"))
        self.count(len(self.baseline["materials"]))

    def tearDown(self):
        try:
            self.assertEqual(self.errors, [], "Browser errors or failed Dash callbacks")
        finally:
            self.context.close()

    def control(self, name):
        return self.page.locator("#CTClusterExplorer_" + name)

    def open(self, fragment=""):
        self.page.goto(self.base + fragment, wait_until="domcontentloaded")
        expect(self.control("result-count")).to_have_text(re.compile(r"[\d,]+ matches"), timeout=180000)

    def count(self, number):
        expect(self.control("result-count")).to_have_text(f"{number:,} matches", timeout=90000)

    def choose(self, name, text, search=None):
        control = self.control(name)
        control.scroll_into_view_if_needed()
        control.click()
        if search:
            control.locator("input").fill(search)
        pattern = re.escape(text) if name == "example" else "^" + re.escape(text) + "$"
        control.locator(".VirtualizedSelectOption").filter(has_text=re.compile(pattern)).last.click()

    def query(self, value):
        self.control("search").fill(value)
        self.control("search").press("Tab")

    def reset(self):
        self.control("reset").click()
        self.count(len(self.baseline["materials"]))

    def group(self, name):
        group = self.page.locator("details.ce-filter-group").filter(
            has=self.page.get_by_text(name, exact=True))
        if group.get_attribute("open") is None:
            group.locator("summary").click()
        return group

    def download(self, label):
        with self.page.expect_download(timeout=90000) as result:
            self.page.get_by_role("button", name=label, exact=True).click()
        download = result.value
        data = Path(download.path()).read_bytes()
        (self.artifacts / download.suggested_filename).write_bytes(data)
        return data

    def rows(self):
        return list(csv.DictReader(io.StringIO(self.download("Download CSV").decode())))

    def select(self, mid):
        self.query(mid)
        self.count(1)
        row = self.control("results").locator(".ag-center-cols-container .ag-row").first
        expect(row.locator('[col-id="identifier"]')).to_have_text(mid)
        row.click()
        self.loaded(mid)

    def loaded(self, mid):
        expect(self.control("selected-badge")).to_have_text(mid, timeout=90000)
        expect(self.control("material-details").get_by_role("link", name="Share this material", exact=True)).to_have_attribute(
            "href", re.compile(r"[&#]material=" + re.escape(mid) + r"(?:&|$)"), timeout=90000)

    def viewer(self):
        self.group("Viewer settings")
        viewer = self.control("viewer-wrapper")
        expect(viewer).to_be_visible(timeout=90000)
        expect(self.control("viewer-status")).to_contain_text("supercell 1×1×1", timeout=90000)
        viewer.scroll_into_view_if_needed()
        return viewer

    def draw(self, name, checked):
        with self.page.expect_response(lambda response:
                "_dash-update-component" in response.url and response.request.method == "POST" and
                response.request.post_data_json.get("output") == "CTClusterExplorer_viewer-response.data",
                timeout=90000) as result:
            self.control("draw").get_by_label(name, exact=True).set_checked(checked)
        payload = result.value.json()["response"]["CTClusterExplorer_viewer-response"]["data"]
        self.assertNotIn("error", payload)
        expect(self.control("viewer-wrapper")).to_have_attribute("data-scene-key", payload["sceneKey"])
        return payload["scene"]

    def test_01_startup_complete_index_and_result_table(self):
        """Load a complete public index, both child tables, and the requested grid."""
        materials = self.baseline["materials"]
        ids = {row["identifier"] for row in materials}
        self.assertEqual(len(ids), len(materials))
        for name in ["clusters", "clusterPointGroups"]:
            self.assertEqual({row["materialId"] for row in self.baseline[name]}, ids)
        self.assertEqual(self.baseline["provenance"]["materials"], len(materials))
        self.assertEqual(self.baseline["provenance"]["project"], "cluster_materials")
        expect(self.control("results").locator(".ag-header-cell-text")).to_have_text([
            "MP ID", "Formula", "Clusters", "Cluster sizes", "Cluster point groups", "Dimension", "Energy above the hull",
        ])
        self.assertLessEqual(self.control("results").locator(".ag-center-cols-container .ag-row").count(), 50)
        expect(self.page.get_by_text("Local preview", exact=True)).to_have_count(0)
        expect(self.page.locator(".ce-data-notes")).not_to_have_attribute("open", "")
        self.loaded("mp-1003545")

    def test_02_search_elements_paging_and_full_result_sort(self):
        """Search literal text, select elements, page, and sort every matching row."""
        self.query("[")
        self.count(0)
        self.reset()
        grid = self.control("results")
        first = grid.locator('.ag-center-cols-container [col-id="identifier"]').first.inner_text()
        self.control("next").click()
        expect(grid.locator('.ag-center-cols-container [col-id="identifier"]').first).not_to_have_text(first)
        self.control("previous").click()
        expect(grid.locator('.ag-center-cols-container [col-id="identifier"]').first).to_have_text(first)
        self.page.locator(".ce-periodic").get_by_text("Nb", exact=True).click()
        expected = {row["identifier"] for row in self.baseline["materials"] if "Nb" in json.loads(row["elements"])}
        self.count(len(expected))
        self.assertEqual({row["identifier"] for row in self.rows()}, expected)
        self.reset()
        self.choose("sort", "Band gap")
        self.choose("direction", "Descending")
        values = [float(row["band_gap"]) for row in self.rows() if row["band_gap"]]
        self.assertEqual(values, sorted(values, reverse=True))

    def test_03_scientific_filters_and_native_ranges(self):
        """Cluster criteria share a row; native ranges exclude missing values."""
        group = self.group("Clusters & symmetry")
        expect(group.locator(".label")).to_have_text([
            "Cluster size", "Cluster point group", "Sub-lattice space group", "Connectivity",
            "Polar class", "Piezoelectric class", "Enantiomorphic class",
        ])
        self.choose("size", "2")
        self.choose("connectivity", "Isolated")
        expected = {row["materialId"] for row in self.baseline["clusters"]
                    if int(row["size"]) == 2 and row["isExtended"] == row["isShared"] == "False"}
        self.count(len(expected))
        self.assertEqual({row["identifier"] for row in self.rows()}, expected)
        self.reset()
        self.choose("polar", "Yes")
        self.count(sum(row["isPolar"] == "True" for row in self.baseline["materials"]))
        self.reset()
        self.group("Property ranges")
        handle = self.control("hullRange").locator(".rc-slider-handle").nth(1)
        handle.scroll_into_view_if_needed()
        handle.click()
        handle.press("Home")
        expect(handle).to_have_attribute("aria-valuenow", handle.get_attribute("aria-valuemin"))
        handle.press("ArrowRight")
        expect(handle).not_to_have_attribute("aria-valuenow", handle.get_attribute("aria-valuemin"))
        bound = float(handle.get_attribute("aria-valuenow"))
        expected = {row["identifier"] for row in self.baseline["materials"]
                    if row["energy_above_hull"] and float(row["energy_above_hull"]) <= bound}
        self.count(len(expected))
        self.assertEqual({row["identifier"] for row in self.rows()}, expected)

    def test_04_examples_details_links_and_scientific_notation(self):
        """Each curated example retains its ID and displays formula subscripts."""
        examples = [("mp-29950", "Nb3Cl8", ["3", "8"]), ("mp-4474", "V4GaS8", ["4", "8"]),
                    ("mp-673706", "Li7(Mo3Se4)4", ["7", "3", "4", "4"])]
        for mid, formula, subscripts in examples:
            self.choose("example", mid, search=formula)
            self.loaded(mid)
            expect(self.control("example").locator(".Select-value-label")).to_have_text(f"{formula} · {mid}")
            expect(self.control("example").locator(".Select-value-label sub")).to_have_text(subscripts)
            expect(self.control("material-details").locator("h3").first).to_have_text(formula)
            expect(self.control("material-details").get_by_role("link", name="Open contribution", exact=True)).to_have_attribute(
                "href", re.compile(r"^https://next-gen\.materialsproject\.org/contribs/contributions/[a-f0-9]{24}$"))
            for name in ["clusters", "clusterPointGroups"]:
                expected = sum(row["materialId"] == mid for row in self.baseline[name])
                expect(self.page.locator(f'[data-table="{name}"] tbody tr')).to_have_count(expected)
        self.select("mp-1103089")
        expect(self.control("material-details").get_by_role("link", name=re.compile(r"^ICSD \d+$")).first).to_have_attribute(
            "href", re.compile(r"^https://www\.ccdc\.cam\.ac\.uk/structures/Search\?Ccdcid=%20\d+&DatabaseToSearch=Published$"))

    def test_05_flat_band_and_automatic_native_battery_data(self):
        """Subset membership stays distinct from cited flat bands and live electrodes."""
        self.choose("availability", "Flat-band materials")
        self.count(sum(row["hasFlatData"] == "True" for row in self.baseline["materials"]))
        self.select("mp-10056")
        expect(self.control("material-details")).to_contain_text("Neves et al. (2024)")
        self.reset()
        self.choose("availability", "Battery materials")
        self.count(sum(row["hasBatteryData"] == "True" for row in self.baseline["materials"]))
        self.reset()
        self.select("mp-1176523")
        expect(self.control("battery-details")).to_contain_text("Insertion", timeout=90000)
        expect(self.control("battery-details")).to_contain_text("Voltage (V)")
        # Its scene contains exactly three position vectors in one primitive.
        # This previously failed in the display-only z-up transformation.
        expect(self.control("viewer-wrapper")).to_be_visible(timeout=90000)
        expect(self.control("viewer-status")).to_contain_text("supercell 1×1×1", timeout=90000)
        self.reset()
        self.select("mp-1029801")
        expect(self.control("battery-details")).to_contain_text("Conversion", timeout=90000)
        expect(self.control("battery-details")).not_to_contain_text("Li0-1.25VO2")
        self.select("mp-2223706")
        expect(self.control("battery-details")).to_contain_text("No current MP battery records", timeout=90000)

    def test_06_viewer_interaction_toggles_camera_and_reset(self):
        """Keep one interactive canvas, opt-in spin/labels, z-up, and corner axes."""
        self.loaded("mp-1003545")
        viewer = self.viewer()
        canvas, compass = viewer.locator("canvas"), self.control("orientation-axes")
        draw = self.control("draw")
        for name in ["Spin", "Labels", "Axes (x/y/z)", "Auto rotate"]:
            expect(draw.get_by_label(name, exact=True)).not_to_be_checked()
        expect(draw.get_by_label("Cluster atoms", exact=True)).to_be_checked()
        scene = self.draw("Cluster atoms", False)
        def atom_colors(node):
            return ([node["color"]] if node.get("type") == "spheres" else []) + [
                color for child in node.get("contents", []) for color in atom_colors(child)]
        ordinary_colors = atom_colors(scene)
        scene = self.draw("Cluster atoms", True)
        if not self.control("highlight-status").inner_text():
            self.assertIn("#f07818", atom_colors(scene))
            self.assertTrue(set(atom_colors(scene)) <= {"#f07818", "#c8cdd4"})
            self.assertNotEqual(atom_colors(scene), ordinary_colors)
            def context_surfaces(node):
                return ([node] if node.get("reference", "").startswith("muted-context-") else []) + [
                    surface for child in node.get("contents", []) for surface in context_surfaces(child)]
            context = context_surfaces(scene)
            self.assertTrue(context)
            self.assertTrue(all(n["type"] == "surface" and n["opacity"] == (.9 if n["reference"] == "muted-context-atoms" else .65) for n in context))
            def cluster_tooltips(node):
                return ([node["tooltip"]] if node.get("type") == "spheres" and node.get("color") == "#f07818" else []) + [
                    tooltip for child in node.get("contents", []) for tooltip in cluster_tooltips(child)]
            self.assertTrue(all("Cluster point group:" in t and "Mean distance:" in t and "Å" in t for t in cluster_tooltips(scene)))
            legend = self.control("crystal_legend_container")
            expect(legend).to_contain_text("Cluster atoms (Mn)")
            expect(legend).not_to_contain_text("Other atoms")
        # Double-click an actual atom on the canvas, rather than injecting a
        # synthetic selectedObject. Keep this in the existing viewer workflow.
        dialog = self.control("color-dialog")
        canvas.scroll_into_view_if_needed()
        box = canvas.bounding_box()
        hit = None
        for y_fraction in [.5, .4, .6, .3, .7]:
            for x_fraction in [.5, .4, .6, .3, .7]:
                point = {"x": box["width"] * x_fraction, "y": box["height"] * y_fraction}
                canvas.dblclick(position=point)
                try:
                    expect(dialog).to_be_visible(timeout=350)
                    hit = point
                    break
                except AssertionError:
                    pass
            if hit:
                break
        self.assertIsNotNone(hit, "Double-clicking an atom should open the element color dialog")
        element = self.control("color-element-name").inner_text()
        self.assertIn(element, ["Mg", "Mn", "O"])
        self.control("element-color-input").evaluate("el => { el.value = '#4361ee'; }")
        with self.page.expect_response(lambda response:
                "_dash-update-component" in response.url and response.request.method == "POST" and
                response.request.post_data_json.get("output") == "CTClusterExplorer_viewer-response.data",
                timeout=90000) as result:
            self.control("apply-element-color").click()
        changed = result.value.json()["response"]["CTClusterExplorer_viewer-response"]["data"]
        self.assertNotIn("error", changed)
        expect(dialog).not_to_be_visible()
        expect(viewer).to_have_attribute("data-scene-key", changed["sceneKey"])
        def tagged_atoms(node):
            return ([node] if node.get("ceElement") else []) + [
                atom for child in node.get("contents", []) for atom in tagged_atoms(child)]
        self.assertTrue(all(n["ceColor"] == "#4361ee" for n in tagged_atoms(changed["scene"]) if n["ceElement"] == element))
        expect(draw.get_by_text("Polyhedra", exact=True)).to_have_count(0)
        self.draw("Labels", True)
        expect(viewer.locator(".tooltip")).not_to_have_count(0)
        positions = viewer.locator(".tooltip").evaluate_all("els => els.map(el => { const r=el.getBoundingClientRect(); return [Math.round(r.x),Math.round(r.y)].join(','); })")
        self.assertGreater(len(set(positions)), 1)
        self.draw("Labels", False)
        expect(viewer.locator(".tooltip")).to_have_count(0)
        scene = self.draw("Spin", True)

        def arrows(node):
            return ([node["color"]] if node.get("type") == "arrows" else []) + [
                color for child in node.get("contents", []) for color in arrows(child)]

        self.assertEqual(set(arrows(scene)), {"#000000"})
        self.draw("Spin", False)
        self.draw("Axes (x/y/z)", True)
        expect(compass).to_be_visible()
        svg = ElementTree.fromstring(unquote(compass.get_attribute("src").split(",", 1)[1]))
        axes = {node.attrib["data-axis"]: node.attrib for node in svg.iter() if "data-axis" in node.attrib}
        z = axes["z"]["d"].split(" L")[-1].split()
        self.assertAlmostEqual(float(z[0]), 56, delta=.1)
        self.assertLess(float(z[1]), 30)
        original = compass.get_attribute("src")
        canvas.scroll_into_view_if_needed()
        box = canvas.bounding_box()
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        self.page.mouse.move(x, y)
        self.page.mouse.down()
        self.page.mouse.move(x + 80, y + 45, steps=12)
        self.page.mouse.up()
        expect(compass).not_to_have_attribute("src", original)
        corner = compass.bounding_box()
        self.assertLessEqual(corner["x"] - box["x"], 20)
        self.assertLessEqual(box["y"] + box["height"] - corner["y"] - corner["height"], 20)
        self.choose("representation", "Wireframe")
        expect(draw.get_by_label("Atoms", exact=True)).to_be_disabled()
        self.choose("camera", "Along X")
        self.page.get_by_role("button", name="Reset viewer", exact=True).click()
        expect(self.control("camera").locator(".Select-value-label")).to_have_text("Z-up")
        expect(draw.get_by_label("Axes (x/y/z)", exact=True)).not_to_be_checked()
        expect(draw.get_by_label("Cluster atoms", exact=True)).to_be_checked()
        expect(canvas).to_have_count(1)
        viewer.screenshot(path=str(self.artifacts / "viewer-desktop.png"))

    def test_07_downloads_preserve_full_results_and_structure_coordinates(self):
        """CSV/ZIP cover all matches; JSON/CIF retain source and supercell data."""
        self.assertEqual(len(self.rows()), len(self.baseline["materials"]))
        self.loaded("mp-1003545")
        self.viewer()
        original = json.loads(self.download("Structure JSON"))
        self.control("supercell-x").fill("2")
        self.control("supercell-x").press("Tab")
        expect(self.control("viewer-status")).to_contain_text("supercell 2×1×1", timeout=90000)
        repeated = json.loads(self.download("Structure JSON"))
        self.assertEqual(len(repeated["sites"]), 2 * len(original["sites"]))
        expected = [2 * value for value in original["lattice"]["matrix"][0]]
        self.assertEqual(repeated["lattice"]["matrix"][0], expected)
        self.assertIn(b"_cell_length_a", self.download("Download CIF"))
        self.assertEqual(self.baseline["provenance"]["units"]["averageDistance"], "angstrom")

    def test_08_shared_links_aliases_and_session_isolation(self):
        """Restore supported state without leaking filters into another session."""
        self.choose("availability", "Flat-band materials")
        expected = sum(row["hasFlatData"] == "True" for row in self.baseline["materials"])
        self.count(expected)
        # Link generation is a separate callback from result filtering.
        expect(self.control("share-search")).to_have_attribute("href", re.compile(r"[&#]availability=flat(?:&|$)"))
        saved = self.control("share-search").get_attribute("href")
        self.page.goto(urljoin(self.base, saved), wait_until="domcontentloaded")
        self.count(expected)
        other = self.browser.new_context()
        try:
            page = other.new_page()
            page.goto(self.base, wait_until="domcontentloaded")
            expect(page.locator("#CTClusterExplorer_result-count")).to_have_text(
                f"{len(self.baseline['materials']):,} matches", timeout=180000)
            self.count(expected)
        finally:
            other.close()
        self.open("#v=1&material=mp-2223706")
        self.loaded("mp-2223706")
        expect(self.control("structure-status")).to_contain_text("Deprecated MP record")
        self.viewer()

    def test_09_responsive_pinned_filters_and_dropdown_labels(self):
        """Keep Examples/Reset accessible as filters expand on desktop and mobile."""
        self.choose("example", "mp-673706")
        for width, height in [(1440, 1100), (1280, 650), (900, 1000), (390, 844)]:
            self.page.set_viewport_size({"width": width, "height": height})
            actions = self.page.locator(".ce-filter-actions")
            self.page.evaluate("window.scrollTo(0,document.querySelector('.ce-sidebar').getBoundingClientRect().top+scrollY+16)")
            before = actions.bounding_box()
            self.group("Clusters & symmetry")
            self.group("Property ranges")
            if width > 900:
                self.page.locator(".ce-filters").evaluate("el => el.scrollTop=el.scrollHeight")
            else:
                self.page.evaluate("window.scrollBy(0,350)")
            after = actions.bounding_box()
            self.assertAlmostEqual(before["y"], after["y"], delta=1)
            self.assertGreaterEqual(after["y"], 0)
            self.assertLess(after["y"] + after["height"], height)
            self.control("example").click()
            options = self.control("example").locator(".VirtualizedSelectOption")
            expect(options).to_have_count(3)
            self.assertTrue(options.evaluate_all("els=>els.every(el=>el.scrollWidth<=el.clientWidth+1 && el.scrollHeight<=el.clientHeight+1)"))
            self.page.screenshot(path=str(self.artifacts / f"filter-actions-{width}.png"))
            self.page.keyboard.press("Escape")
            self.assertTrue(self.page.evaluate("document.documentElement.scrollWidth <= innerWidth+2"))

    def test_10_invalid_links_unknown_materials_and_stale_content(self):
        """Malformed state fails safely and unknown IDs never retain old geometry."""
        self.open("#v=1&hullMin=not-a-number&material=mp-1003545")
        expect(self.control("url-warning")).to_contain_text("invalid", timeout=90000)
        self.reset()
        expect(self.control("url-warning")).to_be_empty()
        self.open("#v=1&pair=Mn-O&material=mp-1003545")
        expect(self.control("url-warning")).to_contain_text("no longer available", timeout=90000)
        self.open("#v=1&material=mp-999999999999")
        expect(self.control("material-details")).to_contain_text("This material could not be loaded", timeout=90000)
        expect(self.control("viewer-wrapper")).not_to_be_visible()
        expect(self.control("contribution-tables")).to_be_empty()


if __name__ == "__main__":
    unittest.main(verbosity=2)
