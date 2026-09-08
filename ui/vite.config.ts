import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The coordinator serves the built assets from its own origin, so /api and /v1
// are same-origin in production. In dev we proxy them to a coordinator on :8080.
// Declared rather than pulling in @types/node for one variable.
declare const process: { env: Record<string, string | undefined> }
const gateway = process.env.DERATE_GATEWAY ?? 'http://localhost:8080'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      // The coordinator is on :8080 by default. DERATE_GATEWAY moves it,
      // for when :8080 is already taken on the dev machine.
      // `ws: true` on /api is not optional once the node terminal exists:
      // http-proxy does not forward an HTTP Upgrade without it, so the socket
      // would fail the handshake under `npm run dev` and the terminal would
      // simply never connect -- with no /api error to trace it to. Production
      // is unaffected either way; there is no proxy there.
      '/api': { target: gateway, changeOrigin: true, ws: true },
      '/v1': { target: gateway, changeOrigin: true },
    },
  },
  build: { outDir: 'dist', sourcemap: true },
})
