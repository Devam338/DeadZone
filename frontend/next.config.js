/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // Allow the app to be exported as static HTML if needed
  output: process.env.STATIC_EXPORT ? 'export' : undefined,
  // Proxy /api/query to FastAPI backend
  async rewrites() {
    const base = process.env.API_URL || 'http://localhost:8000';
    return [
      { source: '/api/query',          destination: `${base}/query` },
      { source: '/api/chat',           destination: `${base}/chat` },
      { source: '/api/chat/:sid',      destination: `${base}/chat/:sid` },
      { source: '/api/top-dead-zones', destination: `${base}/top-dead-zones` },
      { source: '/api/health',         destination: `${base}/health` },
    ];
  },
};

module.exports = nextConfig;
