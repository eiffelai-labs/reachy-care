/* icons.js — hand-drawn SVG icons, vanilla JS port of Icons.jsx.
 * Usage:
 *   element.appendChild(Icons.svg('power', 24));
 *   element.replaceChildren(Icons.svg('wifi', 20, 'var(--terra-brand)'));
 * Size in px. Color defaults to currentColor for CSS inheritance.
 * Returns an SVGElement (not a string) so no sanitization needed at call sites.
 */
(function () {
  const SVG_NS = 'http://www.w3.org/2000/svg';

  function el(tag, attrs) {
    const node = document.createElementNS(SVG_NS, tag);
    for (const [k, v] of Object.entries(attrs)) {
      node.setAttribute(k, String(v));
    }
    return node;
  }

  function stroked(tag, attrs, color) {
    return el(tag, {
      ...attrs,
      fill: 'none',
      stroke: color,
      'stroke-width': 1.6,
      'stroke-linecap': 'round',
      'stroke-linejoin': 'round',
    });
  }

  function filled(tag, attrs, color) {
    return el(tag, { ...attrs, fill: color });
  }

  const builders = {
    home: (c) => [stroked('path', { d: 'M3 11l9-7 9 7v9a2 2 0 0 1-2 2h-4v-7h-6v7H5a2 2 0 0 1-2-2z' }, c)],
    book: (c) => [stroked('path', { d: 'M4 4h7a3 3 0 0 1 3 3v13M20 4h-7a3 3 0 0 0-3 3v13M4 4v16h16V4' }, c)],
    bell: (c) => [stroked('path', { d: 'M6 17V11a6 6 0 1 1 12 0v6l2 2H4zM10 21a2 2 0 0 0 4 0' }, c)],
    gear: (c) => [
      stroked('circle', { cx: 12, cy: 12, r: 3 }, c),
      stroked('path', { d: 'M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3h0a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8v0a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z' }, c),
    ],
    chat: (c) => [stroked('path', { d: 'M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8z' }, c)],
    wifi: (c) => [
      stroked('path', { d: 'M5 12.5a10 10 0 0 1 14 0M8.5 16a5 5 0 0 1 7 0' }, c),
      filled('circle', { cx: 12, cy: 19.5, r: 1 }, c),
    ],
    wifiOff: (c) => [
      stroked('path', { d: 'M5 12.5a10 10 0 0 1 14 0M8.5 16a5 5 0 0 1 7 0' }, c),
      filled('circle', { cx: 12, cy: 19.5, r: 1 }, c),
      stroked('path', { d: 'M3 3l18 18' }, c),
    ],
    pill: (c) => [
      el('rect', { x: 3, y: 9, width: 18, height: 6, rx: 3, transform: 'rotate(-30 12 12)', fill: 'none', stroke: c, 'stroke-width': 1.6, 'stroke-linecap': 'round', 'stroke-linejoin': 'round' }),
      stroked('path', { d: 'M8.5 8.5l7 7' }, c),
    ],
    bookOpen: (c) => [stroked('path', { d: 'M3 5a17 17 0 0 1 9 2v13a17 17 0 0 0-9-2zM21 5a17 17 0 0 0-9 2v13a17 17 0 0 1 9-2z' }, c)],
    pen: (c) => [stroked('path', { d: 'M14 4l6 6-11 11H3v-6zM13 5l6 6' }, c)],
    quote: (c) => [stroked('path', { d: 'M6 7h4v4H6c0 3 2 4 4 4M14 7h4v4h-4c0 3 2 4 4 4' }, c)],
    heart: (c) => [stroked('path', { d: 'M12 20s-7-4.5-7-10a4 4 0 0 1 7-2.6A4 4 0 0 1 19 10c0 5.5-7 10-7 10z' }, c)],
    pulse: (c) => [stroked('path', { d: 'M3 12h4l2-6 4 12 2-6h6' }, c)],
    meal: (c) => [stroked('path', { d: 'M6 3v8a2 2 0 0 0 2 2v8M8 3v8M18 3c-2 1-3 3-3 5s1 3 3 3v9' }, c)],
    sleepZ: (c) => [stroked('path', { d: 'M8 6h7l-7 10h7M17 4h3l-3 4h3' }, c)],
    mood: (c) => [
      stroked('circle', { cx: 12, cy: 12, r: 9 }, c),
      stroked('path', { d: 'M8 14c1.5 1.5 6.5 1.5 8 0' }, c),
      filled('circle', { cx: 9, cy: 10, r: 1.1 }, c),
      filled('circle', { cx: 15, cy: 10, r: 1.1 }, c),
    ],
    walk: (c) => [
      stroked('circle', { cx: 13, cy: 4.5, r: 1.6 }, c),
      stroked('path', { d: 'M9 21l3-7-3-3 4-4 3 3 3 1M7 14l2-4' }, c),
    ],
    battery: (c) => [
      stroked('rect', { x: 3, y: 8, width: 16, height: 10, rx: 2 }, c),
      stroked('line', { x1: 21, y1: 11, x2: 21, y2: 15 }, c),
    ],
    mic: (c) => [
      stroked('rect', { x: 9, y: 3, width: 6, height: 12, rx: 3 }, c),
      stroked('path', { d: 'M5 11a7 7 0 0 0 14 0M12 18v3' }, c),
    ],
    micOff: (c) => [stroked('path', { d: 'M3 3l18 18M9 9v2a3 3 0 0 0 4.5 2.6M15 11V6a3 3 0 0 0-6-.5M5 11a7 7 0 0 0 10.3 6.2M19 11a7 7 0 0 1-1 3.5' }, c)],
    play: (c) => [filled('polygon', { points: '6,4 20,12 6,20', stroke: c, 'stroke-linejoin': 'round' }, c)],
    pause: (c) => [
      filled('rect', { x: 6, y: 4, width: 4, height: 16 }, c),
      filled('rect', { x: 14, y: 4, width: 4, height: 16 }, c),
    ],
    moon: (c) => [stroked('path', { d: 'M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z' }, c)],
    sun: (c) => [
      stroked('circle', { cx: 12, cy: 12, r: 4 }, c),
      stroked('path', { d: 'M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4' }, c),
    ],
    chevron: (c) => [stroked('path', { d: 'M9 6l6 6-6 6' }, c)],
    chevronDown: (c) => [stroked('path', { d: 'M6 9l6 6 6-6' }, c)],
    back: (c) => [stroked('path', { d: 'M15 18l-6-6 6-6' }, c)],
    plus: (c) => [stroked('path', { d: 'M12 5v14M5 12h14' }, c)],
    search: (c) => [
      stroked('circle', { cx: 11, cy: 11, r: 7 }, c),
      stroked('path', { d: 'M20 20l-4-4' }, c),
    ],
    face: (c) => [
      stroked('circle', { cx: 12, cy: 12, r: 9 }, c),
      filled('circle', { cx: 9, cy: 10, r: 1 }, c),
      filled('circle', { cx: 15, cy: 10, r: 1 }, c),
      stroked('path', { d: 'M8 15c1.5 1.5 6.5 1.5 8 0' }, c),
    ],
    alert: (c) => [
      stroked('path', { d: 'M12 3l10 17H2z' }, c),
      stroked('line', { x1: 12, y1: 10, x2: 12, y2: 14 }, c),
      filled('circle', { cx: 12, cy: 17, r: 1 }, c),
    ],
    calendar: (c) => [
      stroked('rect', { x: 3, y: 5, width: 18, height: 16, rx: 2 }, c),
      stroked('path', { d: 'M3 10h18M8 3v4M16 3v4' }, c),
    ],
    download: (c) => [stroked('path', { d: 'M12 4v11M7 11l5 5 5-5M5 20h14' }, c)],
    share: (c) => [
      stroked('circle', { cx: 6, cy: 12, r: 2.5 }, c),
      stroked('circle', { cx: 18, cy: 6, r: 2.5 }, c),
      stroked('circle', { cx: 18, cy: 18, r: 2.5 }, c),
      stroked('path', { d: 'M8 11l8-4M8 13l8 4' }, c),
    ],
    robot: (c) => [
      stroked('rect', { x: 5, y: 8, width: 14, height: 11, rx: 3 }, c),
      stroked('line', { x1: 12, y1: 4, x2: 12, y2: 8 }, c),
      filled('circle', { cx: 12, cy: 3.5, r: 1 }, c),
      filled('circle', { cx: 9, cy: 13, r: 1 }, c),
      filled('circle', { cx: 15, cy: 13, r: 1 }, c),
      stroked('line', { x1: 9, y1: 17, x2: 15, y2: 17 }, c),
    ],
    signal: (c) => [stroked('path', { d: 'M3 20h3v-4H3zM9 20h3v-8H9zM15 20h3V9h-3zM21 20V5h-3v15' }, c)],

    /* Dashboard-specific power controls — not in original Icons.jsx */
    powerOn: (c) => [
      stroked('path', { d: 'M12 2v6' }, c),
      stroked('path', { d: 'M18.4 6.6a9 9 0 1 1-12.8 0' }, c),
    ],
    powerOff: (c) => [
      stroked('rect', { x: 3, y: 3, width: 18, height: 18, rx: 2 }, c),
      filled('rect', { x: 8, y: 8, width: 8, height: 8, rx: 1 }, c),
    ],
    wake: (c) => [
      stroked('circle', { cx: 12, cy: 12, r: 5 }, c),
      stroked('line', { x1: 12, y1: 1, x2: 12, y2: 3 }, c),
      stroked('line', { x1: 12, y1: 21, x2: 12, y2: 23 }, c),
      stroked('line', { x1: 4.22, y1: 4.22, x2: 5.64, y2: 5.64 }, c),
      stroked('line', { x1: 18.36, y1: 18.36, x2: 19.78, y2: 19.78 }, c),
      stroked('line', { x1: 1, y1: 12, x2: 3, y2: 12 }, c),
      stroked('line', { x1: 21, y1: 12, x2: 23, y2: 12 }, c),
      stroked('line', { x1: 4.22, y1: 19.78, x2: 5.64, y2: 18.36 }, c),
      stroked('line', { x1: 18.36, y1: 5.64, x2: 19.78, y2: 4.22 }, c),
    ],
  };

  window.Icons = {
    svg(name, size = 20, color = 'currentColor') {
      const build = builders[name];
      if (!build) return null;
      const svg = el('svg', {
        viewBox: '0 0 24 24',
        width: size,
        height: size,
      });
      svg.style.display = 'inline-block';
      svg.style.verticalAlign = 'middle';
      svg.style.flexShrink = '0';
      for (const child of build(color)) svg.appendChild(child);
      return svg;
    },
    has(name) {
      return Object.prototype.hasOwnProperty.call(builders, name);
    },
    names() {
      return Object.keys(builders);
    },
  };
})();
