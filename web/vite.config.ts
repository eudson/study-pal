import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { tanstackRouter } from "@tanstack/router-plugin/vite";
import { VitePWA } from "vite-plugin-pwa";

const apiProxyTarget = process.env.API_PROXY_TARGET ?? "http://localhost:8000";

export default defineConfig({
  plugins: [
    tanstackRouter({ target: "react", autoCodeSplitting: false }),
    react(),
    VitePWA({
      registerType: "autoUpdate",
      devOptions: {
        enabled: false,
      },
      manifest: {
        name: "StudyPal",
        short_name: "StudyPal",
        theme_color: "#ffffff",
        background_color: "#ffffff",
        icons: [
          {
            src: "/icon.svg",
            sizes: "any",
            type: "image/svg+xml",
            purpose: "any",
          },
        ],
      },
    }),
  ],
  server: {
    // Bind all interfaces so the dev server is reachable through the
    // docker-compose port mapping (not just container-localhost).
    host: true,
    port: 5173,
    proxy: {
      "/health": {
        target: apiProxyTarget,
        changeOrigin: true,
      },
      "/assessments": {
        target: apiProxyTarget,
        changeOrigin: true,
      },
    },
  },
});
