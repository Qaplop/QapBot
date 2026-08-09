import { defineConfig } from 'vite'

export default defineConfig({
  server: {
    port: 5173,
    // Discord's proxy needs a stable, predictable dev port — see activity/README.md's
    // cloudflared-tunnel-for-local-testing section.
  },
})
