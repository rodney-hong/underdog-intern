import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The app calls /api/... (VITE_API_BASE, default /api) in dev and in production. In dev
// (and `vite preview`) this server forwards /api to the backend with the prefix removed,
// as Caddy does on the server, so the browser stays on one origin: no CORS, and the
// SameSite=Strict session cookie works. API_PROXY_TARGET overrides the backend address.
const apiProxy = {
  '/api': {
    target: process.env.API_PROXY_TARGET || 'http://localhost:8000',
    rewrite: (path) => path.replace(/^\/api/, ''),
  },
}

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: apiProxy,
  },
  preview: {
    proxy: apiProxy,
  },
  // Component tests (*.test.jsx, jsdom): npx vitest run. The *.test.js files are plain
  // node --test suites (node --test src/*.test.js).
  test: {
    environment: 'jsdom',
    include: ['src/**/*.test.jsx'],
  },
})
