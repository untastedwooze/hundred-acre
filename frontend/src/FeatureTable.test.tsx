import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import FeatureTable from "./FeatureTable";

afterEach(() => vi.unstubAllGlobals());

const feature = (
  issue: number,
  title: string,
  dollars: string,
  state = "open",
  split: { build_dollars: string; maintenance_dollars: string } = { build_dollars: dollars, maintenance_dollars: "0" },
) => ({
  issue,
  title,
  state,
  dollars,
  ...split,
  completed_at: null,
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
  render(<FeatureTable />);
  expect(await screen.findByText("#1 Spend tracking")).toBeTruthy();
  expect(fetchMock).toHaveBeenCalledWith("/api/spend/features");
  expect(screen.getAllByText("$12.35").length).toBeGreaterThan(0);
  expect(screen.getByText("open")).toBeTruthy();
  expect(screen.getByText("closed")).toBeTruthy();
  expect(rowTitles()).toEqual(["#1 Spend tracking", "#2 Backtester", "Overhead"]);
  expect(screen.getByText("$5.00")).toBeTruthy();
});

test("sorts by Spend when the Spend header is clicked", async () => {
  stubFeatures({ ...base, features: [feature(1, "Big", "50"), feature(2, "Small", "1"), feature(3, "Mid", "20")] });
  render(<FeatureTable />);
  await screen.findByText("#1 Big");
  expect(rowTitles().slice(0, 3)).toEqual(["#1 Big", "#3 Mid", "#2 Small"]);
  fireEvent.click(screen.getByRole("button", { name: /spend/i }));
  expect(rowTitles().slice(0, 3)).toEqual(["#2 Small", "#3 Mid", "#1 Big"]);
});

test("says Feature data is unavailable when GitHub is unreachable", async () => {
  stubFeatures({ ...base, github_available: false, features: [], unresolved_dollars: "10" });
  render(<FeatureTable />);
  expect(await screen.findByText(/GitHub is unreachable/i)).toBeTruthy();
  expect(screen.queryByRole("table")).toBeNull();
});

test("says Spend by Feature is unavailable when the API fails", async () => {
  vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("down")));
  render(<FeatureTable />);
  expect(await screen.findByText(/feature breakdown unavailable/i)).toBeTruthy();
});

test("warns about unpriced usage", async () => {
  stubFeatures({ ...base, unpriced_messages: 2, features: [] });
  render(<FeatureTable />);
  expect(await screen.findByText(/2 unpriced/i)).toBeTruthy();
});

test("shows Build Spend and Maintenance Spend next to the total for each Feature", async () => {
  stubFeatures({
    ...base,
    features: [feature(1, "Spend tracking", "12", "closed", { build_dollars: "10", maintenance_dollars: "2" })],
  });
  render(<FeatureTable />);
  await screen.findByText("#1 Spend tracking");
  const headers = screen.getAllByRole("columnheader").map((h) => h.textContent);
  expect(headers).toEqual(expect.arrayContaining(["Build", "Maintenance"]));
  const cells = within(screen.getAllByRole("row")[1]).getAllByRole("cell").map((c) => c.textContent);
  const at = (name: string) => cells[headers.findIndex((h) => h === name)];
  expect([at("Build"), at("Maintenance")]).toEqual(["$10.00", "$2.00"]);
});
