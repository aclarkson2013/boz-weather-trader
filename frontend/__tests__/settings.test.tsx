import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { UserSettings } from "@/lib/types";

// Mock hooks
const mockUseSettings = vi.fn();
const mockUseAuthStatus = vi.fn();
const mockUseVersion = vi.fn();
const mockUseLogs = vi.fn();
vi.mock("@/lib/hooks", () => ({
  useSettings: () => mockUseSettings(),
  useAuthStatus: () => mockUseAuthStatus(),
  useCurrentWeather: () => ({ data: undefined, error: undefined }),
  useVersion: () => mockUseVersion(),
  useLogs: () => mockUseLogs(),
}));

// Mock API
const mockUpdateSettings = vi.fn();
const mockDisconnect = vi.fn();
vi.mock("@/lib/api", () => ({
  updateSettings: (...args: unknown[]) => mockUpdateSettings(...args),
  disconnect: () => mockDisconnect(),
  fetchLogs: vi.fn().mockResolvedValue([]),
}));

// Mock SWR mutate
vi.mock("swr", async () => {
  const actual = await vi.importActual("swr");
  return { ...actual, mutate: vi.fn() };
});

// Mock next/navigation
vi.mock("next/navigation", () => ({
  usePathname: () => "/settings",
}));

import SettingsPage from "@/app/settings/page";

const MOCK_SETTINGS: UserSettings = {
  trading_mode: "manual",
  max_trade_size_cents: 100,
  daily_loss_limit_cents: 1000,
  max_daily_exposure_cents: 2500,
  min_ev_threshold: 0.05,
  cooldown_per_loss_minutes: 60,
  consecutive_loss_limit: 3,
  active_cities: ["NYC", "CHI", "MIA", "AUS"],
  enabled_weather_sources: ["NWS:gridpoint", "Open-Meteo:GFS", "Open-Meteo:ICON"],
  notifications_enabled: true,
  max_contracts_per_bracket: 3,
  enable_consecutive_loss_limit: true,
  enable_per_loss_cooldown: true,
  model_weight: 0.4,
  max_model_market_divergence: 0.25,
  min_market_prob_for_yes: 0.15,
  fee_estimate_mode: "conservative",
  timezone: "",
};

