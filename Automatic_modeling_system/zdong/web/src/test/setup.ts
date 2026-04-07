import "@testing-library/jest-dom";
import { vi } from "vitest";

if (!globalThis.fetch) {
  globalThis.fetch = vi.fn(() =>
    Promise.resolve(
      new Response(JSON.stringify({}), {
        status: 200,
        headers: {
          "Content-Type": "application/json"
        }
      })
    )
  );
}

if (!globalThis.navigator.clipboard) {
  Object.defineProperty(globalThis.navigator, "clipboard", {
    configurable: true,
    value: {
      writeText: vi.fn()
    }
  });
}
