import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { I18nProvider } from "@/i18n";
import type { SettingDef } from "@/lib/api-client";
import { SettingControl } from "./SettingControl";

function def(overrides: Partial<SettingDef>): SettingDef {
  return {
    key: "k",
    type: "bool",
    group: "g",
    area: "system",
    label: "Label",
    description: "",
    admin_only: true,
    options: null,
    minimum: null,
    maximum: null,
    step: null,
    secret: false,
    ...overrides,
  };
}

const authSetting = def({
  key: "auth_enabled",
  label: "Authentication",
  confirm: { value: false, message_key: "settings.confirm.auth_enabled", message: "fallback" },
});

function renderControl(d: SettingDef, value: unknown, onChange = vi.fn()) {
  render(
    <I18nProvider>
      <SettingControl def={d} value={value} onChange={onChange} />
    </I18nProvider>,
  );
  return onChange;
}

describe("SettingControl — declared confirmation", () => {
  it("turning authentication off asks first, and cancelling changes nothing", async () => {
    const onChange = renderControl(authSetting, true);

    fireEvent.click(screen.getByRole("switch", { name: "Authentication" }));

    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent("Without authentication anyone who can reach this address");
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onChange).not.toHaveBeenCalled();
  });

  it("confirming applies the value", async () => {
    const onChange = renderControl(authSetting, true);

    fireEvent.click(screen.getByRole("switch", { name: "Authentication" }));
    fireEvent.click(await screen.findByRole("button", { name: "Confirm" }));

    expect(onChange).toHaveBeenCalledWith(false);
  });

  it("the safe direction needs no confirmation", () => {
    const onChange = renderControl(authSetting, false);

    fireEvent.click(screen.getByRole("switch", { name: "Authentication" }));

    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(onChange).toHaveBeenCalledWith(true);
  });

  it("a setting without `confirm` never asks", () => {
    const onChange = renderControl(def({ label: "Open registration" }), true);
    fireEvent.click(screen.getByRole("switch", { name: "Open registration" }));
    expect(onChange).toHaveBeenCalledWith(false);
  });
});

describe("SettingControl — enum options", () => {
  it("shows translated option labels, keeps raw values, and falls back to the raw value", () => {
    renderControl(
      def({
        key: "theme",
        type: "enum",
        label: "Theme",
        options: ["light", "dark", "custom"],
        option_label_keys: ["settings.options.theme.light", "settings.options.theme.dark", "settings.options.theme.custom"],
      }),
      "dark",
    );

    const options = screen.getAllByRole("option").map((o) => [o.getAttribute("value"), o.textContent]);
    expect(options).toEqual([
      ["light", "Light"],
      ["dark", "Dark"],
      ["custom", "custom"],
    ]);
  });
});

describe("SettingControl — the policy for new accounts from a provider's sign-in", () => {
  const policy = def({
    key: "provider_login_new_accounts",
    type: "enum",
    label: "catalogue text",
    options: ["disabled", "pending", "active"],
    option_label_keys: ["disabled", "pending", "active"].map((o) => `settings.options.provider_login_new_accounts.${o}`),
  });

  it("is a generated enum control with its options worded in English, the most restrictive first", () => {
    renderControl(policy, "disabled");

    expect(screen.getAllByRole("option").map((o) => [o.getAttribute("value"), o.textContent])).toEqual([
      ["disabled", "Refuse (default)"],
      ["pending", "Create an account that waits for an administrator"],
      ["active", "Create an active account"],
    ]);
  });

  it("is worded in Italian too", () => {
    localStorage.setItem("musizeer-lang", "it");
    renderControl(policy, "pending");

    expect(screen.getAllByRole("option").map((o) => o.textContent)).toEqual([
      "Rifiuta (predefinito)",
      "Crea un account che attende un amministratore",
      "Crea un account attivo",
    ]);
    localStorage.removeItem("musizeer-lang");
  });
});
