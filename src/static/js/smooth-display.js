/**
 * smooth-display.js
 *
 * Production-ready real-time cycling data display with smooth animations.
 * Designed for 4Hz WebSocket data (250ms intervals) rendered at 60fps.
 *
 * Classes:
 *   AnimatedValue   - Smoothly transitions a displayed number using rAF
 *   DataInterpolator - Interpolates 4Hz data to 60fps
 *   SmoothGraph     - Scrolling canvas time-series with zone-colored fill
 *
 * Usage:
 *   const display = initSmoothDisplay({ ftp: 260, maxHr: 190, restHr: 55 });
 *   // On each WS message:
 *   display.onData(telemetryObject);
 *   // To tear down:
 *   display.destroy();
 *
 * Vanilla JS, zero dependencies, minimal GC pressure.
 */

'use strict';

// ---------------------------------------------------------------------------
// AnimatedValue
// ---------------------------------------------------------------------------
// Smoothly transitions a displayed number from current to target over a
// configurable duration using requestAnimationFrame.  Avoids layout thrash
// by only writing textContent when the rounded string actually changes.
// ---------------------------------------------------------------------------

class AnimatedValue {
  /**
   * @param {string} elementId  - DOM element whose textContent will be set
   * @param {Object} [opts]
   * @param {number} [opts.duration=300]   - animation duration in ms
   * @param {number} [opts.decimals=0]     - decimal places to display
   * @param {string} [opts.suffix='']      - appended after the number (e.g. ' km')
   * @param {string} [opts.prefix='']      - prepended before the number
   * @param {string} [opts.placeholder='---'] - shown when value is null/undefined
   * @param {function} [opts.format]       - optional custom formatter(value)->string
   */
  constructor(elementId, opts) {
    const o = opts || {};
    this._elId = elementId;
    this._el = null;                       // lazily resolved
    this._duration = o.duration || 300;
    this._decimals = o.decimals || 0;
    this._suffix = o.suffix || '';
    this._prefix = o.prefix || '';
    this._placeholder = o.placeholder || '---';
    this._format = o.format || null;
    this._current = 0;
    this._from = 0;
    this._to = 0;
    this._startTime = 0;
    this._animating = false;
    this._lastText = '';                   // dedup writes
    this._rafId = 0;
    this._isNull = true;                   // no value received yet
    // Pre-bind to avoid closure allocation per frame
    this._tick = this._tick.bind(this);
  }

  /** Resolve element lazily (supports elements created after construction). */
  _resolve() {
    if (!this._el) {
      this._el = document.getElementById(this._elId);
    }
    return this._el;
  }

  /**
   * Start smooth animation toward `newValue`.
   * Pass null/undefined to show the placeholder.
   */
  update(newValue) {
    if (newValue == null) {
      this._isNull = true;
      this._animating = false;
      if (this._rafId) { cancelAnimationFrame(this._rafId); this._rafId = 0; }
      this._write(this._placeholder);
      return;
    }
    const v = +newValue;
    if (v !== v) return; // NaN guard
    this._isNull = false;
    this._from = this._current;
    this._to = v;
    if (!this._animating) {
      this._animating = true;
      this._startTime = performance.now();
      this._rafId = requestAnimationFrame(this._tick);
    } else {
      // Retarget mid-animation: keep current position, reset timer
      this._from = this._current;
      this._startTime = performance.now();
    }
  }

  /** Internal rAF callback -- hot path, no allocations. */
  _tick(now) {
    const elapsed = now - this._startTime;
    const t = Math.min(elapsed / this._duration, 1);
    // Ease-out cubic for natural deceleration
    const ease = 1 - (1 - t) * (1 - t) * (1 - t);
    this._current = this._from + (this._to - this._from) * ease;

    this._render();

    if (t < 1) {
      this._rafId = requestAnimationFrame(this._tick);
    } else {
      this._current = this._to;
      this._render();
      this._animating = false;
      this._rafId = 0;
    }
  }

  /** Format and write if changed. */
  _render() {
    let text;
    if (this._format) {
      text = this._format(this._current);
    } else {
      text = this._prefix + this._current.toFixed(this._decimals) + this._suffix;
    }
    this._write(text);
  }

  /** Only touch the DOM when the string actually changed. */
  _write(text) {
    if (text === this._lastText) return;
    this._lastText = text;
    const el = this._resolve();
    if (el) el.textContent = text;
  }

  /** Immediately set value without animation (for initial load). */
  set(value) {
    if (value == null) {
      this._isNull = true;
      this._write(this._placeholder);
      return;
    }
    this._current = +value;
    this._from = this._current;
    this._to = this._current;
    this._isNull = false;
    this._render();
  }

  /** Get the current displayed value. */
  get value() {
    return this._isNull ? null : this._current;
  }

  /** Cancel pending animation and release element reference. */
  destroy() {
    if (this._rafId) cancelAnimationFrame(this._rafId);
    this._rafId = 0;
    this._animating = false;
    this._el = null;
  }
}


// ---------------------------------------------------------------------------
// DataInterpolator
// ---------------------------------------------------------------------------
// Receives data frames at arbitrary intervals (typically 4Hz / 250ms) and
// provides smooth interpolated values at any timestamp for 60fps rendering.
// Uses linear interpolation between the two most recent frames.
//
// Ring buffer of fixed size avoids GC churn.
// ---------------------------------------------------------------------------

