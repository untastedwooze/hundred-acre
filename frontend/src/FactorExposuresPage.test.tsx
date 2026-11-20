import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, test, vi } from "vitest";
import App from "./App";
import FactorExposuresPage from "./FactorExposuresPage";

afterEach(() => vi.unstubAllGlobals());

const ok = (body: unknown) => ({ ok: true, json: async () => body });
const vintage = { vintage: "2026-11-01-abc", imported_at: "2026-11-01T00:00:00+00:00", through_date: "2026-10-15" };
const ATTRIBUTION = "Kenneth R. French Data Library";

function point(end: string, alpha: number, factors: string[]) {
  return {
    window_start: "2026-01-01", window_end: end, n_obs: 60, alpha, t_alpha: 1, r_squared: 0.9,
    betas: factors.map((f, i) => ({ factor: f, value: 1 + i, t: 2 })),
  };
}
const FF6 = ["Mkt-RF", "SMB", "HML", "RMW", "CMA", "UMD"];
function exposures(model: string, factors: string[]) {
  return {
    ticker: "AAA", model, vintage, attribution: ATTRIBUTION, empty_reason: null, message: null,
    series: [point("2026-08-31", 0.0001, factors), point("2026-09-30", 0.0003, factors)],
  };
}

function stub(responder: (url: string) => unknown) {
  const fetchMock = vi.fn((url: string) =>
    Promise.resolve(
      url.startsWith("/api/market/factors/exposures")
        ? ok(responder(url))
        : url.startsWith("/api/spend/surfaces/")
          ? ok({ github_available: true, surface: "factor-exposures", feature: { issue: 39, title: "Factor Exposures", state: "open", dollars: "12.5" } })
          : ok({ dollars: "1", unpriced_messages: 0 }),
    ),
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

const renderPage = () => render(<MemoryRouter><FactorExposuresPage /></MemoryRouter>);

function search(ticker: string) {
  fireEvent.change(screen.getByLabelText(/ticker/i), { target: { value: ticker } });
  fireEvent.click(screen.getByRole("button", { name: /show/i }));
}

test("charts each coefficient, alpha and R-squared for the chosen Ticker", async () => {
  const fetchMock = stub(() => exposures("ff6", FF6));
  renderPage();
  search("AAA");
  for (const f of [...FF6, "Alpha", "R²"]) {
    expect(await screen.findByRole("img", { name: new RegExp(`^${f} over time`) })).toBeTruthy();
  }
  expect(fetchMock).toHaveBeenCalledWith("/api/market/factors/exposures?ticker=AAA&model=ff6");
});

test("shows the vintage, through-date and French attribution", async () => {
  stub(() => exposures("ff6", FF6));
  renderPage();
  expect(screen.getByText(/Kenneth R\. French Data Library/)).toBeTruthy();
  expect(screen.getByText(/Tuck School of Business at Dartmouth/)).toBeTruthy();
  search("AAA");
  expect(await screen.findByText(/vintage 2026-11-01-abc, through 2026-10-15/i)).toBeTruthy();
});

test("the model switch changes the series shown", async () => {
  const fetchMock = stub((url) => (url.includes("model=capm") ? exposures("capm", ["Mkt-RF"]) : exposures("ff6", FF6)));
  renderPage();
  search("AAA");
  await screen.findByRole("img", { name: /^UMD over time/ });
  fireEvent.click(screen.getByRole("radio", { name: "CAPM" }));
  await vi.waitFor(() => expect(screen.queryByRole("img", { name: /^UMD over time/ })).toBeNull());
  expect(await screen.findByRole("img", { name: /^Mkt-RF over time/ })).toBeTruthy();
  expect(fetchMock).toHaveBeenCalledWith("/api/market/factors/exposures?ticker=AAA&model=capm");
});

test("explains why there are no exposures", async () => {
  stub(() => ({
    ticker: "NEW", model: "ff6", vintage, attribution: ATTRIBUTION, series: [],
    empty_reason: "no_exposures", message: "No FF6 exposures for NEW: too little history.",
  }));
  renderPage();
  search("NEW");
  expect(await screen.findByText(/too little history/)).toBeTruthy();
  expect(screen.queryByRole("img")).toBeNull();
});

test("says so when the request fails", async () => {
  vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("down")));
  renderPage();
  search("AAA");
  expect(await screen.findByText(/could not load factor exposures/i)).toBeTruthy();
});

test("shows the Spend badge for the factor-exposures surface", async () => {
  const fetchMock = stub(() => exposures("ff6", FF6));
  renderPage();
  expect(await screen.findByRole("link", { name: /built for \$12\.50/i })).toBeTruthy();
  expect(fetchMock).toHaveBeenCalledWith("/api/spend/surfaces/factor-exposures");
});

test("is routed at /factors and linked from the Header", async () => {
  stub(() => exposures("ff6", FF6));
  render(<MemoryRouter initialEntries={["/factors"]}><App /></MemoryRouter>);
  expect(await screen.findByRole("heading", { name: /factor exposures/i })).toBeTruthy();
  expect(screen.getByRole("link", { name: /factor exposures/i, description: "" }).getAttribute("href")).toBe("/factors");
});
