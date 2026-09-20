/* Norrin chart renderer.
 *
 * Plain SVG, no library, no CDN, no build step -- the same constraint the rest
 * of this UI works under, and the right one for a plant terminal that may never
 * see the internet. Every function here takes an element and a spec, measures
 * the element, and writes SVG into it. Nothing in this file fetches anything;
 * the caller brings the numbers.
 *
 * Colours come from the validated palette (blue #2a78d6 / orange #eb6834 on a
 * white surface: worst adjacent CVD deltaE 24.7, normal-vision 33.6, both over
 * their floors). Status colours are the reserved four and always ship next to a
 * word, never alone.
 *
 * House rules, kept deliberately: 2px lines, hairline solid gridlines, a wash
 * for reference bands, no dual axis anywhere -- when two series have different
 * units they are indexed to their own control limit and share one scale.
 */
(function (global) {
  "use strict";

  var C = {
    series1: "#2a78d6",
    series2: "#eb6834",
    good: "#0ca30c",
    warning: "#fab219",
    serious: "#ec835a",
    critical: "#d03b3b",
    ink: "#0b0b0b",
    inkSecondary: "#52514e",
    muted: "#898781",
    grid: "#e1e0d9",
    axis: "#c3c2b7",
    surface: "#ffffff"
  };

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  /* Axis ticks a human would have chosen: 1, 2, 2.5 or 5 times a power of ten. */
  function niceStep(span, count) {
    if (!(span > 0)) return 1;
    var raw = span / Math.max(1, count);
    var mag = Math.pow(10, Math.floor(Math.log10(raw)));
    var norm = raw / mag;
    var step = norm > 5 ? 10 : norm > 2.5 ? 5 : norm > 2 ? 2.5 : norm > 1 ? 2 : 1;
    return step * mag;
  }

  function ticks(lo, hi, count) {
    var step = niceStep(hi - lo, count);
    var start = Math.ceil(lo / step) * step;
    var out = [];
    for (var v = start; v <= hi + step * 1e-6 && out.length < 12; v += step) {
      out.push(Math.abs(v) < step * 1e-9 ? 0 : v);
    }
    return out;
  }

  function fmt(v, step) {
    if (v == null || !isFinite(v)) return "--";
    var abs = Math.abs(v);
    var decimals = step != null && step < 1 ? Math.min(4, Math.ceil(-Math.log10(step))) :
                   abs >= 1000 ? 0 : abs >= 10 ? 1 : abs >= 1 ? 2 : 3;
    var s = v.toFixed(decimals);
    return s.replace(/\B(?=(\d{3})+(?!\d))/g, function (m, g, off, str) {
      return str.indexOf(".") === -1 || off < str.indexOf(".") ? "," : "";
    });
  }

  function extent(values) {
    var lo = Infinity, hi = -Infinity;
    for (var i = 0; i < values.length; i++) {
      var v = values[i];
      if (v == null || !isFinite(v)) continue;
      if (v < lo) lo = v;
      if (v > hi) hi = v;
    }
    if (lo === Infinity) return [0, 1];
    if (lo === hi) return [lo - 1, hi + 1];
    return [lo, hi];
  }

  function path(xs, ys) {
    var d = "", started = false;
    for (var i = 0; i < xs.length; i++) {
      if (ys[i] == null || !isFinite(ys[i])) { started = false; continue; }
      d += (started ? "L" : "M") + xs[i].toFixed(1) + " " + ys[i].toFixed(1) + " ";
      started = true;
    }
    return d.trim();
  }

  /* ---------------------------------------------------------------------
   * The one shared frame: plot box, gridlines, axes, hover surface.
   * ------------------------------------------------------------------- */
  function frame(el, spec) {
    var width = Math.max(240, el.clientWidth || 640);
    var height = spec.height || 220;
    var pad = { top: 18, right: spec.padRight == null ? 56 : spec.padRight, bottom: 26, left: 56 };
    return {
      width: width, height: height, pad: pad,
      plotW: width - pad.left - pad.right,
      plotH: height - pad.top - pad.bottom
    };
  }

  function axes(f, xDomain, yDomain, yTickValues, xLabel) {
    var out = "";
    var step = yTickValues.length > 1 ? yTickValues[1] - yTickValues[0] : null;
    yTickValues.forEach(function (v) {
      var y = f.pad.top + f.plotH * (1 - (v - yDomain[0]) / (yDomain[1] - yDomain[0]));
      if (y < f.pad.top - 1 || y > f.pad.top + f.plotH + 1) return;
      out += '<line x1="' + f.pad.left + '" y1="' + y.toFixed(1) + '" x2="' + (f.pad.left + f.plotW) +
             '" y2="' + y.toFixed(1) + '" stroke="' + C.grid + '" stroke-width="1"/>';
      out += '<text x="' + (f.pad.left - 8) + '" y="' + (y + 3.5).toFixed(1) +
             '" text-anchor="end" font-size="10" fill="' + C.muted +
             '" style="font-variant-numeric:tabular-nums">' + esc(fmt(v, step)) + '</text>';
    });

    var xt = ticks(xDomain[0], xDomain[1], 6);
    xt.forEach(function (v) {
      var x = f.pad.left + f.plotW * ((v - xDomain[0]) / Math.max(1e-9, xDomain[1] - xDomain[0]));
      if (x < f.pad.left - 1 || x > f.pad.left + f.plotW + 1) return;
      out += '<text x="' + x.toFixed(1) + '" y="' + (f.pad.top + f.plotH + 16) +
             '" text-anchor="middle" font-size="10" fill="' + C.muted +
             '" style="font-variant-numeric:tabular-nums">' + esc(fmt(v, 1)) + '</text>';
    });

    out += '<line x1="' + f.pad.left + '" y1="' + (f.pad.top + f.plotH) + '" x2="' + (f.pad.left + f.plotW) +
           '" y2="' + (f.pad.top + f.plotH) + '" stroke="' + C.axis + '" stroke-width="1"/>';
    if (xLabel) {
      out += '<text x="' + (f.pad.left + f.plotW) + '" y="' + (f.height - 2) +
             '" text-anchor="end" font-size="9.5" fill="' + C.muted + '">' + esc(xLabel) + '</text>';
    }
    return out;
  }

  var SEV_COLOR = { high: C.critical, medium: C.serious, low: C.warning };

  function eventBands(f, xDomain, events, tick) {
    var out = "";
    (events || []).forEach(function (ev) {
      var start = ev.start_sample, end = ev.end_sample == null ? ev.start_sample : ev.end_sample;
      if (tick != null && start > tick) return;
      if (tick != null) end = Math.min(end, tick);
      var x0 = f.pad.left + f.plotW * ((start - xDomain[0]) / Math.max(1e-9, xDomain[1] - xDomain[0]));
      var x1 = f.pad.left + f.plotW * ((end - xDomain[0]) / Math.max(1e-9, xDomain[1] - xDomain[0]));
      var w = Math.max(2, x1 - x0);
      var colour = SEV_COLOR[ev.severity] || C.warning;
      out += '<rect x="' + x0.toFixed(1) + '" y="' + f.pad.top + '" width="' + w.toFixed(1) +
             '" height="' + f.plotH + '" fill="' + colour + '" opacity="0.10"/>';
      out += '<rect x="' + x0.toFixed(1) + '" y="' + f.pad.top + '" width="' + w.toFixed(1) +
             '" height="2" fill="' + colour + '"/>';
    });
    return out;
  }

  /* Crosshair and tooltip. An SVG chart in a browser is interactive by default;
   * shipping one without a readout makes the operator guess at values. */
  function wireHover(el, f, xDomain, samples, readout) {
    var svg = el.querySelector("svg");
    if (!svg) return;
    var line = svg.querySelector(".nx-crosshair");
    var tip = el.querySelector(".nx-tip");
    if (!tip) {
      tip = document.createElement("div");
      tip.className = "nx-tip";
      el.appendChild(tip);
    }

    function hide() { if (line) line.setAttribute("opacity", "0"); tip.style.display = "none"; }

    svg.addEventListener("mouseleave", hide);
    svg.addEventListener("mousemove", function (e) {
      var box = svg.getBoundingClientRect();
      var px = e.clientX - box.left;
      if (px < f.pad.left || px > f.pad.left + f.plotW || !samples.length) { hide(); return; }
      var value = xDomain[0] + (px - f.pad.left) / f.plotW * (xDomain[1] - xDomain[0]);
      var idx = 0, best = Infinity;
      for (var i = 0; i < samples.length; i++) {
        var d = Math.abs(samples[i] - value);
        if (d < best) { best = d; idx = i; }
      }
      var x = f.pad.left + f.plotW * ((samples[idx] - xDomain[0]) / Math.max(1e-9, xDomain[1] - xDomain[0]));
      if (line) { line.setAttribute("opacity", "1"); line.setAttribute("x1", x); line.setAttribute("x2", x); }
      tip.innerHTML = readout(idx);
      tip.style.display = "block";
      var tipW = tip.offsetWidth || 140;
      tip.style.left = Math.min(Math.max(0, x - tipW / 2), f.width - tipW) + "px";
      tip.style.top = "0px";
    });
  }

  function mount(el, f, body) {
    el.style.position = "relative";
    el.innerHTML =
      '<svg width="' + f.width + '" height="' + f.height + '" viewBox="0 0 ' + f.width + ' ' + f.height +
      '" role="img" style="display:block">' + body +
      '<line class="nx-crosshair" y1="' + f.pad.top + '" y2="' + (f.pad.top + f.plotH) +
      '" stroke="' + C.muted + '" stroke-width="1" opacity="0"/></svg>';
  }

  /* ---------------------------------------------------------------------
   * One channel over time, against the range S2 called normal for it.
   * ------------------------------------------------------------------- */
  function channel(el, spec) {
    var samples = spec.samples || [], values = spec.values || [];
    var f = frame(el, spec);
    if (!samples.length) {
      el.innerHTML = '<div class="nx-empty">No samples yet at this point in the replay.</div>';
      return;
    }

    var xDomain = [spec.xMin == null ? samples[0] : spec.xMin,
                   spec.xMax == null ? samples[samples.length - 1] : spec.xMax];
    if (xDomain[1] <= xDomain[0]) xDomain[1] = xDomain[0] + 1;

    var span = extent(values);
    var band = spec.band;
    if (band && isFinite(band.lo) && isFinite(band.hi)) {
      span = [Math.min(span[0], band.lo), Math.max(span[1], band.hi)];
    }
    var padY = (span[1] - span[0]) * 0.12;
    var yDomain = [span[0] - padY, span[1] + padY];

    var X = function (s) { return f.pad.left + f.plotW * ((s - xDomain[0]) / (xDomain[1] - xDomain[0])); };
    var Y = function (v) { return f.pad.top + f.plotH * (1 - (v - yDomain[0]) / (yDomain[1] - yDomain[0])); };

    var body = "";
    if (band && isFinite(band.lo) && isFinite(band.hi)) {
      // Reference range stays grey: it is context, not a second series, and the
      // line has to be the only loud thing in the frame.
      body += '<rect x="' + f.pad.left + '" y="' + Y(band.hi).toFixed(1) + '" width="' + f.plotW +
              '" height="' + Math.max(1, Y(band.lo) - Y(band.hi)).toFixed(1) +
              '" fill="' + C.muted + '" opacity="0.12"/>';
      body += '<line x1="' + f.pad.left + '" y1="' + Y(band.mid).toFixed(1) + '" x2="' + (f.pad.left + f.plotW) +
              '" y2="' + Y(band.mid).toFixed(1) + '" stroke="' + C.muted + '" stroke-width="1" opacity="0.55"/>';
    }
    body += eventBands(f, xDomain, spec.events, spec.tick);
    body += axes(f, xDomain, yDomain, ticks(yDomain[0], yDomain[1], 4), spec.xLabel || "sample");

    var xs = samples.map(X), ys = values.map(function (v) { return v == null ? null : Y(v); });
    body += '<path d="' + path(xs, ys) + '" fill="none" stroke="' + C.series1 +
            '" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>';

    var lastX = xs[xs.length - 1], lastY = ys[ys.length - 1];
    if (lastY != null) {
      body += '<circle cx="' + lastX.toFixed(1) + '" cy="' + lastY.toFixed(1) +
              '" r="4.5" fill="' + C.series1 + '" stroke="' + C.surface + '" stroke-width="2"/>';
      body += '<text x="' + Math.min(f.width - 4, lastX + 9).toFixed(1) + '" y="' + (lastY + 3.5).toFixed(1) +
              '" font-size="11" font-weight="600" text-anchor="' + (lastX + 60 > f.width ? "end" : "start") +
              '" fill="' + C.ink + '">' + esc(fmt(values[values.length - 1])) + '</text>';
    }

    mount(el, f, body);
    var unit = spec.unit ? " " + spec.unit : "";
    wireHover(el, f, xDomain, samples, function (i) {
      var deviation = band && band.sigma ? ((values[i] - band.mid) / band.sigma) : null;
      return '<b>sample ' + samples[i] + '</b><br>' + esc(fmt(values[i])) + esc(unit) +
             (deviation != null && isFinite(deviation)
               ? '<br><span class="nx-tip-sub">' + (deviation >= 0 ? "+" : "") +
                 deviation.toFixed(1) + ' sigma from normal</span>' : "");
    });
  }

  /* ---------------------------------------------------------------------
   * The detector's own scores. Two statistics, two units -- so each is
   * divided by its own control limit and 1.0 means "at the limit". One
   * scale, one axis: a second y-axis here would be the classic lie.
   * ------------------------------------------------------------------- */
  function score(el, spec) {
    var samples = spec.samples || [];
    var f = frame(el, spec);
    if (!samples.length) {
      el.innerHTML = '<div class="nx-empty">No detector scores yet at this point in the replay.</div>';
      return;
    }

    var t2 = spec.t2 || [], spe = spec.spe || [];
    var span = extent(t2.concat(spe));
    var yDomain = [0, Math.max(1.35, span[1] * 1.12)];
    var xDomain = [spec.xMin == null ? samples[0] : spec.xMin,
                   spec.xMax == null ? samples[samples.length - 1] : spec.xMax];
    if (xDomain[1] <= xDomain[0]) xDomain[1] = xDomain[0] + 1;

    var X = function (s) { return f.pad.left + f.plotW * ((s - xDomain[0]) / (xDomain[1] - xDomain[0])); };
    var Y = function (v) { return f.pad.top + f.plotH * (1 - (v - yDomain[0]) / (yDomain[1] - yDomain[0])); };

    var body = eventBands(f, xDomain, spec.events, spec.tick);
    body += axes(f, xDomain, yDomain, ticks(yDomain[0], yDomain[1], 4), spec.xLabel || "sample");

    var limitY = Y(1);
    body += '<line x1="' + f.pad.left + '" y1="' + limitY.toFixed(1) + '" x2="' + (f.pad.left + f.plotW) +
            '" y2="' + limitY.toFixed(1) + '" stroke="' + C.critical + '" stroke-width="1.5"/>';
    body += '<text x="' + (f.pad.left + f.plotW + 6) + '" y="' + (limitY + 3.5).toFixed(1) +
            '" font-size="10" font-weight="600" fill="' + C.inkSecondary + '">limit</text>';

    var xs = samples.map(X);
    [[t2, C.series1, "T2"], [spe, C.series2, "SPE"]].forEach(function (pair) {
      if (!pair[0].length) return;
      var ys = pair[0].map(function (v) { return v == null ? null : Y(v); });
      body += '<path d="' + path(xs, ys) + '" fill="none" stroke="' + pair[1] +
              '" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>';
      var ly = ys[ys.length - 1];
      if (ly != null) {
        body += '<circle cx="' + xs[xs.length - 1].toFixed(1) + '" cy="' + ly.toFixed(1) +
                '" r="4" fill="' + pair[1] + '" stroke="' + C.surface + '" stroke-width="2"/>';
      }
    });

    mount(el, f, body);
    wireHover(el, f, xDomain, samples, function (i) {
      return '<b>sample ' + samples[i] + '</b><br>' +
             'T2 ' + esc(fmt(t2[i])) + ' x limit<br>SPE ' + esc(fmt(spe[i])) + ' x limit';
    });
  }

  /* A 12-point trace for a grid tile. No axes, no labels: it answers "is this
   * one moving?" and hands the rest to the drill-down. */
  function sparkline(values, opts) {
    opts = opts || {};
    var w = opts.width || 96, h = opts.height || 26, pad = 3;
    if (!values || values.length < 2) {
      return '<svg width="' + w + '" height="' + h + '" aria-hidden="true"></svg>';
    }
    var span = extent(values);
    var stroke = opts.color || C.muted;
    var xs = values.map(function (_, i) { return pad + (w - pad * 2) * i / (values.length - 1); });
    var ys = values.map(function (v) {
      return pad + (h - pad * 2) * (1 - (v - span[0]) / Math.max(1e-9, span[1] - span[0]));
    });
    var last = '<circle cx="' + xs[xs.length - 1].toFixed(1) + '" cy="' + ys[ys.length - 1].toFixed(1) +
               '" r="2.5" fill="' + stroke + '" stroke="' + C.surface + '" stroke-width="1.5"/>';
    return '<svg width="' + w + '" height="' + h + '" viewBox="0 0 ' + w + ' ' + h +
           '" aria-hidden="true" style="display:block"><path d="' + path(xs, ys) +
           '" fill="none" stroke="' + stroke + '" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/>' +
           last + '</svg>';
  }

  /* Confidence as a filled track. The number is always beside it -- a bar on
   * its own invites reading 60% as "fine". */
  function meter(pct, opts) {
    opts = opts || {};
    var w = opts.width || 132, h = 8, value = Math.max(0, Math.min(100, Number(pct) || 0));
    var fill = value >= 75 ? C.series1 : value >= 45 ? C.warning : C.serious;
    return '<svg width="' + w + '" height="' + h + '" viewBox="0 0 ' + w + ' ' + h +
           '" aria-hidden="true" style="display:block">' +
           '<rect x="0" y="0" width="' + w + '" height="' + h + '" rx="4" fill="#e8eef7"/>' +
           '<rect x="0" y="0" width="' + (w * value / 100).toFixed(1) + '" height="' + h +
           '" rx="4" fill="' + fill + '"/></svg>';
  }

  /* How much of the deviation each channel accounts for. One measure, one hue;
   * the value rides the tip of its own bar. */
  function attribution(el, items, opts) {
    opts = opts || {};
    items = (items || []).slice(0, 8);
    if (!items.length) { el.innerHTML = '<div class="nx-empty">No attribution recorded.</div>'; return; }

    var rowH = 26, barH = 14, labelW = opts.labelW || 78, valueW = 46;
    var width = Math.max(240, el.clientWidth || 420);
    var plotW = width - labelW - valueW;
    var max = Math.max.apply(null, items.map(function (d) { return Math.abs(d.share || 0); })) || 1;
    var height = items.length * rowH + 4;

    var body = "";
    items.forEach(function (d, i) {
      var y = i * rowH + 2;
      var w = Math.max(2, plotW * Math.abs(d.share || 0) / max);
      // A row-wide transparent target: the bar for a 0.8% share is four pixels
      // wide, and nobody should have to hit that.
      body += '<rect class="nx-row" data-col="' + esc(d.col_id) + '" x="0" y="' + (y - 3) + '" width="' + width +
              '" height="' + rowH + '" fill="transparent" style="cursor:pointer"/>';
      body += '<text x="0" y="' + (y + barH - 2) + '" font-size="11" font-weight="600" fill="' + C.ink +
              '" pointer-events="none">' + esc(d.label || d.col_id) + '</text>';
      body += '<path d="M' + labelW + ' ' + y + ' h' + (w - 4).toFixed(1) + ' a4 4 0 0 1 4 4 v' + (barH - 8) +
              ' a4 4 0 0 1 -4 4 h-' + (w - 4).toFixed(1) + ' z" fill="' + (opts.color || C.series1) +
              '" pointer-events="none"/>';
      body += '<text x="' + (labelW + w + 7).toFixed(1) + '" y="' + (y + barH - 2) +
              '" font-size="11" fill="' + C.inkSecondary + '" pointer-events="none"' +
              ' style="font-variant-numeric:tabular-nums">' +
              esc((Math.abs(d.share || 0) * 100).toFixed(1)) + '%</text>';
    });

    el.innerHTML = '<svg width="' + width + '" height="' + height + '" viewBox="0 0 ' + width + ' ' + height +
                   '" style="display:block">' + body + '</svg>';

    if (opts.onSelect) {
      Array.prototype.forEach.call(el.querySelectorAll(".nx-row"), function (row) {
        row.addEventListener("click", function () { opts.onSelect(row.getAttribute("data-col")); });
      });
    }
  }

  /* Several channels on one chart, each already normalised by the caller to the
   * same unit. The rule this respects is the one that gets broken most often:
   * one axis, and it means the same thing for every line on it. If two measures
   * cannot be put on a shared scale honestly, they get two charts instead. */
  function multi(el, spec) {
    var samples = spec.samples || [];
    var keys = Object.keys(spec.series || {});
    var f = frame(el, { height: spec.height || 300, padRight: 120 });
    if (!samples.length || !keys.length) {
      el.innerHTML = '<div class="nx-empty">Nothing to draw yet.</div>';
      return;
    }

    var all = [];
    keys.forEach(function (k) { all = all.concat(spec.series[k]); });
    var span = extent(all);
    var padY = (span[1] - span[0]) * 0.1;
    var yDomain = [span[0] - padY, span[1] + padY];
    var xDomain = [samples[0], samples[samples.length - 1]];
    if (xDomain[1] <= xDomain[0]) xDomain[1] = xDomain[0] + 1;

    var X = function (s) { return f.pad.left + f.plotW * ((s - xDomain[0]) / (xDomain[1] - xDomain[0])); };
    var Y = function (v) { return f.pad.top + f.plotH * (1 - (v - yDomain[0]) / (yDomain[1] - yDomain[0])); };

    var body = eventBands(f, xDomain, spec.events, null);
    body += axes(f, xDomain, yDomain, ticks(yDomain[0], yDomain[1], 5), spec.unit || "sample");

    if (yDomain[0] < 0 && yDomain[1] > 0) {
      body += '<line x1="' + f.pad.left + '" y1="' + Y(0).toFixed(1) + '" x2="' + (f.pad.left + f.plotW) +
              '" y2="' + Y(0).toFixed(1) + '" stroke="' + C.axis + '" stroke-width="1"/>';
    }

    var palette = [C.series1, C.series2, "#1baf7a", "#eda100", "#4a3aa7", "#e87ba4"];
    var xs = samples.map(X);
    var ends = [];
    keys.forEach(function (key, i) {
      var colour = palette[i % palette.length];
      var ys = spec.series[key].map(function (v) { return v == null ? null : Y(v); });
      body += '<path d="' + path(xs, ys) + '" fill="none" stroke="' + colour +
              '" stroke-width="2" stroke-linejoin="round" stroke-linecap="round" opacity="0.9"/>';
      var ly = ys[ys.length - 1];
      if (ly != null) {
        body += '<circle cx="' + xs[xs.length - 1].toFixed(1) + '" cy="' + ly.toFixed(1) +
                '" r="4" fill="' + colour + '" stroke="' + C.surface + '" stroke-width="2"/>';
        ends.push({ y: ly, colour: colour, text: (spec.labels && spec.labels[i]) || key });
      }
    });

    // End labels detach from their lines when they collide, so they are nudged
    // apart in order and only ever downward from their own position.
    ends.sort(function (a, b) { return a.y - b.y; });
    var lastY = -Infinity;
    ends.forEach(function (end) {
      var y = Math.max(end.y, lastY + 13);
      lastY = y;
      body += '<text x="' + (f.pad.left + f.plotW + 10) + '" y="' + (y + 3.5).toFixed(1) +
              '" font-size="10.5" font-weight="600" fill="' + C.ink + '">' + esc(end.text) + '</text>';
      body += '<line x1="' + (f.pad.left + f.plotW + 2) + '" y1="' + end.y.toFixed(1) +
              '" x2="' + (f.pad.left + f.plotW + 7) + '" y2="' + y.toFixed(1) +
              '" stroke="' + end.colour + '" stroke-width="1.5"/>';
    });

    mount(el, f, body);
    wireHover(el, f, xDomain, samples, function (i) {
      return '<b>sample ' + samples[i] + '</b><br>' + keys.map(function (k, n) {
        return ((spec.labels && spec.labels[n]) || k) + " " + fmt(spec.series[k][i]);
      }).join("<br>");
    });
  }

  global.NorrinCharts = {
    multi: multi,
    channel: channel,
    score: score,
    sparkline: sparkline,
    meter: meter,
    attribution: attribution,
    colors: C,
    format: fmt
  };
})(window);
