import { defineConfig, type Plugin } from 'vite';
import react from '@vitejs/plugin-react';
import { fileURLToPath } from 'node:url';
import { createSecretaryReleaseManifest } from './scripts/releaseManifest';

const projectRoot = fileURLToPath(new URL('..', import.meta.url));
const releaseManifest = createSecretaryReleaseManifest(projectRoot);
const releaseManifestPlugin: Plugin = {
  name: 'secretary-release-manifest',
  generateBundle() {
    this.emitFile({
      type: 'asset',
      fileName: 'secretary-release.json',
      source: `${JSON.stringify(releaseManifest, null, 2)}\n`,
    });
  },
};

export default defineConfig({
  plugins: [react(), releaseManifestPlugin],
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
