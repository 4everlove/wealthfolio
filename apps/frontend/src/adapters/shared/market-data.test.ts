import { expect, it, vi } from "vitest";
const mocks = vi.hoisted(() => ({ invoke: vi.fn(), logger: { error: vi.fn() } }));
vi.mock("./platform", () => mocks);
import { resetProviderHistory } from "./market-data";
it("uses the same reset command and assetId on the platform adapter", async () => {
  mocks.invoke.mockResolvedValue({ assetId: "asset-1" });
  expect(await resetProviderHistory("asset-1")).toEqual({ assetId: "asset-1" });
  expect(mocks.invoke).toHaveBeenCalledWith("reset_provider_history", { assetId: "asset-1" });
});

it("uses the global command without an asset payload", async () => {
  const { resetAllProviderHistory } = await import("./market-data");
  mocks.invoke.mockResolvedValue({
    results: [],
    failures: [],
    skipped: [],
  });
  await resetAllProviderHistory();
  expect(mocks.invoke).toHaveBeenCalledWith("reset_all_provider_history");
});

it.each(["single", "all"])(
  "classifies %s reset errors without inspecting messages",
  async (scope) => {
    const { resetAllProviderHistory } = await import("./market-data");
    const reset = () =>
      scope === "single" ? resetProviderHistory("asset-1") : resetAllProviderHistory();
    const rejection = {
      message: "Asset or provider settings changed during fetching; history was not replaced",
      outcomeUnknown: false,
    };
    for (const error of [
      rejection,
      Object.assign(new Error(rejection.message), { outcomeUnknown: false }),
    ]) {
      mocks.invoke.mockRejectedValueOnce(error);
      await expect(reset()).rejects.toMatchObject(rejection);
    }
    for (const error of [
      new TypeError("Failed to fetch"),
      new Error("Command timed out"),
      new SyntaxError("Invalid JSON response"),
      { message: "Reset completion could not be confirmed.", outcomeUnknown: true },
      Object.assign(new Error("Internal Server Error"), { outcomeUnknown: true }),
    ]) {
      mocks.invoke.mockRejectedValueOnce(error);
      await expect(reset()).rejects.toMatchObject({ outcomeUnknown: true, message: error.message });
    }
  },
);
