import tailwindcss from '@tailwindcss/vite';
import vue from '@vitejs/plugin-vue';
import path from 'path';
import {defineConfig} from 'vite';

export default defineConfig(() => {
  return {
    plugins: [vue(), tailwindcss()],
    resolve: {
      alias: {
        '@': path.resolve(__dirname, '.'),
      },
    },
    server: {
      port: 3000,
      proxy: {
        // Dev convenience: sidesteps CORS for RAG upload-session creation.
        // tusd itself still needs its own CORS config, since the browser
        // uploads directly to it (not proxied) for true resumability.
        '/upload-api': {
          target: 'http://localhost:9003',
          changeOrigin: true,
          rewrite: (path) => path.replace(/^\/upload-api/, ''),
        },
      },
      hmr: process.env.DISABLE_HMR !== 'true',
      watch: process.env.DISABLE_HMR === 'true' ? null : undefined,
    },
  };
});
