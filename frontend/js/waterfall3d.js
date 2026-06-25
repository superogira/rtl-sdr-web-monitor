/**
 * Waterfall3D — Three.js powered 3D waterfall renderer.
 *
 * Renders the live spectrum as a scrolling "terrain" mesh:
 *   X axis = frequency bins (left → right)
 *   Z axis = time           (newest near camera, oldest far away)
 *   Y axis = signal power   (peaks rise up like mountains)
 * Vertex colors follow the same dB→color LUT as the 2D waterfall so the two
 * views look consistent.
 *
 * Interaction (via THREE.OrbitControls):
 *   - Left drag   → orbit / rotate the view
 *   - Wheel       → zoom in/out
 *   - Right drag  → pan
 *
 * Public API (used by app.js):
 *   Waterfall3D.init(containerEl) → bool   lazily set up WebGL context
 *   Waterfall3D.feed(spectrum, dbMin, dbMax) push a new spectrum line
 *   Waterfall3D.setSignalLabels(signals, ctx) spawn frequency labels for signals > squelch
 *   Waterfall3D.dispose()                tear down + free GPU resources
 */
const Waterfall3D = (function () {

    // === Tunables ===
    const ROWS = 128;            // history depth (time slices kept on the mesh)
    const MAX_BINS = 256;        // downsample input spectrum if larger (perf)
    const PLANE_W = 60;          // world-space width  (frequency axis)
    const PLANE_D = 48;          // world-space depth (time axis)
    const HEIGHT_SCALE = 16;     // vertical exaggeration of signal peaks
    const ROW_Z_STEP = PLANE_D / (ROWS - 1);

    // === Signal-label tunables ===
    const MAX_LABELS = 10;       // hard cap on simultaneous labels (perf/readability)
    const DEDUP_HZ = 50000;      // ±50 kHz — don't spawn a new label if one is nearby
    const REFRESH_ROWS = 12;     // ~1s at typical 12 fps: re-tag a continuous signal
    const LABEL_Y_OFFSET = 2.2;  // world units above the peak
    const LABEL_SCALE = 7.0;     // sprite world-space size

    // === State ===
    let _container = null;
    let _renderer = null;
    let _scene = null;
    let _camera = null;
    let _controls = null;
    let _mesh = null;
    let _geometry = null;
    let _positions = null;       // Float32Array (vertex XYZ)
    let _colors = null;          // Float32Array (vertex RGB)
    let _history = null;         // Float32Array (ROWS × bins, dB values)
    let _bins = 0;               // active bin count (after downsample)
    let _writeRow = 0;           // ring-buffer write index (0..ROWS-1)
    let _rafId = null;           // requestAnimationFrame handle
    let _inited = false;
    let _dbMin = -110;
    let _dbMax = -55;
    let _colorLUT = null;        // Uint8ClampedArray (N×3)
    let _colorLUTN = 0;

    // === Signal labels ===
    // _labels: array of {
    //   sprite:    THREE.Sprite (text "145.500 MHz", billboard)
    //   stick:     THREE.Line from floor to peak
    //   freqHz:    number (center of the signal)
    //   x:         world X (freq axis position, fixed for the label's life)
    //   z:         world Z (time axis, increases as the label scrolls back)
    //   peakY:     world Y (signal height at birth; fixed)
    // }
    let _labels = [];
    // Frequency→world-X mapping context, set by app.js via setSignalLabels().
    let _freqMap = { lowHz: 0, binHz: 1, numBins: MAX_BINS, sr: 2048000, centerHz: 0 };

    // --- dB → RGB lookup table (mirrors app.js _buildColorLUT stops) ---
    function _buildLUT() {
        const N = 512;
        const lut = new Uint8ClampedArray(N * 3);
        const stops = [
            [0.00, 250, 80, 8],
            [0.20, 220, 85, 35],
            [0.40, 185, 90, 50],
            [0.60, 120, 85, 45],
            [0.80, 50,  95, 55],
            [1.00, 0,   95, 65],
        ];
        const hslToRgb = (h, s, l) => {
            h = ((h % 360) + 360) % 360;
            const c = (1 - Math.abs(2 * l - 1)) * s;
            const hp = h / 60;
            const x = c * (1 - Math.abs((hp % 2) - 1));
            let r1 = 0, g1 = 0, b1 = 0;
            if (hp < 1)      { r1 = c; g1 = x; }
            else if (hp < 2) { r1 = x; g1 = c; }
            else if (hp < 3) { g1 = c; b1 = x; }
            else if (hp < 4) { g1 = x; b1 = c; }
            else if (hp < 5) { r1 = x; b1 = c; }
            else             { r1 = c; b1 = x; }
            const m = l - c / 2;
            return [
                Math.round((r1 + m) * 255),
                Math.round((g1 + m) * 255),
                Math.round((b1 + m) * 255),
            ];
        };
        for (let i = 0; i < N; i++) {
            const norm = i / (N - 1);
            let seg = 0;
            while (seg < stops.length - 2 && norm > stops[seg + 1][0]) seg++;
            const s0 = stops[seg], s1 = stops[seg + 1];
            const t = (norm - s0[0]) / (s1[0] - s0[0]);
            const h = s0[1] + t * (s1[1] - s0[1]);
            const s = (s0[2] + t * (s1[2] - s0[2])) / 100;
            const l = (s0[3] + t * (s1[3] - s0[3])) / 100;
            const [r, g, b] = hslToRgb(h, s, l);
            lut[i * 3] = r;
            lut[i * 3 + 1] = g;
            lut[i * 3 + 2] = b;
        }
        _colorLUT = lut;
        _colorLUTN = N;
    }

    /** Downsample a spectrum array to at most MAX_BINS bins (averaging). */
    function _downsample(spectrum, target) {
        const n = spectrum.length;
        if (n <= target) {
            // pad with the last value to reach `target` so geometry stays stable
            const out = new Float32Array(target);
            for (let i = 0; i < target; i++) out[i] = i < n ? spectrum[i] : spectrum[n - 1] || _dbMin;
            return out;
        }
        const out = new Float32Array(target);
        const ratio = n / target;
        for (let i = 0; i < target; i++) {
            const start = Math.floor(i * ratio);
            const end = Math.min(n, Math.floor((i + 1) * ratio));
            let sum = 0, cnt = 0;
            for (let j = start; j < end; j++) { sum += spectrum[j]; cnt++; }
            out[i] = cnt > 0 ? sum / cnt : _dbMin;
        }
        return out;
    }

    /**
     * Push a new spectrum line into the ring buffer + trigger a geometry update.
     * spectrum: Int16Array | Float32Array | number[]  (dB values per bin)
     */
    function feed(spectrum, dbMin, dbMax) {
        if (!_inited || !spectrum || !spectrum.length) return;
        if (dbMin !== undefined) _dbMin = dbMin;
        if (dbMax !== undefined) _dbMax = dbMax;

        const line = _downsample(spectrum, _bins);
        // Write into the ring buffer at the current write row.
        const off = _writeRow * _bins;
        for (let i = 0; i < _bins; i++) _history[off + i] = line[i];
        _writeRow = (_writeRow + 1) % ROWS;

        _updateGeometry();
        _advanceLabels();   // scroll frequency labels back in time with the terrain
    }

    /**
     * Rebuild vertex Y positions + colors from the history ring buffer.
     * Newest row (writeRow - 1) maps to Z = 0 (nearest camera); older rows
     * recede towards +Z.
     */
    function _updateGeometry() {
        const bins = _bins;
        const range = _dbMax - _dbMin || 1;
        const lutN = _colorLUTN;
        const lutNMinus1 = lutN - 1;
        const lut = _colorLUT;
        const dbMin = _dbMin;

        // The newest row we just wrote is (_writeRow - 1 + ROWS) % ROWS.
        // displayRow 0 → newest (Z=0), displayRow (ROWS-1) → oldest (Z=max).
        for (let displayRow = 0; displayRow < ROWS; displayRow++) {
            const dataRow = (_writeRow - 1 - displayRow + ROWS) % ROWS;
            const dataOff = dataRow * bins;
            const z = displayRow * ROW_Z_STEP;
            for (let b = 0; b < bins; b++) {
                const db = _history[dataOff + b];
                let norm = (db - dbMin) / range;
                if (norm < 0) norm = 0; else if (norm > 1) norm = 1;
                const vIdx = (displayRow * bins + b) * 3;
                // Y = signal height
                _positions[vIdx + 1] = norm * HEIGHT_SCALE;
                // Color
                let lutIdx = (norm * lutNMinus1) | 0;
                const lutOff = lutIdx * 3;
                _colors[vIdx]     = lut[lutOff] / 255;
                _colors[vIdx + 1] = lut[lutOff + 1] / 255;
                _colors[vIdx + 2] = lut[lutOff + 2] / 255;
            }
        }
        _geometry.attributes.position.needsUpdate = true;
        _geometry.attributes.color.needsUpdate = true;
        _geometry.computeVertexNormals();
    }

    function _onResize() {
        if (!_container || !_renderer || !_camera) return;
        const w = _container.clientWidth;
        const h = _container.clientHeight;
        if (w <= 0 || h <= 0) return;
        _renderer.setSize(w, h, false);
        _camera.aspect = w / h;
        _camera.updateProjectionMatrix();
    }

    function _renderLoop() {
        _rafId = requestAnimationFrame(_renderLoop);
        if (_controls) _controls.update();
        if (_renderer && _scene && _camera) _renderer.render(_scene, _camera);
    }

    /**
     * Lazily initialize the Three.js scene inside `containerEl`.
     * Returns true on success, false if WebGL / THREE is unavailable.
     */
    function init(containerEl) {
        // Clean up any previous instance first.
        if (_inited) dispose();
        if (typeof THREE === 'undefined') {
            console.error('Waterfall3D: THREE is not loaded');
            return false;
        }
        if (!containerEl) return false;
        _container = containerEl;

        const w = _container.clientWidth || 800;
        const h = _container.clientHeight || 400;

        try {
            _renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
        } catch (e) {
            console.error('Waterfall3D: WebGL not supported', e);
            return false;
        }
        _renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
        _renderer.setSize(w, h, false);
        _renderer.setClearColor(0x0a0e17, 1);
        _container.appendChild(_renderer.domElement);

        _scene = new THREE.Scene();
        _scene.fog = new THREE.Fog(0x0a0e17, 60, 140);

        _camera = new THREE.PerspectiveCamera(50, w / h, 0.1, 500);
        _camera.position.set(0, 30, 46);
        _camera.lookAt(0, 4, 12);

        // === Lighting ===
        _scene.add(new THREE.AmbientLight(0x445566, 0.7));
        const dir = new THREE.DirectionalLight(0xffffff, 0.9);
        dir.position.set(20, 40, 20);
        _scene.add(dir);
        const back = new THREE.DirectionalLight(0x335577, 0.5);
        back.position.set(-10, 20, -20);
        _scene.add(back);

        // === Terrain mesh ===
        _bins = MAX_BINS;
        _buildLUT();
        _buildMesh();

        // Grid helper on the floor for spatial reference.
        const grid = new THREE.GridHelper(PLANE_D * 1.6, 16, 0x1e3a5f, 0x122036);
        grid.position.y = -0.05;
        _scene.add(grid);

        // === OrbitControls ===
        if (THREE.OrbitControls) {
            _controls = new THREE.OrbitControls(_camera, _renderer.domElement);
            _controls.enableDamping = true;
            _controls.dampingFactor = 0.1;
            _controls.minDistance = 20;
            _controls.maxDistance = 130;
            _controls.maxPolarAngle = Math.PI * 0.49; // don't go under the floor
            _controls.target.set(0, 4, 12);
            _controls.update();
        }

        // Resize handling
        if (window.ResizeObserver && _container) {
            _ro = new ResizeObserver(_onResize);
            _ro.observe(_container);
        }
        window.addEventListener('resize', _onResize);

        _inited = true;
        _onResize();
        _renderLoop();
        console.log('📊 Waterfall3D initialized');
        return true;
    }

    let _ro = null; // ResizeObserver

    /** Build (or rebuild) the plane mesh + history buffer for `_bins` bins. */
    function _buildMesh() {
        if (_mesh) {
            _scene.remove(_mesh);
            _mesh.geometry.dispose();
            _mesh.material.dispose();
            _mesh = null;
        }
        const bins = _bins;
        // PlaneGeometry(width, height, widthSegments, heightSegments)
        // segments = (vertices - 1); we want bins vertices across and ROWS along depth.
        const geo = new THREE.PlaneGeometry(PLANE_W, PLANE_D, bins - 1, ROWS - 1);
        // Plane is in XY by default; rotate so it lies in XZ (Y = up).
        geo.rotateX(-Math.PI / 2);
        // After rotation, the plane spans X (width) and Z (depth), Y = 0.

        const vertCount = bins * ROWS;
        const positions = geo.attributes.position.array;
        const colors = new Float32Array(vertCount * 3);

        // Keep a reference to the X/Z layout; we only mutate Y (+ colors).
        _positions = positions;
        _colors = colors;

        // Set initial positions for every vertex according to our convention:
        //   X spread evenly across -halfW..+halfW (frequency axis)
        //   Z = displayRow * ROW_Z_STEP (0 near camera → PLANE_D far)
        const halfW = PLANE_W / 2;
        for (let r = 0; r < ROWS; r++) {
            const z = r * ROW_Z_STEP;   // 0 (near) … PLANE_D (far)
            for (let b = 0; b < bins; b++) {
                const idx = (r * bins + b) * 3;
                const x = -halfW + (b / (bins - 1)) * PLANE_W;
                _positions[idx]     = x;
                _positions[idx + 1] = 0; // Y set later
                _positions[idx + 2] = z;
            }
        }

        geo.setAttribute('color', new THREE.BufferAttribute(colors, 3));
        geo.attributes.position.needsUpdate = true;

        const mat = new THREE.MeshStandardMaterial({
            vertexColors: true,
            side: THREE.DoubleSide,
            roughness: 0.75,
            metalness: 0.05,
            flatShading: false,
        });

        _mesh = new THREE.Mesh(geo, mat);
        _scene.add(_mesh);

        _geometry = geo;
        _history = new Float32Array(ROWS * bins);
        _history.fill(_dbMin);
        _writeRow = 0;
    }

    // ============================================================
    // === Signal labels (frequency tags above peaks) =============
    // ============================================================
    // Each label is a THREE.Sprite (billboard text "145.500 MHz") + a vertical
    // stick line from the floor to the peak. Labels are spawned by
    // setSignalLabels() (called from app.js each spectrum frame with the
    // detected signals). Once spawned, a label scrolls backward in time (its
    // Z increases by ROW_Z_STEP per feed()) until it passes PLANE_D, then it
    // is removed. New labels for the same frequency are suppressed for
    // REFRESH_ROWS rows so a continuous signal re-tags ~once per second
    // rather than every frame.

    /** Map a frequency (Hz) to world-space X on the freq axis. */
    function _freqToX(freqHz) {
        // _freqMap describes the ORIGINAL (un-downsampled) bin layout.
        // X spans -PLANE_W/2 .. +PLANE_W/2 across lowHz .. lowHz+sr.
        const lowHz = _freqMap.lowHz;
        const sr = _freqMap.sr || 1;
        const frac = (freqHz - lowHz) / sr;   // 0..1 left→right
        return -PLANE_W / 2 + frac * PLANE_W;
    }

    /** Create a CanvasTexture for a given frequency text. */
    function _makeLabelTexture(text) {
        const c = document.createElement('canvas');
        c.width = 256; c.height = 64;
        const ctx = c.getContext('2d');
        ctx.clearRect(0, 0, c.width, c.height);
        // rounded pill background
        ctx.fillStyle = 'rgba(10, 20, 35, 0.78)';
        const r = 14;
        ctx.beginPath();
        ctx.moveTo(r, 6);
        ctx.lineTo(c.width - r, 6);
        ctx.quadraticCurveTo(c.width - 2, 6, c.width - 2, 6 + r);
        ctx.lineTo(c.width - 2, c.height - r - 6);
        ctx.quadraticCurveTo(c.width - 2, c.height - 6, c.width - r, c.height - 6);
        ctx.lineTo(r, c.height - 6);
        ctx.quadraticCurveTo(2, c.height - 6, 2, c.height - r - 6);
        ctx.lineTo(2, 6 + r);
        ctx.quadraticCurveTo(2, 6, r, 6);
        ctx.closePath();
        ctx.fill();
        ctx.strokeStyle = 'rgba(120, 200, 255, 0.85)';
        ctx.lineWidth = 2;
        ctx.stroke();
        // text
        ctx.font = 'bold 34px monospace';
        ctx.fillStyle = '#8cdcff';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.shadowColor = 'rgba(0,0,0,0.8)';
        ctx.shadowBlur = 4;
        ctx.fillText(text, c.width / 2, c.height / 2);
        const tex = new THREE.CanvasTexture(c);
        tex.minFilter = THREE.LinearFilter;
        tex.magFilter = THREE.LinearFilter;
        return tex;
    }

    function _fmtFreq(freqHz) {
        // Show MHz with 4 decimals (kHz precision).
        if (freqHz >= 1e6) return (freqHz / 1e6).toFixed(4) + ' MHz';
        return (freqHz / 1e3).toFixed(1) + ' kHz';
    }

    /**
     * Build a new label (sprite + stick) at the given world position.
     */
    function _createLabel(freqHz, x, peakY) {
        const text = _fmtFreq(freqHz);
        const tex = _makeLabelTexture(text);
        const mat = new THREE.SpriteMaterial({ map: tex, depthTest: true, depthWrite: false });
        const sprite = new THREE.Sprite(mat);
        sprite.scale.set(LABEL_SCALE, LABEL_SCALE * (tex.image.height / tex.image.width), 1);
        sprite.position.set(x, peakY + LABEL_Y_OFFSET, 0);
        _scene.add(sprite);

        // Stick: vertical line from floor (y=0) to peak.
        const stickMat = new THREE.LineBasicMaterial({ color: 0x66bbff, transparent: true, opacity: 0.7 });
        const stickGeo = new THREE.BufferGeometry();
        stickGeo.setAttribute('position', new THREE.BufferAttribute(new Float32Array(6), 3));
        const sp = stickGeo.attributes.position.array;
        sp[0] = x; sp[1] = 0; sp[2] = 0;
        sp[3] = x; sp[4] = peakY; sp[5] = 0;
        const stick = new THREE.Line(stickGeo, stickMat);
        _scene.add(stick);

        _labels.push({ sprite, stick, freqHz, x, z: 0, peakY });
    }

    /** Remove a single label and free its GPU resources. */
    function _removeLabel(i) {
        const lbl = _labels[i];
        if (!lbl) return;
        _scene.remove(lbl.sprite);
        if (lbl.sprite.material) {
            if (lbl.sprite.material.map) lbl.sprite.material.map.dispose();
            lbl.sprite.material.dispose();
        }
        _scene.remove(lbl.stick);
        if (lbl.stick.geometry) lbl.stick.geometry.dispose();
        if (lbl.stick.material) lbl.stick.material.dispose();
        _labels.splice(i, 1);
    }

    /** Clear all labels (used on dispose / signal-tag toggle off). */
    function _clearAllLabels() {
        while (_labels.length > 0) _removeLabel(_labels.length - 1);
    }

    /**
     * Advance every label's Z by one ROW_Z_STEP (it scrolls backward in time).
     * Remove labels that have scrolled past the far edge (PLANE_D).
     */
    function _advanceLabels() {
        if (!_scene) return;
        for (let i = _labels.length - 1; i >= 0; i--) {
            const lbl = _labels[i];
            lbl.z += ROW_Z_STEP;
            lbl.sprite.position.z = lbl.z;
            lbl.stick.position.z = lbl.z;
            if (lbl.z > PLANE_D + ROW_Z_STEP) {
                _removeLabel(i);
            }
        }
    }

    /**
     * Called by app.js once per spectrum frame with the currently-detected
     * signals and the frequency mapping context.
     *
     * signals:  array of { freq, rightFreq } (Hz) — may be empty.
     * ctx:      { lowHz, binHz, numBins, sr, centerHz } — original (pre-downsample)
     *           bin layout so we can map freq→X exactly like the 2D view.
     *
     * Spawning rules:
     *   - Skip if a label for a nearby freq (±DEDUP_HZ) already exists at the
     *     near edge (z < REFRESH_ROWS * ROW_Z_STEP). This avoids duplicate
     *     labels for the same continuous signal.
     *   - Hard cap: never exceed MAX_LABELS; if full, drop the oldest first.
     */
    function setSignalLabels(signals, ctx) {
        if (!_inited || !_scene) return;
        if (ctx) _freqMap = ctx;

        if (!signals || signals.length === 0) return;

        const nearZ = REFRESH_ROWS * ROW_Z_STEP; // "near" zone for dedup
        for (const sig of signals) {
            const freqHz = sig.freq;
            if (!isFinite(freqHz)) continue;

            // Dedup: is there already a label for a nearby freq near the front?
            const dup = _labels.some(l =>
                Math.abs(l.freqHz - freqHz) < DEDUP_HZ && l.z < nearZ
            );
            if (dup) continue;

            // Cap: if at the limit, remove the oldest (largest Z).
            if (_labels.length >= MAX_LABELS) {
                let oldestIdx = 0, oldestZ = -Infinity;
                for (let i = 0; i < _labels.length; i++) {
                    if (_labels[i].z > oldestZ) { oldestZ = _labels[i].z; oldestIdx = i; }
                }
                _removeLabel(oldestIdx);
            }

            // Compute the peak height at this freq from the latest row.
            const x = _freqToX(freqHz);
            const halfW = PLANE_W / 2;
            // Clamp X to the plane (in case the freq is at/over the edge).
            const cx = Math.max(-halfW, Math.min(halfW, x));
            const bFrac = (cx + halfW) / PLANE_W;       // 0..1
            const bIdx = Math.min(_bins - 1, Math.max(0, Math.round(bFrac * (_bins - 1))));
            const dataRow = (_writeRow - 1 + ROWS) % ROWS; // newest row
            const db = _history[dataRow * _bins + bIdx];
            const range = _dbMax - _dbMin || 1;
            let norm = (db - _dbMin) / range;
            if (norm < 0) norm = 0; else if (norm > 1) norm = 1;
            const peakY = norm * HEIGHT_SCALE;

            _createLabel(freqHz, cx, peakY);
        }
    }

    /** Tear down the scene, free GPU resources, stop the render loop. */
    function dispose() {
        if (_rafId) { cancelAnimationFrame(_rafId); _rafId = null; }
        if (_ro) { _ro.disconnect(); _ro = null; }
        window.removeEventListener('resize', _onResize);
        if (_controls) { _controls.dispose(); _controls = null; }
        if (_mesh) {
            if (_mesh.geometry) _mesh.geometry.dispose();
            if (_mesh.material) _mesh.material.dispose();
            _mesh = null;
        }
        _geometry = null;
        _positions = null;
        _colors = null;
        _history = null;
        _clearAllLabels();
        if (_renderer) {
            if (_renderer.domElement && _renderer.domElement.parentNode) {
                _renderer.domElement.parentNode.removeChild(_renderer.domElement);
            }
            _renderer.dispose();
            _renderer = null;
        }
        _scene = null;
        _camera = null;
        _inited = false;
        console.log('📊 Waterfall3D disposed');
    }

    return { init, feed, dispose, setSignalLabels };
})();