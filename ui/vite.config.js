import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // The control API is a separate deployable; in development it is proxied
    // so the browser sees one origin.
    proxy: { '/api': 'http://127.0.0.1:8000' },
  },
})
