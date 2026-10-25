import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ResetProviderHistoryDialog } from "./reset-provider-history-dialog";
import { RefreshQuotesConfirmDialog } from "./refresh-quotes-confirm-dialog";

const mocks = vi.hoisted(() => ({
  resetProviderHistory: vi.fn(),
  resetAllProviderHistory: vi.fn(),
  toast: vi.fn(),
}));
vi.mock("@/adapters", () => ({
  resetProviderHistory: mocks.resetProviderHistory,
  resetAllProviderHistory: mocks.resetAllProviderHistory,
}));
vi.mock("@wealthfolio/ui/components/ui/use-toast", () => ({
  useToast: () => ({ toast: mocks.toast }),
}));

function setup(global = false) {
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  const onOpenChange = vi.fn();
  render(
    <QueryClientProvider client={client}>
      <ResetProviderHistoryDialog
        allAssets={global}
        assetId={global ? undefined : "asset-1"}
        assetName={global ? undefined : "Example"}
        open
        onOpenChange={onOpenChange}
      />
    </QueryClientProvider>,
  );
  return { client, onOpenChange };
}

const result = {
  assetId: "asset-1",
  source: "YAHOO",
  fromDate: "2020-01-01",
  toDate: "2026-09-17",
  insertedCount: 3,
  deletedCount: 7,
};