class DataInterpolator {
  /**
   * @param {string[]} fields  - which numeric fields to interpolate
   * @param {number} [bufferSize=16] - frames to keep (power of 2 preferred)
   */
  constructor(fields, bufferSize) {
    this._fields = fields;
    this._size = bufferSize || 16;
    // Ring buffer: pre-allocated array of frame objects
    this._buf = new Array(this._size);
    for (let i = 0; i < this._size; i++) {
      this._buf[i] = { t: 0 };
      for (let f = 0; f < fields.length; f++) {
        this._buf[i][fields[f]] = 0;
      }
    }
    this._head = 0;      // next write index
    this._count = 0;     // frames stored
    // Reusable output object -- returned by getInterpolated, never allocate new
    this._out = {};
    for (let f = 0; f < fields.length; f++) {
      this._out[fields[f]] = 0;
    }
  }

  /**
   * Store a new data frame.  `data.t` should be a monotonic timestamp (ms).
   * If `data.t` is not set, performance.now() is used.
   */
  pushFrame(data) {
    const slot = this._buf[this._head % this._size];
    slot.t = data.t != null ? data.t : performance.now();
    const fields = this._fields;
    for (let f = 0; f < fields.length; f++) {
      const key = fields[f];
      slot[key] = data[key] != null ? +data[key] : slot[key]; // hold last if missing
    }
    this._head++;
    if (this._count < this._size) this._count++;
  }

  /**
   * Return interpolated values at time `t` (ms, same clock as pushFrame).
   * Returns the same reusable object every call -- do NOT store a reference.
   */
  getInterpolated(t) {
    const out = this._out;
    if (this._count === 0) return out;
    if (this._count === 1) {
      const only = this._buf[(this._head - 1 + this._size) % this._size];
      const fields = this._fields;
      for (let f = 0; f < fields.length; f++) {
        out[fields[f]] = only[fields[f]];
      }
      return out;
    }
    // Find the two frames bracketing `t`
    const newest = this._buf[(this._head - 1 + this._size) % this._size];
    const prev = this._buf[(this._head - 2 + this._size) % this._size];

    if (t >= newest.t) {
      // Extrapolate / hold newest
      const fields = this._fields;
      for (let f = 0; f < fields.length; f++) {
        out[fields[f]] = newest[fields[f]];
      }
      return out;
    }
    if (t <= prev.t) {
      const fields = this._fields;
      for (let f = 0; f < fields.length; f++) {
        out[fields[f]] = prev[fields[f]];
      }
      return out;
    }
    // Interpolate
    const span = newest.t - prev.t;
    const frac = span > 0 ? (t - prev.t) / span : 0;
    const fields = this._fields;
    for (let f = 0; f < fields.length; f++) {
      const key = fields[f];
      out[key] = prev[key] + (newest[key] - prev[key]) * frac;
    }
    return out;
  }

  /** Reset the buffer. */
  clear() {
    this._head = 0;
    this._count = 0;
  }
}


// ---------------------------------------------------------------------------
// SmoothGraph
// ---------------------------------------------------------------------------
// High-performance scrolling time-series on <canvas>.
//
// Features:
//   - Zone-colored filled area (power zones based on FTP)
//   - Smooth per-pixel scrolling (not discrete jumps)
//   - 1200-point history ring buffer (5 min at 4 Hz — v3.6.0-fix27d-4hz;
//     was 600 at 1 Hz. Bumped when backend started broadcasting the
//     latest BLE power sample on every 250 ms lite tick for reference HUD
//     parity — one graph point per pedal-stroke sample.)
//   - FTP reference line with label
//   - HR overlay polyline
//   - 5-point moving average for power smoothing
//   - Runs its own rAF loop or can be driven externally
//
// Performance notes:
//   - Single offscreen ImageData not used (compositing zone colors requires
//     path-based drawing).  Instead, we batch draw calls by zone color to
//     minimize state changes.
//   - All arrays are pre-allocated and reused.
//   - No string concatenation or DOM access in the draw loop.
// ---------------------------------------------------------------------------

