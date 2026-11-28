/**
 * Unified 16x16 inline SVG icon library for GridForge Studio.
 * All icons share a 1.75px stroke weight, round caps/joins, and currentColor
 * so they inherit dark/daylight theme ink cleanly without OS emoji drift.
 */

const SVG_BASE =
  'viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"';

function icon(body: string, cls = "gf-icon", size = 15): string {
  return `<svg class="${cls}" width="${size}" height="${size}" ${SVG_BASE}>${body}</svg>`;
}

export const ICON_SIDEBAR = icon(
  '<rect x="2" y="2.5" width="12" height="11" rx="1.6" /><line x1="6" y1="2.5" x2="6" y2="13.5" />',
  "gf-btn-icon",
  16
);

export const ICON_SHARE = icon(
  '<path d="M6.75 9.25l2.5-2.5" /><path d="M8.5 5.25l1.2-1.2a2.2 2.2 0 1 1 3.1 3.1l-1.2 1.2" /><path d="M7.5 10.75l-1.2 1.2a2.2 2.2 0 1 1-3.1-3.1l1.2-1.2" />',
  "gf-btn-icon",
  15
);

export const ICON_PROJECTS = icon(
  '<path d="M2 4.4a1.3 1.3 0 0 1 1.3-1.3h2.9l1.3 1.4h5.2A1.3 1.3 0 0 1 14 5.8v5.9a1.3 1.3 0 0 1-1.3 1.3H3.3A1.3 1.3 0 0 1 2 11.7V4.4z" />',
  "gf-btn-icon",
  15
);

export const ICON_ADMIN = icon(
  '<path d="M8 2.2l4.8 1.9v3.6c0 3.1-2 5.3-4.8 6.2-2.8-.9-4.8-3.1-4.8-6.2V4.1L8 2.2z" />',
  "gf-btn-icon",
  15
);

export const ICON_COPY = icon(
  '<rect x="5.2" y="5.2" width="7.8" height="8.3" rx="1.3" /><path d="M10.5 5.2V3.8A1.3 1.3 0 0 0 9.2 2.5H4.1A1.3 1.3 0 0 0 2.8 3.8v6.1a1.3 1.3 0 0 0 1.3 1.3h1.1" />',
  "gf-copy-icon",
  14
);

export const ICON_CHEVRON_DOWN = icon(
  '<path d="M4.25 6.25L8 10l3.75-3.75" />',
  "gf-chevron-icon",
  13
);

export const ICON_CHEVRON_UP = icon(
  '<path d="M4.25 9.75L8 6l3.75 3.75" />',
  "gf-chevron-icon",
  13
);

export const ICON_SUN = icon(
  '<circle cx="8" cy="8" r="3" /><line x1="8" y1="1.75" x2="8" y2="3.2" /><line x1="8" y1="12.8" x2="8" y2="14.25" /><line x1="1.75" y1="8" x2="3.2" y2="8" /><line x1="12.8" y1="8" x2="14.25" y2="8" /><line x1="3.6" y1="3.6" x2="4.6" y2="4.6" /><line x1="11.4" y1="11.4" x2="12.4" y2="12.4" /><line x1="12.4" y1="3.6" x2="11.4" y2="4.6" /><line x1="4.6" y1="11.4" x2="3.6" y2="12.4" />',
  "gf-btn-icon",
  15
);

export const ICON_MOON = icon(
  '<path d="M13.2 9.8A5.5 5.5 0 0 1 6.2 2.8a5.6 5.6 0 1 0 7 7z" />',
  "gf-btn-icon",
  15
);

export const ICON_FIT = icon(
  '<path d="M2.5 5.5V3a.5.5 0 0 1 .5-.5h2.5" /><path d="M13.5 5.5V3a.5.5 0 0 0-.5-.5h-2.5" /><path d="M2.5 10.5V13a.5.5 0 0 0 .5.5h2.5" /><path d="M13.5 10.5V13a.5.5 0 0 1-.5.5h-2.5" /><circle cx="8" cy="8" r="1.5" />',
  "gf-tool-icon",
  14
);

export const ICON_PLUS = icon(
  '<line x1="8" y1="3.5" x2="8" y2="12.5" /><line x1="3.5" y1="8" x2="12.5" y2="8" />',
  "gf-tool-icon",
  14
);

export const ICON_MINUS = icon(
  '<line x1="3.5" y1="8" x2="12.5" y2="8" />',
  "gf-tool-icon",
  14
);

