import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import test from "node:test";

import {
  BASEMAP_OPTIONS,
  DPI_OPTIONS,
  SCALE_OPTIONS,
  buildCliCommand,
  createInitialFormState,
} from "../src/presets.ts";

// Capture the tokens a POSIX shell actually hands to Docker.
function dockerArgs(state) {
  const { command, error } = buildCliCommand(state);
  assert.ifError(error);
  assert.equal(typeof command, "string");
  const shell = spawnSync("sh", ["-c", "docker() { printf '%s\\0' \"$@\"; }\n" + command], {
    encoding: "utf8",
  });
  assert.equal(shell.status, 0, shell.stderr);
  const args = shell.stdout.split("\0").slice(0, -1);
  assert.deepEqual(args.slice(0, 5), ["compose", "run", "--rm", "cli", "gridforge"]);
  return args.slice(5);
}

function option(args, flag) {
  assert.ok(args.includes(flag), flag);
  return args[args.indexOf(flag) + 1];
}

test("PDF and both archives use the Compose output mount and preserve input arguments", () => {
  const state = createInitialFormState();
  state.title = "Walker's field notes!";
  state.overlayFile = { name: "Creek's walking trail.geojson" };
  state.pack_id = "local-fixture";
  Object.assign(state.outputs, { mbtiles: true, kmz: true, geopdf: true, include_grid_on_digital: false });
  const args = dockerArgs(state);
  assert.equal(args[0], "render");
  assert.equal(option(args, "--overlay"), "/out/inputs/Creek's walking trail.geojson");
  assert.equal(option(args, "--title"), state.title);
  assert.equal(option(args, "--pack-id"), state.pack_id);
  assert.ok(args.includes("--geopdf") && args.includes("--no-export-grid") && !args.includes("--no-grid"));
  for (const [flag, ext] of [["--output", "pdf"], ["--mbtiles", "mbtiles"], ["--kmz", "kmz"]]) {
    assert.equal(option(args, flag), "/out/walker_s_field_notes_a4_color." + ext);
  }
});

test("device-only requests use the center and the host output directory", () => {
  const state = createInitialFormState();
  state.grid_cols = 3;
  state.grid_rows = 3;
  state.outputs.pdf = false;
  state.carto.show_grid = false;
  for (const [mbtiles, kmz, format, suffix] of [
    [true, false, "mbtiles", ".mbtiles"], [false, true, "kmz", ".kmz"], [true, true, "both", ""],
  ]) {
    Object.assign(state.outputs, { mbtiles, kmz });
    const args = dockerArgs(state);
    assert.equal(args[0], "export");
    assert.equal(option(args, "--format"), format);
    assert.equal(option(args, "--output"), "/out/sheet_25k_a4_color" + suffix);
    assert.ok(args.includes("--no-grid"));
  }
});

test("a rectangular PDF or an empty output selection explains why nothing can be copied", () => {
  const state = createInitialFormState();
  state.grid_rows = 2;
  assert.match(buildCliCommand(state).error, /atlas/i);
  assert.equal(buildCliCommand(state).command, undefined);
  state.outputs.pdf = false;
  assert.match(buildCliCommand(state).error, /output/i);
});

test("strip PDFs use the mounted input and cannot silently drop a requested archive", () => {
  const state = createInitialFormState();
  state.stripPlan = { sheets: [{}, {}] };
  state.overlayFile = { name: "creek walk.gpx" };
  const args = dockerArgs(state);
  assert.equal(option(args, "--strip-route"), "/out/inputs/creek walk.gpx");
  assert.ok(!args.includes("--overlay"));
  state.outputs.kmz = true;
  assert.match(buildCliCommand(state).error, /strip/i);
  assert.equal(buildCliCommand(state).command, undefined);
});

