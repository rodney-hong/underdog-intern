import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
  },
  // Component tests (*.test.jsx, jsdom): npx vitest run. The *.test.js files are plain
  // node --test suites (node --test src/*.test.js).
  test: {
    environment: 'jsdom',
    include: ['src/**/*.test.jsx'],
  },
})
