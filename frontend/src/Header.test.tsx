import { render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import Header from "./Header";

afterEach(() => vi.unstubAllGlobals());

function stubTotal(body: unknown) {
  const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => body });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

test("shows total Spend to the cent", async () => {
  const fetchMock = stubTotal({ dollars: "12.34567", unpriced_messages: 0 });
  render(<MemoryRouter><Header /></MemoryRouter>);
  expect(await screen.findByText("$12.35")).toBeTruthy();
  expect(fetchMock).toHaveBeenCalledWith("/api/spend/total");
  expect(screen.queryByText(/unpriced/i)).toBeNull();
});

test("warns when some usage is unpriced", async () => {
  stubTotal({ dollars: "1.5", unpriced_messages: 3 });
  render(<MemoryRouter><Header /></MemoryRouter>);
  expect(await screen.findByText("$1.50")).toBeTruthy();
  expect(screen.getByText(/3 unpriced/i)).toBeTruthy();
});

test("shows Spend as unavailable when the API fails", async () => {
  vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("down")));
  render(<MemoryRouter><Header /></MemoryRouter>);
  expect(await screen.findByText(/spend unavailable/i)).toBeTruthy();
});
