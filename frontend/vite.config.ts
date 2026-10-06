import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { fileURLToPath } from 'node:url';

export default defineConfig({
  plugins: [react()],
  base: './',
  build: {
    manifest: true,
    rollupOptions: {
      input: {
        secretary: fileURLToPath(new URL('./index.html', import.meta.url)),
        team: fileURLToPath(new URL('./team.html', import.meta.url)),
      },
    },
  },
  server: {
    host: '127.0.0.1',
    strictPort: true,
    proxy: {
      '/api/team': { target: 'http://127.0.0.1:8766', changeOrigin: false },
      '/api': { target: 'http://127.0.0.1:8765', changeOrigin: false },
    },
  },
});