class SmoothGraph {
  /**
   * @param {string|HTMLCanvasElement} canvas - id or element
   * @param {Object} opts
   * @param {number} opts.ftp           - functional threshold power
   * @param {number} [opts.maxHr=196]   - athlete max heart rate
   * @param {number} [opts.restHr=60]   - athlete resting heart rate
   * @param {number} [opts.lthr=170]    - lactate threshold HR (for zone bands)
   * @param {number} [opts.historySize=1200] - data points to keep
   *   (v3.6.0-fix27d-4hz: default sized for 4-Hz broadcasts over a
   *    5-min visible window — 300 s × 4 samples/s. Was 600.)
   * @param {number} [opts.pixelsPerPoint=1.35] - horizontal density
   * @param {boolean} [opts.showHrAxis=true] - render HR tick labels + zone bands
   */
  constructor(canvas, opts) {
    const o = opts || {};
    this._canvas = typeof canvas === 'string' ? document.getElementById(canvas) : canvas;
    this._ctx = this._canvas ? this._canvas.getContext('2d') : null;
    this._ftp = o.ftp || 250;
    this._maxHr = o.maxHr || 196;
    this._restHr = o.restHr || 60;
    this._lthr = o.lthr || 170;
    // v3.6.0-fix26 §4.3: Critical Power reference line on the power
    // graph. Rendered as a dashed tick so users see when they're above
    // vs below the CP threshold (the depletion boundary for W'bal).
    // 0 hides the line; set via `graph.cp = N` as telemetry broadcasts
    // `cp_w`.
    this._cp = o.cp || 0;
    this._showHrAxis = o.showHrAxis !== false;
    // v3.6.0-fix27d-4hz: 1200 = 5 min at 4 Hz (reference HUD parity). Caller
    // can still override, e.g. for a smaller viewport. Reject explicit
    // values below 1200 too — the backend now emits at 4 Hz, a smaller
    // buffer would truncate the visible history to <5 min.
    this._histSize = o.historySize || 1200;
    this._pxPerPt = o.pixelsPerPoint || 0; // 0 = auto-fit to canvas width

    // Ring buffers for raw data
    this._powers = new Float32Array(this._histSize);
    this._hrs = new Float32Array(this._histSize);
    this._head = 0;
    this._count = 0;

    // Pre-allocated scratch arrays for smoothed power
    this._smoothed = new Float32Array(this._histSize);
    // v3.6.0-fix31-live-hr-smooth: scratch for 3-point rolling mean HR (NaN-
    // preserving — gaps break the polyline, same policy as post-ride fix31
    // SVG renderer). Separate array so raw this._hrs[] retains the sentinels
    // (NaN = no physical reading this tick).
    this._smoothedHr = new Float32Array(this._histSize);

    // Scroll offset for sub-pixel smooth scrolling (0..1 fraction of one point width)
    this._scrollFrac = 0;
    this._lastPushTime = 0;
    this._pushInterval = 1000; // expected ms between pushes (updated dynamically)

    // Zone color lookup -- matches the app's existing palette
    this._zoneColors = [
      '#64748b',  // Z1  <56% FTP
      '#3b82f6',  // Z2  56-75%
      '#22c55e',  // Z3  76-90%  (green, matching existing zc())
      '#eab308',  // Z4  91-105%
      '#f97316',  // Z5  106-120%
      '#ef4444',  // Z6  121-150%
      '#a855f7',  // Z7  >150%   (purple)
    ];

    // HR zone colors (LTHR-anchored, matching :root --z1..--z5)
    // Z1 gray, Z2 blue, Z3 green, Z4 yellow, Z5 orange
    this._hrZoneColors = [
      '#64748b',  // Z1  <82% LTHR
      '#3b82f6',  // Z2  82-89%
      '#22c55e',  // Z3  89-94%
      '#eab308',  // Z4  94-100%
      '#f97316',  // Z5  >=100%
    ];
    // Upper-bound percentages of LTHR for each zone (matches app conventions)
    this._hrZoneEdgesPct = [0.82, 0.89, 0.94, 1.00];

    // Animation state
    this._rafId = 0;
    this._running = false;
    this._drawBound = this._draw.bind(this);

    // Resize handling
    this._onResize = this._handleResize.bind(this);
    this._resizeObserver = null;
    this._W = 0;
    this._H = 0;
    this._dpr = window.devicePixelRatio || 1;

    if (this._canvas) {
      this._setupSize();
      this._installResizeObserver();
    }
  }

  // -- Public API ----------------------------------------------------------

  /** Push a data point. Call this on every WS frame.
   * v3.6.0-fix26 URG-R2-4: caller passes NaN for missing/unphysical HR; we
   * preserve NaN through the ring buffer so the draw loop can render a gap
   * instead of a spurious 0-bpm dip. Power is still clamped to 0 for gaps.
   */
  push(power, hr) {
    const now = performance.now();
    if (this._lastPushTime > 0) {
      // Exponential moving average of push interval for smooth scroll timing
      const dt = now - this._lastPushTime;
      this._pushInterval = this._pushInterval * 0.7 + dt * 0.3;
    }
    this._lastPushTime = now;

    const idx = this._head % this._histSize;
    this._powers[idx] = power || 0;
    // Preserve NaN as sentinel for "no HR this tick" (Float32Array keeps NaN
    // intact). If caller passes a legitimate 0 or null, we still treat it as
    // missing — HR < 30 bpm is unphysical (see fix26 §0.8).
    const hrNum = (typeof hr === 'number' && hr >= 30) ? hr : NaN;
    this._hrs[idx] = hrNum;
    this._head++;
    if (this._count < this._histSize) this._count++;

    // Reset scroll fraction on new data (will animate from 0 to 1 until next push)
    this._scrollFrac = 0;
  }

  /** Update FTP (e.g. after settings change). */
  set ftp(v) { this._ftp = v || 250; }
  get ftp() { return this._ftp; }

  /** v3.6.0-fix26 §4.3: Critical Power reference line (watts). Rendered
   * as a dashed horizontal tick with a small 'CP' label — complements
   * the existing FTP line so users see the W'bal-relevant threshold.
   * Set to 0 (or null) to hide. */
  set cp(v) { this._cp = v || 0; }
  get cp() { return this._cp; }

  /** Update HR range. */
  setHrRange(rest, max) {
    this._restHr = rest || 60;
    this._maxHr = max || 196;
  }

  /** Update LTHR (drives HR zone bands). */
  set lthr(v) { this._lthr = v || 170; }
  get lthr() { return this._lthr; }

  /** Start the internal rAF draw loop. */
  start() {
    if (this._running) return;
    this._running = true;
    this._rafId = requestAnimationFrame(this._drawBound);
  }

  /** Stop the internal draw loop. */
  stop() {
    this._running = false;
    if (this._rafId) { cancelAnimationFrame(this._rafId); this._rafId = 0; }
  }

