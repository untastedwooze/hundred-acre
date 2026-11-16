import { render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import App from "./App";

afterEach(() => vi.unstubAllGlobals());

test("renders the API health status", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({ ok: true, json: async () => ({ status: "ok" }) }),
  );
  render(<App />);
  expect(await screen.findByText(/API status: ok/i)).toBeTruthy();
});

test("shows an error when the API is unreachable", async () => {
  vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("down")));
  render(<App />);
  expect(await screen.findByText(/API status: unreachable/i)).toBeTruthy();
});
