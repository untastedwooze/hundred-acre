import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import FeatureTable from "./FeatureTable";

afterEach(() => vi.unstubAllGlobals());

const feature = (issue: number, title: string, dollars: string, state = "open") => ({
  issue,
  title,
  state,
  dollars,
  sessions: 1,
  low_confidence_sessions: 0,
});

function stubFeatures(body: unknown) {
  const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => body });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

const base = { github_available: true, overhead_dollars: "5", unresolved_dollars: "0", unpriced_messages: 0 };

function rowTitles() {
  return screen.getAllByRole("row").slice(1).map((r) => within(r).getAllByRole("cell")[0].textContent);
}

test("lists Features with live Spend to the cent, open state, and an Overhead row", async () => {
  const fetchMock = stubFeatures({
    ...base,
    features: [feature(1, "Spend tracking", "12.345"), feature(2, "Backtester", "3", "closed")],
  });
  render(<FeatureTable />, { wrapper: MemoryRouter });
  expect(await screen.findByText("#1 Spend tracking")).toBeTruthy();
  expect(fetchMock).toHaveBeenCalledWith("/api/spend/features");
  expect(screen.getByText("$12.35")).toBeTruthy();
  expect(screen.getByText("open")).toBeTruthy();
  expect(screen.getByText("closed")).toBeTruthy();
  expect(rowTitles()).toEqual(["#1 Spend tracking", "#2 Backtester", "Overhead"]);
  expect(screen.getByText("$5.00")).toBeTruthy();
});

test("sorts by Spend when the Spend header is clicked", async () => {
  stubFeatures({ ...base, features: [feature(1, "Big", "50"), feature(2, "Small", "1"), feature(3, "Mid", "20")] });
  render(<FeatureTable />, { wrapper: MemoryRouter });
  await screen.findByText("#1 Big");
  expect(rowTitles().slice(0, 3)).toEqual(["#1 Big", "#3 Mid", "#2 Small"]);
  fireEvent.click(screen.getByRole("button", { name: /spend/i }));
  expect(rowTitles().slice(0, 3)).toEqual(["#2 Small", "#3 Mid", "#1 Big"]);
});

test("says Feature data is unavailable when GitHub is unreachable", async () => {
  stubFeatures({ ...base, github_available: false, features: [], unresolved_dollars: "10" });
  render(<FeatureTable />, { wrapper: MemoryRouter });
  expect(await screen.findByText(/GitHub is unreachable/i)).toBeTruthy();
  expect(screen.queryByRole("table")).toBeNull();
});

test("says Spend by Feature is unavailable when the API fails", async () => {
  vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("down")));
  render(<FeatureTable />, { wrapper: MemoryRouter });
  expect(await screen.findByText(/feature breakdown unavailable/i)).toBeTruthy();
});

test("warns about unpriced usage", async () => {
  stubFeatures({ ...base, unpriced_messages: 2, features: [] });
  render(<FeatureTable />, { wrapper: MemoryRouter });
  expect(await screen.findByText(/2 unpriced/i)).toBeTruthy();
});

test("links each Feature row to its detail view", async () => {
  stubFeatures({ ...base, features: [feature(7, "Backtester", "3")] });
  render(<FeatureTable />, { wrapper: MemoryRouter });
  const link = await screen.findByRole("link", { name: "#7 Backtester" });
  expect(link.getAttribute("href")).toBe("/spend/features/7");
});