  /** Single draw call (for external rAF loops). */
  draw() { this._draw(performance.now()); }

  /** Clean up. */
  destroy() {
    this.stop();
    if (this._resizeObserver) {
      this._resizeObserver.disconnect();
      this._resizeObserver = null;
    }
    this._ctx = null;
    this._canvas = null;
  }

  // -- Internals -----------------------------------------------------------

  _setupSize() {
    const c = this._canvas;
    const parent = c.parentElement;
    if (!parent) return;
    const rect = parent.getBoundingClientRect();
    this._dpr = window.devicePixelRatio || 1;
    this._W = Math.round(rect.width);
    this._H = Math.round(rect.height);
    c.width = this._W * this._dpr;
    c.height = this._H * this._dpr;
    c.style.width = this._W + 'px';
    c.style.height = this._H + 'px';
    if (this._ctx) this._ctx.setTransform(this._dpr, 0, 0, this._dpr, 0, 0);
  }

  _installResizeObserver() {
    if (typeof ResizeObserver === 'undefined') return;
    const parent = this._canvas.parentElement;
    if (!parent) return;
    this._resizeObserver = new ResizeObserver(() => { this._setupSize(); });
    this._resizeObserver.observe(parent);
  }

  _handleResize() { this._setupSize(); }

  /** Determine zone color for a given wattage. */
  _zoneColor(watts) {
    const pct = watts / this._ftp * 100;
    if (pct < 56) return this._zoneColors[0];
    if (pct < 76) return this._zoneColors[1];
    if (pct < 91) return this._zoneColors[2];
    if (pct < 106) return this._zoneColors[3];
    if (pct < 121) return this._zoneColors[4];
    if (pct < 150) return this._zoneColors[5];
    return this._zoneColors[6];
  }

  /** 2-point moving average into this._smoothed[] (v3.6.0-fix28: 5pt→2pt to preserve 4-Hz pedal-stroke ripple). */
  _smooth() {
    const n = this._count;
    const raw = this._powers;
    const out = this._smoothed;
    const base = this._head - n;
    for (let i = 0; i < n; i++) {
      const lo = Math.max(0, i - 1);
      const hi = i;
      let sum = 0;
      for (let j = lo; j <= hi; j++) {
        sum += raw[(base + j + this._histSize) % this._histSize];
      }
      out[i] = sum / (hi - lo + 1);
    }
  }

  /** 3-point rolling mean into this._smoothedHr[] — NaN-preserving so gaps
   *  render as polyline breaks rather than spike-to-zero.  Parity with the
   *  post-ride fix31 SVG smoothing (templates/dashboard.html).  HR is
   *  visually noisier beat-to-beat than power, so a 3-pt window (0.75 s at
   *  4 Hz) trims sawtooth without masking real pacing drift.
   *  v3.6.0-fix31-live-hr-smooth.
   */
  _smoothHrFilter() {
    const n = this._count;
    const raw = this._hrs;
    const out = this._smoothedHr;
    const base = this._head - n;
    for (let i = 0; i < n; i++) {
      const lo = Math.max(0, i - 1);
      const hi = Math.min(n - 1, i + 1);
      let sum = 0;
      let cnt = 0;
      for (let j = lo; j <= hi; j++) {
        const v = raw[(base + j + this._histSize) % this._histSize];
        // Skip NaN / unphysical samples — do not let a missing beat drag
        // the mean to zero (post-ride fix31 bug class).
        if (v >= 30 && v <= 230) {
          sum += v;
          cnt++;
        }
      }
      // If the window has no valid samples, propagate NaN so the polyline
      // breaks at exactly the same index as the raw ring buffer would.
      out[i] = cnt > 0 ? (sum / cnt) : NaN;
    }
  }

  /**
   * Draw HR Y-axis: faint zone-colored bands (LTHR-anchored), faint grid
   * lines at each tick, and white numeric BPM labels on the left edge.
   * Tick positions adapt to the visible HR range, snapping to multiples of
   * 20 BPM that fall within [hMin, hMax].  Defaults yield 100/120/140/160/180
   * for typical athlete ranges.
   */
  _drawHrAxis(ctx, W, H, PAD, hMin, hMax, hRange) {
    // -- Zone bands (LTHR-anchored), low-alpha background --
    const lthr = this._lthr || 170;
    const edges = this._hrZoneEdgesPct;
    const colors = this._hrZoneColors;
    const yOf = bpm => H - ((bpm - hMin) / hRange) * (H - PAD);

    // Build zone boundary BPM list: [hMin, 0.82*lthr, 0.89*lthr, 0.94*lthr,
    //                                 1.00*lthr, hMax]
    const bounds = [hMin];
    for (let i = 0; i < edges.length; i++) bounds.push(edges[i] * lthr);
    bounds.push(hMax);

    ctx.save();
    for (let i = 0; i < colors.length; i++) {
      const lo = Math.max(hMin, Math.min(hMax, bounds[i]));
      const hi = Math.max(hMin, Math.min(hMax, bounds[i + 1]));
      if (hi <= lo) continue;
      const yTop = yOf(hi);
      const yBot = yOf(lo);
      ctx.fillStyle = colors[i];
      ctx.globalAlpha = 0.08;
      ctx.fillRect(0, yTop, W, yBot - yTop);
    }
    ctx.restore();

    // -- Tick positions: multiples of 20 within [hMin, hMax], capped at 6 --
    const startTick = Math.ceil(hMin / 20) * 20;
    const ticks = [];
    for (let v = startTick; v <= hMax && ticks.length < 6; v += 20) {
      if (v > hMin + 2) ticks.push(v); // skip ticks crammed against bottom
    }

    // -- Grid lines + labels --
    ctx.save();
    ctx.lineWidth = 1;
    ctx.font = '10px -apple-system, system-ui, sans-serif';
    ctx.textAlign = 'left';
    ctx.textBaseline = 'middle';
    for (let i = 0; i < ticks.length; i++) {
      const v = ticks[i];
      const y = yOf(v);
      // Faint horizontal grid line
      ctx.strokeStyle = 'rgba(255,255,255,0.08)';
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(W, y);
      ctx.stroke();
      // White numeric BPM label, left-aligned with subtle shadow for legibility
      ctx.fillStyle = 'rgba(0,0,0,0.55)';
      ctx.fillText(String(v), 4, y + 1);
      ctx.fillStyle = '#ffffff';
      ctx.fillText(String(v), 3, y);
    }
    ctx.restore();
  }

