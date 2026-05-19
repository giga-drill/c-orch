import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { domscribe } from "@domscribe/react/vite";

const apiTarget = process.env.C_ORCH_API_TARGET ?? "http://127.0.0.1:8765";

export default defineConfig({
  plugins: [react(), domscribe()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": apiTarget,
    },
  },
});
