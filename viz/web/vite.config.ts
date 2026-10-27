import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 开发态只开一个口：页面与 /api 同源（都在 5173），由 Vite 把 /api 转给 8765 的 FastAPI。
// 生产态（8765 单进程）由 server.py 托管 dist/，同样同源——两种形态都不需要 CORS。
export default defineConfig({
  plugins: [react()],
  base: "/",
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    // SSE 也走这条代理：http-proxy 逐块转发，不攒批（已实测逐帧到达）
    proxy: { "/api": { target: "http://127.0.0.1:8765" } },
  },
  build: { outDir: "dist", emptyOutDir: true, sourcemap: false },
});