  /** Main draw -- called via rAF or externally. */
  _draw(now) {
    if (this._running) {
      this._rafId = requestAnimationFrame(this._drawBound);
    }

    const ctx = this._ctx;
    if (!ctx || this._count < 2) return;
    const W = this._W;
    const H = this._H;
    if (W < 10 || H < 10) return;

    // Compute smooth scroll fraction (0 to 1 between data pushes)
    if (this._lastPushTime > 0 && this._pushInterval > 0) {
      const elapsed = now - this._lastPushTime;
      this._scrollFrac = Math.min(elapsed / this._pushInterval, 1);
    }

    // Smooth the power data
    this._smooth();
    // v3.6.0-fix31-live-hr-smooth: HR now rendered from the 3-pt rolling
    // mean (same 0.75 s window as post-ride fix31) so the live polyline
    // no longer sawtooths beat-to-beat.
    this._smoothHrFilter();

    const n = this._count;
    const smoothed = this._smoothed;
    const hrs = this._smoothedHr;
    const base = this._head - n;

    // Compute power scale — reference-parity: anchored to FTP, expands briefly
    // for sprints via a rolling 30 s peak window (4 Hz samples).
    // Fixes "160 W hits top when max_seen=170 W" for low-watt rides on a
    // 1000-W-capable rider: baseline 1.2×FTP stays visible regardless of
    // recent intensity, while short peaks lift the ceiling by +5 % headroom.
    const ftp = this._ftp || 250;
    const PEAK_WINDOW_S = 30;
    const nSamples = Math.min(n, PEAK_WINDOW_S * 4);  // 4 Hz data rate
    let recentPeak = 0;
    for (let i = n - nSamples; i < n; i++) {
      const v = smoothed[i] || 0;
      if (v > recentPeak) recentPeak = v;
    }
    const pMax = Math.max(1.2 * ftp, recentPeak * 1.05);
    const pMin = 0;
    const pRange = pMax - pMin || 1;

    // HR scale — reference-parity: fixed to athlete range (rest_hr-10 to max_hr).
    const restHr = this._restHr || 55;
    const hMin = Math.max(40, restHr - 10);
    const hMax = this._maxHr;
    const hRange = hMax - hMin || 1;

    // Pixels per data point
    const MAX = this._histSize;
    const ppx = this._pxPerPt > 0 ? this._pxPerPt : W / MAX;
    const scrollPx = this._scrollFrac * ppx;

    // Clear
    ctx.clearRect(0, 0, W, H);

    // --- Zone-colored filled power area ---
    // Batch segments by color to reduce state changes
    const PAD = 4; // top/bottom padding in px

    // --- HR Y-axis: zone bands + grid lines + numeric BPM tick labels ---
    // Drawn first so the data series sits on top of the background bands.
    // Skipped on narrow canvases to avoid clutter (e.g. small mobile widths).
    const showAxis = this._showHrAxis && W >= 200;
    if (showAxis) {
      this._drawHrAxis(ctx, W, H, PAD, hMin, hMax, hRange);
    }

    // --- reference-parity power zone bands ---
    // 6 horizontal bands at FTP × [0, 0.60, 0.75, 0.89, 1.04, 1.18, ∞] with
    // alpha ~0.15 (hex '26'). Palette: grey, blue, green, yellow, orange, red.
    // Drawn at low alpha so the foreground zone-colored power fill (0.25 alpha)
    // and the power stroke remain dominant.
    const zoneBoundaries = [0, 0.60, 0.75, 0.89, 1.04, 1.18];
    const zoneColors = ['#64748b', '#3b82f6', '#10b981', '#eab308', '#f97316', '#ef4444'];
    for (let z = 0; z < zoneBoundaries.length; z++) {
      const upperMul = zoneBoundaries[z + 1] != null ? zoneBoundaries[z + 1] : 2.0;
      const yTop = H - ((upperMul * ftp - pMin) / pRange) * (H - PAD);
      const yBot = H - ((zoneBoundaries[z] * ftp - pMin) / pRange) * (H - PAD);
      const yTopClamped = Math.max(0, yTop);
      const yBotClamped = Math.min(H, yBot);
      if (yBotClamped <= yTopClamped) continue;
      ctx.fillStyle = zoneColors[z] + '26';
      ctx.fillRect(0, yTopClamped, W, yBotClamped - yTopClamped);
    }

    // We draw column-by-column (each data point interval), filling from
    // the power line down to the baseline.  For smooth scrolling, the
    // newest point slides in from the right edge.

    ctx.globalAlpha = 0.25;
    let prevColor = '';
    let pathStarted = false;

    for (let i = 1; i < n; i++) {
      const offset = MAX - n;
      const x1 = (offset + i - 1) * ppx - scrollPx;
      const x2 = (offset + i) * ppx - scrollPx;
      if (x2 < 0) continue;
      if (x1 > W) break;

      const y1 = H - ((smoothed[i - 1] - pMin) / pRange) * (H - PAD);
      const y2 = H - ((smoothed[i] - pMin) / pRange) * (H - PAD);
      const col = this._zoneColor(smoothed[i]);

      // Fill trapezoid
      ctx.fillStyle = col;
      ctx.beginPath();
      ctx.moveTo(x1, y1);
      ctx.lineTo(x2, y2);
      ctx.lineTo(x2, H);
      ctx.lineTo(x1, H);
      ctx.closePath();
      ctx.fill();
    }

    // --- Power stroke line ---
    ctx.globalAlpha = 1;
    ctx.lineWidth = 2;
    let prevX = 0;
    let prevY = 0;
    let started = false;

    for (let i = 0; i < n; i++) {
      const offset = MAX - n;
      const x = (offset + i) * ppx - scrollPx;
      if (x < -ppx) continue;
      if (x > W + ppx) break;

      const y = H - ((smoothed[i] - pMin) / pRange) * (H - PAD);

      if (!started) {
        started = true;
        prevX = x;
        prevY = y;
        continue;
      }

      const col = this._zoneColor(smoothed[i]);
      ctx.strokeStyle = col;
      ctx.beginPath();
      ctx.moveTo(prevX, prevY);
      ctx.lineTo(x, y);
      ctx.stroke();
      prevX = x;
      prevY = y;
    }

    // --- FTP reference line ---
    const ftpY = H - ((this._ftp - pMin) / pRange) * (H - PAD);
    if (ftpY > 5 && ftpY < H - 5) {
      ctx.strokeStyle = 'rgba(255,255,255,0.2)';
      ctx.lineWidth = 1;
      ctx.setLineDash([4, 3]);
      ctx.beginPath();
      ctx.moveTo(0, ftpY);
      ctx.lineTo(W, ftpY);
      ctx.stroke();
      ctx.setLineDash([]);

      // FTP label
      ctx.fillStyle = 'rgba(255,255,255,0.35)';
      ctx.font = '8px -apple-system, system-ui, sans-serif';
      ctx.textAlign = 'right';
      ctx.fillText('FTP', W - 3, ftpY - 2);
    }

    // --- CP reference line (v3.6.0-fix26 §4.3) ---
    // Distinct from FTP so the user can visually see the W'bal-relevant
    // threshold. When CP ≈ FTP (within a few W) the labels overlap;
    // we nudge the CP label left of FTP to avoid collision. A 0/undefined
    // CP hides the tick (fallback when no CP source wired).
    if (this._cp > 0) {
      const cpY = H - ((this._cp - pMin) / pRange) * (H - PAD);
      if (cpY > 5 && cpY < H - 5) {
        ctx.strokeStyle = 'rgba(168, 85, 247, 0.45)';  // purple — distinct from white FTP
        ctx.lineWidth = 1;
        ctx.setLineDash([2, 4]);
        ctx.beginPath();
        ctx.moveTo(0, cpY);
        ctx.lineTo(W, cpY);
        ctx.stroke();
        ctx.setLineDash([]);

        ctx.fillStyle = 'rgba(168, 85, 247, 0.8)';
        ctx.font = '8px -apple-system, system-ui, sans-serif';
        ctx.textAlign = 'right';
        // Push CP label to the left of the FTP label if the two ticks
        // are within 15 px of each other (preserves both legible).
        const xOffset = (Math.abs(cpY - ftpY) < 15) ? 30 : 3;
        ctx.fillText('CP', W - xOffset, cpY - 2);
      }
    }

    // --- HR overlay polyline ---
    // v3.6.0-fix26 URG-R2-4: NaN samples (no physical reading this tick) break
    // the line — we `moveTo` the next valid sample instead of `lineTo(y=0)`.
    // Previously a missing beat rendered as a spike down to the baseline,
    // producing the "0 → beat → 0 → beat" oscillation the user screenshotted.
    // v3.6.0-fix31-live-hr-smooth: sample source is now the linear
    // this._smoothedHr[] scratch (3-pt rolling mean, NaN-preserving — see
    // _smoothHrFilter). Indexing is 0..n-1, NOT ring-buffer-modulo, because
    // the smoother outputs into a linear scratch.
    ctx.strokeStyle = '#ef4444';
    ctx.lineWidth = 1.5;
    ctx.globalAlpha = 0.5;
    ctx.beginPath();
    let hrPenDown = false; // true once we've drawn at least one valid segment

    for (let i = 0; i < n; i++) {
      const offset = MAX - n;
      const x = (offset + i) * ppx - scrollPx;
      if (x < -ppx) continue;
      if (x > W + ppx) break;

      const hrVal = hrs[i];
      // Skip NaN / unphysical / undefined samples — leave a gap.
      if (!(hrVal >= 30 && hrVal <= 230)) {
        hrPenDown = false;
        continue;
      }
      const y = H - ((hrVal - hMin) / hRange) * (H - PAD);
      if (!hrPenDown) {
        ctx.moveTo(x, y);
        hrPenDown = true;
      } else {
        ctx.lineTo(x, y);
      }
    }
    ctx.stroke();
    ctx.globalAlpha = 1;
  }
}