test("annotation text is preserved or the copy action explains the limitation", () => {
  const state = createInitialFormState();
  state.waypoints = [{ label: "Creek crossing", lat: -33.731234, lon: 150.312345, category: "waypoint" }];
  assert.equal(option(dockerArgs(state), "--waypoint"), "Creek crossing,-33.731234,150.312345,waypoint");
  state.waypoints[0].notes = "Bridge, wooden deck";
  assert.ok(option(dockerArgs(state), "--waypoint").endsWith(",Bridge, wooden deck"));
  state.waypoints[0].label = "Fork, left";
  assert.match(buildCliCommand(state).error, /label/i);
  assert.equal(buildCliCommand(state).command, undefined);
});

test("v1.4.0 themes, Canada overlays, RF links, tactical graphics, and new digital exports emit exact flags", () => {
  const state = createInitialFormState();
  state.color_mode = "night-red";
  state.dem_source = "ca-hrdem";
  state.ca_overlays = ["ca-canvec-trails"];
  state.us_overlays = ["us-trails", "us-public-lands"];
  state.include_rf_link_card = true;
  state.rfLinks = [
    {
      id: "rf-1",
      loading: false,
      error: null,
      profile: null,
      spec: {
        from_lat: -36.456,
        from_lon: 148.263,
        to_lat: -36.421,
        to_lon: 148.301,
        from_height_m: 3,
        to_height_m: 2,
        freq_mhz: 915,
        k_factor: 1.333,
        from_label: "A1",
        to_label: "B1",
      },
    },
  ];
  state.tacticalGraphics = [
    {
      kind: "range_ring",
      label: "BASE",
      points: [[-36.456, 148.263]],
      radii_km: [1, 3, 5],
    },
  ];
  Object.assign(state.outputs, {
    gpkg_tiles: true,
    osmand_sqlitedb: true,
    field_bundle_zip: true,
  });

  const args = dockerArgs(state);
  assert.equal(args[0], "render");
  assert.equal(option(args, "--mode"), "night-red");
  assert.equal(option(args, "--dem-source"), "ca-hrdem");
  assert.equal(option(args, "--ca-overlay"), "ca-canvec-trails");
  assert.ok(args.includes("--rf-link-card"));
  assert.equal(option(args, "--rf-link"), "-36.45600,148.26300,-36.42100,148.30100,915,3,2");
  assert.equal(option(args, "--range-ring"), "-36.45600,148.26300,1:3:5,BASE");
  assert.equal(option(args, "--gpkg"), "/out/sheet_25k_a4_night-red.gpkg");
  assert.equal(option(args, "--osmand"), "/out/sheet_25k_a4_night-red.sqlitedb");
  assert.equal(option(args, "--field-bundle"), "/out/sheet_25k_a4_night-red_field_bundle.zip");
});

test("fine tactical scales (1:10k, 1:12.5k), high DPI options, and Vector HD basemap work cleanly", () => {
  assert.ok(SCALE_OPTIONS.some((s) => s.value === 10000));
  assert.ok(SCALE_OPTIONS.some((s) => s.value === 12500));
  assert.deepEqual(
    DPI_OPTIONS.map((d) => d.value),
    [300, 450, 600]
  );
  assert.ok(BASEMAP_OPTIONS.some((b) => b.id === "vector"));

  const state = createInitialFormState();
  state.scale = 12500;
  state.dpi = 600;
  state.outputs.mbtiles = true;
  state.outputs.export_dpi = 450;

  const args = dockerArgs(state);
  assert.equal(args[0], "render");
  assert.equal(option(args, "--scale"), "12500");
  assert.equal(option(args, "--dpi"), "600");
  assert.equal(option(args, "--export-dpi"), "450");
  assert.equal(option(args, "--output"), "/out/sheet_12_5k_a4_color.pdf");

  // Digital-only export uses --dpi from outputs.export_dpi
  state.outputs.pdf = false;
  state.scale = 10000;
  const expArgs = dockerArgs(state);
  assert.equal(expArgs[0], "export");
  assert.equal(option(expArgs, "--scale"), "10000");
  assert.equal(option(expArgs, "--dpi"), "450");
  assert.equal(option(expArgs, "--output"), "/out/sheet_10k_a4_color.mbtiles");
});
