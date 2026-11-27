import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";
import { viteSingleFile } from "vite-plugin-singlefile";

const mcpAppRoot = fileURLToPath(new URL("./mcp-app", import.meta.url));
const mcpAssetsOutDir = fileURLToPath(
  new URL("../src/gridforge/mcp/assets", import.meta.url)
);

export default defineConfig({
  root: mcpAppRoot,
  plugins: [
    viteSingleFile({
      outputFileName: "mcp_app.html",
      removeViteModuleLoader: true,
    }),
  ],
  build: {
    outDir: mcpAssetsOutDir,
    emptyOutDir: false,
    sourcemap: false,
    target: "es2022",
    minify: "esbuild",
    rollupOptions: {
      input: fileURLToPath(new URL("./mcp-app/index.html", import.meta.url)),
    },
  },
});
