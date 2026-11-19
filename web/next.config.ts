import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "standalone",
  // Keep the live event stream unbuffered through the proxy.
  compress: false,
  async rewrites() {
    const api = process.env.API_URL ?? "http://127.0.0.1:8701"
    return [{ source: "/api/:path*", destination: `${api}/api/:path*` }]
  },
  cacheComponents: true,
  // The advisor can take a minute or two on the free tier; the default 30 s cuts it off.
  experimental: { proxyTimeout: 200_000 },
  partialPrefetching: true,
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
};

export default nextConfig;
