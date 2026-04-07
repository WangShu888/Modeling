import { defineConfig } from "vite";
import react from "@vitejs/plugin-react-swc";

const backendUrl = "http://127.0.0.1:3000";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    setupFiles: "./src/test/setup.ts",
    clearMocks: true,
    restoreMocks: true
  },
  server: {
    host: "0.0.0.0",
    port: 3001,
    watch: {
      usePolling: true,
      interval: 1000
    },
    proxy: {
      "/api": backendUrl,
      "/health": backendUrl
    }
  }
});
