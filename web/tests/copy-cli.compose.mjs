import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { copyFileSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { basename, join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { buildCliCommand, createInitialFormState } from "../src/presets.ts";

// Requires a local gridforge:dev image. Only this temporary Compose project's
// inputs, outputs, and cache are writable. No data providers are reachable.
test("the copied Compose command leaves all requested artifacts on the host", () => {
  const repo = fileURLToPath(new URL("../../", import.meta.url));
  const cwd = mkdtempSync(join(tmpdir(), "gridforge-copy-"));
  const env = { ...process.env, COMPOSE_PROJECT_NAME: basename(cwd).toLowerCase(),
    COMPOSE_FILE: ["docker-compose.yml", "test.yml"].join(process.platform === "win32" ? ";" : ":") };
  const options = { cwd, env, encoding: "utf8", stdio: "pipe" };
  try {
    copyFileSync(join(repo, "docker-compose.yml"), join(cwd, "docker-compose.yml"));
    writeFileSync(join(cwd, "test.yml"), JSON.stringify({ services: { cli: {
      network_mode: "none", pull_policy: "never",
      volumes: [{ type: "bind", source: repo, target: "/app", read_only: true }],
    } } }));
    const pack = join(cwd, "packs", "renamed pack directory");
    mkdirSync(pack, { recursive: true });
    const fixtures = join(repo, "tests", "fixtures");
    for (const file of ["synthetic_dem.tif", "synthetic_osm.osm"]) {
      copyFileSync(join(fixtures, file), join(pack, file));
    }
    const state = createInitialFormState();
    state.title = "Walker's field notes";
    state.include_elevation_profile = true;
    state.pack_id = "local-fixture";
    writeFileSync(join(pack, "pack.json"), JSON.stringify({
      pack_id: state.pack_id, name: "Test pack", region: "test", utm_zones: ["56H"],
      bbox_wgs84: { min_x: 150.20, min_y: -33.85, max_x: 150.42, max_y: -33.62 },
      dem_path: "synthetic_dem.tif", osm_pbf_path: "synthetic_osm.osm", created_at: "2026-01-01",
    }));
    const inputs = join(cwd, "out", "inputs");
    mkdirSync(inputs, { recursive: true });
    state.overlayFile = { name: "Creek's walking trail.geojson" };
    copyFileSync(join(fixtures, "synthetic_overlay.geojson"), join(inputs, state.overlayFile.name));
    Object.assign(state.outputs, { mbtiles: true, kmz: true, include_grid_on_digital: false });
    const { command } = buildCliCommand(state);
    execFileSync("sh", ["-ec", command], options);
    const pdf = execFileSync("docker", ["compose", "run", "--rm", "cli", "pdfinfo",
      "/out/walker_s_field_notes_a4_color.pdf"], options);
    assert.match(pdf, /^Pages:\s+2$/m, "expected the overlay elevation profile and map");
    for (const [ext, signature] of [["pdf", "%PDF-"], ["mbtiles", "SQLite format 3"], ["kmz", "PK"]]) {
      const bytes = readFileSync(join(cwd, "out", "walker_s_field_notes_a4_color." + ext));
      assert.ok(bytes.length > 1024);
      assert.equal(bytes.toString("ascii", 0, signature.length), signature);
    }
  } finally {
    try {
      execFileSync("docker", ["compose", "--profile", "cli", "down", "--volumes"], options);
    } finally {
      rmSync(cwd, { recursive: true, force: true });
    }
  }
});
