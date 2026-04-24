/* reachy.js — watercolor Reachy Mini avatar, vanilla JS port of Reachy.jsx.
 * 6 postures : idle, listen, speak, read, alert, sleep (+ warn).
 * Usage:
 *   const avatar = Reachy.render('idle', 180);
 *   element.replaceChildren(avatar);
 * Returns a DOM element containing the watercolor wash + SVG figure.
 */
(function () {
  const SVG_NS = 'http://www.w3.org/2000/svg';

  const WASH = {
    rose:  ['oklch(94% 0.032 25)',  'oklch(88% 0.055 25)'],
    sage:  ['oklch(93% 0.035 155)', 'oklch(86% 0.060 155)'],
    sky:   ['oklch(94% 0.030 230)', 'oklch(87% 0.052 230)'],
    ochre: ['oklch(94% 0.045 72)',  'oklch(86% 0.085 72)'],
    terra: ['oklch(92% 0.045 28)',  'oklch(82% 0.120 28)'],
    dusk:  ['oklch(93% 0.028 290)', 'oklch(85% 0.055 290)'],
    cream: ['oklch(95% 0.020 75)',  'oklch(88% 0.040 60)'],
  };

  const HUE_BY_POSTURE = {
    idle: 'cream', listen: 'sky', speak: 'rose', read: 'sage',
    alert: 'terra', sleep: 'dusk', warn: 'ochre',
  };

  const TILT_BY_POSTURE = {
    idle: 0, listen: -6, speak: 4, read: -3, alert: 0, sleep: 10, warn: 0,
  };

  const ANTENNA_BY_POSTURE = {
    idle:   { l: -4,   r:   4 },
    listen: { l: -22,  r:   6 },
    speak:  { l:  8,   r:  20 },
    read:   { l: -125, r: 125 },
    alert:  { l: -28,  r:  28 },
    sleep:  { l:  22,  r: -22 },
    warn:   { l: -18,  r:  18 },
  };

  const INK = 'oklch(30% 0.025 255)';
  const SHELL = 'oklch(98% 0.008 80)';
  const SHELL_SHADE = 'oklch(92% 0.015 70)';

  function svgEl(tag, attrs = {}) {
    const node = document.createElementNS(SVG_NS, tag);
    for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, String(v));
    return node;
  }

  function renderWash(hue, size) {
    const [c0, c1] = WASH[hue] || WASH.rose;
    const svg = svgEl('svg', {
      viewBox: '0 0 220 260',
      width: size,
      height: size * 260 / 220,
    });
    svg.style.display = 'block';

    const defs = svgEl('defs');
    const grad = svgEl('radialGradient', { id: `w-${hue}`, cx: '45%', cy: '45%', r: '65%' });
    grad.appendChild(svgEl('stop', { offset: '0%', 'stop-color': c0, 'stop-opacity': '0.95' }));
    grad.appendChild(svgEl('stop', { offset: '60%', 'stop-color': c1, 'stop-opacity': '0.70' }));
    grad.appendChild(svgEl('stop', { offset: '100%', 'stop-color': c1, 'stop-opacity': '0' }));
    defs.appendChild(grad);
    svg.appendChild(defs);

    svg.appendChild(svgEl('path', {
      d: 'M 110 14 C 160 18, 196 52, 202 112 C 208 172, 178 220, 128 236 C 76 252, 28 232, 18 176 C 8 118, 30 50, 78 22 C 90 16, 100 14, 110 14 Z',
      fill: `url(#w-${hue})`,
    }));
    return svg;
  }

  function renderFigure(posture, size) {
    const tilt = TILT_BY_POSTURE[posture] ?? 0;
    const headTilt = posture === 'read' ? -8 : tilt;
    const ant = ANTENNA_BY_POSTURE[posture] || ANTENNA_BY_POSTURE.idle;
    const eyeOpen = posture === 'sleep' ? 0.15 : 1;

    const EYE_L_R = 16;
    const EYE_R_R = 12;
    const EYE_L_CX = 95;
    const EYE_R_CX = 126;
    const EYE_CY = 108;

    const W = 220, H = 260;
    const heightPx = size * H / W;

    const svg = svgEl('svg', {
      viewBox: `0 0 ${W} ${H}`,
      width: size,
      height: heightPx,
    });
    svg.style.position = 'relative';

    // defs
    const defs = svgEl('defs');
    const shellG = svgEl('radialGradient', { id: 'shell-g', cx: '38%', cy: '28%', r: '85%' });
    shellG.appendChild(svgEl('stop', { offset: '0%', 'stop-color': '#ffffff' }));
    shellG.appendChild(svgEl('stop', { offset: '55%', 'stop-color': SHELL }));
    shellG.appendChild(svgEl('stop', { offset: '100%', 'stop-color': SHELL_SHADE }));
    defs.appendChild(shellG);

    const eyeG = svgEl('radialGradient', { id: 'eye-g', cx: '32%', cy: '28%', r: '75%' });
    eyeG.appendChild(svgEl('stop', { offset: '0%', 'stop-color': '#4a4a4a' }));
    eyeG.appendChild(svgEl('stop', { offset: '40%', 'stop-color': '#161616' }));
    eyeG.appendChild(svgEl('stop', { offset: '100%', 'stop-color': '#000' }));
    defs.appendChild(eyeG);

    const eyeInner = svgEl('radialGradient', { id: 'eye-inner', cx: '50%', cy: '50%', r: '50%' });
    eyeInner.appendChild(svgEl('stop', { offset: '0%', 'stop-color': 'oklch(45% 0.03 255)', 'stop-opacity': '0.6' }));
    eyeInner.appendChild(svgEl('stop', { offset: '100%', 'stop-color': '#000', 'stop-opacity': '0' }));
    defs.appendChild(eyeInner);

    const baseG = svgEl('radialGradient', { id: 'base-g', cx: '50%', cy: '40%', r: '60%' });
    baseG.appendChild(svgEl('stop', { offset: '0%', 'stop-color': 'oklch(55% 0.015 255)' }));
    baseG.appendChild(svgEl('stop', { offset: '100%', 'stop-color': 'oklch(38% 0.015 255)' }));
    defs.appendChild(baseG);

    svg.appendChild(defs);

    // ombre au sol
    svg.appendChild(svgEl('ellipse', {
      cx: 110, cy: 248, rx: 60, ry: 5,
      fill: 'oklch(75% 0.025 50)', opacity: '0.45',
    }));

    // antennes ancrées aux coins de la tête (66,82) et (154,82)
    const antGroup = svgEl('g', {
      transform: `rotate(${headTilt} 110 110)`,
      stroke: INK, 'stroke-width': '1.3', fill: 'none', 'stroke-linecap': 'round',
    });
    const antL = svgEl('g', { transform: `rotate(${ant.l} 66 82)` });
    antL.appendChild(svgEl('path', { d: 'M 64 84 q -3 -2 0 -4 t 0 -4 t 0 -4', 'stroke-width': '1.1' }));
    antL.appendChild(svgEl('path', { d: 'M 66 82 L 66 14' }));
    antL.appendChild(svgEl('circle', { cx: 66, cy: 12, r: 2.2, fill: INK }));
    antGroup.appendChild(antL);
    const antR = svgEl('g', { transform: `rotate(${ant.r} 154 82)` });
    antR.appendChild(svgEl('path', { d: 'M 156 84 q 3 -2 0 -4 t 0 -4 t 0 -4', 'stroke-width': '1.1' }));
    antR.appendChild(svgEl('path', { d: 'M 154 82 L 154 14' }));
    antR.appendChild(svgEl('circle', { cx: 154, cy: 12, r: 2.2, fill: INK }));
    antGroup.appendChild(antR);
    svg.appendChild(antGroup);

    // tête + yeux (synchronisés avec headTilt pour la posture read)
    const head = svgEl('g', { transform: `rotate(${headTilt} 110 110)` });
    head.appendChild(svgEl('rect', {
      x: 58, y: 76, width: 104, height: 66, rx: 22, ry: 22,
      fill: 'url(#shell-g)', stroke: INK, 'stroke-width': '1.2', 'stroke-opacity': '0.85',
    }));
    head.appendChild(svgEl('ellipse', { cx: 82, cy: 86, rx: 18, ry: 6, fill: '#fff', opacity: '0.55' }));
    head.appendChild(svgEl('ellipse', { cx: 72, cy: 108, rx: 3, ry: 14, fill: '#fff', opacity: '0.2' }));

    // yeux
    const eyes = svgEl('g');
    eyes.appendChild(svgEl('path', {
      d: `M ${EYE_L_CX + EYE_L_R - 1} ${EYE_CY} L ${EYE_R_CX - EYE_R_R + 1} ${EYE_CY}`,
      stroke: INK, 'stroke-width': '2.4', 'stroke-linecap': 'round',
    }));
    eyes.appendChild(svgEl('circle', { cx: EYE_L_CX, cy: EYE_CY, r: EYE_L_R + 1.5, fill: 'none', stroke: INK, 'stroke-width': '1', 'stroke-opacity': '0.55' }));
    eyes.appendChild(svgEl('circle', { cx: EYE_R_CX, cy: EYE_CY, r: EYE_R_R + 1.5, fill: 'none', stroke: INK, 'stroke-width': '1', 'stroke-opacity': '0.55' }));
    eyes.appendChild(svgEl('circle', { cx: EYE_L_CX, cy: EYE_CY, r: EYE_L_R, fill: 'url(#eye-g)' }));
    eyes.appendChild(svgEl('circle', { cx: EYE_R_CX, cy: EYE_CY, r: EYE_R_R, fill: 'url(#eye-g)' }));
    eyes.appendChild(svgEl('circle', { cx: EYE_L_CX, cy: EYE_CY, r: EYE_L_R - 2, fill: 'url(#eye-inner)' }));
    eyes.appendChild(svgEl('circle', { cx: EYE_R_CX, cy: EYE_CY, r: EYE_R_R - 2, fill: 'url(#eye-inner)' }));

    if (eyeOpen > 0.5) {
      eyes.appendChild(svgEl('ellipse', { cx: EYE_L_CX - 5, cy: EYE_CY - 6, rx: 4.5, ry: 3.6, fill: '#fff', opacity: '0.92' }));
      eyes.appendChild(svgEl('ellipse', { cx: EYE_R_CX - 4, cy: EYE_CY - 5, rx: 3.2, ry: 2.6, fill: '#fff', opacity: '0.92' }));
      eyes.appendChild(svgEl('circle', { cx: EYE_L_CX + 5, cy: EYE_CY + 7, r: 1.4, fill: '#fff', opacity: '0.55' }));
      eyes.appendChild(svgEl('circle', { cx: EYE_R_CX + 4, cy: EYE_CY + 5, r: 1, fill: '#fff', opacity: '0.55' }));
    }
    if (posture === 'sleep') {
      eyes.appendChild(svgEl('rect', { x: EYE_L_CX - EYE_L_R - 2, y: EYE_CY - 4, width: EYE_L_R * 2 + 4, height: 8, fill: SHELL }));
      eyes.appendChild(svgEl('rect', { x: EYE_R_CX - EYE_R_R - 2, y: EYE_CY - 4, width: EYE_R_R * 2 + 4, height: 8, fill: SHELL }));
      eyes.appendChild(svgEl('path', { d: `M ${EYE_L_CX - EYE_L_R} ${EYE_CY + 2} Q ${EYE_L_CX} ${EYE_CY + 6} ${EYE_L_CX + EYE_L_R} ${EYE_CY + 2}`, stroke: INK, 'stroke-width': '1.4', fill: 'none', 'stroke-linecap': 'round' }));
      eyes.appendChild(svgEl('path', { d: `M ${EYE_R_CX - EYE_R_R} ${EYE_CY + 2} Q ${EYE_R_CX} ${EYE_CY + 6} ${EYE_R_CX + EYE_R_R} ${EYE_CY + 2}`, stroke: INK, 'stroke-width': '1.4', fill: 'none', 'stroke-linecap': 'round' }));
    }
    head.appendChild(eyes);
    head.appendChild(svgEl('circle', { cx: 110, cy: 142, r: 1.4, fill: INK, opacity: '0.4' }));
    svg.appendChild(head);

    // cou
    svg.appendChild(svgEl('path', {
      d: 'M 100 152 L 100 160 M 120 152 L 120 160',
      stroke: INK, 'stroke-width': '1.2', 'stroke-linecap': 'round', opacity: '0.7',
    }));

    // corps
    svg.appendChild(svgEl('path', {
      d: 'M 76 160 C 66 168, 62 192, 66 212 C 70 230, 86 238, 110 238 C 134 238, 150 230, 154 212 C 158 192, 154 168, 144 160 C 136 156, 124 154, 110 154 C 96 154, 84 156, 76 160 Z',
      fill: 'url(#shell-g)', stroke: INK, 'stroke-width': '1.2', 'stroke-opacity': '0.85',
    }));
    svg.appendChild(svgEl('ellipse', { cx: 86, cy: 185, rx: 9, ry: 22, fill: '#fff', opacity: '0.4' }));
    svg.appendChild(svgEl('path', { d: 'M 70 222 Q 110 228 150 222', stroke: INK, 'stroke-width': '0.8', fill: 'none', opacity: '0.35' }));
    svg.appendChild(svgEl('path', {
      d: 'M 78 236 C 88 244, 132 244, 142 236 L 142 244 C 132 250, 88 250, 78 244 Z',
      fill: 'url(#base-g)', stroke: INK, 'stroke-width': '0.8', 'stroke-opacity': '0.6',
    }));
    const slits = svgEl('g', { stroke: INK, 'stroke-width': '0.9', 'stroke-linecap': 'round', opacity: '0.5' });
    slits.appendChild(svgEl('line', { x1: 100, y1: 234, x2: 105, y2: 234 }));
    slits.appendChild(svgEl('line', { x1: 115, y1: 234, x2: 120, y2: 234 }));
    svg.appendChild(slits);

    // feuillage grimpant
    const ivy = svgEl('g', { stroke: 'oklch(52% 0.08 135)', 'stroke-width': '1.2', fill: 'none', 'stroke-linecap': 'round' });
    ivy.appendChild(svgEl('path', { d: 'M 62 170 Q 80 184 96 174 Q 108 168 124 178 Q 140 188 156 178' }));
    const leaves = svgEl('g', { fill: 'oklch(62% 0.08 135)', stroke: 'none' });
    leaves.appendChild(svgEl('ellipse', { cx: 78, cy: 176, rx: 3.5, ry: 1.8, transform: 'rotate(-20 78 176)' }));
    leaves.appendChild(svgEl('ellipse', { cx: 104, cy: 168, rx: 3.5, ry: 1.8, transform: 'rotate(15 104 168)' }));
    leaves.appendChild(svgEl('ellipse', { cx: 132, cy: 176, rx: 3.5, ry: 1.8, transform: 'rotate(-10 132 176)' }));
    leaves.appendChild(svgEl('ellipse', { cx: 154, cy: 174, rx: 3.5, ry: 1.8, transform: 'rotate(20 154 174)' }));
    ivy.appendChild(leaves);
    svg.appendChild(ivy);

    // oiseau rouge-gorge (idle, sleep, read)
    if (posture === 'idle' || posture === 'sleep' || posture === 'read') {
      const bird = svgEl('g', { transform: 'translate(146, 58)' });
      bird.appendChild(svgEl('ellipse', { cx: 0, cy: 0, rx: 10, ry: 7, fill: 'oklch(62% 0.04 70)' }));
      bird.appendChild(svgEl('ellipse', { cx: -4, cy: 2, rx: 5, ry: 4, fill: 'oklch(65% 0.13 40)' }));
      bird.appendChild(svgEl('circle', { cx: -9, cy: -3, r: 4.5, fill: 'oklch(62% 0.04 70)' }));
      bird.appendChild(svgEl('circle', { cx: -9, cy: -3, r: 2.5, fill: 'oklch(65% 0.13 40)' }));
      bird.appendChild(svgEl('circle', { cx: -10, cy: -4, r: 0.9, fill: '#111' }));
      bird.appendChild(svgEl('path', { d: 'M -12 -3 L -15 -2.5 L -12 -2 Z', fill: INK }));
      bird.appendChild(svgEl('path', { d: 'M 8 -1 L 14 -3 L 10 1 Z', fill: 'oklch(52% 0.04 70)' }));
      bird.appendChild(svgEl('line', { x1: -4, y1: 6, x2: -4, y2: 10, stroke: INK, 'stroke-width': '0.8' }));
      bird.appendChild(svgEl('line', { x1: 0, y1: 6, x2: 0, y2: 10, stroke: INK, 'stroke-width': '0.8' }));
      svg.appendChild(bird);
    }

    // posture-specific overlays
    if (posture === 'speak') {
      const g = svgEl('g', { fill: 'none', stroke: 'oklch(62% 0.13 40)', 'stroke-width': '1.6', 'stroke-linecap': 'round', opacity: '0.7' });
      g.appendChild(svgEl('path', { d: 'M 168 110 Q 178 120 168 130' }));
      g.appendChild(svgEl('path', { d: 'M 180 100 Q 196 120 180 140' }));
      svg.appendChild(g);
    } else if (posture === 'listen') {
      const g = svgEl('g', { fill: 'none', stroke: 'oklch(50% 0.08 230)', 'stroke-width': '1.4', 'stroke-linecap': 'round', opacity: '0.7' });
      g.appendChild(svgEl('path', { d: 'M 42 104 Q 36 120 42 136' }));
      g.appendChild(svgEl('path', { d: 'M 30 94 Q 20 120 30 146' }));
      svg.appendChild(g);
    } else if (posture === 'read') {
      const g = svgEl('g');
      g.appendChild(svgEl('path', {
        d: 'M 78 202 L 110 198 L 142 202 L 142 220 L 110 216 L 78 220 Z',
        fill: SHELL, stroke: INK, 'stroke-width': '1.1',
      }));
      g.appendChild(svgEl('line', { x1: 110, y1: 198, x2: 110, y2: 216, stroke: INK, 'stroke-width': '0.9' }));
      const lines = svgEl('g', { stroke: INK, 'stroke-width': '0.5', opacity: '0.5' });
      lines.appendChild(svgEl('line', { x1: 84, y1: 206, x2: 104, y2: 204 }));
      lines.appendChild(svgEl('line', { x1: 84, y1: 210, x2: 104, y2: 208 }));
      lines.appendChild(svgEl('line', { x1: 116, y1: 204, x2: 136, y2: 206 }));
      lines.appendChild(svgEl('line', { x1: 116, y1: 208, x2: 136, y2: 210 }));
      g.appendChild(lines);
      svg.appendChild(g);
    } else if (posture === 'alert') {
      const g = svgEl('g');
      g.appendChild(svgEl('circle', { cx: 180, cy: 50, r: 16, fill: 'oklch(92% 0.045 28)', stroke: 'oklch(56% 0.155 28)', 'stroke-width': '1.2' }));
      g.appendChild(svgEl('line', { x1: 180, y1: 43, x2: 180, y2: 52, stroke: 'oklch(56% 0.155 28)', 'stroke-width': '2.4', 'stroke-linecap': 'round' }));
      g.appendChild(svgEl('circle', { cx: 180, cy: 57, r: 1.6, fill: 'oklch(56% 0.155 28)' }));
      svg.appendChild(g);
    } else if (posture === 'sleep') {
      const g = svgEl('g', { fill: INK });
      const t1 = svgEl('text', { x: 170, y: 58, 'font-family': 'Fraunces, serif', 'font-size': '18', 'font-style': 'italic', opacity: '0.8' });
      t1.textContent = 'z';
      const t2 = svgEl('text', { x: 186, y: 46, 'font-family': 'Fraunces, serif', 'font-size': '13', 'font-style': 'italic', opacity: '0.55' });
      t2.textContent = 'z';
      const t3 = svgEl('text', { x: 196, y: 36, 'font-family': 'Fraunces, serif', 'font-size': '9', 'font-style': 'italic', opacity: '0.35' });
      t3.textContent = 'z';
      g.appendChild(t1); g.appendChild(t2); g.appendChild(t3);
      svg.appendChild(g);
    }

    return svg;
  }

  window.Reachy = {
    render(posture = 'idle', size = 180, opts = {}) {
      const { showWash = true } = opts;
      const hue = HUE_BY_POSTURE[posture] || 'cream';
      const heightPx = size * 260 / 220;

      const wrap = document.createElement('div');
      wrap.style.position = 'relative';
      wrap.style.width = `${size}px`;
      wrap.style.height = `${heightPx}px`;

      if (showWash) {
        const washWrap = document.createElement('div');
        washWrap.style.position = 'absolute';
        washWrap.style.inset = '0';
        washWrap.appendChild(renderWash(hue, size));
        wrap.appendChild(washWrap);
      }
      wrap.appendChild(renderFigure(posture, size));
      return wrap;
    },
    postures() {
      return Object.keys(HUE_BY_POSTURE);
    },
  };
})();
