import { defineConfig } from "vite";
export default defineConfig({
  resolve: { preserveSymlinks: true },
  build: { outDir: "../src/autopilot/static", emptyOutDir: true },
  server: { proxy: { "/api": "http://127.0.0.1:8765" } },
});
