import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    // The frontend calls /api/... and the prefix is stripped before the request
    // reaches the local Python service, so backend routes stay unprefixed
    // (/health, /tools, /projects). Local only: never a remote target.
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: false,
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
    },
  },
})
