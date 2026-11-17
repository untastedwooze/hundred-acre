import { render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import App from "./App";

afterEach(() => vi.unstubAllGlobals());

const ok = (body: unknown) => ({ ok: true, json: async () => body });

test("renders the API health status", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string) =>
      Promise.resolve(url === "/api/health" ? ok({ status: "ok" }) : ok({ dollars: "0", unpriced_messages: 0 })),
    ),
  );
  render(<MemoryRouter><App /></MemoryRouter>);
  expect(await screen.findByText(/API status: ok/i)).toBeTruthy();
});

test("shows an error when the API is unreachable", async () => {
  vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("down")));
  render(<MemoryRouter><App /></MemoryRouter>);
  expect(await screen.findByText(/API status: unreachable/i)).toBeTruthy();
});

test("shows total Spend in the header of the app", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string) =>
      Promise.resolve(url === "/api/health" ? ok({ status: "ok" }) : ok({ dollars: "42.4", unpriced_messages: 0 })),
    ),
  );
  render(<MemoryRouter><App /></MemoryRouter>);
  expect(await screen.findByText("$42.40")).toBeTruthy();
});