// ---------------------------------------------------------------------------
// CSS Transition Utilities
// ---------------------------------------------------------------------------
// Inject a <style> block with utility classes for smooth CSS transitions.
// These can be added to any element for animated property changes.
// ---------------------------------------------------------------------------

const _SMOOTH_CSS = `
/* Smooth transition utilities for real-time data display */
.smooth-color {
  transition: color 0.3s ease-out;
}
.smooth-bg {
  transition: background-color 0.3s ease-out;
}
.smooth-width {
  transition: width 0.3s ease-out;
}
.smooth-all {
  transition: all 0.3s ease-out;
}
.smooth-border {
  transition: border-color 0.3s ease-out, border 0.3s ease-out;
}
.smooth-opacity {
  transition: opacity 0.3s ease-out;
}
.smooth-transform {
  transition: transform 0.3s ease-out;
}

/* Faster variants for latency-sensitive fields */
.smooth-color-fast {
  transition: color 0.15s ease-out;
}
.smooth-bg-fast {
  transition: background-color 0.15s ease-out;
}
.smooth-width-fast {
  transition: width 0.15s ease-out;
}

/* Slower variants for less frequent changes */
.smooth-color-slow {
  transition: color 0.6s ease-out;
}
.smooth-bg-slow {
  transition: background-color 0.6s ease-out;
}
.smooth-width-slow {
  transition: width 0.6s ease-out;
}

/* Combined: color + background together */
.smooth-visual {
  transition: color 0.3s ease-out, background-color 0.3s ease-out,
              border-color 0.3s ease-out, box-shadow 0.3s ease-out;
}

/* Pulse animation for critical values (e.g. W'bal low) */
@keyframes smoothPulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.6; }
}
.smooth-pulse {
  animation: smoothPulse 1s ease-in-out infinite;
}
`;

