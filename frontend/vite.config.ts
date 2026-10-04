import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    // The frontend calls /api/... and the prefix is stripped before the request
    // reaches the local Python service, so backend routes stay unprefixed
    // (/health, /tools, /projects). Local only: never a remote target.
    // VIDEO_FACTORY_API_PORT points a second dev server at a second backend
    // (used for testing against a throwaway workspace); 8000 otherwise.
    proxy: {
      '/api': {
        target: `http://127.0.0.1:${process.env.VIDEO_FACTORY_API_PORT ?? '8000'}`,
        changeOrigin: false,
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
    },
  },
})
