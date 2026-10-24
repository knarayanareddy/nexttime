import type { Plugin } from "vite";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

declare const process: { env: Record<string, string | undefined> };

const apiProxyTarget = process.env.VITE_API_PROXY_TARGET ?? "http://127.0.0.1:8000";

function rootToBaseRedirectPlugin(): Plugin {
  return {
    name: "nexttime-root-redirect",
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        const reqUrl = (req as { url?: string }).url;
        if (reqUrl === "/" || reqUrl === "") {
          res.statusCode = 302;
          res.setHeader("Location", "/nexttime/");
          res.end();
          return;
        }
        next();
      });
    },
    configurePreviewServer(server) {
      server.middlewares.use((req, res, next) => {
        const reqUrl = (req as { url?: string }).url;
        if (reqUrl === "/" || reqUrl === "") {
          res.statusCode = 302;
          res.setHeader("Location", "/nexttime/");
          res.end();
          return;
        }
        next();
      });
    },
  };
}

export default defineConfig({
  base: "/nexttime/",
  plugins: [react(), rootToBaseRedirectPlugin()],
  server: {
    host: "0.0.0.0",
    port: 3000,
    allowedHosts: true,
    proxy: {
      "/v1": {
        target: apiProxyTarget,
        changeOrigin: true,
        ws: true,
      },
      "/healthz": {
        target: apiProxyTarget,
        changeOrigin: true,
      },
    },
  },
  preview: {
    host: "0.0.0.0",
    port: 3000,
    allowedHosts: true,
    proxy: {
      "/v1": {
        target: apiProxyTarget,
        changeOrigin: true,
        ws: true,
      },
      "/healthz": {
        target: apiProxyTarget,
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
  },
});
