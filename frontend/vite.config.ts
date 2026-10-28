import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Dev proxy: all /api calls go to the FastAPI backend, so the
// backend needs no CORS changes.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
})
