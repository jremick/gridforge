import { defineConfig } from "vite";

export default defineConfig({
  base: "/",
  server: {
    host: "127.0.0.1",
    cors: false,
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8080",
      },
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: false,
    target: "es2022",
    chunkSizeWarningLimit: 1200,
    rollupOptions: {
      output: {
        manualChunks: {
          maplibre: ["maplibre-gl"],
        },
      },
    },
  },
});
