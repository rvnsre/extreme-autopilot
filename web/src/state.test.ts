import { describe, it, expect } from "vitest";
import { briefLength, money, terminal } from "./state";
describe("brief validation matches Python Unicode codepoints", () => {
  it("trims outer whitespace and counts astral codepoints once", () =>
    expect(briefLength("  😀ə\n ")).toBe(2));
  it("uses the 200 character boundary", () => {
    expect(briefLength("ə".repeat(199))).toBe(199);
    expect(briefLength("ə".repeat(200))).toBe(200);
  });
  it("formats the billing estimate", () =>
    expect(money(0.12345)).toBe("$0.123"));
  it("keeps recovering cancellable", () => {
    expect(terminal.has("recovering")).toBe(false);
    expect(terminal.has("completed")).toBe(true);
  });
});
