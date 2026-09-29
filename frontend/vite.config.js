import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const coreTarget = process.env.CORE_PROXY_TARGET || 'http://localhost:8000'
const coreHost = coreTarget.replace(/^http(s?):\/\//, '')

export default defineConfig({
  plugins: [react()],
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
