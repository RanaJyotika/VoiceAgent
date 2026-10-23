import type { NextConfig } from "next";
import { PHASE_DEVELOPMENT_SERVER } from "next/constants";

// The Python server (backend/) owns every /api, /ws and /plivo route and the original dashboard at /classic.
const BACKEND = process.env.BACKEND_URL ?? "http://127.0.0.1:8000";

export default function config(phase: string): NextConfig {
  const base: NextConfig = {
    // Pages end in "/" so each one builds to its own folder/index.html, which the Python server serves as is.
    trailingSlash: true,
    // No round "N" badge in the corner while `npm run dev` runs (owner, 22 Oct 2026).
    devIndicators: false,
    turbopack: {
      rules: {
        "*.css": {
          loaders: ["@tailwindcss/turbopack"],
          as: "*.css",
        },
      },
    },
  };

  if (phase === PHASE_DEVELOPMENT_SERVER) {
    // `npm run dev` (port 3000): hand the backend's routes to the Python server.
    return {
      ...base,
      async rewrites() {
        return ["/api/:path*", "/ws/:path*", "/static/:path*", "/classic"].map((source) => ({
          source,
          destination: `${BACKEND}${source}`,
        }));
      },
    };
  }

  // `npm run build`: plain files in out/, served by the Python server on port 8000. No Node server in production.
  return { ...base, output: "export" };
}
