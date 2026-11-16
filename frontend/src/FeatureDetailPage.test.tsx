import { render, screen, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, expect, test, vi } from "vitest";
import FeatureDetailPage from "./FeatureDetailPage";

afterEach(() => vi.unstubAllGlobals());

const detail = {
  github_available: true,
  issue: 1,
  title: "Spend tracking",
  state: "open",
  dollars: "30",
  build_dollars: "25",
  maintenance_dollars: "5",
  unpriced_messages: 0,
  by_stage: [
    { stage: "implement", dollars: "10" },
    { stage: "review", dollars: "20" },
  ],
  by_sub_issue: [
    { issue: 2, title: "Sub work", dollars: "20" },
    { issue: 1, title: "Spend tracking", dollars: "10" },
  ],
  by_model: [{ model: "claude-sonnet-5-5", dollars: "30" }],
  by_session: [
    { session_id: "sess-b", stage: "review", sub_issue: 2, first_seen: "2026-11-16T03:00:00Z", dollars: "20" },
    { session_id: "sess-a", stage: "implement", sub_issue: 1, first_seen: "2026-11-15T03:00:00Z", dollars: "10" },
  ],
};

function renderAt(path: string, response: unknown) {
  const fetchMock = vi.fn().mockResolvedValue(response);
  vi.stubGlobal("fetch", fetchMock);
  render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/spend/features/:issue" element={<FeatureDetailPage />} />
      </Routes>
    </MemoryRouter>,
  );
  return fetchMock;
}

const ok = (body: unknown) => ({ ok: true, status: 200, json: async () => body });

test("shows the Feature's Spend by Stage, sub-issue, model and Session", async () => {
  const fetchMock = renderAt("/spend/features/1", ok(detail));
  expect(await screen.findByRole("heading", { name: /#1 Spend tracking/ })).toBeTruthy();
  expect(fetchMock).toHaveBeenCalledWith("/api/spend/features/1");
  expect(screen.getByText("$30.00", { selector: "strong" })).toBeTruthy();

  const stages = within(screen.getByRole("region", { name: /by stage/i }));
  expect(stages.getByText("implement")).toBeTruthy();
  expect(stages.getByText("$20.00")).toBeTruthy();

  const subs = within(screen.getByRole("region", { name: /by sub-issue/i }));
  expect(subs.getByText("#2 Sub work")).toBeTruthy();

  const models = within(screen.getByRole("region", { name: /by model/i }));
  expect(models.getByText("claude-sonnet-5-5")).toBeTruthy();

  const sessions = within(screen.getByRole("region", { name: /by session/i }));
  expect(sessions.getByText("sess-b")).toBeTruthy();
  expect(sessions.getByText("review")).toBeTruthy();
});

test("links back to the Spend page", async () => {
  renderAt("/spend/features/1", ok(detail));
  const back = await screen.findByRole("link", { name: /back to spend/i });
  expect(back.getAttribute("href")).toBe("/spend");
});

test("says the Feature was not found on a 404", async () => {
  renderAt("/spend/features/9", { ok: false, status: 404, json: async () => ({}) });
  expect(await screen.findByText(/feature not found/i)).toBeTruthy();
});

test("says Feature data is unavailable when GitHub is unreachable", async () => {
  renderAt("/spend/features/1", ok({ github_available: false }));
  expect(await screen.findByText(/GitHub is unreachable/i)).toBeTruthy();
});

test("says the breakdown is unavailable when the API fails", async () => {
  vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("down")));
  render(
    <MemoryRouter initialEntries={["/spend/features/1"]}>
      <Routes>
        <Route path="/spend/features/:issue" element={<FeatureDetailPage />} />
      </Routes>
    </MemoryRouter>,
  );
  expect(await screen.findByText(/breakdown unavailable/i)).toBeTruthy();
});

test("shows the Feature's Build Spend and Maintenance Spend", async () => {
  renderAt("/spend/features/1", ok(detail));
  await screen.findByRole("heading", { name: /#1 Spend tracking/ });
  expect(screen.getByText(/Build/).textContent).toContain("$25.00");
  expect(screen.getByText(/Maintenance/).textContent).toContain("$5.00");
});
