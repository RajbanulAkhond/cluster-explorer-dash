/* Public component props plus cleanup of this app's rendered label overlays.
 * Keep one canvas alive; camera updates have one callback owner. */
(function () {
    const ns = window.dash_clientside = window.dash_clientside || {};
    ns.cluster_explorer = ns.cluster_explorer || {};
    const tooltipRoots = new WeakSet();
    const colorEditors = new WeakMap();

    function syncColorEditor(response, selected, overrides, clicked, valid) {
        const byId = name => document.getElementById('CTClusterExplorer_' + name);
        const root = byId('viewer-wrapper'), dialog = byId('color-dialog');
        if (!root || !dialog) return;
        let editor = colorEditors.get(root);
        if (!editor) {
            const container = byId('element-color-input-container');
            if (!container) return;
            const input = document.createElement('input');
            input.type = 'color';
            input.id = 'CTClusterExplorer_element-color-input';
            input.setAttribute('aria-labelledby', 'CTClusterExplorer_color-dialog-title');
            input.className = 'ce-native-color-input';
            container.replaceChildren(input);
            editor = {dialog, input, enabled: false, intent: 0, colors: {}, choice: null};
            colorEditors.set(root, editor);
            const close = () => {
                editor.intent++;
                editor.element = null;
                if (dialog.open) dialog.close();
            };
            const apply = reset => {
                if (!editor.enabled || !editor.element) return close();
                const colors = {...editor.colors};
                if (reset) delete colors[editor.element];
                else if (/^#[0-9a-f]{6}$/i.test(input.value)) colors[editor.element] = input.value.toLowerCase();
                ns.set_props('CTClusterExplorer_element-colors', {data: colors});
                close();
            };
            byId('apply-element-color').addEventListener('click', () => apply(false));
            byId('reset-element-color').addEventListener('click', () => apply(true));
            byId('cancel-element-color').addEventListener('click', close);
            dialog.addEventListener('cancel', () => { editor.intent++; editor.element = null; });
            root.addEventListener('dblclick', event => {
                if (event.target?.tagName !== 'CANVAS' || !editor.enabled) return;
                event.preventDefault();
                event.stopPropagation();
                const intent = ++editor.intent;
                // The renderer publishes atom selection on click. Allow its
                // public selectedObject update to settle after the two clicks;
                // never raycast against private scene/component state.
                requestAnimationFrame(() => requestAnimationFrame(() => {
                    const choice = editor.choice;
                    if (editor.intent !== intent || !editor.enabled || !choice || choice.ceViewTag !== editor.sceneKey) return;
                    editor.element = choice.ceElement;
                    input.value = editor.colors[choice.ceElement] || choice.ceColor;
                    byId('color-element-name').textContent = choice.ceElement;
                    if (!dialog.open) dialog.showModal();
                    input.focus();
                }));
            });
        }
        const enabled = valid && !response.error;
        if (!enabled || editor.sceneKey !== response.sceneKey || editor.selected !== selected) {
            editor.intent++;
            editor.element = null;
            if (dialog.open) dialog.close();
        }
        editor.enabled = enabled;
        editor.sceneKey = enabled ? response.sceneKey : null;
        editor.selected = selected;
        editor.colors = overrides || {};
        // Only atom primitives carry these app-owned tags. Empty clicks, bonds,
        // and selections from another material/scene cannot open the editor.
        editor.choice = enabled && Array.isArray(clicked) ? clicked.find(object =>
            /^[A-Z][a-z]?$/.test(object?.ceElement || '') &&
            /^#[0-9a-f]{6}$/i.test(object?.ceColor || '') && object.ceViewTag === response.sceneKey) : null;
    }

    ns.cluster_explorer.clusterCalloutParts = function (text) {
        if (!text.includes('Cluster point group: ')) return null;
        return text.split('\n').map(line => {
            if (!line.startsWith('Cluster point group: ')) return {text: line, heading: line.includes(' · Cluster ')};
            const raw = line.slice('Cluster point group: '.length);
            const match = raw.match(/^([CDS])_?((?:\d+|[*∞])[hvd]?|[is])$|^([TOIK])_?([hd])$/);
            const tokens = match ? [{text: match[1] || match[3]},
                {text: (match[2] || match[4]).replace('*', '∞'), script: 'sub'}] : [{text: raw}];
            return {label: 'Cluster point group: ', tokens};
        });
    };

    function installClusterTooltips() {
        const root = document.getElementById('CTClusterExplorer_viewer-wrapper');
        if (!root || tooltipRoots.has(root)) return;
        tooltipRoots.add(root);
        const options = {subtree: true, childList: true, characterData: true};
        const observer = new MutationObserver(() => {
            // The renderer supplies an escaped textContent hover tooltip. Format
            // only that public DOM output; never read private Three/React state.
            observer.disconnect();
            try {
                root.querySelectorAll('.tooltiptext').forEach(element => {
                    const rows = ns.cluster_explorer.clusterCalloutParts(element.textContent || '');
                    element.style.visibility = '';
                    element.classList.toggle('ce-cluster-callout', !!rows);
                    if (!rows) return;
                    const fragment = document.createDocumentFragment();
                    rows.forEach((row, index) => {
                        if (index) fragment.appendChild(document.createTextNode('\n'));
                        const line = document.createElement('div');
                        line.className = row.heading ? 'ce-callout-title' : 'ce-callout-line';
                        if (row.tokens) {
                            line.appendChild(document.createTextNode(row.label));
                            row.tokens.forEach(token => {
                                const span = document.createElement(token.script === 'sub' ? 'sub' : 'span');
                                span.textContent = token.text;
                                line.appendChild(span);
                            });
                        } else line.textContent = row.text || '\u00a0';
                        fragment.appendChild(line);
                    });
                    element.replaceChildren(fragment);
                });
            } finally { observer.observe(root, options); }
        });
        observer.observe(root, options);
        root.addEventListener('pointerleave', () => {
            root.querySelectorAll('.tooltiptext').forEach(element => { element.style.visibility = 'hidden'; });
        });
    }

    function defaultPose(current, resetZoom) {
        const p = current?.position || {x: 15, y: 15, z: 15};
        const d = Math.max(10, Math.hypot(p.x, p.y, p.z)), elevation = Math.PI / 15;
        return {position: {x: 0, y: d * Math.sin(elevation), z: d * Math.cos(elevation)},
            quaternion: {x: -Math.sin(elevation / 2), y: 0, z: 0, w: Math.cos(elevation / 2)},
            zoom: resetZoom ? 4 : (current?.zoom || 4)};
    }

    ns.cluster_explorer.orientationCompass = function (state, draw) {
        if (!Array.isArray(draw) || !draw.includes('axes')) return ['', {display: 'none'}];
        const raw = state?.quaternion || defaultPose(null, true).quaternion;
        const norm = Math.hypot(raw.x, raw.y, raw.z, raw.w);
        const q = norm > 0 && Number.isFinite(norm) ? raw : defaultPose(null, true).quaternion;
        const n = Math.hypot(q.x, q.y, q.z, q.w), x = q.x / n, y = q.y / n, z = q.z / n, w = q.w / n;
        // Inverse camera rotation, composed with the physical -> renderer frame
        // (x,y,z) -> (x,z,-y). These remain Cartesian axes, not lattice a/b/c.
        const axes = [
            {name: 'x', color: '#d62728', vector: [1-2*(y*y+z*z), 2*(x*y-z*w), 2*(x*z+y*w)]},
            {name: 'y', color: '#228833', vector: [-2*(x*z-y*w), -2*(y*z+x*w), -(1-2*(x*x+y*y))]},
            {name: 'z', color: '#3366cc', vector: [2*(x*y+z*w), 1-2*(x*x+z*z), 2*(y*z-x*w)]},
        ].sort((a,b) => a.vector[2]-b.vector[2]);
        const f = value => Number(value).toFixed(3);
        const paths = axes.map(axis => {
            const [vx, vy] = axis.vector, length = Math.hypot(vx, vy);
            const ex = 56 + 30 * vx, ey = 56 - 30 * vy;
            if (length < .25) return `<circle data-axis="${axis.name}" cx="56" cy="56" r="3" fill="${axis.color}"/><text data-label="${axis.name}" x="43" y="72">${axis.name}</text>`;
            const tx = ex + 12 * vx / length, ty = ey - 12 * vy / length;
            return `<path data-axis="${axis.name}" d="M56 56 L${f(ex)} ${f(ey)}" stroke="${axis.color}" stroke-width="2.2" fill="none" marker-end="url(#arrow-${axis.name})"/><text data-label="${axis.name}" x="${f(tx)}" y="${f(ty)}">${axis.name}</text>`;
        }).join('');
        const markers = axes.map(axis => `<marker id="arrow-${axis.name}" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" markerUnits="userSpaceOnUse" orient="auto"><path d="M0 0 L8 4 L0 8 Z" fill="${axis.color}"/></marker>`).join('');
        const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 112 112" role="img" aria-label="Cartesian x y z orientation"><defs>${markers}</defs><g font-family="sans-serif" font-size="12" text-anchor="middle" dominant-baseline="central" fill="#2c3e50">${paths}</g></svg>`;
        return ['data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svg), {display: 'block'}];
    };

    ns.cluster_explorer.presentViewer = function (
        response, selected, x, y, z, representation, colors, atoms, bonds, draw, elementColors, clickedObjects,
        tick, camera, reset, state, applied
    ) {
        installClusterTooltips();
        const unchanged = ns.no_update;
        const out = Array(9).fill(unchanged);
        const triggers = ns.callback_context.triggered.map(t => t.prop_id);
        const triggered = key => triggers.some(t => t.startsWith('CTClusterExplorer_' + key + '.'));
        const colorPairs = Object.entries(elementColors || {}).sort((a,b) => a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0);
        const params = [x, y, z, representation, colors, atoms, bonds, draw, colorPairs];
        const valid = response && response.identifier === selected &&
            JSON.stringify(response.params) === JSON.stringify(params);
        syncColorEditor(response, selected, elementColors, clickedObjects, valid);

        function clearOldLabels() {
            // Removing a native scene root does not remove its descendant CSS2D
            // elements in the pinned renderer. Never inspect private scene state.
            const root = document.getElementById('CTClusterExplorer_viewer-wrapper');
            root?.querySelectorAll('.tooltiptext').forEach(label => { label.style.visibility = 'hidden'; });
            const old = Array.from(root?.querySelectorAll('.tooltip') || []);
            old.forEach(label => label.remove());
            // Remove only these old nodes if an already queued render reattaches
            // them before React applies the new data. New labels are untouched.
            requestAnimationFrame(() => old.forEach(label => label.remove()));
        }

        function preset(view, current, resetZoom) {
            const p = current?.position || {x: 15, y: 15, z: 15};
            const zoom = resetZoom ? 4 : (current?.zoom || 4);
            const d = Math.max(10, Math.hypot(p.x, p.y, p.z));
            if (view === 'Cartesian X') return {position: {x: d, y: 0, z: 0}, quaternion: {x: 0, y: Math.SQRT1_2, z: 0, w: Math.SQRT1_2}, zoom};
            if (view === 'Cartesian Y') return {position: {x: 0, y: 0, z: -d}, quaternion: {x: 0, y: 1, z: 0, w: 0}, zoom};
            if (view === 'Cartesian Z') return {position: {x: 0, y: d, z: 0}, quaternion: {x: -Math.SQRT1_2, y: 0, z: 0, w: Math.SQRT1_2}, zoom};
            return defaultPose(current, resetZoom);
        }

        if (triggered('reset-viewer')) out[6] = preset('Perspective', state, true);
        else if (triggered('camera')) out[6] = preset(camera, state, false);
        if (!valid) {
            // Late responses must not hide a newer, already accepted scene.
            if (triggered('viewer-response')) return out;
            out[2] = {display: 'none'};
            out[3] = 'Updating crystal preview…';
            out[4] = true;
            out[8] = '';
            return out;
        }
        if (response.error) {
            clearOldLabels();
            out[0] = {name: 'cluster-explorer-crystal', contents: []};
            out[1] = [];
            out[2] = {display: 'none'};
            out[3] = response.error;
            out[4] = true;
            out[5] = {'cluster-explorer-crystal': false};
            out[7] = null;
            out[8] = '';
            return out;
        }
        // A tick batched with fresh scene/control inputs must not swallow their
        // update (notably Reset, which also disables rotation).
        if (triggers.length === 1 && triggered('rotation-tick') && response.sceneKey === applied?.sceneKey) {
            // Ignore a queued interval after the checkbox was switched off.
            if (!draw.includes('rotate') || !state?.position || !state?.quaternion) return out;
            const p = state.position, q = state.quaternion;
            const a = .025, c = Math.cos(a), s = Math.sin(a), h = Math.sin(a / 2), w = Math.cos(a / 2);
            out[6] = {position: {x: p.x * c + p.z * s, y: p.y, z: p.z * c - p.x * s},
                quaternion: {x: w * q.x + h * q.z, y: w * q.y + h * q.w,
                    z: w * q.z - h * q.x, w: w * q.w - h * q.y}, zoom: state.zoom || 4};
            return out;
        }
        const geometry = [selected, x, y, z];
        if (response.sceneKey !== applied?.sceneKey) {
            clearOldLabels();
            out[0] = response.scene;
            // Data updates alone bypass the pinned renderer's redraw. Its
            // documented visibility prop performs a synchronous final render.
            out[5] = {'cluster-explorer-crystal': true,
                atoms: draw.includes('atoms') && representation !== 'Wireframe',
                bonds: draw.includes('bonds') && representation !== 'Space Filling',
                unit_cell: draw.includes('unit_cell'), labels: draw.includes('labels')};
            // Keep the user's pan/zoom/rotation for display-only updates. A
            // different structure/supercell is fitted afresh to its own bounds.
            const sameGeometry = JSON.stringify(geometry) === JSON.stringify(applied?.geometry);
            if (out[6] === unchanged) out[6] = sameGeometry && state?.position && state?.quaternion
                ? {position: state.position, quaternion: state.quaternion, zoom: state.zoom || 4}
                : preset(camera, null, true);
            out[7] = {sceneKey: response.sceneKey, geometry};
        }
        out[1] = response.legend;
        out[2] = {display: 'block'};
        out[3] = response.status;
        out[4] = !draw.includes('rotate');
        out[8] = response.sceneKey;
        return out;
    };
}());
