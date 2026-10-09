import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const coreTarget = process.env.CORE_PROXY_TARGET || 'http://localhost:8000'
const coreHost = coreTarget.replace(/^http(s?):\/\//, '')

export default defineConfig({
  plugins: [react()],
  build: {
    rollupOptions: {
      output: {
        manualChunks: {
          echarts: ['echarts/core', 'echarts/charts', 'echarts/components', 'echarts/renderers'],
          vendor: ['react', 'react-dom', 'react-router-dom', '@tanstack/react-query', 'zustand'],
        },
      },
    },
  },
  server: {
    port: 5173,
    host: true,
    allowedHosts: ['.monkeycode-ai.online'],
    proxy: {
      '/api': {
        target: coreTarget,
        changeOrigin: true,
      },
      '/ws': {
        target: `ws://${coreHost}`,
        ws: true,
        changeOrigin: true,
      },
    },
  },
})
