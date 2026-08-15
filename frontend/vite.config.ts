import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { '@': new URL('./src', import.meta.url).pathname },
  },
  server: {
    port: 5173,
    proxy: {
      // Dev only. In production nginx serves the SPA and proxies /api to the API container,
      // so the app is same-origin and the session cookie needs no CORS relaxation.
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: false,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
    rollupOptions: {
      output: {
        manualChunks: {
          vendor: ['react', 'react-dom', 'wouter'],
          i18n: ['i18next', 'react-i18next'],
        },
      },
    },
  },
  test: {
    environment: 'node',
    // Component tests opt into jsdom with a per-file `@vitest-environment` docblock, so the
    // pure helper tests keep running in node.
    include: ['src/**/*.test.ts', 'src/**/*.test.tsx'],
  },
})
