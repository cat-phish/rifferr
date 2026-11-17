import { describe, expect, it } from "vitest";
import { webUrl } from "./webUrl";

describe("webUrl", () => {
  it("keeps only http(s) links", () => {
    expect(webUrl("https://a.example/x")).toBe("https://a.example/x");
    expect(webUrl("vscode://file/x")).toBeNull();
    expect(webUrl("javascript:alert(1)")).toBeNull();
    expect(webUrl("a.example/x")).toBeNull();
    expect(webUrl(null)).toBeNull();
  });
});
