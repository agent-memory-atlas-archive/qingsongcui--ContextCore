import { defineConfig } from 'vitest/config';

export default defineConfig({
  esbuild: { jsx: 'automatic' },
  // Run one isolated DOM suite at a time to avoid paging multiple jsdom heaps
  // while exercising the full-size workflow and pagination fixtures.
  test: { environment: 'node', fileParallelism: false },
});
