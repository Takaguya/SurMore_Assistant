import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In development the API runs separately on :8000; Vite forwards /api requests to it.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5180,
    proxy: {
      "/api": { target: "http://127.0.0.1:8000", changeOrigin: true },
    },
  },
});