describe("Reset provider history", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.resetProviderHistory.mockResolvedValue(result);
  });

  it("describes replacement and preserves prices until confirmation; cancellation sends nothing", () => {
    const { onOpenChange } = setup();
    expect(screen.getByText(/replacement may be shorter/)).toHaveTextContent(
      "Manual and broker prices will be preserved",
    );
    expect(mocks.resetProviderHistory).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onOpenChange).toHaveBeenCalledWith(false);
    expect(mocks.resetProviderHistory).not.toHaveBeenCalled();
  });

  it("submits only the selected asset once and disables repeat submission while pending", async () => {
    let finish!: (value: typeof result) => void;
    mocks.resetProviderHistory.mockReturnValue(
      new Promise((resolve) => {
        finish = resolve;
      }),
    );
    setup();
    const confirm = screen.getByRole("button", { name: "Reset provider history" });
    fireEvent.click(confirm);
    fireEvent.click(confirm);
    await waitFor(() => expect(mocks.resetProviderHistory).toHaveBeenCalledTimes(1));
    expect(mocks.resetProviderHistory).toHaveBeenCalledWith("asset-1");
    expect(screen.getByRole("button", { name: "Resetting…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();
    await act(async () => {
      finish(result);
      await Promise.resolve();
    });
  });

  it("can reset the same asset again after a successful controlled close and reopen", async () => {
    const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
    const onOpenChange = vi.fn();
    const dialog = (open: boolean) => (
      <QueryClientProvider client={client}>
        <ResetProviderHistoryDialog
          assetId="asset-1"
          assetName="Example"
          open={open}
          onOpenChange={onOpenChange}
        />
      </QueryClientProvider>
    );
    const { rerender } = render(dialog(true));
    fireEvent.click(screen.getByRole("button", { name: "Reset provider history" }));
    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false));
    rerender(dialog(false));
    expect(
      screen.queryByRole("button", { name: "Reset provider history" }),
    ).not.toBeInTheDocument();
    rerender(dialog(true));
    const confirm = screen.getByRole("button", { name: "Reset provider history" });
    expect(confirm).toBeEnabled();
    fireEvent.click(confirm);
    await waitFor(() => expect(mocks.resetProviderHistory).toHaveBeenCalledTimes(2));
    expect(mocks.resetProviderHistory).toHaveBeenNthCalledWith(1, "asset-1");
    expect(mocks.resetProviderHistory).toHaveBeenNthCalledWith(2, "asset-1");
    expect(mocks.resetAllProviderHistory).not.toHaveBeenCalled();
  });

  it("reports saved prices and requested recalculation, and refreshes caches", async () => {
    const { client, onOpenChange } = setup();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    fireEvent.click(screen.getByRole("button", { name: "Reset provider history" }));
    await waitFor(() =>
      expect(mocks.toast).toHaveBeenCalledWith(
        expect.objectContaining({
          title: "Provider history replaced",
          description: "Prices were replaced. Portfolio recalculation has been requested.",
        }),
      ),
    );
    expect(invalidate).toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Reset provider history" })).toBeEnabled();
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it.each([
    "Already refreshing this asset",
    "Invalid provider history: invalid returned rows",
    "Asset or provider settings changed during fetching; history was not replaced",
    "Provider network connection failed before replacement",
  ])("shows backend rejection: %s", async (message) => {
    mocks.resetProviderHistory.mockRejectedValue(
      Object.assign(new Error(message), { outcomeUnknown: false }),
    );
    setup();
    fireEvent.click(screen.getByRole("button", { name: "Reset provider history" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    expect(mocks.toast).not.toHaveBeenCalled();
    expect(screen.queryByText(/prices may already have been replaced/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reset provider history" })).toBeEnabled();
    expect(mocks.resetProviderHistory).toHaveBeenCalledTimes(1);
  });

  it.each([
    "Request timed out",
    "Internal Server Error",
    "Reset completion could not be confirmed. Reload quotes before retrying.",
  ])("does not retry an uncertain response or claim a rollback: %s", async (message) => {
    mocks.resetProviderHistory.mockRejectedValue(
      Object.assign(new Error(message), { outcomeUnknown: true }),
    );
    setup();
    fireEvent.click(screen.getByRole("button", { name: "Reset provider history" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "prices may already have been replaced",
    );
    expect(screen.getByRole("button", { name: "Reset provider history" })).toBeDisabled();
    expect(mocks.resetProviderHistory).toHaveBeenCalledTimes(1);
  });

  it("ordinary refresh confirms merging instead of deletion", () => {
    render(
      <RefreshQuotesConfirmDialog
        open
        onOpenChange={vi.fn()}
        onConfirm={vi.fn()}
        assetName="Example"
      />,
    );
    expect(screen.getByText(/merge it with existing prices/)).toHaveTextContent("Older history");
    expect(screen.queryByText(/delete and replace/)).not.toBeInTheDocument();
  });
});

describe("Reset all provider history", () => {
  beforeEach(() => vi.clearAllMocks());
  it("does not interpret a missing asset as authorization to reset everything", async () => {
    const client = new QueryClient();
    render(
      <QueryClientProvider client={client}>
        <ResetProviderHistoryDialog open onOpenChange={vi.fn()} />
      </QueryClientProvider>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Reset provider history" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("No asset selected");
    expect(mocks.resetAllProviderHistory).not.toHaveBeenCalled();
    expect(mocks.resetProviderHistory).not.toHaveBeenCalled();
  });
  it("confirms the global scope and cancels without sending a request", () => {
    setup(true);
    expect(screen.getByText(/all eligible assets/)).toHaveTextContent(
      "successful replacements are not rolled back",
    );
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(mocks.resetAllProviderHistory).not.toHaveBeenCalled();
  });
  it("reports partial failure by asset and prevents repeating successful resets", async () => {
    mocks.resetAllProviderHistory.mockResolvedValue({
      results: [result],
      failures: [{ assetId: "failed-asset", error: "Invalid history" }],
      skipped: [{ assetId: "manual-asset", reason: "Manual prices" }],
    });
    const { onOpenChange } = setup(true);
    fireEvent.click(screen.getByRole("button", { name: "Reset provider history" }));
    const status = await screen.findByRole("status");
    expect(status).toHaveTextContent("Replaced: 1. Failed: 1. Skipped: 1.");
    expect(status).toHaveTextContent("failed-asset: Invalid history");
    expect(status).toHaveTextContent("manual-asset: Manual prices");
    expect(status).toHaveTextContent("recalculation has been requested");
    expect(
      screen.queryByRole("button", { name: "Reset provider history" }),
    ).not.toBeInTheDocument();
    expect(mocks.resetAllProviderHistory).toHaveBeenCalledTimes(1);
    expect(mocks.resetProviderHistory).not.toHaveBeenCalled();
    expect(onOpenChange).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });
  it("never automatically retries a global reset after an interrupted response", async () => {
    mocks.resetAllProviderHistory.mockRejectedValue(
      Object.assign(new Error("Network connection lost"), { outcomeUnknown: true }),
    );
    setup(true);
    fireEvent.click(screen.getByRole("button", { name: "Reset provider history" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "prices may already have been replaced",
    );
    expect(screen.getByRole("button", { name: "Reset provider history" })).toBeDisabled();
    expect(mocks.resetAllProviderHistory).toHaveBeenCalledTimes(1);
  });
});
