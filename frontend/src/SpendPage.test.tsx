import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeAll, expect, test, vi } from "vitest";
import App from "./App";
import SpendPage from "./SpendPage";

beforeAll(() => {
  vi.stubEnv("TZ", "America/New_York"); // UTC-5 in November; proves UTC -> local conversion
});
afterEach(() => {
  vi.unstubAllGlobals();
});

const ok = (body: unknown) => ({ ok: true, json: async () => body });

function stubSeries(body: unknown) {
  const fetchMock = vi.fn((url: string) =>
    Promise.resolve(url === "/api/spend/series" ? ok(body) : ok({ dollars: "25", unpriced_messages: 0 })),
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

const series = {
  points: [
    { timestamp: "2026-11-14T10:00:00Z", dollars: "15" },
    { timestamp: "2026-11-16T03:00:00Z", dollars: "25.5" },
  ],
  unpriced_messages: 0,
};

test("renders the cumulative chart from the API series", async () => {
  const fetchMock = stubSeries(series);
  render(<SpendPage />, { wrapper: MemoryRouter });
  const chart = await screen.findByRole("img", { name: /cumulative spend/i });
  expect(fetchMock).toHaveBeenCalledWith("/api/spend/series");
  expect(chart.querySelectorAll("circle")).toHaveLength(2);
  expect(screen.getByText("$25.50", { selector: "strong" })).toBeTruthy(); // latest cumulative figure, to the cent
});

test("shows the series' start and end in local time", async () => {
  stubSeries(series);
  render(<SpendPage />, { wrapper: MemoryRouter });
  await screen.findByRole("img", { name: /cumulative spend/i });
  // 10:00Z is 05:00 in New York on the 14th; 03:00Z on the 16th is 22:00 on the 15th.
  expect(screen.getByText(/Nov 14, 2026, 5:00 AM/, { selector: "time" })).toBeTruthy();
  expect(screen.getByText(/Nov 15, 2026, 10:00 PM/, { selector: "time" })).toBeTruthy();
});

test("shows an empty state when there is no Spend yet", async () => {
  stubSeries({ points: [], unpriced_messages: 0 });
  render(<SpendPage />, { wrapper: MemoryRouter });
  expect(await screen.findByText(/no spend recorded yet/i)).toBeTruthy();
  expect(screen.queryByRole("img", { name: /cumulative spend/i })).toBeNull();
});

test("warns when unpriced usage is excluded from the series", async () => {
  stubSeries({ ...series, unpriced_messages: 4 });
  render(<SpendPage />, { wrapper: MemoryRouter });
  expect(await screen.findByText(/4 unpriced messages/i)).toBeTruthy();
});

test("shows Spend as unavailable when the API fails", async () => {
  vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("down")));
  render(<SpendPage />, { wrapper: MemoryRouter });
  expect(await screen.findByText(/spend unavailable/i)).toBeTruthy();
});

test("states what the figure covers and excludes", async () => {
  stubSeries(series);
  render(<SpendPage />, { wrapper: MemoryRouter });
  const coverage = await screen.findByRole("note", { name: /coverage/i });
  expect(coverage.textContent).toMatch(/estimate/i);
  expect(coverage.textContent).toMatch(/local Claude Code Session transcripts/i);
  expect(coverage.textContent).toMatch(/background calls/i);
  expect(coverage.textContent).toMatch(/cloud sessions/i);
  expect(coverage.textContent).toMatch(/claude\.ai chats/i);
});

test("the header total links to the Spend page, which is routed at /spend", async () => {
  stubSeries(series);
  render(
    <MemoryRouter initialEntries={["/"]}>
      <App />
    </MemoryRouter>,
  );
  const link = await screen.findByRole("link", { name: /spend to date/i });
  expect(link.getAttribute("href")).toBe("/spend");
});

test("/spend renders the Spend page inside the app", async () => {
  stubSeries(series);
  render(
    <MemoryRouter initialEntries={["/spend"]}>
      <App />
    </MemoryRouter>,
  );
  expect(await screen.findByRole("heading", { name: /^spend$/i })).toBeTruthy();
  expect(await screen.findByRole("img", { name: /cumulative spend/i })).toBeTruthy();
});
