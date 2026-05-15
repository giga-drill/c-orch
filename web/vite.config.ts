import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const apiTarget = process.env.C_ORCH_API_TARGET ?? "http://127.0.0.1:8765";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": apiTarget,
    },
  },
});
