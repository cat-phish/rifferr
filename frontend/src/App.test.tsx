import { render, screen } from "@testing-library/react";
import { App } from "./App";

test("DEV_TOOLING_9 renders the app shell placeholder", () => {
  render(<App />);
  expect(screen.getByRole("heading", { name: "Rifferr" })).toBeInTheDocument();
});
