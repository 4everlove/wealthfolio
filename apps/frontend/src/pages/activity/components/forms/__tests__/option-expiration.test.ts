import { ACTIVITY_SUBTYPES } from "@/lib/constants";
import { isValidOptionExpiration } from "@/lib/occ-symbol";
import type { TFunction } from "i18next";
import { describe, expect, it } from "vitest";
import { validateTradeFields } from "../../mobile-forms/mobile-activity-form";
import { buyFormSchema } from "../buy-form";
import { sellFormSchema } from "../sell-form";

const trade = {
  activityType: "BUY",
  assetType: "option",
  accountId: "account-1",
  assetId: "",
  activityDate: new Date("2026-09-15T00:00:00Z"),
  quantity: 1,
  unitPrice: 5,
  fee: 0,
  tax: 0,
  currency: "USD",
  underlyingSymbol: "AAPL",
  strikePrice: 150,
  optionType: "CALL",
  contractMultiplier: 100,
  subtype: ACTIVITY_SUBTYPES.POSITION_OPEN,
};
const t = ((key: string) => key) as TFunction;

describe("option expiration validation", () => {
  it.each([
    undefined,
    "",
    "0002-12-31",
    "0999-12-31",
    "1000-01-01",
    "1999-12-31",
    "2100-01-01",
    "2027-02-29",
    "2028-02-30",
    "2027-13-01",
    "2027-1-01",
  ])("rejects %s across desktop and mobile trades", (expirationDate) => {
    expect(isValidOptionExpiration(expirationDate)).toBe(false);
    const values = { ...trade, expirationDate };
    for (const schema of [buyFormSchema, sellFormSchema]) {
      const result = schema.safeParse(values);
      expect(result.success).toBe(false);
      if (!result.success)
        expect(result.error.issues.some((issue) => issue.path[0] === "expirationDate")).toBe(true);
    }
    for (const activityType of ["BUY", "SELL"]) {
      expect(validateTradeFields({ ...values, activityType }, t)?.field).toBe("expirationDate");
    }
  });

  it.each(["2000-01-01", "2000-02-29", "2027-12-31", "2028-02-29", "2099-12-31"])(
    "accepts %s",
    (expirationDate) => {
      expect(isValidOptionExpiration(expirationDate)).toBe(true);
      const values = { ...trade, expirationDate };
      expect(buyFormSchema.safeParse(values).success).toBe(true);
      expect(sellFormSchema.safeParse(values).success).toBe(true);
      expect(validateTradeFields(values, t)).toBeNull();
    },
  );
});
