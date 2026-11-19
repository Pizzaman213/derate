import { defineConfig } from 'vitest/config'

export default defineConfig({
  test: {
    environment: 'jsdom',
    environmentOptions: { jsdom: { pretendToBeVisual: true } },
    setupFiles: ['./tests/setup.ts'],
    include: ['core/**/*.test.ts', 'adapters/**/*.test.ts', 'time/**/*.test.ts', 'chooser/**/*.test.ts', 'types/**/*.test.ts', 'tests/unit/**/*.test.ts', 'datasets/**/*.test.ts', 'icons/**/*.test.ts'],
    testTimeout: 30000,
    reporters: ['default', 'json'],
    outputFile: { json: './tests/.results/vitest.json' },
    pool: 'threads',
    includeTaskLocation: true,
  },
})
