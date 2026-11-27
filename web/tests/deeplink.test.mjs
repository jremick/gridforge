import test from "node:test";
import assert from "node:assert/strict";

import { createInitialFormState } from "../src/presets.ts";
import {
  MAX_DIRECT_URL_LENGTH,
  buildDirectShareUrl,
  extractShareIdFromSearch,
  hydrateStateFromSearch,
  hydrateStateFromSnapshot,
  serializeStateToParams,
} from "../src/deeplink.ts";

test("URL deep link round-trips full cartography, outputs, view, booklet toggles, and markups", () => {
  const original = createInitialFormState();
  original.center = "-36.45590, 148.26360";
  original.lat = -36.4559;
  original.lon = 148.2636;
  original.title = "Kosciuszko Winter Ops";
  original.scale = 50000;
  original.paper = "a3";
  original.orientation = "landscape";
  original.color_mode = "alpine";
  original.units = "imperial";
  original.dpi = 600;
  original.grid_cols = 2;
  original.grid_rows = 2;
  original.dem_source = "au-elvis";
  original.pack_id = "au_nsw_alpine";
  original.au_overlays = ["au-state-trails", "au-hydro", "au-parks"];
  original.nz_overlays = ["nz-doc-trails"];
  original.ca_overlays = ["ca-canvec-trails"];
  original.us_overlays = ["us-trails", "fema"];

  original.carto.contour_density = "high";
  original.carto.hillshade_strength = "strong";
  original.carto.label_density = "low";
  original.carto.show_buildings = false;
  original.carto.show_power = false;
  original.carto.show_river_arrows = false;
  original.carto.show_slope_shading = true;

  original.outputs.pdf = true;
  original.outputs.geopdf = true;
  original.outputs.mbtiles = true;
  original.outputs.mbtiles_format = "jpg";
  original.outputs.kmz = true;
  original.outputs.garmin_profile = "modern";
  original.outputs.include_grid_on_digital = false;
  original.outputs.gpkg_tiles = true;
  original.outputs.osmand_sqlitedb = true;
  original.outputs.field_bundle_zip = true;
  original.outputs.export_dpi = 450;

  original.include_calibration_page = true;
  original.include_route_card = true;
  original.include_elevation_profile = true;
  original.include_atlas_index = false; // explicitly unchecked on 2x2 grid!
  original.include_rf_link_card = true;
  original.show_fold_marks = true;

  original.viewsheds = [
    {
      id: "vs-1",
      observer: {
        label: "Ridge, North; 1",
        lat: -36.45123,
        lon: 148.26123,
        observer_height_m: 3.5,
        target_height_m: 2,
        max_distance_m: 12500,
      },
      loading: false,
      error: null,
      preview: null,
    },
  ];

  original.waypoints = [
    {
      label: "Base, Camp; A",
      lat: -36.45,
      lon: 148.26,
      category: "camp",
      notes: "Sheltered; near creek, 100%",
    },
    {
      label: "Water Drop",
      lat: -36.46,
      lon: 148.27,
      category: "water",
    },
  ];

  original.rfLinks = [
    {
      id: "rf-1",
      spec: {
        from_lat: -36.45,
        from_lon: 148.26,
        to_lat: -36.47,
        to_lon: 148.29,
        from_height_m: 4,
        to_height_m: 2.5,
        freq_mhz: 477,
        k_factor: 1.333,
        from_label: "Base, 1",
        to_label: "Peak; 2",
      },
      loading: false,
      error: null,
      profile: null,
    },
  ];

  original.tacticalGraphics = [
    {
      kind: "range_ring",
      label: "LZ RING",
      points: [[-36.455, 148.263]],
      radii_km: [1, 3, 5],
      color: "#38BDF8",
    },
    {
      kind: "sector",
      label: "AOI Alpha",
      points: [
        [-36.44, 148.25],
        [-36.44, 148.28],
        [-36.47, 148.27],
      ],
      radii_km: [],
      color: "#F59E0B",
      notes: "Watch cornice",
    },
  ];

  const serialized = serializeStateToParams(original, {
    basemap: "vector",
    zoom: 13.4,
  });
  assert.equal(serialized.truncated, false);
  assert.deepEqual(serialized.omittedGroups, []);
  assert.equal(serialized.params.get("index"), "0");
  assert.equal(serialized.params.get("bm"), "vector");
  assert.equal(serialized.params.get("z"), "13.4");

  const restored = createInitialFormState();
  const hydration = hydrateStateFromSearch(`?${serialized.queryString}`, restored);

  assert.equal(hydration.hasCustomCenter, true);
  assert.equal(hydration.needsFlyToResolvedCenter, false);
  assert.equal(hydration.basemap, "vector");
  assert.equal(hydration.hasCustomBasemap, true);
  assert.equal(hydration.zoom, 13.4);
  assert.equal(hydration.shareId, null);

  assert.equal(restored.lat, -36.4559);
  assert.equal(restored.lon, 148.2636);
  assert.equal(restored.title, "Kosciuszko Winter Ops");
  assert.equal(restored.scale, 50000);
  assert.equal(restored.paper, "a3");
  assert.equal(restored.orientation, "landscape");
  assert.equal(restored.color_mode, "alpine");
  assert.equal(restored.units, "imperial");
  assert.equal(restored.dpi, 600);
  assert.equal(restored.grid_cols, 2);
  assert.equal(restored.grid_rows, 2);
  assert.equal(restored.dem_source, "au-elvis");
  assert.equal(restored.pack_id, "au_nsw_alpine");
  assert.deepEqual(restored.au_overlays, ["au-state-trails", "au-hydro", "au-parks"]);
  assert.deepEqual(restored.nz_overlays, ["nz-doc-trails"]);
  assert.deepEqual(restored.ca_overlays, ["ca-canvec-trails"]);
  assert.deepEqual(restored.us_overlays, ["us-trails", "fema"]);

  assert.deepEqual(restored.carto, original.carto);
  assert.deepEqual(restored.outputs, original.outputs);

  assert.equal(restored.include_calibration_page, true);
  assert.equal(restored.include_route_card, true);
  assert.equal(restored.include_elevation_profile, true);
  assert.equal(restored.include_atlas_index, false);
  assert.equal(restored.include_rf_link_card, true);
  assert.equal(restored.show_fold_marks, true);

  assert.equal(restored.viewsheds.length, 1);
  assert.deepEqual(restored.viewsheds[0].observer, original.viewsheds[0].observer);

  assert.deepEqual(restored.waypoints, original.waypoints);
  assert.equal(restored.rfLinks.length, 1);
  assert.deepEqual(restored.rfLinks[0].spec, original.rfLinks[0].spec);
  assert.deepEqual(restored.tacticalGraphics, original.tacticalGraphics);
});