describe("SettingsPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // Suppress confirm dialog
    vi.spyOn(window, "confirm").mockReturnValue(false);
    // Default version mock
    mockUseVersion.mockReturnValue({
      data: {
        current_version: "1.0.0",
        latest_version: "1.0.0",
        update_available: false,
        release_url: null,
      },
      error: undefined,
    });
    // Default auth status mock
    mockUseAuthStatus.mockReturnValue({
      data: {
        authenticated: true,
        user_id: "test-user",
        demo_mode: true,
        key_id_prefix: "abc123...",
      },
      error: undefined,
      isLoading: false,
    });
    // Default logs mock
    mockUseLogs.mockReturnValue({
      data: [],
      error: undefined,
      isLoading: false,
    });
  });

  it("shows loading state", () => {
    mockUseSettings.mockReturnValue({
      data: undefined,
      error: undefined,
      isLoading: true,
    });

    render(<SettingsPage />);
    // "Settings" appears as both heading and tab button
    const settingsElements = screen.getAllByText("Settings");
    expect(settingsElements.length).toBeGreaterThanOrEqual(2);
    const skeletons = document.querySelectorAll('[aria-hidden="true"]');
    expect(skeletons.length).toBeGreaterThan(0);
  });

  it("shows error state", () => {
    mockUseSettings.mockReturnValue({
      data: undefined,
      error: new Error("Failed to load"),
      isLoading: false,
    });

    render(<SettingsPage />);
    expect(screen.getByText("Failed to load")).toBeInTheDocument();
  });

  it("renders settings form with current values", () => {
    mockUseSettings.mockReturnValue({
      data: MOCK_SETTINGS,
      error: undefined,
      isLoading: false,
    });

    render(<SettingsPage />);

    // Trading mode
    expect(screen.getByText("Trading Mode")).toBeInTheDocument();
    expect(screen.getByText("manual")).toBeInTheDocument();
    expect(screen.getByText("auto")).toBeInTheDocument();

    // Risk limits section
    expect(screen.getByText("Risk Limits")).toBeInTheDocument();
    expect(screen.getByText("$1.00")).toBeInTheDocument(); // max trade size
    expect(screen.getByText("$10.00")).toBeInTheDocument(); // daily loss limit

    // Cities
    expect(screen.getByText("Active Cities")).toBeInTheDocument();
    expect(screen.getByText("NYC")).toBeInTheDocument();
    expect(screen.getByText("CHI")).toBeInTheDocument();

    // Notifications
    expect(screen.getByText("Notifications")).toBeInTheDocument();

    // Save button
    expect(screen.getByText("Save Settings")).toBeInTheDocument();
  });

  it("toggles trading mode", () => {
    mockUseSettings.mockReturnValue({
      data: MOCK_SETTINGS,
      error: undefined,
      isLoading: false,
    });

    render(<SettingsPage />);

    const autoButton = screen.getByText("auto");
    fireEvent.click(autoButton);

    // Check the auto button gets the active style
    expect(autoButton.className).toContain("bg-boz-primary");
  });

  it("toggles city selection", () => {
    mockUseSettings.mockReturnValue({
      data: MOCK_SETTINGS,
      error: undefined,
      isLoading: false,
    });

    render(<SettingsPage />);

    // NYC should be active initially
    const nycButton = screen.getByText("NYC");
    expect(nycButton.className).toContain("bg-boz-primary");

    // Click to deselect
    fireEvent.click(nycButton);
    expect(nycButton.className).not.toContain("bg-boz-primary");

    // Click to reselect
    fireEvent.click(nycButton);
    expect(nycButton.className).toContain("bg-boz-primary");
  });

  it("calls updateSettings on save", async () => {
    mockUpdateSettings.mockResolvedValue(MOCK_SETTINGS);
    mockUseSettings.mockReturnValue({
      data: MOCK_SETTINGS,
      error: undefined,
      isLoading: false,
    });

    render(<SettingsPage />);

    fireEvent.click(screen.getByText("Save Settings"));

    await waitFor(() => {
      expect(mockUpdateSettings).toHaveBeenCalledWith(
        expect.objectContaining({
          trading_mode: "manual",
          max_trade_size_cents: 100,
          active_cities: ["NYC", "CHI", "MIA", "AUS"],
        })
      );
    });
  });

  it("shows save success message", async () => {
    mockUpdateSettings.mockResolvedValue(MOCK_SETTINGS);
    mockUseSettings.mockReturnValue({
      data: MOCK_SETTINGS,
      error: undefined,
      isLoading: false,
    });

    render(<SettingsPage />);
    fireEvent.click(screen.getByText("Save Settings"));

    await waitFor(() => {
      expect(screen.getByText("Settings saved!")).toBeInTheDocument();
    });
  });

  it("shows save error message", async () => {
    mockUpdateSettings.mockRejectedValue(new Error("Save failed"));
    mockUseSettings.mockReturnValue({
      data: MOCK_SETTINGS,
      error: undefined,
      isLoading: false,
    });

    render(<SettingsPage />);
    fireEvent.click(screen.getByText("Save Settings"));

    await waitFor(() => {
      expect(screen.getByText("Save failed")).toBeInTheDocument();
    });
  });

  it("has disconnect button", () => {
    mockUseSettings.mockReturnValue({
      data: MOCK_SETTINGS,
      error: undefined,
      isLoading: false,
    });

    render(<SettingsPage />);
    expect(
      screen.getByText("Disconnect Kalshi Account")
    ).toBeInTheDocument();
  });

  it("shows connection status with demo badge", () => {
    mockUseSettings.mockReturnValue({
      data: MOCK_SETTINGS,
      error: undefined,
      isLoading: false,
    });

    render(<SettingsPage />);
    expect(screen.getByText("Connection Status")).toBeInTheDocument();
    expect(screen.getByText("Connected")).toBeInTheDocument();
    expect(screen.getByText("DEMO")).toBeInTheDocument();
    expect(screen.getByText("Key: abc123...")).toBeInTheDocument();
  });

  it("shows live badge when not in demo mode", () => {
    mockUseAuthStatus.mockReturnValue({
      data: {
        authenticated: true,
        user_id: "test-user",
        demo_mode: false,
        key_id_prefix: "xyz789...",
      },
      error: undefined,
      isLoading: false,
    });
    mockUseSettings.mockReturnValue({
      data: MOCK_SETTINGS,
      error: undefined,
      isLoading: false,
    });

    render(<SettingsPage />);
    expect(screen.getByText("LIVE")).toBeInTheDocument();
    expect(screen.getByText("Key: xyz789...")).toBeInTheDocument();
  });

  it("slider changes update display values", () => {
    mockUseSettings.mockReturnValue({
      data: MOCK_SETTINGS,
      error: undefined,
      isLoading: false,
    });

    render(<SettingsPage />);

    // Find max trade size slider by its associated label text
    const sliders = screen.getAllByRole("slider");
    expect(sliders.length).toBeGreaterThan(0);

    // Change the first slider (max trade size)
    fireEvent.change(sliders[0], { target: { value: "200" } });
    expect(screen.getByText("$2.00")).toBeInTheDocument();
  });

  // ─── Tab switching tests ───

  it("renders Settings and Logs tab buttons", () => {
    mockUseSettings.mockReturnValue({
      data: MOCK_SETTINGS,
      error: undefined,
      isLoading: false,
    });

    render(<SettingsPage />);

    // Both tab buttons should be present
    // "Settings" appears as both heading and tab — use getAllByText
    const settingsElements = screen.getAllByText("Settings");
    expect(settingsElements.length).toBeGreaterThanOrEqual(2); // heading + tab button
    expect(screen.getByText("Logs")).toBeInTheDocument();
  });

  it("shows settings content by default, not logs", () => {
    mockUseSettings.mockReturnValue({
      data: MOCK_SETTINGS,
      error: undefined,
      isLoading: false,
    });

    render(<SettingsPage />);

    // Settings content visible
    expect(screen.getByText("Trading Mode")).toBeInTheDocument();
    expect(screen.getByText("Risk Limits")).toBeInTheDocument();

    // Logs filter buttons should NOT be visible
    expect(screen.queryByText("WEATHER")).not.toBeInTheDocument();
  });

  it("switches to Logs tab and hides settings content", () => {
    mockUseSettings.mockReturnValue({
      data: MOCK_SETTINGS,
      error: undefined,
      isLoading: false,
    });

    render(<SettingsPage />);

    // Click the Logs tab
    fireEvent.click(screen.getByText("Logs"));

    // Settings content should be hidden
    expect(screen.queryByText("Trading Mode")).not.toBeInTheDocument();
    expect(screen.queryByText("Risk Limits")).not.toBeInTheDocument();
    expect(screen.queryByText("Save Settings")).not.toBeInTheDocument();

    // Logs empty state should be visible (since mock returns empty array)
    expect(screen.getByText("No Logs")).toBeInTheDocument();
  });

  it("switches back to Settings tab from Logs", () => {
    mockUseSettings.mockReturnValue({
      data: MOCK_SETTINGS,
      error: undefined,
      isLoading: false,
    });

    render(<SettingsPage />);

    // Switch to Logs
    fireEvent.click(screen.getByText("Logs"));
    expect(screen.queryByText("Trading Mode")).not.toBeInTheDocument();

    // Switch back to Settings — find the tab button (not the heading)
    const settingsButtons = screen.getAllByText("Settings");
    // The tab button is the one inside the tab toggle
    const tabButton = settingsButtons.find((el) =>
      el.closest(".flex.bg-gray-100")
    );
    expect(tabButton).toBeDefined();
    fireEvent.click(tabButton!);

    // Settings content should be visible again
    expect(screen.getByText("Trading Mode")).toBeInTheDocument();
    expect(screen.getByText("Save Settings")).toBeInTheDocument();
  });

  // ─── Phase 38: Bracket cap + consecutive loss toggle tests ───

  it("renders bracket cap slider with default value", () => {
    mockUseSettings.mockReturnValue({
      data: MOCK_SETTINGS,
      error: undefined,
      isLoading: false,
    });

    render(<SettingsPage />);
    expect(screen.getByText("Max Contracts Per Bracket")).toBeInTheDocument();
    const slider = screen.getByTestId("bracket-cap-slider");
    expect(slider).toBeInTheDocument();
    expect(slider).toHaveValue("3");
  });

  it("renders consecutive loss toggle", () => {
    mockUseSettings.mockReturnValue({
      data: MOCK_SETTINGS,
      error: undefined,
      isLoading: false,
    });

    render(<SettingsPage />);
    const toggle = screen.getByTestId("consecutive-loss-toggle");
    expect(toggle).toBeInTheDocument();
    // Toggle should be "on" by default (blue bg)
    expect(toggle.className).toContain("bg-boz-primary");
  });

  it("disables consecutive loss slider when toggle is off", () => {
    mockUseSettings.mockReturnValue({
      data: { ...MOCK_SETTINGS, enable_consecutive_loss_limit: false },
      error: undefined,
      isLoading: false,
    });

    render(<SettingsPage />);

    // Should show "Off" instead of the number
    expect(screen.getByText("Off")).toBeInTheDocument();

    // The toggle should have gray bg
    const toggle = screen.getByTestId("consecutive-loss-toggle");
    expect(toggle.className).toContain("bg-gray-300");
  });

  it("saves new bracket cap and toggle fields", async () => {
    mockUpdateSettings.mockResolvedValue(MOCK_SETTINGS);
    mockUseSettings.mockReturnValue({
      data: MOCK_SETTINGS,
      error: undefined,
      isLoading: false,
    });

    render(<SettingsPage />);
    fireEvent.click(screen.getByText("Save Settings"));

    await waitFor(() => {
      expect(mockUpdateSettings).toHaveBeenCalledWith(
        expect.objectContaining({
          max_contracts_per_bracket: 3,
          enable_consecutive_loss_limit: true,
        })
      );
    });
  });

  it("bracket cap slider changes value", () => {
    mockUseSettings.mockReturnValue({
      data: MOCK_SETTINGS,
      error: undefined,
      isLoading: false,
    });

    render(<SettingsPage />);
    const slider = screen.getByTestId("bracket-cap-slider");
    fireEvent.change(slider, { target: { value: "10" } });
    expect(screen.getByText("10")).toBeInTheDocument();
  });

  // ─── Phase 41: Model Guardrails tests ───

  it("renders model guardrails section with default values", () => {
    mockUseSettings.mockReturnValue({
      data: MOCK_SETTINGS,
      error: undefined,
      isLoading: false,
    });

    render(<SettingsPage />);
    expect(screen.getByText("Model Guardrails")).toBeInTheDocument();
    expect(screen.getByText("Model Weight in Blend")).toBeInTheDocument();
    expect(screen.getByText("Max Model-Market Divergence")).toBeInTheDocument();
    expect(screen.getByText("Min Market Prob for YES")).toBeInTheDocument();

    // Default sliders present
    expect(screen.getByTestId("model-weight-slider")).toHaveValue("0.4");
    expect(screen.getByTestId("max-divergence-slider")).toHaveValue("0.25");
    expect(screen.getByTestId("min-market-prob-slider")).toHaveValue("0.15");
  });

  it("model weight slider changes display value", () => {
    mockUseSettings.mockReturnValue({
      data: MOCK_SETTINGS,
      error: undefined,
      isLoading: false,
    });

    render(<SettingsPage />);
    const slider = screen.getByTestId("model-weight-slider");
    fireEvent.change(slider, { target: { value: "0.6" } });
    // Should show "60% model / 40% market" in the description
    expect(screen.getByText(/60% model/)).toBeInTheDocument();
    expect(screen.getByText(/40% market/)).toBeInTheDocument();
  });

  it("max divergence slider changes display value", () => {
    mockUseSettings.mockReturnValue({
      data: MOCK_SETTINGS,
      error: undefined,
      isLoading: false,
    });

    render(<SettingsPage />);
    const slider = screen.getByTestId("max-divergence-slider");
    fireEvent.change(slider, { target: { value: "0.35" } });
    // Should show 35% in the label and description (multiple elements)
    const matches = screen.getAllByText(/35%/);
    expect(matches.length).toBeGreaterThanOrEqual(1);
  });

  it("saves guardrail settings on save", async () => {
    mockUpdateSettings.mockResolvedValue(MOCK_SETTINGS);
    mockUseSettings.mockReturnValue({
      data: MOCK_SETTINGS,
      error: undefined,
      isLoading: false,
    });

    render(<SettingsPage />);
    fireEvent.click(screen.getByText("Save Settings"));

    await waitFor(() => {
      expect(mockUpdateSettings).toHaveBeenCalledWith(
        expect.objectContaining({
          model_weight: 0.4,
          max_model_market_divergence: 0.25,
          min_market_prob_for_yes: 0.15,
        })
      );
    });
  });

  it("min market prob slider changes display value", () => {
    mockUseSettings.mockReturnValue({
      data: MOCK_SETTINGS,
      error: undefined,
      isLoading: false,
    });

    render(<SettingsPage />);
    const slider = screen.getByTestId("min-market-prob-slider");
    fireEvent.change(slider, { target: { value: "0.2" } });
    // Should show "Skip YES trades on brackets the market prices below 20%"
    expect(screen.getByText(/below 20%/)).toBeInTheDocument();
  });

  describe("per-loss cooldown toggle", () => {
    it("renders toggle in ON state by default", () => {
      mockUseSettings.mockReturnValue({
        data: MOCK_SETTINGS,
        error: undefined,
        isLoading: false,
      });

      render(<SettingsPage />);
      const toggle = screen.getByTestId("per-loss-cooldown-toggle");
      expect(toggle).toBeInTheDocument();
      expect(toggle.className).toContain("bg-boz-primary");
      expect(screen.getByText("60 min")).toBeInTheDocument();
      expect(screen.getByText(/Pauses trading for 60 minutes after each loss/)).toBeInTheDocument();
    });

    it("toggles to OFF and shows disabled state", () => {
      mockUseSettings.mockReturnValue({
        data: MOCK_SETTINGS,
        error: undefined,
        isLoading: false,
      });

      render(<SettingsPage />);
      const toggle = screen.getByTestId("per-loss-cooldown-toggle");
      fireEvent.click(toggle);

      expect(toggle.className).toContain("bg-gray-300");
      expect(screen.getByText("Off")).toBeInTheDocument();
      expect(screen.getByText("Per-loss cooldown disabled")).toBeInTheDocument();
    });

    it("disables cooldown slider when toggle is OFF", () => {
      mockUseSettings.mockReturnValue({
        data: { ...MOCK_SETTINGS, enable_per_loss_cooldown: false },
        error: undefined,
        isLoading: false,
      });

      render(<SettingsPage />);
      const toggle = screen.getByTestId("per-loss-cooldown-toggle");
      expect(toggle.className).toContain("bg-gray-300");
      // Find the slider that's a sibling of the label containing the toggle
      const sliders = document.querySelectorAll('input[type="range"][disabled]');
      // At least one disabled slider (the cooldown one) should exist
      const cooldownSlider = Array.from(sliders).find(
        (s) => s.getAttribute("max") === "1440"
      );
      expect(cooldownSlider).toBeTruthy();
      expect(cooldownSlider).toBeDisabled();
    });
  });

  describe("Timezone Setting", () => {
    it("renders timezone dropdown with Browser Default selected", () => {
      mockUseSettings.mockReturnValue({
        data: MOCK_SETTINGS,
        error: undefined,
        isLoading: false,
      });

      render(<SettingsPage />);
      const select = screen.getByTestId("timezone-select") as HTMLSelectElement;
      expect(select).toBeInTheDocument();
      expect(select.value).toBe("");
    });

    it("renders timezone dropdown with saved timezone selected", () => {
      mockUseSettings.mockReturnValue({
        data: { ...MOCK_SETTINGS, timezone: "America/Chicago" },
        error: undefined,
        isLoading: false,
      });

      render(<SettingsPage />);
      const select = screen.getByTestId("timezone-select") as HTMLSelectElement;
      expect(select.value).toBe("America/Chicago");
    });

    it("can change timezone selection", () => {
      mockUseSettings.mockReturnValue({
        data: MOCK_SETTINGS,
        error: undefined,
        isLoading: false,
      });

      render(<SettingsPage />);
      const select = screen.getByTestId("timezone-select");
      fireEvent.change(select, { target: { value: "America/New_York" } });
      expect((select as HTMLSelectElement).value).toBe("America/New_York");
    });

    it("includes timezone in save payload", async () => {
      mockUpdateSettings.mockResolvedValue({});
      mockUseSettings.mockReturnValue({
        data: { ...MOCK_SETTINGS, timezone: "America/Denver" },
        error: undefined,
        isLoading: false,
      });

      render(<SettingsPage />);
      const saveBtn = screen.getByRole("button", { name: /save settings/i });
      fireEvent.click(saveBtn);

      await waitFor(() => {
        expect(mockUpdateSettings).toHaveBeenCalledWith(
          expect.objectContaining({ timezone: "America/Denver" })
        );
      });
    });
  });
});


