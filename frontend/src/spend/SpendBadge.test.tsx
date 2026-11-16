import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, test, vi } from "vitest";
import SpendBadge from "./SpendBadge";

afterEach(() => vi.unstubAllGlobals());

function stub(response: { ok: boolean; status?: number; body?: unknown } | "network-error") {
  const fetchMock = vi.fn(() =>
    response === "network-error"
      ? Promise.reject(new Error("down"))
      : Promise.resolve({ ok: response.ok, status: response.status ?? 200, json: async () => response.body }),
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

const renderBadge = (surface = "spend") =>
  render(
    <MemoryRouter>
      <SpendBadge surface={surface} />
    </MemoryRouter>,
  );

test("shows what the surface's Feature cost to build, linking to its breakdown", async () => {
  const fetchMock = stub({
    ok: true,
    body: {
      github_available: true,
      surface: "spend",
      feature: { issue: 1, title: "Spend tracking", state: "open", dollars: "123.456" },
    },
  });
  renderBadge();
  const link = await screen.findByRole("link", { name: /built for \$123\.46/i });
  expect(link.getAttribute("href")).toBe("/spend/features/1");
  expect(fetchMock).toHaveBeenCalledWith("/api/spend/surfaces/spend");
});

test("says the Spend is unavailable when GitHub is unreachable", async () => {
  stub({ ok: true, body: { github_available: false, surface: "spend", feature: null } });
  renderBadge();
  expect(await screen.findByText(/spend unavailable/i)).toBeTruthy();
  expect(screen.queryByRole("link")).toBeNull();
});

test("says the Spend is unavailable when the request fails", async () => {
  stub("network-error");
  renderBadge();
  expect(await screen.findByText(/spend unavailable/i)).toBeTruthy();
});

test("says the surface is unknown when no Feature declares it", async () => {
  stub({ ok: false, status: 404 });
  renderBadge("nope");
  expect(await screen.findByText(/unknown ui surface: nope/i)).toBeTruthy();
  expect(screen.queryByRole("link")).toBeNull();
});