test("legacy URLs remain backward-compatible and default multi-sheet atlas index to true", () => {
  const restored = createInitialFormState();
  const h = hydrateStateFromSearch(
    "?center=56H+LH+43723+66899&scale=25000&grid=2x2&slope=true&los=-33.7,150.3,2,1.5,10",
    restored
  );

  assert.equal(h.hasCustomCenter, true);
  assert.equal(h.needsFlyToResolvedCenter, true);
  assert.equal(h.basemap, "opentopo");
  assert.equal(h.hasCustomBasemap, false);
  assert.equal(h.zoom, null);
  assert.equal(restored.grid_cols, 2);
  assert.equal(restored.grid_rows, 2);
  assert.equal(restored.include_atlas_index, true);
  assert.equal(restored.carto.show_slope_shading, true);
  assert.equal(restored.viewsheds.length, 1);
  assert.equal(restored.viewsheds[0].observer.label, "OBS1");
});

test("default state serializes cleanly without non-default clutter params", () => {
  const state = createInitialFormState();
  const res = serializeStateToParams(state, { basemap: "opentopo", zoom: 12 });
  assert.equal(res.params.has("bm"), false);
  assert.equal(res.params.has("z"), false);
  assert.equal(res.params.has("contours"), false);
  assert.equal(res.params.has("hillshade"), false);
  assert.equal(res.params.has("labels"), false);
  assert.equal(res.params.has("hide"), false);
  assert.equal(res.params.has("out"), false);
  assert.equal(res.params.has("mbfmt"), false);
  assert.equal(res.params.has("garmin"), false);
  assert.equal(res.params.has("dgrid"), false);
  assert.equal(res.params.has("index"), false);
  assert.equal(res.params.has("wp"), false);
  assert.equal(res.params.has("rf"), false);
  assert.equal(res.params.has("tac"), false);
});