describe("SettingsPage — weather sources", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockUseSettings.mockReturnValue({
      settings: MOCK_SETTINGS,
      isLoading: false,
      error: undefined,
    });
    mockUseAuthStatus.mockReturnValue({ status: undefined, isLoading: false });
    mockUseVersion.mockReturnValue({ version: undefined, isLoading: false });
    mockUseLogs.mockReturnValue({ logs: [], isLoading: false, error: undefined });
    mockUpdateSettings.mockResolvedValue(MOCK_SETTINGS);
  });

  it("renders a toggle for every weather source", () => {
    render(<SettingsPage />);
    for (const source of [
      "NWS",
      "NWS:gridpoint",
      "Open-Meteo:ECMWF",
      "Open-Meteo:GFS",
      "Open-Meteo:ICON",
    ]) {
      expect(screen.getByTestId(`weather-source-${source}`)).toBeInTheDocument();
    }
  });

  it("marks only the enabled sources as pressed", () => {
    render(<SettingsPage />);
    expect(
      screen.getByTestId("weather-source-Open-Meteo:ICON"),
    ).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("weather-source-NWS")).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });

  it("enabling a disabled source includes it in the save payload", async () => {
    render(<SettingsPage />);
    fireEvent.click(screen.getByTestId("weather-source-Open-Meteo:ECMWF"));
    fireEvent.click(screen.getByText("Save Settings"));

    await waitFor(() => {
      expect(mockUpdateSettings).toHaveBeenCalled();
    });
    const payload = mockUpdateSettings.mock.calls[0][0];
    expect(payload.enabled_weather_sources).toContain("Open-Meteo:ECMWF");
  });

  it("disabling a source removes it from the save payload", async () => {
    render(<SettingsPage />);
    fireEvent.click(screen.getByTestId("weather-source-Open-Meteo:GFS"));
    fireEvent.click(screen.getByText("Save Settings"));

    await waitFor(() => {
      expect(mockUpdateSettings).toHaveBeenCalled();
    });
    const payload = mockUpdateSettings.mock.calls[0][0];
    expect(payload.enabled_weather_sources).not.toContain("Open-Meteo:GFS");
  });

  it("blocks dropping below the two-source minimum", () => {
    render(<SettingsPage />);
    // 3 enabled -> disable one, leaving 2 (the minimum)
    fireEvent.click(screen.getByTestId("weather-source-Open-Meteo:GFS"));
    // The remaining two must now be disabled controls
    const icon = screen.getByTestId("weather-source-Open-Meteo:ICON");
    expect(icon).toBeDisabled();
    fireEvent.click(icon);
    expect(icon).toHaveAttribute("aria-pressed", "true");
  });
});