function _injectCSS() {
  if (document.getElementById('smooth-display-css')) return;
  const style = document.createElement('style');
  style.id = 'smooth-display-css';
  style.textContent = _SMOOTH_CSS;
  document.head.appendChild(style);
}


// ---------------------------------------------------------------------------
// initSmoothDisplay  (integration function)
// ---------------------------------------------------------------------------
// Wires AnimatedValue instances to the known element IDs, creates the
// SmoothGraph on the existing <canvas id="data-graph">, sets up a unified
// rAF loop, and returns an interface for the WS handler.
//
// Usage in training.html:
//   const smoothUI = initSmoothDisplay({ ftp: STATE.ftp, maxHr: STATE.maxHr });
//   // Then in handleTelemetry(d):
//   smoothUI.onData(d);
// ---------------------------------------------------------------------------

function initSmoothDisplay(config) {
  const cfg = config || {};
  const ftp = cfg.ftp || 250;
  const maxHr = cfg.maxHr || 196;
  const restHr = cfg.restHr || Math.round(maxHr * 0.35);

  _injectCSS();

  // ---- Animated Values ----
  // Each maps to an element ID in the existing training.html layout.

  const animPower = new AnimatedValue('hero-power', {
    duration: 250,
    decimals: 0,
    placeholder: '---',
  });

  const animSpeed = new AnimatedValue('m-speed', {
    duration: 400,
    decimals: 1,
    placeholder: '---',
  });

  const animDist = new AnimatedValue('m-dist', {
    duration: 600,
    decimals: 1,
    suffix: ' km',
    placeholder: '---',
  });

  const animHr = new AnimatedValue('hr-big', {
    duration: 350,
    decimals: 0,
    placeholder: '---',
  });

  const animCad = new AnimatedValue('m-cad', {
    duration: 300,
    decimals: 0,
    placeholder: '---',
  });

  const animElev = new AnimatedValue('m-elev', {
    duration: 500,
    decimals: 0,
    suffix: 'm',
  });

  // Elapsed time needs a custom formatter (mm:ss or h:mm:ss)
  const animElapsed = new AnimatedValue('m-elapsed', {
    duration: 200,
    format: function(v) {
      const total = Math.round(v);
      const h = Math.floor(total / 3600);
      const m = Math.floor((total % 3600) / 60);
      const s = total % 60;
      const ss = s < 10 ? '0' + s : '' + s;
      const mm = (h > 0 && m < 10) ? '0' + m : '' + m;
      return h > 0 ? h + ':' + mm + ':' + ss : m + ':' + ss;
    },
  });

  const allAnimated = [animPower, animSpeed, animDist, animHr, animCad, animElev, animElapsed];

  // ---- Data Interpolator ----
  const interpolator = new DataInterpolator(
    ['power', 'speed', 'hr', 'cadence', 'elevation'],
    16
  );

  // ---- Smooth Graph ----
  // v3.6.0-fix27d-4hz: 1200 = 5 min × 4 Hz (reference HUD parity; backend
  // broadcasts each BLE power sample at 250 ms cadence on lite ticks).
  const graph = new SmoothGraph('data-graph', {
    ftp: ftp,
    maxHr: maxHr,
    restHr: restHr,
    historySize: 1200,
  });

  // ---- Add CSS transition classes to data elements ----
  const _addClass = function(id, cls) {
    const el = document.getElementById(id);
    if (el) el.classList.add(cls);
  };
  _addClass('hero-power', 'smooth-color-fast');
  _addClass('hr-big', 'smooth-color');
  _addClass('hr-stat', 'smooth-visual');
  _addClass('dev-fill', 'smooth-width-fast');
  _addClass('wbal-fill', 'smooth-width');
  _addClass('wbal-fill', 'smooth-bg');

  // ---- Unified rAF Loop ----
  // Instead of each AnimatedValue running its own rAF, we can also
  // drive the graph from here.  AnimatedValue still manages its own
  // rAF for responsiveness (it auto-starts on update()), but the graph
  // is driven from this central loop for consistency.
  let _loopRunning = false;
  let _loopRafId = 0;

  function _loop() {
    if (!_loopRunning) return;
    graph.draw();
    _loopRafId = requestAnimationFrame(_loop);
  }

  function startLoop() {
    if (_loopRunning) return;
    _loopRunning = true;
    _loopRafId = requestAnimationFrame(_loop);
  }

  function stopLoop() {
    _loopRunning = false;
    if (_loopRafId) { cancelAnimationFrame(_loopRafId); _loopRafId = 0; }
  }

  // ---- WS Data Handler ----
  // Replaces direct textContent writes with smooth animated updates.
  // Call this from handleTelemetry(d) instead of updateMetrics(d).

  function onData(d) {
    // Push to interpolator (using performance.now as timestamp)
    interpolator.pushFrame({
      t: performance.now(),
      power: d.power || 0,
      speed: d.speed || 0,
      hr: d.hr || 0,
      cadence: d.cadence || 0,
      elevation: d.elevation || (d.course ? d.course.elevation : 0) || 0,
    });

    // Animate numeric displays
    // v3.6.0-fix27 §2.2: read canonical `d.power` (raw instantaneous 1-s
    // mean from backend), NOT the legacy `d.display_power` alias. Both
    // fields normally hold the same value except during hold-last-value
    // gaps where `display_power` is `null`; reading `power` keeps the tile
    // consistent with the graph (which already reads `d.power`) and
    // eliminates the Path-B vs Path-A mismatch documented in the fix27
    // power-pipeline audit.
    animPower.update(d.power);
    animSpeed.update(d.speed);
    animHr.update(d.hr);
    animCad.update(d.cadence);

    // Distance
    if (d.distance != null) {
      animDist.update(d.distance);
    }

    // Elevation
    const elev = d.elevation || (d.course ? d.course.elevation : 0) || 0;
    animElev.update(elev);

    // Elapsed time
    if (d.t != null) {
      animElapsed.update(d.t);
    }

    // Push to graph — preserve NaN/null HR so the graph can render gaps
    // (not 0-bpm dips). See fix26 URG-R2-4.
    const _hrForGraph = (typeof d.hr === 'number' && d.hr >= 30) ? d.hr : NaN;
    graph.push(d.power || 0, _hrForGraph);

    // v3.6.0-fix26 §4.3: keep the CP tick in sync with the broadcast.
    // The server re-reads CP from the profile on every ride start, and
    // the ICU / Monod pipeline can update it between sessions, so the
    // graph should always mirror the live `cp_w` rather than a cached
    // init value.
    if (typeof d.cp_w === 'number' && d.cp_w > 0) {
      graph.cp = d.cp_w;
    }
  }

  // ---- Configuration Updates ----
  function updateConfig(newConfig) {
    if (newConfig.ftp != null) graph.ftp = newConfig.ftp;
    if (newConfig.cp != null) graph.cp = newConfig.cp;
    if (newConfig.maxHr != null || newConfig.restHr != null) {
      graph.setHrRange(
        newConfig.restHr != null ? newConfig.restHr : restHr,
        newConfig.maxHr != null ? newConfig.maxHr : maxHr
      );
    }
  }

  // ---- Start ----
  startLoop();

  // ---- Public Interface ----
  return {
    /** Feed a telemetry frame from the WebSocket. */
    onData: onData,

    /** Update display configuration (ftp, maxHr, restHr). */
    updateConfig: updateConfig,

    /** Access individual animated values for custom styling. */
    animated: {
      power: animPower,
      speed: animSpeed,
      distance: animDist,
      hr: animHr,
      cadence: animCad,
      elevation: animElev,
      elapsed: animElapsed,
    },

    /** Access the interpolator for custom 60fps rendering. */
    interpolator: interpolator,

    /** Access the graph for direct control. */
    graph: graph,

    /** Start the unified render loop (auto-started on init). */
    start: startLoop,

    /** Stop the unified render loop. */
    stop: stopLoop,

    /** Tear down everything. */
    destroy: function() {
      stopLoop();
      graph.destroy();
      for (let i = 0; i < allAnimated.length; i++) {
        allAnimated[i].destroy();
      }
    },
  };
}


// ---------------------------------------------------------------------------
// Export
// ---------------------------------------------------------------------------
// Works as a plain <script> (globals), ES module, or CommonJS.
// ---------------------------------------------------------------------------

if (typeof window !== 'undefined') {
  window.AnimatedValue = AnimatedValue;
  window.DataInterpolator = DataInterpolator;
  window.SmoothGraph = SmoothGraph;
  window.initSmoothDisplay = initSmoothDisplay;
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { AnimatedValue, DataInterpolator, SmoothGraph, initSmoothDisplay };
}
