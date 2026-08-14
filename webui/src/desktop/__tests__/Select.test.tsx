// Tests for the custom single-select listbox that replaced native <select>
// (see Select.tsx for why: WKWebView draws OS chrome around <select> popups
// that CSS can't reach). Covers the trigger's displayed/accessible value,
// opening via click and keyboard, cursor-vs-commit separation, Escape/
// outside-click dismissal, aria-activedescendant tracking, typeahead, and the
// empty-options edge case.

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import Select from "../Select";

// jsdom has no layout engine and so no scrollIntoView; the keep-the-cursor-
// visible effect calls it on every cursor move.
beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn();
});

afterEach(cleanup);

const OPTIONS = [
  { value: "a", label: "alpha" },
  { value: "b", label: "bravo" },
  { value: "c", label: "charlie" },
];

function setup(value = "a", onChange = vi.fn()) {
  render(
    <Select value={value} options={OPTIONS} onChange={onChange} label="theme" />,
  );
  return { onChange };
}

describe("Select", () => {
  it("renders the current value on the trigger", () => {
    setup("b");
    expect(screen.getByRole("button")).toHaveTextContent("bravo");
  });

  it("opens on click", () => {
    setup();
    fireEvent.click(screen.getByRole("button"));
    expect(screen.getByRole("listbox")).toBeInTheDocument();
  });

  it("opens on Enter from the trigger", () => {
    setup();
    fireEvent.keyDown(screen.getByRole("button"), { key: "Enter" });
    expect(screen.getByRole("listbox")).toBeInTheDocument();
  });

  it("opens on ArrowDown from the trigger", () => {
    setup();
    fireEvent.keyDown(screen.getByRole("button"), { key: "ArrowDown" });
    expect(screen.getByRole("listbox")).toBeInTheDocument();
  });

  it("moves the cursor with ArrowDown/ArrowUp without firing onChange", () => {
    const { onChange } = setup("a");
    fireEvent.click(screen.getByRole("button"));
    const list = screen.getByRole("listbox");
    fireEvent.keyDown(list, { key: "ArrowDown" });
    fireEvent.keyDown(list, { key: "ArrowDown" });
    fireEvent.keyDown(list, { key: "ArrowUp" });
    expect(onChange).not.toHaveBeenCalled();
  });

  it("commits the cursor's option on Enter, firing onChange exactly once", () => {
    const { onChange } = setup("a");
    fireEvent.click(screen.getByRole("button"));
    const list = screen.getByRole("listbox");
    fireEvent.keyDown(list, { key: "ArrowDown" }); // cursor -> bravo
    fireEvent.keyDown(list, { key: "Enter" });
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange).toHaveBeenCalledWith("b");
  });

  it("closes on Escape and returns focus to the trigger", () => {
    setup();
    const trigger = screen.getByRole("button");
    fireEvent.click(trigger);
    const list = screen.getByRole("listbox");
    fireEvent.keyDown(list, { key: "Escape" });
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it("closes when clicking outside", () => {
    setup();
    fireEvent.click(screen.getByRole("button"));
    expect(screen.getByRole("listbox")).toBeInTheDocument();
    fireEvent.pointerDown(document.body);
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });

  it("tracks the cursor via aria-activedescendant", () => {
    setup("a");
    fireEvent.click(screen.getByRole("button"));
    const list = screen.getByRole("listbox");
    const options = screen.getAllByRole("option");
    expect(list.getAttribute("aria-activedescendant")).toBe(options[0].id);
    fireEvent.keyDown(list, { key: "ArrowDown" });
    expect(list.getAttribute("aria-activedescendant")).toBe(options[1].id);
  });

  it("moves the cursor to the first option matching typed characters", () => {
    setup("a");
    fireEvent.click(screen.getByRole("button"));
    const list = screen.getByRole("listbox");
    fireEvent.keyDown(list, { key: "c" }); // -> charlie
    const options = screen.getAllByRole("option");
    expect(list.getAttribute("aria-activedescendant")).toBe(options[2].id);
  });

  it("renders without crashing when options is empty", () => {
    render(<Select value="" options={[]} onChange={vi.fn()} label="empty" />);
    fireEvent.click(screen.getByRole("button"));
    expect(screen.getByRole("listbox")).toBeInTheDocument();
    expect(screen.queryAllByRole("option")).toHaveLength(0);
  });
});