export const ICON_WAYPOINT = icon(
  '<path d="M8 14s4.5-4.1 4.5-7.6a4.5 4.5 0 1 0-9 0C3.5 9.9 8 14 8 14z" /><circle cx="8" cy="6.4" r="1.5" />',
  "gf-tool-icon",
  14
);

export const ICON_RULER = icon(
  '<path d="M3.2 10.2l7-7 2.6 2.6-7 7-2.6-2.6z" /><line x1="5.7" y1="7.7" x2="6.9" y2="8.9" /><line x1="7.7" y1="5.7" x2="8.9" y2="6.9" />',
  "gf-tool-icon",
  14
);

export const ICON_RING = icon(
  '<circle cx="8" cy="8" r="5.6" /><circle cx="8" cy="8" r="2.6" /><circle cx="8" cy="8" r="0.6" fill="currentColor" />',
  "gf-tool-icon",
  14
);

export const ICON_SECTOR = icon(
  '<polygon points="8,2.2 13.6,6.3 11.5,13 4.5,13 2.4,6.3" />',
  "gf-tool-icon",
  14
);

export const ICON_LOS = icon(
  '<circle cx="8" cy="8.5" r="1.3" fill="currentColor" /><path d="M5.1 11.4a4.1 4.1 0 0 1 0-5.8" /><path d="M10.9 5.6a4.1 4.1 0 0 1 0 5.8" /><path d="M3 13.5a7 7 0 0 1 0-10" /><path d="M13 3.5a7 7 0 0 1 0 10" />',
  "gf-tool-icon",
  14
);

export const ICON_RF_LINK = icon(
  '<circle cx="3.2" cy="11.5" r="1.6" /><circle cx="12.8" cy="4.5" r="1.6" /><path d="M4.6 10.5l6.8-5" stroke-dasharray="2.2 1.8" /><path d="M5.5 7.4c1.6-1.5 3.6-1.8 5.2-1" />',
  "gf-tool-icon",
  14
);

export const ICON_REFRESH = icon(
  '<path d="M13.2 8a5.2 5.2 0 1 1-1.3-3.4" /><polyline points="13.5 2.4 13.5 5.4 10.5 5.4" />',
  "gf-action-icon",
  14
);

export const ICON_CLOSE = icon(
  '<line x1="4" y1="4" x2="12" y2="12" /><line x1="12" y1="4" x2="4" y2="12" />',
  "gf-action-icon",
  14
);

export const ICON_DOWNLOAD = icon(
  '<path d="M8 2.8v7.4" /><polyline points="4.8 7.4 8 10.6 11.2 7.4" /><path d="M3 13.2h10" />',
  "gf-action-icon",
  14
);

export const ICON_EXTERNAL = icon(
  '<path d="M6.5 3.2h6.3v6.3" /><line x1="12.5" y1="3.5" x2="6.2" y2="9.8" /><path d="M11.5 10.2v2.1a1 1 0 0 1-1 1H3.7a1 1 0 0 1-1-1V5.5a1 1 0 0 1 1-1h2.1" />',
  "gf-action-icon",
  14
);

export const ICON_TERMINAL = icon(
  '<polyline points="3.2 5 6.4 8 3.2 11" /><line x1="8" y1="11.2" x2="12.8" y2="11.2" />',
  "gf-action-icon",
  14
);

export const ICON_SLIDERS = icon(
  '<line x1="2.5" y1="4.5" x2="13.5" y2="4.5" /><circle cx="5.5" cy="4.5" r="1.5" fill="var(--bg-panel)" /><line x1="2.5" y1="8" x2="13.5" y2="8" /><circle cx="10.5" cy="8" r="1.5" fill="var(--bg-panel)" /><line x1="2.5" y1="11.5" x2="13.5" y2="11.5" /><circle cx="6.5" cy="11.5" r="1.5" fill="var(--bg-panel)" />',
  "gf-action-icon",
  14
);

export const ICON_LAYERS = icon(
  '<polygon points="8,2.4 13.8,5.6 8,8.8 2.2,5.6" /><polyline points="2.2 8.4 8 11.6 13.8 8.4" /><polyline points="2.2 11.2 8 14.4 13.8 11.2" />',
  "gf-tool-icon",
  14
);

export const ICON_GITHUB = `<svg class="gf-action-icon" width="14" height="14" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27s1.36.09 2 .27c1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8z"/></svg>`;