test("URL length budget drops heavy markups in priority order and signals truncation", () => {
  const state = createInitialFormState();
  for (let i = 0; i < 45; i += 1) {
    state.waypoints.push({
      label: `Checkpoint ${i + 1} Ridge Saddle`,
      lat: -33.7 - i * 0.001,
      lon: 150.3 + i * 0.001,
      category: "waypoint",
      notes: "Long field note for testing URL length bounding",
    });
  }
  state.tacticalGraphics.push({
    kind: "sector",
    label: "Large AOI Polygon",
    points: Array.from({ length: 30 }, (_, idx) => [
      -33.7 - idx * 0.001,
      150.3 + idx * 0.001,
    ]),
    radii_km: [],
    color: "#F59E0B",
  });

  const built = buildDirectShareUrl(
    "https://gridforge.example/",
    state,
    { basemap: "opentopo", zoom: 12 },
    900
  );
  assert.equal(built.truncated, true);
  assert.ok(built.omittedGroups.includes("tactical"));
  assert.ok(built.url.length <= 900);
  assert.ok(MAX_DIRECT_URL_LENGTH >= 2000);
});

test("malformed or malicious URL params are safely sanitized or ignored", () => {
  const state = createInitialFormState();
  hydrateStateFromSearch(
    "?scale=99999&paper=evil&bm=javascript:alert(1)&z=999&pack=../etc/passwd&out=geopdf&wp=999,999,bad,X;-33.7,150.3,evil,OK&tac=not_valid_base64!!!&share=../bad",
    state
  );

  assert.equal(state.scale, 25000);
  assert.equal(state.paper, "a4");
  assert.equal(state.pack_id, "");
  assert.equal(state.outputs.pdf, true);
  assert.equal(state.outputs.geopdf, false);
  assert.equal(state.waypoints.length, 1);
  assert.equal(state.waypoints[0].category, "waypoint");
  assert.equal(state.tacticalGraphics.length, 0);
  assert.equal(extractShareIdFromSearch("?share=../bad"), null);
  assert.equal(extractShareIdFromSearch("?share=AbC123_-xyz98765"), "AbC123_-xyz98765");
});

test("hydrateStateFromSnapshot normalizes full saved/shared snapshots cleanly", () => {
  const target = createInitialFormState();
  let vsSeq = 1;
  let rfSeq = 1;
  const view = hydrateStateFromSnapshot(
    {
      center: "-45.0312, 168.6626",
      lat: -45.0312,
      lon: 168.6626,
      title: "Milford Track",
      scale: 25000,
      paper: "a4",
      orientation: "landscape",
      color_mode: "night-red",
      units: "metric",
      dpi: 300,
      grid_cols: 1,
      grid_rows: 2,
      dem_source: "nz-linz",
      nz_overlays: ["nz-doc-trails", "bogus"],
      basemap: "satellite",
      map_zoom: 11.75,
      carto: { contour_density: "low", show_buildings: false, show_slope_shading: true },
      outputs: { pdf: false, kmz: true, garmin_profile: "modern", geopdf: true },
      waypoints: [{ label: "Hut 1", lat: -45.03, lon: 168.66, category: "camp" }],
      viewsheds: [{ label: "Pass", lat: -45.03, lon: 168.66, observer_height_m: 3, target_height_m: 2, max_distance_m: 8000 }],
      rf_links: [{ from_lat: -45.03, from_lon: 168.66, to_lat: -45.04, to_lon: 168.67, from_height_m: 2, to_height_m: 2, freq_mhz: 868, k_factor: 1.333, from_label: "Hut", to_label: "Pass" }],
      tactical_graphics: [{ kind: "range_ring", label: "R1", points: [[-45.03, 168.66]], radii_km: [2, 5], color: "#10B981" }],
      include_route_card: true,
      include_atlas_index: true,
    },
    target,
    {
      nextViewshedId: () => `vs-${vsSeq++}`,
      nextRFLinkId: () => `rf-${rfSeq++}`,
    }
  );

  assert.equal(view.basemap, "satellite");
  assert.equal(view.zoom, 11.75);
  assert.equal(target.color_mode, "night-red");
  assert.deepEqual(target.nz_overlays, ["nz-doc-trails"]);
  assert.equal(target.carto.contour_density, "low");
  assert.equal(target.carto.show_buildings, false);
  assert.equal(target.carto.show_trees, true);
  assert.equal(target.outputs.pdf, false);
  assert.equal(target.outputs.geopdf, false); // stripped because pdf=false
  assert.equal(target.outputs.kmz, true);
  assert.equal(target.outputs.garmin_profile, "modern");
  assert.equal(target.waypoints.length, 1);
  assert.equal(target.viewsheds.length, 1);
  assert.equal(target.rfLinks.length, 1);
  assert.equal(target.tacticalGraphics.length, 1);
});
