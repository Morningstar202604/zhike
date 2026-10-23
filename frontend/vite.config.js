import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const coreTarget = process.env.CORE_PROXY_TARGET || 'http://localhost:8010'
const coreHost = coreTarget.replace(/^http(s?):\/\//, '')

export default defineConfig({
  plugins: [react()],
  build: {
    rollupOptions: {
      output: {
        // Vite 8（rolldown）只接受函数形式：按模块 id 分组，避免主包超 500KB
        manualChunks(id) {
          if (id.includes('node_modules/echarts')) return 'echarts'
          if (/node_modules[\/](react|react-dom|react-router|react-router-dom|@tanstack|zustand)/.test(id)) return 'vendor'
          return undefined
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
