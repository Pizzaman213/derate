import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The coordinator serves the built assets from its own origin, so /api and /v1
// are same-origin in production. In dev we proxy them to a coordinator on :8080.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://localhost:8080', changeOrigin: true },
      '/v1': { target: 'http://localhost:8080', changeOrigin: true },
    },
  },
  build: { outDir: 'dist', sourcemap: true },
})
