import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The backend has no CORS; the dev and preview servers proxy /api to it instead.
// preview.proxy defaults to server.proxy.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
});
