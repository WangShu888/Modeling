import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react-swc";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
    include: ["src/**/*.test.{ts,tsx}"],
    setupFiles: "src/test/setup.ts",
    mockReset: true,
    coverage: {
      reporter: ["text", "html"],
      exclude: ["src/test/**"]
    }
  }
});
