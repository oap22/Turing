import { describe, expect, it } from "vitest";

import { wsBadgeFor } from "../App";

describe("wsBadgeFor", () => {
  it("says nothing before the gateway has ever answered", () => {
    // The cold-start case: driving the app with no gateway running should not
    // paint a permanent amber warning in the chrome.
    expect(wsBadgeFor("reconnecting", false)).toBeNull();
    expect(wsBadgeFor("closed", false)).toBeNull();
    expect(wsBadgeFor("open", false)).toBeNull();
  });

  it("stays silent while a connection is healthy", () => {
    expect(wsBadgeFor("open", true)).toBeNull();
  });

  it("speaks only once a live connection drops", () => {
    expect(wsBadgeFor("closed", true)).toBe("closed");
    expect(wsBadgeFor("reconnecting", true)).toBe("reconnecting");
  });
});
