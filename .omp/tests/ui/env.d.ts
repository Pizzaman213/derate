// Module shapes the gallery and checks import that TypeScript cannot see on
// its own: stylesheets (esbuild emits them) and JSON fixtures.
declare module '*.css'
