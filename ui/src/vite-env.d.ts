/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** "auto" (default), "live", or "fixture". See src/api/client.ts. */
  readonly VITE_API_MODE?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
