import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import SessionAttributions from "./SessionAttributions";

afterEach(() => vi.unstubAllGlobals());

const ok = (body: unknown) => ({ ok: true, json: async () => body });
const session = (id: string, over = {}) => ({
  session_id: id,
  dollars: "10",
  last_seen: "2026-11-16T03:00:00Z",
  source: "inferred",
  target_issue: 1,
  target_title: "One",
  low_confidence: false,
  ...over,
});
const features = {
  github_available: true,
  features: [
    { issue: 1, title: "One", state: "open", dollars: "10", sessions: 1, low_confidence_sessions: 0 },
    { issue: 2, title: "Two", state: "open", dollars: "5", sessions: 1, low_confidence_sessions: 0 },
  ],
};

function stub(sessions: { low_confidence: boolean }[], post: unknown = ok({})) {
  const fetchMock = vi.fn((url: string, init?: RequestInit) => {
    if (init?.method === "POST") return Promise.resolve(post);
    if (url === "/api/spend/features") return Promise.resolve(ok(features));
    const only = url.includes("needs_review=true");
    return Promise.resolve(ok({ github_available: true, sessions: only ? sessions.filter((s) => s.low_confidence) : sessions }));
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

test("lists Sessions with Feature, source and a low-confidence flag", async () => {
  stub([session("aaa", { low_confidence: true }), session("bbb", { source: "manual", target_issue: null, target_title: null })]);
  render(<SessionAttributions />);
  const rows = (await screen.findAllByRole("row")).slice(1);
  expect(within(rows[0]).getByText("#1 One", { exact: false, selector: "td" })).toBeTruthy();
  expect(within(rows[0]).getByText(/needs review/i)).toBeTruthy();
  expect(within(rows[1]).getByText("Overhead", { selector: "td" })).toBeTruthy();
  expect(within(rows[1]).getByText("manual")).toBeTruthy();
});

test("the needs-review filter asks the API for low-confidence Sessions only", async () => {
  const fetchMock = stub([session("aaa", { low_confidence: true }), session("bbb")]);
  render(<SessionAttributions />);
  await screen.findByText("bbb");
  fireEvent.click(screen.getByLabelText(/needs review only/i));
  await waitFor(() => expect(screen.queryByText("bbb")).toBeNull());
  expect(fetchMock).toHaveBeenCalledWith("/api/spend/sessions?needs_review=true");
});

test("re-attribute control posts the chosen Feature, then refreshes", async () => {
  const fetchMock = stub([session("aaa", { low_confidence: true })]);
  render(<SessionAttributions />);
  const select = await screen.findByLabelText(/re-attribute aaa/i);
  await screen.findByRole("option", { name: "#2 Two" });
  fireEvent.change(select, { target: { value: "2" } });
  await waitFor(() =>
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/spend/sessions/aaa/attribution",
      expect.objectContaining({ method: "POST", body: JSON.stringify({ target_issue: 2 }) }),
    ),
  );
});

test("choosing Overhead posts a null target", async () => {
  const fetchMock = stub([session("aaa")]);
  render(<SessionAttributions />);
  const select = await screen.findByLabelText(/re-attribute aaa/i);
  fireEvent.change(select, { target: { value: "overhead" } });
  await waitFor(() =>
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/spend/sessions/aaa/attribution",
      expect.objectContaining({ body: JSON.stringify({ target_issue: null }) }),
    ),
  );
});

test("shows an error when re-attribution fails", async () => {
  stub([session("aaa")], { ok: false, json: async () => ({}) });
  render(<SessionAttributions />);
  fireEvent.change(await screen.findByLabelText(/re-attribute aaa/i), { target: { value: "overhead" } });
  expect(await screen.findByRole("alert")).toBeTruthy();
});
