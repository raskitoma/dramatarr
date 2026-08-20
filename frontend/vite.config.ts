import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'
import { VitePWA } from 'vite-plugin-pwa'

/* One id per build, so the shell cache name changes and `activate` sweeps the previous one.
   Only the shell — the API and media caches deliberately carry no id, or every deploy would
   delete a library somebody spent a night downloading. */
const BUILD_ID = process.env.BUILD_ID ?? String(Date.now())

export default defineConfig({
  define: { __BUILD_ID__: JSON.stringify(BUILD_ID) },
  plugins: [
    react(),
    /* The service worker is hand-written (`src/sw.ts`); the plugin is here for exactly one
       thing, `self.__WB_MANIFEST` — the list of built files with their hashed names, which
       cannot be known before the build. Nothing from workbox runs at runtime.

       `manifest: false` because `public/manifest.webmanifest` already exists and index.html
       links it. `injectRegister: false` because registration is a decision the app makes
       (`src/offline/register.ts`), not something to be injected into index.html.

       Source maps are excluded deliberately: `dist` is 2.3 MB of maps against 947 KB of real
       assets, and precaching a map buys nothing on a plane. */
    VitePWA({
      strategies: 'injectManifest',
      srcDir: 'src',
      filename: 'sw.ts',
      injectRegister: false,
      manifest: false,
      injectManifest: {
        globPatterns: ['**/*.{js,css,html,svg,png,webmanifest}'],
        globIgnores: ['**/*.map'],
      },
    }),
  ],
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
