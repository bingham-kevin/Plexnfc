import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Built into the plexnfc package, which serves it at /. Addresses are relative ("./") so the
// panel also works inside Home Assistant's sidebar, which serves it under a path of its own.
export default defineConfig({
  plugins: [react()],
  base: "./",
  build: { outDir: "../plexnfc/static/panel", emptyOutDir: true },
  server: { proxy: { "/panel/api": "http://127.0.0.1:8080", "/poster": "http://127.0.0.1:8080" } },
});
